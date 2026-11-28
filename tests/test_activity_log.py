from __future__ import annotations

import dataclasses
import json
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, get_args

import pytest

import hermes_topic_memory.activity_log as activity_log
from hermes_topic_memory.activity_log import (
    ActivityAction, ActivityLedger, ActivityLogCorruptionError, ActivityRecord,
    ActivitySource, ActivityStatus,
)


def _record(index: int = 1, **changes: Any) -> ActivityRecord:
    fields = {
        "action": "recall", "source": "mcp", "actor": "token:env-admin",
        "topic_id": "alpha", "status": "ok", "latency_ms": 12,
        "summary": "Recall completed", "meta": {"version": 2, "cache": False},
        "event_id": f"{index:032x}", "created_at": "2026-08-11T08:00:00Z",
    }
    fields.update(changes)
    return ActivityRecord(**fields)


def test_record_defaults_and_frozen_detached_metadata() -> None:
    supplied = {"topic_ids": ["alpha"], "topic_versions": {"alpha": 2}}
    record = ActivityRecord(action="recall", source="mcp", meta=supplied)
    assert len(record.event_id) == 32
    int(record.event_id, 16)
    assert record.created_at.endswith("Z")
    assert record.actor == "anonymous"
    assert record.topic_id is None
    assert record.status == "ok"
    assert record.latency_ms == 0
    assert record.summary == ""
    supplied["topic_ids"].append("beta")
    supplied["topic_versions"]["alpha"] = 9
    public = record.to_dict()
    assert public["meta"] == {"topic_ids": ["alpha"], "topic_versions": {"alpha": 2}}
    public["meta"]["topic_ids"].append("beta")
    assert record.to_dict()["meta"]["topic_ids"] == ["alpha"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.actor = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        record.meta["version"] = 9  # type: ignore[index]


def test_reopening_month_rotation_append_only_and_ordering(tmp_path: Path) -> None:
    ledger = ActivityLedger(tmp_path)
    oldest = _record(1, created_at="2026-07-31T23:59:59Z")
    first = ledger.append(oldest)
    original = (tmp_path / "activity-log" / "2026-07.jsonl").read_bytes()
    middle = ledger.append(_record(2, created_at="2026-08-01T00:00:00Z"))
    newest = ledger.append(_record(3, created_at="2026-09-01T00:00:00Z"))
    assert first is oldest
    reopened = ActivityLedger(tmp_path)
    assert reopened.list() == {
        "items": [newest.to_dict(), middle.to_dict(), oldest.to_dict()],
        "page": 1, "page_size": 50, "total": 3,
    }
    later_in_july = reopened.append(_record(4, created_at="2026-07-31T23:59:59.500000Z"))
    july = tmp_path / "activity-log" / "2026-07.jsonl"
    assert july.read_bytes().startswith(original)
    assert july.read_bytes().count(b"\n") == 2
    assert reopened.list()["items"][-2:] == [later_in_july.to_dict(), oldest.to_dict()]
    assert len(tuple((tmp_path / "activity-log").glob("*.jsonl"))) == 3


def test_timestamp_ties_use_event_id_for_stable_order(tmp_path: Path) -> None:
    ledger = ActivityLedger(tmp_path)
    for index in (2, 1, 3):
        ledger.append(_record(index))
    assert [item["event_id"] for item in ledger.list()["items"]] == [f"{i:032x}" for i in (3, 2, 1)]


def test_append_locks_and_fsyncs_record_directory_and_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "activity-log"
    directory.mkdir(mode=0o755)
    path = directory / "2026-08.jsonl"
    path.touch(mode=0o644)
    locks: list[int] = []
    synced: list[int] = []
    real_flock = activity_log.fcntl.flock
    real_fsync = activity_log.os.fsync

    def flock(fd: int, operation: int) -> None:
        locks.append(operation)
        real_flock(fd, operation)

    def fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(activity_log.fcntl, "flock", flock)
    monkeypatch.setattr(activity_log.os, "fsync", fsync)
    ledger = ActivityLedger(tmp_path)
    ledger.append(_record())
    assert locks == [activity_log.fcntl.LOCK_EX, activity_log.fcntl.LOCK_UN]
    assert len(synced) == 3
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert b": " not in path.read_bytes()
    assert path.read_bytes().endswith(b"\n")
    locks.clear()
    ledger.list()
    assert locks == [activity_log.fcntl.LOCK_SH, activity_log.fcntl.LOCK_UN]


def test_concurrent_instances_append_complete_distinct_records(tmp_path: Path) -> None:
    def append(index: int) -> None:
        ActivityLedger(tmp_path).append(_record(index))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(1, 81)))
    result = ActivityLedger(tmp_path).list(page_size=100)
    assert result["total"] == 80
    assert len({item["event_id"] for item in result["items"]}) == 80
    lines = (tmp_path / "activity-log" / "2026-08.jsonl").read_bytes().splitlines()
    assert len(lines) == 80
    assert all(ActivityRecord.from_dict(json.loads(line)) for line in lines)


@pytest.mark.parametrize("operation", ["append", "list"])
def test_real_exclusive_lock_blocks_readers_and_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    ledger = ActivityLedger(tmp_path)
    ledger.append(_record())
    path = tmp_path / "activity-log" / "2026-08.jsonl"
    attempted = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []
    real_flock = activity_log.fcntl.flock

    def tracking_flock(fd: int, op: int) -> None:
        if op in {activity_log.fcntl.LOCK_EX, activity_log.fcntl.LOCK_SH}:
            attempted.set()
        real_flock(fd, op)

    def worker() -> None:
        try:
            if operation == "append":
                ledger.append(_record(2))
            else:
                ledger.list()
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    with path.open("rb") as held:
        real_flock(held.fileno(), activity_log.fcntl.LOCK_EX)
        monkeypatch.setattr(activity_log.fcntl, "flock", tracking_flock)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            assert attempted.wait(3)
            assert not finished.is_set()
        finally:
            real_flock(held.fileno(), activity_log.fcntl.LOCK_UN)
            thread.join(3)
    assert finished.is_set()
    assert not thread.is_alive()
    assert not failures


def test_conjunctive_filters_casefold_search_and_pagination(tmp_path: Path) -> None:
    ledger = ActivityLedger(tmp_path)
    for index, changes in enumerate((
        {}, {"source": "rest"}, {"status": "no_fact"}, {"topic_id": "beta"},
        {"action": "remember"}, {"summary": "Unicode CAFÉ match", "meta": {"source_agent": "agent-one"}},
    ), start=1):
        ledger.append(_record(index, **changes))
    matching = ledger.list(action="recall", source="mcp", topic_id="alpha", status="ok", q="café")
    assert matching["total"] == 1
    assert matching["items"][0]["event_id"] == f"{6:032x}"
    assert ledger.list(q="AGENT-ONE")["total"] == 1
    assert ledger.list(q="no match")["items"] == []
    assert ledger.list(source="rest")["total"] == 1
    assert ledger.list(status="no_fact")["total"] == 1
    assert ledger.list(topic_id="beta")["total"] == 1
    assert ledger.list(action="remember")["total"] == 1
    assert ledger.list(page=2, page_size=2) == {
        "items": [_record(4, topic_id="beta").to_dict(), _record(3, status="no_fact").to_dict()],
        "page": 2, "page_size": 2, "total": 6,
    }
    assert ledger.list(page=100, page_size=100)["items"] == []
    assert ledger.list(q="  ")["total"] == 6
    assert ActivityLedger(tmp_path / "empty").list()["total"] == 0


@pytest.mark.parametrize("field,value", [
    ("page", 0), ("page", -1), ("page", True), ("page", "1"),
    ("page_size", 0), ("page_size", -1), ("page_size", 101), ("page_size", False),
    ("page_size", 1.5), ("action", "unknown"), ("source", "hermes"),
    ("status", "answered"), ("topic_id", "Bad/Topic"), ("q", "x" * 301),
    ("q", 3), ("q", "line\nbreak"),
])
def test_invalid_filter_and_pagination_boundaries(tmp_path: Path, field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        ActivityLedger(tmp_path).list(**{field: value})


@pytest.mark.parametrize("field,value", [
    ("action", "unknown"), ("source", "hermes"), ("status", "answered"),
    ("event_id", "not-a-uuid"), ("event_id", "A" * 32), ("event_id", "a" * 31),
    ("created_at", "2026-08-01T00:00:00+00:00"), ("created_at", "2026-08-01T00:00:00+01:00"),
    ("created_at", "2026-08-01"), ("created_at", "not-a-timeZ"), ("created_at", 1),
    ("topic_id", "Bad/Topic"), ("topic_id", "alpha--beta"), ("topic_id", "a" * 129),
    ("latency_ms", -1), ("latency_ms", True), ("latency_ms", 1.1),
    ("actor", "x" * 129), ("actor", ""), ("actor", "name\nheader"),
    ("summary", "x" * 301), ("summary", "body\x00text"), ("meta", []),
    ("meta", {"cache": object()}), ("meta", {"cache": float("nan")}),
    ("meta", {"source_agent": "x" * 161}), ("meta", {"cache": [1, 2]}),
    ("meta", {"topic_ids": ["Bad/Topic"]}), ("meta", {"topic_versions": {"alpha": True}}),
    ("meta", {"unknown": "x" * 4096}), ("meta", {str(i): i for i in range(33)}),
    ("meta", {"unknown": {"a": {"b": {"c": 1}}}}),
])
def test_schema_rejects_invalid_or_unbounded_values(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        _record(**{field: value})


def test_schema_accepts_exact_text_bounds_and_all_enumerations() -> None:
    assert _record(actor="a" * 128, summary="x" * 300).summary == "x" * 300
    for action in get_args(ActivityAction):
        assert _record(action=action).action == action
    for source in get_args(ActivitySource):
        assert _record(source=source).source == source
    for status in get_args(ActivityStatus):
        assert _record(status=status).status == status


def test_secret_redaction_and_request_metadata_discarded_before_persistence(tmp_path: Path) -> None:
    record = _record(
        actor="Bearer hidden-actor-value",
        summary="Bearer hidden-bearer-value password=hidden-password cookie:hidden-cookie PRIVATE_CREDENTIAL",
        meta={"source_agent": "token=hidden-agent-token", "password": "hidden-meta-password",
              "request": {"fact": "private raw fact"}, "query": "private raw question",
              "path": "/api/topics/{topic_id}", "version": 2},
    )
    ActivityLedger(tmp_path).append(record)
    serialized = (tmp_path / "activity-log" / "2026-08.jsonl").read_text()
    for secret in ("hidden-actor-value", "hidden-bearer-value", "hidden-password", "hidden-cookie",
                   "PRIVATE_CREDENTIAL", "hidden-agent-token", "hidden-meta-password", "private raw fact", "private raw question"):
        assert secret not in serialized
    assert record.actor == "[redacted]"
    assert record.meta["source_agent"] == "[redacted]"
    assert "request" not in record.meta
    assert ActivityLedger(tmp_path).list()["items"] == [record.to_dict()]


@pytest.mark.parametrize("payload", [
    b"{broken\n", b"\xff\n", b"{}\n", b"[]\n",
    json.dumps(_record(created_at="2026-09-01T00:00:00Z").to_dict()).encode() + b"\n",
    json.dumps({**_record().to_dict(), "status": "unknown"}).encode() + b"\n",
    json.dumps({**_record().to_dict(), "summary": "Bearer unsafe-stored-secret"}).encode() + b"\n",
    json.dumps({**_record().to_dict(), "meta": {"password": "unsafe-stored-secret"}}).encode() + b"\n",
])
def test_corruption_is_explicit_and_does_not_leak_payload(tmp_path: Path, payload: bytes) -> None:
    directory = tmp_path / "activity-log"
    directory.mkdir()
    (directory / "2026-08.jsonl").write_bytes(b"\n" + payload)
    with pytest.raises(ActivityLogCorruptionError) as caught:
        ActivityLedger(tmp_path).list(q="no matching records")
    assert str(caught.value) == "invalid activity record 2026-08.jsonl:2"
    assert "unsafe-stored-secret" not in str(caught.value)


def test_non_month_files_are_ignored_and_append_requires_record(tmp_path: Path) -> None:
    directory = tmp_path / "activity-log"
    directory.mkdir()
    (directory / "notes.jsonl").write_text("corrupt private text")
    (directory / "2026-08.jsonl").mkdir()
    assert ActivityLedger(tmp_path).list()["items"] == []
    with pytest.raises(TypeError):
        ActivityLedger(tmp_path).append({})  # type: ignore[arg-type]


def test_partial_write_is_reported_not_claimed_durable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity_log.os, "write", lambda descriptor, payload: len(payload) - 1)
    with pytest.raises(OSError, match="incomplete activity record write"):
        ActivityLedger(tmp_path).append(_record())


@pytest.mark.parametrize("actor", ["token:env-admin", "sso:openid-prefix", "sso:安全昵称", "anonymous"])
def test_safe_actor_identity_round_trips_exactly(tmp_path: Path, actor: str) -> None:
    record = _record(actor=actor)
    ActivityLedger(tmp_path).append(record)
    assert ActivityLedger(tmp_path).list()["items"][0]["actor"] == actor


@pytest.mark.parametrize("credential", [
    'password="hidden quoted password"', "token='hidden quoted token'",
    'password="hidden truncated credential', "Cookie: session=hidden-session; csrf=hidden-csrf",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhbm9uIn0.signature-value",
    "https://private-user:hidden-password@example.com/path",
])
def test_common_credential_forms_are_redacted(credential: str) -> None:
    record = _record(summary=credential, meta={"source_agent": credential})
    public = json.dumps(record.to_dict())
    assert "hidden" not in public
    assert "private-user" not in public
    assert "signature-value" not in public


def test_reopened_ledger_tightens_existing_read_permissions(tmp_path: Path) -> None:
    ledger = ActivityLedger(tmp_path)
    ledger.append(_record())
    directory = tmp_path / "activity-log"
    path = directory / "2026-08.jsonl"
    directory.chmod(0o755)
    path.chmod(0o644)
    reopened = ActivityLedger(tmp_path)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert reopened.list()["total"] == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_activity_enumerations_match_the_public_schema() -> None:
    assert set(get_args(ActivityAction)) == {
        "recall", "remember", "dream", "bind_topic", "list_topics", "whoami",
        "topic_create", "topic_edit", "topic_append", "topic_rollback", "topic_archive",
        "topic_restore", "topic_trash", "topic_delete", "core_init", "core_edit",
        "core_rollback", "auth_failed", "sso_login", "sso_logout", "key_create", "key_revoke",
    }
    assert set(get_args(ActivitySource)) == {"mcp", "rest", "panel", "studio", "cli", "system"}
    assert set(get_args(ActivityStatus)) == {"ok", "no_fact", "duplicate", "skipped", "error", "denied"}


@pytest.mark.parametrize("action", ["key_create", "key_revoke"])
def test_key_events_redact_managed_secrets_from_every_text_field(tmp_path: Path, action: str) -> None:
    secret = "htm_" + "Ab_cd-09" * 4
    record = _record(
        action=action, actor=f"key:{secret}(key_0123456789ab)",
        summary=f"created ({secret})",
        meta={"key_id": "key_0123456789ab", "source_agent": f"agent-{secret}",
              "path": f"/api/{secret}", "request": {"secret": secret}},
    )
    ActivityLedger(tmp_path).append(record)
    public = ActivityLedger(tmp_path).list()["items"][0]
    serialized = json.dumps(public)
    assert secret not in serialized
    assert "Ab_cd-09" not in serialized
    assert "[redacted]" in public["actor"]
    assert "[redacted]" in public["summary"]
    assert "[redacted]" in public["meta"]["source_agent"]
    assert "[redacted]" in public["meta"]["path"]
    assert public["meta"]["key_id"] == "key_0123456789ab"
    assert "request" not in public["meta"]


def test_scoped_activity_pagination_excludes_partial_multi_topic_records(tmp_path: Path) -> None:
    ledger = ActivityLedger(tmp_path)
    for index, changes in enumerate((
        {"topic_id": "alpha"},
        {"topic_id": None, "action": "whoami"},
        {"topic_id": None, "meta": {"topic_ids": ["alpha", "beta"]}},
        {"topic_id": "beta"},
        {"topic_id": None, "meta": {"topic_ids": ["alpha"]}},
    ), start=1):
        ledger.append(_record(index, **changes))
    scope = frozenset({"alpha"})
    first = ledger.list(allowed_topics=scope, page_size=2)
    second = ledger.list(allowed_topics=scope, page=2, page_size=2)
    assert first["total"] == second["total"] == 3
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert {item["event_id"] for item in first["items"] + second["items"]} == {
        f"{index:032x}" for index in (1, 2, 5)
    }
    assert ledger.list(allowed_topics=scope, topic_id="beta")["total"] == 0


@pytest.mark.parametrize("action", ["recall", "remember", "dream", "bind_topic", "list_topics"])
def test_scoped_ledger_hides_topic_actions_without_complete_topic_ids(tmp_path: Path, action: str) -> None:
    ledger = ActivityLedger(tmp_path)
    unknown = _record(1, action=action, topic_id=None, meta={"topic_count": 9})
    allowed = _record(2, action=action, topic_id=None, meta={"topic_ids": ["alpha"], "topic_count": 1})
    mixed = _record(3, action=action, topic_id=None, meta={"topic_ids": ["alpha", "beta"], "topic_count": 2})
    global_record = _record(4, action="whoami", topic_id=None)
    for record in (unknown, allowed, mixed, global_record):
        ledger.append(record)
    result = ledger.list(allowed_topics=frozenset({"alpha"}), page_size=1)
    assert result["total"] == 2
    assert result["items"] == [global_record.to_dict()]
    assert ledger.list(allowed_topics=frozenset({"alpha"}), page=2, page_size=1)["items"] == [allowed.to_dict()]
    assert ledger.list()["total"] == 4
