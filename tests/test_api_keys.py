from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import hermes_topic_memory.api_keys as api_keys
from hermes_topic_memory.api_keys import (
    ApiKeyCorruptionError, ApiKeyError, ApiKeyNotFoundError, ApiKeyRecord, ApiKeyStore,
)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, datetime]:
    value = {"now": datetime(2026, 8, 11, 8, 0, tzinfo=UTC)}
    monkeypatch.setattr(api_keys, "_utc_now", lambda: value["now"])
    return value


def _create(store: ApiKeyStore, **changes: Any) -> tuple[ApiKeyRecord, str]:
    fields = {"name": "Agent", "role": "read", "created_by": "token:env-admin"}
    fields.update(changes)
    return store.create_key(**fields)


def _document(root: Path) -> dict[str, Any]:
    return json.loads((root / "api-keys.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("role", ["read", "write", "admin"])
def test_creation_hash_only_storage_and_public_metadata(
    tmp_path: Path, clock: dict[str, datetime], role: str,
) -> None:
    store = ApiKeyStore(tmp_path)
    record, secret = _create(
        store, name="  Research Agent  ", role=role,
        allowed_topics=["alpha", "beta-2"], expires_in_days=7,
    )
    assert re.fullmatch(r"htm_[A-Za-z0-9_-]{32}", secret)
    assert re.fullmatch(r"key_[0-9a-f]{12}", record.key_id)
    assert record.name == "Research Agent"
    assert record.role == role
    assert record.allowed_topics == ("alpha", "beta-2")
    assert record.prefix == secret[:12]
    assert record.key_hash == hashlib.sha256(secret.encode("utf-8")).hexdigest()
    assert record.created_at == "2026-08-11T08:00:00Z"
    assert record.expires_at == "2026-08-18T08:00:00Z"
    assert record.created_by == "token:env-admin"
    assert record.last_used_at is None
    assert record.revoked_at is None
    assert record.status == "active"
    public = record.to_dict()
    assert public == {
        "key_id": record.key_id, "name": "Research Agent", "prefix": secret[:12],
        "role": role, "allowed_topics": ["alpha", "beta-2"],
        "created_at": "2026-08-11T08:00:00Z", "created_by": "token:env-admin",
        "expires_at": "2026-08-18T08:00:00Z", "last_used_at": None,
        "revoked_at": None, "status": "active",
    }
    assert "key_hash" not in public
    assert "secret" not in public
    assert secret not in json.dumps(public)
    assert record.key_hash not in repr(record)
    document = _document(tmp_path)
    assert document["version"] == 1
    stored = document["keys"][0]
    assert stored["key_hash"] == record.key_hash
    assert stored["prefix"] == secret[:12]
    assert set(stored) == set(ApiKeyRecord.__dataclass_fields__)
    assert "status" not in stored
    assert "secret" not in stored
    for path in tmp_path.iterdir():
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
    assert ApiKeyRecord.from_dict(stored) == record
    assert ApiKeyStore(tmp_path).list_keys() == [record]


def test_empty_initialization_is_private_and_reopening_does_not_reset(
    tmp_path: Path, clock: dict[str, datetime],
) -> None:
    root = tmp_path / "nested" / "data"
    store = ApiKeyStore(root)
    assert store.list_keys() == []
    assert _document(root) == {"version": 1, "keys": []}
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    path = root / "api-keys.json"
    lock = root / ".api-keys.lock"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    lock_inode = lock.stat().st_ino
    record, _ = _create(store)
    assert record.allowed_topics is None
    assert record.expires_at is None
    assert record.to_dict()["allowed_topics"] is None
    saved = path.read_bytes()
    path.chmod(0o644)
    lock.chmod(0o644)
    reopened = ApiKeyStore(root)
    assert reopened.list_keys() == [record]
    assert path.read_bytes() == saved
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert lock.stat().st_ino == lock_inode
    reopened.revoke_key(record.key_id)
    assert lock.stat().st_ino == lock_inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not tuple(root.glob("*.tmp"))


def test_record_and_returned_scope_are_detached_and_immutable(
    tmp_path: Path, clock: dict[str, datetime],
) -> None:
    supplied = ["alpha"]
    store = ApiKeyStore(tmp_path)
    record, _ = _create(store, allowed_topics=supplied)
    supplied.append("beta")
    assert record.allowed_topics == ("alpha",)
    public = record.to_dict()
    public["allowed_topics"].append("gamma")
    assert record.to_dict()["allowed_topics"] == ["alpha"]
    records = store.list_keys()
    records.clear()
    assert store.list_keys() == [record]
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.role = "admin"  # type: ignore[misc]


@pytest.mark.parametrize("changes", [
    {"name": None}, {"name": 1}, {"name": ""}, {"name": "   "},
    {"name": "x" * 81}, {"name": "\tAgent"}, {"name": "Agent\n"},
    {"name": "A\x00B"}, {"name": "A\x7fB"}, {"name": "A\x85B"},
    {"name": "A\u200bB"}, {"name": "A\u202eB"}, {"name": "A\ud800B"},
    {"name": "A\u2028B"}, {"name": "A\u2029B"},
    {"role": "READ"}, {"role": "owner"}, {"role": None},
    {"role": []}, {"role": True}, {"role": "read "},
    {"created_by": ""}, {"created_by": None}, {"created_by": 1},
    {"created_by": "x" * 129}, {"created_by": "sso:\nadmin"},
    {"created_by": "sso:\u200badmin"}, {"created_by": "sso:\ud800admin"},
    {"allowed_topics": []}, {"allowed_topics": ()}, {"allowed_topics": "alpha"},
    {"allowed_topics": {"alpha"}}, {"allowed_topics": ["alpha", "alpha"]},
    {"allowed_topics": [1]}, {"allowed_topics": [None]}, {"allowed_topics": [True]},
    {"allowed_topics": ["Alpha"]}, {"allowed_topics": ["alpha_beta"]},
    {"allowed_topics": ["alpha--beta"]}, {"allowed_topics": ["../alpha"]},
    {"allowed_topics": ["-alpha"]}, {"allowed_topics": ["alpha-"]},
    {"allowed_topics": [" alpha"]}, {"allowed_topics": ["alpha\n"]},
    {"allowed_topics": [""]}, {"allowed_topics": ["x" * 129]},
    {"expires_in_days": 0}, {"expires_in_days": -1}, {"expires_in_days": 1.5},
    {"expires_in_days": "7"}, {"expires_in_days": True}, {"expires_in_days": False},
    {"expires_in_days": []}, {"expires_in_days": 10**100},
])
def test_invalid_creation_does_not_change_store(
    tmp_path: Path, clock: dict[str, datetime], changes: dict[str, Any],
) -> None:
    store = ApiKeyStore(tmp_path)
    _create(store)
    before = (tmp_path / "api-keys.json").read_bytes()
    with pytest.raises(ValueError):
        _create(store, **changes)
    assert (tmp_path / "api-keys.json").read_bytes() == before


@pytest.mark.parametrize("topics", [None, ["alpha", "beta-2", "3"], ("x" * 128,)])
def test_valid_unicode_and_input_boundaries(
    tmp_path: Path, clock: dict[str, datetime], topics: Any,
) -> None:
    record, _ = _create(
        ApiKeyStore(tmp_path), name=" " + "研" * 80 + " ",
        created_by=" " + "a" * 128 + " ", allowed_topics=topics,
        expires_in_days=1,
    )
    assert record.name == "研" * 80
    assert record.created_by == "a" * 128
    assert record.allowed_topics == (tuple(topics) if topics is not None else None)
    assert record.expires_at == "2026-08-12T08:00:00Z"


def test_authentication_active_unknown_and_malformed_tokens(
    tmp_path: Path, clock: dict[str, datetime], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ApiKeyStore(tmp_path)
    first, secret = _create(store)
    _create(store, name="Other")
    _create(store, name="Third")
    comparisons: list[tuple[str, str]] = []
    real_compare = api_keys.hmac.compare_digest

    def compare(left: str, right: str) -> bool:
        comparisons.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(api_keys.hmac, "compare_digest", compare)
    authenticated = ApiKeyStore(tmp_path).authenticate(secret)
    assert authenticated is not None
    assert authenticated.key_id == first.key_id
    assert authenticated.last_used_at == "2026-08-11T08:00:00Z"
    assert len(comparisons) == 3
    assert all(len(left) == len(right) == 64 for left, right in comparisons)
    comparisons.clear()
    before = (tmp_path / "api-keys.json").read_bytes()
    assert store.authenticate("htm_" + "!" * 32) is None
    assert store.authenticate("htm_" + "a" * 31) is None
    assert store.authenticate("htm_" + "a" * 33) is None
    assert store.authenticate(secret + "\n") is None
    assert store.authenticate("") is None
    assert store.authenticate(None) is None  # type: ignore[arg-type]
    assert store.authenticate(123) is None  # type: ignore[arg-type]
    assert comparisons == []
    unknown = "htm_" + ("a" if secret != "htm_" + "a" * 32 else "b") * 32
    assert store.authenticate(unknown) is None
    assert len(comparisons) == 3
    assert (tmp_path / "api-keys.json").read_bytes() == before


def test_last_use_is_durable_and_throttled_at_exact_sixty_seconds(
    tmp_path: Path, clock: dict[str, datetime], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ApiKeyStore(tmp_path)
    record, secret = _create(store)
    initial = clock["now"]
    writes: list[list[ApiKeyRecord]] = []
    real_write = store._write_unlocked

    def write(records: list[ApiKeyRecord]) -> None:
        writes.append(records.copy())
        real_write(records)

    monkeypatch.setattr(store, "_write_unlocked", write)
    first = store.authenticate(secret)
    assert first is not None
    assert first.last_used_at == record.created_at
    assert len(writes) == 1
    saved = (tmp_path / "api-keys.json").read_bytes()
    for elapsed in (0, 1, 59, 59.999999, -1):
        clock["now"] = initial + timedelta(seconds=elapsed)
        current = store.authenticate(secret)
        assert current is not None
        assert current.last_used_at == first.last_used_at
        assert (tmp_path / "api-keys.json").read_bytes() == saved
    assert len(writes) == 1
    clock["now"] = initial + timedelta(seconds=60)
    second = store.authenticate(secret)
    assert second is not None
    assert second.last_used_at == "2026-08-11T08:01:00Z"
    assert len(writes) == 2
    assert ApiKeyStore(tmp_path).list_keys()[0].last_used_at == second.last_used_at
    clock["now"] = initial + timedelta(seconds=119)
    assert store.authenticate(secret) == second
    assert len(writes) == 2
    clock["now"] = initial + timedelta(seconds=120)
    assert store.authenticate(secret).last_used_at == "2026-08-11T08:02:00Z"  # type: ignore[union-attr]
    assert len(writes) == 3


def test_expiry_is_utc_boundary_and_never_updates_expired_key(
    tmp_path: Path, clock: dict[str, datetime],
) -> None:
    store = ApiKeyStore(tmp_path)
    record, secret = _create(store, expires_in_days=1)
    expiry = clock["now"] + timedelta(days=1)
    clock["now"] = expiry - timedelta(microseconds=1)
    assert record.to_dict()["status"] == "active"
    assert store.authenticate(secret) is not None
    before = (tmp_path / "api-keys.json").read_bytes()
    clock["now"] = expiry.astimezone(timezone(timedelta(hours=8)))
    assert record.status == "expired"
    assert record.to_dict()["status"] == "expired"
    assert store.authenticate(secret) is None
    clock["now"] = expiry + timedelta(days=100)
    assert ApiKeyStore(tmp_path).authenticate(secret) is None
    assert (tmp_path / "api-keys.json").read_bytes() == before
    never, never_secret = _create(store, expires_in_days=None)
    clock["now"] += timedelta(days=1000)
    assert never.status == "active"
    assert store.authenticate(never_secret) is not None


def test_revoke_is_durable_idempotent_and_dominates_expiry(
    tmp_path: Path, clock: dict[str, datetime],
) -> None:
    store = ApiKeyStore(tmp_path)
    record, secret = _create(store, expires_in_days=1)
    used = store.authenticate(secret)
    clock["now"] += timedelta(seconds=10)
    revoked = store.revoke_key(record.key_id)
    assert revoked.revoked_at == "2026-08-11T08:00:10Z"
    assert revoked.last_used_at == used.last_used_at  # type: ignore[union-attr]
    assert revoked.status == "revoked"
    before = (tmp_path / "api-keys.json").read_bytes()
    clock["now"] += timedelta(days=2)
    reopened = ApiKeyStore(tmp_path)
    assert reopened.authenticate(secret) is None
    assert reopened.revoke_key(record.key_id) == revoked
    assert reopened.list_keys()[0].to_dict()["status"] == "revoked"
    assert (tmp_path / "api-keys.json").read_bytes() == before


def test_expired_key_can_be_revoked_without_authenticating(
    tmp_path: Path, clock: dict[str, datetime],
) -> None:
    store = ApiKeyStore(tmp_path)
    record, secret = _create(store, expires_in_days=1)
    clock["now"] += timedelta(days=1)
    assert record.status == "expired"
    assert store.authenticate(secret) is None
    revoked = store.revoke_key(record.key_id)
    assert revoked.status == "revoked"
    assert revoked.last_used_at is None


def test_missing_revoke_and_invalid_key_ids_do_not_change_store(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path)
    before = (tmp_path / "api-keys.json").read_bytes()
    with pytest.raises(ApiKeyNotFoundError, match="API key not found"):
        store.revoke_key("key_" + "0" * 12)
    for invalid in (None, "", "key_ABCDEF123456", "../api-keys.json", "key_" + "a" * 13):
        with pytest.raises(ValueError, match="invalid key id"):
            store.revoke_key(invalid)  # type: ignore[arg-type]
    assert (tmp_path / "api-keys.json").read_bytes() == before


@pytest.mark.parametrize("changes", [
    {"key_id": "key_bad"}, {"key_id": None}, {"key_id": "key_ABCDEF123456"},
    {"name": " Agent "}, {"name": "A\u200bB"}, {"name": ""},
    {"prefix": "htm_short"}, {"prefix": "htm_abcdefghijk"}, {"prefix": None},
    {"key_hash": "a" * 63}, {"key_hash": "A" * 64}, {"key_hash": None},
    {"role": "owner"}, {"role": ["admin"]},
    {"allowed_topics": []}, {"allowed_topics": "alpha"},
    {"allowed_topics": ["alpha", "alpha"]}, {"allowed_topics": ["../alpha"]},
    {"allowed_topics": [["alpha"]]}, {"allowed_topics": ["Alpha"]},
    {"created_by": " admin "}, {"created_by": "a" * 129}, {"created_by": "a\n"},
    {"created_at": "2026-08-11"}, {"created_at": "2026-08-11T08:00:00+00:00"},
    {"created_at": "2026-08-11T08:00:00+08:00Z"}, {"created_at": "2026-02-30T08:00:00Z"},
    {"created_at": "2026-08-11T08:00:00.1234567Z"}, {"created_at": 123},
    {"expires_at": "2026-08-11T07:59:59Z"}, {"expires_at": "2026-08-11T08:00:00Z"},
    {"expires_at": "never"}, {"expires_at": False},
    {"last_used_at": "2026-08-11T07:59:59Z"}, {"last_used_at": "invalid"},
    {"revoked_at": "2026-08-11T07:59:59Z"}, {"revoked_at": []},
    {"revoked_at": "2026-08-11T08:00:10Z", "last_used_at": "2026-08-11T08:00:11Z"},
    {"secret": "htm_sensitive_plaintext"}, {"status": "active"},
])
def test_corrupt_records_fail_closed_without_raw_details(
    tmp_path: Path, clock: dict[str, datetime], changes: dict[str, Any],
) -> None:
    store = ApiKeyStore(tmp_path)
    record, secret = _create(store)
    data = _document(tmp_path)
    data["keys"][0].update(changes)
    path = tmp_path / "api-keys.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    for operation in (
        lambda: ApiKeyStore(tmp_path), store.list_keys,
        lambda: store.authenticate(secret), lambda: _create(store),
        lambda: store.revoke_key(record.key_id),
    ):
        with pytest.raises(ApiKeyCorruptionError) as caught:
            operation()
        assert str(caught.value) == "API key store is invalid"
        assert caught.value.__suppress_context__
    assert path.read_bytes() == before


@pytest.mark.parametrize("payload", [
    b"", b"not json: htm_sensitive_plaintext", b"\xff", b"null", b"[]",
    b'{"version":1}', b'{"version":true,"keys":[]}', b'{"version":2,"keys":[]}',
    b'{"version":1,"keys":{}}', b'{"version":1,"keys":[null]}',
    b'{"version":1,"keys":[{}]}', b'{"version":1,"keys":[],"secret":"secret"}',
    b'{"version":1,"version":1,"keys":[]}', b'{"version":NaN,"keys":[]}',
])
def test_malformed_store_is_never_initialized_over(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "api-keys.json"
    path.write_bytes(payload)
    with pytest.raises(ApiKeyCorruptionError, match="API key store is invalid"):
        ApiKeyStore(tmp_path)
    assert path.read_bytes() == payload


@pytest.mark.parametrize("mutation", ["missing_field", "duplicate_id", "duplicate_hash", "duplicate_json_field"])
def test_ambiguous_or_incomplete_records_are_corruption(
    tmp_path: Path, clock: dict[str, datetime], mutation: str,
) -> None:
    store = ApiKeyStore(tmp_path)
    first, _ = _create(store)
    second, _ = _create(store, name="Other")
    data = _document(tmp_path)
    if mutation == "missing_field":
        del data["keys"][0]["expires_at"]
    elif mutation == "duplicate_id":
        data["keys"][1]["key_id"] = first.key_id
    elif mutation == "duplicate_hash":
        data["keys"][1]["key_hash"] = first.key_hash
    payload = json.dumps(data)
    if mutation == "duplicate_json_field":
        payload = payload.replace('"name": "Agent"', '"name": "Agent", "name": "Other"')
    (tmp_path / "api-keys.json").write_text(payload, encoding="utf-8")
    with pytest.raises(ApiKeyCorruptionError):
        store.list_keys()
    assert first.key_id != second.key_id


def test_deleted_store_does_not_silently_lose_existing_keys(
    tmp_path: Path, clock: dict[str, datetime],
) -> None:
    store = ApiKeyStore(tmp_path)
    record, secret = _create(store)
    (tmp_path / "api-keys.json").unlink()
    for operation in (store.list_keys, lambda: store.authenticate(secret), lambda: _create(store),
                      lambda: store.revoke_key(record.key_id)):
        with pytest.raises(ApiKeyError, match="API key store is unavailable"):
            operation()
    assert not (tmp_path / "api-keys.json").exists()


@pytest.mark.parametrize("target", ["api-keys.json", ".api-keys.lock"])
def test_symlinks_are_rejected_without_touching_target(tmp_path: Path, target: str) -> None:
    root = tmp_path / "data"
    root.mkdir()
    external = tmp_path / "external.json"
    external.write_text('{"version":1,"keys":[]}', encoding="utf-8")
    external.chmod(0o644)
    (root / target).symlink_to(external)
    with pytest.raises(ApiKeyError, match="API key store is unavailable"):
        ApiKeyStore(root)
    assert external.read_text(encoding="utf-8") == '{"version":1,"keys":[]}'
    assert stat.S_IMODE(external.stat().st_mode) == 0o644


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_failed_atomic_write_preserves_old_store_and_removes_temporary_files(
    tmp_path: Path, clock: dict[str, datetime], monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    store = ApiKeyStore(tmp_path)
    record, secret = _create(store)
    before = (tmp_path / "api-keys.json").read_bytes()

    def fail(*args: Any) -> None:
        raise OSError("sensitive raw details htm_private")

    monkeypatch.setattr(api_keys.os, failure, fail)
    with pytest.raises(ApiKeyError) as caught:
        _create(store, name="New")
    assert str(caught.value) == "API key store is unavailable"
    assert caught.value.__suppress_context__
    assert (tmp_path / "api-keys.json").read_bytes() == before
    assert not tuple(tmp_path.glob("*.tmp"))
    # A failed last-use persistence cannot return a successfully authenticated record.
    with pytest.raises(ApiKeyError):
        store.authenticate(secret)
    assert store.list_keys() == [record]
    assert not tuple(tmp_path.glob("*.tmp"))


def test_atomic_replace_sees_only_private_digest_only_complete_payload(
    tmp_path: Path, clock: dict[str, datetime], monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_replace = api_keys.os.replace
    payloads: list[dict[str, Any]] = []
    replaced: list[Path] = []

    def replace(source: Path, destination: Path) -> None:
        assert source.parent == destination.parent == tmp_path
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        payloads.append(json.loads(source.read_bytes()))
        replaced.append(source)
        real_replace(source, destination)

    monkeypatch.setattr(api_keys.os, "replace", replace)
    store = ApiKeyStore(tmp_path)
    record, secret = _create(store)
    store.authenticate(secret)
    store.revoke_key(record.key_id)
    assert len(payloads) == 4
    assert all("secret" not in item for payload in payloads for item in payload["keys"])
    assert all(secret not in json.dumps(payload) for payload in payloads)
    assert all(not path.exists() for path in replaced)
    assert payloads[-1]["keys"][0]["revoked_at"] == record.created_at


def test_concurrent_store_instances_create_without_lost_updates(
    tmp_path: Path, clock: dict[str, datetime],
) -> None:
    def create(index: int) -> tuple[ApiKeyRecord, str]:
        return _create(ApiKeyStore(tmp_path), name=f"Agent {index}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        created = list(executor.map(create, range(40)))
    records = ApiKeyStore(tmp_path).list_keys()
    assert len(records) == 40
    assert {record.key_id for record in records} == {record.key_id for record, _ in created}
    assert len({record.key_hash for record in records}) == 40
    assert {record.name for record in records} == {f"Agent {index}" for index in range(40)}
    payload = (tmp_path / "api-keys.json").read_text(encoding="utf-8")
    assert all(secret not in payload for _, secret in created)


def test_concurrent_create_authenticate_revoke_preserve_all_updates(
    tmp_path: Path, clock: dict[str, datetime],
) -> None:
    initial_store = ApiKeyStore(tmp_path)
    pairs = [_create(initial_store, name=f"Existing {index}") for index in range(24)]
    clock["now"] += timedelta(seconds=60)

    def mutate(index: int) -> None:
        store = ApiKeyStore(tmp_path)
        record, secret = pairs[index]
        if index % 3 == 0:
            store.revoke_key(record.key_id)
        elif index % 3 == 1:
            assert store.authenticate(secret) is not None
        else:
            _create(store, name=f"New {index}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(mutate, range(24)))
    records = {record.key_id: record for record in ApiKeyStore(tmp_path).list_keys()}
    assert len(records) == 32
    for index, (record, _) in enumerate(pairs):
        result = records[record.key_id]
        if index % 3 == 0:
            assert result.revoked_at == "2026-08-11T08:01:00Z"
        elif index % 3 == 1:
            assert result.last_used_at == "2026-08-11T08:01:00Z"
        else:
            assert result == record


def test_concurrent_auth_and_revoke_never_resurrect_key_or_lose_last_use(
    tmp_path: Path, clock: dict[str, datetime],
) -> None:
    store = ApiKeyStore(tmp_path)
    record, secret = _create(store)
    barrier = threading.Barrier(2)

    def authenticate() -> ApiKeyRecord | None:
        other = ApiKeyStore(tmp_path)
        barrier.wait(timeout=3)
        return other.authenticate(secret)

    def revoke() -> ApiKeyRecord:
        other = ApiKeyStore(tmp_path)
        barrier.wait(timeout=3)
        return other.revoke_key(record.key_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        auth_future = executor.submit(authenticate)
        revoke_future = executor.submit(revoke)
        authenticated = auth_future.result(timeout=5)
        revoked = revoke_future.result(timeout=5)
    final = store.list_keys()[0]
    assert final.revoked_at == revoked.revoked_at
    assert final.last_used_at == (authenticated.last_used_at if authenticated is not None else None)
    assert store.authenticate(secret) is None


@pytest.mark.parametrize("operation", ["initialize", "create", "list", "authenticate", "revoke"])
def test_stable_exclusive_lock_blocks_every_operation_even_after_replace(
    tmp_path: Path, clock: dict[str, datetime], monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    store = ApiKeyStore(tmp_path)
    record, secret = _create(store)
    held_fd = os.open(tmp_path / ".api-keys.lock", os.O_RDWR)
    real_flock = api_keys.fcntl.flock
    # Replace the JSON inode while retaining an open handle to the stable lock inode.
    _create(store, name="After replacement")
    attempted = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []
    operations: list[int] = []

    def track(fd: int, op: int) -> None:
        operations.append(op)
        if op == api_keys.fcntl.LOCK_EX:
            attempted.set()
        real_flock(fd, op)

    def worker() -> None:
        try:
            if operation == "initialize":
                ApiKeyStore(tmp_path)
            elif operation == "create":
                _create(store, name="Blocked")
            elif operation == "list":
                store.list_keys()
            elif operation == "authenticate":
                store.authenticate(secret)
            else:
                store.revoke_key(record.key_id)
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    try:
        real_flock(held_fd, api_keys.fcntl.LOCK_EX)
        monkeypatch.setattr(api_keys.fcntl, "flock", track)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            assert attempted.wait(3)
            assert not finished.is_set()
        finally:
            real_flock(held_fd, api_keys.fcntl.LOCK_UN)
            thread.join(3)
        assert finished.is_set()
        assert not thread.is_alive()
        assert not failures
        assert operations == [api_keys.fcntl.LOCK_EX, api_keys.fcntl.LOCK_UN]
    finally:
        os.close(held_fd)


def test_nonregular_store_fails_closed_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "api-keys.json", mode=0o600)
    with pytest.raises(ApiKeyCorruptionError, match="API key store is invalid"):
        ApiKeyStore(tmp_path)


def test_concurrent_authentication_throttle_is_shared_across_instances(
    tmp_path: Path, clock: dict[str, datetime], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ApiKeyStore(tmp_path)
    record, secret = _create(store)
    writes: list[str | None] = []
    real_write = ApiKeyStore._write_unlocked

    def write(self: ApiKeyStore, records: list[ApiKeyRecord]) -> None:
        writes.append(records[0].last_used_at)
        real_write(self, records)

    def authenticate(_: int) -> ApiKeyRecord | None:
        return ApiKeyStore(tmp_path).authenticate(secret)

    monkeypatch.setattr(ApiKeyStore, "_write_unlocked", write)
    with ThreadPoolExecutor(max_workers=8) as executor:
        first = list(executor.map(authenticate, range(24)))
    assert all(result is not None and result.key_id == record.key_id for result in first)
    assert writes == ["2026-08-11T08:00:00Z"]
    clock["now"] += timedelta(seconds=60)
    with ThreadPoolExecutor(max_workers=8) as executor:
        second = list(executor.map(authenticate, range(24)))
    assert all(result is not None and result.last_used_at == "2026-08-11T08:01:00Z" for result in second)
    assert writes == ["2026-08-11T08:00:00Z", "2026-08-11T08:01:00Z"]
    assert store.list_keys()[0].last_used_at == "2026-08-11T08:01:00Z"