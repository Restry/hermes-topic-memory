from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hermes_topic_memory.store import (
    FileTopicStore,
    TopicNotFoundError,
    TopicStateError,
)


def test_create_and_read_topic_with_content_fingerprint(tmp_path: Path) -> None:
    store = FileTopicStore(tmp_path)
    created = store.create_topic(
        "alpha",
        "# Alpha\n\nCurrent fact.\n",
        display_name="Alpha Topic",
        aliases=("a",),
        source_policy="verified-evidence-only",
    )

    loaded = store.get_topic("alpha")

    assert loaded == created
    assert loaded.body == "# Alpha\n\nCurrent fact.\n"
    assert loaded.metadata.version == 1
    assert loaded.metadata.content_fingerprint == hashlib.sha256(
        loaded.body.encode("utf-8")
    ).hexdigest()
    assert loaded.metadata.aliases == ("a",)
    assert loaded.metadata.state == "active"
    assert loaded.metadata.source_policy == "verified-evidence-only"


def test_update_saves_history_and_atomically_replaces_current_body(tmp_path: Path) -> None:
    store = FileTopicStore(tmp_path)
    store.create_topic("alpha", "old body\n")

    updated = store.update_topic("alpha", "new body\n")

    assert updated.metadata.version == 2
    assert store.get_topic("alpha").body == "new body\n"
    assert store.read_version("alpha", 1) == "old body\n"
    assert store.list_versions("alpha") == (1, 2)
    topic_dir = tmp_path / "topics" / "alpha"
    assert (topic_dir / "memory.md").read_bytes() == b"new body\n"
    assert not list(topic_dir.glob("*.tmp"))
    assert not list((topic_dir / "versions").glob("*.tmp"))


def test_rollback_restores_historical_body_as_a_new_version(tmp_path: Path) -> None:
    store = FileTopicStore(tmp_path)
    store.create_topic("alpha", "version one\n")
    store.update_topic("alpha", "version two\n")

    rolled_back = store.rollback_topic("alpha", 1)

    assert rolled_back.body == "version one\n"
    assert rolled_back.metadata.version == 3
    assert store.read_version("alpha", 2) == "version two\n"
    assert store.list_versions("alpha") == (1, 2, 3)


def test_archived_topic_rejects_canonical_updates_and_rollback(tmp_path: Path) -> None:
    store = FileTopicStore(tmp_path)
    store.create_topic("alpha", "version one\n")
    store.update_topic("alpha", "version two\n")
    store.set_topic_state("alpha", "archived", expected_state="active")

    with pytest.raises(TopicStateError, match="read-only"):
        store.update_topic("alpha", "version three\n")
    with pytest.raises(TopicStateError, match="read-only"):
        store.rollback_topic("alpha", 1)

    assert store.get_topic("alpha").body == "version two\n"
    assert store.list_versions("alpha") == (1, 2)



def test_missing_topic_and_invalid_identifiers_fail_closed(tmp_path: Path) -> None:
    store = FileTopicStore(tmp_path)

    with pytest.raises(TopicNotFoundError):
        store.get_topic("missing")
    with pytest.raises(ValueError):
        store.create_topic("../escape", "body")
    with pytest.raises(ValueError):
        store.create_topic("alpha", "")
