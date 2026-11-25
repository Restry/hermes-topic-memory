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
from urllib.parse import parse_qsl, urlsplit

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
from .query_history import QueryHistoryStore, QueryRecord
from .router import TopicRegistry
from .store import FileTopicStore, TopicNotFoundError
from .topic_planner import PlanningError, TopicPlan, TopicPlanner
from .topic_admin import TopicAdminError, TopicAdminService

_TOPIC_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_PAGE_SIZE = 100
_DEFAULT_PAGE_SIZE = 50
_MAX_QUERY_FIELDS = 32


@dataclass(frozen=True, slots=True)
class PanelResponse:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"


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
        "core_memory",
        "data_root",
        "history",
        "memory_service",
        "mcp",
        "store",
    )

    def __init__(self, data_root: str | Path, reader: object, auth_token: str) -> None:
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
        self.store = FileTopicStore(self.data_root)
        self.history = QueryHistoryStore(self.data_root)
        self.admin = TopicAdminService(self.data_root)
        self.core_memory = CoreMemoryService(self.data_root)
        self.memory_service = MemoryService(self.data_root, reader)
        self.mcp = McpProtocol(self.memory_service)

    def dispatch(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> PanelResponse:
        if not self._is_authorized(headers):
            return _error_response(401, "unauthorized", "Authentication required.")

        is_mutation = _is_topic_mutation_target(method, target)
        try:
            path, params = _parse_target(target)
            if path == ("mcp",):
                _reject_params(params, set())
                if method.upper() != "POST":
                    return _error_response(405, "method_not_allowed", "Method not allowed.")
                status, result = self.mcp.dispatch(body)
                return PanelResponse(status, b"") if result is None else _json_response(status, result)
            if path[:2] == ("api", "v1"):
                return self._dispatch_service(method.upper(), path, params, body)
            payload = _parse_json_body(body)
            request_method = method.upper()
            if request_method == "GET":
                return self._dispatch_get(path, params)
            if request_method == "POST":
                return self._dispatch_post(path, params, payload)
            return _error_response(405, "method_not_allowed", "Method not allowed.")
        except _RequestError as error:
            code = "invalid_request" if is_mutation else error.code
            message = _MUTATION_MESSAGES[code] if is_mutation else error.message
            return _error_response(400, code, message)
        except CoreMemoryNotFoundError:
            if is_mutation:
                return _mutation_error("core_memory_not_found")
            return _error_response(404, "not_found", "Resource not found.")
        except (_ResourceNotFound, TopicNotFoundError):
            if is_mutation:
                return _mutation_error("topic_not_found")
            return _error_response(404, "not_found", "Resource not found.")
        except Exception:
            if is_mutation:
                return _mutation_error("operation_failed")
            return _error_response(
                500, "internal_error", "Request could not be completed."
            )

    def _is_authorized(self, headers: Mapping[str, str] | None) -> bool:
        authorization = ""
        if headers is not None:
            for name, value in headers.items():
                if str(name).casefold() == "authorization":
                    authorization = str(value)
                    break
        prefix = "Bearer "
        has_bearer_prefix = authorization.startswith(prefix)
        supplied = authorization[len(prefix) :] if has_bearer_prefix else ""
        matches = secrets.compare_digest(supplied, self._auth_token)
        return has_bearer_prefix and matches

    def _dispatch_service(
        self,
        method: str,
        path: tuple[str, ...],
        params: dict[str, str],
        body: bytes,
    ) -> PanelResponse:
        _reject_params(params, set())
        routes = {
            ("api", "v1", "whoami"): ("GET", "whoami"),
            ("api", "v1", "topics"): ("GET", "list_topics"),
            ("api", "v1", "recall"): ("POST", "recall"),
            ("api", "v1", "remember"): ("POST", "remember"),
            ("api", "v1", "topics", "bind"): ("POST", "bind_topic"),
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
            result = self.memory_service.call(route[1], payload or {}, source="rest")
            return _json_response(200, result)
        except MemoryServiceError as error:
            return _json_response(error.status, {"error": error.to_dict()})

    def _dispatch_get(
        self, path: tuple[str, ...], params: dict[str, str]
    ) -> PanelResponse:
        if path == ("api", "overview"):
            _reject_params(params, set())
            return _json_response(200, self._overview())
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
            return _json_response(200, self._topics(params))
        if path == ("api", "questions"):
            return _json_response(200, self._questions(params))
        if path == ("api", "recycle-bin"):
            _reject_params(params, set())
            return _json_response(200, {"items": self._recycle_items()})

        if len(path) >= 3 and path[:2] == ("api", "topics"):
            topic_id = _validated_topic_id(path[2])
            if len(path) == 3:
                _reject_params(params, set())
                return _json_response(200, {"topic": self._topic_detail(topic_id)})
            if len(path) == 4 and path[3] == "body":
                _reject_params(params, set())
                return _json_response(200, self._current_body(topic_id))
            if len(path) == 4 and path[3] == "questions":
                return _json_response(
                    200, self._questions(params, topic_id=topic_id)
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

    def _dispatch_post(
        self,
        path: tuple[str, ...],
        params: dict[str, str],
        payload: dict[str, Any] | None,
    ) -> PanelResponse:
        _reject_params(params, set())
        if path == ("api", "core-memory", "initialize"):
            return self._initialize_core(payload)
        if path == ("api", "core-memory", "replace"):
            return self._replace_core(payload)
        if path == ("api", "core-memory", "rollback"):
            return self._rollback_core(payload)
        if path == ("api", "recall", "plan"):
            return self._recall_plan(payload)
        if path == ("api", "recall", "run"):
            return self._recall_run(payload)
        if len(path) == 4 and path[:3] == ("api", "recall", "rerun"):
            return self._recall_rerun(path[3], payload)
        if path == ("api", "topics"):
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

    def _recall_plan(self, payload: dict[str, Any] | None) -> PanelResponse:
        value = _required_payload(payload, {"question"})
        question = _raw_question(value)
        registry, planner, _engine = self._recall_services()
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

    def _recall_run(self, payload: dict[str, Any] | None) -> PanelResponse:
        value = _required_payload(payload, {"question", "manual_topics"})
        question = _raw_question(value)
        registry, planner, engine = self._recall_services()
        planning_started_ns = time.perf_counter_ns()
        if "manual_topics" in value:
            selections = _manual_selections(value["manual_topics"])
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
        self, query_id: str, payload: dict[str, Any] | None
    ) -> PanelResponse:
        _required_payload(payload or {}, set())
        try:
            original = self.history.get(query_id)
        except ValueError as error:
            raise _RequestError("invalid_request", "Invalid query id.") from error
        if original is None:
            raise _ResourceNotFound

        registry, planner, engine = self._recall_services()
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
        self,
    ) -> tuple[TopicRegistry, TopicPlanner, MultiRecallEngine]:
        registry = TopicRegistry.from_file(self.data_root / "topic-registry.json")
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

    def _overview(self) -> dict[str, Any]:
        snapshot = TopicCatalog(self.data_root).snapshot()
        recycled = self._recycle_items()
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
                _query_summary(record) for record in self.history.list(limit=10)
            ],
        }

    def _topics(self, params: dict[str, str]) -> dict[str, Any]:
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

        topics = list(TopicCatalog(self.data_root).snapshot().topics)
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

    def _topic_detail(self, topic_id: str) -> dict[str, Any]:
        memory = self.store.get_topic(topic_id)
        try:
            summary = TopicCatalog(self.data_root).snapshot().topic(topic_id)
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

    def _questions(
        self,
        params: dict[str, str],
        *,
        topic_id: str | None = None,
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
        if source is not None and source not in {"hermes", "panel", "cli"}:
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

        records = list(self.history.list())
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

    def _recycle_items(self) -> list[dict[str, Any]]:
        recycle_root = self.data_root / "recycle-bin"
        if not recycle_root.exists():
            return []
        if recycle_root.is_symlink() or not recycle_root.is_dir():
            raise RuntimeError("invalid recycle bin")
        items = [
            self._recycle_item(entry.name)
            for entry in recycle_root.iterdir()
            if not entry.name.startswith(".")
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
    number = int(value)
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
