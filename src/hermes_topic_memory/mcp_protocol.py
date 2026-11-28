from __future__ import annotations

import json
import math
from typing import Any

from .memory_service import MemoryService, MemoryServiceError, tool_definitions

DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "hermes-topic-memory"
SERVER_VERSION = "0.1.0"


class McpProtocol:
    """Stateless JSON-response mode of the MCP Streamable HTTP transport."""

    def __init__(self, service: MemoryService) -> None:
        self.service = service

    def dispatch(self, body: bytes, *, actor: str = "anonymous") -> tuple[int, dict[str, Any] | None]:
        try:
            request = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            return 400, _error(None, -32700, "Parse error")
        if not isinstance(request, dict):
            return 400, _error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        valid_id = request_id is None or (
            isinstance(request_id, (str, int, float))
            and not isinstance(request_id, bool)
            and (not isinstance(request_id, float) or math.isfinite(request_id))
        )
        if (
            request.get("jsonrpc") != "2.0"
            or not isinstance(request.get("method"), str)
            or not request["method"]
            or not valid_id
            or not set(request).issubset({"jsonrpc", "id", "method", "params"})
        ):
            return 400, _error(None, -32600, "Invalid Request")
        notification = "id" not in request
        params = request.get("params", {})
        if not isinstance(params, dict):
            return (202, None) if notification else (200, _error(request_id, -32602, "Invalid params"))
        method = request["method"]
        if method == "notifications/initialized":
            if notification:
                return 202, None
            return 200, _error(request_id, -32600, "Expected a notification")
        try:
            result = self._method(method, params, actor=actor)
        except _ProtocolError as error:
            return (202, None) if notification else (200, _error(request_id, error.code, str(error)))
        except Exception:
            return (202, None) if notification else (200, _error(request_id, -32603, "Internal error"))
        if notification:
            return 202, None
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _method(self, method: str, params: dict[str, Any], *, actor: str = "anonymous") -> dict[str, Any]:
        if method == "initialize":
            version = params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION)
            if (
                not isinstance(version, str)
                or not version.strip()
                or not isinstance(params.get("capabilities", {}), dict)
                or not isinstance(params.get("clientInfo", {}), dict)
            ):
                raise _ProtocolError(-32602, "Invalid params")
            return {
                "protocolVersion": version,
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "capabilities": {"tools": {}},
            }
        if method == "tools/list":
            if set(params) - {"_meta"}:
                raise _ProtocolError(-32602, "Invalid params")
            return {"tools": tool_definitions()}
        if method == "tools/call":
            if (
                set(params) - {"name", "arguments", "_meta"}
                or not isinstance(params.get("name"), str)
                or not isinstance(params.get("arguments", {}), dict)
            ):
                raise _ProtocolError(-32602, "Invalid params")
            try:
                value = self.service.call(params["name"], params.get("arguments", {}), source="mcp", actor=actor)
            except MemoryServiceError as error:
                if error.code in {"invalid_request", "unknown_tool"}:
                    raise _ProtocolError(-32602, "Invalid params") from None
                return _tool_result({"error": error.to_dict()}, is_error=True)
            return _tool_result(value, is_error=value.get("status") == "error")
        raise _ProtocolError(-32601, "Method not found")


class _ProtocolError(ValueError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _reject_constant(value: str) -> None:
    raise ValueError("Invalid JSON constant")


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_result(value: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, separators=(",", ":"))}],
        "structuredContent": value,
        "isError": is_error,
    }
