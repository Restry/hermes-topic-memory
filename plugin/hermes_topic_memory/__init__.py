"""Hermes Topic Memory plugin entrypoint."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlsplit

from agent.memory_provider import MemoryProvider
from hermes_topic_memory.cache import QuestionCache
from hermes_topic_memory.engine import RecallEngine, RecallResult
from hermes_topic_memory.evidence import EvidenceLedger, EvidenceNotFoundError, EvidenceRecord
from hermes_topic_memory.models import TopicMemory
from hermes_topic_memory.operations import apply_evidence_update
from hermes_topic_memory.reader import OpenAICompatibleReader, Reader, ReaderRequest
from hermes_topic_memory.router import (
    AmbiguousRegistryError,
    RouteDecision,
    TopicRegistry,
    TopicRoutingError,
)
from hermes_topic_memory.store import FileTopicStore
from hermes_topic_memory.updater import (
    EvidenceUpdatePipeline,
    ProposalValidationError,
    TopicUpdater,
    UpdateProposal,
)

logger = logging.getLogger(__name__)

_CONFIG_KEYS = {
    "data_root",
    "registry_path",
    "reader_endpoint",
    "reader_model",
    "default_topic",
    "batch_size",
    "automatic_updates",
    "jaccard_threshold",
    "request_timeout",
}

_MAX_UPDATE_BODY_CHARS = 200_000
_MAX_APPEND_TEXT_CHARS = 20_000
_MISSING = object()

RECALL_SCHEMA = {
    "name": "topic_memory_recall",
    "description": (
        "Answer from exactly one named project or topic archive. Copy the query verbatim from "
        "the user's current archive question; never invent or rewrite dates. After a successful "
        "recall, return only verbatim_answer exactly as received; never add, remove, infer, "
        "normalize, or alter its dates, times, or years. Topic may be omitted when the query "
        "uniquely names a configured topic."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Copy the user's current archive question verbatim; never invent or rewrite "
                    "dates. An exact topic name in it can select the topic."
                ),
            },
            "topic": {
                "type": "string",
                "description": "Optional exact topic ID, alias, or full display name.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

UPDATE_SCHEMA = {
    "name": "topic_memory_update",
    "description": (
        "Safely update one canonical topic using supplied verified raw evidence IDs, or evidence "
        "captured from the active current user turn when evidence IDs are omitted. Exactly one "
        "of body or append_text is required. For a request to add or append a subject, item, or "
        "note without deleting existing content, the agent must use append_text and must not use "
        "topic_memory_recall to obtain the canonical body. Never request, expose, summarize, or "
        "reconstruct the full canonical document."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Optional topic ID or alias."},
            "body": {
                "type": "string",
                "maxLength": _MAX_UPDATE_BODY_CHARS,
                "description": (
                    "Complete replacement Markdown body; use only for an intentional complete "
                    "replacement, never for an additive request."
                ),
            },
            "append_text": {
                "type": "string",
                "maxLength": _MAX_APPEND_TEXT_CHARS,
                "description": (
                    "Only the minimal user-requested Markdown addition, with no invented facts "
                    "and none of the existing canonical body."
                ),
            },
            "evidence_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "uniqueItems": True,
                "description": (
                    "Optional verified raw evidence IDs. Omit only during an active user turn."
                ),
            },
            "reason": {"type": "string", "description": "Why the evidence requires this update."},
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
}

TOPICS_SCHEMA = {
    "name": "topic_memory_topics",
    "description": (
        "Discovery only: list configured topic names and versions; not for archive recall and "
        "never reads topic bodies."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


@dataclass(frozen=True, slots=True)
class _SessionContext:
    chat_id: str | None
    default_topic: str | None


@dataclass(frozen=True, slots=True)
class _RoutedArchiveQuery:
    topic_id: str
    query: str


class _ReaderTopicUpdater:
    def __init__(self, reader: Reader) -> None:
        self.reader = reader

    def propose(
        self, memory: TopicMemory, evidence: tuple[EvidenceRecord, ...]
    ) -> UpdateProposal:
        evidence_payload = json.dumps(
            [record.to_dict() for record in evidence],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        prefix = (
            "You update one canonical topic from verified completed-turn evidence. "
            "Return JSON only with action, topic_id, evidence_ids, reason, and proposed_body. "
            "action must be no_change or topic_update. A topic_update proposed_body must be the "
            "complete replacement Markdown. Never use cached answers as evidence.\n"
            f"TOPIC_ID:{memory.metadata.topic_id}\n"
            f"CURRENT_MEMORY_BEGIN\n{memory.body}CURRENT_MEMORY_END\n"
            f"EVIDENCE_JSON:{evidence_payload}\n"
        )
        response = self.reader.read(
            ReaderRequest(prefix=prefix, question="Classify and propose the bounded canonical update.")
        )
        value = json.loads(response.answer)
        if not isinstance(value, dict):
            raise ValueError("updater response must be a JSON object")
        proposed_body = value.get("proposed_body")
        return UpdateProposal(
            action=value.get("action"),
            topic_id=str(value.get("topic_id", "")),
            evidence_ids=tuple(str(item) for item in value.get("evidence_ids", [])),
            reason=str(value.get("reason", "")),
            proposed_body=proposed_body if isinstance(proposed_body, str) else None,
        )


class HermesTopicMemoryProvider(MemoryProvider):
    """Profile-scoped topic memory with background prefetch and evidence writes."""

    def __init__(
        self,
        *,
        reader: Reader | None = None,
        updater: TopicUpdater | None = None,
    ) -> None:
        self._injected_reader = reader
        self._injected_updater = updater
        self._reader: Reader | None = None
        self._updater: TopicUpdater | None = None
        self._store: FileTopicStore | None = None
        self._ledger: EvidenceLedger | None = None
        self._registry: TopicRegistry | None = None
        self._engine: RecallEngine | None = None
        self._data_root: Path | None = None
        self._current_session_id = ""
        self._contexts: dict[str, _SessionContext] = {}
        self._session_evidence: dict[str, list[str]] = {}
        self._session_topics: dict[str, str] = {}
        self._raw_turn_questions: dict[str, str] = {}
        self._routed_archive_queries: dict[str, _RoutedArchiveQuery] = {}
        self._prefetch_results: dict[tuple[str, str], str] = {}
        self._prefetch_tickets: dict[tuple[str, str], object] = {}
        self._write_futures: list[Future[Any]] = []
        self._prefetch_executor: ThreadPoolExecutor | None = None
        self._write_executor: ThreadPoolExecutor | None = None
        self._lock = threading.RLock()
        self._batch_size = 8
        self._automatic_updates = True
        self._write_enabled = True
        self._initialized = False

    @property
    def name(self) -> str:
        return "hermes_topic_memory"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        hermes_home_value = kwargs.get("hermes_home")
        if not isinstance(hermes_home_value, str) or not hermes_home_value.strip():
            raise ValueError("hermes_home is required")
        hermes_home = Path(hermes_home_value).expanduser().resolve()
        config = _load_config(hermes_home)
        data_root = _resolve_profile_path(
            config.get("data_root"), hermes_home, Path("topic-memory/data")
        )
        registry_path = _resolve_profile_path(
            config.get("registry_path"), hermes_home, data_root / "topic-registry.json"
        )
        registry = TopicRegistry.from_file(registry_path)
        store = FileTopicStore(data_root)
        for topic in registry.topics:
            store.get_topic(topic.topic_id)

        reader = self._injected_reader or _build_reader(config)
        updater = self._injected_updater
        if updater is None and bool(config.get("automatic_updates", True)):
            updater = _ReaderTopicUpdater(reader)

        self._reader = reader
        self._updater = updater
        self._store = store
        self._ledger = EvidenceLedger(data_root)
        self._registry = registry
        self._engine = RecallEngine(
            store=store,
            cache=QuestionCache(
                data_root,
                jaccard_threshold=float(config.get("jaccard_threshold", 0.8)),
            ),
            reader=reader,
        )
        self._data_root = data_root
        self._batch_size = _positive_int(config.get("batch_size", 8), "batch_size")
        self._automatic_updates = bool(config.get("automatic_updates", True))
        self._write_enabled = kwargs.get("agent_context", "primary") == "primary"
        self._current_session_id = session_id
        self._raw_turn_questions = {}
        self._routed_archive_queries = {}
        self._session_topics = {}
        self._contexts = {
            session_id: _SessionContext(
                chat_id=_chat_id(kwargs),
                default_topic=_optional_text(config.get("default_topic")),
            )
        }
        self._prefetch_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="topic-memory-prefetch"
        )
        self._write_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="topic-memory-write"
        )
        self._initialized = True

    def system_prompt_block(self) -> str:
        return (
            "# Topic Memory\n"
            "Questions about a named project or topic archive must use topic_memory_recall; "
            "an exact unique name in the current query overrides session routing. "
            "The recall query must copy the user's current archive question verbatim; never "
            "invent or rewrite dates. After a successful recall, return only verbatim_answer "
            "exactly as received. Never add, remove, infer, normalize, or alter any content from "
            "it, especially dates, times, or years. topic_memory_topics is for discovery only "
            "when available names are unknown, not for answering archive questions. Use "
            "topic_memory_update with supplied verified evidence IDs or during an active user "
            "turn so the current turn can be captured as evidence. For a user request to add or "
            "append a subject, item, or note without deleting existing content, you must use "
            "append_text and must not use topic_memory_recall to obtain the canonical body. Never "
            "request, expose, summarize, or reconstruct the full canonical document. append_text "
            "must contain only the minimal user-requested Markdown addition and no invented facts; "
            "use body only for an intentional complete replacement. Recall and updates are isolated "
            "to one topic; never infer cross-topic context."
        )

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        del turn_number
        session_id = _optional_text(kwargs.get("session_id")) or self._current_session_id
        if not session_id:
            return
        raw_question = message if isinstance(message, str) and message.strip() else None
        with self._lock:
            if raw_question is None:
                self._raw_turn_questions.pop(session_id, None)
                return
            self._raw_turn_questions[session_id] = raw_question
            if not self._initialized:
                return
            try:
                decision = self._route(session_id=session_id, query=raw_question)
            except ValueError:
                return
            if decision.source == "query":
                self._routed_archive_queries[session_id] = _RoutedArchiveQuery(
                    decision.topic_id, raw_question
                )

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not query.strip() or not self._initialized or self._prefetch_executor is None:
            return
        sid = session_id or self._current_session_id
        try:
            decision = self._route(session_id=sid, query=query)
            if decision.source == "query":
                self._remember_routed_archive_query(sid, decision.topic_id, query)
        except Exception:
            logger.debug("Topic-memory prefetch routing failed")
            return
        key = (sid, query)
        ticket = object()
        with self._lock:
            self._prefetch_tickets[key] = ticket
        self._prefetch_executor.submit(
            self._run_prefetch, key, ticket, decision.topic_id, query
        )

    def _run_prefetch(
        self,
        key: tuple[str, str],
        ticket: object,
        topic_id: str,
        query: str,
    ) -> None:
        try:
            result = self._require_engine().recall(topic_id, query)
            block = _format_prefetch(result)
        except Exception:
            logger.debug("Topic-memory background prefetch failed")
            block = ""
        with self._lock:
            if self._prefetch_tickets.get(key) is ticket:
                self._prefetch_tickets.pop(key, None)
                if block:
                    self._prefetch_results[key] = block

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        sid = session_id or self._current_session_id
        with self._lock:
            return self._prefetch_results.pop((sid, query), "")

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: List[Dict[str, Any]] | None = None,
    ) -> None:
        del messages
        if (
            not self._initialized
            or not self._write_enabled
            or not user_content
            or self._write_executor is None
        ):
            return
        sid = session_id or self._current_session_id
        try:
            try:
                decision = self._route(session_id=sid, query=user_content)
            except (TopicRoutingError, ValueError) as error:
                logger.debug(
                    "Topic-memory turn routing failed: %s: %s",
                    type(error).__name__,
                    error,
                )
                return
            if decision.source == "query":
                self._remember_session_topic(sid, decision.topic_id)
            future = self._write_executor.submit(
                self._append_turn,
                decision.topic_id,
                sid,
                user_content,
                assistant_content,
            )
            with self._lock:
                self._write_futures.append(future)
        finally:
            with self._lock:
                self._raw_turn_questions.pop(sid, None)

    def _append_turn(
        self, topic_id: str, session_id: str, user_content: str, assistant_content: str
    ) -> None:
        ledger = self._require_ledger()
        record = ledger.append_turn(topic_id, session_id, user_content, assistant_content)
        should_consolidate = False
        with self._lock:
            pending = self._session_evidence.setdefault(session_id, [])
            pending.append(record.evidence_id)
            should_consolidate = len(pending) >= self._batch_size
        if should_consolidate:
            self._consolidate_session(session_id)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        del messages
        if not self._initialized:
            return
        with self._lock:
            self._raw_turn_questions.pop(self._current_session_id, None)
            self._routed_archive_queries.pop(self._current_session_id, None)
        self._flush_writes()
        self._consolidate_session(self._current_session_id)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        del messages
        if not self._initialized:
            return ""
        with self._lock:
            had_pending = bool(self._session_evidence.get(self._current_session_id)) or bool(
                self._write_futures
            )
        self._flush_writes()
        self._consolidate_session(self._current_session_id)
        return "Topic-memory evidence persisted before compression." if had_pending else ""

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        del parent_session_id
        if not new_session_id.strip():
            raise ValueError("new_session_id must not be empty")
        previous = self._current_session_id
        if reset and self._initialized:
            self._flush_writes()
            self._consolidate_session(previous)
        previous_context = self._contexts.get(previous, _SessionContext(None, None))
        context = _SessionContext(
            chat_id=_chat_id(kwargs) or (None if reset else previous_context.chat_id),
            default_topic=_optional_text(kwargs.get("default_topic"))
            or previous_context.default_topic,
        )
        with self._lock:
            self._current_session_id = new_session_id
            self._raw_turn_questions.pop(previous, None)
            self._raw_turn_questions.pop(new_session_id, None)
            self._routed_archive_queries.pop(previous, None)
            self._routed_archive_queries.pop(new_session_id, None)
            self._contexts[new_session_id] = context
            if reset or rewound:
                self._prefetch_results = {
                    key: value
                    for key, value in self._prefetch_results.items()
                    if key[0] != new_session_id
                }
                self._prefetch_tickets = {
                    key: value
                    for key, value in self._prefetch_tickets.items()
                    if key[0] != new_session_id
                }
                self._session_evidence.pop(new_session_id, None)
                self._session_topics.pop(new_session_id, None)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [RECALL_SCHEMA, UPDATE_SCHEMA, TOPICS_SCHEMA]

    def handle_tool_call(
        self, tool_name: str, args: Dict[str, Any], **kwargs: Any
    ) -> str:
        if tool_name not in {schema["name"] for schema in self.get_tool_schemas()}:
            return _json_result({"error": "unknown_tool"})
        if not self._initialized:
            return _json_result({"error": "provider_not_initialized"})
        if not isinstance(args, dict):
            return _json_result({"error": "invalid_request"})
        if tool_name == "topic_memory_topics":
            try:
                return _json_result(self._topics_result())
            except Exception as error:
                logger.debug(
                    "Topic-memory topic listing failed: %s: %s",
                    type(error).__name__,
                    error,
                )
                return _json_result({"error": "operation_failed"})
        session_id = str(kwargs.get("session_id") or self._current_session_id)
        if tool_name == "topic_memory_recall":
            return self._handle_recall(args, session_id=session_id, kwargs=kwargs)
        return self._handle_update(args, session_id=session_id, kwargs=kwargs)

    def _handle_recall(
        self,
        args: dict[str, Any],
        *,
        session_id: str,
        kwargs: dict[str, Any],
    ) -> str:
        raw_query = args.get("query")
        if not isinstance(raw_query, str) or not raw_query.strip():
            return _json_result({"error": "query_required"})
        explicit = args.get("topic")
        if explicit is not None and (
            not isinstance(explicit, str) or not explicit.strip()
        ):
            return _json_result({"error": "invalid_request"})
        with self._lock:
            current_question = self._raw_turn_questions.get(session_id)
            routed_query = self._routed_archive_queries.get(session_id)
            route_query = (
                routed_query.query
                if routed_query is not None and routed_query.query == current_question
                else raw_query
            )
        try:
            decision = self._route(
                session_id=session_id,
                explicit_topic=explicit,
                query=route_query,
                chat_id=_optional_text(kwargs.get("chat_id")),
            )
            query = raw_query
            if (
                routed_query is not None
                and routed_query.topic_id == decision.topic_id
                and (
                    routed_query.query == current_question
                    or routed_query.query == raw_query
                )
            ):
                query = routed_query.query
            if decision.source in {"explicit", "query"}:
                self._remember_routed_archive_query(
                    session_id, decision.topic_id, query
                )
            result = self._require_engine().recall(decision.topic_id, query)
        except (KeyError, TypeError, ValueError) as error:
            logger.debug(
                "Topic-memory recall rejected: %s: %s",
                type(error).__name__,
                error,
            )
            return _json_result({"error": "invalid_request"})
        except Exception as error:
            logger.debug(
                "Topic-memory recall failed: %s: %s",
                type(error).__name__,
                error,
            )
            return _json_result({"error": "operation_failed"})
        if (
            result.failure is None
            and result.answer is not None
            and decision.source in {"explicit", "query"}
        ):
            self._remember_session_topic(session_id, decision.topic_id)
        return _json_result(_recall_result(result))

    def _handle_update(
        self,
        args: dict[str, Any],
        *,
        session_id: str,
        kwargs: dict[str, Any],
    ) -> str:
        raw_body = args.get("body", _MISSING)
        raw_append_text = args.get("append_text", _MISSING)
        reason = args.get("reason")
        body_supplied = raw_body is not _MISSING
        append_supplied = raw_append_text is not _MISSING
        with self._lock:
            raw_turn = self._raw_turn_questions.get(session_id)
        private_values = tuple(
            value
            for value in (raw_body, raw_append_text, reason, raw_turn)
            if isinstance(value, str) and value
        )
        if body_supplied == append_supplied:
            return self._update_failure_result(
                "invalid_update_arguments",
                ValueError("exactly one of body or append_text must be supplied"),
                private_values=private_values,
            )
        if not isinstance(reason, str) or not reason.strip():
            return self._update_failure_result(
                "invalid_update_arguments",
                ValueError("reason must be non-empty"),
                private_values=private_values,
            )
        if body_supplied:
            if (
                not isinstance(raw_body, str)
                or not raw_body.strip()
                or len(raw_body) > _MAX_UPDATE_BODY_CHARS
            ):
                return self._update_failure_result(
                    "invalid_update_arguments",
                    ValueError("body must be non-empty and within limits"),
                    private_values=private_values,
                )
            update_mode = "replace"
        else:
            if (
                not isinstance(raw_append_text, str)
                or not raw_append_text.strip()
                or len(raw_append_text) > _MAX_APPEND_TEXT_CHARS
            ):
                return self._update_failure_result(
                    "invalid_update_arguments",
                    ValueError("append_text must be non-empty and within limits"),
                    private_values=private_values,
                )
            update_mode = "append"

        raw_topic = args.get("topic", _MISSING)
        if raw_topic is not _MISSING and (
            not isinstance(raw_topic, str) or not raw_topic.strip()
        ):
            return self._update_failure_result(
                "invalid_update_arguments",
                ValueError("topic must be a non-empty string when supplied"),
                private_values=private_values,
            )
        explicit_topic = raw_topic if isinstance(raw_topic, str) else None

        raw_evidence_ids = args.get("evidence_ids", _MISSING)
        evidence_supplied = raw_evidence_ids is not _MISSING
        if evidence_supplied and (
            not isinstance(raw_evidence_ids, list)
            or not raw_evidence_ids
            or not all(
                isinstance(item, str) and bool(item.strip()) for item in raw_evidence_ids
            )
            or len(set(raw_evidence_ids)) != len(raw_evidence_ids)
        ):
            return self._update_failure_result(
                "invalid_update_arguments",
                ValueError("evidence_ids must contain unique non-empty strings"),
                private_values=private_values,
            )

        try:
            decision = self._route(
                session_id=session_id,
                explicit_topic=explicit_topic,
                query=raw_turn,
                chat_id=_optional_text(kwargs.get("chat_id")),
            )
        except AmbiguousRegistryError as error:
            return self._update_failure_result(
                "topic_ambiguous", error, private_values=private_values
            )
        except TopicRoutingError as error:
            return self._update_failure_result(
                "topic_unresolved", error, private_values=private_values
            )

        try:
            memory = self._require_store().get_topic(decision.topic_id)
            if update_mode == "replace":
                replacement_body = raw_body
                if replacement_body == memory.body:
                    return self._update_failure_result(
                        "body_unchanged",
                        ValueError("replacement body must differ from current body"),
                        private_values=private_values,
                    )
            else:
                addition = _normalize_append_text(raw_append_text)
                if _contains_markdown_block(memory.body, addition):
                    return self._update_failure_result(
                        "append_duplicate",
                        ValueError("append_text already exists as a canonical Markdown block"),
                        private_values=private_values,
                    )
                replacement_body = _append_markdown(memory.body, addition)
                if len(replacement_body) > _MAX_UPDATE_BODY_CHARS:
                    return self._update_failure_result(
                        "invalid_update_arguments",
                        ValueError("appended body exceeds maximum size"),
                        private_values=private_values,
                    )

            if evidence_supplied:
                evidence_ids = tuple(raw_evidence_ids)
                evidence_source = "supplied"
            else:
                if raw_turn is None:
                    return self._update_failure_result(
                        "current_turn_evidence_unavailable",
                        ValueError("active raw user turn is unavailable"),
                        private_values=private_values,
                    )
                record = self._require_ledger().append_turn(
                    decision.topic_id,
                    session_id,
                    raw_turn,
                    "",
                )
                evidence_ids = (record.evidence_id,)
                evidence_source = "current_turn"

            result = apply_evidence_update(
                store=self._require_store(),
                ledger=self._require_ledger(),
                topic_id=decision.topic_id,
                body=replacement_body,
                evidence_ids=evidence_ids,
                reason=reason,
            )
        except EvidenceNotFoundError as error:
            return self._update_failure_result(
                "evidence_not_found", error, private_values=private_values
            )
        except ProposalValidationError as error:
            message = str(error)
            if "must differ" in message:
                code = "body_unchanged"
            elif "topic mismatch" in message:
                code = "evidence_topic_mismatch"
            else:
                code = "invalid_update_arguments"
            return self._update_failure_result(
                code, error, private_values=private_values
            )
        except ValueError as error:
            code = "body_unchanged" if "must differ" in str(error) else "invalid_update_arguments"
            return self._update_failure_result(
                code, error, private_values=private_values
            )
        except Exception as error:
            return self._update_failure_result(
                "operation_failed",
                error,
                private_values=private_values,
                unexpected=True,
            )

        if decision.source in {"explicit", "query"}:
            self._remember_session_topic(session_id, decision.topic_id)
        return _json_result(
            {
                "status": result.status,
                "topic_id": result.topic_id,
                "update_mode": update_mode,
                "previous_version": result.previous_version,
                "current_version": result.current_version,
                "evidence_ids": list(result.evidence_ids),
                "evidence_source": evidence_source,
            }
        )

    @staticmethod
    def _update_failure_result(
        code: str,
        error: Exception,
        *,
        private_values: tuple[str, ...],
        unexpected: bool = False,
    ) -> str:
        message = str(error)
        for private_value in private_values:
            message = message.replace(private_value, "<redacted>")
        message = " ".join(message.splitlines()) or "<empty>"
        if len(message) > 300:
            message = f"{message[:297]}..."
        log = logger.warning if unexpected else logger.debug
        log(
            "Topic-memory update failed [%s]: %s: %s",
            code,
            type(error).__name__,
            message,
        )
        return _json_result({"error": code})

    def _remember_session_topic(self, session_id: str, topic_id: str) -> None:
        with self._lock:
            self._session_topics[session_id] = topic_id

    def _remember_routed_archive_query(
        self, session_id: str, topic_id: str, query: str
    ) -> None:
        with self._lock:
            self._routed_archive_queries[session_id] = _RoutedArchiveQuery(topic_id, query)

    def _topics_result(self) -> dict[str, Any]:
        registry = self._require_registry()
        store = self._require_store()
        topics = []
        for topic in registry.topics:
            memory = store.get_topic(topic.topic_id)
            topics.append(
                {
                    "topic_id": topic.topic_id,
                    "display_name": topic.display_name,
                    "aliases": list(topic.aliases),
                    "version": memory.metadata.version,
                }
            )
        return {"topics": topics}

    def _route(
        self,
        *,
        session_id: str,
        explicit_topic: str | None = None,
        query: str | None = None,
        chat_id: str | None = None,
    ) -> RouteDecision:
        with self._lock:
            context = self._contexts.get(session_id, _SessionContext(None, None))
            current_topic = self._session_topics.get(session_id)
        registry = self._require_registry()
        if explicit_topic is not None:
            return registry.route(explicit_topic=explicit_topic)
        if query is not None:
            mentioned_topic = registry.mentioned_topic(query)
            if mentioned_topic is not None:
                return RouteDecision(mentioned_topic, "query")
        if current_topic is not None:
            return RouteDecision(
                registry.resolve_name(current_topic, context="session topic"),
                "session",
            )
        return registry.route(
            chat_id=chat_id or context.chat_id,
            default_topic=context.default_topic,
        )

    def _flush_writes(self) -> None:
        while True:
            with self._lock:
                futures, self._write_futures = self._write_futures, []
            if not futures:
                return
            for future in futures:
                try:
                    future.result()
                except Exception:
                    logger.debug("Topic-memory background write failed")

    def _consolidate_session(self, session_id: str) -> None:
        if not self._automatic_updates or self._updater is None:
            return
        with self._lock:
            evidence_ids = tuple(self._session_evidence.pop(session_id, []))
        if not evidence_ids:
            return
        try:
            first = self._require_ledger().get(evidence_ids[0])
            if any(self._require_ledger().get(item).topic_id != first.topic_id for item in evidence_ids):
                raise ValueError("one session batch crossed topic boundaries")
            EvidenceUpdatePipeline(
                store=self._require_store(),
                ledger=self._require_ledger(),
                updater=self._updater,
            ).process(first.topic_id, evidence_ids)
        except Exception:
            with self._lock:
                self._session_evidence.setdefault(session_id, [])[:0] = list(evidence_ids)
            logger.debug("Topic-memory evidence consolidation failed")

    def shutdown(self) -> None:
        with self._lock:
            self._raw_turn_questions.clear()
            self._session_topics.clear()
            self._routed_archive_queries.clear()
        if not self._initialized:
            return
        self._flush_writes()
        for session_id in tuple(self._session_evidence):
            self._consolidate_session(session_id)
        if self._write_executor is not None:
            self._write_executor.shutdown(wait=True)
        if self._prefetch_executor is not None:
            self._prefetch_executor.shutdown(wait=True)
        self._write_executor = None
        self._prefetch_executor = None
        self._initialized = False

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "reader_endpoint",
                "description": "OpenAI-compatible Reader base URL",
                "required": True,
            },
            {"key": "reader_model", "description": "Reader model name", "required": True},
            {
                "key": "api_key",
                "description": "Reader API key",
                "secret": True,
                "required": False,
                "env_var": "HERMES_TOPIC_MEMORY_API_KEY",
            },
            {
                "key": "data_root",
                "description": "Absolute data root or path relative to active HERMES_HOME",
                "default": "topic-memory/data",
            },
            {
                "key": "registry_path",
                "description": "Absolute registry path or path relative to active HERMES_HOME",
                "default": "topic-memory/data/topic-registry.json",
            },
            {
                "key": "default_topic",
                "description": "Optional session fallback topic ID or alias",
                "default": "",
            },
            {
                "key": "batch_size",
                "description": "Completed turns per automatic update batch",
                "default": 8,
            },
            {
                "key": "automatic_updates",
                "description": "Run evidence updater at batch and session boundaries",
                "default": True,
                "choices": [True, False],
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        home = Path(hermes_home).expanduser().resolve()
        endpoint = values.get("reader_endpoint")
        if isinstance(endpoint, str) and (urlsplit(endpoint).username or urlsplit(endpoint).password):
            raise ValueError("reader_endpoint must not contain credentials")
        config = {key: values[key] for key in _CONFIG_KEYS if key in values}
        config.pop("api_key", None)
        path = home / "topic-memory" / "config.json"
        _atomic_json_write(path, config)

    def backup_paths(self) -> List[str]:
        return []

    def _require_engine(self) -> RecallEngine:
        if self._engine is None:
            raise RuntimeError("provider is not initialized")
        return self._engine

    def _require_store(self) -> FileTopicStore:
        if self._store is None:
            raise RuntimeError("provider is not initialized")
        return self._store

    def _require_ledger(self) -> EvidenceLedger:
        if self._ledger is None:
            raise RuntimeError("provider is not initialized")
        return self._ledger

    def _require_registry(self) -> TopicRegistry:
        if self._registry is None:
            raise RuntimeError("provider is not initialized")
        return self._registry


def _load_config(hermes_home: Path) -> dict[str, Any]:
    path = hermes_home / "topic-memory" / "config.json"
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("topic-memory config root must be an object")
    return {key: item for key, item in value.items() if key in _CONFIG_KEYS}


def _build_reader(config: dict[str, Any]) -> OpenAICompatibleReader:
    endpoint = _optional_text(config.get("reader_endpoint"))
    model = _optional_text(config.get("reader_model"))
    if endpoint is None or model is None:
        raise ValueError("reader_endpoint and reader_model are required")
    return OpenAICompatibleReader(
        endpoint=endpoint,
        model=model,
        api_key=os.environ.get("HERMES_TOPIC_MEMORY_API_KEY"),
        timeout=float(config.get("request_timeout", 60.0)),
    )


def _resolve_profile_path(value: Any, hermes_home: Path, default: Path) -> Path:
    raw = Path(value).expanduser() if isinstance(value, str) and value.strip() else default
    if raw.is_absolute():
        return raw.resolve()
    resolved = (hermes_home / raw).resolve()
    if resolved != hermes_home and hermes_home not in resolved.parents:
        raise ValueError("relative provider paths must stay under hermes_home")
    return resolved


def _chat_id(kwargs: dict[str, Any]) -> str | None:
    for key in ("chat_id", "user_id", "user_id_alt"):
        value = _optional_text(kwargs.get(key))
        if value is not None:
            return value
    return None


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    result = int(value)
    if result < 1:
        raise ValueError(f"{field} must be a positive integer")
    return result


def _format_prefetch(result: RecallResult) -> str:
    if result.answer is None:
        return ""
    return f"[Topic Memory: {result.topic_id} v{result.topic_version}]\n{result.answer}"


def _recall_result(result: RecallResult) -> dict[str, Any]:
    value: dict[str, Any] = {
        "topic_id": result.topic_id,
        "topic_version": result.topic_version,
        "answer": result.answer,
        "source": result.source,
    }
    if result.failure is None and result.answer is not None:
        value["verbatim_answer"] = result.answer
        value["response_policy"] = "copy_verbatim_answer_exactly"
    if result.failure is not None:
        value["failure"] = {
            "code": result.failure.code,
            "message": result.failure.message,
            "attempts": result.failure.attempts,
        }
    return value


def _normalize_append_text(value: str) -> str:
    return value.strip("\r\n")


def _contains_markdown_block(body: str, addition: str) -> bool:
    position = body.find(addition)
    while position >= 0:
        end = position + len(addition)
        starts_on_boundary = position == 0 or body[position - 1] in "\r\n"
        ends_on_boundary = end == len(body) or body[end] in "\r\n"
        if starts_on_boundary and ends_on_boundary:
            return True
        position = body.find(addition, position + 1)
    return False


def _append_markdown(body: str, addition: str) -> str:
    if body.endswith(("\n\n", "\r\n\r\n", "\r\r")):
        separator = ""
    elif body.endswith(("\n", "\r")):
        separator = "\n"
    else:
        separator = "\n\n"
    return f"{body}{separator}{addition}\n"


def _json_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def register(ctx: Any) -> None:
    ctx.register_memory_provider(HermesTopicMemoryProvider())
