from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from hermes_topic_memory.activity_log import ActivityLedger, ActivityRecord
from hermes_topic_memory.core_memory import CORE_DISPLAY_NAME, CoreMemoryCorruptionError, CoreMemoryService
from hermes_topic_memory.panel_app import PanelApplication, PanelResponse
from hermes_topic_memory.reader import ReaderRequest, ReaderResponse
from hermes_topic_memory.topic_admin import TopicAdminService
from hermes_topic_memory.wx_sso import WxGatewaySso, WxSsoConfig

_TOKEN = "ACTIVITY_BEARER_PRIVATE_4a7d"
_BODY = "# Alpha\nACTIVITY_CANONICAL_PRIVATE_9c2e\n"
_CORE = "ACTIVITY_CORE_PRIVATE_48ab"
_QUERY = "ACTIVITY_QUESTION_PRIVATE_51da"
_FACT = "ACTIVITY_FACT_PRIVATE_62fe"
_ANSWER = "ACTIVITY_CAPSULE_PRIVATE_73bc"
_BACKEND = "ACTIVITY_BACKEND_PRIVATE_84cd"
_SSO_SECRET = "ACTIVITY_SSO_SECRET_PRIVATE_95de"
_ACTIONS = (
    "recall", "remember", "dream", "bind_topic", "list_topics", "whoami",
    "topic_create", "topic_edit", "topic_append", "topic_rollback", "topic_archive",
    "topic_restore", "topic_trash", "topic_delete", "core_init", "core_edit",
    "core_rollback", "auth_failed", "sso_login", "sso_logout",
)
_SOURCES = ("mcp", "rest", "panel", "studio", "cli", "system")
_STATUSES = ("ok", "no_fact", "duplicate", "skipped", "error", "denied")
_ROUTES = {
    "whoami": ("GET", "/api/v1/whoami"),
    "list_topics": ("GET", "/api/v1/topics"),
    "recall": ("POST", "/api/v1/recall"),
    "remember": ("POST", "/api/v1/remember"),
    "bind_topic": ("POST", "/api/v1/topics/bind"),
    "dream": ("POST", "/api/v1/dream"),
}


class _Reader:
    api_key = _BACKEND

    def __init__(self) -> None:
        self.mode = "ok"
        self.on_read: Callable[[], None] | None = None
        self.requests: list[ReaderRequest] = []

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.requests.append(request)
        if self.on_read is not None:
            self.on_read()
        if self.mode == "error":
            raise RuntimeError(_BACKEND)
        if "DREAM MEMORY CONSOLIDATION RULES:" in request.prefix:
            return ReaderResponse(answer=json.dumps({
                "action": "consolidate", "summary": "Safe topic summary",
                "new_body": "# Alpha\n" + _FACT + "\n",
                "changes": ["Consolidated durable facts"],
                "pruned_count": 0, "merged_count": 1, "conflicts_resolved": 0,
            }), model="activity-dream")
        if "TOPIC PLANNING RULES" in request.prefix:
            return ReaderResponse(
                answer='{"topics":[{"topic":"alpha","reason":"explicit topic"}]}',
                model="activity-planner",
            )
        if "Synthesize only the labeled topic capsules" in request.prefix:
            labels = [
                line.removeprefix("LABEL:")
                for line in request.prefix.splitlines()
                if line.startswith("LABEL:")
            ]
            return ReaderResponse(
                answer="; ".join(f"{label}: {_ANSWER}" for label in labels),
                model="activity-synthesizer",
            )
        answer = "no stored fact answers the question" if self.mode == "no_fact" else _ANSWER
        return ReaderResponse(answer=answer, model="activity-reader")


@dataclass(frozen=True)
class _Fixture:
    app: PanelApplication
    root: Path
    reader: _Reader


@pytest.fixture
def activity_app(tmp_path: Path) -> _Fixture:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    admin.create_topic("alpha", "Alpha", ("first",), _BODY, make_default=True)
    admin.create_topic("beta", "Beta", (), "# Beta\n")
    reader = _Reader()
    app = PanelApplication(root, reader, _TOKEN)
    assert app.activities is app.memory_service.activities
    return _Fixture(app, root, reader)


def _request(
    app: PanelApplication, method: str, path: str, payload: dict[str, Any] | None = None,
    *, headers: dict[str, str] | None = None,
) -> tuple[PanelResponse, dict[str, Any]]:
    response = app.dispatch(
        method, path,
        headers={"Authorization": f"Bearer {_TOKEN}"} if headers is None else headers,
        body=b"" if payload is None else json.dumps(payload).encode("utf-8"),
    )
    return response, json.loads(response.body) if response.body else {}


def _service(
    app: PanelApplication, source: str, action: str, arguments: dict[str, Any],
    *, headers: dict[str, str] | None = None,
) -> tuple[PanelResponse, dict[str, Any]]:
    if source == "rest":
        method, path = _ROUTES[action]
        return _request(app, method, path, arguments if method == "POST" else None, headers=headers)
    response, envelope = _request(app, "POST", "/mcp", {
        "jsonrpc": "2.0", "id": "activity-test", "method": "tools/call",
        "params": {"name": action, "arguments": arguments},
    }, headers=headers)
    assert response.status == 200 and envelope["id"] == "activity-test"
    if "error" in envelope:
        return response, envelope
    result = envelope["result"]
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return response, result["structuredContent"]


def _records(root: Path) -> dict[str, dict[str, Any]]:
    page = ActivityLedger(root).list(page_size=100)
    assert page["total"] <= 100
    return {item["event_id"]: item for item in page["items"]}


def _one_new(
    fixture: _Fixture, before: dict[str, dict[str, Any]], *, action: str,
    source: str = "panel", actor: str = "token:env-admin", status: str = "ok",
    topic: str | None = None,
) -> dict[str, Any]:
    after = _records(fixture.root)
    assert len(after) == len(before) + 1
    assert all(after[event_id] == item for event_id, item in before.items())
    added = list(after.keys() - before.keys())
    assert len(added) == 1
    item = after[added[0]]
    assert (item["action"], item["source"], item["actor"], item["status"], item["topic_id"]) == (
        action, source, actor, status, topic,
    )
    assert type(item["latency_ms"]) is int and item["latency_ms"] >= 0
    assert item["created_at"].endswith("Z") and len(item["event_id"]) == 32
    assert item["summary"]
    return item


def _private_absent(fixture: _Fixture, *extra: str) -> None:
    serialized = json.dumps(ActivityLedger(fixture.root).list(page_size=100), ensure_ascii=False)
    durable = "\n".join(path.read_text(encoding="utf-8") for path in (fixture.root / "activity-log").glob("*.jsonl"))
    for secret in (_TOKEN, _BODY, _CORE, _QUERY, _FACT, _ANSWER, _BACKEND, _SSO_SECRET, str(fixture.root), *extra):
        assert secret not in serialized and secret not in durable


def _preconditions(app: PanelApplication, topic: str = "alpha") -> dict[str, Any]:
    response, body = _request(app, "GET", f"/api/topics/{topic}/body")
    assert response.status == 200
    return {"expected_version": body["version"], "expected_fingerprint": body["fingerprint"]}


@pytest.mark.parametrize("source", ("mcp", "rest"))
@pytest.mark.parametrize("marker", (None, "studio"))
@pytest.mark.parametrize("action", tuple(_ROUTES))
def test_each_service_success_logs_one_durable_transport_and_actor(
    activity_app: _Fixture, source: str, marker: str | None, action: str,
) -> None:
    app = activity_app.app
    if action == "whoami":
        CoreMemoryService(activity_app.root).initialize(_CORE)
    arguments = {
        "whoami": {}, "list_topics": {},
        "recall": {"query": _QUERY, "topic": "alpha"},
        "remember": {"fact": _FACT, "topic": "alpha"},
        "bind_topic": {"topic": "alpha", "chat_id": "private-chat-binding"},
        "dream": {"topic": "alpha", "force": True, "dry_run": True},
    }[action]
    headers = {"authorization": f"Bearer {_TOKEN}"}
    if marker is not None:
        headers["X-Topic-Memory-Source"] = marker
    before = _records(activity_app.root)
    response, value = _service(app, source, action, arguments, headers=headers)
    assert response.status == 200 and value.get("error") is None
    if action == "whoami":
        assert value["body"] == _CORE and value["version"] == 1
    elif action == "list_topics":
        assert [topic["topic_id"] for topic in value["topics"]] == ["alpha", "beta"]
    elif action == "recall":
        assert value["status"] == "answered" and value["capsule"] == _ANSWER
    elif action == "remember":
        assert value["status"] == "remembered" and value["topic_version"] == 2
    elif action == "bind_topic":
        assert value["topic"]["chat_ids"] == ["private-chat-binding"]
    else:
        assert value["status"] == "dry_run" and value["before_version"] == value["after_version"] == 1
    item = _one_new(activity_app, before, action=action, source=source,
                    topic=None if action in {"whoami", "list_topics"} else "alpha")
    if action == "recall":
        assert item["meta"]["query_chars"] == len(_QUERY)
        assert app.history.get(value["query_id"]).source == source
    if action == "remember":
        assert item["meta"]["fact_chars"] == len(_FACT)
    _private_absent(activity_app, "private-chat-binding")


@pytest.mark.parametrize("source", ("mcp", "rest"))
@pytest.mark.parametrize("action", tuple(_ROUTES))
def test_each_service_error_logs_once_without_replacing_service_call(
    activity_app: _Fixture, monkeypatch: pytest.MonkeyPatch, source: str, action: str,
) -> None:
    arguments: dict[str, Any] = {}
    expected_code, expected_http = "routing_error", 400
    if action == "whoami":
        def corrupt_core(_self: CoreMemoryService) -> None:
            raise CoreMemoryCorruptionError(_BACKEND)
        monkeypatch.setattr(CoreMemoryService, "get_optional", corrupt_core)
        expected_code, expected_http = "operation_failed", 500
    elif action == "list_topics":
        (activity_app.root / "topics/alpha/metadata.json").write_text(_BACKEND, encoding="utf-8")
        expected_code, expected_http = "operation_failed", 500
    elif action == "recall":
        arguments = {"query": _QUERY, "topic": "missing"}
    elif action == "remember":
        arguments = {"fact": "", "topic": "alpha"}
        expected_code = "invalid_request"
    elif action == "bind_topic":
        arguments = {"topic": "missing", "chat_id": "private-chat-binding"}
    else:
        arguments = {"topic": "missing", "force": True}
    before = _records(activity_app.root)
    response, value = _service(activity_app.app, source, action, arguments)
    assert response.status == (200 if source == "mcp" else expected_http)
    assert value["error"]["code"] == (-32602 if source == "mcp" and expected_code == "invalid_request" else expected_code)
    item = _one_new(activity_app, before, action=action, source=source, status="error", topic=arguments.get("topic"))
    assert item["meta"]["error_code"] == expected_code
    _private_absent(activity_app, "private-chat-binding")


@pytest.mark.parametrize("source", ("mcp", "rest"))
@pytest.mark.parametrize("mode,status", (("error", "error"), ("no_fact", "no_fact")))
def test_service_recall_reader_outcomes_are_durable_and_private(
    activity_app: _Fixture, source: str, mode: str, status: str,
) -> None:
    activity_app.reader.mode = mode
    before = _records(activity_app.root)
    response, result = _service(activity_app.app, source, "recall", {"query": _QUERY, "topic": "alpha"})
    assert response.status == 200 and result["status"] == status and result["capsule"] is None
    _one_new(activity_app, before, action="recall", source=source, status=status, topic="alpha")
    assert activity_app.app.history.get(result["query_id"]).source == source
    _private_absent(activity_app)


@pytest.mark.parametrize("source", ("mcp", "rest"))
def test_remember_duplicate_is_one_duplicate_activity(activity_app: _Fixture, source: str) -> None:
    arguments = {"topic": "alpha", "fact": _FACT, "idempotency_key": "private-idempotency-key"}
    _, first = _service(activity_app.app, source, "remember", arguments)
    before = _records(activity_app.root)
    _, second = _service(activity_app.app, source, "remember", arguments)
    assert second == {**first, "status": "duplicate"}
    _one_new(activity_app, before, action="remember", source=source, status="duplicate", topic="alpha")
    _private_absent(activity_app, "private-idempotency-key")


@pytest.mark.parametrize("source", ("mcp", "rest"))
def test_dream_reader_failure_logs_one_error_without_model_exception(activity_app: _Fixture, source: str) -> None:
    activity_app.reader.mode = "error"
    before = _records(activity_app.root)
    _, value = _service(activity_app.app, source, "dream", {"topic": "alpha", "force": True})
    assert value["status"] == "error" and _BACKEND not in json.dumps(value)
    _one_new(activity_app, before, action="dream", source=source, status="error", topic="alpha")
    _private_absent(activity_app)


@pytest.mark.parametrize("source", ("mcp", "rest"))
def test_uninitialized_whoami_records_skipped_not_false_ok(activity_app: _Fixture, source: str) -> None:
    before = _records(activity_app.root)
    _, result = _service(activity_app.app, source, "whoami", {})
    assert result["status"] == "uninitialized"
    _one_new(activity_app, before, action="whoami", source=source, status="skipped")


def test_core_lifecycle_logs_named_actions_and_versions_without_topic(activity_app: _Fixture) -> None:
    app = activity_app.app
    before = _records(activity_app.root)
    response, initialized = _request(app, "POST", "/api/core-memory/initialize", {"body": _CORE})
    assert response.status == 201 and initialized["core_memory"]["version"] == 1
    item = _one_new(activity_app, before, action="core_init")
    assert item["meta"]["version"] == 1 and item["meta"]["size_chars"] == len(_CORE)
    _, current = _request(app, "GET", "/api/core-memory/body")
    before = _records(activity_app.root)
    response, edited = _request(app, "POST", "/api/core-memory/replace", {
        "body": _CORE + " edited", "expected_version": 1, "expected_fingerprint": current["fingerprint"],
    })
    assert response.status == 200 and edited["core_memory"]["version"] == 2
    item = _one_new(activity_app, before, action="core_edit")
    assert item["meta"]["version"] == 2 and item["meta"]["previous_version"] == 1
    assert item["meta"]["size_chars"] == len(_CORE + " edited")
    _, current = _request(app, "GET", "/api/core-memory/body")
    before = _records(activity_app.root)
    response, rolled = _request(app, "POST", "/api/core-memory/rollback", {
        "target_version": 1, "expected_version": 2, "expected_fingerprint": current["fingerprint"],
        "confirmation": CORE_DISPLAY_NAME,
    })
    assert response.status == 200 and rolled["core_memory"]["version"] == 3
    item = _one_new(activity_app, before, action="core_rollback")
    assert item["meta"]["version"] == 3
    assert item["meta"]["previous_version"] == 2 and item["meta"]["target_version"] == 1
    _, current = _request(app, "GET", "/api/core-memory/body")
    assert current["body"] == _CORE
    _private_absent(activity_app)


@pytest.mark.parametrize("operation,action,status,code", (
    ("initialize", "core_init", "duplicate", "core_memory_exists"),
    ("replace", "core_edit", "error", "concurrent_update"),
    ("rollback", "core_rollback", "error", "confirmation_mismatch"),
))
def test_core_rejections_record_exact_error_metadata(
    activity_app: _Fixture, operation: str, action: str, status: str, code: str,
) -> None:
    current = CoreMemoryService(activity_app.root).initialize(_CORE)
    arguments = {"body": _CORE}
    if operation == "replace":
        arguments.update(expected_version=2, expected_fingerprint=current.metadata.content_fingerprint)
    elif operation == "rollback":
        arguments = {"target_version": 1, "expected_version": 1,
                     "expected_fingerprint": current.metadata.content_fingerprint,
                     "confirmation": "PRIVATE_WRONG_CONFIRMATION"}
    before = _records(activity_app.root)
    response, value = _request(activity_app.app, "POST", f"/api/core-memory/{operation}", arguments)
    assert response.status == 409 and value["error"]["code"] == code
    item = _one_new(activity_app, before, action=action, status=status)
    assert item["meta"]["error_code"] == code
    _private_absent(activity_app, "PRIVATE_WRONG_CONFIRMATION")


def test_topic_and_recycle_lifecycle_has_exact_actions_and_safe_metadata(activity_app: _Fixture) -> None:
    app, topic = activity_app.app, "journey"
    before = _records(activity_app.root)
    response, value = _request(app, "POST", "/api/topics", {
        "topic_id": topic, "display_name": "Journey", "aliases": [], "body": _BODY,
    })
    assert response.status == 201 and value["topic"]["version"] == 1
    item = _one_new(activity_app, before, action="topic_create", topic=topic)
    assert item["meta"]["version"] == 1 and item["meta"]["size_chars"] == len(_BODY)
    for operation, action, arguments, version in (
        ("append", "topic_append", {"text": _FACT}, 2),
        ("replace", "topic_edit", {"body": _BODY + " replacement"}, 3),
        ("rollback", "topic_rollback", {"target_version": 1}, 4),
        ("archive", "topic_archive", {}, 4), ("restore", "topic_restore", {}, 4),
        ("trash", "topic_trash", {}, 4),
    ):
        preconditions = _preconditions(app, topic)
        before = _records(activity_app.root)
        response, value = _request(app, "POST", f"/api/topics/{topic}/{operation}", {**preconditions, **arguments})
        assert response.status == 200
        resource = value.get("topic", value.get("item"))
        if operation != "trash":
            assert resource["version"] == version
        item = _one_new(activity_app, before, action=action, topic=topic)
        assert item["meta"]["version"] == version
        assert item["meta"]["previous_version"] == preconditions["expected_version"]
        if "text" in arguments or "body" in arguments:
            assert item["meta"]["size_chars"] == len(arguments.get("text", arguments.get("body")))
        if operation == "rollback":
            assert item["meta"]["target_version"] == 1
        if operation in {"archive", "restore"}:
            assert resource["state"] == ("archived" if operation == "archive" else "active")
    before = _records(activity_app.root)
    response, restored = _request(app, "POST", f"/api/recycle-bin/{topic}/restore", {"trashed_at": value["item"]["trashed_at"]})
    assert response.status == 200 and restored["topic"]["version"] == 4
    item = _one_new(activity_app, before, action="topic_restore", topic=topic)
    assert item["meta"]["version"] == 4
    _, trashed = _request(app, "POST", f"/api/topics/{topic}/trash", _preconditions(app, topic))
    before = _records(activity_app.root)
    response, deleted = _request(app, "POST", f"/api/recycle-bin/{topic}/delete", {
        "trashed_at": trashed["item"]["trashed_at"], "confirmation": "Journey",
    })
    assert response.status == 200 and deleted["result"]["status"] == "permanently_deleted"
    _one_new(activity_app, before, action="topic_delete", topic=topic)
    assert not (activity_app.root / "topics" / topic).exists()
    assert not (activity_app.root / "recycle-bin" / topic).exists()
    _private_absent(activity_app)


@pytest.mark.parametrize("operation,action", (
    ("append", "topic_append"), ("replace", "topic_edit"), ("rollback", "topic_rollback"),
    ("archive", "topic_archive"), ("restore", "topic_restore"), ("trash", "topic_trash"),
))
def test_topic_mutation_rejection_logs_one_error(activity_app: _Fixture, operation: str, action: str) -> None:
    if operation == "restore":
        activity_app.app.admin.archive("alpha", expected_version=1)
    arguments = {"expected_version": 99, "expected_fingerprint": "0" * 64}
    arguments.update({"append": {"text": _FACT}, "replace": {"body": _BODY}, "rollback": {"target_version": 1}}.get(operation, {}))
    before = _records(activity_app.root)
    response, value = _request(activity_app.app, "POST", f"/api/topics/alpha/{operation}", arguments)
    assert response.status == 409 and value["error"]["code"] == "concurrent_update"
    item = _one_new(activity_app, before, action=action, status="error", topic="alpha")
    assert item["meta"]["error_code"] == "concurrent_update" and item["meta"]["previous_version"] == 99
    _private_absent(activity_app)


def test_duplicate_topic_create_and_append_have_duplicate_status(activity_app: _Fixture) -> None:
    before = _records(activity_app.root)
    response, value = _request(activity_app.app, "POST", "/api/topics", {
        "topic_id": "alpha", "display_name": "Alpha", "aliases": [], "body": _BODY,
    })
    assert response.status == 409 and value["error"]["code"] == "topic_exists"
    item = _one_new(activity_app, before, action="topic_create", status="duplicate", topic="alpha")
    assert item["meta"]["error_code"] == "topic_exists"
    _request(activity_app.app, "POST", "/api/topics/alpha/append", {**_preconditions(activity_app.app), "text": _FACT})
    before = _records(activity_app.root)
    response, value = _request(activity_app.app, "POST", "/api/topics/alpha/append", {**_preconditions(activity_app.app), "text": _FACT})
    assert response.status == 409 and value["error"]["code"] == "append_duplicate"
    item = _one_new(activity_app, before, action="topic_append", status="duplicate", topic="alpha")
    assert item["meta"]["error_code"] == "append_duplicate"
    _private_absent(activity_app)


@pytest.mark.parametrize("operation,failpoint", (("restore", "after_restore_move"), ("delete", "before_permanent_remove")))
@pytest.mark.parametrize("failure", ("stale", "storage"))
def test_recycle_rejections_and_transaction_errors_are_recorded(
    activity_app: _Fixture, operation: str, failpoint: str, failure: str,
) -> None:
    app = activity_app.app
    _, trashed = _request(app, "POST", "/api/topics/alpha/trash", _preconditions(app))
    arguments = {"trashed_at": trashed["item"]["trashed_at"]}
    if operation == "delete":
        arguments["confirmation"] = "Alpha"
    if failure == "stale":
        arguments["trashed_at"] = "2000-01-01T00:00:00Z"
    else:
        def fail(point: str) -> None:
            if point == failpoint:
                raise OSError(_BACKEND)
        app.admin._failure_injector = fail
    before = _records(activity_app.root)
    try:
        response, value = _request(app, "POST", f"/api/recycle-bin/alpha/{operation}", arguments)
    finally:
        app.admin._failure_injector = None
    code = "concurrent_update" if failure == "stale" else "operation_failed"
    assert response.status == (409 if failure == "stale" else 500) and value["error"]["code"] == code
    item = _one_new(activity_app, before, action="topic_restore" if operation == "restore" else "topic_delete", status="error", topic="alpha")
    assert item["meta"]["error_code"] == code
    assert (activity_app.root / "recycle-bin/alpha").is_dir()
    _private_absent(activity_app)


def test_recycle_delete_confirmation_rejection_logs_error(activity_app: _Fixture) -> None:
    _, trashed = _request(activity_app.app, "POST", "/api/topics/alpha/trash", _preconditions(activity_app.app))
    before = _records(activity_app.root)
    response, value = _request(activity_app.app, "POST", "/api/recycle-bin/alpha/delete", {
        "trashed_at": trashed["item"]["trashed_at"], "confirmation": "PRIVATE_WRONG_CONFIRMATION",
    })
    assert response.status == 409 and value["error"]["code"] == "confirmation_mismatch"
    item = _one_new(activity_app, before, action="topic_delete", status="error", topic="alpha")
    assert item["meta"]["error_code"] == "confirmation_mismatch"
    _private_absent(activity_app, "PRIVATE_WRONG_CONFIRMATION")


@pytest.mark.parametrize("mode,status", (("ok", "ok"), ("no_fact", "no_fact"), ("error", "error")))
@pytest.mark.parametrize("rerun", (False, True))
def test_panel_recall_run_and_rerun_record_outcomes_without_question_or_capsule(
    activity_app: _Fixture, mode: str, status: str, rerun: bool,
) -> None:
    app = activity_app.app
    arguments = {"question": _QUERY, "manual_topics": [{"topic": "alpha", "reason": "PRIVATE_MANUAL_REASON"}]}
    path, old_id = "/api/recall/run", None
    if rerun:
        _, original = _request(app, "POST", path, arguments)
        old_id = original["query_id"]
        path, arguments = f"/api/recall/rerun/{old_id}", {}
        app.store.update_topic("alpha", _BODY + " current version")
    activity_app.reader.mode = mode
    before = _records(activity_app.root)
    response, value = _request(app, "POST", path, arguments)
    assert response.status == 200 and value["status"] == ("answered" if mode == "ok" else status)
    assert value["question"] == _QUERY and (value["final_answer"] is not None) == (mode == "ok")
    item = _one_new(activity_app, before, action="recall", status=status, topic="alpha")
    assert item["meta"]["version"] == (2 if rerun else 1)
    if mode == "error":
        assert item["meta"]["error_code"] == value["failure"]["code"]
    record = app.history.get(value["query_id"])
    assert record is not None and record.source == "panel" and record.question == _QUERY
    if rerun:
        assert old_id != value["query_id"] and value["plan"]["mode"] == "rerun"
    _private_absent(activity_app, "PRIVATE_MANUAL_REASON")


@pytest.mark.parametrize("path,arguments", (
    ("/api/recall/run", {"question": _QUERY, "manual_topics": [{"topic": "missing", "reason": "PRIVATE_SCOPE"}]}),
    ("/api/recall/rerun/missing-query", {}),
))
def test_recall_rejected_scope_or_missing_rerun_logs_error(activity_app: _Fixture, path: str, arguments: dict[str, Any]) -> None:
    before = _records(activity_app.root)
    response, value = _request(activity_app.app, "POST", path, arguments)
    assert response.status in {400, 404}
    item = _one_new(activity_app, before, action="recall", status="error")
    assert item["meta"]["error_code"] == value["error"]["code"]
    assert not activity_app.reader.requests
    _private_absent(activity_app, "PRIVATE_SCOPE", "missing-query")


@pytest.mark.parametrize("header,value,source", (
    ("X-Topic-Memory-Source", "studio", "studio"), ("x-topic-memory-source", "studio", "studio"),
    ("X-Topic-Memory-Source", "Studio", "panel"), ("X-Topic-Memory-Source", " studio", "panel"),
    ("X-Topic-Memory-Source", "studio ", "panel"), ("X-Topic-Memory-Source", "mcp", "panel"),
    ("X-Memory-Source", "studio", "panel"),
))
def test_only_exact_studio_marker_changes_classic_panel_source(
    activity_app: _Fixture, header: str, value: str, source: str,
) -> None:
    before = _records(activity_app.root)
    response, _ = _request(activity_app.app, "POST", "/api/core-memory/initialize", {"body": _CORE},
                           headers={"Authorization": f"Bearer {_TOKEN}", header: value})
    assert response.status == 201
    _one_new(activity_app, before, action="core_init", source=source)
    _private_absent(activity_app)


@pytest.mark.parametrize("method,path,source", (
    ("GET", "/api/activities", "panel"), ("POST", "/api/topics/alpha/append", "panel"),
    ("POST", "/mcp", "mcp"), *((method, path, "rest") for method, path in _ROUTES.values()),
))
@pytest.mark.parametrize("auth", ("missing", "invalid", "cookie", "unicode"))
def test_unauthorized_requests_log_one_anonymous_denial_without_request_secrets(
    activity_app: _Fixture, method: str, path: str, source: str, auth: str,
) -> None:
    headers = {"X-Private": "HEADER_PRIVATE_a1b2"}
    if auth == "invalid":
        headers["Authorization"] = "Bearer TOKEN_PRIVATE_c3d4"
    elif auth == "cookie":
        headers["Cookie"] = "htm_session=COOKIE_PRIVATE_f2e1"
    elif auth == "unicode":
        headers["Authorization"] = "Bearer 非ASCII_PRIVATE"
    before = _records(activity_app.root)
    response, value = _request(activity_app.app, method, path + "?q=QUERY_PRIVATE_e5f6", {"body": "BODY_PRIVATE_a7b8"}, headers=headers)
    assert response.status == 401 and value["error"]["code"] == "unauthorized"
    item = _one_new(activity_app, before, action="auth_failed", source=source, actor="anonymous", status="denied")
    assert item["meta"]["method"] == method
    assert item["meta"]["path"] == ("/api/topics/:topic_id/append" if "/alpha/" in path else path)
    assert not activity_app.reader.requests
    _private_absent(activity_app, "COOKIE_PRIVATE_f2e1", "HEADER_PRIVATE_a1b2", "TOKEN_PRIVATE_c3d4", "QUERY_PRIVATE_e5f6", "BODY_PRIVATE_a7b8", "非ASCII_PRIVATE")


def test_unauthorized_unknown_path_and_studio_marker_are_safely_classified(activity_app: _Fixture) -> None:
    before = _records(activity_app.root)
    response, _ = _request(activity_app.app, "POST", "/UNKNOWN_PRIVATE_PATH?q=PRIVATE_QUERY", {"body": _BODY},
                           headers={"X-Topic-Memory-Source": "studio", "Authorization": "Bearer INVALID_PRIVATE"})
    assert response.status == 401
    item = _one_new(activity_app, before, action="auth_failed", source="studio", actor="anonymous", status="denied")
    assert item["meta"] == {"method": "POST", "path": "/unknown"}
    _private_absent(activity_app, "UNKNOWN_PRIVATE_PATH", "PRIVATE_QUERY", "INVALID_PRIVATE")


def _seed_activities(app: PanelApplication) -> list[dict[str, Any]]:
    items = []
    for index in range(105):
        record = ActivityRecord(
            action=_ACTIONS[index % len(_ACTIONS)], source=_SOURCES[index % len(_SOURCES)],
            status=_STATUSES[index % len(_STATUSES)], topic_id="alpha" if index % 2 == 0 else "beta",
            actor="token:env-admin", summary=f"Needle activity {index}",
            event_id=f"{index + 1:032x}", created_at="2026-01-01T00:00:00Z",
        )
        app.activities.append(record)
        items.append(record.to_dict())
    return list(reversed(items))


def test_activity_endpoint_defaults_cap_pagination_totals_and_restart(activity_app: _Fixture) -> None:
    expected = _seed_activities(activity_app.app)
    response, first = _request(activity_app.app, "GET", "/api/activities")
    assert response.status == 200
    assert first == {"items": expected[:30], "page": 1, "page_size": 30, "total": 105}
    _, second = _request(activity_app.app, "GET", "/api/activities?page=2")
    assert second == {"items": expected[30:60], "page": 2, "page_size": 30, "total": 105}
    _, capped = _request(activity_app.app, "GET", "/api/activities?page_size=1000")
    assert capped == {"items": expected[:100], "page": 1, "page_size": 100, "total": 105}
    _, last = _request(activity_app.app, "GET", "/api/activities?page=2&page_size=100")
    assert last["items"] == expected[100:] and last["total"] == 105
    _, empty = _request(activity_app.app, "GET", "/api/activities?page=99&page_size=100")
    assert empty["items"] == [] and empty["total"] == 105
    restarted = PanelApplication(activity_app.root, _Reader(), _TOKEN)
    _, recovered = _request(restarted, "GET", "/api/activities?page_size=1000")
    assert recovered == capped and restarted.activities is restarted.memory_service.activities
    assert ActivityLedger(activity_app.root).list(page_size=100)["total"] == 105


@pytest.mark.parametrize("filters", (
    {"action": "recall"}, {"source": "mcp"}, {"source": "rest"}, {"source": "studio"},
    {"topic": "alpha"}, {"status": "error"}, {"q": "nEeDlE AcTiViTy 10"},
    {"action": "recall", "source": "mcp", "topic": "alpha", "status": "ok", "q": "NEEDLE"},
    {"action": "topic_edit", "source": "cli", "status": "error", "topic": "alpha"},
))
def test_activity_endpoint_filters_compose_before_pagination(activity_app: _Fixture, filters: dict[str, str]) -> None:
    seeded = _seed_activities(activity_app.app)
    expected = [item for item in seeded if all(
        (value.casefold() in item["summary"].casefold()) if key == "q" else item["topic_id" if key == "topic" else key] == value
        for key, value in filters.items()
    )]
    response, result = _request(activity_app.app, "GET", "/api/activities?" + urlencode({**filters, "page_size": 2, "page": 2}))
    assert response.status == 200
    assert result == {"items": expected[2:4], "page": 2, "page_size": 2, "total": len(expected)}


@pytest.mark.parametrize("field,values", (("action", _ACTIONS), ("source", _SOURCES), ("status", _STATUSES)))
def test_activity_endpoint_accepts_every_contract_enum(activity_app: _Fixture, field: str, values: tuple[str, ...]) -> None:
    seeded = _seed_activities(activity_app.app)
    for value in values:
        response, result = _request(activity_app.app, "GET", "/api/activities?" + urlencode({field: value}))
        assert response.status == 200 and result["total"] == sum(item[field] == value for item in seeded)
        assert all(item[field] == value for item in result["items"])


@pytest.mark.parametrize("query", (
    "unknown=1", "limit=30", "topic_id=alpha", "action=unknown", "source=hermes", "status=answered",
    "action=", "source=", "status=", "topic=", "topic=Alpha", "topic=..%2Falpha",
    "page=0", "page=-1", "page=1.5", "page=true", "page=", "page=%EF%BC%91",
    "page_size=0", "page_size=-2", "page_size=2.5", "page_size=", "page_size=NaN",
    "action=recall&action=remember", "source=mcp&source=mcp", "topic=alpha&topic=beta",
    "status=ok&status=error", "q=a&q=b", "page=1&page=2", "page_size=1&page_size=2",
    "q=%00", "q=%0A", "q=" + "x" * 301,
    "page=" + "9" * 5000, "page_size=" + "9" * 5000,
))
def test_activity_invalid_unknown_duplicate_or_unsafe_filters_are_400(activity_app: _Fixture, query: str) -> None:
    before = _records(activity_app.root)
    response, value = _request(activity_app.app, "GET", "/api/activities?" + query)
    assert response.status == 400 and value["error"]["code"] == "invalid_request"
    assert _records(activity_app.root) == before


@pytest.mark.parametrize("source", ("mcp", "rest"))
def test_questions_accept_service_source_and_show_actual_service_history(activity_app: _Fixture, source: str) -> None:
    _, recalled = _service(activity_app.app, source, "recall", {"query": _QUERY, "topic": "alpha"})
    before = _records(activity_app.root)
    response, value = _request(activity_app.app, "GET", f"/api/questions?source={source}")
    assert response.status == 200 and value["total"] == 1
    assert value["items"][0]["query_id"] == recalled["query_id"] and value["items"][0]["source"] == source
    assert _records(activity_app.root) == before


def _sso_application(
    fixture: _Fixture, monkeypatch: pytest.MonkeyPatch, *, nickname: str | None = "  Alice ._-  ",
    openid: str = "openid_123456_FULL_PRIVATE", outcome: str = "ok",
) -> tuple[PanelApplication, list[dict[str, Any]]]:
    monkeypatch.setattr("hermes_topic_memory.wx_sso.time.time", lambda: 2_000_000_000.0)
    exchanges: list[dict[str, Any]] = []

    def exchange(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        message = f"sso-token|{payload['code']}|{payload['redirect_uri']}|{payload['code_verifier']}|{payload['state']}|{headers['X-Ts']}|{headers['X-App-Name']}"
        expected = hmac.new(_SSO_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
        assert hmac.compare_digest(headers["X-Internal-Auth"], expected)
        exchanges.append({"url": url, "payload": payload, "headers": headers})
        if outcome == "exchange_error":
            return 502, {"ok": False, "error": _BACKEND}
        return 200, {
            "ok": True, "issuer": "https://gateway.example", "app": "topic-memory",
            "user": {"openid": openid, "nickname": nickname, "unionid": "UNION_PRIVATE", "avatarUrl": "https://avatar.example/AVATAR_PRIVATE"},
            "sessionExpiresAt": 2_000_003_600,
        }

    config = WxSsoConfig(
        gateway_base="https://gateway.example", app_name="topic-memory", app_secret=_SSO_SECRET,
        public_base_url="https://memory.example", allowed_openids=("allowed-other",) if outcome == "forbidden" else (openid,),
    )
    sso = WxGatewaySso(config, session_secret=_TOKEN, token_exchanger=exchange)
    return PanelApplication(fixture.root, fixture.reader, _TOKEN, wx_sso=sso), exchanges


def _sso_start(app: PanelApplication) -> tuple[str, str, str]:
    response = app.dispatch("GET", "/api/sso/start?next=%2F", headers={})
    assert response.status == 302
    query = parse_qs(urlsplit(dict(response.headers)["Location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://memory.example/api/sso/callback"]
    cookie = dict(response.headers)["Set-Cookie"].split(";", 1)[0]
    assert cookie.startswith("htm_sso_tx=")
    return query["state"][0], cookie, query["code_challenge"][0]


def _sso_login(app: PanelApplication) -> tuple[str, tuple[str, ...]]:
    state, tx_cookie, challenge = _sso_start(app)
    code = "SSO_AUTHORIZATION_CODE_PRIVATE"
    response = app.dispatch("GET", "/api/sso/callback?" + urlencode({"code": code, "state": state, "iss": "https://gateway.example"}), headers={"Cookie": tx_cookie})
    assert response.status == 303 and dict(response.headers)["Location"] == "/"
    session = next(value.split(";", 1)[0] for name, value in response.headers if name.lower() == "set-cookie" and value.startswith("htm_session="))
    return session, (state, tx_cookie, challenge, code, session)


@pytest.mark.parametrize("nickname,openid,actor", (
    ("  Alice ._-  ", "openid_123456_FULL_PRIVATE", "sso:Alice ._-"),
    ("爸爸", "openid_123456_FULL_PRIVATE", "sso:爸爸"),
    ("N" * 70, "openid_123456_FULL_PRIVATE", "sso:" + "N" * 64),
    (None, "openid_123456_FULL_PRIVATE", "sso:openid_12345"),
    ("<>/\n", "openid_123456_FULL_PRIVATE", "sso:openid_12345"),
    ("cookie=SSO_NICK_SECRET", "openid_123456_FULL_PRIVATE", "sso:openid_12345"),
    ("token=SSO_NICK_SECRET", "openid_123456_FULL_PRIVATE", "sso:openid_12345"),
    ("api_key=SSO_NICK_SECRET", "openid_123456_FULL_PRIVATE", "sso:openid_12345"),
    (None, "cookie=SSO_OPENID_SECRET", "sso:user"),
))
def test_real_sso_login_uses_minted_safe_actor_and_logout_current_identity(
    activity_app: _Fixture, monkeypatch: pytest.MonkeyPatch, nickname: str | None, openid: str, actor: str,
) -> None:
    app, exchanges = _sso_application(activity_app, monkeypatch, nickname=nickname, openid=openid)
    before = _records(activity_app.root)
    session, secrets = _sso_login(app)
    _one_new(activity_app, before, action="sso_login", actor=actor)
    assert len(exchanges) == 1
    for source in ("mcp", "rest"):
        before = _records(activity_app.root)
        response, result = _service(app, source, "list_topics", {}, headers={"Cookie": session, "X-Topic-Memory-Source": "studio"})
        assert response.status == 200 and len(result["topics"]) == 2
        _one_new(activity_app, before, action="list_topics", source=source, actor=actor)
    before = _records(activity_app.root)
    response, value = _request(app, "POST", "/api/sso/logout", headers={"Cookie": session})
    assert response.status == 200 and value["ok"] is True
    assert any(name.lower() == "set-cookie" and cookie.startswith("htm_session=;") and "Max-Age=0" in cookie for name, cookie in response.headers)
    _one_new(activity_app, before, action="sso_logout", actor=actor)
    exchange = exchanges[0]
    _private_absent(activity_app, *secrets, exchange["payload"]["code_verifier"], exchange["headers"]["X-Internal-Auth"], openid, "UNION_PRIVATE", "AVATAR_PRIVATE", "SSO_NICK_SECRET", "SSO_OPENID_SECRET")


@pytest.mark.parametrize("failure,http,status,code", (
    ("denied", 403, "denied", "sso_denied"), ("state", 400, "error", "state_mismatch"),
    ("missing", 400, "error", "invalid_callback"),
    ("exchange_error", 502, "error", "token_exchange_failed"), ("forbidden", 403, "denied", "forbidden_user"),
))
def test_failed_sso_callback_logs_one_anonymous_named_outcome_without_secrets(
    activity_app: _Fixture, monkeypatch: pytest.MonkeyPatch, failure: str, http: int, status: str, code: str,
) -> None:
    app, exchanges = _sso_application(activity_app, monkeypatch, outcome=failure)
    state, cookie, challenge = _sso_start(app)
    arguments = {"code": "SSO_CALLBACK_CODE_PRIVATE", "state": state, "iss": "https://gateway.example"}
    if failure == "denied":
        arguments["error"] = "SSO_DENIAL_PRIVATE"
    elif failure == "state":
        arguments["state"] = "SSO_WRONG_STATE_PRIVATE"
    elif failure == "missing":
        arguments.pop("code")
    before = _records(activity_app.root)
    response = app.dispatch("GET", "/api/sso/callback?" + urlencode(arguments), headers={"Cookie": cookie, "X-Secret": "SSO_HEADER_PRIVATE"})
    assert response.status == http
    assert not any(name.lower() == "set-cookie" and value.startswith("htm_session=") for name, value in response.headers)
    item = _one_new(activity_app, before, action="sso_login", actor="anonymous", status=status)
    assert item["meta"]["error_code"] == code
    exchange_secrets = tuple(value for exchange in exchanges for value in (exchange["payload"]["code_verifier"], exchange["headers"]["X-Internal-Auth"]))
    _private_absent(activity_app, state, cookie, challenge, "SSO_CALLBACK_CODE_PRIVATE", "SSO_DENIAL_PRIVATE", "SSO_WRONG_STATE_PRIVATE", "SSO_HEADER_PRIVATE", *exchange_secrets)


@pytest.mark.parametrize("cookie", (
    "htm_session=INVALID_COOKIE_PRIVATE", "htm_session=INVALID_PAYLOAD_PRIVATE.bad-signature",
    "htm_session=非ASCII_PRIVATE.signature", "htm_session=payload.非ASCII_PRIVATE",
))
def test_invalid_sso_cookie_is_anonymous_for_denial_and_logout(
    activity_app: _Fixture, monkeypatch: pytest.MonkeyPatch, cookie: str,
) -> None:
    app, _ = _sso_application(activity_app, monkeypatch)
    before = _records(activity_app.root)
    response, _ = _request(app, "GET", "/api/activities", headers={"Cookie": cookie})
    assert response.status == 401
    _one_new(activity_app, before, action="auth_failed", actor="anonymous", status="denied")
    before = _records(activity_app.root)
    response, _ = _request(app, "POST", "/api/sso/logout", headers={"Cookie": cookie})
    assert response.status == 200
    _one_new(activity_app, before, action="sso_logout", actor="anonymous")
    _private_absent(activity_app, cookie, "INVALID_COOKIE_PRIVATE", "INVALID_PAYLOAD_PRIVATE", "非ASCII_PRIVATE")


def test_overlapping_service_requests_keep_actor_bound_to_each_invocation(
    activity_app: _Fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = _sso_application(activity_app, monkeypatch, nickname="Alice")
    session, secrets = _sso_login(app)
    barrier = Barrier(2)
    activity_app.reader.on_read = lambda: barrier.wait(timeout=5)
    requests = (
        ("mcp", "short-private-query", {"Authorization": f"Bearer {_TOKEN}"}, "token:env-admin"),
        ("rest", "longer-private-query-for-sso", {"Cookie": session}, "sso:Alice"),
    )
    before = _records(activity_app.root)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_service, app, source, "recall", {"query": query, "topic": "alpha"}, headers=headers) for source, query, headers, _actor in requests]
            for future in futures:
                response, result = future.result(timeout=10)
                assert response.status == 200 and result["status"] == "answered"
    finally:
        activity_app.reader.on_read = None
    after = _records(activity_app.root)
    added = [after[event_id] for event_id in after.keys() - before.keys()]
    assert len(after) == len(before) + 2 and len(added) == 2
    for source, query, _headers, actor in requests:
        matching = [item for item in added if item["meta"]["query_chars"] == len(query)]
        assert len(matching) == 1
        assert (matching[0]["actor"], matching[0]["source"], matching[0]["action"]) == (actor, source, "recall")
    _private_absent(activity_app, *secrets, *(query for _source, query, _headers, _actor in requests))


@pytest.mark.parametrize("boundary,code", (
    ("mutation", "operation_failed"), ("sso_login", "operation_failed"),
    ("sso_logout", "operation_failed"), ("auth", "internal_error"),
))
def test_activity_write_failure_is_safe_500_never_false_success(
    activity_app: _Fixture, monkeypatch: pytest.MonkeyPatch, boundary: str, code: str,
) -> None:
    app = activity_app.app
    method, path, body, headers = "POST", "/api/core-memory/initialize", json.dumps({"body": _CORE}).encode(), {"Authorization": f"Bearer {_TOKEN}"}
    if boundary.startswith("sso"):
        app, _ = _sso_application(activity_app, monkeypatch)
        if boundary == "sso_login":
            state, cookie, _challenge = _sso_start(app)
            method, path, body, headers = "GET", "/api/sso/callback?" + urlencode({"code": "PRIVATE_FAILED_SSO_CODE", "state": state}), b"", {"Cookie": cookie}
        else:
            session, _secrets = _sso_login(app)
            method, path, body, headers = "POST", "/api/sso/logout", b"", {"Cookie": session}
    elif boundary == "auth":
        method, path, body, headers = "GET", "/api/activities?token=PRIVATE_AUTH_QUERY", b"", {"Authorization": "Bearer PRIVATE_BAD_TOKEN"}
    before = _records(activity_app.root)

    def fail_append(_record: ActivityRecord) -> None:
        raise OSError(_BACKEND)

    monkeypatch.setattr(app.activities, "append", fail_append)
    response = app.dispatch(method, path, headers=headers, body=body)
    assert response.status == 500 and json.loads(response.body)["error"]["code"] == code
    assert _BACKEND.encode() not in response.body and _TOKEN.encode() not in response.body
    assert not response.headers
    assert _records(activity_app.root) == before
    _private_absent(activity_app, "PRIVATE_FAILED_SSO_CODE", "PRIVATE_AUTH_QUERY", "PRIVATE_BAD_TOKEN")


@pytest.mark.parametrize("placement", ("query", "cookie", "body", "header"))
def test_activity_endpoint_never_accepts_token_outside_authorization(
    activity_app: _Fixture, placement: str,
) -> None:
    activity_app.app.activities.append(ActivityRecord(
        action="topic_edit", source="panel", summary="PRIOR_ACTIVITY_PRIVATE",
    ))
    path, body, headers = "/api/activities", {}, {}
    if placement == "query":
        path += "?token=" + _TOKEN
    elif placement == "cookie":
        headers["Cookie"] = "token=" + _TOKEN
    elif placement == "body":
        body["token"] = _TOKEN
    else:
        headers["X-Authorization"] = "Bearer " + _TOKEN
    before = _records(activity_app.root)
    response, value = _request(activity_app.app, "GET", path, body, headers=headers)
    assert response.status == 401 and value["error"]["code"] == "unauthorized"
    assert b"PRIOR_ACTIVITY_PRIVATE" not in response.body
    assert _TOKEN.encode() not in response.body
    _one_new(activity_app, before, action="auth_failed", actor="anonymous", status="denied")
    _private_absent(activity_app)


@pytest.mark.parametrize("bearer,actor", ((True, "token:env-admin"), (False, "sso:Alice")))
def test_service_actor_auth_precedence_is_per_request(
    activity_app: _Fixture, monkeypatch: pytest.MonkeyPatch, bearer: bool, actor: str,
) -> None:
    app, _ = _sso_application(activity_app, monkeypatch, nickname="Alice")
    session, secrets = _sso_login(app)
    before = _records(activity_app.root)
    response, _ = _service(app, "rest", "list_topics", {}, headers={
        "Cookie": session, "Authorization": "Bearer " + (_TOKEN if bearer else "INVALID_BEARER_PRIVATE"),
    })
    assert response.status == 200
    _one_new(activity_app, before, action="list_topics", source="rest", actor=actor)
    _private_absent(activity_app, *secrets, "INVALID_BEARER_PRIVATE")


def test_invalid_logout_method_is_a_named_error_for_current_actor(
    activity_app: _Fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = _sso_application(activity_app, monkeypatch, nickname="Alice")
    session, secrets = _sso_login(app)
    before = _records(activity_app.root)
    response, value = _request(app, "GET", "/api/sso/logout", headers={"Cookie": session})
    assert response.status == 405 and value["error"]["code"] == "method_not_allowed"
    item = _one_new(activity_app, before, action="sso_logout", actor="sso:Alice", status="error")
    assert item["meta"]["error_code"] == "method_not_allowed"
    _private_absent(activity_app, *secrets)
