from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .cache import QuestionCache
from .models import TopicMetadata
from .query_history import QueryHistoryStore
from .router import TopicConfig, TopicRegistry
from .store import TopicCorruptionError

_BEIJING = ZoneInfo("Asia/Shanghai")
_DAY_COUNT = 30
_CACHE_MAXSIZE = 32
_FilesystemSignature = tuple[tuple[str, int, int, int, int, int, int], ...]


@dataclass(frozen=True, slots=True)
class DailyQuestionCount:
    day: str
    question_count: int

    def to_public_dict(self) -> dict[str, Any]:
        return {"day": self.day, "question_count": self.question_count}


@dataclass(frozen=True, slots=True)
class TopicSummary:
    topic_id: str
    display_name: str
    aliases: tuple[str, ...]
    version: int
    updated_at: str
    status: Literal["active", "archived"]
    question_count: int
    active_cache_count: int
    source_relative_path: str
    chars: int
    bytes: int
    lines: int
    version_count: int
    imported_at: str | None
    source_sha256: str | None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "version": self.version,
            "updated_at": self.updated_at,
            "status": self.status,
            "question_count": self.question_count,
            "active_cache_count": self.active_cache_count,
            "chars": self.chars,
        }


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    active_topic_count: int
    total_question_count: int
    active_cache_count: int
    questions_last_30_days: int
    total_chars: int
    daily_questions: tuple[DailyQuestionCount, ...]
    topics: tuple[TopicSummary, ...]

    def topic(self, topic_id: str) -> TopicSummary:
        for topic in self.topics:
            if topic.topic_id == topic_id:
                return topic
        raise KeyError(topic_id)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "active_topic_count": self.active_topic_count,
            "total_question_count": self.total_question_count,
            "active_cache_count": self.active_cache_count,
            "questions_last_30_days": self.questions_last_30_days,
            "total_chars": self.total_chars,
            "daily_questions": [item.to_public_dict() for item in self.daily_questions],
            "topics": [topic.to_public_dict() for topic in self.topics],
        }


@dataclass(slots=True)
class _SnapshotCacheEntry:
    lock: threading.RLock = field(default_factory=threading.RLock)
    signature: _FilesystemSignature | None = None
    registry: TopicRegistry | None = None
    snapshots: OrderedDict[frozenset[str] | None, CatalogSnapshot] = field(
        default_factory=OrderedDict
    )


_CACHE_ENTRY_LOCK = threading.Lock()


@lru_cache(maxsize=_CACHE_MAXSIZE)
def _snapshot_cache_entry(data_root: Path, day: date) -> _SnapshotCacheEntry:
    return _SnapshotCacheEntry()


class TopicCatalog:
    def __init__(self, data_root: str | Path) -> None:
        self.data_root = Path(data_root).expanduser().resolve(strict=True)

    def snapshot(
        self, *, now: datetime | None = None, allowed_topics: frozenset[str] | None = None
    ) -> CatalogSnapshot:
        last_day = _as_beijing(now or datetime.now(_BEIJING)).date()
        # Serialize entry creation too: lru_cache may call a cold factory twice
        # when concurrent callers arrive before the first result is stored.
        with _CACHE_ENTRY_LOCK:
            entry = _snapshot_cache_entry(self.data_root, last_day)
        with entry.lock:
            while True:
                before = _filesystem_signature(self.data_root)
                if entry.signature == before and allowed_topics in entry.snapshots:
                    assert entry.registry is not None
                    self._validate_topic_directories(entry.registry)
                    if _filesystem_signature(self.data_root) == before:
                        entry.snapshots.move_to_end(allowed_topics)
                        return entry.snapshots[allowed_topics]
                    continue

                registry = TopicRegistry.from_file(self.data_root / "topic-registry.json")
                self._validate_topic_directories(registry)
                snapshot = self._build_snapshot(registry, last_day, allowed_topics)
                if _filesystem_signature(self.data_root) != before:
                    # Do not associate a read of the old tree with a new signature.
                    continue
                if entry.signature != before:
                    entry.snapshots.clear()
                entry.signature = before
                entry.registry = registry
                entry.snapshots[allowed_topics] = snapshot
                entry.snapshots.move_to_end(allowed_topics)
                if len(entry.snapshots) > _CACHE_MAXSIZE:
                    entry.snapshots.popitem(last=False)
                return snapshot

    def _build_snapshot(
        self,
        registry: TopicRegistry,
        last_day: date,
        allowed_topics: frozenset[str] | None,
    ) -> CatalogSnapshot:
        query_records = QueryHistoryStore(self.data_root).list()
        if allowed_topics is not None:
            query_records = [
                record for record in query_records
                if record.selected_topics
                and all(result.topic_id in allowed_topics for result in record.selected_topics)
            ]
        topic_question_counts: Counter[str] = Counter()
        for record in query_records:
            topic_question_counts.update(
                result.topic_id for result in record.selected_topics
            )

        first_day = last_day - timedelta(days=_DAY_COUNT - 1)
        daily_counts: Counter[date] = Counter()
        for record in query_records:
            query_day = _parse_utc_timestamp(record.created_at).astimezone(_BEIJING).date()
            if first_day <= query_day <= last_day:
                daily_counts[query_day] += 1
        daily_questions = tuple(
            DailyQuestionCount(
                day=(first_day + timedelta(days=offset)).isoformat(),
                question_count=daily_counts[first_day + timedelta(days=offset)],
            )
            for offset in range(_DAY_COUNT)
        )

        cache = QuestionCache(self.data_root)
        topics: list[TopicSummary] = []
        for config in registry.topics:
            if config.state == "trashed" or (
                allowed_topics is not None and config.topic_id not in allowed_topics
            ):
                continue
            metadata = self._read_metadata(config.topic_id)
            if metadata.state != config.state:
                raise TopicCorruptionError(
                    f"registry and metadata state differ: {config.topic_id}"
                )

            current_cache_count = sum(
                record.topic_version == metadata.version
                for record in cache.records(config.topic_id)
            )
            chars, byte_count, lines = self._canonical_size(metadata)
            topics.append(
                TopicSummary(
                    topic_id=config.topic_id,
                    display_name=config.display_name,
                    aliases=config.aliases,
                    version=metadata.version,
                    updated_at=metadata.updated_at,
                    status=config.state,
                    question_count=topic_question_counts[config.topic_id],
                    active_cache_count=current_cache_count,
                    source_relative_path=_safe_source_relative_path(config, metadata),
                    chars=chars,
                    bytes=byte_count,
                    lines=lines,
                    version_count=self._version_count(metadata),
                    imported_at=metadata.imported_at,
                    source_sha256=metadata.source_sha256,
                )
            )

        active_cache_count = sum(topic.active_cache_count for topic in topics)
        return CatalogSnapshot(
            active_topic_count=sum(topic.status == "active" for topic in topics),
            total_question_count=len(query_records),
            active_cache_count=active_cache_count,
            questions_last_30_days=sum(item.question_count for item in daily_questions),
            total_chars=sum(topic.chars for topic in topics),
            daily_questions=daily_questions,
            topics=tuple(topics),
        )

    def _validate_topic_directories(self, registry: TopicRegistry) -> None:
        registered = {
            topic.topic_id for topic in registry.topics if topic.state != "trashed"
        }
        topics_dir = self.data_root / "topics"
        actual = (
            {
                path.name
                for path in topics_dir.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            }
            if topics_dir.is_dir()
            else set()
        )
        if actual != registered:
            raise ValueError("registry and canonical topic directories differ")

    def _read_metadata(self, topic_id: str) -> TopicMetadata:
        path = self.data_root / "topics" / topic_id / "metadata.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise TypeError("metadata root must be an object")
            metadata = TopicMetadata.from_dict(value)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise TopicCorruptionError(f"invalid metadata for topic: {topic_id}") from error
        if metadata.topic_id != topic_id:
            raise TopicCorruptionError(f"metadata topic mismatch: {topic_id}")
        return metadata

    def _canonical_size(self, metadata: TopicMetadata) -> tuple[int, int, int]:
        recorded_sizes = (
            metadata.source_chars,
            metadata.source_bytes,
            metadata.source_lines,
        )
        if (
            metadata.source_sha256 == metadata.content_fingerprint
            and all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in recorded_sizes
            )
        ):
            chars, byte_count, lines = recorded_sizes
            return int(chars), int(byte_count), int(lines)

        path = self.data_root / "topics" / metadata.topic_id / "memory.md"
        try:
            payload = path.read_bytes()
            body = payload.decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as error:
            raise TopicCorruptionError(
                f"invalid canonical body for topic: {metadata.topic_id}"
            ) from error
        if hashlib.sha256(payload).hexdigest() != metadata.content_fingerprint:
            raise TopicCorruptionError(
                f"metadata does not match topic body: {metadata.topic_id}"
            )
        return len(body), len(payload), len(body.splitlines())

    def _version_count(self, metadata: TopicMetadata) -> int:
        versions_dir = self.data_root / "topics" / metadata.topic_id / "versions"
        versions = {
            int(path.name.removesuffix("-memory.md"))
            for path in versions_dir.glob("*-memory.md")
            if path.name.removesuffix("-memory.md").isdigit()
        }
        versions.add(metadata.version)
        return len(versions)


def _filesystem_signature(data_root: Path) -> _FilesystemSignature:
    """Stat-only identity of every input, including missing paths and shapes."""
    entries: list[tuple[str, int, int, int, int, int, int]] = []

    def record(path: Path) -> os.stat_result | None:
        relative = path.relative_to(data_root).as_posix()
        try:
            value = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            entries.append((relative, 0, 0, 0, 0, 0, 0))
            return None
        entries.append(
            (
                relative,
                stat.S_IFMT(value.st_mode),
                value.st_mtime_ns,
                value.st_size,
                value.st_ctime_ns,
                value.st_dev,
                value.st_ino,
            )
        )
        return value

    def children(path: Path) -> list[Path]:
        try:
            with os.scandir(path) as directory:
                return [path / name for name in sorted(item.name for item in directory)]
        except (FileNotFoundError, NotADirectoryError):
            return []

    def tree(path: Path, ancestors: frozenset[tuple[int, int]]) -> None:
        value = record(path)
        if value is None or not stat.S_ISDIR(value.st_mode):
            return
        identity = (value.st_dev, value.st_ino)
        if identity in ancestors:
            return
        ancestors = ancestors | {identity}
        for child in children(path):
            tree(child, ancestors)

    record(data_root / "topic-registry.json")
    topics_dir = data_root / "topics"
    topics_stat = record(topics_dir)
    if topics_stat is not None and stat.S_ISDIR(topics_stat.st_mode):
        for topic_dir in children(topics_dir):
            value = record(topic_dir)
            if value is not None and stat.S_ISDIR(value.st_mode):
                for name in ("metadata.json", "memory.md", "versions"):
                    record(topic_dir / name)
    # QuestionCache uses qa-cache; also track the conventional cache directory
    # and nested entries so additions/deletions cannot hide behind parent stats.
    for name in ("query-history", "qa-cache", "cache"):
        tree(data_root / name, frozenset())
    return tuple(entries)


def _safe_source_relative_path(
    config: TopicConfig,
    metadata: TopicMetadata,
) -> str:
    value = config.source_relative_path or metadata.source_relative_path
    if not value:
        return "未记录"
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return "未记录"
    return value


def _as_beijing(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return value.astimezone(_BEIJING)


def _parse_utc_timestamp(value: str) -> datetime:
    suffix = "+00:00" if value.endswith("Z") else ""
    parsed = datetime.fromisoformat(value.removesuffix("Z") + suffix)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("query timestamp must use UTC")
    return parsed.astimezone(UTC)
