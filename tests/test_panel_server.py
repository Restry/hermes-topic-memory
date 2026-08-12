from __future__ import annotations

import inspect
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from hermes_topic_memory.panel_app import PanelApplication
from hermes_topic_memory.panel_server import (
    DEFAULT_BIND,
    create_panel_server,
)
from hermes_topic_memory.topic_admin import TopicAdminService

_TOKEN = "http-panel-token-DO-NOT-LOG"
_BODY = "HTTP_ADAPTER_PRIVATE_BODY"


@pytest.fixture
def running_panel(tmp_path: Path) -> Iterator[str]:
    data_root = tmp_path / "data"
    TopicAdminService(data_root).create_topic("alpha", "Alpha", (), _BODY)
    application = PanelApplication(data_root, object(), _TOKEN)
    server = create_panel_server(application, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_server_defaults_to_loopback_and_adapts_success_response(
    running_panel: str,
) -> None:
    assert DEFAULT_BIND == "127.0.0.1"
    assert inspect.signature(create_panel_server).parameters["host"].default == DEFAULT_BIND

    request = Request(
        f"{running_panel}/api/topics/alpha/body",
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    with urlopen(request, timeout=2) as response:
        payload = json.loads(response.read())
        headers = response.headers

    assert response.status == 200
    assert payload["body"] == _BODY
    assert headers.get_content_type() == "application/json"
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_server_preserves_structured_auth_and_path_errors_without_private_data(
    running_panel: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases = (
        (Request(f"{running_panel}/api/overview"), 401, "unauthorized"),
        (
            Request(
                f"{running_panel}/api/topics/%252e%252e/alpha/body",
                headers={"Authorization": f"Bearer {_TOKEN}"},
            ),
            400,
            "invalid_path",
        ),
    )
    for request, expected_status, expected_code in cases:
        with pytest.raises(HTTPError) as caught:
            urlopen(request, timeout=2)
        body = caught.value.read()
        assert caught.value.code == expected_status
        assert json.loads(body)["error"]["code"] == expected_code
        assert _BODY.encode() not in body
        assert _TOKEN.encode() not in body

    captured = capsys.readouterr()
    assert _TOKEN not in captured.out
    assert _TOKEN not in captured.err
    assert _BODY not in captured.out
    assert _BODY not in captured.err


def test_server_forwards_request_body_and_returns_malformed_json_400(
    running_panel: str,
) -> None:
    request = Request(
        f"{running_panel}/api/overview",
        data=b'{"broken":',
        headers={
            "Authorization": f"Bearer {_TOKEN}",
            "Content-Type": "application/json",
        },
        method="GET",
    )

    with pytest.raises(HTTPError) as caught:
        urlopen(request, timeout=2)

    assert caught.value.code == 400
    payload = json.loads(caught.value.read())
    assert payload["error"]["code"] == "invalid_json"
    assert _BODY not in json.dumps(payload)
    assert _TOKEN not in json.dumps(payload)
