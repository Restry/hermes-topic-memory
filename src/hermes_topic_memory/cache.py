from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

_TOPIC_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TOKEN = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]")


@dataclass(frozen=True, slots=True)
class CacheRecord:
    question: str
    normalized_question: str
    question_hash: str
    answer: str
    topic_id: str
    topic_version: int
    model: str
    prompt_version: str
    created_at: str
    similarity: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "normalized_question": self.normalized_question,
            "question_hash": self.question_hash,
            "answer": self.answer,
            "topic_id": self.topic_id,
            "topic_version": self.topic_version,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "created_at": self.created_at,
            "similarity": self.similarity,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CacheRecord:
        return cls(
            question=str(value["question"]),
            normalized_question=str(value["normalized_question"]),
            question_hash=str(value["question_hash"]),
            answer=str(value["answer"]),
            topic_id=str(value["topic_id"]),
            topic_version=int(value["topic_version"]),
            model=str(value["model"]),
            prompt_version=str(value["prompt_version"]),
            created_at=str(value["created_at"]),
            similarity=float(value["similarity"]),
        )


@dataclass(frozen=True, slots=True)
class CacheHit:
    answer: str
    match_kind: str
    similarity: float
    record: CacheRecord


class QuestionCache:
    def __init__(self, root: str | Path, *, jaccard_threshold: float = 0.8) -> None:
        if not 0.0 <= jaccard_threshold <= 1.0:
            raise ValueError("jaccard threshold must be between zero and one")
        self.root = Path(root)
        self.cache_dir = self.root / "qa-cache"
        self.jaccard_threshold = jaccard_threshold
        self._records: dict[str, list[CacheRecord]] = {}
        self._loaded: set[str] = set()

    def put(
        self,
        *,
        question: str,
        answer: str,
        topic_id: str,
        topic_version: int,
        model: str,
        prompt_version: str,
    ) -> CacheRecord:
        self._validate_topic_id(topic_id)
        normalized = normalize_question(question)
        if not normalized:
            raise ValueError("question must contain searchable tokens")
        if not answer or len(answer) > 200:
            raise ValueError("answer must contain 1 to 200 characters")
        if topic_version < 1:
            raise ValueError("topic version must be positive")
        record = CacheRecord(
            question=question,
            normalized_question=normalized,
            question_hash=_question_hash(normalized),
            answer=answer,
            topic_id=topic_id,
            topic_version=topic_version,
            model=model,
            prompt_version=prompt_version,
            created_at=_utc_now(),
            similarity=1.0,
        )
        self._ensure_loaded(topic_id)
        payload = json.dumps(
            record.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        path = self._path(topic_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock(topic_id):
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                written = os.write(descriptor, payload)
                if written != len(payload):
                    raise OSError("incomplete cache record write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self._records[topic_id].append(record)
        return record

    def lookup(
        self,
        question: str,
        *,
        topic_id: str,
        topic_version: int,
        exact_only: bool = False,
    ) -> CacheHit | None:
        self._validate_topic_id(topic_id)
        normalized = normalize_question(question)
        if not normalized or topic_version < 1:
            return None
        self._ensure_loaded(topic_id)
        current_records = [
            record
            for record in self._records[topic_id]
            if record.topic_version == topic_version
        ]
        question_hash = _question_hash(normalized)
        for record in reversed(current_records):
            if (
                record.question_hash == question_hash
                and record.normalized_question == normalized
            ):
                return CacheHit(record.answer, "exact", 1.0, record)
        if exact_only:
            return None

        query_tokens = frozenset(normalized.split())
        best_record: CacheRecord | None = None
        best_similarity = -1.0
        for record in reversed(current_records):
            similarity = _jaccard(query_tokens, frozenset(record.normalized_question.split()))
            if similarity > best_similarity:
                best_record = record
                best_similarity = similarity
        if best_record is None or best_similarity < self.jaccard_threshold:
            return None
        return CacheHit(best_record.answer, "jaccard", best_similarity, best_record)

    def records(self, topic_id: str) -> tuple[CacheRecord, ...]:
        self._validate_topic_id(topic_id)
        self._ensure_loaded(topic_id)
        return tuple(self._records[topic_id])

    def _ensure_loaded(self, topic_id: str) -> None:
        if topic_id in self._loaded:
            return
        records: list[CacheRecord] = []
        path = self._path(topic_id)
        if path.is_file():
            with path.open(encoding="utf-8") as cache_file:
                for line_number, line in enumerate(cache_file, start=1):
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                        record = CacheRecord.from_dict(value)
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                        raise ValueError(
                            f"invalid cache record {path}:{line_number}"
                        ) from error
                    if record.topic_id != topic_id:
                        raise ValueError(f"cache record topic mismatch {path}:{line_number}")
                    records.append(record)
        self._records[topic_id] = records
        self._loaded.add(topic_id)

    @contextmanager
    def _lock(self, topic_id: str) -> Iterator[None]:
        lock_path = self.cache_dir / f".{topic_id}.lock"
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _path(self, topic_id: str) -> Path:
        return self.cache_dir / f"{topic_id}.jsonl"

    @staticmethod
    def _validate_topic_id(topic_id: str) -> None:
        if not _TOPIC_ID.fullmatch(topic_id):
            raise ValueError(f"invalid topic id: {topic_id!r}")


def normalize_question(question: str) -> str:
    normalized = unicodedata.normalize("NFKC", question).casefold()
    return " ".join(_TOKEN.findall(normalized))


def _question_hash(normalized_question: str) -> str:
    return hashlib.sha256(normalized_question.encode("utf-8")).hexdigest()


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
