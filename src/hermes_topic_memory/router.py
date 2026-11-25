from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import TopicState

_TOPIC_REFERENCE_MARKERS = (
    " topic",
    "topic ",
    " project",
    "project ",
    "话题",
    "主题",
    "项目",
)


class TopicRoutingError(ValueError):
    """Raised when routing cannot select one topic."""


class AmbiguousRegistryError(TopicRoutingError):
    """Raised when registry keys map to multiple topics."""


class MultiTopicRequiredError(TopicRoutingError):
    """Raised when one query names several independently valid topics."""

    def __init__(self, topic_ids: tuple[str, ...]) -> None:
        if len(topic_ids) < 2:
            raise ValueError("multi-topic routing requires at least two topic ids")
        self.topic_ids = topic_ids
        super().__init__(f"query requires multiple topics: {', '.join(topic_ids)}")

@dataclass(frozen=True, slots=True)
class TopicConfig:
    topic_id: str
    display_name: str
    aliases: tuple[str, ...]
    chat_ids: tuple[str, ...]
    source_relative_path: str | None = None
    state: TopicState = "active"
    repo_urls: tuple[str, ...] = ()
    workspace_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RouteDecision:
    topic_id: str
    source: str


@dataclass(frozen=True, slots=True)
class _NameMatch:
    start: int
    end: int
    name: str
    topic_ids: tuple[str, ...]

class TopicRegistry:
    def __init__(
        self,
        topics: tuple[TopicConfig, ...],
        *,
        default_topic: str | None = None,
    ) -> None:
        topics = tuple(
            replace(
                topic,
                repo_urls=tuple(normalize_repo_url(repo) for repo in topic.repo_urls),
                workspace_paths=tuple(
                    normalize_workspace_path(path) for path in topic.workspace_paths
                ),
            )
            for topic in topics
        )
        self._topics = {topic.topic_id: topic for topic in topics}
        if len(self._topics) != len(topics):
            raise AmbiguousRegistryError("duplicate topic id")
        if any(
            topic.state not in ("active", "archived", "trashed") for topic in topics
        ):
            raise ValueError("registry contains an invalid topic state")

        visible_topics = tuple(topic for topic in topics if topic.state != "trashed")
        self._names, self._ambiguous_names = _build_name_indexes(visible_topics)
        active_topics = tuple(topic for topic in topics if topic.state == "active")
        self._implicit_names, self._implicit_ambiguous_names = _build_name_indexes(
            active_topics
        )

        self._chat_ids: dict[str, str] = {}
        self._repo_urls: dict[str, str] = {}
        self._workspace_paths: dict[str, str] = {}
        for topic in active_topics:
            for chat_id in topic.chat_ids:
                normalized = _normalize_chat_id(chat_id)
                if not normalized:
                    raise ValueError(f"empty chat id for topic: {topic.topic_id}")
                self._bind(self._chat_ids, normalized, topic.topic_id, "chat id")
            for repo in topic.repo_urls:
                self._bind(self._repo_urls, repo, topic.topic_id, "repository")
            for path in topic.workspace_paths:
                self._bind(self._workspace_paths, path, topic.topic_id, "workspace path")

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
                    source_relative_path=_optional_text(raw.get("source_relative_path")),
                    state=_topic_state(raw.get("state", "active")),
                    repo_urls=_string_tuple(raw.get("repo_urls", ()), "repo_urls"),
                    workspace_paths=_string_tuple(
                        raw.get("workspace_paths", ()), "workspace_paths"
                    ),
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
        normalized = _normalize_name(name)
        ambiguous = self._ambiguous_names.get(normalized)
        if ambiguous is not None:
            raise AmbiguousRegistryError(
                f"ambiguous {context}: {name!r} maps to {', '.join(ambiguous)}"
            )
        topic_id = self._names.get(normalized)
        if topic_id is None:
            raise TopicRoutingError(f"unknown {context}: {name!r}")
        return topic_id

    def mentioned_topics(self, query: str) -> tuple[str, ...]:
        normalized_query = _normalize_name(query)
        candidates = [
            _NameMatch(start, end, name, (topic_id,))
            for name, topic_id in self._implicit_names.items()
            for start, end in _query_name_spans(normalized_query, name)
        ]
        candidates.extend(
            _NameMatch(start, end, name, topic_ids)
            for name, topic_ids in self._implicit_ambiguous_names.items()
            for start, end in _query_name_spans(normalized_query, name)
        )
        candidates.sort(
            key=lambda match: (
                match.start,
                -(match.end - match.start),
                match.name,
            )
        )

        selected: list[_NameMatch] = []
        occupied_until = -1
        for candidate in candidates:
            if candidate.start < occupied_until:
                continue
            selected.append(candidate)
            occupied_until = candidate.end

        ambiguous = tuple(match.name for match in selected if len(match.topic_ids) > 1)
        if ambiguous:
            raise AmbiguousRegistryError(
                f"query mentions ambiguous topic names: {', '.join(ambiguous)}"
            )

        topic_ids: list[str] = []
        seen: set[str] = set()
        for match in selected:
            topic_id = match.topic_ids[0]
            if topic_id in seen:
                continue
            seen.add(topic_id)
            topic_ids.append(topic_id)
        return tuple(topic_ids)

    def mentioned_topic(self, query: str) -> str | None:
        matched_topics = self.mentioned_topics(query)
        if len(matched_topics) > 1:
            raise MultiTopicRequiredError(matched_topics)
        if matched_topics:
            return matched_topics[0]
        normalized_query = _normalize_name(query)
        if any(marker in normalized_query for marker in _TOPIC_REFERENCE_MARKERS):
            raise TopicRoutingError("query mentions an unknown topic")
        return None

    def route_context(
        self,
        *,
        explicit_topic: str | None = None,
        chat_id: str | None = None,
        repo: str | None = None,
        workspace_path: str | None = None,
        query: str | None = None,
    ) -> RouteDecision:
        if explicit_topic is not None:
            return self.route(explicit_topic=explicit_topic)
        if chat_id is not None:
            topic_id = self._chat_ids.get(_normalize_chat_id(chat_id))
            if topic_id is not None:
                return RouteDecision(topic_id, "chat_id")
        if repo is not None:
            topic_id = self._repo_urls.get(normalize_repo_url(repo))
            if topic_id is not None:
                return RouteDecision(topic_id, "repo")
        if workspace_path is not None:
            path = normalize_workspace_path(workspace_path)
            matches = (
                prefix
                for prefix in self._workspace_paths
                if path == prefix or path.startswith(prefix.rstrip("/") + "/")
            )
            prefix = max(matches, key=len, default=None)
            if prefix is not None:
                return RouteDecision(self._workspace_paths[prefix], "workspace_path")
        if query is not None:
            topic_id = self.mentioned_topic(query)
            if topic_id is not None:
                return RouteDecision(topic_id, "query")
        return self.route()

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
            topic_id = self.resolve_name(default_topic, context="default topic")
            if self._topics[topic_id].state == "active":
                return RouteDecision(topic_id, "default")
        if (
            self._default_topic is not None
            and self._topics[self._default_topic].state == "active"
        ):
            return RouteDecision(self._default_topic, "default")
        if "global" in self._topics and self._topics["global"].state == "active":
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



def _build_name_indexes(
    topics: tuple[TopicConfig, ...],
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    names: dict[str, str] = {}
    for topic in topics:
        TopicRegistry._bind(names, _normalize_name(topic.topic_id), topic.topic_id, "name")
    display_topics: dict[str, set[str]] = {}
    for topic in topics:
        display_name = _normalize_name(topic.display_name)
        if not display_name:
            raise ValueError(f"empty display name for topic: {topic.topic_id}")
        display_topics.setdefault(display_name, set()).add(topic.topic_id)
    ambiguous: dict[str, tuple[str, ...]] = {}
    for display_name, topic_ids in display_topics.items():
        existing = names.get(display_name)
        if existing is not None and topic_ids != {existing}:
            other = next(topic_id for topic_id in sorted(topic_ids) if topic_id != existing)
            raise AmbiguousRegistryError(
                f"ambiguous display name {display_name!r}: {existing!r} and {other!r}"
            )
        if existing is not None:
            continue
        if len(topic_ids) == 1:
            names[display_name] = next(iter(topic_ids))
        else:
            ambiguous[display_name] = tuple(sorted(topic_ids))
    for topic in topics:
        for alias in topic.aliases:
            normalized = _normalize_name(alias)
            if not normalized:
                raise ValueError(f"empty alias for topic: {topic.topic_id}")
            if normalized in ambiguous:
                raise AmbiguousRegistryError(
                    f"ambiguous alias {normalized!r}: conflicts with duplicate display name"
                )
            TopicRegistry._bind(names, normalized, topic.topic_id, "alias")
    return names, ambiguous

def _normalize_name(value: str) -> str:
    return value.strip().casefold()


def _query_name_spans(query: str, name: str) -> Iterator[tuple[int, int]]:
    start = 0
    while True:
        index = query.find(name, start)
        if index < 0:
            return
        end = index + len(name)
        left_bound = index == 0 or not (
            name[0].isascii()
            and name[0].isalnum()
            and query[index - 1].isascii()
            and query[index - 1].isalnum()
        )
        right_bound = end == len(query) or not (
            name[-1].isascii()
            and name[-1].isalnum()
            and query[end].isascii()
            and query[end].isalnum()
        )
        if left_bound and right_bound:
            yield index, end
        start = index + 1


def normalize_repo_url(value: str) -> str:
    """Canonicalize repository remotes without accessing the network."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("repository must be non-empty text")
    remote = value.strip()
    if any(character.isspace() for character in remote):
        raise ValueError("repository must not contain whitespace")
    if "://" in remote:
        parsed = urlsplit(remote)
        if (
            parsed.scheme.casefold() not in {"https", "http", "ssh", "git"}
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"invalid repository: {value!r}")
        host = parsed.hostname.casefold()
        port = parsed.port
        if port is not None and port != {
            "https": 443, "http": 80, "ssh": 22, "git": 9418
        }[parsed.scheme.casefold()]:
            host += f":{port}"
        path = parsed.path
    else:
        scp = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):(.+)", remote)
        if scp is not None:
            host, path = scp.groups()
            host = host.casefold()
        else:
            parts = remote.split("/", 1)
            if len(parts) == 2 and ("." in parts[0] or parts[0] == "localhost"):
                host, path = parts
                host = host.casefold()
            else:
                host, path = "github.com", remote
    path = path.strip("/").casefold()
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"repository requires an owner and name: {value!r}")
    if any(character in path for character in "?#\\"):
        raise ValueError(f"invalid repository: {value!r}")
    return path if host == "github.com" else f"{host}/{path}"


def normalize_workspace_path(value: str) -> str:
    """Normalize lexical directory paths without requiring them to exist."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("workspace path must be non-empty text")
    return posixpath.normpath(value.strip())


def _normalize_chat_id(value: str) -> str:
    return value.strip()


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"{field} must be a list of strings")
    return tuple(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("source_relative_path must be a string")
    return value


def _topic_state(value: Any) -> TopicState:
    if value not in ("active", "archived", "trashed"):
        raise ValueError(f"invalid topic state: {value!r}")
    return value
