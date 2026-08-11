from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

_REQUIRED_PLUGIN_FILES = ("__init__.py", "plugin.yaml", "README.md")


@dataclass(frozen=True, slots=True)
class InstallResult:
    source: Path
    target: Path
    source_hash: str
    installed_hash: str | None
    backup: Path | None
    dry_run: bool

    def to_dict(self) -> dict[str, str | bool | None]:
        value = asdict(self)
        return {key: str(item) if isinstance(item, Path) else item for key, item in value.items()}


def tree_hash(root: str | Path) -> str:
    """Hash relative paths and file bytes; reject symlinks and non-directories."""
    root_path = Path(root)
    if not root_path.is_dir():
        raise ValueError(f"plugin source is not a directory: {root_path}")
    digest = hashlib.sha256()
    files = sorted(path for path in root_path.rglob("*") if path.is_file() or path.is_symlink())
    for path in files:
        if path.is_symlink():
            raise ValueError(f"plugin tree must not contain symlinks: {path}")
        relative = path.relative_to(root_path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as source_file:
            while chunk := source_file.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def install_plugin(
    *, source: str | Path, target: str | Path, dry_run: bool = False
) -> InstallResult:
    """Copy one explicitly selected plugin tree without changing Hermes configuration."""
    source_path = Path(source).expanduser().resolve()
    target_path = Path(target).expanduser().resolve()
    _validate_paths(source_path, target_path)
    source_hash = tree_hash(source_path)
    if dry_run:
        return InstallResult(source_path, target_path, source_hash, None, None, True)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path = Path(
        tempfile.mkdtemp(prefix=f".{target_path.name}.install-", dir=target_path.parent)
    )
    staging_path.rmdir()
    backup_path: Path | None = None
    try:
        shutil.copytree(source_path, staging_path, copy_function=shutil.copy2)
        if tree_hash(staging_path) != source_hash:
            raise OSError("staged plugin hash does not match source")

        if target_path.exists():
            backup_path = target_path.with_name(
                f"{target_path.name}.backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
            )
            os.replace(target_path, backup_path)
        try:
            os.replace(staging_path, target_path)
            installed_hash = tree_hash(target_path)
            if installed_hash != source_hash:
                raise OSError("installed plugin hash does not match source")
        except BaseException:
            if target_path.exists():
                shutil.rmtree(target_path)
            if backup_path is not None and backup_path.exists():
                os.replace(backup_path, target_path)
                backup_path = None
            raise
    finally:
        if staging_path.exists():
            shutil.rmtree(staging_path)

    return InstallResult(
        source_path,
        target_path,
        source_hash,
        installed_hash,
        backup_path,
        False,
    )


def _validate_paths(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise ValueError(f"plugin source is not a directory: {source}")
    missing = [name for name in _REQUIRED_PLUGIN_FILES if not (source / name).is_file()]
    if missing:
        raise ValueError(f"plugin source is missing required files: {', '.join(missing)}")
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("source and target must be separate directory trees")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install the Hermes Topic Memory plugin files")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = install_plugin(source=args.source, target=args.target, dry_run=args.dry_run)
    print(json.dumps(result.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
