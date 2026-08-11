from __future__ import annotations

from pathlib import Path

from hermes_topic_memory.install import install_plugin, tree_hash


def make_source(tmp_path: Path) -> Path:
    source = tmp_path / "source-plugin"
    source.mkdir()
    (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "plugin.yaml").write_text("name: hermes_topic_memory\n", encoding="utf-8")
    (source / "README.md").write_text("# Plugin\n", encoding="utf-8")
    return source


def test_dry_run_has_explicit_paths_and_makes_no_filesystem_changes(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    target = tmp_path / "ordinary-profile" / "plugins" / "memory" / "hermes_topic_memory"

    result = install_plugin(source=source, target=target, dry_run=True)

    assert result.dry_run is True
    assert result.source == source.resolve()
    assert result.target == target.resolve()
    assert result.source_hash == tree_hash(source)
    assert result.installed_hash is None
    assert result.backup is None
    assert not target.exists()
    assert not target.parent.exists()


def test_install_verifies_hash_backs_up_existing_target_and_does_not_activate(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    profile = tmp_path / "ordinary-profile"
    target = profile / "plugins" / "memory" / "hermes_topic_memory"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("old plugin\n", encoding="utf-8")
    config = profile / "config.yaml"
    config.write_text("memory:\n  provider: builtin\n", encoding="utf-8")

    result = install_plugin(source=source, target=target)

    assert result.dry_run is False
    assert result.installed_hash == result.source_hash == tree_hash(source) == tree_hash(target)
    assert result.backup is not None and result.backup.is_dir()
    assert (result.backup / "old.txt").read_text(encoding="utf-8") == "old plugin\n"
    assert config.read_text(encoding="utf-8") == "memory:\n  provider: builtin\n"
    assert not (profile / ".env").exists()
