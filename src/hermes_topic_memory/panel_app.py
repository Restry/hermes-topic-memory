from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

from .activity_log import ActivityAction, ActivityLedger, ActivityRecord, ActivitySource, ActivityStatus
from .api_keys import ApiKeyError, ApiKeyNotFoundError, ApiKeyStore
from .cache import QuestionCache
from .core_memory import (
    CORE_DISPLAY_NAME,
    CORE_ID,
    CORE_MAX_BODY_CHARS,
    CoreMemoryError,
    CoreMemoryNotFoundError,
    CoreMemoryService,
)
from .catalog import TopicCatalog
from .engine import RecallEngine
from .mcp_protocol import McpProtocol
from .memory_service import MemoryService, MemoryServiceError
from .multi_recall import (
    MultiRecallEngine,
    MultiRecallResult,
    ReaderCapsuleSynthesizer,
    TopicSelection,
)
from .principal import Principal, PermissionDeniedError, require_role, require_topic, restrict_registry
from .query_history import QueryHistoryStore, QueryRecord
from .router import TopicRegistry, TopicRoutingError
from .store import FileTopicStore, TopicNotFoundError
from .topic_planner import PlanningError, TopicPlan, TopicPlanner
from .topic_admin import TopicAdminError, TopicAdminService
from .wx_sso import WxGatewaySso, WxSsoError

_TOPIC_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_PAGE_SIZE = 100
_DEFAULT_PAGE_SIZE = 50
_MAX_QUERY_FIELDS = 32


@dataclass(frozen=True, slots=True)
class PanelResponse:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = ()


class _RequestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class _ResourceNotFound(LookupError):
    pass


class PanelApplication:
    """Authenticated Panel routing and serialization, independent of HTTP."""

    __slots__ = (
        "_auth_token",
        "_reader",
        "admin",
        "activities",
        "core_memory",
        "api_keys",
        "data_root",
        "history",
        "memory_service",
        "mcp",
        "store",
        "ui_mode",
        "wx_sso",
    )

    def __init__(
        self,
        data_root: str | Path,
        reader: object,
        auth_token: str,
        *,
        wx_sso: WxGatewaySso | None = None,
        ui_mode: str = "auto",
    ) -> None:
        root_input = Path(data_root).expanduser()
        if root_input.is_symlink():
            raise ValueError("data root must not be a symlink")
        self.data_root = root_input.resolve(strict=True)
        if (
            not isinstance(auth_token, str)
            or not auth_token
            or "\n" in auth_token
            or "\r" in auth_token
        ):
            raise ValueError("Panel auth token must be non-empty single-line text")
        self._reader = reader
        self._auth_token = auth_token
        self.wx_sso = wx_sso
        self.ui_mode = "studio" if (ui_mode == "studio" or (ui_mode == "auto" and wx_sso is not None)) else "classic"
        self.store = FileTopicStore(self.data_root)
        self.history = QueryHistoryStore(self.data_root)
        self.admin = TopicAdminService(self.data_root)
        self.core_memory = CoreMemoryService(self.data_root)
        self.activities = ActivityLedger(self.data_root)
        self.api_keys = ApiKeyStore(self.data_root)
        self.memory_service = MemoryService(self.data_root, reader, activity_ledger=self.activities)  # type: ignore[arg-type]
        self.mcp = McpProtocol(self.memory_service)

    def dispatch(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> PanelResponse:
        started_ns = time.perf_counter_ns()
        request_method = method.upper()
        raw_path = urlsplit(target).path
        # Public auth/SSO endpoints (no Bearer/session required).
        if raw_path == "/api/auth/me":
            if request_method != "GET":
                return _error_response(405, "method_not_allowed", "Method not allowed.")
            try:
                return self._auth_me(headers)
            except Exception:
                return _error_response(500, "internal_error", "Request could not be completed.")
        if raw_path.startswith("/api/sso/"):
            return self._sso_response(request_method, target, raw_path, headers, started_ns)

        try:
            principal = self._request_principal(headers)
        except ApiKeyError:
            return _error_response(500, "internal_error", "Request could not be completed.")
        if principal is None:
            try:
                self.activities.append(ActivityRecord(
                    action="auth_failed", source=_panel_activity_source(headers, raw_path),
                    status="denied", latency_ms=_elapsed_ms(started_ns),
                    summary="Protected request denied.",
                    meta={"method": _safe_method(request_method), "path": _safe_activity_path(raw_path)},
                ))
            except Exception:
                return _error_response(500, "internal_error", "Request could not be completed.")
            return _error_response(401, "unauthorized", "Authentication required.")

        is_mutation = _is_topic_mutation_target(method, target)
        action, topic_id = _panel_activity_action(request_method, raw_path)
        payload = None
        denied = False
        try:
            path, params = _parse_target(target)
            if path == ("mcp",):
                _reject_params(params, set())
                if request_method != "POST":
                    return _error_response(405, "method_not_allowed", "Method not allowed.")
                status, result = self.mcp.dispatch(body, principal=principal)
                return PanelResponse(status, b"") if result is None else _json_response(status, result)
            if path[:2] == ("api", "v1"):
                return self._dispatch_service(request_method, path, params, body, principal=principal)
            self._authorize_panel(request_method, path, params, principal)
            payload = _parse_json_body(body)
            if request_method == "GET":
                response = self._dispatch_get(path, params, principal=principal)
            elif request_method == "POST":
                response = self._dispatch_post(path, params, payload, principal=principal)
            else:
                response = _error_response(405, "method_not_allowed", "Method not allowed.")
        except PermissionDeniedError as error:
            denied = True
            if topic_id is None:
                candidate = path[2] if len(path) >= 3 and path[:2] in {("api", "topics"), ("api", "recycle-bin")} else params.get("topic")
                topic_id = _safe_activity_topic(candidate)
            response = _error_response(403, error.code, str(error))
        except _RequestError as error:
            code = "invalid_request" if is_mutation else error.code
            message = _MUTATION_MESSAGES[code] if is_mutation else error.message
            response = _error_response(400, code, message)
        except ApiKeyNotFoundError:
            response = _error_response(404, "not_found", "API key not found.")
        except CoreMemoryNotFoundError:
            response = _mutation_error("core_memory_not_found") if is_mutation else _error_response(404, "not_found", "Resource not found.")
        except (_ResourceNotFound, TopicNotFoundError):
            response = _mutation_error("topic_not_found") if is_mutation else _error_response(404, "not_found", "Resource not found.")
        except Exception:
            response = _mutation_error("operation_failed") if is_mutation else _error_response(500, "internal_error", "Request could not be completed.")
        if action is not None or denied:
            try:
                self._record_response(
                    action or "auth_failed", topic_id, response, actor=principal.actor,
                    principal=principal, source=_panel_activity_source(headers),
                    started_ns=started_ns, payload=payload,
                    request_meta={"method": _safe_method(request_method), "path": _safe_activity_path(raw_path)} if denied else None,
                )
            except Exception:
                return _mutation_error("operation_failed")
        return response

    def _request_principal(self, headers: Mapping[str, str] | None) -> Principal | None:
        supplied = _bearer_token(headers)
        try:
            matches_env = secrets.compare_digest(supplied or "", self._auth_token)
        except (TypeError, ValueError):
            matches_env = False
        if supplied is not None:
            if matches_env:
                return Principal(mode="env_token", actor="token:env-admin", role="admin")
            record = self.api_keys.authenticate(supplied)
            if record is not None:
                return Principal(
                    mode="api_key", actor=f"key:{record.name}({record.key_id})",
                    role=record.role, key_id=record.key_id,
                    allowed_topics=frozenset(record.allowed_topics) if record.allowed_topics else None,
                )
        if self.wx_sso is not None:
            try:
                session = self.wx_sso.read_session(headers)
            except (TypeError, ValueError):
                session = None
            if session is not None:
                nickname = _safe_actor_component(session.nickname, 64)
                identity = nickname or _safe_actor_component(session.openid, 12) or "user"
                return Principal(mode="sso", actor=f"sso:{identity}", role="admin")
        return None

    def _authorize_panel(
        self, method: str, path: tuple[str, ...], params: dict[str, str], principal: Principal
    ) -> None:
        if path[:2] in {("api", "keys"), ("api", "recycle-bin")}:
            require_role(principal, "admin")
        if method == "POST" and not (
            path in {("api", "recall", "plan"), ("api", "recall", "run")}
            or (len(path) == 4 and path[:3] == ("api", "recall", "rerun"))
        ):
            require_role(principal, "admin")
        if len(path) >= 3 and path[:2] in {("api", "topics"), ("api", "recycle-bin")}:
            require_topic(principal, _validated_topic_id(path[2]))
        if "topic" in params and path[:2] in {("api", "questions"), ("api", "activities"), ("api", "topics")}:
            require_topic(principal, _validated_topic_id(params["topic"]))

    def _create_key(self, payload: dict[str, Any] | None, principal: Principal) -> PanelResponse:
        value = _required_payload(payload, {"name", "role", "allowed_topics", "expires_in_days"})
        if not {"name", "role"}.issubset(value):
            raise _RequestError("invalid_request", "Key name and role are required.")
        topics = value.get("allowed_topics")
        if topics is not None and (
            not isinstance(topics, list) or not topics or not all(isinstance(topic, str) for topic in topics)
        ):
            raise _RequestError("invalid_request", "Topic scope must be a non-empty list of topic ids.")
        if principal.allowed_topics is not None:
            if topics is None:
                raise PermissionDeniedError("topic_forbidden")
            for topic in topics:
                require_topic(principal, topic)
        try:
            record, secret = self.api_keys.create_key(
                name=value["name"], role=value["role"], created_by=principal.actor,
                allowed_topics=topics, expires_in_days=value.get("expires_in_days"),
            )
        except ValueError:
            raise _RequestError("invalid_request", "Invalid API key fields.") from None
        return _json_response(201, {"key": record.to_dict(), "secret": secret})

    def _sso_response(
        self, method: str, target: str, raw_path: str,
        headers: Mapping[str, str] | None, started_ns: int,
    ) -> PanelResponse:
        principal = None
        error_code = None
        try:
            principal = self._request_principal(headers)
            path, params = _parse_target(target)
            response = self._dispatch_sso(method, path, params, headers)
        except WxSsoError as error:
            error_code = error.code
            response = _sso_error_response(error)
        except _RequestError as error:
            response = _error_response(400, error.code, error.message)
        except Exception:
            response = _error_response(500, "internal_error", "Request could not be completed.")
        action: ActivityAction | None = None
        if raw_path == "/api/sso/callback":
            action = "sso_login"
        elif raw_path == "/api/sso/logout":
            action = "sso_logout"
        if action is not None:
            if action == "sso_login" and response.status == 303:
                cookies = "; ".join(
                    value.split(";", 1)[0]
                    for name, value in response.headers
                    if name.casefold() == "set-cookie"
                )
                principal = self._request_principal({"Cookie": cookies})
            try:
                self._record_response(
                    action, None, response, actor=principal.actor if principal else "anonymous",
                    principal=principal, source=_panel_activity_source(headers),
                    started_ns=started_ns, error_code=error_code,
                )
            except Exception:
                return _mutation_error("operation_failed")
        return response

    def _record_response(
        self, action: ActivityAction, topic_id: str | None, response: PanelResponse, *,
        actor: str, source: ActivitySource, started_ns: int,
        payload: dict[str, Any] | None = None, error_code: str | None = None,
        principal: Principal | None = None, request_meta: dict[str, Any] | None = None,
    ) -> None:
        value: dict[str, Any] = {}
        if response.content_type.startswith("application/json"):
            value = json.loads(response.body)
        error = value.get("error", {})
        code = error_code or (error.get("code") if isinstance(error, dict) else None)
        status: ActivityStatus = "ok"
        if response.status >= 400:
            status = "denied" if response.status in {401, 403} else "error"
            if code in {"append_duplicate", "core_memory_exists", "topic_exists"}:
                status = "duplicate"
        elif value.get("status") in {"no_fact", "duplicate", "skipped", "error"}:
            status = value["status"]
        meta: dict[str, Any] = {"http_status": response.status}
        if request_meta is not None:
            meta.update(request_meta)
        if principal is not None and principal.key_id is not None:
            meta["key_id"] = principal.key_id
        if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code):
            meta["error_code"] = code
        resource = value.get("topic") or value.get("core_memory") or value.get("item") or value.get("result")
        if isinstance(resource, dict):
            candidate = resource.get("topic_id")
            if _safe_activity_topic(candidate) is not None:
                topic_id = candidate
            version = resource.get("version")
            if type(version) is int and version > 0:
                meta["version"] = version
        if payload is not None:
            if action == "topic_create" and topic_id is None:
                topic_id = _safe_activity_topic(payload.get("topic_id"))
            for field, key in (("expected_version", "previous_version"), ("target_version", "target_version")):
                number = payload.get(field)
                if type(number) is int and 0 < number <= 2**63 - 1:
                    meta[key] = number
                    if action == "topic_trash" and status == "ok" and field == "expected_version":
                        meta["version"] = number
            if status == "ok" and action != "recall":
                text = payload.get("body", payload.get("text"))
                if isinstance(text, str):
                    meta["size_chars"] = len(text)
        if action == "recall":
            results = value.get("topic_results", [])
            if isinstance(results, list):
                topic_ids = [_safe_activity_topic(item.get("topic_id")) for item in results if isinstance(item, dict)]
                if topic_ids and len(topic_ids) <= 8 and all(topic_ids):
                    meta["topic_ids"] = topic_ids
            if isinstance(results, list) and len(results) == 1 and isinstance(results[0], dict):
                topic_id = _safe_activity_topic(results[0].get("topic_id"))
                version = results[0].get("topic_version")
                if type(version) is int and version > 0:
                    meta["version"] = version
            failure = value.get("failure")
            if isinstance(failure, dict):
                code = failure.get("code")
                if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code):
                    meta["error_code"] = code
        self.activities.append(ActivityRecord(
            action=action, source=source, actor=actor, topic_id=topic_id,
            status=status, latency_ms=_elapsed_ms(started_ns),
            summary=f"{action.replace('_', ' ').capitalize()}: {status}.", meta=meta,
        ))



    def _auth_me(self, headers: Mapping[str, str] | None) -> PanelResponse:
        principal = self._request_principal(headers)
        value: dict[str, Any] = {
            "authenticated": principal is not None,
            "mode": ("token" if principal.mode == "env_token" else principal.mode) if principal else None,
            "sso_enabled": self.wx_sso is not None,
            "user": None,
        }
        if principal is not None:
            value.update(actor=principal.actor, role=principal.role, key_id=principal.key_id)
            if principal.mode == "sso" and self.wx_sso is not None:
                session = self.wx_sso.read_session(headers)
                if session is not None:
                    value["user"] = {
                        "openid": session.openid, "nickname": session.nickname, "avatar_url": session.avatar_url,
                    }
                value["central_logout_url"] = f"{self.wx_sso.config.issuer}/sso/logout"
        return _json_response(200, value)

    def _dispatch_sso(
        self,
        method: str,
        path: tuple[str, ...],
        params: dict[str, str],
        headers: Mapping[str, str] | None,
    ) -> PanelResponse:
        if self.wx_sso is None:
            return _error_response(404, "sso_disabled", "WeChat SSO is not configured.")
        if path == ("api", "sso", "start"):
            if method != "GET":
                return _error_response(405, "method_not_allowed", "Method not allowed.")
            authorize_url, tx_cookie = self.wx_sso.start(return_to=params.get("next", "/"))
            return PanelResponse(
                status=302,
                body=b"",
                content_type="text/plain; charset=utf-8",
                headers=(("Location", authorize_url), ("Set-Cookie", tx_cookie)),
            )
        if path == ("api", "sso", "callback"):
            if method != "GET":
                return _error_response(405, "method_not_allowed", "Method not allowed.")
            return_to, cookies = self.wx_sso.complete_callback(params, headers)
            resp_headers = [("Location", return_to)] + [("Set-Cookie", c) for c in cookies]
            return PanelResponse(
                status=303,
                body=b"",
                content_type="text/plain; charset=utf-8",
                headers=tuple(resp_headers),
            )
        if path == ("api", "sso", "logout"):
            if method != "POST":
                return _error_response(405, "method_not_allowed", "Method not allowed.")
            return PanelResponse(
                status=200,
                body=json.dumps(
                    {
                        "ok": True,
                        "central_logout_url": f"{self.wx_sso.config.issuer}/sso/logout",
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                headers=(("Set-Cookie", self.wx_sso.logout_cookie()),),
            )
        return _error_response(404, "not_found", "Resource not found.")

    def _dispatch_service(
        self,
        method: str,
        path: tuple[str, ...],
        params: dict[str, str],
        body: bytes,
        *,
        principal: Principal,
    ) -> PanelResponse:
        _reject_params(params, set())
        routes = {
            ("api", "v1", "whoami"): ("GET", "whoami"),
            ("api", "v1", "topics"): ("GET", "list_topics"),
            ("api", "v1", "recall"): ("POST", "recall"),
            ("api", "v1", "remember"): ("POST", "remember"),
            ("api", "v1", "topics", "bind"): ("POST", "bind_topic"),
            ("api", "v1", "dream"): ("POST", "dream"),
        }
        route = routes.get(path)
        if route is None:
            raise _ResourceNotFound
        if method != route[0]:
            return _error_response(405, "method_not_allowed", "Method not allowed.")
        payload = _parse_json_body(body)
        if method == "GET" and payload is not None:
            raise _RequestError("invalid_request", "GET requests do not accept arguments.")
        try:
            result = self.memory_service.call(route[1], payload or {}, source="rest", principal=principal)
            return _json_response(200, result)
        except MemoryServiceError as error:
            return _json_response(error.status, {"error": error.to_dict()})

    def _dispatch_get(
        self, path: tuple[str, ...], params: dict[str, str], *, principal: Principal
    ) -> PanelResponse:
        if path == ("api", "keys"):
            _reject_params(params, set())
            return _json_response(200, {"items": [record.to_dict() for record in self.api_keys.list_keys()]})
        if path == ("api", "overview"):
            _reject_params(params, set())
            return _json_response(200, self._overview(principal))
        if path == ("api", "activities"):
            return _json_response(200, self._activities(params, principal))
        if path == ("api", "core-memory"):
            _reject_params(params, set())
            return _json_response(200, {"core_memory": self._core_metadata()})
        if path == ("api", "core-memory", "body"):
            _reject_params(params, set())
            return _json_response(200, self._core_body())
        if path == ("api", "core-memory", "versions"):
            return _json_response(200, self._core_versions(params))
        if (
            len(path) == 5
            and path[:3] == ("api", "core-memory", "versions")
            and path[4] == "body"
        ):
            _reject_params(params, set())
            version = _positive_integer(path[3], "version")
            return _json_response(200, self._core_version_body(version))
        if path == ("api", "topics"):
            return _json_response(200, self._topics(params, principal))
        if path == ("api", "questions"):
            return _json_response(200, self._questions(params, principal=principal))
        if path == ("api", "dreams"):
            _reject_params(params, {"limit"})
            return _json_response(200, {"items": self._dream_reports(params, principal)})
        if path == ("api", "runtime"):
            _reject_params(params, set())
            return _json_response(200, self._runtime_info())
        if path == ("api", "recycle-bin"):
            _reject_params(params, set())
            return _json_response(200, {"items": self._recycle_items(principal)})

        if len(path) >= 3 and path[:2] == ("api", "topics"):
            topic_id = _validated_topic_id(path[2])
            if len(path) == 3:
                _reject_params(params, set())
                return _json_response(200, {"topic": self._topic_detail(topic_id, principal)})
            if len(path) == 4 and path[3] == "body":
                _reject_params(params, set())
                return _json_response(200, self._current_body(topic_id))
            if len(path) == 4 and path[3] == "questions":
                return _json_response(
                    200, self._questions(params, topic_id=topic_id, principal=principal)
                )
            if len(path) == 4 and path[3] == "versions":
                return _json_response(200, self._versions(topic_id, params))
            if (
                len(path) == 6
                and path[3] == "versions"
                and path[5] == "body"
            ):
                _reject_params(params, set())
                version = _positive_integer(path[4], "version")
                return _json_response(200, self._version_body(topic_id, version))

        raise _ResourceNotFound

    def _activities(self, params: dict[str, str], principal: Principal) -> dict[str, Any]:
        _reject_params(params, {"action", "source", "topic", "status", "q", "page", "page_size"})
        page = _positive_integer(params.get("page", "1"), "page")
        page_size = min(_positive_integer(params.get("page_size", "30"), "page_size"), _MAX_PAGE_SIZE)
        try:
            return self.activities.list(
                action=params.get("action"), source=params.get("source"),
                topic_id=params.get("topic"), status=params.get("status"), q=params.get("q"),
                page=page, page_size=page_size, allowed_topics=principal.allowed_topics,
            )
        except ValueError as error:
            raise _RequestError("invalid_request", "Invalid activity filters.") from error

    def _dispatch_post(
        self,
        path: tuple[str, ...],
        params: dict[str, str],
        payload: dict[str, Any] | None,
        *, principal: Principal,
    ) -> PanelResponse:
        _reject_params(params, set())
        if path == ("api", "keys"):
            return self._create_key(payload, principal)
        if len(path) == 4 and path[:2] == ("api", "keys") and path[3] == "revoke":
            _required_payload(payload or {}, set())
            if not re.fullmatch(r"key_[0-9a-f]{12}", path[2]):
                raise _RequestError("invalid_request", "Invalid key id.")
            return _json_response(200, {"key": self.api_keys.revoke_key(path[2]).to_dict()})
        if path == ("api", "core-memory", "initialize"):
            return self._initialize_core(payload)
        if path == ("api", "core-memory", "replace"):
            return self._replace_core(payload)
        if path == ("api", "core-memory", "rollback"):
            return self._rollback_core(payload)
        if path == ("api", "recall", "plan"):
            return self._recall_plan(payload, principal)
        if path == ("api", "recall", "run"):
            return self._recall_run(payload, principal)
        if len(path) == 4 and path[:3] == ("api", "recall", "rerun"):
            return self._recall_rerun(path[3], payload, principal)
        if path == ("api", "topics"):
            if payload is not None and isinstance(payload.get("topic_id"), str):
                require_topic(principal, _validated_topic_id(payload["topic_id"]))
            return self._create_topic(payload)
        if len(path) == 4 and path[:2] == ("api", "topics"):
            return self._mutate_topic(_validated_topic_id(path[2]), path[3], payload)
        if len(path) == 4 and path[:2] == ("api", "recycle-bin"):
            return self._mutate_recycled_topic(
                _validated_topic_id(path[2]), path[3], payload
            )
        raise _ResourceNotFound

    def _initialize_core(self, payload: dict[str, Any] | None) -> PanelResponse:
        try:
            value = _required_payload(payload, {"body"})
            initialized = self.core_memory.initialize(
                _text_field(value, "body", CORE_MAX_BODY_CHARS)
            )
            return _json_response(
                201, {"core_memory": self._verified_core(initialized)}
            )
        except Exception as error:
            return _safe_mutation_exception(error)

    def _replace_core(self, payload: dict[str, Any] | None) -> PanelResponse:
        try:
            value = _required_payload(
                payload, {"body", "expected_version", "expected_fingerprint"}
            )
            updated = self.core_memory.replace(
                _text_field(value, "body", CORE_MAX_BODY_CHARS),
                expected_version=_integer_field(value, "expected_version"),
                expected_fingerprint=_fingerprint_field(
                    value, "expected_fingerprint"
                ),
            )
            return _json_response(200, {"core_memory": self._verified_core(updated)})
        except Exception as error:
            return _safe_mutation_exception(error)

    def _rollback_core(self, payload: dict[str, Any] | None) -> PanelResponse:
        try:
            value = _required_payload(
                payload,
                {
                    "target_version",
                    "expected_version",
                    "expected_fingerprint",
                    "confirmation",
                },
            )
            updated = self.core_memory.rollback(
                _integer_field(value, "target_version"),
                expected_version=_integer_field(value, "expected_version"),
                expected_fingerprint=_fingerprint_field(
                    value, "expected_fingerprint"
                ),
                confirmation=_text_field(value, "confirmation", 200),
            )
            return _json_response(200, {"core_memory": self._verified_core(updated)})
        except Exception as error:
            return _safe_mutation_exception(error)

    def _create_topic(self, payload: dict[str, Any] | None) -> PanelResponse:
        try:
            value = _required_payload(
                payload, {"topic_id", "display_name", "aliases", "body"}
            )
            topic_id = _validated_topic_id(_text_field(value, "topic_id", 100))
            display_name = _text_field(value, "display_name", 200, strip=True)
            body = _text_field(value, "body", 2_000_000)
            aliases = _alias_fields(value)
            created = self.admin.create_topic(topic_id, display_name, aliases, body)
            return _json_response(201, {"topic": self._verified_topic(created)})
        except Exception as error:
            return _safe_mutation_exception(error)

    def _mutate_topic(
        self, topic_id: str, action: str, payload: dict[str, Any] | None
    ) -> PanelResponse:
        if action not in {"append", "replace", "rollback", "archive", "restore", "trash"}:
            raise _ResourceNotFound
        try:
            allowed = {"expected_version", "expected_fingerprint"}
            if action == "append":
                allowed.add("text")
            elif action == "replace":
                allowed.add("body")
            elif action == "rollback":
                allowed.add("target_version")
            value = _required_payload(payload, allowed)
            expected_version = _integer_field(value, "expected_version")
            expected_fingerprint = _fingerprint_field(value, "expected_fingerprint")
            if action == "append":
                updated = self.admin.append_text(
                    topic_id,
                    _text_field(value, "text", 65_536),
                    expected_version=expected_version,
                    expected_fingerprint=expected_fingerprint,
                )
            elif action == "replace":
                updated = self.admin.full_edit(
                    topic_id,
                    _text_field(value, "body", 2_000_000),
                    expected_version=expected_version,
                    expected_fingerprint=expected_fingerprint,
                )
            elif action == "rollback":
                target_version = _integer_field(value, "target_version")
                if target_version >= expected_version:
                    raise ValueError("rollback target must be historical")
                updated = self.admin.rollback_if_current(
                    topic_id,
                    target_version,
                    expected_version=expected_version,
                    expected_fingerprint=expected_fingerprint,
                )
            elif action == "archive":
                updated = self.admin.archive(
                    topic_id,
                    expected_version=expected_version,
                    expected_fingerprint=expected_fingerprint,
                )
            elif action == "restore":
                updated = self.admin.restore(
                    topic_id,
                    expected_version=expected_version,
                    expected_fingerprint=expected_fingerprint,
                )
            else:
                trashed = self.admin.trash(
                    topic_id,
                    expected_version=expected_version,
                    expected_fingerprint=expected_fingerprint,
                )
                item = self._recycle_item(topic_id)
                if item["trashed_at"] != trashed.trashed_at:
                    raise TopicAdminError("operation_failed", "trash readback mismatch")
                return _json_response(200, {"item": item})
            return _json_response(200, {"topic": self._verified_topic(updated)})
        except Exception as error:
            return _safe_mutation_exception(error)

    def _mutate_recycled_topic(
        self, topic_id: str, action: str, payload: dict[str, Any] | None
    ) -> PanelResponse:
        if action not in {"restore", "delete"}:
            raise _ResourceNotFound
        try:
            allowed = {"trashed_at"} if action == "restore" else {
                "trashed_at",
                "confirmation",
            }
            value = _required_payload(payload, allowed)
            trashed_at = _text_field(value, "trashed_at", 64)
            if action == "restore":
                restored = self.admin.restore_from_trash(
                    topic_id, expected_trashed_at=trashed_at
                )
                return _json_response(
                    200, {"topic": self._verified_topic(restored)}
                )
            result = self.admin.permanent_delete(
                topic_id,
                confirmation=_text_field(value, "confirmation", 200),
                expected_trashed_at=trashed_at,
            )
            if (self.data_root / "recycle-bin" / topic_id).exists() or (
                self.data_root / "topics" / topic_id
            ).exists():
                raise TopicAdminError("operation_failed", "delete readback mismatch")
            return _json_response(
                200,
                {
                    "result": {
                        "status": result.status,
                        "topic_id": result.topic_id,
                        "deleted_at": result.deleted_at,
                    }
                },
            )
        except Exception as error:
            return _safe_mutation_exception(error)

    def _recall_plan(self, payload: dict[str, Any] | None, principal: Principal) -> PanelResponse:
        value = _required_payload(payload, {"question"})
        question = _raw_question(value)
        registry, planner, _engine = self._recall_services(principal)
        started_ns = time.perf_counter_ns()
        try:
            plan = planner.plan(question)
        except PlanningError:
            return _error_response(
                422, "planning_failed", "Topic planning produced no safe plan."
            )
        latency_ms = _elapsed_ms(started_ns)
        return _json_response(
            200,
            {
                "question": question,
                "plan": _plan_dict(plan, registry),
                "latency_ms": latency_ms,
            },
        )

    def _recall_run(self, payload: dict[str, Any] | None, principal: Principal) -> PanelResponse:
        value = _required_payload(payload, {"question", "manual_topics"})
        question = _raw_question(value)
        full_registry = TopicRegistry.from_file(self.data_root / "topic-registry.json")
        registry, planner, engine = self._recall_services(principal, registry=full_registry)
        planning_started_ns = time.perf_counter_ns()
        if "manual_topics" in value:
            selections = _manual_selections(value["manual_topics"])
            for selection in selections:
                try:
                    topic_id = full_registry.resolve_name(selection.topic_id, context="manual recall topic")
                except TopicRoutingError:
                    raise _RequestError("invalid_recall_scope", "Manual recall scope is invalid.") from None
                require_topic(principal, topic_id)
            try:
                plan = planner.manual(question=question, selections=selections)
            except PlanningError as error:
                raise _RequestError(
                    "invalid_recall_scope", "Manual recall scope is invalid."
                ) from error
        else:
            try:
                plan = planner.plan(question)
            except PlanningError:
                return _error_response(
                    422, "planning_failed", "Topic planning produced no safe plan."
                )
        planning_ms = _elapsed_ms(planning_started_ns)
        return self._execute_recall(
            plan=plan,
            registry=registry,
            engine=engine,
            planning_ms=planning_ms,
        )

    def _recall_rerun(
        self, query_id: str, payload: dict[str, Any] | None, principal: Principal
    ) -> PanelResponse:
        _required_payload(payload or {}, set())
        try:
            original = self.history.get(query_id)
        except ValueError as error:
            raise _RequestError("invalid_request", "Invalid query id.") from error
        if original is None:
            raise _ResourceNotFound
        for result in original.selected_topics:
            require_topic(principal, result.topic_id)

        registry, planner, engine = self._recall_services(principal)
        selections = tuple(
            TopicSelection(
                result.topic_id,
                original.selection_reasons[result.topic_id],
            )
            for result in original.selected_topics
        )
        try:
            plan = planner.rerun(question=original.question, selections=selections)
        except PlanningError as error:
            raise _RequestError(
                "invalid_recall_scope", "Rerun topic scope is no longer available."
            ) from error
        return self._execute_recall(
            plan=plan,
            registry=registry,
            engine=engine,
            planning_ms=0,
        )

    def _recall_services(
        self, principal: Principal, *, registry: TopicRegistry | None = None,
    ) -> tuple[TopicRegistry, TopicPlanner, MultiRecallEngine]:
        registry = restrict_registry(
            registry if registry is not None else TopicRegistry.from_file(self.data_root / "topic-registry.json"),
            principal,
        )
        planner = TopicPlanner(registry=registry, model=self._reader)  # type: ignore[arg-type]
        engine = MultiRecallEngine(
            recall_engine=RecallEngine(
                store=self.store,
                cache=QuestionCache(self.data_root),
                reader=self._reader,  # type: ignore[arg-type]
            ),
            registry=registry,
            synthesizer=ReaderCapsuleSynthesizer(self._reader),  # type: ignore[arg-type]
            query_history=self.history,
        )
        return registry, planner, engine

    def _execute_recall(
        self,
        *,
        plan: TopicPlan,
        registry: TopicRegistry,
        engine: MultiRecallEngine,
        planning_ms: int,
    ) -> PanelResponse:
        recall_started_ns = time.perf_counter_ns()
        result = engine.recall_multi(
            question=plan.question,
            selections=plan.selections,
            source="panel",
        )
        recall_ms = _elapsed_ms(recall_started_ns)
        return _json_response(
            200,
            _recall_dict(
                result=result,
                plan=plan,
                registry=registry,
                planning_ms=planning_ms,
                recall_ms=recall_ms,
            ),
        )

    def _runtime_info(self) -> dict[str, Any]:
        raw_endpoint = getattr(self._reader, "endpoint", None)
        safe_endpoint: str | None = None
        if isinstance(raw_endpoint, str) and raw_endpoint.strip():
            parts = urlsplit(raw_endpoint.strip())
            if parts.scheme and parts.netloc:
                safe_endpoint = f"{parts.scheme}://{parts.netloc}{parts.path}"
            else:
                safe_endpoint = raw_endpoint.strip()
        model = getattr(self._reader, "model", None)
        return {
            "reader_model": str(model) if isinstance(model, str) and model else "unknown",
            "reader_endpoint": safe_endpoint or "in-process",
            "ui_mode": self.ui_mode,
            "sso_enabled": self.wx_sso is not None,
            "sso_gateway": self.wx_sso.config.issuer if self.wx_sso is not None else None,
            "allowed_openids_count": len(self.wx_sso.config.allowed_openids) if self.wx_sso is not None else 0,
        }

    def _dream_reports(self, params: dict[str, str], principal: Principal) -> list[dict[str, Any]]:
        limit = _positive_integer(params["limit"], "limit") if "limit" in params else 30
        limit = min(limit, 100)
        reports_dir = self.data_root / "dream-reports"
        if not reports_dir.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(reports_dir.glob("*.jsonl")):
            if path.name.startswith(".") or (
                principal.allowed_topics is not None and path.stem not in principal.allowed_topics
            ):
                continue
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and (
                    principal.allowed_topics is None or entry.get("topic_id") in principal.allowed_topics
                ):
                    items.append(entry)
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return items[:limit]

    def _overview(self, principal: Principal) -> dict[str, Any]:
        snapshot = TopicCatalog(self.data_root).snapshot(allowed_topics=principal.allowed_topics)
        recycled = self._recycle_items(principal)
        topics = [topic.to_public_dict() for topic in snapshot.topics]
        distribution = sorted(
            (
                {
                    "topic_id": topic.topic_id,
                    "display_name": topic.display_name,
                    "question_count": topic.question_count,
                }
                for topic in snapshot.topics
            ),
            key=lambda item: (
                -item["question_count"],
                item["display_name"].casefold(),
                item["topic_id"],
            ),
        )
        return {
            "core_memory": self._core_metadata(),
            "kpis": {
                "active_topic_count": snapshot.active_topic_count,
                "total_question_count": snapshot.total_question_count,
                "active_cache_count": snapshot.active_cache_count,
                "questions_last_30_days": snapshot.questions_last_30_days,
                "total_chars": snapshot.total_chars,
                "archived_topic_count": sum(
                    topic.status == "archived" for topic in snapshot.topics
                ),
                "trashed_topic_count": len(recycled),
            },
            "charts": {
                "daily_questions": [
                    item.to_public_dict() for item in snapshot.daily_questions
                ],
                "topic_distribution": distribution,
            },
            "topics": topics,
            "recent_questions": [
                _query_summary(record) for record in self._visible_history(principal)[:10]
            ],
        }

    def _topics(self, params: dict[str, str], principal: Principal) -> dict[str, Any]:
        _reject_params(
            params, {"q", "status", "sort", "order", "page", "page_size"}
        )
        query = params.get("q", "").strip().casefold()
        if len(query) > 200:
            raise _RequestError("invalid_request", "Search text is too long.")
        status = params.get("status")
        if status is not None and status not in {"active", "archived"}:
            raise _RequestError("invalid_request", "Invalid topic status.")
        sort_name = params.get("sort", "title")
        if sort_name not in {
            "title",
            "question_count",
            "updated_at",
            "version",
        }:
            raise _RequestError("invalid_request", "Invalid topic sort.")
        order = params.get("order", "asc")
        if order not in {"asc", "desc"}:
            raise _RequestError("invalid_request", "Invalid sort order.")
        page, page_size = _pagination(params)

        topics = list(TopicCatalog(self.data_root).snapshot(allowed_topics=principal.allowed_topics).topics)
        if query:
            topics = [
                topic
                for topic in topics
                if query
                in "\n".join((topic.topic_id, topic.display_name, *topic.aliases)).casefold()
            ]
        if status is not None:
            topics = [topic for topic in topics if topic.status == status]
        topics.sort(key=_topic_sort_key(sort_name), reverse=order == "desc")
        return _page([topic.to_public_dict() for topic in topics], page, page_size)

    def _core_metadata(self) -> dict[str, Any]:
        memory = self.core_memory.get_optional()
        value: dict[str, Any] = {
            "id": CORE_ID,
            "display_name": CORE_DISPLAY_NAME,
            "present": memory is not None,
            "max_body_chars": CORE_MAX_BODY_CHARS,
        }
        if memory is not None:
            metadata = memory.metadata
            value.update(
                {
                    "version": metadata.version,
                    "content_fingerprint": metadata.content_fingerprint,
                    "created_at": metadata.created_at,
                    "updated_at": metadata.updated_at,
                }
            )
        return value

    def _verified_core(self, expected: Any) -> dict[str, Any]:
        readback = self.core_memory.get()
        if (
            readback.metadata.version != expected.metadata.version
            or readback.metadata.content_fingerprint
            != expected.metadata.content_fingerprint
        ):
            raise CoreMemoryError("operation_failed", "Core readback mismatch")
        return self._core_metadata()

    def _core_body(self) -> dict[str, Any]:
        memory = self.core_memory.get()
        return {
            "id": CORE_ID,
            "display_name": CORE_DISPLAY_NAME,
            "version": memory.metadata.version,
            "fingerprint": memory.metadata.content_fingerprint,
            "body": memory.body,
        }

    def _core_versions(self, params: dict[str, str]) -> dict[str, Any]:
        _reject_params(params, {"page", "page_size"})
        page, page_size = _pagination(params)
        current, version_bodies = self.core_memory.version_snapshot()
        items = [
            {
                "version": version,
                "current": version == current.metadata.version,
                "fingerprint": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            }
            for version, body in reversed(version_bodies)
        ]
        return _page(items, page, page_size)

    def _core_version_body(self, version: int) -> dict[str, Any]:
        current, body = self.core_memory.read_version_snapshot(version)
        return {
            "id": CORE_ID,
            "display_name": CORE_DISPLAY_NAME,
            "version": version,
            "fingerprint": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "current": version == current.metadata.version,
            "body": body,
        }

    def _verified_topic(self, expected: Any) -> dict[str, Any]:
        metadata = expected.metadata
        readback = self.store.get_topic(metadata.topic_id)
        current = readback.metadata
        if (
            current.version != metadata.version
            or current.content_fingerprint != metadata.content_fingerprint
            or current.state != metadata.state
        ):
            raise TopicAdminError("operation_failed", "topic readback mismatch")
        return {
            "topic_id": current.topic_id,
            "display_name": current.display_name,
            "aliases": list(current.aliases),
            "version": current.version,
            "content_fingerprint": current.content_fingerprint,
            "created_at": current.created_at,
            "updated_at": current.updated_at,
            "source_policy": current.source_policy,
            "state": current.state,
        }

    def _topic_detail(self, topic_id: str, principal: Principal) -> dict[str, Any]:
        memory = self.store.get_topic(topic_id)
        try:
            summary = TopicCatalog(self.data_root).snapshot(allowed_topics=principal.allowed_topics).topic(topic_id)
        except KeyError as error:
            raise _ResourceNotFound from error
        metadata = memory.metadata
        source_relative_path = _safe_relative_path(metadata.source_relative_path)
        return {
            "topic_id": metadata.topic_id,
            "display_name": metadata.display_name,
            "aliases": list(metadata.aliases),
            "version": metadata.version,
            "content_fingerprint": metadata.content_fingerprint,
            "created_at": metadata.created_at,
            "updated_at": metadata.updated_at,
            "source_policy": metadata.source_policy,
            "state": metadata.state,
            "source_relative_path": source_relative_path,
            "source_sha256": metadata.source_sha256,
            "source_bytes": metadata.source_bytes,
            "source_chars": metadata.source_chars,
            "source_lines": metadata.source_lines,
            "imported_at": metadata.imported_at,
            "question_count": summary.question_count,
            "active_cache_count": summary.active_cache_count,
            "version_count": summary.version_count,
        }

    def _current_body(self, topic_id: str) -> dict[str, Any]:
        memory = self.store.get_topic(topic_id)
        return {
            "topic_id": topic_id,
            "version": memory.metadata.version,
            "body": memory.body,
            "fingerprint": memory.metadata.content_fingerprint,
        }

    def _visible_history(self, principal: Principal) -> list[QueryRecord]:
        records = list(self.history.list())
        if principal.allowed_topics is None:
            return records
        # Mixed-topic questions and synthesized answers cannot be partially exposed.
        return [
            record for record in records if record.selected_topics
            and all(result.topic_id in principal.allowed_topics for result in record.selected_topics)
        ]

    def _questions(
        self,
        params: dict[str, str],
        *,
        topic_id: str | None = None,
        principal: Principal,
    ) -> dict[str, Any]:
        _reject_params(
            params,
            {
                "topic",
                "status",
                "source",
                "date_from",
                "date_to",
                "scope",
                "cache",
                "page",
                "page_size",
            },
        )
        requested_topic = params.get("topic")
        if topic_id is not None:
            self.store.get_topic(topic_id)
            if requested_topic is not None and requested_topic != topic_id:
                raise _RequestError("invalid_request", "Conflicting topic filters.")
            requested_topic = topic_id
        if requested_topic is not None:
            requested_topic = _validated_topic_id(requested_topic)

        status = params.get("status")
        if status is not None and status not in {"answered", "no_fact", "error"}:
            raise _RequestError("invalid_request", "Invalid question status.")
        source = params.get("source")
        if source is not None and source not in {"hermes", "panel", "cli", "mcp", "rest"}:
            raise _RequestError("invalid_request", "Invalid question source.")
        scope = params.get("scope")
        if scope is not None and scope not in {"single", "multi"}:
            raise _RequestError("invalid_request", "Invalid question scope.")
        cache = params.get("cache")
        if cache is not None and cache not in {"hit", "miss"}:
            raise _RequestError("invalid_request", "Invalid cache filter.")
        first_date = _optional_date(params.get("date_from"), "date_from")
        last_date = _optional_date(params.get("date_to"), "date_to")
        if (
            first_date is not None
            and last_date is not None
            and first_date > last_date
        ):
            raise _RequestError("invalid_request", "Invalid date range.")
        page, page_size = _pagination(params)

        records = self._visible_history(principal)
        filtered: list[QueryRecord] = []
        for record in records:
            relevant_results = tuple(
                result
                for result in record.selected_topics
                if requested_topic is None or result.topic_id == requested_topic
            )
            if requested_topic is not None and not relevant_results:
                continue
            if status is not None and not any(
                result.status == status for result in relevant_results
            ):
                continue
            if source is not None and record.source != source:
                continue
            if scope == "single" and len(record.selected_topics) != 1:
                continue
            if scope == "multi" and len(record.selected_topics) <= 1:
                continue
            if cache == "hit" and not any(
                result.source == "cache" for result in relevant_results
            ):
                continue
            if cache == "miss" and any(
                result.source == "cache" for result in relevant_results
            ):
                continue
            record_date = _record_datetime(record).date()
            if first_date is not None and record_date < first_date:
                continue
            if last_date is not None and record_date > last_date:
                continue
            filtered.append(record)
        return _page([_query_dict(record) for record in filtered], page, page_size)

    def _versions(self, topic_id: str, params: dict[str, str]) -> dict[str, Any]:
        _reject_params(params, {"page", "page_size"})
        current = self.store.get_topic(topic_id)
        versions = sorted(self.store.list_versions(topic_id), reverse=True)
        page, page_size = _pagination(params)
        start = (page - 1) * page_size
        items = []
        for version in versions[start : start + page_size]:
            body = self.store.read_version(topic_id, version)
            items.append(
                {
                    "version": version,
                    "current": version == current.metadata.version,
                    "fingerprint": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                }
            )
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": len(versions),
        }

    def _version_body(self, topic_id: str, version: int) -> dict[str, Any]:
        current = self.store.get_topic(topic_id)
        try:
            body = self.store.read_version(topic_id, version)
        except TopicNotFoundError as error:
            raise _ResourceNotFound from error
        return {
            "topic_id": topic_id,
            "version": version,
            "body": body,
            "fingerprint": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "current": version == current.metadata.version,
        }

    def _recycle_items(self, principal: Principal) -> list[dict[str, Any]]:
        recycle_root = self.data_root / "recycle-bin"
        if not recycle_root.exists():
            return []
        if recycle_root.is_symlink() or not recycle_root.is_dir():
            raise RuntimeError("invalid recycle bin")
        items = [
            self._recycle_item(entry.name)
            for entry in recycle_root.iterdir()
            if not entry.name.startswith(".")
            and (principal.allowed_topics is None or entry.name in principal.allowed_topics)
        ]
        items.sort(
            key=lambda item: (item["trashed_at"], item["topic_id"]), reverse=True
        )
        return items

    def _recycle_item(self, topic_id: str) -> dict[str, Any]:
        _validated_topic_id(topic_id)
        entry = self.data_root / "recycle-bin" / topic_id
        if entry.is_symlink() or not entry.is_dir():
            raise TopicAdminError("topic_not_found", "recycled topic does not exist")
        tombstone_path = entry / "tombstone.json"
        if tombstone_path.is_symlink() or not tombstone_path.is_file():
            raise TopicAdminError("operation_failed", "invalid recycle tombstone")
        try:
            value = json.loads(tombstone_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TopicAdminError(
                "operation_failed", "invalid recycle tombstone"
            ) from error
        if not isinstance(value, dict) or value.get("topic_id") != topic_id:
            raise TopicAdminError("operation_failed", "invalid recycle tombstone")
        display_name = value.get("display_name")
        prior_state = value.get("prior_state")
        created_at = value.get("created_at")
        trashed_at = value.get("trashed_at")
        if (
            not isinstance(display_name, str)
            or not display_name
            or prior_state not in {"active", "archived"}
            or not isinstance(created_at, str)
            or not isinstance(trashed_at, str)
        ):
            raise TopicAdminError("operation_failed", "invalid recycle tombstone")
        return {
            "topic_id": topic_id,
            "display_name": display_name,
            "prior_state": prior_state,
            "created_at": created_at,
            "trashed_at": trashed_at,
        }


def _sso_error_response(error: WxSsoError) -> PanelResponse:
    html_error = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>登录失败</title><style>body{font-family:-apple-system,sans-serif;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;"
        "margin:0;background:#f8fafc;color:#0f172a}.card{background:#fff;border:1px solid #e2e8f0;"
        "border-radius:12px;padding:28px;max-width:420px;box-shadow:0 4px 16px rgba(15,23,42,.06)}"
        "a{display:inline-block;margin-top:16px;margin-right:12px;padding:8px 16px;border-radius:8px;"
        "text-decoration:none;font-weight:600}.primary{background:#2563eb;color:#fff}"
        ".secondary{border:1px solid #cbd5e1;color:#334155}</style></head><body><div class='card'>"
        f"<h3>微信登录未完成</h3><p>{error}</p>"
        "<a class='primary' href='/api/sso/start'>重新扫码登录</a>"
        "<a class='secondary' href='/?token_login=1'>使用访问密钥</a>"
        "</div></body></html>"
    ).encode("utf-8")
    return PanelResponse(status=error.status, body=html_error, content_type="text/html; charset=utf-8")


def _bearer_token(headers: Mapping[str, str] | None) -> str | None:
    if headers is not None:
        for name, value in headers.items():
            if str(name).casefold() == "authorization":
                authorization = str(value)
                return authorization[7:] if authorization.startswith("Bearer ") else None
    return None


def _safe_actor_component(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    bounded = value[:maximum]
    if any(not (char.isalnum() or char in " ._-") for char in bounded):
        return ""
    return bounded.strip()


def _safe_activity_topic(value: Any) -> str | None:
    if isinstance(value, str) and len(value) <= 100 and _TOPIC_ID.fullmatch(value):
        return value
    return None


def _panel_activity_source(headers: Mapping[str, str] | None, path: str = "") -> ActivitySource:
    if path == "/mcp":
        return "mcp"
    if path.startswith("/api/v1/"):
        return "rest"
    if headers is not None:
        for name, value in headers.items():
            if str(name).casefold() == "x-topic-memory-source" and value == "studio":
                return "studio"
    return "panel"


def _safe_method(method: str) -> str:
    return method if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "OTHER"


def _safe_activity_path(raw_path: str) -> str:
    # Store only known route templates, never attacker-controlled path/query text.
    path = tuple(unquote(raw_path).strip("/").split("/"))
    if path == ("api", "keys"):
        return "/api/keys"
    if len(path) == 4 and path[:2] == ("api", "keys") and path[3] == "revoke":
        return "/api/keys/:key_id/revoke"
    if path == ("mcp",):
        return "/mcp"
    if len(path) == 3 and path[:2] == ("api", "v1") and path[2] in {"whoami", "topics", "recall", "remember", "dream"}:
        return "/" + "/".join(path)
    if path == ("api", "v1", "topics", "bind"):
        return "/api/v1/topics/bind"
    if len(path) == 2 and path[0] == "api" and path[1] in {"overview", "topics", "questions", "activities", "dreams", "runtime", "core-memory", "recycle-bin"}:
        return "/" + "/".join(path)
    if len(path) == 3 and path[:2] == ("api", "core-memory") and path[2] in {"body", "versions", "initialize", "replace", "rollback"}:
        return "/" + "/".join(path)
    if len(path) == 5 and path[:3] == ("api", "core-memory", "versions") and path[4] == "body":
        return "/api/core-memory/versions/:version/body"
    if len(path) == 3 and path[:2] == ("api", "topics"):
        return "/api/topics/:topic_id"
    if len(path) == 4 and path[:2] == ("api", "topics") and path[3] in {"body", "questions", "versions", "append", "replace", "rollback", "archive", "restore", "trash"}:
        return "/api/topics/:topic_id/" + path[3]
    if len(path) == 6 and path[:2] == ("api", "topics") and path[3] == "versions" and path[5] == "body":
        return "/api/topics/:topic_id/versions/:version/body"
    if len(path) == 4 and path[:2] == ("api", "recycle-bin") and path[3] in {"restore", "delete"}:
        return "/api/recycle-bin/:topic_id/" + path[3]
    if len(path) == 3 and path[:2] == ("api", "recall") and path[2] in {"run", "plan"}:
        return "/" + "/".join(path)
    if len(path) == 4 and path[:3] == ("api", "recall", "rerun"):
        return "/api/recall/rerun/:query_id"
    return "/unknown"


def _panel_activity_action(method: str, raw_path: str) -> tuple[ActivityAction | None, str | None]:
    if method != "POST":
        return None, None
    path = tuple(unquote(raw_path).strip("/").split("/"))
    if path == ("api", "keys"):
        return "key_create", None
    if len(path) == 4 and path[:2] == ("api", "keys") and path[3] == "revoke":
        return "key_revoke", None
    if path == ("api", "topics"):
        return "topic_create", None
    if len(path) == 3 and path[:2] == ("api", "core-memory"):
        actions: dict[str, ActivityAction] = {"initialize": "core_init", "replace": "core_edit", "rollback": "core_rollback"}
        return actions.get(path[2]), None
    if path == ("api", "recall", "run") or (len(path) == 4 and path[:3] == ("api", "recall", "rerun")):
        return "recall", None
    if len(path) == 4 and path[:2] == ("api", "topics"):
        actions = {"append": "topic_append", "replace": "topic_edit", "rollback": "topic_rollback", "archive": "topic_archive", "restore": "topic_restore", "trash": "topic_trash"}
        return actions.get(path[3]), _safe_activity_topic(path[2])
    if len(path) == 4 and path[:2] == ("api", "recycle-bin"):
        actions = {"restore": "topic_restore", "delete": "topic_delete"}
        return actions.get(path[3]), _safe_activity_topic(path[2])
    return None, None


_MUTATION_MESSAGES = {
    "core_memory_exists": "Core / WhoAmI 已初始化。请刷新后使用替换操作。",
    "core_memory_not_found": "Core / WhoAmI 尚未初始化。请先初始化。",
    "invalid_request": "请求字段无效。请按当前表单修正后重试。",
    "topic_exists": "主题 ID 已存在。请更换主题 ID 后重试。",
    "topic_not_found": "主题不存在或已变化。请刷新目录后重试。",
    "alias_conflict": "显示名称或别名与现有主题冲突。请更换后重试。",
    "concurrent_update": "主题已变化。请重新读取当前版本后再提交。",
    "append_duplicate": "这段追加内容已经存在。请检查正文后再提交。",
    "invalid_state": "当前主题状态不允许此操作。请刷新后选择可用操作。",
    "confirmation_mismatch": "确认名称不匹配。请输入回收站中显示的完整名称。",
    "operation_failed": "操作未完成且未确认写入。请重新读取当前状态后重试。",
}


def _safe_mutation_exception(error: Exception) -> PanelResponse:
    if isinstance(error, CoreMemoryError):
        code = error.code if error.code in _MUTATION_MESSAGES else "operation_failed"
        return _mutation_error(code)
    if isinstance(error, TopicAdminError):
        code = error.code if error.code in _MUTATION_MESSAGES else "operation_failed"
        return _mutation_error(code)
    if isinstance(error, TopicNotFoundError):
        return _mutation_error("topic_not_found")
    if isinstance(error, (TypeError, ValueError)):
        return _mutation_error("invalid_request")
    return _mutation_error("operation_failed")


def _mutation_error(code: str) -> PanelResponse:
    if code == "invalid_request":
        status = 400
    elif code in {"topic_not_found", "core_memory_not_found"}:
        status = 404
    elif code == "operation_failed":
        status = 500
    else:
        status = 409
    return _error_response(status, code, _MUTATION_MESSAGES[code])


def _is_topic_mutation_target(method: str, target: str) -> bool:
    if not isinstance(method, str) or method.upper() != "POST" or not isinstance(target, str):
        return False
    path = urlsplit(target).path
    return (
        path == "/api/topics"
        or path.startswith("/api/topics/")
        or path.startswith("/api/recycle-bin/")
        or path.startswith("/api/core-memory/")
    )


def _text_field(
    payload: Mapping[str, Any], name: str, maximum: int, *, strip: bool = False
) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ValueError(f"invalid {name}")
    if strip:
        value = value.strip()
        if not value:
            raise ValueError(f"invalid {name}")
    return value


def _integer_field(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"invalid {name}")
    return value


def _fingerprint_field(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"invalid {name}")
    return value


def _alias_fields(payload: Mapping[str, Any]) -> tuple[str, ...]:
    value = payload.get("aliases")
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("invalid aliases")
    aliases: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError("invalid aliases")
        alias = raw.strip()
        normalized = alias.casefold()
        if not alias or len(alias) > 200 or normalized in seen:
            raise ValueError("invalid aliases")
        seen.add(normalized)
        aliases.append(alias)
    return tuple(aliases)


def _required_payload(
    payload: dict[str, Any] | None, allowed: set[str]
) -> dict[str, Any]:
    if payload is None:
        raise _RequestError("invalid_request", "JSON body is required.")
    if not set(payload).issubset(allowed):
        raise _RequestError("invalid_request", "Unknown JSON field.")
    if "question" in allowed and "question" not in payload:
        raise _RequestError("invalid_request", "Question is required.")
    return payload


def _raw_question(payload: Mapping[str, Any]) -> str:
    question = payload.get("question")
    if (
        not isinstance(question, str)
        or not question.strip()
        or len(question) > 4_096
    ):
        raise _RequestError(
            "invalid_request", "Question must contain 1 to 4096 characters."
        )
    return question


def _manual_selections(value: Any) -> tuple[TopicSelection, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise _RequestError(
            "invalid_recall_scope", "Manual recall scope is invalid."
        )
    selections: list[TopicSelection] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"topic", "reason"}:
            raise _RequestError(
                "invalid_recall_scope", "Manual recall scope is invalid."
            )
        try:
            selections.append(TopicSelection(item["topic"], item["reason"]))
        except (TypeError, ValueError) as error:
            raise _RequestError(
                "invalid_recall_scope", "Manual recall scope is invalid."
            ) from error
    return tuple(selections)


def _plan_dict(plan: TopicPlan, registry: TopicRegistry) -> dict[str, Any]:
    display_names = {
        topic.topic_id: topic.display_name for topic in registry.topics
    }
    return {
        "mode": plan.mode,
        "topics": [
            {
                "topic": selection.topic_id,
                "topic_id": selection.topic_id,
                "display_name": display_names[selection.topic_id],
                "reason": selection.reason,
            }
            for selection in plan.selections
        ],
    }


def _recall_dict(
    *,
    result: MultiRecallResult,
    plan: TopicPlan,
    registry: TopicRegistry,
    planning_ms: int,
    recall_ms: int,
) -> dict[str, Any]:
    reasons = {
        selection.topic_id: selection.reason for selection in plan.selections
    }
    topic_results: list[dict[str, Any]] = []
    topic_latencies: dict[str, int] = {}
    for item in result.topic_results:
        topic_latencies[item.topic_id] = item.latency_ms
        topic_value: dict[str, Any] = {
            "topic_id": item.topic_id,
            "topic_version": item.topic_version,
            "status": item.status,
            "answer": item.answer,
            "source": item.source,
            "cache_status": "hit" if item.source == "cache" else "miss",
            "match_kind": item.match_kind,
            "latency_ms": item.latency_ms,
            "reason": reasons[item.topic_id],
        }
        if item.failure is not None:
            topic_value["failure"] = _failure_dict(item.failure)
        topic_results.append(topic_value)

    topics_ms = sum(topic_latencies.values())
    status = _recall_status(result)
    response: dict[str, Any] = {
        "status": status,
        "question": result.question,
        "query_id": result.query_id,
        "plan": _plan_dict(plan, registry),
        "topic_results": topic_results,
        "final_answer": result.final_answer,
        "final": {
            "answer": result.final_answer,
            "source": result.synthesis_source,
        },
        "synthesis_source": result.synthesis_source,
        "scope_fingerprint": result.scope_fingerprint,
        "latency_ms": {
            "planning": planning_ms,
            "recall": recall_ms,
            "topics": topic_latencies,
            "synthesis": max(0, recall_ms - topics_ms),
            "total": planning_ms + recall_ms,
        },
    }
    if result.failure is not None and status == "error":
        response["failure"] = _failure_dict(result.failure)
    return response


def _recall_status(result: MultiRecallResult) -> str:
    if result.failure is None and result.final_answer is not None:
        return "answered"
    if result.failure is not None and result.failure.code != "no_answered_topics":
        return "error"
    if result.topic_results and all(
        item.status == "no_fact" for item in result.topic_results
    ):
        return "no_fact"
    return "error"


def _failure_dict(failure: Any) -> dict[str, Any]:
    return {
        "code": failure.code,
        "message": failure.message,
        "attempts": failure.attempts,
    }


def _elapsed_ms(started_ns: int) -> int:
    return (time.perf_counter_ns() - started_ns) // 1_000_000


def _parse_target(target: str) -> tuple[tuple[str, ...], dict[str, str]]:
    if not isinstance(target, str):
        raise _RequestError("invalid_path", "Invalid request path.")
    parsed = urlsplit(target)
    raw_path = parsed.path
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not raw_path.startswith("/")
        or "%" in raw_path
        or "\\" in raw_path
        or "\x00" in raw_path
        or "//" in raw_path
        or any(character.isspace() for character in raw_path)
    ):
        raise _RequestError("invalid_path", "Invalid request path.")
    segments = tuple(raw_path.strip("/").split("/")) if raw_path != "/" else ()
    if any(segment in {"", ".", ".."} for segment in segments):
        raise _RequestError("invalid_path", "Invalid request path.")
    try:
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=_MAX_QUERY_FIELDS,
        )
    except ValueError as error:
        raise _RequestError("invalid_request", "Invalid query string.") from error
    params: dict[str, str] = {}
    for name, value in pairs:
        if name in params:
            raise _RequestError("invalid_request", "Duplicate query parameter.")
        params[name] = value
    return segments, params


def _parse_json_body(body: bytes) -> dict[str, Any] | None:
    if not isinstance(body, bytes):
        raise _RequestError("invalid_json", "Malformed JSON body.")
    if not body:
        return None
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _RequestError("invalid_json", "Malformed JSON body.") from error
    if not isinstance(value, dict):
        raise _RequestError("invalid_json", "JSON body must be an object.")
    return value


def _validated_topic_id(topic_id: str) -> str:
    if not _TOPIC_ID.fullmatch(topic_id):
        raise _RequestError("invalid_request", "Invalid topic id.")
    return topic_id


def _positive_integer(value: str, label: str) -> int:
    if not value.isascii() or not value.isdigit():
        raise _RequestError("invalid_request", f"Invalid {label}.")
    try:
        number = int(value)
    except ValueError as error:
        raise _RequestError("invalid_request", f"Invalid {label}.") from error
    if number < 1:
        raise _RequestError("invalid_request", f"Invalid {label}.")
    return number


def _pagination(params: Mapping[str, str]) -> tuple[int, int]:
    page = _positive_integer(params.get("page", "1"), "page")
    page_size = _positive_integer(
        params.get("page_size", str(_DEFAULT_PAGE_SIZE)), "page size"
    )
    if page_size > _MAX_PAGE_SIZE:
        raise _RequestError("invalid_request", "Page size exceeds 100.")
    return page, page_size


def _page(
    items: list[dict[str, Any]], page: int, page_size: int
) -> dict[str, Any]:
    start = (page - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total": len(items),
    }


def _reject_params(params: Mapping[str, str], allowed: set[str]) -> None:
    if not set(params).issubset(allowed):
        raise _RequestError("invalid_request", "Unknown query parameter.")


def _topic_sort_key(name: str):
    if name == "title":
        return lambda topic: (topic.display_name.casefold(), topic.topic_id)
    if name == "question_count":
        return lambda topic: (
            topic.question_count,
            topic.display_name.casefold(),
            topic.topic_id,
        )
    if name == "updated_at":
        return lambda topic: (
            topic.updated_at,
            topic.display_name.casefold(),
            topic.topic_id,
        )
    return lambda topic: (topic.version, topic.display_name.casefold(), topic.topic_id)


def _safe_relative_path(value: str | None) -> str | None:
    if value is None:
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def _optional_date(value: str | None, label: str) -> date | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise _RequestError("invalid_request", f"Invalid {label}.") from error
    if parsed.isoformat() != value:
        raise _RequestError("invalid_request", f"Invalid {label}.")
    return parsed


def _record_datetime(record: QueryRecord) -> datetime:
    suffix = "+00:00" if record.created_at.endswith("Z") else ""
    parsed = datetime.fromisoformat(record.created_at.removesuffix("Z") + suffix)
    return parsed.astimezone(UTC)


def _query_summary(record: QueryRecord) -> dict[str, Any]:
    return {
        "query_id": record.query_id,
        "question": record.question,
        "created_at": record.created_at,
        "topic_ids": [result.topic_id for result in record.selected_topics],
        "scope": "single" if len(record.selected_topics) == 1 else "multi",
        "source": record.source,
    }


def _query_dict(record: QueryRecord) -> dict[str, Any]:
    return {
        "query_id": record.query_id,
        "question": record.question,
        "created_at": record.created_at,
        "selected_topics": [
            result.to_dict() for result in record.selected_topics
        ],
        "final_answer": record.final_answer,
        "selection_reasons": dict(record.selection_reasons),
        "source": record.source,
        "total_latency_ms": record.total_latency_ms,
        "scope": "single" if len(record.selected_topics) == 1 else "multi",
    }


def _json_response(status: int, value: Mapping[str, Any]) -> PanelResponse:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return PanelResponse(status=status, body=payload)


def _error_response(status: int, code: str, message: str) -> PanelResponse:
    return _json_response(status, {"error": {"code": code, "message": message}})
