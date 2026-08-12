from __future__ import annotations

import json
import os
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .panel_app import PanelApplication, PanelResponse

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8080
PANEL_AUTH_TOKEN_ENV = "HERMES_TOPIC_MEMORY_PANEL_TOKEN"
_MAX_REQUEST_BODY = 64 * 1024
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; object-src 'none'"
)
_STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class TopicMemoryPanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        application: PanelApplication,
    ) -> None:
        self.application = application
        super().__init__(server_address, TopicMemoryPanelHandler)


class TopicMemoryPanelHandler(BaseHTTPRequestHandler):
    """Translate HTTP details only; PanelApplication owns all behavior."""

    server: TopicMemoryPanelServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._handle_request()

    def do_POST(self) -> None:
        self._handle_request()

    def do_PUT(self) -> None:
        self._handle_request()

    def do_DELETE(self) -> None:
        self._handle_request()

    def do_PATCH(self) -> None:
        self._handle_request()

    def do_OPTIONS(self) -> None:
        self._handle_request()

    def log_message(self, format: str, *args: object) -> None:
        # The default logger emits request targets. Keep the read boundary silent so
        # future private query parameters cannot be copied into process logs.
        return

    def _handle_request(self) -> None:
        if self.command == "GET":
            asset = _asset_response(urlsplit(self.path).path)
            if asset is not None:
                self._send_response(asset)
                return
        body_or_error = self._read_body()
        if isinstance(body_or_error, PanelResponse):
            self._send_response(body_or_error)
            return
        response = self.server.application.dispatch(
            self.command,
            self.path,
            headers=self.headers,
            body=body_or_error,
        )
        self._send_response(response)

    def _read_body(self) -> bytes | PanelResponse:
        if self.headers.get("Transfer-Encoding") is not None:
            return _transport_error(400, "invalid_request", "Unsupported request encoding.")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except ValueError:
            return _transport_error(400, "invalid_request", "Invalid request length.")
        if length < 0:
            return _transport_error(400, "invalid_request", "Invalid request length.")
        if length > _MAX_REQUEST_BODY:
            return _transport_error(413, "request_too_large", "Request body is too large.")
        return self.rfile.read(length)

    def _send_response(self, response: PanelResponse) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        self.end_headers()
        self.wfile.write(response.body)


@lru_cache(maxsize=len(_STATIC_ASSETS) + 1)
def _asset_response(path: str) -> PanelResponse | None:
    if path == "/favicon.ico":
        return PanelResponse(status=204, body=b"", content_type="image/x-icon")
    asset = _STATIC_ASSETS.get(path)
    if asset is None:
        return None
    filename, content_type = asset
    try:
        body = (
            resources.files("hermes_topic_memory")
            .joinpath("panel_assets", filename)
            .read_bytes()
        )
    except OSError:
        return _transport_error(500, "asset_unavailable", "Panel asset unavailable.")
    return PanelResponse(status=200, body=body, content_type=content_type)


def create_panel_server(
    application: PanelApplication,
    *,
    host: str = DEFAULT_BIND,
    port: int = DEFAULT_PORT,
) -> TopicMemoryPanelServer:
    return TopicMemoryPanelServer((host, port), application)


def serve_panel(
    data_root: str | Path,
    reader: object,
    *,
    auth_token: str | None = None,
    host: str = DEFAULT_BIND,
    port: int = DEFAULT_PORT,
    environ: Mapping[str, str] | None = None,
) -> None:
    environment = os.environ if environ is None else environ
    token = auth_token if auth_token is not None else environment.get(PANEL_AUTH_TOKEN_ENV)
    if not token:
        raise RuntimeError(f"{PANEL_AUTH_TOKEN_ENV} is required")
    application = PanelApplication(data_root, reader, token)
    with create_panel_server(application, host=host, port=port) as server:
        server.serve_forever()


def _transport_error(status: int, code: str, message: str) -> PanelResponse:
    body = json.dumps(
        {"error": {"code": code, "message": message}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return PanelResponse(status=status, body=body)
