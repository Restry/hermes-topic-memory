from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping


ApiKeyRole = Literal["read", "write", "admin"]
ApiKeyStatus = Literal["active", "revoked", "expired"]

_KEY_ID = re.compile(r"key_[0-9a-f]{12}")
_SECRET = re.compile(r"htm_[A-Za-z0-9_-]{32}")
_PREFIX = re.compile(r"htm_[A-Za-z0-9_-]{8}")
_HASH = re.compile(r"[0-9a-f]{64}")
_TOPIC_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")


class ApiKeyError(RuntimeError):
    """An API key store cannot be accessed safely."""


class ApiKeyCorruptionError(ApiKeyError):
    """An API key store contains invalid data."""


class ApiKeyNotFoundError(LookupError):
    """The requested API key does not exist."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError("timestamps must be ISO-8601 UTC Z timestamps")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError("timestamps must be ISO-8601 UTC Z timestamps") from None


def _safe_text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for char in value
    ):
        raise ValueError(f"{label} must be safe single-line text")
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= maximum:
        raise ValueError(f"{label} must contain 1..{maximum} characters")
    return cleaned


def _topics(value: tuple[str, ...] | list[str] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (tuple, list)) or not value:
        raise ValueError("allowed_topics must be a non-empty list of topic ids or null")
    for topic_id in value:
        if not isinstance(topic_id, str) or len(topic_id) > 128 or not _TOPIC_ID.fullmatch(topic_id):
            raise ValueError("invalid topic id")
    if len(set(value)) != len(value):
        raise ValueError("allowed_topics must contain unique topic ids")
    return tuple(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiKeyRecord:
    key_id: str
    name: str
    prefix: str
    key_hash: str = field(repr=False)
    role: ApiKeyRole
    allowed_topics: tuple[str, ...] | None
    created_at: str
    created_by: str
    expires_at: str | None = None
    last_used_at: str | None = None
    revoked_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key_id, str) or not _KEY_ID.fullmatch(self.key_id):
            raise ValueError("invalid key id")
        if not isinstance(self.prefix, str) or not _PREFIX.fullmatch(self.prefix):
            raise ValueError("invalid key prefix")
        if not isinstance(self.key_hash, str) or not _HASH.fullmatch(self.key_hash):
            raise ValueError("invalid key hash")
        if not isinstance(self.role, str) or self.role not in ("read", "write", "admin"):
            raise ValueError("invalid key role")
        object.__setattr__(self, "name", _safe_text(self.name, "name", 80))
        object.__setattr__(self, "created_by", _safe_text(self.created_by, "created_by", 128))
        object.__setattr__(self, "allowed_topics", _topics(self.allowed_topics))
        created = _timestamp(self.created_at)
        for value in (self.expires_at, self.last_used_at, self.revoked_at):
            if value is not None and _timestamp(value) < created:
                raise ValueError("key timestamps must not precede creation")
        if self.expires_at is not None and _timestamp(self.expires_at) == created:
            raise ValueError("expiry must follow creation")
        if self.last_used_at is not None and self.revoked_at is not None:
            if _timestamp(self.last_used_at) > _timestamp(self.revoked_at):
                raise ValueError("last use must not follow revocation")

    def _status_at(self, now: datetime) -> ApiKeyStatus:
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at is not None and _timestamp(self.expires_at) <= now:
            return "expired"
        return "active"

    @property
    def status(self) -> ApiKeyStatus:
        return self._status_at(_utc_now())

    def to_dict(self) -> dict[str, Any]:
        """Public metadata; the digest and one-time secret are never exposed."""
        return {
            "key_id": self.key_id, "name": self.name, "prefix": self.prefix,
            "role": self.role,
            "allowed_topics": list(self.allowed_topics) if self.allowed_topics is not None else None,
            "created_at": self.created_at, "created_by": self.created_by,
            "expires_at": self.expires_at, "last_used_at": self.last_used_at,
            "revoked_at": self.revoked_at, "status": self.status,
        }

    def _storage_dict(self) -> dict[str, Any]:
        # Keep durable serialization separate from the public response shape.
        return {
            "key_id": self.key_id, "name": self.name, "prefix": self.prefix,
            "key_hash": self.key_hash, "role": self.role,
            "allowed_topics": list(self.allowed_topics) if self.allowed_topics is not None else None,
            "created_at": self.created_at, "created_by": self.created_by,
            "expires_at": self.expires_at, "last_used_at": self.last_used_at,
            "revoked_at": self.revoked_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ApiKeyRecord:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("invalid API key record fields")
        if value["allowed_topics"] is not None and not isinstance(value["allowed_topics"], list):
            raise ValueError("invalid stored topic scope")
        record = cls(**value)
        if record._storage_dict() != value:
            raise ValueError("API key record is not sanitized")
        return record


class ApiKeyStore:
    def __init__(self, data_root: str | Path) -> None:
        self.root = Path(data_root)
        self.path = self.root / "api-keys.json"
        self.lock_path = self.root / ".api-keys.lock"
        with self._lock():
            try:
                self._read_unlocked()
            except FileNotFoundError:
                self._write_unlocked([])

    def create_key(
        self, *, name: str, role: ApiKeyRole, created_by: str,
        allowed_topics: tuple[str, ...] | list[str] | None = None,
        expires_in_days: int | None = None,
    ) -> tuple[ApiKeyRecord, str]:
        name = _safe_text(name, "name", 80)
        created_by = _safe_text(created_by, "created_by", 128)
        if not isinstance(role, str) or role not in ("read", "write", "admin"):
            raise ValueError("invalid key role")
        topics = _topics(allowed_topics)
        if expires_in_days is not None and (
            not isinstance(expires_in_days, int) or isinstance(expires_in_days, bool)
            or expires_in_days <= 0
        ):
            raise ValueError("expires_in_days must be a positive integer or null")
        now = _utc_now()
        try:
            expiry = now + timedelta(days=expires_in_days) if expires_in_days is not None else None
        except OverflowError:
            raise ValueError("expires_in_days is too large") from None
        with self._lock():
            records = self._read_unlocked()
            while True:
                secret = f"htm_{secrets.token_urlsafe(24)}"
                key_id = f"key_{uuid.uuid4().hex[:12]}"
                key_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
                if not any(record.key_id == key_id or record.key_hash == key_hash for record in records):
                    break
            record = ApiKeyRecord(
                key_id=key_id, name=name, prefix=secret[:12], key_hash=key_hash,
                role=role, allowed_topics=topics, created_at=_format_timestamp(now),
                created_by=created_by,
                expires_at=_format_timestamp(expiry) if expiry is not None else None,
            )
            records.append(record)
            self._write_unlocked(records)
        return record, secret

    def list_keys(self) -> list[ApiKeyRecord]:
        with self._lock():
            return self._read_unlocked()

    def authenticate(self, token: str) -> ApiKeyRecord | None:
        if not isinstance(token, str) or not _SECRET.fullmatch(token):
            return None
        key_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock():
            records = self._read_unlocked()
            match: int | None = None
            # Compare every digest, without a prefix filter or an early match exit.
            for index, record in enumerate(records):
                if hmac.compare_digest(record.key_hash, key_hash):
                    match = index
            if match is None:
                return None
            record = records[match]
            now = _utc_now()
            if record._status_at(now) != "active":
                return None
            if record.last_used_at is None or now - _timestamp(record.last_used_at) >= timedelta(seconds=60):
                record = replace(record, last_used_at=_format_timestamp(now))
                records[match] = record
                self._write_unlocked(records)
            return record

    def revoke_key(self, key_id: str) -> ApiKeyRecord:
        if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
            raise ValueError("invalid key id")
        with self._lock():
            records = self._read_unlocked()
            for index, record in enumerate(records):
                if record.key_id == key_id:
                    if record.revoked_at is None:
                        record = replace(record, revoked_at=_format_timestamp(_utc_now()))
                        records[index] = record
                        self._write_unlocked(records)
                    return record
        raise ApiKeyNotFoundError("API key not found")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ApiKeyError("API key store is unavailable")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    os.fchmod(descriptor, 0o600)
                    yield
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        except OSError:
            raise ApiKeyError("API key store is unavailable") from None

    def _read_unlocked(self) -> list[ApiKeyRecord]:
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ApiKeyCorruptionError("API key store is invalid")
            os.fchmod(source.fileno(), 0o600)
            try:
                data = json.load(source, object_pairs_hook=_unique_object)
                if not isinstance(data, dict) or set(data) != {"version", "keys"}:
                    raise ValueError("invalid store fields")
                if type(data["version"]) is not int or data["version"] != 1 or not isinstance(data["keys"], list):
                    raise ValueError("invalid store version or keys")
                records = [ApiKeyRecord.from_dict(item) for item in data["keys"]]
                if len({record.key_id for record in records}) != len(records):
                    raise ValueError("duplicate key ids")
                if len({record.key_hash for record in records}) != len(records):
                    raise ValueError("duplicate key hashes")
                return records
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise ApiKeyCorruptionError("API key store is invalid") from None

    def _write_unlocked(self, records: list[ApiKeyRecord]) -> None:
        payload = json.dumps(
            {"version": 1, "keys": [record._storage_dict() for record in records]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8") + b"\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".api-keys.", suffix=".tmp", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON fields")
        result[key] = value
    return result
