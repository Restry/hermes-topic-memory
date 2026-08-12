from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator, Literal, Protocol, runtime_checkable

from .evidence import EvidenceLedger, EvidenceRecord
from .models import TopicMemory
from .store import FileTopicStore


class ProposalValidationError(ValueError):
    """Raised when an updater proposal is not deterministically safe."""


@dataclass(frozen=True, slots=True)
class UpdateProposal:
    action: Literal["no_change", "topic_update"]
    topic_id: str
    evidence_ids: tuple[str, ...]
    reason: str
    proposed_body: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateResult:
    status: Literal["no_change", "updated"]
    topic_id: str
    previous_version: int
    current_version: int
    evidence_ids: tuple[str, ...]
    reason: str


@runtime_checkable
class TopicUpdater(Protocol):
    def propose(
        self, memory: TopicMemory, evidence: tuple[EvidenceRecord, ...]
    ) -> UpdateProposal: ...


class EvidenceUpdatePipeline:
    def __init__(
        self,
        *,
        store: FileTopicStore,
        ledger: EvidenceLedger,
        updater: TopicUpdater,
        max_body_chars: int = 200_000,
        replace_topic: Callable[[TopicMemory, str], TopicMemory] | None = None,
    ) -> None:
        if max_body_chars < 1:
            raise ValueError("maximum body size must be positive")
        self.store = store
        self.ledger = ledger
        self.updater = updater
        self.max_body_chars = max_body_chars
        self.replace_topic = replace_topic or self._replace_topic
        self.audit_dir = store.root / "update-proposals"

    def process(
        self, topic_id: str, evidence_ids: tuple[str, ...]
    ) -> UpdateResult:
        evidence = self.ledger.get_many(evidence_ids)
        if any(record.topic_id != topic_id for record in evidence):
            raise ProposalValidationError("raw evidence topic mismatch")
        memory = self.store.get_topic(topic_id)
        proposal = self.updater.propose(memory, evidence)
        self._validate_common(proposal, topic_id)

        if proposal.action == "no_change":
            if proposal.proposed_body is not None:
                raise ProposalValidationError("no_change must not propose a body")
            result = UpdateResult(
                status="no_change",
                topic_id=topic_id,
                previous_version=memory.metadata.version,
                current_version=memory.metadata.version,
                evidence_ids=proposal.evidence_ids,
                reason=proposal.reason,
            )
            self._append_audit(proposal, result)
            return result

        self._validate_update(proposal, memory, evidence)
        updated = self.replace_topic(memory, proposal.proposed_body or "")
        result = UpdateResult(
            status="updated",
            topic_id=topic_id,
            previous_version=memory.metadata.version,
            current_version=updated.metadata.version,
            evidence_ids=proposal.evidence_ids,
            reason=proposal.reason,
        )
        self._append_audit(proposal, result)
        return result

    def _replace_topic(self, memory: TopicMemory, body: str) -> TopicMemory:
        return self.store.update_topic(
            memory.metadata.topic_id,
            body,
            expected_version=memory.metadata.version,
            expected_fingerprint=memory.metadata.content_fingerprint,
        )

    @staticmethod
    def _validate_common(proposal: UpdateProposal, topic_id: str) -> None:
        if proposal.action not in ("no_change", "topic_update"):
            raise ProposalValidationError(f"unknown proposal action: {proposal.action!r}")
        if proposal.topic_id != topic_id:
            raise ProposalValidationError("proposal topic mismatch")
        if not proposal.reason.strip():
            raise ProposalValidationError("proposal reason must not be empty")

    def _validate_update(
        self,
        proposal: UpdateProposal,
        memory: TopicMemory,
        evidence: tuple[EvidenceRecord, ...],
    ) -> None:
        body = proposal.proposed_body
        if not isinstance(body, str) or not body.strip():
            raise ProposalValidationError("proposed body must be non-empty")
        if len(body) > self.max_body_chars:
            raise ProposalValidationError("proposed body exceeds maximum size")
        if body == memory.body:
            raise ProposalValidationError("proposed body must differ from current body")
        if not proposal.evidence_ids or len(set(proposal.evidence_ids)) != len(
            proposal.evidence_ids
        ):
            raise ProposalValidationError("update requires unique matching raw evidence ids")
        available_ids = {record.evidence_id for record in evidence}
        if not set(proposal.evidence_ids).issubset(available_ids):
            raise ProposalValidationError("update requires matching raw evidence ids")
        evidence_by_id = {record.evidence_id: record for record in evidence}
        if any(
            evidence_by_id[evidence_id].topic_id != proposal.topic_id
            for evidence_id in proposal.evidence_ids
        ):
            raise ProposalValidationError("proposal evidence topic mismatch")

    def _append_audit(
        self, proposal: UpdateProposal, result: UpdateResult
    ) -> None:
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        path = self.audit_dir / f"{proposal.topic_id}.jsonl"
        record = {
            "action": proposal.action,
            "topic_id": proposal.topic_id,
            "proposed_body": proposal.proposed_body,
            "evidence_ids": list(proposal.evidence_ids),
            "reason": proposal.reason,
            "status": "applied" if result.status == "updated" else "no_change",
            "previous_version": result.previous_version,
            "current_version": result.current_version,
            "created_at": _utc_now(),
        }
        payload = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        with self._audit_lock(proposal.topic_id):
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                written = os.write(descriptor, payload)
                if written != len(payload):
                    raise OSError("incomplete update audit write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @contextmanager
    def _audit_lock(self, topic_id: str) -> Iterator[None]:
        lock_path = self.audit_dir / f".{topic_id}.lock"
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
