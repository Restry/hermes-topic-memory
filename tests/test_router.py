from __future__ import annotations

import pytest

from hermes_topic_memory.router import (
    AmbiguousRegistryError,
    MultiTopicRequiredError,
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


def mention_registry() -> TopicRegistry:
    return TopicRegistry.from_dict(
        {
            "topics": [
                {
                    "topic_id": "dm-mafu",
                    "display_name": "Mafu Project",
                    "aliases": ["Mafu", "马夫"],
                    "chat_ids": [],
                },
                {
                    "topic_id": "ai",
                    "display_name": "AI Tools",
                    "aliases": ["AI"],
                    "chat_ids": [],
                },
                {
                    "topic_id": "tools",
                    "display_name": "Tools Archive",
                    "aliases": ["Tools"],
                    "chat_ids": [],
                },
            ]
        }
    )


def test_several_valid_mentions_are_ordered_and_require_multi_topic() -> None:
    topics = mention_registry()

    assert topics.mentioned_topics("compare Mafu and AI") == ("dm-mafu", "ai")
    assert topics.mentioned_topics("AI then Mafu") == ("ai", "dm-mafu")
    with pytest.raises(MultiTopicRequiredError) as captured:
        topics.mentioned_topic("compare Mafu and AI")

    assert captured.value.topic_ids == ("dm-mafu", "ai")


def test_mentions_use_longest_name_and_collapse_duplicate_aliases() -> None:
    topics = mention_registry()

    assert topics.mentioned_topics("AI Tools and Mafu") == ("ai", "dm-mafu")
    assert topics.mentioned_topics("Mafu, dm-mafu, 马夫, then AI") == (
        "dm-mafu",
        "ai",
    )


def test_mentioned_topics_omit_unknown_text_without_implicit_all_topic() -> None:
    topics = mention_registry()

    assert topics.mentioned_topics("Mafu and an unknown archive") == ("dm-mafu",)
    assert topics.mentioned_topics("an unknown archive") == ()
    assert topics.mentioned_topics("status") == ()


def test_one_conflicting_name_remains_registry_ambiguity_not_multi_topic() -> None:
    topics = TopicRegistry.from_dict(
        {
            "topics": [
                {
                    "topic_id": "alpha",
                    "display_name": "Shared",
                    "aliases": [],
                    "chat_ids": [],
                },
                {
                    "topic_id": "beta",
                    "display_name": "Shared",
                    "aliases": [],
                    "chat_ids": [],
                },
            ]
        }
    )

    with pytest.raises(AmbiguousRegistryError, match="ambiguous"):
        topics.mentioned_topics("what does Shared say?")
    with pytest.raises(AmbiguousRegistryError, match="ambiguous"):
        topics.mentioned_topic("what does Shared say?")


def test_archived_topics_are_explicit_only_and_trashed_topics_never_route() -> None:
    topics = TopicRegistry.from_dict(
        {
            "topics": [
                {
                    "topic_id": "active",
                    "display_name": "Active",
                    "aliases": [],
                    "chat_ids": [],
                    "state": "active",
                },
                {
                    "topic_id": "archive",
                    "display_name": "Archive",
                    "aliases": ["old"],
                    "chat_ids": ["archived-chat"],
                    "state": "archived",
                },
                {
                    "topic_id": "trash",
                    "display_name": "Trash",
                    "aliases": [],
                    "chat_ids": [],
                    "state": "trashed",
                },
            ],
            "default_topic": "archive",
        }
    )

    assert topics.route(explicit_topic="old").topic_id == "archive"
    assert topics.mentioned_topics("Archive and Active") == ("active",)
    with pytest.raises(TopicRoutingError, match="unknown explicit topic"):
        topics.route(explicit_topic="trash")
    with pytest.raises(TopicRoutingError, match="no topic context"):
        topics.route(chat_id="archived-chat")
