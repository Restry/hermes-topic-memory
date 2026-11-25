from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .cache import QuestionCache
from .core_memory import CoreMemoryService
from .engine import RecallEngine, RecallFailure, RecallResult
from .query_history import QueryHistoryStore, QueryRecord, QuerySource, TopicQueryResult
from .remember import RememberService
from .router import TopicConfig, TopicRegistry, TopicRoutingError
from .store import FileTopicStore, TopicNotFoundError, TopicStateError
from .topic_admin import TopicAdminError, TopicAdminService


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
        "Route context to a topic and recall a capsule of at most 200 characters.",
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
        {"topic": _TEXT, "chat_id": _TEXT, "repo_url": _TEXT, "workspace_path": _TEXT},
        ("topic",),
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

    def __init__(self, data_root: str | Path, reader: object) -> None:
        self.root = Path(data_root)
        self.reader = reader
        self.store = FileTopicStore(self.root)
        self.history = QueryHistoryStore(self.root)
        self.admin = TopicAdminService(self.root)
        self.core = CoreMemoryService(self.root)
        self.remember_service = RememberService(self.root)

    def call(self, name: str, arguments: Any, *, source: QuerySource) -> dict[str, Any]:
        definition = next((item for item in _TOOL_DEFINITIONS if item[0] == name), None)
        if definition is None:
            raise MemoryServiceError("unknown_tool", "Unknown tool.", 404)
        _validate_arguments(arguments, definition[2], definition[3])
        try:
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
                    "topics": [self._topic_dict(topic) for topic in self._registry().topics if topic.state == "active"]
                }
            if name == "bind_topic":
                topic_id = self._registry().resolve_name(arguments["topic"])
                topic = self.admin.bind_context(
                    topic_id,
                    chat_id=arguments.get("chat_id"),
                    repo_url=arguments.get("repo_url"),
                    workspace_path=arguments.get("workspace_path"),
                )
                return {"topic": self._topic_dict(topic)}
            if name == "remember":
                decision = self._registry().route_context(
                    explicit_topic=arguments.get("topic"),
                    chat_id=arguments.get("chat_id"),
                    repo=arguments.get("repo"),
                    workspace_path=arguments.get("workspace_path"),
                )
                result = self.remember_service.remember(
                    decision.topic_id,
                    arguments["fact"],
                    source_agent=arguments.get("source_agent", "unknown"),
                    source_session=arguments.get("source_session"),
                    idempotency_key=arguments.get("idempotency_key"),
                    reason=arguments.get("reason"),
                )
                return asdict(result)
            return self._recall(arguments, source=source)
        except MemoryServiceError:
            raise
        except TopicNotFoundError:
            raise MemoryServiceError("topic_not_found", "Topic not found.", 404) from None
        except TopicStateError:
            raise MemoryServiceError("topic_read_only", "Topic is not active.", 409) from None
        except TopicRoutingError:
            raise MemoryServiceError("routing_error", "Context could not select one active topic.") from None
        except TopicAdminError as error:
            if error.code == "topic_not_found":
                raise MemoryServiceError("topic_not_found", "Topic not found.", 404) from None
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
            "state": topic.state,
            "version": self.store.get_topic(topic.topic_id).metadata.version,
        }

    def _recall(self, arguments: dict[str, Any], *, source: QuerySource) -> dict[str, Any]:
        started = time.perf_counter_ns()
        question = arguments["query"]
        decision = self._registry().route_context(
            explicit_topic=arguments.get("topic"),
            chat_id=arguments.get("chat_id"),
            repo=arguments.get("repo"),
            workspace_path=arguments.get("workspace_path"),
            query=question,
        )
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


def _validate_arguments(value: Any, properties: dict[str, Any], required: tuple[str, ...]) -> None:
    if not isinstance(value, dict) or not set(value).issubset(properties) or not set(required).issubset(value):
        raise MemoryServiceError("invalid_request", "Invalid tool arguments.")
    for name, text in value.items():
        if not isinstance(text, str) or not text.strip() or len(text) > properties[name].get("maxLength", 2_000_000):
            raise MemoryServiceError("invalid_request", "Invalid tool arguments.")
