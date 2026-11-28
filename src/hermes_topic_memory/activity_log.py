from __future__ import annotations

import fcntl
import json
import math
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping, get_args

ActivityAction = Literal[
    "recall", "remember", "dream", "bind_topic", "list_topics", "whoami",
    "topic_create", "topic_edit", "topic_append", "topic_rollback", "topic_archive",
    "topic_restore", "topic_trash", "topic_delete", "core_init", "core_edit",
    "core_rollback", "auth_failed", "sso_login", "sso_logout", "key_create", "key_revoke",
]
ActivitySource = Literal["mcp", "rest", "panel", "studio", "cli", "system"]
ActivityStatus = Literal["ok", "no_fact", "duplicate", "skipped", "error", "denied"]

_EVENT_ID = re.compile(r"^[0-9a-f]{32}$")
_TOPIC_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MONTH_FILE = re.compile(r"^\d{4}-\d{2}\.jsonl$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SECRET = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)"
    r"|\bBearer\s+\S+"
    r"|htm_[A-Za-z0-9_-]+"
    r"|\b(?:cookie|set-cookie)\s*[:=].*$"
    r"|\b(?:access[_-]?token|refresh[_-]?token|token|password|passwd|"
    r"authorization|api[_-]?key|secret|private[_-]?key)[\"']?\s*[:=]\s*"
    r"(?:\"[^\"]*(?:\"|$)|'[^']*(?:'|$)|[^\s,;]+)"
    r"|\b(?:sk-[A-Za-z0-9_-]{8,}|PRIVATE_[A-Za-z0-9_]+)"
    r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|(?<=://)[^\s/@:]+:[^\s/@]+@",
    re.IGNORECASE | re.DOTALL,
)
_META_KEYS = frozenset({
    "topic_id", "topic_ids", "topic_version", "topic_versions", "version",
    "previous_version", "target_version", "before_version", "after_version",
    "route_source", "cache", "source_agent", "status", "result_source",
    "query_chars", "fact_chars", "answer_chars", "body_chars", "size_chars",
    "before_chars", "after_chars", "topic_count", "error_count", "skipped_count",
    "no_fact_count", "consolidated_count", "evidence_count", "pruned_count",
    "merged_count", "conflicts_resolved", "dry_run", "force", "method", "path",
    "http_status", "error_code", "key_id",
})


class ActivityLogError(RuntimeError):
    """Base error for durable activity-log operations."""


class ActivityLogCorruptionError(ActivityLogError):
    """Raised when an activity record cannot be parsed safely."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("created_at must be an ISO-8601 UTC Z timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError("created_at must be an ISO-8601 UTC Z timestamp") from None
    if "T" not in value or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("created_at must be an ISO-8601 UTC Z timestamp")
    return parsed


def _topic(value: str) -> None:
    if not isinstance(value, str) or len(value) > 128 or not _TOPIC_ID.fullmatch(value):
        raise ValueError("invalid topic id")


def _integer(value: int, label: str, *, minimum: int = 0) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")


def _text(value: str, label: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or _CONTROL.search(value):
        raise ValueError(f"{label} must be safe text of at most {maximum} characters")
    if not empty and not value.strip():
        raise ValueError(f"{label} must not be empty")
    return _SECRET.sub("[redacted]", value)[:maximum]


def _check_json(value: Any, depth: int = 0) -> None:
    if depth > 3:
        raise ValueError("meta must be small JSON-safe data")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, Mapping) and len(value) <= 32:
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("meta keys must be text")
            _check_json(item, depth + 1)
        return
    if isinstance(value, (list, tuple)) and len(value) <= 8:
        for item in value:
            _check_json(item, depth + 1)
        return
    raise ValueError("meta must be small JSON-safe data")


def _metadata(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("meta must be a mapping")
    _check_json(value)
    if len(json.dumps(_thaw(value), ensure_ascii=False, allow_nan=False).encode("utf-8")) > 4096:
        raise ValueError("meta must not exceed 4096 bytes")
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        # Only operational attributes are retained, never arbitrary request data.
        if key not in _META_KEYS:
            continue
        if key == "topic_versions":
            if not isinstance(item, Mapping) or len(item) > 8:
                raise ValueError("topic_versions must map at most eight topic ids to versions")
            versions: dict[str, int] = {}
            for topic_id, version in item.items():
                _topic(topic_id)
                _integer(version, "topic version", minimum=1)
                versions[topic_id] = version
            sanitized[key] = MappingProxyType(versions)
        elif key == "topic_ids":
            if not isinstance(item, (list, tuple)) or len(item) > 8:
                raise ValueError("topic_ids must contain at most eight topic ids")
            for topic_id in item:
                _topic(topic_id)
            sanitized[key] = tuple(item)
        elif isinstance(item, str):
            sanitized[key] = _text(item, f"meta {key}", 160, empty=True)
        elif item is None or isinstance(item, (bool, int, float)):
            sanitized[key] = item
        else:
            raise ValueError("meta operational values must be scalars")
    return MappingProxyType(sanitized)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityRecord:
    action: ActivityAction
    source: ActivitySource
    actor: str = "anonymous"
    topic_id: str | None = None
    status: ActivityStatus = "ok"
    latency_ms: int = 0
    summary: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if self.action not in get_args(ActivityAction):
            raise ValueError("invalid activity action")
        if self.source not in get_args(ActivitySource):
            raise ValueError("invalid activity source")
        if self.status not in get_args(ActivityStatus):
            raise ValueError("invalid activity status")
        if not isinstance(self.event_id, str) or not _EVENT_ID.fullmatch(self.event_id):
            raise ValueError("event_id must be lowercase uuid hex")
        _timestamp(self.created_at)
        if self.topic_id is not None:
            _topic(self.topic_id)
        _integer(self.latency_ms, "latency_ms")
        actor = _text(self.actor, "actor", 128)
        # This is an identity label, not an access token.
        if self.actor == "token:env-admin":
            actor = self.actor
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "summary", _text(self.summary, "summary", 300, empty=True))
        object.__setattr__(self, "meta", _metadata(self.meta))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "created_at": self.created_at,
            "action": self.action, "source": self.source, "actor": self.actor,
            "topic_id": self.topic_id, "status": self.status,
            "latency_ms": self.latency_ms, "summary": self.summary, "meta": _thaw(self.meta),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ActivityRecord:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("invalid activity record fields")
        record = cls(**value)
        if record.to_dict() != value:
            raise ValueError("activity record is not sanitized")
        return record


class ActivityLedger:
    def __init__(self, data_root: str | Path) -> None:
        self.root = Path(data_root)
        self.activity_dir = self.root / "activity-log"
        if self.activity_dir.is_dir():
            self.activity_dir.chmod(0o700)

    def append(self, record: ActivityRecord) -> ActivityRecord:
        if not isinstance(record, ActivityRecord):
            raise TypeError("record must be an ActivityRecord")
        payload = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
        self.activity_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.activity_dir.chmod(0o700)
        path = self.activity_dir / f"{_timestamp(record.created_at):%Y-%m}.jsonl"
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                os.fchmod(descriptor, 0o600)
                if os.write(descriptor, payload) != len(payload):
                    raise OSError("incomplete activity record write")
                os.fsync(descriptor)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        _fsync_directory(self.activity_dir)
        _fsync_directory(self.root)
        return record

    def list(
        self, *, action: ActivityAction | None = None, source: ActivitySource | None = None,
        topic_id: str | None = None, status: ActivityStatus | None = None,
        q: str | None = None, page: int = 1, page_size: int = 50,
        allowed_topics: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        for value, choices, label in (
            (action, get_args(ActivityAction), "action"),
            (source, get_args(ActivitySource), "source"),
            (status, get_args(ActivityStatus), "status"),
        ):
            if value is not None and value not in choices:
                raise ValueError(f"invalid activity {label}")
        if topic_id is not None:
            _topic(topic_id)
        _integer(page, "page", minimum=1)
        _integer(page_size, "page_size", minimum=1)
        if page_size > 100:
            raise ValueError("page_size must not exceed 100")
        if q is not None and (not isinstance(q, str) or len(q) > 300 or _CONTROL.search(q)):
            raise ValueError("q must be safe text of at most 300 characters")
        needle = q.strip().casefold() if q else ""
        records = self._read_records()
        records.sort(key=lambda record: (_timestamp(record.created_at), record.event_id), reverse=True)
        items = []
        for record in records:
            if allowed_topics is not None:
                record_topics = set(record.meta.get("topic_ids", ()))
                if record.topic_id is not None:
                    record_topics.add(record.topic_id)
                if not record_topics and record.action in (
                    "recall", "remember", "dream", "bind_topic", "list_topics",
                ):
                    continue
                if not record_topics.issubset(allowed_topics):
                    continue
            if any(value is not None and getattr(record, key) != value for key, value in (
                ("action", action), ("source", source), ("topic_id", topic_id), ("status", status),
            )):
                continue
            item = record.to_dict()
            if needle and needle not in json.dumps(item, ensure_ascii=False).casefold():
                continue
            items.append(item)
        start = (page - 1) * page_size
        return {"items": items[start:start + page_size], "page": page,
                "page_size": page_size, "total": len(items)}

    def _read_records(self) -> list[ActivityRecord]:
        if not self.activity_dir.is_dir():
            return []
        records = []
        paths = sorted(path for path in self.activity_dir.iterdir()
                       if path.is_file() and _MONTH_FILE.fullmatch(path.name))
        for path in paths:
            with path.open("rb") as activity_file:
                fcntl.flock(activity_file.fileno(), fcntl.LOCK_SH)
                try:
                    os.fchmod(activity_file.fileno(), 0o600)
                    for line_number, raw_line in enumerate(activity_file, start=1):
                        if not raw_line.strip():
                            continue
                        try:
                            record = ActivityRecord.from_dict(json.loads(raw_line.decode("utf-8")))
                            if f"{_timestamp(record.created_at):%Y-%m}" != path.stem:
                                raise ValueError("record timestamp does not match month file")
                        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
                            raise ActivityLogCorruptionError(
                                f"invalid activity record {path.name}:{line_number}"
                            ) from None
                        records.append(record)
                finally:
                    fcntl.flock(activity_file.fileno(), fcntl.LOCK_UN)
        return records


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
