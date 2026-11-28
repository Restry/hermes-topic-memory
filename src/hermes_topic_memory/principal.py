from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .router import TopicRegistry, TopicRoutingError


@dataclass(frozen=True, slots=True)
class Principal:
    mode: Literal["sso", "env_token", "api_key"]
    actor: str
    role: Literal["read", "write", "admin"]
    key_id: str | None = None
    allowed_topics: frozenset[str] | None = None


class PermissionDeniedError(PermissionError):
    """An authorization denial with a fixed, public-safe message."""

    def __init__(self, code: Literal["forbidden", "topic_forbidden"]) -> None:
        super().__init__(
            "Topic access is forbidden." if code == "topic_forbidden" else "Operation is forbidden."
        )
        self.code = code
        self.status = 403


def require_role(principal: Principal | None, minimum: Literal["read", "write", "admin"]) -> None:
    if principal is None:
        return
    levels = {"read": 0, "write": 1, "admin": 2}
    if levels.get(principal.role, -1) < levels[minimum]:
        raise PermissionDeniedError("forbidden")


def require_topic(principal: Principal | None, topic_id: str) -> None:
    if principal is not None and principal.allowed_topics is not None and topic_id not in principal.allowed_topics:
        raise PermissionDeniedError("topic_forbidden")


def restrict_registry(registry: TopicRegistry, principal: Principal | None) -> TopicRegistry:
    """Limit auto-routing metadata and fallbacks before planning or reading."""
    if principal is None or principal.allowed_topics is None:
        return registry
    topics = tuple(
        topic for topic in registry.topics
        if topic.state == "active" and topic.topic_id in principal.allowed_topics
    )
    default_topic = None
    try:
        fallback = registry.route().topic_id
    except TopicRoutingError:
        pass
    else:
        if fallback in principal.allowed_topics:
            default_topic = fallback
    return TopicRegistry(topics, default_topic=default_topic)
