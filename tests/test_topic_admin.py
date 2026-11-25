from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_topic_memory.models import TopicMetadata
from hermes_topic_memory.router import TopicRegistry, TopicRoutingError
from hermes_topic_memory.store import FileTopicStore
from hermes_topic_memory.topic_admin import TopicAdminError, TopicAdminService
from hermes_topic_memory.validation import validate_data_root


def _registry(root: Path) -> dict:
    return json.loads((root / "topic-registry.json").read_text(encoding="utf-8"))


def _metadata(root: Path, topic_id: str) -> TopicMetadata:
    value = json.loads(
        (root / "topics" / topic_id / "metadata.json").read_text(encoding="utf-8")
    )
    return TopicMetadata.from_dict(value)


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name != ".topic-admin.lock"
    }


def _admin_with_two_topics(root: Path) -> TopicAdminService:
    admin = TopicAdminService(root)
    admin.create_topic("alpha", "Alpha Topic", ("a",), "# Alpha\n")
    admin.create_topic("beta", "Beta Topic", ("b",), "# Beta\n")
    return admin


def test_full_lifecycle_sequence_and_recycle_layout(tmp_path: Path) -> None:
    root = tmp_path / "data"
    admin = TopicAdminService(root)

    created = admin.create_topic("e2e-topic", "E2E Topic", (), "# Body\n")
    archived = admin.archive("e2e-topic", expected_version=1)
    assert archived.metadata.state == "archived"
    assert _registry(root)["topics"][0]["state"] == "archived"

    restored = admin.restore("e2e-topic")
    assert restored.metadata.state == "active"
    trashed = admin.trash("e2e-topic", expected_version=1)
    assert trashed.state == "trashed"
    assert not (root / "topics/e2e-topic").exists()
    recycle = root / "recycle-bin/e2e-topic"
    assert (recycle / "topic/memory.md").read_text(encoding="utf-8") == "# Body\n"
    tombstone = json.loads((recycle / "tombstone.json").read_text(encoding="utf-8"))
    assert tombstone["topic_id"] == "e2e-topic"
    assert tombstone["display_name"] == "E2E Topic"
    assert tombstone["prior_state"] == "active"
    assert "trashed_at" in tombstone

    restored_again = admin.restore_from_trash("e2e-topic")
    assert restored_again.metadata.state == "active"
    admin.trash("e2e-topic", expected_version=1)
    deleted = admin.permanent_delete("e2e-topic", confirmation="E2E Topic")

    assert created.metadata.version == restored.metadata.version == 1
    assert restored_again.metadata.version == 1
    assert deleted.status == "permanently_deleted"
    assert not (root / "topics/e2e-topic").exists()
    assert not recycle.exists()


@pytest.mark.parametrize(
    ("topic_id", "display_name", "aliases"),
    [
        ("alpha", "Different", ()),
        ("new-id", "ALPHA TOPIC", ()),
        ("new-id", "New Topic", ("A",)),
        ("new-id", "a", ()),
    ],
)
def test_duplicate_create_is_atomic_and_leaves_no_orphan(
    tmp_path: Path,
    topic_id: str,
    display_name: str,
    aliases: tuple[str, ...],
) -> None:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    admin.create_topic("alpha", "Alpha Topic", ("a",), "alpha body")
    before = _files(root)

    with pytest.raises(TopicAdminError) as captured:
        admin.create_topic(topic_id, display_name, aliases, "new body")

    assert captured.value.code in {"topic_exists", "alias_conflict"}
    assert _files(root) == before
    if topic_id != "alpha":
        assert not (root / "topics" / topic_id).exists()


@pytest.mark.parametrize(
    ("expected_version", "expected_fingerprint"),
    [(0, None), (1, "0" * 64)],
)
def test_full_edit_concurrency_mismatch_has_zero_writes(
    tmp_path: Path,
    expected_version: int,
    expected_fingerprint: str | None,
) -> None:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    created = admin.create_topic("alpha", "Alpha", (), "old body")
    before = _files(root)

    with pytest.raises(TopicAdminError) as captured:
        admin.full_edit(
            "alpha",
            "new body",
            expected_version=expected_version,
            expected_fingerprint=(
                created.metadata.content_fingerprint
                if expected_fingerprint is None
                else expected_fingerprint
            ),
        )

    assert captured.value.code == "concurrent_update"
    assert _files(root) == before


def test_full_edit_preserves_prior_version_and_invalidates_only_topic_cache(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    admin = _admin_with_two_topics(root)
    alpha = FileTopicStore(root).get_topic("alpha")
    cache_dir = root / "qa-cache"
    cache_dir.mkdir()
    (cache_dir / "alpha.jsonl").write_text("alpha-cache\n", encoding="utf-8")
    (cache_dir / "beta.jsonl").write_text("beta-cache\n", encoding="utf-8")

    updated = admin.full_edit(
        "alpha",
        "# Alpha v2\n",
        expected_version=alpha.metadata.version,
        expected_fingerprint=alpha.metadata.content_fingerprint,
    )

    store = FileTopicStore(root)
    assert updated.metadata.version == 2
    assert store.read_version("alpha", 1) == "# Alpha\n"
    assert not (cache_dir / "alpha.jsonl").exists()
    assert (cache_dir / "beta.jsonl").read_text(encoding="utf-8") == "beta-cache\n"


def test_wrong_permanent_delete_confirmation_changes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    admin.create_topic("alpha", "Alpha Topic", (), "PRIVATE BODY")
    admin.trash("alpha", expected_version=1)
    before = _files(root)

    with pytest.raises(TopicAdminError) as captured:
        admin.permanent_delete("alpha", confirmation="alpha topic")

    assert captured.value.code == "confirmation_mismatch"
    assert _files(root) == before


def test_deletion_audit_is_fsynced_before_any_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    events: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    def fail_before_remove(point: str) -> None:
        if point == "before_permanent_remove":
            audit = root / "operation-audit/deletions.jsonl"
            assert audit.is_file()
            assert "fsync" in events
            events.append("remove")
            raise RuntimeError("injected removal failure")

    monkeypatch.setattr("hermes_topic_memory.topic_admin.os.fsync", recording_fsync)
    admin = TopicAdminService(root, failure_injector=fail_before_remove)
    admin.create_topic("alpha", "Alpha", (), "body")
    admin.trash("alpha", expected_version=1)
    events.clear()

    with pytest.raises(RuntimeError, match="injected removal failure"):
        admin.permanent_delete("alpha", confirmation="Alpha")

    assert events.index("fsync") < events.index("remove")
    assert (root / "recycle-bin/alpha/topic/memory.md").is_file()


@pytest.mark.parametrize(
    "crafted_id", ["../escape", "alpha/../../escape", "/tmp/escape", ".", "alpha\\beta"]
)
def test_crafted_ids_cannot_escape_topics(tmp_path: Path, crafted_id: str) -> None:
    root = tmp_path / "data"
    outside = tmp_path / "escape"
    outside.write_text("keep", encoding="utf-8")
    admin = TopicAdminService(root)

    with pytest.raises(ValueError):
        admin.create_topic(crafted_id, "Escape", (), "body")
    for operation in (
        lambda: admin.archive(crafted_id, expected_version=1),
        lambda: admin.restore(crafted_id),
        lambda: admin.trash(crafted_id, expected_version=1),
        lambda: admin.restore_from_trash(crafted_id),
        lambda: admin.permanent_delete(crafted_id, confirmation="Escape"),
    ):
        with pytest.raises(ValueError):
            operation()

    assert outside.read_text(encoding="utf-8") == "keep"


def test_injected_transition_failure_restores_registry_and_metadata(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"

    def fail_after_metadata(point: str) -> None:
        if point == "after_metadata_write":
            raise RuntimeError("injected transition failure")

    admin = TopicAdminService(root)
    admin.create_topic("alpha", "Alpha", (), "body")
    failing = TopicAdminService(root, failure_injector=fail_after_metadata)

    with pytest.raises(RuntimeError, match="injected transition failure"):
        failing.archive("alpha", expected_version=1)

    assert _metadata(root, "alpha").state == "active"
    config = TopicRegistry.from_file(root / "topic-registry.json").topics[0]
    assert config.state == "active"
    assert not (root / ".topic-admin-transaction").exists()


def test_pending_failed_rollback_is_recovered_before_next_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"

    def fail_after_metadata(point: str) -> None:
        if point == "after_metadata_write":
            raise RuntimeError("injected transition failure")

    failing = TopicAdminService(root, failure_injector=fail_after_metadata)
    TopicAdminService(root).create_topic("alpha", "Alpha", (), "body")

    def leave_pending(_entries: list[dict[str, object]]) -> None:
        raise OSError("injected rollback failure")

    monkeypatch.setattr(failing, "_restore_transaction", leave_pending)
    with pytest.raises(OSError, match="injected rollback failure"):
        failing.archive("alpha", expected_version=1)
    assert (root / ".topic-admin-transaction/manifest.json").is_file()

    recovered = TopicAdminService(root)
    memory = FileTopicStore(root).get_topic("alpha")
    with pytest.raises(TopicAdminError) as captured:
        recovered.full_edit(
            "alpha",
            "changed",
            expected_version=0,
            expected_fingerprint=memory.metadata.content_fingerprint,
        )

    assert captured.value.code == "concurrent_update"
    assert _metadata(root, "alpha").state == "active"
    assert TopicRegistry.from_file(root / "topic-registry.json").topics[0].state == "active"
    assert not (root / ".topic-admin-transaction").exists()


def test_trash_preserves_cache_and_query_history(tmp_path: Path) -> None:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    admin.create_topic("alpha", "Alpha", (), "body")
    cache = root / "qa-cache/alpha.jsonl"
    history = root / "query-history/2026-08.jsonl"
    cache.parent.mkdir()
    history.parent.mkdir()
    cache.write_text("cache sentinel\n", encoding="utf-8")
    history.write_text("history sentinel\n", encoding="utf-8")

    admin.trash("alpha", expected_version=1)

    assert cache.read_text(encoding="utf-8") == "cache sentinel\n"
    assert history.read_text(encoding="utf-8") == "history sentinel\n"


def test_permanent_delete_removes_only_target_assets_and_keeps_minimal_audit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    admin = _admin_with_two_topics(root)
    (root / "topics/alpha/evidence.jsonl").write_text("ALPHA EVIDENCE", encoding="utf-8")
    (root / "topics/beta/evidence.jsonl").write_text("BETA EVIDENCE", encoding="utf-8")
    for directory in (root / "qa-cache", root / "update-proposals"):
        directory.mkdir()
        (directory / "alpha.jsonl").write_text("ALPHA ASSET", encoding="utf-8")
        (directory / "beta.jsonl").write_text("BETA ASSET", encoding="utf-8")
    history = root / "query-history/2026-08.jsonl"
    history.parent.mkdir()
    history.write_text("QUERY HISTORY", encoding="utf-8")
    admin.trash("alpha", expected_version=1)
    beta_before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "topics/beta").rglob("*")
        if path.is_file()
    }

    admin.permanent_delete("alpha", confirmation="Alpha Topic")

    assert not (root / "topics/alpha").exists()
    assert not (root / "recycle-bin/alpha").exists()
    assert not (root / "qa-cache/alpha.jsonl").exists()
    assert not (root / "update-proposals/alpha.jsonl").exists()
    assert (root / "qa-cache/beta.jsonl").read_text(encoding="utf-8") == "BETA ASSET"
    assert (root / "update-proposals/beta.jsonl").read_text(encoding="utf-8") == "BETA ASSET"
    assert history.read_text(encoding="utf-8") == "QUERY HISTORY"
    assert {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "topics/beta").rglob("*")
        if path.is_file()
    } == beta_before
    audit_text = (root / "operation-audit/deletions.jsonl").read_text(encoding="utf-8")
    audit = json.loads(audit_text)
    assert audit == {
        "action": "permanent_delete",
        "display_name": "Alpha Topic",
        "topic_id": "alpha",
        "deleted_at": audit["deleted_at"],
    }
    assert "PRIVATE" not in audit_text
    assert "body" not in audit_text.casefold()


def test_archived_topic_is_explicit_read_only_but_not_implicit(tmp_path: Path) -> None:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    admin.create_topic("alpha", "Alpha", ("a",), "body")
    admin.archive("alpha", expected_version=1)
    registry = TopicRegistry.from_file(root / "topic-registry.json")

    assert registry.route(explicit_topic="a").topic_id == "alpha"
    assert registry.mentioned_topics("what does Alpha say?") == ()
    with pytest.raises(TopicRoutingError, match="no topic context"):
        registry.route()
    with pytest.raises(TopicAdminError) as captured:
        memory = FileTopicStore(root).get_topic("alpha")
        admin.full_edit(
            "alpha",
            "changed",
            expected_version=memory.metadata.version,
            expected_fingerprint=memory.metadata.content_fingerprint,
        )
    assert captured.value.code == "invalid_state"


def test_create_update_and_bind_persist_normalized_contexts(tmp_path: Path) -> None:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    created = admin.create_topic(
        "alpha", "Alpha", ("a",), "canonical body",
        chat_ids=(" chat-first ",),
        repo_urls=("https://GitHub.com/Owner/Repo.git/",),
        workspace_paths=("/work/alpha/",),
        make_default=True,
    )
    item = _registry(root)["topics"][0]
    assert item["repo_urls"] == ["owner/repo"]
    assert item["workspace_paths"] == ["/work/alpha"]
    assert item["chat_ids"] == ["chat-first"]

    updated = admin.update_topic("alpha", repo_urls=("Another/Repo.git",))
    assert updated.repo_urls == ("another/repo",)
    assert updated.workspace_paths == ("/work/alpha",)
    bound = admin.bind_context(
        "alpha", chat_id="chat-second", repo_url="git@github.com:THIRD/REPO.git",
        workspace_path="/work/second/",
    )
    assert bound.chat_ids == ("chat-first", "chat-second")
    assert bound.repo_urls == ("another/repo", "third/repo")
    assert bound.workspace_paths == ("/work/alpha", "/work/second")
    before = _files(root)
    assert admin.bind_context("alpha", repo_url="https://github.com/Third/Repo.git/") == bound
    assert _files(root) == before

    registry = TopicRegistry.from_file(root / "topic-registry.json")
    assert registry.topics[0] == bound
    assert registry.route_context(repo="third/repo").topic_id == "alpha"
    assert registry.route_context(chat_id="chat-second").topic_id == "alpha"
    assert registry.route_context(workspace_path="/work/second/subdir").topic_id == "alpha"
    assert FileTopicStore(root).get_topic("alpha") == created
    assert _registry(root)["default_topic"] == "alpha"
    assert validate_data_root(root) == 1


def test_context_update_can_clear_fields_and_preserves_legacy_registry_data(tmp_path: Path) -> None:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    admin.create_topic("alpha", "Alpha", ("a",), "body")
    value = _registry(root)
    item = value["topics"][0]
    item.pop("repo_urls")
    item.pop("workspace_paths")
    item["source_relative_path"] = "source/alpha.md"
    item["custom_field"] = {"keep": True}
    value["custom_root"] = ["keep"]
    (root / "topic-registry.json").write_text(json.dumps(value), encoding="utf-8")

    bound = admin.bind_context("alpha", repo_url="OWNER/REPO", workspace_path="/work/")
    assert bound.repo_urls == ("owner/repo",)
    assert bound.workspace_paths == ("/work",)
    cleared = admin.update_topic("alpha", repo_urls=(), chat_ids=())
    assert cleared.repo_urls == cleared.chat_ids == ()
    assert cleared.workspace_paths == ("/work",)
    persisted = _registry(root)
    assert persisted["custom_root"] == ["keep"]
    assert persisted["topics"][0]["source_relative_path"] == "source/alpha.md"
    assert persisted["topics"][0]["custom_field"] == {"keep": True}
    assert persisted["topics"][0]["aliases"] == ["a"]


@pytest.mark.parametrize("operation", ["create", "update", "bind"])
@pytest.mark.parametrize("context", ["repo", "workspace", "chat"])
def test_context_conflicts_fail_atomically(
    tmp_path: Path, operation: str, context: str
) -> None:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    admin.create_topic(
        "alpha", "Alpha", (), "alpha body", chat_ids=("shared-chat",),
        repo_urls=("Owner/Repo",), workspace_paths=("/work/alpha/",),
    )
    if operation != "create":
        admin.create_topic("beta", "Beta", (), "beta body")
    before = _files(root)
    fields = {
        "repo": {"repo_urls": ("git@github.com:OWNER/REPO.git",)},
        "workspace": {"workspace_paths": ("/work/alpha",)},
        "chat": {"chat_ids": (" shared-chat ",)},
    }[context]
    scalar = {
        "repo": {"repo_url": "https://github.com/owner/repo.git/"},
        "workspace": {"workspace_path": "/work/alpha"},
        "chat": {"chat_id": "shared-chat"},
    }[context]

    with pytest.raises(TopicAdminError) as captured:
        if operation == "create":
            admin.create_topic("beta", "Beta", (), "beta body", **fields)
        elif operation == "update":
            admin.update_topic("beta", **fields)
        else:
            admin.bind_context("beta", **scalar)
    assert captured.value.code == "alias_conflict"
    assert _files(root) == before


def test_context_bindings_survive_archive_restore_trash_and_restore(tmp_path: Path) -> None:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    admin.create_topic(
        "alpha", "Alpha", (), "body", repo_urls=("Owner/Repo",),
        workspace_paths=("/work/alpha/",), make_default=True,
    )
    original = _registry(root)["topics"][0]
    admin.archive("alpha", expected_version=1)
    archived = TopicRegistry.from_file(root / "topic-registry.json")
    assert archived.topics[0].repo_urls == ("owner/repo",)
    with pytest.raises(TopicRoutingError, match="no topic context"):
        archived.route_context(repo="owner/repo", workspace_path="/work/alpha/src")
    with pytest.raises(TopicAdminError) as captured:
        admin.bind_context("alpha", repo_url="other/repo")
    assert captured.value.code == "invalid_state"
    admin.restore("alpha")
    assert _registry(root)["topics"][0] == original
    admin.trash("alpha", expected_version=1)
    assert _registry(root)["topics"] == []
    tombstone = json.loads(
        (root / "recycle-bin/alpha/tombstone.json").read_text(encoding="utf-8")
    )
    assert tombstone["prior_registry_item"] == original
    admin.restore_from_trash("alpha")
    assert _registry(root)["topics"][0] == original
    assert _registry(root)["default_topic"] == "alpha"
    assert validate_data_root(root) == 1


def test_restore_rejects_context_reassigned_after_trash_without_writes(tmp_path: Path) -> None:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    admin.create_topic("alpha", "Alpha", (), "body", repo_urls=("owner/repo",))
    admin.trash("alpha", expected_version=1)
    admin.create_topic("beta", "Beta", (), "body", repo_urls=("Owner/Repo.git",))
    before = _files(root)

    with pytest.raises(TopicAdminError) as captured:
        admin.restore_from_trash("alpha")
    assert captured.value.code == "alias_conflict"
    assert _files(root) == before


def test_context_mutation_rolls_back_registry_when_write_fails(tmp_path: Path) -> None:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    admin.create_topic("alpha", "Alpha", (), "body")
    before = _files(root)

    def fail(stage: str) -> None:
        if stage == "after_registry_write":
            raise RuntimeError("injected write failure")

    failing = TopicAdminService(root, failure_injector=fail)
    with pytest.raises(RuntimeError, match="injected write failure"):
        failing.bind_context("alpha", repo_url="owner/repo")
    assert _files(root) == before


@pytest.mark.parametrize(
    "replacement",
    [
        {"repo_urls": ["git@github.com:OWNER/REPO.git"]},
        {"workspace_paths": ["/work/alpha/"]},
        {"repo_urls": [""]},
        {"workspace_paths": [" "]},
    ],
)
def test_validate_data_root_checks_context_binding_errors(
    tmp_path: Path, replacement: dict[str, object]
) -> None:
    root = tmp_path / "data"
    admin = TopicAdminService(root)
    admin.create_topic(
        "alpha", "Alpha", (), "body", repo_urls=("owner/repo",),
        workspace_paths=("/work/alpha",),
    )
    admin.create_topic("beta", "Beta", (), "body")
    value = _registry(root)
    value["topics"][1].update(replacement)
    (root / "topic-registry.json").write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError):
        validate_data_root(root)
