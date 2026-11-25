from __future__ import annotations

import pytest

from hermes_topic_memory.router import (
    AmbiguousRegistryError,
    MultiTopicRequiredError,
    RouteDecision,
    TopicConfig,
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


@pytest.mark.parametrize(
    "remote",
    [
        "Owner/Repo",
        " OWNER/REPO.git/ ",
        "https://github.com/Owner/Repo.git/",
        "http://GitHub.COM/Owner/Repo",
        "git@github.com:Owner/Repo.git",
        "ssh://git@GITHUB.com:22/Owner/Repo.git/",
        "github.com/OWNER/REPO/",
        "git://github.com/Owner/Repo.git",
    ],
)
def test_repository_forms_have_one_canonical_binding(remote: str) -> None:
    topics = TopicRegistry.from_dict(
        {"topics": [{"topic_id": "alpha", "repo_urls": [remote]}]}
    )

    assert topics.topics[0].repo_urls == ("owner/repo",)
    assert topics.route_context(repo="git@github.com:OWNER/REPO.git") == RouteDecision(
        "alpha", "repo"
    )


def test_repository_hosts_remain_distinct() -> None:
    topics = TopicRegistry.from_dict(
        {
            "topics": [
                {"topic_id": "github", "repo_urls": ["owner/repo"]},
                {"topic_id": "gitlab", "repo_urls": ["https://gitlab.com/Owner/Repo.git"]},
            ]
        }
    )

    assert topics.topics[1].repo_urls == ("gitlab.com/owner/repo",)
    assert topics.route_context(repo="git@gitlab.com:owner/repo").topic_id == "gitlab"


def test_route_context_applies_every_priority_before_default_and_global() -> None:
    topics = TopicRegistry.from_dict(
        {
            "default_topic": "fallback",
            "topics": [
                {"topic_id": "explicit", "aliases": ["selected"]},
                {"topic_id": "chat", "chat_ids": ["chat-1"]},
                {"topic_id": "repo", "repo_urls": ["owner/repo"]},
                {"topic_id": "workspace", "workspace_paths": ["/work/project/"]},
                {"topic_id": "query", "aliases": ["mentioned"]},
                {"topic_id": "fallback"},
                {"topic_id": "global"},
            ],
        }
    )
    context = {
        "explicit_topic": "SELECTED",
        "chat_id": "chat-1",
        "repo": "https://github.com/OWNER/REPO.git/",
        "workspace_path": "/work/project/src/",
        "query": "explain mentioned",
    }
    for field, expected in (
        ("explicit_topic", RouteDecision("explicit", "explicit")),
        ("chat_id", RouteDecision("chat", "chat_id")),
        ("repo", RouteDecision("repo", "repo")),
        ("workspace_path", RouteDecision("workspace", "workspace_path")),
        ("query", RouteDecision("query", "query")),
    ):
        assert topics.route_context(**context) == expected
        context.pop(field)
    assert topics.route_context() == RouteDecision("fallback", "default")
    assert registry().route_context(query="status") == RouteDecision("global", "global")
    with pytest.raises(TopicRoutingError, match="unknown explicit topic"):
        topics.route_context(explicit_topic="missing", chat_id="chat-1")


def test_workspace_uses_longest_directory_prefix_and_path_boundary() -> None:
    topics = TopicRegistry.from_dict(
        {
            "topics": [
                {"topic_id": "parent", "workspace_paths": ["/work/"]},
                {"topic_id": "child", "workspace_paths": ["/work/project///"]},
                {"topic_id": "global"},
            ]
        }
    )

    assert topics.topics[1].workspace_paths == ("/work/project",)
    for path in ("/work/project", "/work/project/", "/work/project/src/file.py"):
        assert topics.route_context(workspace_path=path).topic_id == "child"
    assert topics.route_context(workspace_path="/work/project-two").topic_id == "parent"
    assert topics.route_context(workspace_path="/work/project/../other").topic_id == "parent"
    assert topics.route_context(workspace_path="/workbench").topic_id == "global"
    root = TopicRegistry.from_dict(
        {"topics": [{"topic_id": "root", "workspace_paths": ["/"]}]}
    )
    assert root.route_context(workspace_path="/any/directory").topic_id == "root"


@pytest.mark.parametrize(
    ("field", "first", "second"),
    [
        ("repo_urls", "git@github.com:Owner/Repo.git", "https://github.com/owner/repo/"),
        ("workspace_paths", "/work/project/", "/work/project"),
    ],
)
def test_normalized_bindings_cannot_select_two_active_topics(
    field: str, first: str, second: str
) -> None:
    with pytest.raises(AmbiguousRegistryError):
        TopicRegistry.from_dict(
            {
                "topics": [
                    {"topic_id": "alpha", field: [first]},
                    {"topic_id": "beta", field: [second]},
                ]
            }
        )
    repeated = TopicRegistry.from_dict(
        {"topics": [{"topic_id": "alpha", field: [first, second]}]}
    )
    assert len(repeated.topics) == 1


def test_context_bindings_preserve_archived_and_trashed_isolation() -> None:
    topics = TopicRegistry.from_dict(
        {
            "topics": [
                {"topic_id": "global"},
                {
                    "topic_id": "archive",
                    "aliases": ["old"],
                    "state": "archived",
                    "chat_ids": ["inactive-chat"],
                    "repo_urls": ["owner/inactive"],
                    "workspace_paths": ["/inactive"],
                },
                {
                    "topic_id": "trash",
                    "state": "trashed",
                    "chat_ids": ["inactive-chat"],
                    "repo_urls": ["owner/inactive"],
                    "workspace_paths": ["/inactive/"],
                },
            ]
        }
    )

    assert topics.route_context(explicit_topic="old").topic_id == "archive"
    for context in (
        {"chat_id": "inactive-chat"},
        {"repo": "owner/inactive"},
        {"workspace_path": "/inactive/subdirectory"},
        {"query": "archive and trash"},
    ):
        assert topics.route_context(**context) == RouteDecision("global", "global")
    with pytest.raises(TopicRoutingError, match="unknown explicit topic"):
        topics.route_context(explicit_topic="trash")
    active = TopicRegistry.from_dict(
        {
            "topics": [
                {"topic_id": "alpha", "repo_urls": ["owner/repo"]},
                {"topic_id": "old", "state": "archived", "repo_urls": ["OWNER/REPO.git"]},
            ]
        }
    )
    assert active.route_context(repo="owner/repo").topic_id == "alpha"


def test_context_query_retains_multi_topic_and_unknown_topic_errors() -> None:
    with pytest.raises(MultiTopicRequiredError):
        mention_registry().route_context(query="compare Mafu and AI")
    with pytest.raises(TopicRoutingError, match="unknown topic"):
        registry().route_context(query="missing topic")


def test_old_registries_and_positional_topic_configs_keep_empty_context_defaults() -> None:
    config = TopicConfig("alpha", "Alpha", (), (), "source.md", "active")
    assert config.repo_urls == config.workspace_paths == ()
    assert registry().topics[0].repo_urls == registry().topics[0].workspace_paths == ()
    assert registry().route_context(chat_id="chat-beta").topic_id == "beta"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repo_urls", "owner/repo"),
        ("repo_urls", [""]),
        ("repo_urls", ["owner"]),
        ("repo_urls", ["https://github.com/owner/repo?token=secret"]),
        ("repo_urls", ["owner/../repo"]),
        ("workspace_paths", [" "]),
        ("workspace_paths", [None]),
        ("workspace_paths", "/work"),
    ],
)
def test_registry_rejects_invalid_context_bindings(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        TopicRegistry.from_dict({"topics": [{"topic_id": "alpha", field: value}]})
