from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .evidence import EvidenceLedger, EvidenceRecord
from .models import TopicMemory
from .store import FileTopicStore
from .updater import EvidenceUpdatePipeline, UpdateProposal, UpdateResult


@dataclass(frozen=True, slots=True)
class _ExplicitUpdater:
    proposal: UpdateProposal

    def propose(
        self, memory: TopicMemory, evidence: tuple[EvidenceRecord, ...]
    ) -> UpdateProposal:
        return self.proposal


def apply_evidence_update(
    *,
    store: FileTopicStore,
    ledger: EvidenceLedger,
    topic_id: str,
    body: str,
    evidence_ids: tuple[str, ...],
    reason: str,
    replace_topic: Callable[[TopicMemory, str], TopicMemory] | None = None,
) -> UpdateResult:
    """Apply a complete replacement after the core pipeline validates its evidence."""
    proposal = UpdateProposal(
        action="topic_update",
        topic_id=topic_id,
        evidence_ids=evidence_ids,
        reason=reason,
        proposed_body=body,
    )
    return EvidenceUpdatePipeline(
        store=store,
        ledger=ledger,
        updater=_ExplicitUpdater(proposal),
        replace_topic=replace_topic,
    ).process(topic_id, evidence_ids)
