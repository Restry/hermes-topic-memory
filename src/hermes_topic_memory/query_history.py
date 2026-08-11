from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

_QUERY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOPIC_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SESSION_HASH = re.compile(r"^[0-9a-f]{64}$")
_MONTH_FILE = re.compile(r"^\d{4}-\d{2}\.jsonl$")
_MAX_QUESTION_CHARS = 4_096
_MAX_ANSWER_CHARS = 200
_MAX_REASON_CHARS = 500
_MAX_MATCH_KIND_CHARS = 64
_MAX_SESSION_REF_CHARS = 1_024
_MAX_SELECTED_TOPICS = 8

QueryStatus = Literal["answered", "no_fact", "error"]
TopicResultSource = Literal["cache", "reader", "failure"]
QuerySource = Literal["hermes", "panel", "cli"]


class QueryHistoryError(RuntimeError):
    """Base error for durable query-history operations."""


class QueryHistoryCorruptionError(QueryHistoryError):
    """Raised when a query-history JSONL record cannot be parsed safely."""


@dataclass(frozen=True, slots=True)
class TopicQueryResult:
    topic_id: str
    topic_version: int
    status: QueryStatus
    answer: str | None
    source: TopicResultSource
    match_kind: str | None
    latency_ms: int

    def __post_init__(self) -> None:
        _validate_topic_id(self.topic_id)
        _validate_positive_int(self.topic_version, "topic version")
        if self.status not in {"answered", "no_fact", "error"}:
            raise ValueError(f"invalid query result status: {self.status!r}")
        if self.answer is not None:
            _validate_bounded_text(
                self.answer,
                "answer",
                maximum=_MAX_ANSWER_CHARS,
            )
        elif self.status == "answered":
            raise ValueError("answer must contain 1 to 200 characters")
        if self.source not in {"cache", "reader", "failure"}:
            raise ValueError(f"invalid query result source: {self.source!r}")
        if self.match_kind is not None:
            _validate_bounded_text(
                self.match_kind,
                "match kind",
                maximum=_MAX_MATCH_KIND_CHARS,
            )
        _validate_nonnegative_int(self.latency_ms, "latency")

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "topic_version": self.topic_version,
            "status": self.status,
            "answer": self.answer,
            "source": self.source,
            "match_kind": self.match_kind,
            "latency_ms": self.latency_ms,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TopicQueryResult:
        if not isinstance(value, Mapping):
            raise TypeError("topic query result must be an object")
        return cls(
            topic_id=value["topic_id"],
            topic_version=value["topic_version"],
            status=value["status"],
            answer=value.get("answer"),
            source=value["source"],
            match_kind=value.get("match_kind"),
            latency_ms=value["latency_ms"],
        )


@dataclass(frozen=True, slots=True)
class QueryRecord:
    query_id: str
    question: str
    created_at: str
    selected_topics: tuple[TopicQueryResult, ...]
    final_answer: str | None
    selection_reasons: Mapping[str, str]
    source: QuerySource
    session_ref: str | None
    total_latency_ms: int

    def __post_init__(self) -> None:
        _validate_query_id(self.query_id)
        _validate_bounded_text(
            self.question,
            "question",
            maximum=_MAX_QUESTION_CHARS,
        )
        _parse_utc_timestamp(self.created_at)

        selected_topics = tuple(self.selected_topics)
        if not 1 <= len(selected_topics) <= _MAX_SELECTED_TOPICS:
            raise ValueError("selected topics must contain 1 to 8 items")
        if any(not isinstance(item, TopicQueryResult) for item in selected_topics):
            raise TypeError("selected topics must contain topic query results")
        topic_ids = tuple(item.topic_id for item in selected_topics)
        if len(set(topic_ids)) != len(topic_ids):
            raise ValueError("duplicate topic id in selected topics")
        object.__setattr__(self, "selected_topics", selected_topics)

        if self.final_answer is not None:
            _validate_bounded_text(
                self.final_answer,
                "final answer",
                maximum=_MAX_ANSWER_CHARS,
            )
        if not isinstance(self.selection_reasons, Mapping):
            raise TypeError("selection reasons must be an object")
        reasons = dict(self.selection_reasons)
        if set(reasons) != set(topic_ids):
            raise ValueError("selection reasons must match selected topic ids")
        for topic_id, reason in reasons.items():
            if not isinstance(topic_id, str):
                raise TypeError("selection reason topic ids must be text")
            _validate_bounded_text(
                reason,
                "selection reason",
                maximum=_MAX_REASON_CHARS,
            )
        object.__setattr__(self, "selection_reasons", MappingProxyType(reasons))

        if self.source not in {"hermes", "panel", "cli"}:
            raise ValueError(f"invalid query source: {self.source!r}")
        if self.session_ref is not None:
            _validate_bounded_text(
                self.session_ref,
                "session reference",
                maximum=_MAX_SESSION_REF_CHARS,
            )
        _validate_nonnegative_int(self.total_latency_ms, "total latency")

    @classmethod
    def create(
        cls,
        *,
        question: str,
        selected_topics: tuple[TopicQueryResult, ...],
        final_answer: str | None,
        selection_reasons: Mapping[str, str],
        source: QuerySource,
        session_ref: str | None,
        total_latency_ms: int,
        query_id: str | None = None,
        created_at: datetime | None = None,
    ) -> QueryRecord:
        timestamp = created_at or datetime.now(UTC)
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise ValueError("created_at must be a timezone-aware datetime")
        timestamp = timestamp.astimezone(UTC)
        hashed_session_ref: str | None = None
        if session_ref is not None:
            _validate_bounded_text(
                session_ref,
                "session reference",
                maximum=_MAX_SESSION_REF_CHARS,
            )
            hashed_session_ref = hashlib.sha256(session_ref.encode("utf-8")).hexdigest()
        return cls(
            query_id=query_id or uuid.uuid4().hex,
            question=question,
            created_at=_format_utc_timestamp(timestamp),
            selected_topics=selected_topics,
            final_answer=final_answer,
            selection_reasons=selection_reasons,
            source=source,
            session_ref=hashed_session_ref,
            total_latency_ms=total_latency_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "question": self.question,
            "created_at": self.created_at,
            "selected_topics": [item.to_dict() for item in self.selected_topics],
            "final_answer": self.final_answer,
            "selection_reasons": dict(self.selection_reasons),
            "source": self.source,
            "session_ref": self.session_ref,
            "total_latency_ms": self.total_latency_ms,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> QueryRecord:
        if not isinstance(value, Mapping):
            raise TypeError("query record must be an object")
        raw_topics = value["selected_topics"]
        if not isinstance(raw_topics, (list, tuple)):
            raise TypeError("selected topics must be an array")
        record = cls(
            query_id=value["query_id"],
            question=value["question"],
            created_at=value["created_at"],
            selected_topics=tuple(TopicQueryResult.from_dict(item) for item in raw_topics),
            final_answer=value.get("final_answer"),
            selection_reasons=value["selection_reasons"],
            source=value["source"],
            session_ref=value.get("session_ref"),
            total_latency_ms=value["total_latency_ms"],
        )
        _require_hashed_session(record.session_ref)
        return record


class QueryHistoryStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.history_dir = self.root / "query-history"

    def append(self, record: QueryRecord) -> QueryRecord:
        if not isinstance(record, QueryRecord):
            raise TypeError("record must be a QueryRecord")
        _require_hashed_session(record.session_ref)
        payload = json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        self.history_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._month_path(record.created_at)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                written = os.write(descriptor, payload)
                if written != len(payload):
                    raise OSError("incomplete query history record write")
                os.fsync(descriptor)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        _fsync_directory(self.history_dir)
        return record

    def get(self, query_id: str) -> QueryRecord | None:
        _validate_query_id(query_id)
        for record in self.list():
            if record.query_id == query_id:
                return record
        return None

    def list(
        self,
        *,
        topic_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[QueryRecord, ...]:
        if topic_id is not None:
            _validate_topic_id(topic_id)
        _validate_nonnegative_int(offset, "offset")
        if limit is not None:
            _validate_nonnegative_int(limit, "limit")

        records = self._read_records()
        records.sort(
            key=lambda record: (_parse_utc_timestamp(record.created_at), record.query_id),
            reverse=True,
        )
        if topic_id is not None:
            records = [
                record
                for record in records
                if any(item.topic_id == topic_id for item in record.selected_topics)
            ]
        stop = None if limit is None else offset + limit
        return tuple(records[offset:stop])

    def _read_records(self) -> list[QueryRecord]:
        if not self.history_dir.is_dir():
            return []
        records: list[QueryRecord] = []
        paths = sorted(
            (
                path
                for path in self.history_dir.iterdir()
                if path.is_file() and _MONTH_FILE.fullmatch(path.name)
            ),
            reverse=True,
        )
        for path in paths:
            with path.open("rb") as history_file:
                fcntl.flock(history_file.fileno(), fcntl.LOCK_SH)
                try:
                    for line_number, raw_line in enumerate(history_file, start=1):
                        if not raw_line.strip():
                            continue
                        try:
                            value = json.loads(raw_line.decode("utf-8"))
                            record = QueryRecord.from_dict(value)
                            if self._month_key(record.created_at) != path.stem:
                                raise ValueError("record timestamp does not match month file")
                        except (
                            KeyError,
                            TypeError,
                            ValueError,
                            json.JSONDecodeError,
                            UnicodeDecodeError,
                        ):
                            raise QueryHistoryCorruptionError(
                                f"invalid query history record {path.name}:{line_number}"
                            ) from None
                        records.append(record)
                finally:
                    fcntl.flock(history_file.fileno(), fcntl.LOCK_UN)
        return records

    def _month_path(self, created_at: str) -> Path:
        return self.history_dir / f"{self._month_key(created_at)}.jsonl"

    @staticmethod
    def _month_key(created_at: str) -> str:
        return _parse_utc_timestamp(created_at).strftime("%Y-%m")


def _validate_query_id(query_id: str) -> None:
    if not isinstance(query_id, str) or not _QUERY_ID.fullmatch(query_id):
        raise ValueError(f"invalid query id: {query_id!r}")


def _validate_topic_id(topic_id: str) -> None:
    if not isinstance(topic_id, str) or not _TOPIC_ID.fullmatch(topic_id):
        raise ValueError(f"invalid topic id: {topic_id!r}")


def _validate_bounded_text(value: str, label: str, *, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must contain 1 to {maximum} characters")


def _validate_positive_int(value: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{label} must be a positive integer")


def _validate_nonnegative_int(value: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def _require_hashed_session(session_ref: str | None) -> None:
    if session_ref is not None and not _SESSION_HASH.fullmatch(session_ref):
        raise ValueError("session reference must be hashed before persistence")


def _parse_utc_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError("created_at must be text")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    except ValueError:
        raise ValueError("created_at must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("created_at must use UTC")
    return parsed.astimezone(UTC)


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
