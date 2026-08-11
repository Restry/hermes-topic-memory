from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_topic_memory.install import install_plugin, tree_hash

REPOSITORY_ROOT = Path(__file__).parents[1]


def make_source(tmp_path: Path) -> Path:
    source = tmp_path / "source-plugin"
    source.mkdir()
    (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "plugin.yaml").write_text("name: hermes_topic_memory\n", encoding="utf-8")
    (source / "README.md").write_text("# Plugin\n", encoding="utf-8")
    return source


def test_dry_run_has_explicit_paths_and_makes_no_filesystem_changes(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    target = tmp_path / "ordinary-profile" / "plugins" / "hermes_topic_memory"

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
    target = profile / "plugins" / "hermes_topic_memory"
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


def test_real_hermes_discovers_installed_user_provider_by_configured_name(
    tmp_path: Path,
) -> None:
    hermes_agent_root = _find_hermes_agent_root()
    hermes_python = _find_hermes_python(hermes_agent_root)
    source = REPOSITORY_ROOT / "plugin" / "hermes_topic_memory"
    hermes_home = tmp_path / "hermes-home"
    target = hermes_home / "plugins" / "hermes_topic_memory"
    fake_user_home = tmp_path / "user-home"
    fake_user_home.mkdir()
    (hermes_home / "config.yaml").parent.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        "memory:\n  provider: hermes_topic_memory\n", encoding="utf-8"
    )

    result = install_plugin(source=source, target=target)

    assert result.target == target.resolve()
    assert result.installed_hash == result.source_hash == tree_hash(target)
    script = """
import os
from pathlib import Path

from hermes_cli.config import cfg_get, load_config
from plugins.memory import find_provider_dir, load_memory_provider

configured_name = cfg_get(load_config(), "memory", "provider")
expected = Path(os.environ["HERMES_HOME"]) / "plugins" / configured_name
found = find_provider_dir(configured_name)
assert found is not None
assert found.resolve() == expected.resolve()
provider = load_memory_provider(configured_name)
assert provider is not None
assert provider.__class__.__name__ == "HermesTopicMemoryProvider"
assert provider.name == configured_name
"""
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(fake_user_home),
            "HERMES_HOME": str(hermes_home),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": os.pathsep.join(
                (str(hermes_agent_root), str(REPOSITORY_ROOT / "src"))
            ),
        }
    )
    for name in ("HERMES_CONFIG", "HERMES_ENV", "HERMES_PROFILE"):
        environment.pop(name, None)

    completed = subprocess.run(
        [str(hermes_python), "-c", script],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert not (fake_user_home / ".hermes").exists()


def _find_hermes_agent_root() -> Path:
    configured = os.environ.get("HERMES_AGENT_ROOT")
    candidates = [Path(configured)] if configured else []
    candidates.append(REPOSITORY_ROOT.parent / "hermes-agent")
    for candidate in candidates:
        if (
            (candidate / "plugins" / "memory" / "__init__.py").is_file()
            and (candidate / "agent" / "memory_provider.py").is_file()
        ):
            return candidate.resolve()
    pytest.skip("Hermes source is unavailable; set HERMES_AGENT_ROOT to exercise discovery")


def _find_hermes_python(hermes_agent_root: Path) -> Path:
    candidates = (
        hermes_agent_root / "venv" / "bin" / "python",
        hermes_agent_root / ".venv" / "bin" / "python",
        hermes_agent_root / "venv" / "Scripts" / "python.exe",
        hermes_agent_root / ".venv" / "Scripts" / "python.exe",
        Path(sys.executable),
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        probe = subprocess.run(
            [str(candidate), "-c", "import yaml"],
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            return candidate
    pytest.skip("Hermes source dependencies are unavailable")
