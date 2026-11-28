from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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


class TopicCatalog:
    def __init__(self, data_root: str | Path) -> None:
        self.data_root = Path(data_root).expanduser().resolve(strict=True)

    def snapshot(self, *, now: datetime | None = None) -> CatalogSnapshot:
        registry = TopicRegistry.from_file(self.data_root / "topic-registry.json")
        self._validate_topic_directories(registry)
        query_records = QueryHistoryStore(self.data_root).list()
        topic_question_counts: Counter[str] = Counter()
        for record in query_records:
            topic_question_counts.update(
                result.topic_id for result in record.selected_topics
            )

        beijing_now = _as_beijing(now or datetime.now(_BEIJING))
        last_day = beijing_now.date()
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
            if config.state == "trashed":
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
