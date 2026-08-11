from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_topic_memory.importer import import_markdown_directory
from hermes_topic_memory.router import TopicRegistry, TopicRoutingError
from hermes_topic_memory.store import FileTopicStore


def write_source(root: Path, relative: str, body: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode("utf-8"))
    return path


def test_recursive_import_is_one_file_per_topic_and_preserves_verbatim_utf8(tmp_path: Path) -> None:
    source = tmp_path / "source"
    expected = {
        "一级/同名.md": "# 标题\r\n\r\n第一份 café。\r\n",
        "二级/同名.markdown": "第二份，没有标题。\n",
        "tiny.md": "x",
    }
    for relative, body in expected.items():
        write_source(source, relative, body)
    target = tmp_path / "data"

    result = import_markdown_directory(source, target)

    assert result.topic_count == len(expected)
    registry_value = json.loads((target / "topic-registry.json").read_text(encoding="utf-8"))
    assert "default_topic" not in registry_value
    assert len(registry_value["topics"]) == len(expected)
    assert {item["source_relative_path"] for item in registry_value["topics"]} == set(expected)
    store = FileTopicStore(target)
    for item in registry_value["topics"]:
        relative = item["source_relative_path"]
        memory = store.get_topic(item["topic_id"])
        assert memory.body == expected[relative]
        assert (target / "topics" / item["topic_id"] / "memory.md").read_bytes() == expected[
            relative
        ].encode("utf-8")
        assert memory.metadata.display_name == Path(relative).stem
        assert memory.metadata.source_relative_path == relative
        assert memory.metadata.source_sha256
        assert memory.metadata.source_bytes == len(expected[relative].encode("utf-8"))
        assert memory.metadata.source_chars == len(expected[relative])
        assert memory.metadata.imported_at


def test_topic_ids_are_stable_url_safe_and_suffix_only_slug_collisions(tmp_path: Path) -> None:
    source = tmp_path / "source"
    for relative in ("a-b.md", "a b.md", "北/同名.md", "南/同名.md", "left/report.md", "right/report.md"):
        write_source(source, relative, relative)

    first = import_markdown_directory(source, tmp_path / "first", dry_run=True)
    second = import_markdown_directory(source, tmp_path / "second", dry_run=True)
    imported = import_markdown_directory(source, tmp_path / "data")
    registry = json.loads((tmp_path / "data" / "topic-registry.json").read_text(encoding="utf-8"))
    by_path = {item["source_relative_path"]: item["topic_id"] for item in registry["topics"]}

    assert first.topic_ids == second.topic_ids == imported.topic_ids
    assert first.collision_count == 4
    assert by_path["left/report.md"] == "left-report"
    assert by_path["right/report.md"] == "right-report"
    assert by_path["a-b.md"].startswith("a-b-")
    assert by_path["a b.md"].startswith("a-b-")
    assert by_path["a-b.md"] != by_path["a b.md"]
    assert all(topic_id.replace("-", "").isalnum() and topic_id.isascii() for topic_id in first.topic_ids)


def test_imported_registry_routes_51_explicit_topics_and_chat_ids_without_fallback(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    for index in range(51):
        write_source(source, f"folder-{index:02d}/document.md", f"document {index}\n")
    target = tmp_path / "data"
    import_markdown_directory(source, target)
    value = json.loads((target / "topic-registry.json").read_text(encoding="utf-8"))
    for index, item in enumerate(value["topics"]):
        item["chat_ids"] = [f"chat-{index}"]
    registry = TopicRegistry.from_dict(value)

    assert len(registry.topics) == 51
    for index, topic in enumerate(registry.topics):
        assert registry.route(explicit_topic=topic.topic_id).topic_id == topic.topic_id
        assert registry.route(chat_id=f"chat-{index}").topic_id == topic.topic_id
    with pytest.raises(TopicRoutingError, match="no topic context"):
        registry.route(chat_id="unconfigured")


def test_dry_run_writes_nothing_and_replace_creates_rollback_backup(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_source(source, "one.md", "first body\n")
    target = tmp_path / "data"

    dry_run = import_markdown_directory(source, target, dry_run=True)
    assert dry_run.dry_run is True
    assert not target.exists()

    import_markdown_directory(source, target)
    write_source(source, "one.md", "replacement body\n")
    with pytest.raises(FileExistsError, match="--replace"):
        import_markdown_directory(source, target)

    replaced = import_markdown_directory(source, target, replace=True)
    assert replaced.backup_path is not None and replaced.backup_path.is_dir()
    assert FileTopicStore(target).get_topic(replaced.topic_ids[0]).body == "replacement body\n"
    assert FileTopicStore(replaced.backup_path).get_topic(replaced.topic_ids[0]).body == "first body\n"
    assert not list(tmp_path.glob(".data.import-*"))


def test_import_rejects_invalid_utf8_and_any_symlink_without_writes(tmp_path: Path) -> None:
    invalid_source = tmp_path / "invalid"
    invalid_source.mkdir()
    (invalid_source / "bad.md").write_bytes(b"\xff\xfe")
    invalid_target = tmp_path / "invalid-data"
    with pytest.raises(ValueError, match="valid UTF-8"):
        import_markdown_directory(invalid_source, invalid_target)
    assert not invalid_target.exists()

    source = tmp_path / "source"
    write_source(source, "good.md", "safe\n")
    outside = write_source(tmp_path / "outside", "private.md", "outside\n")
    os.symlink(outside, source / "linked.md")
    target = tmp_path / "data"
    with pytest.raises(ValueError, match="symlink"):
        import_markdown_directory(source, target)
    assert not target.exists()
