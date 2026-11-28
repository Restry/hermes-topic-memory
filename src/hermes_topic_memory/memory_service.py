from __future__ import annotations

import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .activity_log import ActivityLedger, ActivityRecord, ActivityStatus
from .cache import QuestionCache
from .core_memory import CoreMemoryService
from .dream import DreamConsolidator
from .engine import RecallEngine, RecallFailure, RecallResult
from .multi_recall import (
    MultiRecallEngine,
    MultiRecallResult,
    ReaderCapsuleSynthesizer,
    TopicSelection,
)
from .principal import PermissionDeniedError, Principal, require_role, require_topic, restrict_registry
from .query_history import QueryHistoryStore, QueryRecord, QuerySource, TopicQueryResult
from .reader import Reader
from .remember import RememberService
from .router import (
    MultiTopicRequiredError,
    RouteDecision,
    TopicConfig,
    TopicRegistry,
    TopicRoutingError,
)
from .store import FileTopicStore, TopicNotFoundError, TopicStateError
from .topic_admin import TopicAdminError, TopicAdminService
from .topic_planner import PlanningError, TopicPlanner


class MemoryServiceError(RuntimeError):
    """A public error whose message never contains backend exception text."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


_TEXT = {"type": "string", "minLength": 1}
_CONTEXT = {"topic": _TEXT, "chat_id": _TEXT, "repo": _TEXT, "workspace_path": _TEXT}
_TOOL_DEFINITIONS = (
    ("whoami", "Read the shared Core / WhoAmI memory.", {}, ()),
    (
        "recall",
        "Recall a capsule of at most 200 characters. Context fields are optional; when omitted, the server automatically routes via the lightweight topic catalog.",
        {"query": {**_TEXT, "maxLength": 4096}, **_CONTEXT},
        ("query",),
    ),
    (
        "remember",
        "Persist a fact with durable provenance and optional idempotency.",
        {
            "fact": _TEXT,
            **_CONTEXT,
            "source_agent": _TEXT,
            "source_session": _TEXT,
            "idempotency_key": _TEXT,
            "reason": _TEXT,
        },
        ("fact",),
    ),
    ("list_topics", "List active topics and their context bindings and versions.", {}, ()),
    (
        "bind_topic",
        "Bind a chat, repository, or workspace to an active topic by name or alias.",
        {
            "topic": _TEXT,
            "chat_id": _TEXT,
            "repo_url": _TEXT,
            "workspace_path": _TEXT,
            "expected_version": {"type": "integer", "minimum": 1},
            "expected_fingerprint": {**_TEXT, "pattern": "^[0-9a-f]{64}$"},
        },
        ("topic",),
    ),
    (
        "dream",
        "Run offline memory consolidation (merge duplicates, resolve contradictions, prune noise, refresh catalog summaries).",
        {
            "topic": _TEXT,
            "dry_run": {"type": "boolean"},
            "force": {"type": "boolean"},
        },
        (),
    ),
)


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": list(required),
                "additionalProperties": False,
            },
        }
        for name, description, properties, required in _TOOL_DEFINITIONS
    ]


class MemoryService:
    """One implementation shared by the MCP tools and versioned REST routes."""

    def __init__(
        self, data_root: str | Path, reader: Reader, *, activity_ledger: ActivityLedger | None = None
    ) -> None:
        self.root = Path(data_root)
        self.reader = reader
        self.store = FileTopicStore(self.root)
        self.history = QueryHistoryStore(self.root)
        self.activities = activity_ledger if activity_ledger is not None else ActivityLedger(self.root)
        self.admin = TopicAdminService(self.root)
        self.core = CoreMemoryService(self.root)
        self.remember_service = RememberService(self.root)
        self.dream_consolidator = DreamConsolidator(self.root, reader)

    def call(
        self, name: str, arguments: Any, *, source: QuerySource = "mcp", actor: str = "anonymous",
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        definition = next((item for item in _TOOL_DEFINITIONS if item[0] == name), None)
        if definition is None:
            raise MemoryServiceError("unknown_tool", "Unknown tool.", 404)
        started = time.perf_counter_ns()
        if principal is not None:
            actor = principal.actor
        try:
            _validate_arguments(arguments, definition[2], definition[3])
            result = self._dispatch(name, arguments, source=source, principal=principal)
        except MemoryServiceError as error:
            self._record_activity(
                name, arguments, source=source, actor=actor, started=started, error=error,
                principal=principal,
            )
            raise
        self._record_activity(
            name, arguments, source=source, actor=actor, started=started, result=result,
            principal=principal,
        )
        return result

    def _record_activity(
        self, name: str, arguments: Any, *, source: QuerySource, actor: str, started: int,
        result: dict[str, Any] | None = None, error: MemoryServiceError | None = None,
        principal: Principal | None = None,
    ) -> None:
        result = result or {}
        status = ("denied" if error.code in {"forbidden", "topic_forbidden"} else "error") if error is not None else _activity_status(name, result)
        meta, topic_id = _activity_metadata(name, arguments, result)
        if principal is not None:
            if principal.key_id is not None:
                meta["key_id"] = principal.key_id
            if principal.allowed_topics is not None and topic_id not in principal.allowed_topics:
                topic_id = None
        if error is not None:
            meta["error_code"] = error.code if error.code in {
                "invalid_request", "topic_not_found", "topic_read_only", "routing_error", "operation_failed",
                "forbidden", "topic_forbidden",
            } else "operation_failed"
        transport = source if source in {"mcp", "rest", "panel", "cli"} else "system"
        try:
            self.activities.append(ActivityRecord(
                action=name, source=transport, actor=_activity_label(actor, 128) or "anonymous",
                topic_id=topic_id, status=status,
                latency_ms=max(0, (time.perf_counter_ns() - started) // 1_000_000),
                summary=f"{name}: {status}", meta=meta,
            ))
        except Exception:
            raise MemoryServiceError("operation_failed", "Operation could not be completed.", 500) from None

    def _dispatch(
        self, name: str, arguments: dict[str, Any], *, source: QuerySource,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        try:
            require_role(principal, "write" if name in {"remember", "bind_topic", "dream"} else "read")
            if name == "whoami":
                memory = self.core.get_optional()
                if memory is None:
                    return {
                        "status": "uninitialized",
                        "body": None,
                        "version": None,
                        "notice": {"code": "core_memory_uninitialized", "message": "Core memory is not initialized."},
                    }
                return {"status": "initialized", "body": memory.body, "version": memory.metadata.version}
            if name == "list_topics":
                return {
                    "topics": [
                        self._topic_dict(topic) for topic in self._registry().topics
                        if topic.state == "active" and (
                            principal is None or principal.allowed_topics is None
                            or topic.topic_id in principal.allowed_topics
                        )
                    ]
                }
            if name == "bind_topic":
                if ("expected_version" in arguments) != ("expected_fingerprint" in arguments):
                    raise MemoryServiceError("invalid_request", "Both binding preconditions are required.")
                topic_id = self._registry().resolve_name(arguments["topic"])
                require_topic(principal, topic_id)
                topic = self.admin.bind_context(
                    topic_id,
                    chat_id=arguments.get("chat_id"),
                    repo_url=arguments.get("repo_url"),
                    workspace_path=arguments.get("workspace_path"),
                    expected_version=arguments.get("expected_version"),
                    expected_fingerprint=arguments.get("expected_fingerprint"),
                )
                return {"topic": self._topic_dict(topic)}
            if name == "remember":
                decision = self._registry().route_context(
                    explicit_topic=arguments.get("topic"),
                    chat_id=arguments.get("chat_id"),
                    repo=arguments.get("repo"),
                    workspace_path=arguments.get("workspace_path"),
                )
                require_topic(principal, decision.topic_id)
                result = self.remember_service.remember(
                    decision.topic_id,
                    arguments["fact"],
                    source_agent=arguments.get("source_agent", "unknown"),
                    source_session=arguments.get("source_session"),
                    idempotency_key=arguments.get("idempotency_key"),
                    reason=arguments.get("reason"),
                )
                return asdict(result)
            if name == "dream":
                dry_run = bool(arguments.get("dry_run", False))
                force = bool(arguments.get("force", False))
                if "topic" in arguments:
                    topic_id = self._registry().resolve_name(arguments["topic"])
                    require_topic(principal, topic_id)
                    report = self.dream_consolidator.dream_topic(
                        topic_id, dry_run=dry_run, force=force
                    ).to_dict()
                else:
                    report = self.dream_consolidator.dream_all(
                        dry_run=dry_run, force=force,
                        allowed_topics=principal.allowed_topics if principal is not None else None,
                    ).to_dict()
                return _public_dream_result(report)
            return self._recall(arguments, source=source, principal=principal)
        except MemoryServiceError:
            raise
        except PermissionDeniedError as error:
            raise MemoryServiceError(error.code, str(error), error.status) from None
        except TopicNotFoundError:
            raise MemoryServiceError("topic_not_found", "Topic not found.", 404) from None
        except TopicStateError:
            raise MemoryServiceError("topic_read_only", "Topic is not active.", 409) from None
        except TopicRoutingError:
            raise MemoryServiceError("routing_error", "Context could not select one active topic.") from None
        except TopicAdminError as error:
            if error.code == "topic_not_found":
                raise MemoryServiceError("topic_not_found", "Topic not found.", 404) from None
            if error.code == "concurrent_update":
                raise MemoryServiceError("concurrent_update", "Topic changed; reload before binding.", 409) from None
            raise MemoryServiceError("invalid_request", "Topic operation could not be completed.") from None
        except (ValueError, TypeError):
            raise MemoryServiceError("invalid_request", "Invalid tool arguments.") from None
        except Exception:
            raise MemoryServiceError("operation_failed", "Operation could not be completed.", 500) from None

    def _registry(self) -> TopicRegistry:
        return TopicRegistry.from_file(self.root / "topic-registry.json")

    def _topic_dict(self, topic: TopicConfig) -> dict[str, Any]:
        return {
            "topic_id": topic.topic_id,
            "display_name": topic.display_name,
            "aliases": list(topic.aliases),
            "chat_ids": list(topic.chat_ids),
            "repo_urls": list(topic.repo_urls),
            "workspace_paths": list(topic.workspace_paths),
            "summary": topic.summary,
            "state": topic.state,
            "version": self.store.get_topic(topic.topic_id).metadata.version,
        }

    def _recall(
        self, arguments: dict[str, Any], *, source: QuerySource, principal: Principal | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter_ns()
        question = arguments["query"]
        registry = self._registry()
        candidate_registry = restrict_registry(registry, principal)
        decision, multi_selections, multi_route_source = self._resolve_recall_target(
            registry, arguments, question, principal=principal, candidate_registry=candidate_registry,
        )
        registry = candidate_registry
        if multi_selections is not None:
            for selection in multi_selections:
                require_topic(principal, selection.topic_id)
        elif decision is not None:
            require_topic(principal, decision.topic_id)
        if multi_selections is not None:
            return self._recall_multi(
                registry=registry,
                question=question,
                selections=multi_selections,
                route_source=multi_route_source,
                source=source,
            )
        assert decision is not None
        topic = self.store.get_topic(decision.topic_id)
        try:
            result = RecallEngine(
                store=self.store, cache=QuestionCache(self.root), reader=self.reader
            ).recall(decision.topic_id, question)
        except Exception:
            # Failed Reader calls still receive durable history without exposing
            # backend exception strings or credentials to clients.
            result = RecallResult(
                topic_id=decision.topic_id,
                topic_version=topic.metadata.version,
                answer=None,
                source="failure",
                status="error",
                failure=RecallFailure("recall_failed", "Recall could not be completed.", 1),
            )
        elapsed_ms = (time.perf_counter_ns() - started) // 1_000_000
        record = QueryRecord.create(
            question=question,
            selected_topics=(
                TopicQueryResult(
                    topic_id=result.topic_id,
                    topic_version=result.topic_version,
                    status=result.status,
                    answer=result.answer,
                    source=result.source,
                    match_kind=result.match_kind,
                    latency_ms=elapsed_ms,
                ),
            ),
            final_answer=result.answer,
            selection_reasons={result.topic_id: decision.source},
            source=source,
            session_ref=None,
            total_latency_ms=elapsed_ms,
        )
        self.history.append(record)
        response = {
            "capsule": result.answer,
            "topic_id": result.topic_id,
            "topic_version": result.topic_version,
            "source": result.source,
            "status": result.status,
            "route_source": decision.source,
            "query_id": record.query_id,
        }
        if result.failure is not None:
            response["error"] = {"code": "recall_failed", "message": "Recall could not be completed."}
        return response

    def _resolve_recall_target(
        self,
        registry: TopicRegistry,
        arguments: dict[str, Any],
        question: str,
        *, principal: Principal | None = None, candidate_registry: TopicRegistry,
    ) -> tuple[RouteDecision | None, tuple[TopicSelection, ...] | None, str]:
        explicit_topic = arguments.get("topic")
        if explicit_topic is not None:
            decision = registry.route(explicit_topic=explicit_topic)
            require_topic(principal, decision.topic_id)
            return decision, None, "explicit"

        # Try deterministic context bindings first (chat_id -> repo -> workspace_path).
        if any(arguments.get(k) is not None for k in ("chat_id", "repo", "workspace_path")):
            try:
                bound = registry.route_context(
                    chat_id=arguments.get("chat_id"),
                    repo=arguments.get("repo"),
                    workspace_path=arguments.get("workspace_path"),
                )
                if bound.source in {"chat_id", "repo", "workspace_path"}:
                    require_topic(principal, bound.topic_id)
                    return bound, None, bound.source
            except TopicRoutingError:
                pass

        registry = candidate_registry
        if not registry.topics and principal is not None and principal.allowed_topics is not None:
            raise PermissionDeniedError("topic_forbidden")

        # Next check explicit topic mentions inside the question.
        try:
            mentioned = registry.mentioned_topic(question)
        except MultiTopicRequiredError as error:
            selections = tuple(
                TopicSelection(tid, "query_multi") for tid in error.topic_ids[:8]
            )
            return None, selections, "query_multi"
        except TopicRoutingError:
            mentioned = None
        if mentioned is not None:
            return RouteDecision(mentioned, "query"), None, "query"

        # No context matched: automatically route via the lightweight topic catalog.
        try:
            plan = TopicPlanner(registry=registry, model=self.reader).plan(question)
            if len(plan.selections) == 1:
                return RouteDecision(plan.selections[0].topic_id, "catalog_planner"), None, "catalog_planner"
            return None, plan.selections, "catalog_planner"
        except PlanningError:
            pass

        return registry.route(), None, "default"

    def _recall_multi(
        self,
        *,
        registry: TopicRegistry,
        question: str,
        selections: tuple[TopicSelection, ...],
        route_source: str,
        source: QuerySource,
    ) -> dict[str, Any]:
        recall_engine = RecallEngine(
            store=self.store, cache=QuestionCache(self.root), reader=self.reader
        )
        multi_engine = MultiRecallEngine(
            recall_engine=recall_engine,
            registry=registry,
            synthesizer=ReaderCapsuleSynthesizer(self.reader),  # type: ignore[arg-type]
            query_history=self.history,
        )
        result: MultiRecallResult = multi_engine.recall_multi(
            question=question,
            selections=selections,
            source=source,
        )
        has_error = result.failure is not None or any(r.status == "error" for r in result.topic_results)
        if result.final_answer is not None:
            status = "answered"
        elif has_error:
            status = "error"
        else:
            status = "no_fact"
        response: dict[str, Any] = {
            "capsule": result.final_answer,
            "topic_id": result.topic_results[0].topic_id if result.topic_results else None,
            "topic_version": result.topic_results[0].topic_version if result.topic_results else None,
            "selected_topics": [
                {
                    "topic_id": item.topic_id,
                    "topic_version": item.topic_version,
                    "status": item.status,
                    "source": item.source,
                }
                for item in result.topic_results
            ],
            "source": result.synthesis_source,
            "status": status,
            "route_source": route_source,
            "query_id": result.query_id,
        }
        if status == "error":
            response["error"] = {"code": "recall_failed", "message": "Recall could not be completed."}
        return response


def _public_dream_result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("error") is not None:
        result["error"] = "Dream could not be completed."
    for report in result.get("reports", []):
        if report.get("error") is not None:
            report["error"] = "Dream could not be completed."
    return result


def _activity_label(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(char if char.isprintable() else " " for char in value[:maximum]).strip()


def _activity_status(name: str, result: dict[str, Any]) -> ActivityStatus:
    raw_status = result.get("status")
    selected = result.get("selected_topics", [])
    if name == "recall" and selected and result.get("source") == "none" and all(
        item.get("status") == "no_fact" for item in selected
    ):
        return "no_fact"
    if raw_status in {"error", "aborted_concurrent_write"}:
        return "error"
    reports = result.get("reports", [])
    if any(item.get("status") == "error" for item in selected) or any(
        item.get("status") in {"error", "aborted_concurrent_write"} for item in reports
    ):
        return "error"
    if name == "dream" and (result.get("errors", 0) or result.get("aborted", 0)):
        return "error"
    if raw_status in {"no_fact", "duplicate", "skipped"}:
        return raw_status
    if raw_status in {"uninitialized", "no_change", "skipped_no_new_evidence"}:
        return "skipped"
    if name == "dream" and "reports" in result:
        if not reports or all(item.get("status") in {"no_change", "skipped_no_new_evidence"} for item in reports):
            return "skipped"
    return "ok"


def _activity_metadata(
    name: str, arguments: Any, result: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    meta: dict[str, Any] = {}
    if isinstance(arguments, dict):
        for field, key in (("query", "query_chars"), ("fact", "fact_chars")):
            if isinstance(arguments.get(field), str):
                meta[key] = len(arguments[field])
        if name == "remember" and isinstance(arguments.get("source_agent"), str):
            meta["source_agent"] = _activity_label(arguments["source_agent"], 160)
        if name == "dream":
            for key in ("force", "dry_run"):
                if isinstance(arguments.get(key), bool):
                    meta[key] = arguments[key]
    topic = result.get("topic", {})
    topic_id = result.get("topic_id") or topic.get("topic_id")
    if topic_id is None and isinstance(arguments, dict):
        candidate = arguments.get("topic")
        if isinstance(candidate, str) and len(candidate) <= 128 and candidate.isascii() and all(
            part.isalnum() and part.islower() or part.isdigit() for part in candidate.split("-")
        ):
            topic_id = candidate
    version = result.get("topic_version", result.get("version", topic.get("version")))
    if isinstance(version, int) and not isinstance(version, bool):
        meta["version"] = version
    for key in ("before_version", "after_version", "before_chars", "after_chars", "evidence_count",
                "pruned_count", "merged_count", "conflicts_resolved"):
        if isinstance(result.get(key), int) and not isinstance(result[key], bool):
            meta[key] = result[key]
    for key in ("body", "capsule"):
        if isinstance(result.get(key), str):
            meta["body_chars" if key == "body" else "answer_chars"] = len(result[key])
    # Do not copy model identifiers, route explanations, report summaries, or errors.
    if result.get("route_source") in {
        "explicit", "chat_id", "repo", "workspace_path", "query", "query_multi", "catalog_planner", "default"
    }:
        meta["route_source"] = result["route_source"]
    if result.get("source") in {"cache", "reader", "failure", "synthesizer"}:
        meta["result_source"] = result["source"]
        meta["cache"] = result["source"] == "cache"
    if result.get("status") in {
        "answered", "initialized", "uninitialized", "remembered", "duplicate", "no_fact", "error",
        "consolidated", "no_change", "dry_run", "skipped_no_new_evidence", "aborted_concurrent_write",
    }:
        meta["status"] = result["status"]
    selected = result.get("selected_topics")
    reports = result.get("reports")
    if isinstance(selected, list):
        meta["topic_ids"] = [item["topic_id"] for item in selected[:8]]
        meta["topic_versions"] = {item["topic_id"]: item["topic_version"] for item in selected[:8]}
        meta["topic_count"] = len(selected)
        meta["error_count"] = sum(item.get("status") == "error" for item in selected)
        meta["no_fact_count"] = sum(item.get("status") == "no_fact" for item in selected)
        # A multi-topic event describes the whole invocation, not only its first topic.
        topic_id = None
    elif isinstance(reports, list):
        if len(reports) <= 8:
            meta["topic_ids"] = [item["topic_id"] for item in reports]
        meta["topic_count"] = len(reports)
        meta["error_count"] = sum(item.get("status") in {"error", "aborted_concurrent_write"} for item in reports)
        meta["skipped_count"] = sum(item.get("status") in {"no_change", "skipped_no_new_evidence"} for item in reports)
        meta["consolidated_count"] = sum(item.get("status") == "consolidated" for item in reports)
        topic_id = None
    elif isinstance(result.get("topics"), list):
        if len(result["topics"]) <= 8:
            meta["topic_ids"] = [item["topic_id"] for item in result["topics"]]
        meta["topic_count"] = len(result["topics"])
    return meta, topic_id


def _validate_arguments(value: Any, properties: dict[str, Any], required: tuple[str, ...]) -> None:
    if not isinstance(value, dict) or not set(value).issubset(properties) or not set(required).issubset(value):
        raise MemoryServiceError("invalid_request", "Invalid tool arguments.")
    for name, item in value.items():
        schema = properties[name]
        if schema.get("type") == "boolean":
            if not isinstance(item, bool):
                raise MemoryServiceError("invalid_request", "Invalid tool arguments.")
            continue
        if schema.get("type") == "integer":
            if isinstance(item, bool) or not isinstance(item, int) or item < schema.get("minimum", 1):
                raise MemoryServiceError("invalid_request", "Invalid tool arguments.")
            continue
        if not isinstance(item, str) or not item.strip() or len(item) > schema.get("maxLength", 2_000_000):
            raise MemoryServiceError("invalid_request", "Invalid tool arguments.")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], item):
            raise MemoryServiceError("invalid_request", "Invalid tool arguments.")
