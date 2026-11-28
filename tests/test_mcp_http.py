from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from collections.abc import Iterator
from dataclasses import dataclass
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest

from hermes_topic_memory.activity_log import ActivityLedger
import hermes_topic_memory.api_keys as api_keys
from hermes_topic_memory.core_memory import CoreMemoryService
from hermes_topic_memory.evidence import EvidenceLedger
from hermes_topic_memory.panel_app import PanelApplication
from hermes_topic_memory.panel_server import create_panel_server
from hermes_topic_memory.query_history import QueryHistoryStore
from hermes_topic_memory.reader import ReaderRequest, ReaderResponse
from hermes_topic_memory.store import FileTopicStore
from hermes_topic_memory.topic_admin import TopicAdminService

_TOKEN = "private-http-token"
_BACKEND_SECRET = "PRIVATE_READER_CREDENTIAL"


class _Reader:
    def __init__(self) -> None:
        self.requests: list[ReaderRequest] = []
        self.planner_output = "not-json"

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.requests.append(request)
        if "TOPIC PLANNING RULES" in request.prefix:
            return ReaderResponse(answer=self.planner_output, model="test-planner")
        if "DREAM MEMORY CONSOLIDATION RULES" in request.prefix:
            return ReaderResponse(answer=json.dumps({
                "action": "no_change", "new_body": "", "summary": "No durable changes.", "changes": [],
            }), model="test-dream")
        if request.question == "backend failure":
            raise RuntimeError(_BACKEND_SECRET)
        if request.question == "oversized answer":
            return ReaderResponse(answer="x" * 201, model="test-reader")
        if request.question == "missing fact":
            return ReaderResponse(answer="no stored fact answers the question", model="test-reader")
        topic = "beta" if "BETA_CANONICAL" in request.prefix else "alpha"
        return ReaderResponse(answer=f"{topic} recalled capsule", model="test-reader")


@dataclass
class _RunningService:
    address: tuple[str, int]
    root: Path
    reader: _Reader
    app: PanelApplication

    def request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        raw: bytes | None = None,
        token: str | None = _TOKEN,
    ) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        body = raw if raw is not None else (None if payload is None else json.dumps(payload).encode())
        connection = HTTPConnection(*self.address, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            assert response.getheader("Cache-Control") == "no-store"
            return response.status, json.loads(data) if data else None
        finally:
            connection.close()

    def rpc(self, method: str, params: Any = None, *, request_id: Any = 1, token: str | None = _TOKEN) -> tuple[int, Any]:
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        return self.request("POST", "/mcp", payload, token=token)

    def tool(self, name: str, arguments: dict[str, Any] | None = None, *, token: str | None = _TOKEN) -> dict[str, Any]:
        status, response = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, token=token)
        assert status == 200
        assert "error" not in response
        result = response["result"]
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
        return result


@pytest.fixture
def service(tmp_path: Path) -> Iterator[_RunningService]:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    admin.create_topic("alpha", "Alpha Topic", ("first",), "ALPHA_CANONICAL", make_default=True)
    admin.create_topic("beta", "Beta Topic", ("second",), "BETA_CANONICAL", chat_ids=("beta-chat",))
    admin.create_topic("archived", "Archived Topic", (), "ARCHIVED_CANONICAL")
    admin.archive("archived", expected_version=1)
    admin.create_topic("trashed", "Trashed Topic", (), "TRASHED_CANONICAL")
    admin.trash("trashed", expected_version=1)
    reader = _Reader()
    application = PanelApplication(root, reader, _TOKEN)
    server = create_panel_server(application, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _RunningService(server.server_address[:2], root, reader, application)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_initialize_notification_and_tool_discovery(service: _RunningService) -> None:
    for params, version in (({}, "2024-11-05"), ({"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}, "2025-03-26")):
        status, response = service.rpc("initialize", params, request_id="init")
        assert status == 200
        assert response["id"] == "init"
        result = response["result"]
        assert result["protocolVersion"] == version
        assert result["serverInfo"]["name"] == "hermes-topic-memory"
        assert result["capabilities"] == {"tools": {}}
    status, response = service.request("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status in {202, 204}
    assert response is None
    status, response = service.rpc("tools/list")
    assert status == 200
    tools = {item["name"]: item for item in response["result"]["tools"]}
    assert set(tools) == {"whoami", "recall", "remember", "list_topics", "bind_topic", "dream"}
    assert tools["recall"]["inputSchema"]["required"] == ["query"]
    assert tools["remember"]["inputSchema"]["required"] == ["fact"]
    assert tools["bind_topic"]["inputSchema"]["required"] == ["topic"]
    assert set(tools["recall"]["inputSchema"]["properties"]) == {"query", "topic", "chat_id", "repo", "workspace_path"}


def test_whoami_and_topic_listing_use_shared_rest_and_mcp_service(service: _RunningService) -> None:
    uninitialized = service.tool("whoami")["structuredContent"]
    assert uninitialized["status"] == "uninitialized"
    assert uninitialized["notice"]["code"] == "core_memory_uninitialized"
    status, rest = service.request("GET", "/api/v1/whoami")
    assert status == 200
    assert rest == uninitialized
    CoreMemoryService(service.root).initialize("Shared identity")
    identity = service.tool("whoami")["structuredContent"]
    assert identity == {"status": "initialized", "body": "Shared identity", "version": 1}
    assert service.request("GET", "/api/v1/whoami") == (200, identity)
    topics = service.tool("list_topics")["structuredContent"]
    assert service.request("GET", "/api/v1/topics") == (200, topics)
    assert [item["topic_id"] for item in topics["topics"]] == ["alpha", "beta"]
    assert topics["topics"][1]["chat_ids"] == ["beta-chat"]
    assert all(item["version"] == 1 and item["repo_urls"] == [] and item["workspace_paths"] == [] for item in topics["topics"])
    # The preexisting Panel API remains available on the same server.
    status, legacy = service.request("GET", "/api/topics/alpha/body")
    assert status == 200
    assert legacy["body"] == "ALPHA_CANONICAL"


def test_context_bindings_route_recall_and_persist_history(service: _RunningService, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bound = service.tool("bind_topic", {"topic": "second", "repo_url": "git@github.com:Example/Project.git", "workspace_path": str(workspace)})
    assert not bound["isError"]
    assert bound["structuredContent"]["topic"]["repo_urls"] == ["example/project"]
    status, rest_bound = service.request("POST", "/api/v1/topics/bind", {"topic": "first", "chat_id": "alpha-chat"})
    assert status == 200
    assert rest_bound["topic"]["chat_ids"] == ["alpha-chat"]
    cases = (
        ({"query": "which decision", "topic": "first", "chat_id": "beta-chat"}, "alpha", "explicit"),
        ({"query": "which decision", "chat_id": "beta-chat"}, "beta", "chat_id"),
        ({"query": "which decision", "repo": "https://github.com/example/project.git"}, "beta", "repo"),
        ({"query": "which decision", "workspace_path": str(workspace / "subdir")}, "beta", "workspace_path"),
        ({"query": "Beta Topic decision"}, "beta", "query"),
        ({"query": "which decision"}, "alpha", "default"),
    )
    for index, (arguments, topic_id, route_source) in enumerate(cases):
        if index % 2:
            status, result = service.request("POST", "/api/v1/recall", arguments)
            assert status == 200
        else:
            result = service.tool("recall", arguments)["structuredContent"]
        assert result["topic_id"] == topic_id
        assert result["topic_version"] == 1
        assert result["route_source"] == route_source
        assert result["status"] == "answered"
        assert len(result["capsule"]) <= 200
        record = QueryHistoryStore(service.root).get(result["query_id"])
        assert record is not None
        assert record.question == arguments["query"]
        assert record.source == ("rest" if index % 2 else "mcp")
        assert record.selection_reasons == {topic_id: route_source}
        assert record.final_answer == result["capsule"]
    assert len(QueryHistoryStore(service.root).list()) == len(cases)
    cached = service.tool("recall", {"query": "which decision", "topic": "alpha"})["structuredContent"]
    assert cached["source"] == "cache"
    assert len(QueryHistoryStore(service.root).list()) == len(cases) + 1


def test_remember_routes_context_and_deduplicates_across_transports(service: _RunningService) -> None:
    arguments = {
        "fact": "A durable shared decision.",
        "chat_id": "beta-chat",
        "source_agent": "agent-one",
        "source_session": "session-one",
        "idempotency_key": "shared-request",
        "reason": "User supplied a durable fact.",
    }
    first = service.tool("remember", arguments)["structuredContent"]
    assert first["topic_id"] == "beta"
    assert first["topic_version"] == 2
    assert first["status"] == "remembered"
    status, duplicate = service.request("POST", "/api/v1/remember", arguments)
    assert status == 200
    assert duplicate == {**first, "status": "duplicate"}
    assert FileTopicStore(service.root).get_topic("beta").metadata.version == 2
    evidence = EvidenceLedger(service.root).get(first["evidence_id"])
    assert evidence.fact == arguments["fact"]
    assert evidence.source_agent == "agent-one"
    assert evidence.source_session == "session-one"
    assert evidence.idempotency_key == "shared-request"
    status, defaulted = service.request("POST", "/api/v1/remember", {"fact": "Default routing fact."})
    assert status == 200
    assert defaulted["topic_id"] == "alpha"
    assert EvidenceLedger(service.root).get(defaulted["evidence_id"]).source_agent == "unknown"
    # A fresh Reader call after remember observes the current topic version.
    recalled = service.tool("recall", {"query": "newly remembered", "topic": "second"})["structuredContent"]
    assert recalled["topic_version"] == 2
    assert recalled["source"] == "reader"


@pytest.mark.parametrize("path,method", [("/mcp", "POST"), ("/api/v1/whoami", "GET"), ("/api/v1/topics", "GET"), ("/api/v1/recall", "POST"), ("/api/v1/remember", "POST"), ("/api/v1/topics/bind", "POST"), ("/api/v1/dream", "POST")])
def test_every_service_endpoint_requires_bearer_auth(service: _RunningService, path: str, method: str) -> None:
    for token in (None, "wrong-token"):
        status, response = service.request(method, path, raw=b"private malformed body", token=token)
        assert status == 401
        assert response["error"]["code"] == "unauthorized"
        assert "private malformed body" not in json.dumps(response)
    assert QueryHistoryStore(service.root).list() == ()
    assert FileTopicStore(service.root).get_topic("alpha").metadata.version == 1


@pytest.mark.parametrize("payload,expected", [([], -32600), ({"jsonrpc": "1.0", "id": 1, "method": "tools/list"}, -32600), ({"jsonrpc": "2.0", "id": True, "method": "tools/list"}, -32600), ({"jsonrpc": "2.0", "id": {}, "method": "tools/list"}, -32600), ({"jsonrpc": "2.0", "id": 1}, -32600)])
def test_malformed_jsonrpc_envelopes(service: _RunningService, payload: Any, expected: int) -> None:
    status, response = service.request("POST", "/mcp", payload)
    assert status == 400
    assert response["error"]["code"] == expected
    assert response["id"] is None


def test_protocol_and_argument_errors_are_structured(service: _RunningService) -> None:
    for body in (b"{broken", b"\xff", b'{"jsonrpc":"2.0","id":NaN,"method":"tools/list"}'):
        status, response = service.request("POST", "/mcp", raw=body)
        assert status == 400
        assert response["error"]["code"] == -32700
    for method, params, code in (
        ("unknown", {}, -32601),
        ("tools/list", [], -32602),
        ("initialize", {"protocolVersion": 7}, -32602),
        ("tools/call", {"name": "unknown"}, -32602),
        ("tools/call", {"name": "recall", "arguments": {}}, -32602),
        ("tools/call", {"name": "remember", "arguments": {"fact": "ok", "topic_id": "alpha"}}, -32602),
        ("tools/call", {"name": "whoami", "arguments": []}, -32602),
    ):
        status, response = service.rpc(method, params, request_id="error")
        assert status == 200
        assert response["id"] == "error"
        assert response["error"]["code"] == code
    assert service.request("GET", "/mcp")[0] == 405
    assert service.request("GET", "/api/v1/remember")[0] == 405
    status, response = service.request("POST", "/api/v1/recall", raw=b"{broken")
    assert status == 400
    assert response["error"]["code"] == "invalid_json"
    status, response = service.request("POST", "/api/v1/remember", {"fact": ""})
    assert status == 400
    assert response["error"]["code"] == "invalid_request"
    assert service.request("POST", "/api/v1/topics/bind", {"topic": "alpha"})[0] == 400


def test_reader_failures_and_no_fact_results_are_safe_and_durable(service: _RunningService) -> None:
    for query, expected in (("backend failure", "error"), ("oversized answer", "error"), ("missing fact", "no_fact")):
        result = service.tool("recall", {"query": query, "topic": "alpha"})
        content = result["structuredContent"]
        assert content["status"] == expected
        assert content["capsule"] is None
        assert result["isError"] == (expected == "error")
        assert _BACKEND_SECRET not in json.dumps(result)
        record = QueryHistoryStore(service.root).get(content["query_id"])
        assert record is not None
        assert record.selected_topics[0].status == expected
    failed = service.tool("recall", {"query": "anything", "topic": "does-not-exist"})
    assert failed["isError"] is True
    assert failed["structuredContent"]["error"]["code"] == "routing_error"
    status, response = service.request("POST", "/api/v1/recall", {"query": "anything", "topic": "does-not-exist"})
    assert status == 400
    assert response["error"]["code"] == "routing_error"
    assert len(QueryHistoryStore(service.root).list()) == 3


_SERVICE_ROUTES = {
    "whoami": ("GET", "/api/v1/whoami"),
    "list_topics": ("GET", "/api/v1/topics"),
    "recall": ("POST", "/api/v1/recall"),
    "remember": ("POST", "/api/v1/remember"),
    "bind_topic": ("POST", "/api/v1/topics/bind"),
    "dream": ("POST", "/api/v1/dream"),
}


def _issued_key(service: _RunningService, role: str, *, topics: tuple[str, ...] | None = None):
    return service.app.api_keys.create_key(
        name=f"integration-{role}", role=role, created_by="token:env-admin", allowed_topics=topics,
    )


def _service_call(service: _RunningService, transport: str, token: str, name: str, arguments: dict[str, Any]):
    if transport == "mcp":
        result = service.tool(name, arguments, token=token)
        denied = result["isError"] and result["structuredContent"].get("error", {}).get("code") in {"forbidden", "topic_forbidden"}
        return (403 if denied else 200), result["structuredContent"]
    method, path = _SERVICE_ROUTES[name]
    return service.request(method, path, arguments if method == "POST" else None, token=token)


def _domain_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "activity-log" not in path.relative_to(root).parts
        and not path.name.endswith(".lock") and path.name != "api-keys.json"
    }


def _new_key_event(service: _RunningService, before: set[str], record, *, action: str, status: str, source: str):
    events = ActivityLedger(service.root).list(page_size=100)["items"]
    new = [event for event in events if event["event_id"] not in before]
    assert len(new) == 1
    event = new[0]
    assert event["action"] == action
    assert event["source"] == source
    assert event["status"] == status
    assert event["actor"] == f"key:{record.name}({record.key_id})"
    assert event["meta"]["key_id"] == record.key_id
    return event


@pytest.mark.parametrize("transport", ("rest", "mcp"))
@pytest.mark.parametrize("name,arguments", (
    ("whoami", {}), ("list_topics", {}), ("recall", {"topic": "first", "query": "allowed read"}),
    ("remember", {"topic": "alpha", "fact": "Forbidden or allowed durable fact."}),
    ("bind_topic", {"topic": "first", "chat_id": "role-matrix-chat"}),
    ("dream", {"topic": "alpha", "force": True}),
))
@pytest.mark.parametrize("role", ("read", "write", "admin"))
def test_store_issued_keys_enforce_service_role_matrix_and_actor(
    service: _RunningService, transport: str, name: str, arguments: dict[str, Any], role: str,
) -> None:
    record, secret = _issued_key(service, role)
    before = {event["event_id"] for event in ActivityLedger(service.root).list(page_size=100)["items"]}
    domain = _domain_snapshot(service.root)
    requests = len(service.reader.requests)
    status, result = _service_call(service, transport, secret, name, arguments)
    forbidden = role == "read" and name in {"remember", "bind_topic", "dream"}
    assert status == (403 if forbidden else 200), result
    if forbidden:
        assert result["error"]["code"] == "forbidden"
        assert _domain_snapshot(service.root) == domain
        assert len(service.reader.requests) == requests
        event_status = "denied"
    else:
        assert result.get("error") is None
        event_status = "skipped" if name in {"whoami", "dream"} else "ok"
        if name == "remember":
            assert result["status"] == "remembered"
            assert FileTopicStore(service.root).get_topic("alpha").metadata.version == 2
        elif name == "bind_topic":
            assert result["topic"]["chat_ids"] == ["role-matrix-chat"]
        elif name == "recall":
            assert result["topic_id"] == "alpha" and result["capsule"] == "alpha recalled capsule"
        elif name == "dream":
            assert result["status"] == "no_change"
            assert len(service.reader.requests) == requests + 1
    event = _new_key_event(service, before, record, action=name, status=event_status, source=transport)
    if forbidden:
        assert event["meta"]["error_code"] == "forbidden"
    persisted = "".join(path.read_text() for path in (service.root / "activity-log").glob("*.jsonl"))
    assert secret not in persisted and _TOKEN not in persisted
    assert arguments.get("fact", "unused-private-fact") not in persisted


@pytest.mark.parametrize("transport", ("rest", "mcp"))
def test_scoped_topic_list_filters_all_metadata_and_global_default(service: _RunningService, transport: str) -> None:
    _, secret = _issued_key(service, "read", topics=("beta",))
    status, result = _service_call(service, transport, secret, "list_topics", {})
    assert status == 200
    assert [topic["topic_id"] for topic in result["topics"]] == ["beta"]
    serialized = json.dumps(result)
    assert "Alpha Topic" not in serialized and "first" not in serialized
    assert "Archived Topic" not in serialized and "Trashed Topic" not in serialized
    assert result.get("default_topic") in {None, "beta"}


@pytest.mark.parametrize("transport", ("rest", "mcp"))
@pytest.mark.parametrize("name,arguments", (
    ("recall", {"query": "forbidden explicit", "topic": "second"}),
    ("recall", {"query": "forbidden bound", "chat_id": "beta-chat"}),
    ("remember", {"fact": "MUST_NOT_BE_STORED", "topic": "second"}),
    ("remember", {"fact": "MUST_NOT_BE_STORED", "chat_id": "beta-chat"}),
    ("bind_topic", {"topic": "second", "chat_id": "must-not-bind"}),
    ("dream", {"topic": "second", "force": True}),
))
def test_scoped_service_alias_and_context_denials_precede_all_domain_effects(
    service: _RunningService, transport: str, name: str, arguments: dict[str, Any],
) -> None:
    record, secret = _issued_key(service, "write", topics=("alpha",))
    before = {event["event_id"] for event in ActivityLedger(service.root).list(page_size=100)["items"]}
    domain = _domain_snapshot(service.root)
    requests = len(service.reader.requests)
    status, result = _service_call(service, transport, secret, name, arguments)
    assert status == 403 and result["error"]["code"] == "topic_forbidden"
    assert _domain_snapshot(service.root) == domain
    assert len(service.reader.requests) == requests
    event = _new_key_event(service, before, record, action=name, status="denied", source=transport)
    assert event["meta"]["error_code"] == "topic_forbidden"


@pytest.mark.parametrize("transport", ("rest", "mcp"))
@pytest.mark.parametrize("context", ("repo", "workspace_path"))
def test_scoped_service_checks_resolved_repository_and_workspace_context(
    service: _RunningService, tmp_path: Path, transport: str, context: str,
) -> None:
    workspace = tmp_path / "beta-workspace"
    workspace.mkdir()
    service.tool("bind_topic", {"topic": "beta", "repo_url": "git@github.com:Example/Beta.git", "workspace_path": str(workspace)})
    _, secret = _issued_key(service, "write", topics=("alpha",))
    value = "https://github.com/example/beta.git" if context == "repo" else str(workspace / "nested")
    domain = _domain_snapshot(service.root)
    requests = len(service.reader.requests)
    for name, field in (("recall", "query"), ("remember", "fact")):
        status, result = _service_call(service, transport, secret, name, {field: "forbidden context", context: value})
        assert status == 403 and result["error"]["code"] == "topic_forbidden"
        assert _domain_snapshot(service.root) == domain
        assert len(service.reader.requests) == requests
    status, allowed = _service_call(service, transport, secret, "recall", {"query": "allowed explicit", "topic": "first", context: value})
    assert status == 200 and allowed["topic_id"] == "alpha"


@pytest.mark.parametrize("transport", ("rest", "mcp"))
def test_scoped_auto_recall_only_plans_and_reads_allowed_topics(service: _RunningService, transport: str) -> None:
    _, secret = _issued_key(service, "read", topics=("beta",))
    service.reader.planner_output = '{"topics":[{"topic":"second","reason":"allowed match"}]}'
    status, result = _service_call(service, transport, secret, "recall", {"query": "Which stored decision applies?"})
    assert status == 200 and result["topic_id"] == "beta"
    assert result["capsule"] == "beta recalled capsule"
    planner_requests = [request for request in service.reader.requests if "TOPIC PLANNING RULES" in request.prefix]
    assert planner_requests
    for request in planner_requests:
        assert "Beta Topic" in request.prefix
        assert "Alpha Topic" not in request.prefix and "first" not in request.prefix
    for request in service.reader.requests:
        assert "ALPHA_CANONICAL" not in request.prefix
    record = QueryHistoryStore(service.root).get(result["query_id"])
    assert record is not None and [item.topic_id for item in record.selected_topics] == ["beta"]


@pytest.mark.parametrize("transport", ("rest", "mcp"))
def test_scoped_dream_all_only_processes_allowed_topics(service: _RunningService, transport: str) -> None:
    _, secret = _issued_key(service, "write", topics=("alpha",))
    beta_before = FileTopicStore(service.root).get_topic("beta")
    status, result = _service_call(service, transport, secret, "dream", {"force": True})
    assert status == 200, result
    assert service.reader.requests
    assert all("BETA_CANONICAL" not in request.prefix for request in service.reader.requests)
    assert FileTopicStore(service.root).get_topic("beta") == beta_before
    assert "Beta Topic" not in json.dumps(result)


def test_direct_service_anonymous_calls_remain_unrestricted_by_http_principals(service: _RunningService) -> None:
    _issued_key(service, "read", topics=("alpha",))
    result = service.app.memory_service.call("remember", {"topic": "second", "fact": "Direct service compatibility."})
    assert result["topic_id"] == "beta" and result["status"] == "remembered"
    events = ActivityLedger(service.root).list(action="remember")["items"]
    assert len(events) == 1 and events[0]["actor"] == "anonymous"
    assert "key_id" not in events[0]["meta"]


@pytest.mark.parametrize("admin_mode", ("env", "managed"))
def test_admin_http_key_lifecycle_is_one_time_public_idempotent_and_attributed(service: _RunningService, admin_mode: str) -> None:
    admin_record = None
    token = _TOKEN
    actor = "token:env-admin"
    if admin_mode == "managed":
        admin_record, token = _issued_key(service, "admin")
        actor = f"key:{admin_record.name}({admin_record.key_id})"
    status, me = service.request("GET", "/api/auth/me", token=token)
    assert status == 200 and me["authenticated"] is True
    assert me["mode"] == ("token" if admin_mode == "env" else "api_key")
    assert me["role"] == "admin" and me["actor"] == actor
    assert me["key_id"] == (admin_record.key_id if admin_record else None)
    status, created = service.request("POST", "/api/keys", {
        "name": "  reader-client  ", "role": "read", "allowed_topics": ["alpha"], "expires_in_days": 7,
    }, token=token)
    assert status == 201
    key, secret = created["key"], created["secret"]
    assert key["name"] == "reader-client" and key["role"] == "read"
    assert key["allowed_topics"] == ["alpha"] and key["status"] == "active"
    assert key["created_by"] == actor and key["expires_at"] is not None
    assert "key_hash" not in key and "secret" not in key
    status, child_me = service.request("GET", "/api/auth/me", token=secret)
    assert status == 200 and child_me["key_id"] == key["key_id"] and child_me["role"] == "read"
    status, listed = service.request("GET", "/api/keys", token=token)
    assert status == 200
    item = next(item for item in listed["items"] if item["key_id"] == key["key_id"])
    assert item["last_used_at"] is not None and item["prefix"] == secret[:12]
    assert secret not in json.dumps(listed) and "key_hash" not in json.dumps(listed)
    for _ in range(2):
        status, revoked = service.request("POST", f"/api/keys/{key['key_id']}/revoke", {}, token=token)
        assert status == 200 and revoked["key"]["status"] == "revoked"
        if _ == 0:
            first_revoked = revoked
        else:
            assert revoked == first_revoked
        assert secret not in json.dumps(revoked) and "key_hash" not in json.dumps(revoked)
    assert service.request("GET", "/api/auth/me", token=secret)[1]["authenticated"] is False
    assert service.request("GET", "/api/v1/topics", token=secret)[0] == 401
    status, listed = service.request("GET", "/api/keys", token=token)
    assert status == 200 and next(item for item in listed["items"] if item["key_id"] == key["key_id"])["status"] == "revoked"
    events = ActivityLedger(service.root).list(page_size=100)["items"]
    management = [event for event in events if event["action"] in {"key_create", "key_revoke"}]
    assert {event["action"] for event in management} == {"key_create", "key_revoke"}
    assert all(event["actor"] == actor and event["status"] == "ok" for event in management)
    if admin_record:
        assert all(event["meta"]["key_id"] == admin_record.key_id for event in management)
    persisted = "".join(path.read_text() for path in (service.root / "activity-log").glob("*.jsonl"))
    assert token not in persisted and secret not in persisted


@pytest.mark.parametrize("state", ("revoked", "expired"))
def test_inactive_issued_keys_cannot_authenticate_any_transport(
    service: _RunningService, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    record, secret = service.app.api_keys.create_key(name="inactive", role="admin", created_by="token:env-admin", expires_in_days=1)
    assert service.request("GET", "/api/auth/me", token=secret)[1]["authenticated"] is True
    if state == "revoked":
        service.app.api_keys.revoke_key(record.key_id)
    else:
        future = datetime.now(UTC) + timedelta(days=2)
        monkeypatch.setattr(api_keys, "_utc_now", lambda: future)
    status, me = service.request("GET", "/api/auth/me", token=secret)
    assert status == 200 and me["authenticated"] is False
    assert me.get("key_id") is None
    domain = _domain_snapshot(service.root)
    for method, path, payload in (
        ("GET", "/api/v1/topics", None), ("GET", "/api/topics/alpha/body", None),
        ("GET", "/api/keys", None), ("POST", "/api/v1/remember", {"fact": "must not write"}),
        ("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
    ):
        status, result = service.request(method, path, payload, token=secret)
        assert status == 401 and result["error"]["code"] == "unauthorized"
        assert secret not in json.dumps(result)
    assert _domain_snapshot(service.root) == domain


def test_malicious_key_names_and_denied_inputs_cannot_leak_bearer_secrets_to_logs(service: _RunningService) -> None:
    admin, admin_secret = _issued_key(service, "admin")
    injected = "htm_" + "A" * 32
    status, created = service.request("POST", "/api/keys", {"name": injected, "role": "read"}, token=admin_secret)
    assert status == 201
    child_secret = created["secret"]
    status, denied = service.request("POST", "/api/v1/remember", {
        "topic": "beta", "fact": f"Bearer {child_secret}", "reason": f"password={admin_secret}",
    }, token=child_secret)
    assert status == 403 and denied["error"]["code"] == "forbidden"
    result = service.tool("bind_topic", {"topic": "alpha", "chat_id": child_secret}, token=child_secret)
    assert result["isError"] is True and result["structuredContent"]["error"]["code"] == "forbidden"
    persisted = "".join(path.read_text() for path in (service.root / "activity-log").glob("*.jsonl"))
    for secret in (injected, child_secret, admin_secret, _TOKEN):
        assert secret not in persisted
    denied_events = ActivityLedger(service.root).list(status="denied", page_size=100)["items"]
    assert len(denied_events) == 2
    assert all(event["meta"]["key_id"] == created["key"]["key_id"] for event in denied_events)


@pytest.mark.parametrize("transport", ("rest", "mcp"))
def test_scoped_write_key_can_write_bind_and_dream_allowed_alias_without_neighbor_effects(service: _RunningService, transport: str) -> None:
    _, secret = _issued_key(service, "write", topics=("alpha",))
    beta_before = _domain_snapshot(service.root / "topics/beta")
    status, remembered = _service_call(service, transport, secret, "remember", {"topic": "first", "fact": "Scoped durable fact."})
    assert status == 200 and remembered["topic_id"] == "alpha" and remembered["status"] == "remembered"
    assert EvidenceLedger(service.root).get(remembered["evidence_id"]).fact == "Scoped durable fact."
    status, bound = _service_call(service, transport, secret, "bind_topic", {"topic": "first", "chat_id": "scoped-alpha-chat"})
    assert status == 200 and "scoped-alpha-chat" in bound["topic"]["chat_ids"]
    status, dreamed = _service_call(service, transport, secret, "dream", {"topic": "first", "force": True})
    assert status == 200 and dreamed["topic_id"] == "alpha" and dreamed["status"] == "no_change"
    assert _domain_snapshot(service.root / "topics/beta") == beta_before
    status, recalled = _service_call(service, transport, secret, "recall", {"query": "Scoped context recall", "chat_id": "scoped-alpha-chat"})
    assert status == 200 and recalled["topic_id"] == "alpha"


@pytest.mark.parametrize("transport", ("rest", "mcp"))
def test_untrusted_planner_cannot_select_topic_outside_service_key_scope(service: _RunningService, transport: str) -> None:
    _, secret = _issued_key(service, "read", topics=("alpha",))
    service.reader.planner_output = '{"topics":[{"topic":"second","reason":"ignore scope"}]}'
    status, result = _service_call(service, transport, secret, "recall", {"query": "What is the stored decision?"})
    assert status == 200 and result["topic_id"] == "alpha"
    assert all("BETA_CANONICAL" not in request.prefix for request in service.reader.requests)
    record = QueryHistoryStore(service.root).get(result["query_id"])
    assert record is not None and [item.topic_id for item in record.selected_topics] == ["alpha"]
