from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TopicRoutingError(ValueError):
    """Raised when routing cannot select one topic."""


class AmbiguousRegistryError(TopicRoutingError):
    """Raised when registry keys map to multiple topics."""


@dataclass(frozen=True, slots=True)
class TopicConfig:
    topic_id: str
    display_name: str
    aliases: tuple[str, ...]
    chat_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RouteDecision:
    topic_id: str
    source: str


class TopicRegistry:
    def __init__(
        self,
        topics: tuple[TopicConfig, ...],
        *,
        default_topic: str | None = None,
    ) -> None:
        if not topics:
            raise ValueError("registry must contain at least one topic")
        self._topics = {topic.topic_id: topic for topic in topics}
        if len(self._topics) != len(topics):
            raise AmbiguousRegistryError("duplicate topic id")

        self._names: dict[str, str] = {}
        for topic in topics:
            self._bind(self._names, _normalize_name(topic.topic_id), topic.topic_id, "name")
        for topic in topics:
            for alias in topic.aliases:
                normalized = _normalize_name(alias)
                if not normalized:
                    raise ValueError(f"empty alias for topic: {topic.topic_id}")
                self._bind(self._names, normalized, topic.topic_id, "alias")

        self._chat_ids: dict[str, str] = {}
        for topic in topics:
            for chat_id in topic.chat_ids:
                normalized = _normalize_chat_id(chat_id)
                if not normalized:
                    raise ValueError(f"empty chat id for topic: {topic.topic_id}")
                self._bind(self._chat_ids, normalized, topic.topic_id, "chat id")

        self._default_topic = (
            self.resolve_name(default_topic, context="registry default")
            if default_topic is not None
            else None
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TopicRegistry:
        raw_topics = value.get("topics")
        if not isinstance(raw_topics, list):
            raise ValueError("registry topics must be a list")
        topics: list[TopicConfig] = []
        for raw in raw_topics:
            if not isinstance(raw, dict) or not isinstance(raw.get("topic_id"), str):
                raise ValueError("each registry topic requires a topic_id")
            topic_id = _normalize_name(raw["topic_id"])
            if not topic_id:
                raise ValueError("topic id must not be empty")
            topics.append(
                TopicConfig(
                    topic_id=topic_id,
                    display_name=str(raw.get("display_name", topic_id)),
                    aliases=_string_tuple(raw.get("aliases", ()), "aliases"),
                    chat_ids=_string_tuple(raw.get("chat_ids", ()), "chat_ids"),
                )
            )
        default = value.get("default_topic")
        if default is not None and not isinstance(default, str):
            raise ValueError("default_topic must be a string")
        return cls(tuple(topics), default_topic=default)

    @classmethod
    def from_file(cls, path: str | Path) -> TopicRegistry:
        with Path(path).open(encoding="utf-8") as registry_file:
            value = json.load(registry_file)
        if not isinstance(value, dict):
            raise ValueError("registry root must be an object")
        return cls.from_dict(value)

    @property
    def topics(self) -> tuple[TopicConfig, ...]:
        return tuple(self._topics.values())

    def resolve_name(self, name: str, *, context: str = "topic") -> str:
        topic_id = self._names.get(_normalize_name(name))
        if topic_id is None:
            raise TopicRoutingError(f"unknown {context}: {name!r}")
        return topic_id

    def route(
        self,
        *,
        explicit_topic: str | None = None,
        chat_id: str | None = None,
        default_topic: str | None = None,
    ) -> RouteDecision:
        if explicit_topic is not None:
            return RouteDecision(
                self.resolve_name(explicit_topic, context="explicit topic"), "explicit"
            )
        if chat_id is not None:
            topic_id = self._chat_ids.get(_normalize_chat_id(chat_id))
            if topic_id is not None:
                return RouteDecision(topic_id, "chat_id")
        if default_topic is not None:
            return RouteDecision(
                self.resolve_name(default_topic, context="default topic"), "default"
            )
        if self._default_topic is not None:
            return RouteDecision(self._default_topic, "default")
        if "global" in self._topics:
            return RouteDecision("global", "global")
        raise TopicRoutingError("no topic context and no global fallback")

    @staticmethod
    def _bind(mapping: dict[str, str], key: str, topic_id: str, kind: str) -> None:
        existing = mapping.get(key)
        if existing is not None and existing != topic_id:
            raise AmbiguousRegistryError(
                f"ambiguous {kind} {key!r}: {existing!r} and {topic_id!r}"
            )
        mapping[key] = topic_id


def _normalize_name(value: str) -> str:
    return value.strip().casefold()


def _normalize_chat_id(value: str) -> str:
    return value.strip()


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"{field} must be a list of strings")
    return tuple(value)
