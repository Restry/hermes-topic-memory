from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from hermes_topic_memory.core_memory import (
    CORE_DISPLAY_NAME,
    CORE_ID,
    CORE_MAX_BODY_CHARS,
    ConcurrentCoreMemoryUpdateError,
    CoreMemoryError,
    CoreMemoryService,
)
from hermes_topic_memory.panel_app import PanelApplication, PanelResponse
from hermes_topic_memory.store import FileTopicStore
from hermes_topic_memory.topic_admin import TopicAdminService

_AUTH_TOKEN = "core-panel-token-DO-NOT-LEAK"
_CORE_V1 = "# 我是谁\n\n爸爸偏好中文。\n"
_CORE_V2 = "# 我是谁\n\n爸爸偏好中文，所有摘要保持简短。\n"
_CORE_V3 = "# 我是谁\n\n最新的人工维护背景。\n"


def test_missing_core_is_supported_without_creating_topic_membership(tmp_path: Path) -> None:
    root = _topic_root(tmp_path)
    service = CoreMemoryService(root)

    assert service.get_optional() is None
    assert not (root / "core-memory/memory.md").exists()
    registry = json.loads((root / "topic-registry.json").read_text(encoding="utf-8"))
    assert [item["topic_id"] for item in registry["topics"]] == ["alpha"]


def test_initialize_replace_versions_and_deterministic_rollback(tmp_path: Path) -> None:
    root = _topic_root(tmp_path)
    service = CoreMemoryService(root)

    initialized = service.initialize(_CORE_V1)
    assert initialized.metadata.memory_id == CORE_ID == "core"
    assert initialized.metadata.display_name == CORE_DISPLAY_NAME == "Core / WhoAmI"
    assert initialized.metadata.version == 1
    assert initialized.metadata.content_fingerprint == _fingerprint(_CORE_V1)
    assert (root / "core-memory/memory.md").read_bytes() == _CORE_V1.encode("utf-8")
    assert service.list_versions() == (1,)

    replaced = service.replace(
        _CORE_V2,
        expected_version=initialized.metadata.version,
        expected_fingerprint=initialized.metadata.content_fingerprint,
    )
    assert replaced.metadata.version == 2
    assert (root / "core-memory/versions/1.md").read_bytes() == _CORE_V1.encode("utf-8")
    assert service.read_version(1) == _CORE_V1
    assert service.read_version(2) == _CORE_V2
    assert service.list_versions() == (1, 2)

    rolled_back = service.rollback(
        1,
        expected_version=replaced.metadata.version,
        expected_fingerprint=replaced.metadata.content_fingerprint,
        confirmation=CORE_DISPLAY_NAME,
    )
    assert rolled_back.metadata.version == 3
    assert rolled_back.body == _CORE_V1
    assert (root / "core-memory/versions/1.md").read_bytes() == _CORE_V1.encode("utf-8")
    assert (root / "core-memory/versions/2.md").read_bytes() == _CORE_V2.encode("utf-8")
    assert service.list_versions() == (1, 2, 3)

    audit = (root / "operation-audit/core-memory.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in audit.splitlines()]
    assert [record["action"] for record in records] == ["initialize", "replace", "rollback"]
    assert records[-1]["target_version"] == 1
    assert all(record["memory_id"] == CORE_ID for record in records)
    for private_body in (_CORE_V1, _CORE_V2):
        assert private_body not in audit


def test_validation_and_exact_optimistic_concurrency(tmp_path: Path) -> None:
    root = _topic_root(tmp_path)
    service = CoreMemoryService(root)

    for invalid in ("", " \n\t", "x" * (CORE_MAX_BODY_CHARS + 1)):
        with pytest.raises(ValueError):
            service.initialize(invalid)

    current = service.initialize(_CORE_V1)
    with pytest.raises(CoreMemoryError) as duplicate:
        service.initialize(_CORE_V2)
    assert duplicate.value.code == "core_memory_exists"

    with pytest.raises(ConcurrentCoreMemoryUpdateError):
        service.replace(
            _CORE_V2,
            expected_version=current.metadata.version + 1,
            expected_fingerprint=current.metadata.content_fingerprint,
        )
    with pytest.raises(ConcurrentCoreMemoryUpdateError):
        service.replace(
            _CORE_V2,
            expected_version=current.metadata.version,
            expected_fingerprint="0" * 64,
        )
    assert service.get().body == _CORE_V1


def test_audit_failure_rolls_back_replace_and_rollback_transactions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _topic_root(tmp_path)
    service = CoreMemoryService(root)
    first = service.initialize(_CORE_V1)
    second = service.replace(
        _CORE_V2,
        expected_version=first.metadata.version,
        expected_fingerprint=first.metadata.content_fingerprint,
    )
    before = _tree_hash(root)

    real_append_audit = service._append_audit

    def fail_audit(*args: Any, **kwargs: Any) -> None:
        real_append_audit(*args, **kwargs)
        raise OSError("PRIVATE_CORE_AUDIT_FAILURE")

    monkeypatch.setattr(service, "_append_audit", fail_audit)
    with pytest.raises(OSError, match="PRIVATE_CORE_AUDIT_FAILURE"):
        service.replace(
            _CORE_V3,
            expected_version=second.metadata.version,
            expected_fingerprint=second.metadata.content_fingerprint,
        )
    assert _tree_hash(root) == before
    assert service.get().body == _CORE_V2

    with pytest.raises(OSError, match="PRIVATE_CORE_AUDIT_FAILURE"):
        service.rollback(
            1,
            expected_version=second.metadata.version,
            expected_fingerprint=second.metadata.content_fingerprint,
            confirmation=CORE_DISPLAY_NAME,
        )
    assert _tree_hash(root) == before
    assert service.get().body == _CORE_V2


def test_panel_core_api_lifecycle_is_separate_from_topic_kpis(tmp_path: Path) -> None:
    root = _topic_root(tmp_path)
    app = PanelApplication(root, object(), _AUTH_TOKEN)
    _, before_overview = _get(app, "/api/overview")

    metadata_response, missing = _get(app, "/api/core-memory")
    assert metadata_response.status == 200
    assert missing == {
        "core_memory": {
            "id": CORE_ID,
            "display_name": CORE_DISPLAY_NAME,
            "present": False,
            "max_body_chars": CORE_MAX_BODY_CHARS,
        }
    }

    initialized_response, initialized = _post(
        app, "/api/core-memory/initialize", {"body": _CORE_V1}
    )
    assert initialized_response.status == 201
    first = initialized["core_memory"]
    assert first["present"] is True
    assert first["version"] == 1
    assert "body" not in first

    overview_response, overview = _get(app, "/api/overview")
    assert overview["core_memory"] == first
    assert overview["kpis"] == before_overview["kpis"]
    assert overview["charts"] == before_overview["charts"]
    assert [topic["topic_id"] for topic in overview["topics"]] == ["alpha"]
    assert _CORE_V1.encode("utf-8") not in overview_response.body
    assert str(root).encode("utf-8") not in overview_response.body

    body_response, body = _get(app, "/api/core-memory/body")
    assert body_response.status == 200
    assert body == {
        "id": CORE_ID,
        "display_name": CORE_DISPLAY_NAME,
        "version": 1,
        "fingerprint": _fingerprint(_CORE_V1),
        "body": _CORE_V1,
    }

    replaced_response, replaced = _post(
        app,
        "/api/core-memory/replace",
        {
            "body": _CORE_V2,
            "expected_version": body["version"],
            "expected_fingerprint": body["fingerprint"],
        },
    )
    assert replaced_response.status == 200
    assert replaced["core_memory"]["version"] == 2

    _, versions = _get(app, "/api/core-memory/versions?page=1&page_size=1")
    assert versions["total"] == 2
    assert versions["items"] == [
        {"version": 2, "current": True, "fingerprint": _fingerprint(_CORE_V2)}
    ]
    _, historical = _get(app, "/api/core-memory/versions/1/body")
    assert historical == {
        "id": CORE_ID,
        "display_name": CORE_DISPLAY_NAME,
        "version": 1,
        "fingerprint": _fingerprint(_CORE_V1),
        "current": False,
        "body": _CORE_V1,
    }

    rolled_response, rolled = _post(
        app,
        "/api/core-memory/rollback",
        {
            "target_version": 1,
            "expected_version": 2,
            "expected_fingerprint": _fingerprint(_CORE_V2),
            "confirmation": CORE_DISPLAY_NAME,
        },
    )
    assert rolled_response.status == 200
    assert rolled["core_memory"]["version"] == 3
    _, current_body = _get(app, "/api/core-memory/body")
    assert current_body["body"] == _CORE_V1


def test_panel_core_api_rejects_auth_schema_paths_versions_and_confirmation(
    tmp_path: Path,
) -> None:
    root = _topic_root(tmp_path)
    app = PanelApplication(root, object(), _AUTH_TOKEN)
    service = CoreMemoryService(root)
    current = service.initialize(_CORE_V1)

    protected = (
        ("GET", "/api/core-memory", b""),
        ("GET", "/api/core-memory/body", b""),
        ("GET", "/api/core-memory/versions", b""),
        ("GET", "/api/core-memory/versions/1/body", b""),
        ("POST", "/api/core-memory/replace", b'{}'),
        ("POST", "/api/core-memory/rollback", b'{}'),
    )
    for method, path, body in protected:
        response = app.dispatch(method, path, headers={}, body=body)
        assert response.status == 401
        assert json.loads(response.body)["error"]["code"] == "unauthorized"
        assert _CORE_V1.encode("utf-8") not in response.body

    malformed = app.dispatch(
        "POST",
        "/api/core-memory/replace",
        headers=_headers(),
        body=b'{"broken":',
    )
    assert malformed.status == 400
    assert json.loads(malformed.body)["error"]["code"] == "invalid_request"

    failures = (
        (
            "/api/core-memory/replace",
            {
                "body": _CORE_V2,
                "expected_version": 1,
                "expected_fingerprint": current.metadata.content_fingerprint,
                "unknown": "field",
            },
            400,
            "invalid_request",
        ),
        (
            "/api/core-memory/replace",
            {
                "body": _CORE_V2,
                "expected_version": 2,
                "expected_fingerprint": current.metadata.content_fingerprint,
            },
            409,
            "concurrent_update",
        ),
        (
            "/api/core-memory/rollback",
            {
                "target_version": 1,
                "expected_version": 1,
                "expected_fingerprint": current.metadata.content_fingerprint,
                "confirmation": "wrong",
            },
            409,
            "confirmation_mismatch",
        ),
    )
    for path, payload, status, code in failures:
        response, value = _post(app, path, payload)
        assert response.status == status
        assert value["error"]["code"] == code
        assert _CORE_V1.encode("utf-8") not in response.body

    for path, status, code in (
        ("/api/core-memory/versions/not-a-version/body", 400, "invalid_request"),
        ("/api/core-memory/versions/0/body", 400, "invalid_request"),
        ("/api/core-memory/versions/99/body", 404, "not_found"),
        ("/api/core-memory/versions/../body", 400, "invalid_path"),
        ("/api/core-memory/versions/%2e%2e/body", 400, "invalid_path"),
    ):
        response, value = _get(app, path)
        assert response.status == status
        assert value["error"]["code"] == code
        assert _CORE_V1.encode("utf-8") not in response.body


def _topic_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    TopicAdminService(root).create_topic("alpha", "Alpha", (), "# Alpha\n")
    return root


def _fingerprint(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        if item.name in {".core-memory.lock", ".topic-admin.lock"}:
            continue
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(item.read_bytes())
    return digest.hexdigest()


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_AUTH_TOKEN}"}


def _get(app: PanelApplication, path: str) -> tuple[PanelResponse, dict[str, Any]]:
    response = app.dispatch("GET", path, headers=_headers())
    return response, json.loads(response.body)


def _post(
    app: PanelApplication, path: str, payload: dict[str, Any]
) -> tuple[PanelResponse, dict[str, Any]]:
    response = app.dispatch(
        "POST",
        path,
        headers=_headers(),
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    return response, json.loads(response.body)
