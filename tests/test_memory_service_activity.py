from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from hermes_topic_memory.activity_log import ActivityLedger
from hermes_topic_memory.core_memory import CoreMemoryService
from hermes_topic_memory.memory_service import MemoryService, MemoryServiceError, tool_definitions
from hermes_topic_memory.reader import ReaderRequest, ReaderResponse
from hermes_topic_memory.topic_admin import TopicAdminService

_SECRET = "PRIVATE_READER_CREDENTIAL"


class _Reader:
    def __init__(self) -> None:
        self.requests: list[ReaderRequest] = []
        self.dream_change = False
        self.dream_failure = False
        self.beta_failure = False
        self.beta_no_fact = False

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.requests.append(request)
        if request.question == "Consolidate this topic memory and return strict JSON only.":
            if self.dream_failure:
                raise RuntimeError(f"Bearer hidden-backend-token {_SECRET}")
            return ReaderResponse(answer=json.dumps({
                "action": "consolidate" if self.dream_change else "no_change",
                "new_body": "Consolidated canonical body.",
                "summary": "PRIVATE_MODEL_SUMMARY",
                "changes": ["PRIVATE_MODEL_CHANGE"],
            }), model="test-reader")
        if request.question == "Return only the final synthesized answer.":
            return ReaderResponse(
                answer="[Alpha Topic/alpha] observed capsule. [Beta Topic/beta] error.",
                model="PRIVATE_MODEL_NAME",
            )
        if request.question == "backend failure":
            raise RuntimeError(f"password=hidden-backend-password {_SECRET}")
        if "BETA_CANONICAL" in request.prefix:
            if self.beta_failure:
                raise RuntimeError(_SECRET)
            if self.beta_no_fact:
                return ReaderResponse(answer="no stored fact answers the question", model="test-reader")
        if request.question.startswith("missing fact"):
            return ReaderResponse(answer="no stored fact answers the question", model="test-reader")
        return ReaderResponse(answer="An observed capsule.", model="test-reader")


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    admin.create_topic("alpha", "Alpha Topic", ("first",), "ALPHA_CANONICAL", make_default=True)
    admin.create_topic("beta", "Beta Topic", ("second",), "BETA_CANONICAL", chat_ids=("beta-chat",))
    admin.create_topic("archived", "Archived Topic", (), "ARCHIVED_CANONICAL")
    admin.archive("archived", expected_version=1)
    return MemoryService(root, _Reader())


def _only_event(service: MemoryService) -> dict[str, Any]:
    result = service.activities.list()
    assert result["total"] == 1
    assert len(result["items"]) == 1
    return result["items"][0]


def _serialized_activities(service: MemoryService) -> str:
    return "".join(path.read_text() for path in (service.root / "activity-log").glob("*.jsonl"))


@pytest.mark.parametrize("name,arguments,expected_status", [
    ("whoami", {}, "ok"),
    ("list_topics", {}, "ok"),
    ("bind_topic", {"topic": "first", "chat_id": "new-chat"}, "ok"),
    ("remember", {"topic": "alpha", "fact": "PRIVATE_RAW_FACT", "source_agent": "agent-one"}, "ok"),
    ("recall", {"topic": "alpha", "query": "PRIVATE_RAW_QUERY"}, "ok"),
    ("dream", {"topic": "alpha", "dry_run": True, "force": True}, "ok"),
])
def test_all_six_supported_actions_record_one_safe_event(
    service: MemoryService, name: str, arguments: dict[str, Any], expected_status: str,
) -> None:
    CoreMemoryService(service.root).initialize("PRIVATE_CORE_BODY")
    service.reader.dream_change = True
    result = service.call(name, arguments, source="mcp", actor="token:env-admin")
    event = _only_event(service)
    assert event["action"] == name
    assert event["source"] == "mcp"
    assert event["actor"] == "token:env-admin"
    assert event["status"] == expected_status
    assert event["latency_ms"] >= 0
    assert isinstance(event["latency_ms"], int)
    assert len(event["summary"]) <= 300
    assert event["summary"] == f"{name}: {expected_status}"
    if name in {"bind_topic", "remember", "recall", "dream"}:
        assert event["topic_id"] == "alpha"
    if name == "remember":
        assert event["meta"]["fact_chars"] == len(arguments["fact"])
        assert event["meta"]["source_agent"] == "agent-one"
    if name == "recall":
        assert event["meta"]["query_chars"] == len(arguments["query"])
        assert event["meta"]["answer_chars"] == len(result["capsule"])
        assert event["meta"]["route_source"] == "explicit"
        assert event["meta"]["cache"] is False
    if name == "whoami":
        assert event["meta"]["body_chars"] == len("PRIVATE_CORE_BODY")
    serialized = _serialized_activities(service)
    for private in ("PRIVATE_RAW_FACT", "PRIVATE_RAW_QUERY", "PRIVATE_CORE_BODY", "PRIVATE_MODEL_SUMMARY", "PRIVATE_MODEL_CHANGE"):
        assert private not in serialized


@pytest.mark.parametrize("source,expected", [("mcp", "mcp"), ("rest", "rest"), ("panel", "panel"), ("cli", "cli"), ("hermes", "system")])
def test_transport_source_and_actor_are_per_invocation(service: MemoryService, source: str, expected: str) -> None:
    service.call("list_topics", {}, source=source, actor="sso:bounded-nickname")
    assert _only_event(service)["source"] == expected
    assert _only_event(service)["actor"] == "sso:bounded-nickname"


def test_default_actor_and_injected_ledger_persist_across_instances(service: MemoryService, tmp_path: Path) -> None:
    ledger = ActivityLedger(tmp_path / "separate-ledger")
    injected = MemoryService(service.root, service.reader, activity_ledger=ledger)
    assert injected.activities is ledger
    injected.call("list_topics", {}, source="rest")
    reopened = MemoryService(service.root, service.reader, activity_ledger=ActivityLedger(ledger.root))
    assert reopened.activities.list()["total"] == 1
    assert reopened.activities.list()["items"][0]["actor"] == "anonymous"
    assert service.activities.list()["total"] == 0


@pytest.mark.parametrize("name,arguments", [
    ("whoami", {"body": "PRIVATE_VALIDATION_PAYLOAD"}),
    ("list_topics", []), ("bind_topic", {}),
    ("remember", {"fact": " "}), ("recall", {"query": "x" * 4097}),
    ("dream", {"force": "true"}),
    ("recall", {"query": "PRIVATE_VALIDATION_PAYLOAD", "authorization": "Bearer hidden-token"}),
])
def test_argument_validation_failures_record_once_without_request_data(
    service: MemoryService, name: str, arguments: Any,
) -> None:
    with pytest.raises(MemoryServiceError) as caught:
        service.call(name, arguments, source="rest", actor="token:env-admin")
    assert caught.value.to_dict() == {"code": "invalid_request", "message": "Invalid tool arguments."}
    event = _only_event(service)
    assert event["action"] == name
    assert event["status"] == "error"
    assert event["meta"]["error_code"] == "invalid_request"
    serialized = _serialized_activities(service)
    assert "PRIVATE_VALIDATION_PAYLOAD" not in serialized
    assert "hidden-token" not in serialized


@pytest.mark.parametrize("name,arguments,code", [
    ("recall", {"topic": "missing", "query": "private question"}, "routing_error"),
    ("remember", {"topic": "archived", "fact": "private fact"}, "topic_read_only"),
    ("bind_topic", {"topic": "missing", "chat_id": "private binding"}, "routing_error"),
    ("dream", {"topic": "missing"}, "routing_error"),
])
def test_service_errors_record_once_and_keep_public_redaction(
    service: MemoryService, name: str, arguments: dict[str, Any], code: str,
) -> None:
    with pytest.raises(MemoryServiceError) as caught:
        service.call(name, arguments, source="cli")
    assert caught.value.code == code
    event = _only_event(service)
    assert event["status"] == "error"
    assert event["meta"]["error_code"] == code
    assert "private question" not in _serialized_activities(service)
    assert "private fact" not in _serialized_activities(service)
    assert "private binding" not in _serialized_activities(service)


def test_backend_error_is_sanitized_in_public_error_and_activity(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_registry() -> None:
        raise RuntimeError(f"Bearer hidden-backend-token {_SECRET}")

    monkeypatch.setattr(service, "_registry", broken_registry)
    with pytest.raises(MemoryServiceError) as caught:
        service.call("list_topics", {}, source="rest")
    assert caught.value.to_dict() == {"code": "operation_failed", "message": "Operation could not be completed."}
    event = _only_event(service)
    assert event["meta"]["error_code"] == "operation_failed"
    assert _SECRET not in _serialized_activities(service)
    assert "hidden-backend-token" not in _serialized_activities(service)


def test_unknown_tool_keeps_existing_error_and_is_not_supported_by_enum(service: MemoryService) -> None:
    assert {tool["name"] for tool in tool_definitions()} == {"whoami", "recall", "remember", "list_topics", "bind_topic", "dream"}
    with pytest.raises(MemoryServiceError) as caught:
        service.call("topic_edit", {}, source="mcp")
    assert caught.value.code == "unknown_tool"
    assert caught.value.status == 404
    assert service.activities.list()["total"] == 0


def test_duplicate_remember_and_cache_hit_are_logged_per_call(service: MemoryService) -> None:
    arguments = {"topic": "alpha", "fact": "PRIVATE_REMEMBERED_FACT", "source_agent": "agent-one", "idempotency_key": "private-idempotency-key"}
    first = service.call("remember", arguments, source="mcp")
    second = service.call("remember", arguments, source="rest")
    assert first["status"] == "remembered"
    assert second["status"] == "duplicate"
    remembered = service.activities.list(action="remember")
    assert remembered["total"] == 2
    assert {item["status"] for item in remembered["items"]} == {"ok", "duplicate"}
    query = {"topic": "alpha", "query": "PRIVATE_CACHE_QUERY"}
    assert service.call("recall", query, source="mcp")["source"] == "reader"
    assert service.call("recall", query, source="rest")["source"] == "cache"
    recalled = service.activities.list(action="recall")
    assert recalled["total"] == 2
    assert {item["meta"]["cache"] for item in recalled["items"]} == {True, False}
    assert service.history.list()[0].source == "rest"
    assert service.activities.list()["total"] == 4
    assert "PRIVATE_REMEMBERED_FACT" not in _serialized_activities(service)
    assert "private-idempotency-key" not in _serialized_activities(service)


@pytest.mark.parametrize("question,status", [("missing fact", "no_fact"), ("backend failure", "error")])
def test_no_fact_and_reader_failure_return_and_log_correct_status(service: MemoryService, question: str, status: str) -> None:
    result = service.call("recall", {"topic": "alpha", "query": question}, source="mcp")
    assert result["status"] == status
    event = _only_event(service)
    assert event["status"] == status
    assert event["topic_id"] == "alpha"
    assert event["meta"]["status"] == status
    assert len(service.history.list()) == 1
    assert _SECRET not in json.dumps(result)
    assert _SECRET not in _serialized_activities(service)
    assert "hidden-backend-password" not in _serialized_activities(service)


def test_uninitialized_whoami_is_a_skipped_read(service: MemoryService) -> None:
    assert service.call("whoami", {}, source="mcp")["status"] == "uninitialized"
    assert _only_event(service)["status"] == "skipped"


def test_dream_no_change_and_watermark_skip_are_logged_as_skipped(service: MemoryService) -> None:
    assert service.call("dream", {"topic": "alpha"}, source="mcp")["status"] == "no_change"
    assert service.call("dream", {"topic": "alpha"}, source="mcp")["status"] == "skipped_no_new_evidence"
    events = service.activities.list(action="dream")
    assert events["total"] == 2
    assert all(item["status"] == "skipped" for item in events["items"])
    assert {item["meta"]["status"] for item in events["items"]} == {"no_change", "skipped_no_new_evidence"}
    assert "PRIVATE_MODEL_SUMMARY" not in _serialized_activities(service)


@pytest.mark.parametrize("failure,expected", [(False, "skipped"), (True, "error")])
def test_dream_all_aggregates_errors_and_no_ops_without_report_text(service: MemoryService, failure: bool, expected: str) -> None:
    service.reader.dream_failure = failure
    result = service.call("dream", {}, source="rest")
    event = _only_event(service)
    assert event["status"] == expected
    assert event["topic_id"] is None
    assert event["meta"]["topic_count"] == 2
    assert event["meta"]["error_count"] == (2 if failure else 0)
    assert event["meta"]["skipped_count"] == (0 if failure else 2)
    assert len(result["reports"]) == 2
    assert _SECRET not in _serialized_activities(service)
    assert "PRIVATE_MODEL_SUMMARY" not in _serialized_activities(service)
    assert _SECRET not in json.dumps(result)


def test_dream_single_error_does_not_leak_backend_text(service: MemoryService) -> None:
    service.reader.dream_failure = True
    result = service.call("dream", {"topic": "alpha"}, source="mcp")
    assert result["status"] == "error"
    assert result["error"] == "Dream could not be completed."
    assert _only_event(service)["status"] == "error"
    assert _SECRET not in json.dumps(result)
    assert _SECRET not in _serialized_activities(service)


@pytest.mark.parametrize("child_status,expected", [("error", "error"), ("no_fact", "no_fact")])
def test_multi_topic_event_aggregates_child_outcomes_even_with_a_capsule(
    service: MemoryService, child_status: str, expected: str,
) -> None:
    service.reader.beta_failure = child_status == "error"
    question = (
        "Compare Alpha Topic and Beta Topic PRIVATE_MULTI_QUERY"
        if child_status == "error" else "missing fact Alpha Topic Beta Topic PRIVATE_MULTI_QUERY"
    )
    result = service.call("recall", {"query": question}, source="rest")
    # The ledger classifies coverage independently of the established public
    # multi-recall response, which reports no_answered_topics as an error.
    assert result["status"] == ("answered" if child_status == "error" else "error")
    event = _only_event(service)
    assert event["status"] == expected
    assert event["topic_id"] is None
    assert event["meta"]["topic_ids"] == ["alpha", "beta"]
    assert event["meta"]["topic_versions"] == {"alpha": 1, "beta": 1}
    assert event["meta"]["error_count"] == (1 if child_status == "error" else 0)
    assert event["meta"]["no_fact_count"] == (0 if child_status == "error" else 2)
    assert len(service.history.list()) == 1
    for private in ("PRIVATE_MODEL_NAME", "PRIVATE_MULTI_QUERY", _SECRET):
        assert private not in _serialized_activities(service)


def test_actor_source_agent_and_arbitrary_provenance_secrets_are_not_logged(service: MemoryService) -> None:
    service.call("remember", {
        "topic": "alpha", "fact": "PRIVATE_FACT_PAYLOAD", "source_agent": "Bearer hidden-agent-token",
        "source_session": "hidden-session-cookie", "idempotency_key": "hidden-idempotency-key",
        "reason": "password=hidden-reason-password",
    }, source="mcp", actor="sso:cookie=hidden-actor-cookie")
    event = _only_event(service)
    assert event["actor"] == "sso:[redacted]"
    assert event["meta"]["source_agent"] == "[redacted]"
    for private in ("PRIVATE_FACT_PAYLOAD", "hidden-agent-token", "hidden-session-cookie", "hidden-idempotency-key", "hidden-reason-password", "hidden-actor-cookie"):
        assert private not in _serialized_activities(service)


def test_concurrent_actors_do_not_share_mutable_request_context(service: MemoryService) -> None:
    barrier = threading.Barrier(8)

    def call(index: int) -> None:
        barrier.wait(timeout=3)
        service.call("list_topics", {}, source="rest", actor=f"sso:actor-{index}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(call, range(8)))
    events = service.activities.list()
    assert events["total"] == 8
    assert {item["actor"] for item in events["items"]} == {f"sso:actor-{i}" for i in range(8)}
    assert all(item["source"] == "rest" for item in events["items"])


def test_activity_write_failure_preserves_public_backend_redaction(service: MemoryService, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(record: Any) -> None:
        raise OSError(f"Bearer hidden-ledger-credential {_SECRET}")

    monkeypatch.setattr(service.activities, "append", fail)
    with pytest.raises(MemoryServiceError) as caught:
        service.call("list_topics", {}, source="mcp")
    assert caught.value.to_dict() == {"code": "operation_failed", "message": "Operation could not be completed."}
    assert _SECRET not in str(caught.value)


def test_dream_batch_with_one_success_and_one_error_is_not_unconditionally_ok(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.reader.dream_change = True
    real_read = service.reader.read

    def read(request: ReaderRequest) -> ReaderResponse:
        if "BETA_CANONICAL" in request.prefix:
            raise RuntimeError(f"Bearer hidden-batch-token {_SECRET}")
        return real_read(request)

    monkeypatch.setattr(service.reader, "read", read)
    result = service.call("dream", {}, source="rest")
    assert result["consolidated"] == 1
    assert result["errors"] == 1
    event = _only_event(service)
    assert event["status"] == "error"
    assert event["meta"]["consolidated_count"] == 1
    assert event["meta"]["error_count"] == 1
    assert _SECRET not in json.dumps(result)
    assert "hidden-batch-token" not in _serialized_activities(service)


def test_dream_concurrent_write_abort_is_an_error_activity(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_read = service.reader.read

    def read(request: ReaderRequest) -> ReaderResponse:
        response = real_read(request)
        service.store.update_topic("alpha", "Concurrent canonical mutation", expected_version=1)
        return response

    monkeypatch.setattr(service.reader, "read", read)
    result = service.call("dream", {"topic": "alpha"}, source="mcp")
    assert result["status"] == "aborted_concurrent_write"
    event = _only_event(service)
    assert event["status"] == "error"
    assert event["meta"]["before_version"] == 1
    assert event["meta"]["after_version"] == 2


def test_dream_all_without_active_topics_is_skipped(service: MemoryService) -> None:
    service.admin.archive("alpha", expected_version=1)
    service.admin.archive("beta", expected_version=1)
    result = service.call("dream", {}, source="mcp")
    assert result["total_topics"] == 0
    assert result["reports"] == []
    event = _only_event(service)
    assert event["status"] == "skipped"
    assert event["meta"]["topic_count"] == 0
