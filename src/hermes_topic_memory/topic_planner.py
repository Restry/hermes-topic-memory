from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Sequence

from .multi_recall import TopicSelection
from .reader import Reader, ReaderRequest, ReaderResponse
from .router import TopicRegistry, TopicRoutingError

_MAX_QUESTION_CHARS = 4_096
_MAX_PLANNER_OUTPUT_BYTES = 64 * 1_024
_MAX_SELECTIONS = 8


class PlanningError(ValueError):
    """Raised when a topic plan is malformed or unsafe to execute."""


@dataclass(frozen=True, slots=True)
class TopicPlan:
    question: str
    mode: Literal["agent", "manual", "rerun"]
    selections: tuple[TopicSelection, ...]


class TopicPlanner:
    """Select topics from registry metadata without reading canonical data."""

    def __init__(self, *, registry: TopicRegistry, model: Reader) -> None:
        if not isinstance(registry, TopicRegistry):
            raise TypeError("registry must be a TopicRegistry")
        self.registry = registry
        self.model = model
        metadata = [
            {
                "topic_id": topic.topic_id,
                "display_name": topic.display_name,
                "aliases": list(topic.aliases),
            }
            for topic in registry.topics
            if topic.state != "trashed"
        ]
        encoded_metadata = json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._prefix = (
            "TOPIC PLANNING RULES:\n"
            "Select the minimum sufficient ordered topic set for the original question. "
            "Select 1 to 8 unique known topics. Do not add a default topic, and never add "
            "global unless the original question requires it. Use only the registry metadata "
            "below; no topic body, answer, query history, or cache is available. Preserve "
            "the original question verbatim. "
            "Return strict JSON only with exactly this shape: "
            '{"topics":[{"topic":"topic id, display name, or alias",'
            '"reason":"short reason"}]}. Do not add keys or markdown.\n\n'
            f"TOPIC REGISTRY METADATA:\n{encoded_metadata}\n"
            "END TOPIC REGISTRY METADATA\n\n"
        )

    def plan(self, question: str) -> TopicPlan:
        _validate_question(question)
        try:
            response = self.model.read(
                ReaderRequest(prefix=self._prefix, question=question)
            )
        except Exception as error:
            raise PlanningError("topic planner model failed") from error
        if not isinstance(response, ReaderResponse):
            raise PlanningError("topic planner must return a ReaderResponse")
        selections = self._parse_output(response.answer)
        return TopicPlan(question=question, mode="agent", selections=selections)

    def manual(
        self, *, question: str, selections: Sequence[TopicSelection]
    ) -> TopicPlan:
        _validate_question(question)
        return TopicPlan(
            question=question,
            mode="manual",
            selections=self._resolve_selections(tuple(selections)),
        )

    def rerun(
        self, *, question: str, selections: Sequence[TopicSelection]
    ) -> TopicPlan:
        _validate_question(question)
        return TopicPlan(
            question=question,
            mode="rerun",
            selections=self._resolve_selections(tuple(selections)),
        )

    def _parse_output(self, raw_output: str) -> tuple[TopicSelection, ...]:
        if not isinstance(raw_output, str):
            raise PlanningError("planner output must be strict JSON")
        if len(raw_output.encode("utf-8")) > _MAX_PLANNER_OUTPUT_BYTES:
            raise PlanningError("planner output must be strict JSON within size limit")
        try:
            value = json.loads(raw_output, object_pairs_hook=_unique_object)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise PlanningError("planner output must be strict JSON") from error
        if not isinstance(value, dict) or set(value) != {"topics"}:
            raise PlanningError("planner output must be strict JSON with only topics")
        raw_topics = value["topics"]
        if not isinstance(raw_topics, list):
            raise PlanningError("planner topics must contain 1 to 8 items")
        parsed: list[TopicSelection] = []
        for raw_topic in raw_topics:
            if not isinstance(raw_topic, dict) or set(raw_topic) != {"topic", "reason"}:
                raise PlanningError("planner output must be strict JSON topic objects")
            try:
                parsed.append(
                    TopicSelection(raw_topic["topic"], raw_topic["reason"])
                )
            except (TypeError, ValueError) as error:
                raise PlanningError(f"invalid planner topic or reason: {error}") from error
        return self._resolve_selections(tuple(parsed))

    def _resolve_selections(
        self, selections: tuple[TopicSelection, ...]
    ) -> tuple[TopicSelection, ...]:
        if not 1 <= len(selections) <= _MAX_SELECTIONS:
            raise PlanningError("selected topics must contain 1 to 8 items")
        if any(not isinstance(item, TopicSelection) for item in selections):
            raise PlanningError("selected topics must be topic selections")

        resolved: list[TopicSelection] = []
        seen: set[str] = set()
        for selection in selections:
            try:
                topic_id = self.registry.resolve_name(
                    selection.topic_id, context="planner topic"
                )
            except TopicRoutingError as error:
                raise PlanningError(str(error)) from error
            if topic_id in seen:
                raise PlanningError(f"duplicate planner topic: {topic_id}")
            seen.add(topic_id)
            resolved.append(TopicSelection(topic_id, selection.reason))
        return tuple(resolved)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _validate_question(question: str) -> None:
    if (
        not isinstance(question, str)
        or not question.strip()
        or len(question) > _MAX_QUESTION_CHARS
    ):
        raise PlanningError("question must contain 1 to 4096 characters")
