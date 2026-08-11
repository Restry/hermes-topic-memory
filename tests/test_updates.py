from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_topic_memory.cache import QuestionCache
from hermes_topic_memory.evidence import EvidenceLedger, EvidenceNotFoundError
from hermes_topic_memory.store import FileTopicStore
from hermes_topic_memory.updater import (
    EvidenceUpdatePipeline,
    ProposalValidationError,
    UpdateProposal,
)


class FixedUpdater:
    def __init__(self, proposal: UpdateProposal) -> None:
        self.proposal = proposal
        self.received_evidence_ids: tuple[str, ...] = ()

    def propose(self, memory, evidence):
        self.received_evidence_ids = tuple(item.evidence_id for item in evidence)
        return self.proposal


def setup_topic(tmp_path: Path) -> tuple[FileTopicStore, EvidenceLedger]:
    store = FileTopicStore(tmp_path)
    store.create_topic("alpha", "# Alpha\n\nOld canonical fact.\n")
    return store, EvidenceLedger(tmp_path)


def test_raw_evidence_is_fsynced_first_and_round_trips_exact_text(tmp_path: Path) -> None:
    _store, ledger = setup_topic(tmp_path)

    record = ledger.append_turn(
        topic_id="alpha",
        session_id="session-1",
        user_text="User says café is current.",
        assistant_text="Acknowledged — café.",
    )

    evidence_path = tmp_path / "topics" / "alpha" / "evidence.jsonl"
    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert persisted["evidence_id"] == record.evidence_id
    assert ledger.get(record.evidence_id).user_text == "User says café is current."
    assert ledger.get(record.evidence_id).assistant_text == "Acknowledged — café."


def test_no_change_leaves_canonical_memory_and_version_untouched(tmp_path: Path) -> None:
    store, ledger = setup_topic(tmp_path)
    evidence = ledger.append_turn("alpha", "session-1", "hello", "hello")
    updater = FixedUpdater(
        UpdateProposal(
            action="no_change",
            topic_id="alpha",
            evidence_ids=(evidence.evidence_id,),
            reason="No durable fact.",
        )
    )
    pipeline = EvidenceUpdatePipeline(store=store, ledger=ledger, updater=updater)

    result = pipeline.process("alpha", (evidence.evidence_id,))

    assert result.status == "no_change"
    assert result.previous_version == result.current_version == 1
    assert store.get_topic("alpha").body == "# Alpha\n\nOld canonical fact.\n"


def test_validated_topic_update_is_audited_and_bumps_version(tmp_path: Path) -> None:
    store, ledger = setup_topic(tmp_path)
    evidence = ledger.append_turn(
        "alpha", "session-1", "Alpha now uses region two.", "Recorded."
    )
    updater = FixedUpdater(
        UpdateProposal(
            action="topic_update",
            topic_id="alpha",
            proposed_body="# Alpha\n\nCurrent region: two.\n",
            evidence_ids=(evidence.evidence_id,),
            reason="The turn explicitly supersedes the old region.",
        )
    )
    pipeline = EvidenceUpdatePipeline(store=store, ledger=ledger, updater=updater)

    result = pipeline.process("alpha", (evidence.evidence_id,))

    assert result.status == "updated"
    assert (result.previous_version, result.current_version) == (1, 2)
    assert store.get_topic("alpha").body == "# Alpha\n\nCurrent region: two.\n"
    assert updater.received_evidence_ids == (evidence.evidence_id,)
    audit = json.loads(
        (tmp_path / "update-proposals" / "alpha.jsonl").read_text().splitlines()[0]
    )
    assert audit["status"] == "applied"
    assert audit["evidence_ids"] == [evidence.evidence_id]


def test_topic_mismatch_and_missing_evidence_are_rejected(tmp_path: Path) -> None:
    store, ledger = setup_topic(tmp_path)
    store.create_topic("beta", "# Beta\n")
    beta_evidence = ledger.append_turn("beta", "session-2", "beta fact", "ok")
    updater = FixedUpdater(
        UpdateProposal(
            action="topic_update",
            topic_id="alpha",
            proposed_body="# Alpha\n\nChanged.\n",
            evidence_ids=(beta_evidence.evidence_id,),
            reason="wrong topic",
        )
    )
    pipeline = EvidenceUpdatePipeline(store=store, ledger=ledger, updater=updater)

    with pytest.raises(ProposalValidationError, match="topic mismatch"):
        pipeline.process("alpha", (beta_evidence.evidence_id,))
    with pytest.raises(EvidenceNotFoundError):
        pipeline.process("alpha", ("missing-evidence",))
    assert store.get_topic("alpha").metadata.version == 1


def test_qa_cache_records_can_never_be_canonical_update_evidence(tmp_path: Path) -> None:
    store, ledger = setup_topic(tmp_path)
    raw = ledger.append_turn("alpha", "session-1", "real evidence", "ok")
    cache_record = QuestionCache(tmp_path).put(
        question="cached question",
        answer="cached answer",
        topic_id="alpha",
        topic_version=1,
        model="reader",
        prompt_version="v1",
    )
    updater = FixedUpdater(
        UpdateProposal(
            action="topic_update",
            topic_id="alpha",
            proposed_body="# Alpha\n\nPoisoned by cache.\n",
            evidence_ids=(cache_record.question_hash,),
            reason="invalid cache source",
        )
    )
    pipeline = EvidenceUpdatePipeline(store=store, ledger=ledger, updater=updater)

    with pytest.raises(ProposalValidationError, match="matching raw evidence"):
        pipeline.process("alpha", (raw.evidence_id,))
    assert store.get_topic("alpha").body == "# Alpha\n\nOld canonical fact.\n"


def test_deterministic_validation_rejects_unchanged_empty_and_oversized_bodies(
    tmp_path: Path,
) -> None:
    store, ledger = setup_topic(tmp_path)
    evidence = ledger.append_turn("alpha", "session-1", "fact", "ok")

    for body, message in (
        ("# Alpha\n\nOld canonical fact.\n", "must differ"),
        ("", "non-empty"),
        ("x" * 51, "maximum"),
    ):
        proposal = UpdateProposal(
            action="topic_update",
            topic_id="alpha",
            proposed_body=body,
            evidence_ids=(evidence.evidence_id,),
            reason="candidate",
        )
        pipeline = EvidenceUpdatePipeline(
            store=store,
            ledger=ledger,
            updater=FixedUpdater(proposal),
            max_body_chars=50,
        )
        with pytest.raises(ProposalValidationError, match=message):
            pipeline.process("alpha", (evidence.evidence_id,))

    assert store.get_topic("alpha").metadata.version == 1


def test_applied_update_can_be_rolled_back_without_reusing_cache(tmp_path: Path) -> None:
    store, ledger = setup_topic(tmp_path)
    evidence = ledger.append_turn("alpha", "session-1", "new fact", "ok")
    proposal = UpdateProposal(
        action="topic_update",
        topic_id="alpha",
        proposed_body="# Alpha\n\nNew fact.\n",
        evidence_ids=(evidence.evidence_id,),
        reason="verified",
    )
    EvidenceUpdatePipeline(
        store=store, ledger=ledger, updater=FixedUpdater(proposal)
    ).process("alpha", (evidence.evidence_id,))

    rollback = store.rollback_topic("alpha", 1)

    assert rollback.metadata.version == 3
    assert rollback.body == "# Alpha\n\nOld canonical fact.\n"
