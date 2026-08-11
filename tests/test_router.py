from __future__ import annotations

import pytest

from hermes_topic_memory.router import (
    AmbiguousRegistryError,
    TopicRegistry,
    TopicRoutingError,
)


def registry() -> TopicRegistry:
    return TopicRegistry.from_dict(
        {
            "topics": [
                {
                    "topic_id": "alpha",
                    "display_name": "Alpha",
                    "aliases": ["a", "project-a"],
                    "chat_ids": ["chat-alpha"],
                },
                {
                    "topic_id": "beta",
                    "display_name": "Beta",
                    "aliases": ["b"],
                    "chat_ids": ["chat-beta"],
                },
                {
                    "topic_id": "global",
                    "display_name": "Global",
                    "aliases": [],
                    "chat_ids": [],
                },
            ]
        }
    )

def test_full_display_name_is_an_exact_lookup_name() -> None:
    topics = TopicRegistry.from_dict(
        {
            "topics": [
                {
                    "topic_id": "project-alpha",
                    "display_name": "Alpha Project Archive",
                    "aliases": [],
                    "chat_ids": [],
                }
            ]
        }
    )

    assert topics.resolve_name("Alpha Project Archive") == "project-alpha"
    assert topics.route(explicit_topic="alpha project archive").topic_id == "project-alpha"


def test_explicit_topic_or_alias_has_highest_priority() -> None:
    topics = registry()

    assert topics.route(explicit_topic="PROJECT-A", chat_id="chat-beta").topic_id == "alpha"
    assert topics.route(explicit_topic="beta", chat_id="chat-alpha").topic_id == "beta"
    assert topics.route(explicit_topic="a").source == "explicit"


def test_registered_chat_id_routes_to_exactly_one_topic() -> None:
    decision = registry().route(chat_id="chat-beta")

    assert decision.topic_id == "beta"
    assert decision.source == "chat_id"


def test_configured_default_precedes_global_fallback() -> None:
    topics = registry()

    assert topics.route(default_topic="a").topic_id == "alpha"
    assert topics.route().topic_id == "global"
    assert topics.route().source == "global"


def test_unknown_explicit_topic_fails_instead_of_searching_other_topics() -> None:
    topics = registry()

    with pytest.raises(TopicRoutingError, match="unknown explicit topic"):
        topics.route(explicit_topic="missing", chat_id="chat-alpha", default_topic="beta")


def test_registry_rejects_alias_and_chat_id_ambiguity() -> None:
    duplicate_alias = {
        "topics": [
            {"topic_id": "alpha", "aliases": ["shared"], "chat_ids": []},
            {"topic_id": "beta", "aliases": ["SHARED"], "chat_ids": []},
        ]
    }
    duplicate_chat = {
        "topics": [
            {"topic_id": "alpha", "aliases": [], "chat_ids": ["same"]},
            {"topic_id": "beta", "aliases": [], "chat_ids": ["same"]},
        ]
    }

    with pytest.raises(AmbiguousRegistryError):
        TopicRegistry.from_dict(duplicate_alias)
    with pytest.raises(AmbiguousRegistryError):
        TopicRegistry.from_dict(duplicate_chat)


def test_missing_global_and_other_context_fails_closed() -> None:
    topics = TopicRegistry.from_dict(
        {"topics": [{"topic_id": "alpha", "aliases": [], "chat_ids": []}]}
    )

    with pytest.raises(TopicRoutingError, match="no topic context"):
        topics.route(chat_id="unregistered")
