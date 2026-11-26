from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest

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

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.requests.append(request)
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

    def rpc(self, method: str, params: Any = None, *, request_id: Any = 1) -> tuple[int, Any]:
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        return self.request("POST", "/mcp", payload)

    def tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        status, response = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
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
        yield _RunningService(server.server_address[:2], root, reader)
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
