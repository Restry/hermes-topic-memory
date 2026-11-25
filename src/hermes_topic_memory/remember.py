from __future__ import annotations

import fcntl
import hashlib
import json
import re
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Literal

from .evidence import EvidenceError, EvidenceLedger, EvidenceRecord
from .store import FileTopicStore, TopicNotFoundError, TopicStateError
from .updater import append_update_audit

_THREAD_LOCKS: dict[Path, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+", re.MULTILINE)
_REMEMBERED_HEADING = re.compile(
    r"^ {0,3}#{1,6}[ \t]+Remembered Facts[ \t]*#*[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RememberResult:
    topic_id: str
    topic_version: int
    evidence_id: str
    status: Literal["remembered", "duplicate"]


class RememberService:
    def __init__(self, data_root: str | Path) -> None:
        self.data_root = Path(data_root)
        self.store = FileTopicStore(self.data_root)

    def remember(
        self,
        topic_id: str,
        fact: str,
        *,
        source_agent: str,
        source_session: str | None = None,
        idempotency_key: str | None = None,
        reason: str | None = None,
    ) -> RememberResult:
        self.store._validate_topic_id(topic_id)
        bullet = _fact_bullet(fact)
        _validate_label("source_agent", source_agent)
        if source_session is not None:
            _validate_label("source_session", source_session)
        if idempotency_key is not None:
            _validate_label("idempotency_key", idempotency_key)
        if reason is not None and not isinstance(reason, str):
            raise ValueError("reason must be text")

        with self._write_lock(topic_id):
            memory = self.store.get_topic(topic_id)
            if memory.metadata.state != "active":
                raise TopicStateError(
                    f"topic is read-only in state {memory.metadata.state}: {topic_id}"
                )
            # A fresh ledger sees writes from every service instance and process.
            ledger = EvidenceLedger(self.data_root)
            if idempotency_key is not None:
                statuses = self._audit_statuses(topic_id)
                for record in ledger.records(topic_id):
                    if record.idempotency_key != idempotency_key:
                        continue
                    status = statuses.get(record.evidence_id)
                    if status == "failed":
                        continue
                    if status == "applied" or self._was_applied(
                        record, memory.metadata.version
                    ):
                        if status != "applied":
                            self._append_audit(record, record.topic_version - 1, "applied")
                        return RememberResult(
                            topic_id=topic_id,
                            topic_version=record.topic_version,
                            evidence_id=record.evidence_id,
                            status="duplicate",
                        )

            timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            source_label = re.sub(r"([\\`*_{}\[\]<>#!|])", r"\\\1", source_agent.strip())
            bullet = f"- [{timestamp[:10]} | {source_label}] {bullet[2:]}"
            body = _append_fact(memory.body, bullet)
            record = ledger.append_remember(
                topic_id,
                fact,
                source_agent=source_agent,
                source_session=source_session,
                idempotency_key=idempotency_key,
                reason=reason,
                topic_version=memory.metadata.version + 1,
                topic_fingerprint=_fingerprint(body),
                created_at=timestamp,
            )
            # Failed attempts retain their raw evidence but do not reserve keys.
            try:
                updated = self.store.update_topic(
                    topic_id,
                    body,
                    expected_version=memory.metadata.version,
                    expected_fingerprint=memory.metadata.content_fingerprint,
                )
            except Exception:
                current = self.store.get_topic(topic_id)
                if not self._was_applied(record, current.metadata.version):
                    self._append_audit(record, memory.metadata.version, "failed")
                raise
            self._append_audit(record, memory.metadata.version, "applied")
            return RememberResult(
                topic_id=topic_id,
                topic_version=updated.metadata.version,
                evidence_id=record.evidence_id,
                status="remembered",
            )

    def _append_audit(
        self, record: EvidenceRecord, previous_version: int, status: str
    ) -> None:
        append_update_audit(
            self.data_root,
            record.topic_id,
            {
                "action": "remember_append",
                "topic_id": record.topic_id,
                "evidence_id": record.evidence_id,
                "evidence_ids": [record.evidence_id],
                "source_agent": record.source_agent,
                "source_session": record.source_session,
                "idempotency_key": record.idempotency_key,
                "reason": record.reason,
                "status": status,
                "previous_version": previous_version,
                "current_version": record.topic_version if status == "applied" else previous_version,
                "created_at": record.created_at,
            },
        )

    def _audit_statuses(self, topic_id: str) -> dict[str, str]:
        path = self.data_root / "update-proposals" / f"{topic_id}.jsonl"
        if not path.is_file():
            return {}
        statuses: dict[str, str] = {}
        with path.open(encoding="utf-8") as audit_file:
            for line_number, line in enumerate(audit_file, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if record.get("action") == "remember_append":
                        statuses[record["evidence_id"]] = record["status"]
                except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as error:
                    raise EvidenceError(f"invalid update audit {path}:{line_number}") from error
        return statuses

    def _was_applied(self, record: EvidenceRecord, current_version: int) -> bool:
        version = record.topic_version
        if (
            not isinstance(version, int)
            or version < 1
            or version > current_version
            or record.topic_fingerprint is None
        ):
            return False
        try:
            body = self.store.read_version(record.topic_id, version)
        except TopicNotFoundError:
            return False
        return _fingerprint(body) == record.topic_fingerprint

    @contextmanager
    def _write_lock(self, topic_id: str) -> Iterator[None]:
        topic_dir = self.data_root / "topics" / topic_id
        if not topic_dir.is_dir():
            raise TopicNotFoundError(f"topic does not exist: {topic_id}")
        lock_path = topic_dir / ".write.lock"
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(lock_path.resolve(), threading.RLock())
        with thread_lock, lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _validate_label(name: str, value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(unicodedata.category(character) in ("Cc", "Cs", "Zl", "Zp") for character in value)
    ):
        raise ValueError(f"{name} must be non-empty single-line text")


def _fact_bullet(fact: str) -> str:
    if not isinstance(fact, str) or not 1 <= len(fact) <= 4000:
        raise ValueError("fact must contain 1..4000 characters")
    if any(unicodedata.category(character) == "Cs" for character in fact):
        raise ValueError("fact must contain valid Unicode text")
    cleaned = " ".join(
        "".join(
            " " if unicodedata.category(character) == "Cc" else character
            for character in fact
        ).split()
    )
    cleaned = re.sub(r"^(?:[-+*]|\d+[.)])[ \t]+", "", cleaned)
    if not cleaned or cleaned in ("-", "+", "*"):
        raise ValueError("fact must contain non-empty text")
    cleaned = re.sub(r"([\\`*_{}\[\]<>#!|])", r"\\\1", cleaned)
    return f"- {cleaned}"


def _append_fact(body: str, bullet: str) -> str:
    section = _REMEMBERED_HEADING.search(body)
    if section is None:
        return f"{body.rstrip()}\n\n## Remembered Facts\n\n{bullet}\n"
    next_heading = _HEADING.search(body, section.end())
    end = next_heading.start() if next_heading is not None else len(body)
    prefix = body[:end].rstrip()
    separator = "\n\n" if not body[section.end():end].strip() else "\n"
    suffix = f"\n{body[end:]}" if next_heading is not None else ""
    return f"{prefix}{separator}{bullet}\n{suffix}"


def _fingerprint(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
