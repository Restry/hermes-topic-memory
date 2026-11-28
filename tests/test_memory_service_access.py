from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_topic_memory.cache import QuestionCache
from hermes_topic_memory.core_memory import CoreMemoryService
from hermes_topic_memory.evidence import EvidenceLedger
from hermes_topic_memory.mcp_protocol import McpProtocol
from hermes_topic_memory.memory_service import MemoryService, MemoryServiceError
from hermes_topic_memory.principal import Principal
from hermes_topic_memory.reader import ReaderRequest, ReaderResponse
from hermes_topic_memory.router import TopicRegistry
from hermes_topic_memory.topic_admin import TopicAdminService


class _Reader:
    def __init__(self) -> None:
        self.requests: list[ReaderRequest] = []
        self.plan_topics = ["alpha"]
        self.plan_failure = False

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.requests.append(request)
        if request.prefix.startswith("TOPIC PLANNING RULES:"):
            if self.plan_failure:
                raise RuntimeError("htm_" + "backend_secret_" * 3)
            return ReaderResponse(answer=json.dumps({"topics": [
                {"topic": topic, "reason": "relevant"} for topic in self.plan_topics
            ]}), model="test-reader")
        if request.question == "Consolidate this topic memory and return strict JSON only.":
            return ReaderResponse(answer=json.dumps({"action": "no_change"}), model="test-reader")
        if request.question == "Return only the final synthesized answer.":
            return ReaderResponse(answer="Observed shared capsule.", model="test-reader")
        return ReaderResponse(answer="Observed topic capsule.", model="test-reader")


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    admin.create_topic("alpha", "Alpha Topic", ("first",), "ALPHA_CANONICAL", chat_ids=("alpha-chat",))
    admin.create_topic(
        "beta", "Beta Topic", ("second",), "BETA_CANONICAL", make_default=True,
        chat_ids=("beta-chat",), repo_urls=("https://github.com/Example/Private.git",),
        workspace_paths=("/private/workspace",),
    )
    admin.create_topic("global", "Global Topic", (), "GLOBAL_CANONICAL")
    admin.create_topic("archived", "Archived Topic", (), "ARCHIVED_CANONICAL")
    admin.archive("archived", expected_version=1)
    CoreMemoryService(root).initialize("Shared identity")
    return MemoryService(root, _Reader())


def _principal(role: str = "write", topics: frozenset[str] | None = frozenset({"alpha"})) -> Principal:
    return Principal(
        mode="api_key", actor="key:agent-one(key_0123456789ab)", role=role,
        key_id="key_0123456789ab", allowed_topics=topics,
    )


def _event(service: MemoryService) -> dict[str, Any]:
    events = service.activities.list()["items"]
    assert len(events) == 1
    return events[0]


def _assert_denied(service: MemoryService, name: str, arguments: dict[str, Any], code: str) -> None:
    before_registry = (service.root / "topic-registry.json").read_bytes()
    before_memory = service.store.get_topic("beta")
    before_evidence = EvidenceLedger(service.root).records("beta")
    with pytest.raises(MemoryServiceError) as caught:
        service.call(name, arguments, source="rest", actor="untrusted-argument", principal=_principal())
    assert caught.value.code == code
    assert caught.value.status == 403
    event = _event(service)
    assert event["status"] == "denied"
    assert event["meta"]["error_code"] == code
    assert event["actor"] == _principal().actor
    assert event["meta"]["key_id"] == _principal().key_id
    assert event["topic_id"] is None
    assert service.reader.requests == []
    assert service.history.list() == ()
    assert (service.root / "topic-registry.json").read_bytes() == before_registry
    assert service.store.get_topic("beta") == before_memory
    assert EvidenceLedger(service.root).records("beta") == before_evidence


@pytest.mark.parametrize("role", ["read", "write", "admin"])
@pytest.mark.parametrize("name,arguments", [
    ("whoami", {}), ("list_topics", {}),
    ("recall", {"topic": "first", "query": "observed facts"}),
    ("remember", {"topic": "first", "fact": "Observed fact"}),
    ("bind_topic", {"topic": "first", "chat_id": "new-chat"}),
    ("dream", {"topic": "first", "dry_run": True}),
])
def test_role_matrix_and_principal_attribution(
    service: MemoryService, role: str, name: str, arguments: dict[str, Any],
) -> None:
    principal = _principal(role)
    if role == "read" and name in {"remember", "bind_topic", "dream"}:
        with pytest.raises(MemoryServiceError) as caught:
            service.call(name, arguments, principal=principal, actor="not-the-actor")
        assert caught.value.code == "forbidden"
        assert caught.value.status == 403
        assert _event(service)["status"] == "denied"
        assert service.reader.requests == []
    else:
        result = service.call(name, arguments, principal=principal, actor="not-the-actor")
        assert _event(service)["status"] != "denied"
        if name == "list_topics":
            assert [topic["topic_id"] for topic in result["topics"]] == ["alpha"]
    assert _event(service)["actor"] == principal.actor
    assert _event(service)["meta"]["key_id"] == principal.key_id


@pytest.mark.parametrize("target", ["beta", "second", "Beta Topic"])
@pytest.mark.parametrize("name,extra", [
    ("recall", {"query": "private facts"}), ("remember", {"fact": "Private append"}),
    ("bind_topic", {"chat_id": "new-private-binding"}), ("dream", {"force": True}),
])
def test_disallowed_explicit_ids_display_names_and_aliases_have_no_effects(
    service: MemoryService, target: str, name: str, extra: dict[str, Any],
) -> None:
    _assert_denied(service, name, {"topic": target, **extra}, "topic_forbidden")


@pytest.mark.parametrize("context", [
    {"chat_id": "beta-chat"}, {"repo": "git@github.com:Example/Private.git"},
    {"workspace_path": "/private/workspace/child"},
])
@pytest.mark.parametrize("name,payload", [("recall", {"query": "observed facts"}), ("remember", {"fact": "Observed fact"})])
def test_disallowed_resolved_context_is_checked_before_reader_or_writes(
    service: MemoryService, context: dict[str, str], name: str, payload: dict[str, str],
) -> None:
    _assert_denied(service, name, {**context, **payload}, "topic_forbidden")


def test_remember_cannot_use_disallowed_configured_default(service: MemoryService) -> None:
    _assert_denied(service, "remember", {"fact": "Observed fact"}, "topic_forbidden")


def test_scoped_listing_does_not_read_other_canonical_topics(service: MemoryService, monkeypatch: pytest.MonkeyPatch) -> None:
    get_topic = service.store.get_topic
    loaded: list[str] = []

    def only_allowed(topic_id: str):
        loaded.append(topic_id)
        assert topic_id == "alpha"
        return get_topic(topic_id)

    monkeypatch.setattr(service.store, "get_topic", only_allowed)
    result = service.call("list_topics", {}, principal=_principal("read"))
    assert loaded == ["alpha"]
    assert [topic["topic_id"] for topic in result["topics"]] == ["alpha"]
    assert _event(service)["meta"]["topic_count"] == 1
    assert _event(service)["meta"]["topic_ids"] == ["alpha"]
    assert service.activities.list(allowed_topics=frozenset({"alpha"}))["total"] == 1


def test_scoped_auto_recall_restricts_planner_metadata_and_ignores_foreign_cache(service: MemoryService) -> None:
    query = "Which deployment facts are stored?"
    QuestionCache(service.root).put(
        question=query, answer="PRIVATE_BETA_CACHED_CAPSULE", topic_id="beta", topic_version=1,
        model="test-reader", prompt_version="test",
    )
    result = service.call("recall", {"query": query}, principal=_principal("read"))
    assert result["topic_id"] == "alpha"
    assert result["route_source"] == "catalog_planner"
    assert result["source"] == "reader"
    assert len(service.reader.requests) == 2
    planning, reading = service.reader.requests
    catalog = json.loads(planning.prefix.split("TOPIC REGISTRY METADATA:\n", 1)[1].split(
        "\nEND TOPIC REGISTRY METADATA", 1,
    )[0])
    assert catalog == [{"topic_id": "alpha", "display_name": "Alpha Topic", "aliases": ["first"]}]
    for hidden in ("beta", "second", "Beta Topic", "global", "archived", "BETA_CANONICAL"):
        assert hidden not in json.dumps(catalog)
        assert hidden not in reading.prefix
        assert hidden not in json.dumps(result)
    assert "PRIVATE_BETA_CACHED_CAPSULE" not in result["capsule"]
    assert [topic.topic_id for topic in service.history.list()[0].selected_topics] == ["alpha"]


@pytest.mark.parametrize("failure", ["model", "unknown_candidate"])
def test_scoped_planner_failure_never_widens_to_default_or_global(service: MemoryService, failure: str) -> None:
    service.reader.plan_failure = failure == "model"
    service.reader.plan_topics = ["second"]
    with pytest.raises(MemoryServiceError) as caught:
        service.call("recall", {"query": "Which deployment facts are stored?"}, principal=_principal("read"))
    assert caught.value.code == "routing_error"
    assert len(service.reader.requests) == 1
    assert service.history.list() == ()
    assert _event(service)["status"] == "error"
    assert "backend_secret" not in json.dumps(_event(service))


@pytest.mark.parametrize("topics", [frozenset(), frozenset({"missing"}), frozenset({"archived"})])
def test_no_scoped_active_candidates_fails_without_planner_or_reads(service: MemoryService, topics: frozenset[str]) -> None:
    with pytest.raises(MemoryServiceError) as caught:
        service.call("recall", {"query": "Which facts are stored?"}, principal=_principal("read", topics))
    assert caught.value.code == "topic_forbidden"
    assert service.reader.requests == []
    assert service.history.list() == ()
    assert _event(service)["status"] == "denied"


def test_scoped_query_mentions_cannot_add_foreign_candidate(service: MemoryService) -> None:
    result = service.call("recall", {"query": "Compare Alpha Topic and Beta Topic"}, principal=_principal("read"))
    assert result["topic_id"] == "alpha"
    assert result["route_source"] == "query"
    assert len(service.reader.requests) == 1
    assert "BETA_CANONICAL" not in service.reader.requests[0].prefix
    assert "beta" not in json.dumps(result)


def test_scoped_dream_all_only_processes_allowed_active_topics(service: MemoryService) -> None:
    result = service.call("dream", {"dry_run": True}, principal=_principal())
    assert result["total_topics"] == 1
    assert [report["topic_id"] for report in result["reports"]] == ["alpha"]
    assert len(service.reader.requests) == 1
    assert "ALPHA_CANONICAL" in service.reader.requests[0].prefix
    assert "BETA_CANONICAL" not in service.reader.requests[0].prefix
    assert "GLOBAL_CANONICAL" not in service.reader.requests[0].prefix
    assert _event(service)["meta"]["topic_count"] == 1
    assert _event(service)["meta"]["topic_ids"] == ["alpha"]
    assert service.activities.list(allowed_topics=frozenset({"alpha"}))["total"] == 1


def test_multi_topic_activity_retains_full_allowed_set(service: MemoryService) -> None:
    principal = _principal("read", frozenset({"alpha", "beta"}))
    result = service.call("recall", {"query": "Compare Alpha Topic and Beta Topic"}, principal=principal)
    assert {topic["topic_id"] for topic in result["selected_topics"]} == {"alpha", "beta"}
    event = _event(service)
    assert event["topic_id"] is None
    assert set(event["meta"]["topic_ids"]) == {"alpha", "beta"}
    assert service.activities.list(allowed_topics=frozenset({"alpha"}))["total"] == 0


def test_principal_and_argument_managed_secrets_never_enter_activity(service: MemoryService) -> None:
    secret = "htm_" + "Ab_cd-09" * 4
    principal = Principal(mode="api_key", role="write", actor=f"key:{secret}(key_0123456789ab)", key_id="key_0123456789ab")
    service.call("remember", {
        "topic": "alpha", "fact": f"Stored fact {secret}", "source_agent": f"agent-{secret}",
        "reason": secret, "source_session": secret,
    }, principal=principal)
    serialized = json.dumps(_event(service))
    assert secret not in serialized
    assert "Ab_cd-09" not in serialized
    assert _event(service)["meta"]["key_id"] == principal.key_id
    assert "[redacted]" in _event(service)["actor"]
    assert "[redacted]" in _event(service)["meta"]["source_agent"]


@pytest.mark.parametrize("name,arguments,expected_code", [
    ("remember", {"topic": "alpha", "fact": "Observed fact"}, "forbidden"),
    ("recall", {"topic": "second", "query": "Observed facts"}, "topic_forbidden"),
])
def test_mcp_principal_denials_are_tool_errors_not_protocol_errors(
    service: MemoryService, name: str, arguments: dict[str, Any], expected_code: str,
) -> None:
    status, response = McpProtocol(service).dispatch(json.dumps({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments},
    }).encode(), actor="not-the-actor", principal=_principal("read"))
    assert status == 200
    assert response["id"] == 7
    assert "error" not in response
    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == expected_code
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert _event(service)["actor"] == _principal().actor
    assert _event(service)["meta"]["key_id"] == _principal().key_id
    assert _event(service)["status"] == "denied"


def test_mcp_principal_keeps_invalid_params_and_notification_semantics(service: MemoryService) -> None:
    protocol = McpProtocol(service)
    body = {"jsonrpc": "2.0", "id": "bad", "method": "tools/call", "params": {"name": "recall", "arguments": {}}}
    status, response = protocol.dispatch(json.dumps(body).encode(), principal=_principal("read"))
    assert status == 200
    assert response["error"]["code"] == -32602
    body.pop("id")
    assert protocol.dispatch(json.dumps(body).encode(), principal=_principal("read")) == (202, None)
    assert all(item["meta"]["key_id"] == _principal().key_id for item in service.activities.list()["items"])


@pytest.mark.parametrize("count", [8, 9])
@pytest.mark.parametrize("name,arguments", [("list_topics", {}), ("dream", {"dry_run": True})])
def test_aggregate_activity_requires_complete_bounded_topic_set(
    service: MemoryService, count: int, name: str, arguments: dict[str, Any],
) -> None:
    topic_ids = {"alpha", "beta", "global"}
    for index in range(count - len(topic_ids)):
        topic_id = f"extra-{index}"
        service.admin.create_topic(topic_id, f"Extra Topic {index}", (), "EXTRA_CANONICAL")
        topic_ids.add(topic_id)
    service.call(name, arguments, principal=_principal("admin", None))
    event = _event(service)
    assert event["meta"]["topic_count"] == count
    if count == 8:
        assert set(event["meta"]["topic_ids"]) == topic_ids
        assert service.activities.list(allowed_topics=frozenset(topic_ids))["total"] == 1
    else:
        assert "topic_ids" not in event["meta"]
        assert service.activities.list(allowed_topics=frozenset(topic_ids))["total"] == 0
    assert service.activities.list(allowed_topics=frozenset({"alpha"}))["total"] == 0


def test_direct_actor_only_callers_preserve_existing_unrestricted_contract(service: MemoryService) -> None:
    result = service.call("remember", {"topic": "second", "fact": "Observed fact"}, source="cli", actor="cli:legacy")
    assert result["topic_id"] == "beta"
    assert _event(service)["actor"] == "cli:legacy"
    assert "key_id" not in _event(service)["meta"]
    assert {topic.topic_id for topic in TopicRegistry.from_file(service.root / "topic-registry.json").topics} == {"alpha", "beta", "global", "archived"}
