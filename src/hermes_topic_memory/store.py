from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .models import TopicMemory, TopicMetadata

_TOPIC_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class TopicStoreError(RuntimeError):
    """Base error for topic storage failures."""


class TopicNotFoundError(TopicStoreError):
    """Raised when a topic does not exist."""


class TopicExistsError(TopicStoreError):
    """Raised when a topic already exists."""


class TopicCorruptionError(TopicStoreError):
    """Raised when persisted metadata does not match canonical text."""


class FileTopicStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.topics_dir = self.root / "topics"

    def create_topic(
        self,
        topic_id: str,
        body: str,
        *,
        display_name: str | None = None,
        aliases: tuple[str, ...] = (),
        source_policy: str = "verified-evidence-only",
        source_relative_path: str | None = None,
        source_sha256: str | None = None,
        source_bytes: int | None = None,
        source_chars: int | None = None,
        source_lines: int | None = None,
        imported_at: str | None = None,
    ) -> TopicMemory:
        self._validate_topic_id(topic_id)
        self._validate_body(body)
        topic_dir = self._topic_dir(topic_id)
        topic_dir.mkdir(parents=True, exist_ok=True)
        (topic_dir / "versions").mkdir(exist_ok=True)
        with self._lock(topic_id):
            if self._memory_path(topic_id).exists() or self._metadata_path(topic_id).exists():
                raise TopicExistsError(f"topic already exists: {topic_id}")
            now = _utc_now()
            metadata = TopicMetadata(
                topic_id=topic_id,
                display_name=display_name or topic_id,
                aliases=tuple(aliases),
                version=1,
                content_fingerprint=_fingerprint(body),
                created_at=now,
                updated_at=now,
                source_policy=source_policy,
                source_relative_path=source_relative_path,
                source_sha256=source_sha256,
                source_bytes=source_bytes,
                source_chars=source_chars,
                source_lines=source_lines,
                imported_at=imported_at,
            )
            self._atomic_write(self._memory_path(topic_id), body.encode("utf-8"))
            self._write_metadata(topic_id, metadata)
            return TopicMemory(metadata=metadata, body=body)

    def get_topic(self, topic_id: str) -> TopicMemory:
        self._validate_topic_id(topic_id)
        with self._lock(topic_id, require_existing=True):
            return self._read_unlocked(topic_id)

    def update_topic(self, topic_id: str, body: str) -> TopicMemory:
        self._validate_topic_id(topic_id)
        self._validate_body(body)
        with self._lock(topic_id, require_existing=True):
            current = self._read_unlocked(topic_id)
            if current.body == body:
                raise ValueError("replacement body must differ from current body")
            history_path = self._version_path(topic_id, current.metadata.version)
            if history_path.exists():
                if history_path.read_bytes() != current.body.encode("utf-8"):
                    raise TopicCorruptionError("history version conflicts with current body")
            else:
                self._atomic_write(history_path, current.body.encode("utf-8"))
            metadata = replace(
                current.metadata,
                version=current.metadata.version + 1,
                content_fingerprint=_fingerprint(body),
                updated_at=_utc_now(),
            )
            self._atomic_write(self._memory_path(topic_id), body.encode("utf-8"))
            self._write_metadata(topic_id, metadata)
            return TopicMemory(metadata=metadata, body=body)

    def read_version(self, topic_id: str, version: int) -> str:
        if version < 1:
            raise ValueError("version must be positive")
        current = self.get_topic(topic_id)
        if version == current.metadata.version:
            return current.body
        path = self._version_path(topic_id, version)
        if not path.is_file():
            raise TopicNotFoundError(f"topic version does not exist: {topic_id}@{version}")
        return path.read_bytes().decode("utf-8", errors="strict")

    def list_versions(self, topic_id: str) -> tuple[int, ...]:
        current = self.get_topic(topic_id)
        versions = {
            int(path.name.removesuffix("-memory.md"))
            for path in self._versions_dir(topic_id).glob("*-memory.md")
            if path.name.removesuffix("-memory.md").isdigit()
        }
        versions.add(current.metadata.version)
        return tuple(sorted(versions))

    def rollback_topic(self, topic_id: str, target_version: int) -> TopicMemory:
        historical_body = self.read_version(topic_id, target_version)
        return self.update_topic(topic_id, historical_body)

    def _read_unlocked(self, topic_id: str) -> TopicMemory:
        memory_path = self._memory_path(topic_id)
        metadata_path = self._metadata_path(topic_id)
        if not memory_path.is_file() or not metadata_path.is_file():
            raise TopicNotFoundError(f"topic does not exist: {topic_id}")
        body = memory_path.read_bytes().decode("utf-8", errors="strict")
        try:
            raw_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata = TopicMetadata.from_dict(raw_metadata)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise TopicCorruptionError(f"invalid metadata for topic: {topic_id}") from error
        if metadata.topic_id != topic_id or metadata.content_fingerprint != _fingerprint(body):
            raise TopicCorruptionError(f"metadata does not match topic body: {topic_id}")
        return TopicMemory(metadata=metadata, body=body)

    def _write_metadata(self, topic_id: str, metadata: TopicMetadata) -> None:
        payload = json.dumps(
            metadata.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + "\n"
        self._atomic_write(self._metadata_path(topic_id), payload.encode("utf-8"))

    @contextmanager
    def _lock(self, topic_id: str, *, require_existing: bool = False) -> Iterator[None]:
        topic_dir = self._topic_dir(topic_id)
        if require_existing and not topic_dir.is_dir():
            raise TopicNotFoundError(f"topic does not exist: {topic_id}")
        topic_dir.mkdir(parents=True, exist_ok=True)
        lock_path = topic_dir / ".lock"
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(payload)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    def _topic_dir(self, topic_id: str) -> Path:
        return self.topics_dir / topic_id

    def _memory_path(self, topic_id: str) -> Path:
        return self._topic_dir(topic_id) / "memory.md"

    def _metadata_path(self, topic_id: str) -> Path:
        return self._topic_dir(topic_id) / "metadata.json"

    def _versions_dir(self, topic_id: str) -> Path:
        return self._topic_dir(topic_id) / "versions"

    def _version_path(self, topic_id: str, version: int) -> Path:
        return self._versions_dir(topic_id) / f"{version}-memory.md"

    @staticmethod
    def _validate_topic_id(topic_id: str) -> None:
        if not _TOPIC_ID.fullmatch(topic_id):
            raise ValueError(f"invalid topic id: {topic_id!r}")

    @staticmethod
    def _validate_body(body: str) -> None:
        if not isinstance(body, str) or not body:
            raise ValueError("topic body must be non-empty text")


def _fingerprint(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
