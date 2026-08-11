from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from statistics import median
from typing import Any

from .store import FileTopicStore
from .validation import validate_data_root

_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_LENGTH = 96


@dataclass(frozen=True, slots=True)
class SourceDocument:
    path: Path
    relative_path: str
    display_name: str
    body: str
    payload: bytes
    sha256: str
    chars: int
    lines: int
    topic_id: str


@dataclass(frozen=True, slots=True)
class ImportResult:
    source_dir: Path
    data_root: Path
    topic_count: int
    total_bytes: int
    total_chars: int
    total_lines: int
    min_chars: int
    max_chars: int
    median_chars: int | float
    collision_count: int
    topic_ids: tuple[str, ...]
    dry_run: bool
    backup_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "dry-run" if self.dry_run else "imported",
            "source_dir": str(self.source_dir),
            "data_root": str(self.data_root),
            "topic_count": self.topic_count,
            "total_bytes": self.total_bytes,
            "total_chars": self.total_chars,
            "total_lines": self.total_lines,
            "min_chars": self.min_chars,
            "max_chars": self.max_chars,
            "median_chars": self.median_chars,
            "collision_count": self.collision_count,
            "backup_path": str(self.backup_path) if self.backup_path is not None else None,
        }


def import_markdown_directory(
    source_dir: str | Path,
    data_root: str | Path,
    *,
    dry_run: bool = False,
    replace: bool = False,
) -> ImportResult:
    """Import every Markdown file as one topic through a staged data-root swap."""
    source = _resolve_source(source_dir)
    target_input = Path(data_root).expanduser()
    if target_input.is_symlink():
        raise ValueError(f"target data root must not be a symlink: {target_input}")
    target = target_input.resolve(strict=False)
    _validate_separate_trees(source, target)
    if target.exists() or target.is_symlink():
        if not replace:
            raise FileExistsError(f"target data root exists; pass --replace to replace it: {target}")

    documents, collision_count = _read_documents(source)
    summary = _result(
        source,
        target,
        documents,
        collision_count=collision_count,
        dry_run=dry_run,
    )
    if dry_run:
        return summary

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.import-", dir=target.parent))
    backup: Path | None = None
    try:
        imported_at = _utc_now()
        store = FileTopicStore(staging)
        registry_topics: list[dict[str, Any]] = []
        for document in documents:
            store.create_topic(
                document.topic_id,
                document.body,
                display_name=document.display_name,
                source_policy="imported-markdown-verbatim",
                source_relative_path=document.relative_path,
                source_sha256=document.sha256,
                source_bytes=len(document.payload),
                source_chars=document.chars,
                source_lines=document.lines,
                imported_at=imported_at,
            )
            registry_topics.append(
                {
                    "topic_id": document.topic_id,
                    "display_name": document.display_name,
                    "aliases": [],
                    "chat_ids": [],
                    "source_relative_path": document.relative_path,
                }
            )
        _write_registry(staging / "topic-registry.json", {"topics": registry_topics})
        validate_data_root(staging)

        if target.exists() or target.is_symlink():
            backup = _next_backup_path(target)
            os.replace(target, backup)
        try:
            os.replace(staging, target)
            _fsync_directory(target.parent)
        except BaseException:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            if backup is not None and backup.exists():
                os.replace(backup, target)
                backup = None
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    return ImportResult(
        source_dir=summary.source_dir,
        data_root=summary.data_root,
        topic_count=summary.topic_count,
        total_bytes=summary.total_bytes,
        total_chars=summary.total_chars,
        total_lines=summary.total_lines,
        min_chars=summary.min_chars,
        max_chars=summary.max_chars,
        median_chars=summary.median_chars,
        collision_count=summary.collision_count,
        topic_ids=summary.topic_ids,
        dry_run=False,
        backup_path=backup,
    )


def _resolve_source(source_dir: str | Path) -> Path:
    source = Path(source_dir).expanduser()
    if source.is_symlink():
        raise ValueError(f"source directory must not be a symlink: {source}")
    try:
        resolved = source.resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError(f"source directory does not exist: {source}") from error
    if not resolved.is_dir():
        raise ValueError(f"source is not a directory: {resolved}")
    return resolved


def _validate_separate_trees(source: Path, target: Path) -> None:
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("source directory and data root must be separate directory trees")


def _raise_walk_error(error: OSError) -> None:
    raise error


def _read_documents(source: Path) -> tuple[tuple[SourceDocument, ...], int]:
    raw_documents: list[tuple[Path, str, str, bytes, str]] = []
    for current_name, directory_names, file_names in os.walk(
        source, followlinks=False, onerror=_raise_walk_error
    ):
        directory_names.sort()
        file_names.sort()
        current = Path(current_name)
        for name in (*directory_names, *file_names):
            candidate = current / name
            if candidate.is_symlink():
                raise ValueError(f"source tree must not contain symlinks: {candidate}")
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(source):
                raise ValueError(f"source path escapes source root: {candidate}")
        for name in file_names:
            path = current / name
            if path.suffix.casefold() not in _MARKDOWN_SUFFIXES:
                continue
            mode = path.stat(follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError(f"Markdown source must be a regular file: {path}")
            relative_path = path.relative_to(source).as_posix()
            payload = path.read_bytes()
            try:
                body = payload.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise ValueError(
                    f"Markdown source is not valid UTF-8: {relative_path}"
                ) from error
            if not body:
                raise ValueError(f"Markdown source must not be empty: {relative_path}")
            raw_documents.append(
                (path, relative_path, body, payload, _slug_base(relative_path))
            )
    raw_documents.sort(key=lambda item: item[1])
    if not raw_documents:
        raise ValueError(f"source directory contains no Markdown files: {source}")

    topic_ids, collision_count = _assign_topic_ids(raw_documents)
    documents = tuple(
        SourceDocument(
            path=path,
            relative_path=relative_path,
            display_name=PurePosixPath(relative_path).stem,
            body=body,
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
            chars=len(body),
            lines=len(body.splitlines()),
            topic_id=topic_id,
        )
        for (path, relative_path, body, payload, _base), topic_id in zip(
            raw_documents, topic_ids, strict=True
        )
    )
    return documents, collision_count


def _slug_base(relative_path: str) -> str:
    path_without_suffix = PurePosixPath(relative_path).with_suffix("").as_posix()
    ascii_path = unicodedata.normalize("NFKD", path_without_suffix).encode(
        "ascii", "ignore"
    ).decode("ascii")
    slug = _NON_ALNUM.sub("-", ascii_path.casefold()).strip("-")
    return slug[:_MAX_SLUG_LENGTH].rstrip("-") or "topic"


def _assign_topic_ids(
    raw_documents: list[tuple[Path, str, str, bytes, str]],
) -> tuple[tuple[str, ...], int]:
    groups: dict[str, list[int]] = {}
    for index, item in enumerate(raw_documents):
        groups.setdefault(item[4], []).append(index)
    collision_indexes = {
        index for indexes in groups.values() if len(indexes) > 1 for index in indexes
    }
    hashes = [hashlib.sha256(item[1].encode("utf-8")).hexdigest() for item in raw_documents]
    for suffix_length in range(8, 65):
        candidates = tuple(
            f"{item[4]}-{hashes[index][:suffix_length]}"
            if index in collision_indexes
            else item[4]
            for index, item in enumerate(raw_documents)
        )
        if len(set(candidates)) == len(candidates):
            return candidates, len(collision_indexes)
    raise ValueError("could not derive unique topic IDs from source relative paths")


def _result(
    source: Path,
    target: Path,
    documents: tuple[SourceDocument, ...],
    *,
    collision_count: int,
    dry_run: bool,
) -> ImportResult:
    lengths = [document.chars for document in documents]
    return ImportResult(
        source_dir=source,
        data_root=target,
        topic_count=len(documents),
        total_bytes=sum(len(document.payload) for document in documents),
        total_chars=sum(lengths),
        total_lines=sum(document.lines for document in documents),
        min_chars=min(lengths),
        max_chars=max(lengths),
        median_chars=median(lengths),
        collision_count=collision_count,
        topic_ids=tuple(document.topic_id for document in documents),
        dry_run=dry_run,
    )


def _write_registry(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as registry_file:
        registry_file.write(payload)
        registry_file.flush()
        os.fsync(registry_file.fileno())


def _next_backup_path(target: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = target.with_name(f"{target.name}.backup-{timestamp}")
    if candidate.exists() or candidate.is_symlink():
        raise FileExistsError(f"rollback backup path already exists: {candidate}")
    return candidate


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
