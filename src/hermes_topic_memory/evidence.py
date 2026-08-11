from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

_TOPIC_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class EvidenceError(RuntimeError):
    """Base error for raw evidence persistence."""


class EvidenceNotFoundError(EvidenceError):
    """Raised when an ID is not present in the raw evidence ledger."""


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    topic_id: str
    session_id: str
    user_text: str
    assistant_text: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "topic_id": self.topic_id,
            "session_id": self.session_id,
            "user_text": self.user_text,
            "assistant_text": self.assistant_text,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EvidenceRecord:
        return cls(
            evidence_id=str(value["evidence_id"]),
            topic_id=str(value["topic_id"]),
            session_id=str(value["session_id"]),
            user_text=str(value["user_text"]),
            assistant_text=str(value["assistant_text"]),
            created_at=str(value["created_at"]),
        )


class EvidenceLedger:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.topics_dir = self.root / "topics"
        self._by_id: dict[str, EvidenceRecord] = {}
        self._loaded_paths: set[Path] = set()

    def append_turn(
        self,
        topic_id: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
    ) -> EvidenceRecord:
        self._validate_topic_id(topic_id)
        if not session_id.strip():
            raise ValueError("session id must not be empty")
        topic_dir = self.topics_dir / topic_id
        if not topic_dir.is_dir():
            raise EvidenceNotFoundError(f"canonical topic does not exist: {topic_id}")
        record = EvidenceRecord(
            evidence_id=uuid.uuid4().hex,
            topic_id=topic_id,
            session_id=session_id,
            user_text=user_text,
            assistant_text=assistant_text,
            created_at=_utc_now(),
        )
        path = topic_dir / "evidence.jsonl"
        payload = json.dumps(
            record.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        with self._lock(topic_id):
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                written = os.write(descriptor, payload)
                if written != len(payload):
                    raise OSError("incomplete evidence record write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self._by_id[record.evidence_id] = record
        return record

    def get(self, evidence_id: str) -> EvidenceRecord:
        record = self._by_id.get(evidence_id)
        if record is not None:
            return record
        self._load_all()
        record = self._by_id.get(evidence_id)
        if record is None:
            raise EvidenceNotFoundError(f"raw evidence does not exist: {evidence_id}")
        return record

    def get_many(self, evidence_ids: tuple[str, ...]) -> tuple[EvidenceRecord, ...]:
        if not evidence_ids:
            raise EvidenceNotFoundError("at least one raw evidence id is required")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("duplicate evidence ids are not allowed")
        return tuple(self.get(evidence_id) for evidence_id in evidence_ids)

    def records(self, topic_id: str) -> tuple[EvidenceRecord, ...]:
        self._validate_topic_id(topic_id)
        self._load_path(self.topics_dir / topic_id / "evidence.jsonl")
        return tuple(
            record for record in self._by_id.values() if record.topic_id == topic_id
        )

    def _load_all(self) -> None:
        if not self.topics_dir.is_dir():
            return
        for topic_dir in self.topics_dir.iterdir():
            if topic_dir.is_dir():
                self._load_path(topic_dir / "evidence.jsonl")

    def _load_path(self, path: Path) -> None:
        if path in self._loaded_paths:
            return
        if path.is_file():
            with path.open(encoding="utf-8") as evidence_file:
                for line_number, line in enumerate(evidence_file, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = EvidenceRecord.from_dict(json.loads(line))
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                        raise EvidenceError(
                            f"invalid evidence record {path}:{line_number}"
                        ) from error
                    existing = self._by_id.get(record.evidence_id)
                    if existing is not None and existing != record:
                        raise EvidenceError(f"duplicate evidence id: {record.evidence_id}")
                    self._by_id[record.evidence_id] = record
        self._loaded_paths.add(path)

    @contextmanager
    def _lock(self, topic_id: str) -> Iterator[None]:
        lock_path = self.topics_dir / topic_id / ".evidence.lock"
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _validate_topic_id(topic_id: str) -> None:
        if not _TOPIC_ID.fullmatch(topic_id):
            raise ValueError(f"invalid topic id: {topic_id!r}")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
