from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import Protocol, Sequence, runtime_checkable

from .cache import normalize_question
from .engine import RecallEngine, RecallFailure, RecallResult
from .query_history import QueryHistoryStore, QueryRecord, QuerySource, TopicQueryResult
from .reader import Reader, ReaderRequest, ReaderResponse
from .router import TopicRegistry

_MAX_QUESTION_CHARS = 4_096
_MAX_TOPIC_NAME_CHARS = 512
_MAX_REASON_CHARS = 500
_MAX_FINAL_ANSWER_CHARS = 200
_MAX_SELECTIONS = 8
_SYNTHESIS_PROMPT_VERSION = "multi-v1"


@dataclass(frozen=True, slots=True)
class TopicSelection:
    topic_id: str
    reason: str

    def __post_init__(self) -> None:
        _validate_text(self.topic_id, "topic", maximum=_MAX_TOPIC_NAME_CHARS)
        _validate_text(self.reason, "selection reason", maximum=_MAX_REASON_CHARS)


@dataclass(frozen=True, slots=True)
class TopicCapsule:
    topic_id: str
    display_name: str
    topic_version: int
    status: str
    source: str
    answer: str | None
    failure_code: str | None = None

    @property
    def label(self) -> str:
        return f"[{self.display_name}/{self.topic_id}]"


@dataclass(frozen=True, slots=True)
class CapsuleSynthesisRequest:
    question: str
    capsules: tuple[TopicCapsule, ...]
    correction_of: str | None = None
    correction_reason: str | None = None

    @property
    def prompt(self) -> str:
        lines = [
            "SYSTEM INSTRUCTION:",
            "Synthesize only the labeled topic capsules below. Never infer facts from topic names ",
            "or from absent canonical memory. Preserve every date and time exactly. Return 1 to ",
            "200 characters. Cite the exact [display/topic_id] label inline for every claim. ",
            "Retain every no_fact or error label so partial coverage is explicit.",
            "",
            "ORIGINAL QUESTION:",
            self.question,
            "",
            "LABELED TOPIC CAPSULES:",
        ]
        for capsule in self.capsules:
            lines.extend(
                (
                    f"LABEL:{capsule.label}",
                    f"VERSION:{capsule.topic_version}",
                    f"STATUS:{capsule.status}",
                    f"SOURCE:{capsule.source}",
                    f"CAPSULE:{capsule.answer if capsule.answer is not None else '<none>'}",
                    f"FAILURE:{capsule.failure_code or '<none>'}",
                    "END TOPIC CAPSULE",
                )
            )
        if self.correction_of is not None:
            lines.extend(
                (
                    "",
                    "CORRECTION REQUIRED:",
                    f"REASON:{self.correction_reason or 'invalid synthesis'}",
                    "Return one corrected answer satisfying every rule above.",
                    "PREVIOUS ANSWER:",
                    self.correction_of,
                )
            )
        return "\n".join(lines)


@runtime_checkable
class CapsuleSynthesizer(Protocol):
    def synthesize(self, request: CapsuleSynthesisRequest) -> ReaderResponse: ...


class ReaderCapsuleSynthesizer:
    """Use an existing Reader client for a body-free capsule synthesis request."""

    def __init__(self, reader: Reader) -> None:
        self.reader = reader

    def synthesize(self, request: CapsuleSynthesisRequest) -> ReaderResponse:
        return self.reader.read(
            ReaderRequest(
                prefix=f"{request.prompt}\n\n",
                question="Return only the final synthesized answer.",
            )
        )


@dataclass(frozen=True, slots=True)
class MultiRecallResult:
    question: str
    topic_results: tuple[RecallResult, ...]
    final_answer: str | None
    synthesis_source: str
    scope_fingerprint: str
    failure: RecallFailure | None = None
    query_id: str | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedSelection:
    topic_id: str
    display_name: str
    reason: str


class MultiRecallEngine:
    def __init__(
        self,
        *,
        recall_engine: RecallEngine,
        registry: TopicRegistry,
        synthesizer: CapsuleSynthesizer,
        query_history: QueryHistoryStore,
        prompt_version: str = _SYNTHESIS_PROMPT_VERSION,
    ) -> None:
        if not isinstance(recall_engine, RecallEngine):
            raise TypeError("recall_engine must be a RecallEngine")
        if not isinstance(registry, TopicRegistry):
            raise TypeError("registry must be a TopicRegistry")
        if not isinstance(synthesizer, CapsuleSynthesizer):
            raise TypeError("synthesizer must implement CapsuleSynthesizer")
        if not isinstance(query_history, QueryHistoryStore):
            raise TypeError("query_history must be a QueryHistoryStore")
        _validate_text(prompt_version, "prompt version", maximum=128)
        self.recall_engine = recall_engine
        self.registry = registry
        self.synthesizer = synthesizer
        self.query_history = query_history
        self.prompt_version = prompt_version
        self._display_names = {
            topic.topic_id: topic.display_name for topic in self.registry.topics
        }

    def recall_multi(
        self,
        *,
        question: str,
        selections: Sequence[TopicSelection],
        source: QuerySource,
        session_ref: str | None = None,
    ) -> MultiRecallResult:
        started_ns = time.perf_counter_ns()
        _validate_text(question, "question", maximum=_MAX_QUESTION_CHARS)
        if source not in {"hermes", "panel", "cli", "mcp", "rest"}:
            raise ValueError(f"invalid query source: {source!r}")
        if session_ref is not None:
            _validate_text(session_ref, "session reference", maximum=1_024)

        raw_selections = tuple(selections)
        if not 1 <= len(raw_selections) <= _MAX_SELECTIONS:
            raise ValueError("selected topics must contain 1 to 8 items")
        if any(not isinstance(item, TopicSelection) for item in raw_selections):
            raise TypeError("selections must contain TopicSelection values")

        resolved = self._resolve_all(raw_selections)
        captured_versions = {
            item.topic_id: self.recall_engine.store.get_topic(item.topic_id).metadata.version
            for item in resolved
        }

        topic_results: list[RecallResult] = []
        for item in resolved:
            topic_started_ns = time.perf_counter_ns()
            try:
                recalled = self.recall_engine.recall(item.topic_id, question)
            except Exception:
                recalled = RecallResult(
                    topic_id=item.topic_id,
                    topic_version=captured_versions[item.topic_id],
                    answer=None,
                    source="failure",
                    status="error",
                    failure=RecallFailure(
                        "recall_error",
                        "Topic recall failed without exposing backend details",
                        1,
                    ),
                )
            latency_ms = _elapsed_ms(topic_started_ns)
            topic_results.append(replace(recalled, latency_ms=latency_ms))

        results = tuple(topic_results)
        capsules = tuple(
            TopicCapsule(
                topic_id=result.topic_id,
                display_name=self._display_names[result.topic_id],
                topic_version=result.topic_version,
                status=result.status,
                source=result.source,
                answer=result.answer,
                failure_code=result.failure.code if result.failure is not None else None,
            )
            for result in results
        )
        scope_fingerprint = _scope_fingerprint(
            question=question,
            capsules=capsules,
            prompt_version=self.prompt_version,
        )
        final_answer, synthesis_source, failure = self._synthesize(question, capsules)

        total_latency_ms = _elapsed_ms(started_ns)
        reasons = {item.topic_id: item.reason for item in resolved}
        record = QueryRecord.create(
            question=question,
            selected_topics=tuple(_query_topic_result(item) for item in results),
            final_answer=final_answer,
            selection_reasons=reasons,
            source=source,
            session_ref=session_ref,
            total_latency_ms=total_latency_ms,
        )
        try:
            self.query_history.append(record)
        except Exception:
            return MultiRecallResult(
                question=question,
                topic_results=results,
                final_answer=None,
                synthesis_source="failure",
                scope_fingerprint=scope_fingerprint,
                failure=RecallFailure(
                    "query_history_write_failed",
                    "Query history could not be persisted",
                    1,
                ),
            )
        return MultiRecallResult(
            question=question,
            topic_results=results,
            final_answer=final_answer,
            synthesis_source=synthesis_source,
            scope_fingerprint=scope_fingerprint,
            failure=failure,
            query_id=record.query_id,
        )

    def _resolve_all(
        self, selections: tuple[TopicSelection, ...]
    ) -> tuple[_ResolvedSelection, ...]:
        resolved_names = tuple(
            (self.registry.resolve_name(item.topic_id, context="selected topic"), item)
            for item in selections
        )
        unique: list[_ResolvedSelection] = []
        seen: set[str] = set()
        for topic_id, selection in resolved_names:
            if topic_id in seen:
                continue
            seen.add(topic_id)
            unique.append(
                _ResolvedSelection(
                    topic_id=topic_id,
                    display_name=self._display_names[topic_id],
                    reason=selection.reason,
                )
            )
        return tuple(unique)

    def _synthesize(
        self,
        question: str,
        capsules: tuple[TopicCapsule, ...],
    ) -> tuple[str | None, str, RecallFailure | None]:
        if not any(capsule.status == "answered" and capsule.answer for capsule in capsules):
            return (
                None,
                "none",
                RecallFailure(
                    "no_answered_topics",
                    "No selected topic produced an answered capsule",
                    0,
                ),
            )

        request = CapsuleSynthesisRequest(question=question, capsules=capsules)
        try:
            response = self.synthesizer.synthesize(request)
            answer = response.answer.strip()
            failure_code = _invalid_synthesis_code(answer, capsules)
            attempts = 1
            if failure_code is not None:
                response = self.synthesizer.synthesize(
                    CapsuleSynthesisRequest(
                        question=question,
                        capsules=capsules,
                        correction_of=response.answer,
                        correction_reason=failure_code,
                    )
                )
                answer = response.answer.strip()
                failure_code = _invalid_synthesis_code(answer, capsules)
                attempts = 2
        except Exception:
            return (
                None,
                "failure",
                RecallFailure(
                    "synthesis_error",
                    "Capsule synthesis failed without exposing backend details",
                    1,
                ),
            )

        if failure_code is not None:
            messages = {
                "synthesis_answer_empty": "Synthesizer returned an empty answer",
                "synthesis_answer_too_long": "Synthesizer answer exceeded 200 characters",
                "synthesis_missing_attribution": "Synthesizer omitted required topic attribution",
            }
            return (
                None,
                "failure",
                RecallFailure(failure_code, messages[failure_code], attempts),
            )
        return answer, response.model or "synthesizer", None


def _query_topic_result(result: RecallResult) -> TopicQueryResult:
    status = result.status if result.status in {"answered", "no_fact", "error"} else "error"
    source = result.source if result.source in {"cache", "reader"} else "failure"
    return TopicQueryResult(
        topic_id=result.topic_id,
        topic_version=result.topic_version,
        status=status,
        answer=result.answer if status == "answered" else None,
        source=source,
        match_kind=result.match_kind,
        latency_ms=result.latency_ms,
    )


def _scope_fingerprint(
    *,
    question: str,
    capsules: tuple[TopicCapsule, ...],
    prompt_version: str,
) -> str:
    normalized = normalize_question(question) or question.strip().casefold()
    topics = []
    for capsule in capsules:
        capsule_value = capsule.answer or f"<{capsule.status}:{capsule.failure_code or 'none'}>"
        topics.append(
            (
                capsule.topic_id,
                capsule.topic_version,
                hashlib.sha256(capsule_value.encode("utf-8")).hexdigest(),
            )
        )
    payload = json.dumps(
        {
            "question": normalized,
            "topics": topics,
            "prompt_version": prompt_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _invalid_synthesis_code(
    answer: str,
    capsules: tuple[TopicCapsule, ...],
) -> str | None:
    if not answer:
        return "synthesis_answer_empty"
    if len(answer) > _MAX_FINAL_ANSWER_CHARS:
        return "synthesis_answer_too_long"
    if any(capsule.label not in answer for capsule in capsules):
        return "synthesis_missing_attribution"
    return None


def _validate_text(value: str, label: str, *, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must contain 1 to {maximum} characters")


def _elapsed_ms(started_ns: int) -> int:
    return (time.perf_counter_ns() - started_ns) // 1_000_000
