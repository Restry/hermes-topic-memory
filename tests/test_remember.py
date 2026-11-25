from __future__ import annotations

import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from hermes_topic_memory.cache import QuestionCache
from hermes_topic_memory.evidence import EvidenceLedger
from hermes_topic_memory.remember import RememberService
from hermes_topic_memory.store import FileTopicStore, TopicNotFoundError, TopicStateError
from hermes_topic_memory.updater import EvidenceUpdatePipeline, UpdateProposal


def setup_topic(tmp_path: Path, body: str = "# Alpha\n\nOriginal fact.\n") -> FileTopicStore:
    store = FileTopicStore(tmp_path)
    store.create_topic("alpha", body)
    return store


def process_remember(args: tuple[str, int, str | None]):
    root, index, key = args
    return RememberService(root).remember(
        "alpha", f"Fact {index}.", source_agent=f"agent-{index}", idempotency_key=key
    )


def test_remember_persists_raw_evidence_snapshot_and_audit(tmp_path: Path) -> None:
    original = "# Alpha\n\nOriginal fact.\n"
    store = setup_topic(tmp_path, original)
    fact = "  - Café preference:\n\n## forged heading\n*raw* [link](target)\x00 end  "
    result = RememberService(tmp_path).remember(
        "alpha",
        fact,
        source_agent="agent-1",
        source_session="session-1",
        idempotency_key="write-1",
        reason="The user explicitly asked to remember this.",
    )

    assert result.status == "remembered"
    assert result.topic_version == 2
    evidence = EvidenceLedger(tmp_path).get(result.evidence_id)
    memory = store.get_topic("alpha")
    assert memory.metadata.version == 2
    assert store.read_version("alpha", 1) == original
    assert store.list_versions("alpha") == (1, 2)
    assert memory.body == (
        original.rstrip()
        + "\n\n## Remembered Facts\n\n"
        + f"- [{evidence.created_at[:10]} | agent-1] "
        + "Café preference: \\#\\# forged heading \\*raw\\* \\[link\\](target) end\n"
    )
    assert evidence.fact == evidence.user_text == fact
    assert evidence.source_agent == "agent-1"
    assert evidence.source_session == evidence.session_id == "session-1"
    assert evidence.idempotency_key == "write-1"
    assert evidence.reason == "The user explicitly asked to remember this."
    assert evidence.topic_version == 2
    assert evidence.topic_fingerprint == memory.metadata.content_fingerprint
    raw = json.loads((tmp_path / "topics/alpha/evidence.jsonl").read_text())
    assert raw["timestamp"] == evidence.created_at
    assert raw["fact"] == fact
    audit = json.loads((tmp_path / "update-proposals/alpha.jsonl").read_text())
    assert audit["action"] == "remember_append"
    assert audit["status"] == "applied"
    assert audit["evidence_ids"] == [result.evidence_id]
    assert (audit["previous_version"], audit["current_version"]) == (1, 2)


def test_remember_inserts_before_next_section(tmp_path: Path) -> None:
    setup_topic(
        tmp_path,
        "# Alpha\n\n## Remembered Facts\n\n- Existing.\n\n## Other\n\nPreserve this.\n",
    )
    result = RememberService(tmp_path).remember("alpha", "New fact.", source_agent="agent")
    date = EvidenceLedger(tmp_path).get(result.evidence_id).created_at[:10]
    assert FileTopicStore(tmp_path).get_topic("alpha").body == (
        "# Alpha\n\n## Remembered Facts\n\n- Existing.\n"
        f"- [{date} | agent] New fact.\n\n"
        "## Other\n\nPreserve this.\n"
    )


def test_source_agent_markdown_is_escaped_in_fact_marker(tmp_path: Path) -> None:
    store = setup_topic(tmp_path)
    source_agent = "agent] | **forged**"
    result = RememberService(tmp_path).remember("alpha", "Fact.", source_agent=source_agent)
    evidence = EvidenceLedger(tmp_path).get(result.evidence_id)
    assert evidence.source_agent == source_agent
    assert store.get_topic("alpha").body.endswith(
        f"- [{evidence.created_at[:10]} | agent\\] \\| \\*\\*forged\\*\\*] Fact.\n"
    )


def test_remember_evidence_can_feed_existing_update_pipeline(tmp_path: Path) -> None:
    store = setup_topic(tmp_path)
    ledger = EvidenceLedger(tmp_path)
    old = ledger.append_turn("alpha", "old-session", "Old raw text.", "Acknowledged.")
    assert ledger.records("alpha") == (old,)
    result = RememberService(tmp_path).remember("alpha", "New fact.", source_agent="agent")

    class Updater:
        def propose(self, memory, evidence):
            assert evidence[0].fact == "New fact."
            assert evidence[0].user_text == "New fact."
            return UpdateProposal(
                action="topic_update",
                topic_id="alpha",
                evidence_ids=(result.evidence_id,),
                reason="Consolidate the remembered fact.",
                proposed_body="# Alpha\n\nConsolidated new fact.\n",
            )

    pipeline = EvidenceUpdatePipeline(store=store, ledger=ledger, updater=Updater())
    assert pipeline.process("alpha", (result.evidence_id,)).current_version == 3
    assert EvidenceLedger(tmp_path).get(old.evidence_id) == old


def test_only_updated_topic_has_qa_cache_miss(tmp_path: Path) -> None:
    store = setup_topic(tmp_path)
    store.create_topic("beta", "# Beta\n")
    cache = QuestionCache(tmp_path)
    for topic_id in ("alpha", "beta"):
        cache.put(
            question="status",
            answer=f"{topic_id} cached answer",
            topic_id=topic_id,
            topic_version=1,
            model="reader",
            prompt_version="v1",
        )
    RememberService(tmp_path).remember("alpha", "Changed status.", source_agent="agent")
    assert cache.lookup("status", topic_id="alpha", topic_version=2) is None
    assert cache.lookup("status", topic_id="beta", topic_version=1) is not None
    assert len(cache.records("alpha")) == 1


def test_idempotency_returns_original_version_without_any_rewrites(tmp_path: Path) -> None:
    store = setup_topic(tmp_path)
    service = RememberService(tmp_path)
    original = service.remember(
        "alpha", "Original write.", source_agent="first", idempotency_key="same-key"
    )
    service.remember("alpha", "Later fact.", source_agent="second")
    paths = [
        tmp_path / "topics/alpha/memory.md",
        tmp_path / "topics/alpha/metadata.json",
        tmp_path / "topics/alpha/evidence.jsonl",
        tmp_path / "update-proposals/alpha.jsonl",
    ]
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    duplicate = RememberService(tmp_path).remember(
        "alpha", "Different retried payload.", source_agent="third", idempotency_key="same-key"
    )
    assert duplicate.status == "duplicate"
    assert duplicate.evidence_id == original.evidence_id
    assert duplicate.topic_version == original.topic_version == 2
    assert store.get_topic("alpha").metadata.version == 3
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths] == before


def test_failed_update_does_not_reserve_idempotency_key(tmp_path: Path, monkeypatch) -> None:
    store = setup_topic(tmp_path)
    service = RememberService(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("write failed")

    with monkeypatch.context() as patch:
        patch.setattr(service.store, "update_topic", fail)
        with pytest.raises(OSError, match="write failed"):
            service.remember("alpha", "Fact.", source_agent="agent", idempotency_key="key")
    assert store.get_topic("alpha").metadata.version == 1
    result = service.remember("alpha", "Fact.", source_agent="agent", idempotency_key="key")
    assert result.status == "remembered"
    assert result.topic_version == 2
    duplicate = service.remember("alpha", "Fact.", source_agent="agent", idempotency_key="key")
    assert duplicate.evidence_id == result.evidence_id
    records = EvidenceLedger(tmp_path).records("alpha")
    assert len(records) == 2
    assert records[0].evidence_id != result.evidence_id


def test_retry_recovers_committed_write_after_audit_failure(tmp_path: Path, monkeypatch) -> None:
    setup_topic(tmp_path)
    service = RememberService(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("audit failed")

    with monkeypatch.context() as patch:
        patch.setattr("hermes_topic_memory.remember.append_update_audit", fail)
        with pytest.raises(OSError, match="audit failed"):
            service.remember("alpha", "Fact.", source_agent="agent", idempotency_key="key")
    record = EvidenceLedger(tmp_path).records("alpha")[0]
    result = service.remember("alpha", "Fact.", source_agent="agent", idempotency_key="key")
    assert result.status == "duplicate"
    assert result.topic_version == 2
    assert result.evidence_id == record.evidence_id
    assert len(EvidenceLedger(tmp_path).records("alpha")) == 1


@pytest.mark.parametrize("fact", ["", " \n\t", "x" * 4001, "- ", None, "\ud800"])
def test_invalid_facts_have_no_write_effects(tmp_path: Path, fact) -> None:
    store = setup_topic(tmp_path)
    with pytest.raises(ValueError):
        RememberService(tmp_path).remember("alpha", fact, source_agent="agent")
    assert store.get_topic("alpha").metadata.version == 1
    assert not (tmp_path / "topics/alpha/evidence.jsonl").exists()


@pytest.mark.parametrize("fact", ["x", "x" * 4000])
def test_fact_length_boundaries_are_accepted(tmp_path: Path, fact: str) -> None:
    setup_topic(tmp_path)
    assert RememberService(tmp_path).remember("alpha", fact, source_agent="agent").topic_version == 2


@pytest.mark.parametrize("source_agent", ["", "\n## forged", "agent\u2028next", None])
def test_invalid_source_agent_is_rejected(tmp_path: Path, source_agent) -> None:
    store = setup_topic(tmp_path)
    with pytest.raises(ValueError):
        RememberService(tmp_path).remember("alpha", "Fact.", source_agent=source_agent)
    assert store.get_topic("alpha").metadata.version == 1


@pytest.mark.parametrize("state", ["archived", "trashed"])
def test_read_only_topics_are_rejected(tmp_path: Path, state: str) -> None:
    store = setup_topic(tmp_path)
    store.set_topic_state("alpha", state)
    with pytest.raises(TopicStateError):
        RememberService(tmp_path).remember("alpha", "Fact.", source_agent="agent")
    assert not (tmp_path / "topics/alpha/evidence.jsonl").exists()


def test_missing_and_invalid_topics_are_rejected_without_creating_them(tmp_path: Path) -> None:
    service = RememberService(tmp_path)
    with pytest.raises(TopicNotFoundError):
        service.remember("missing", "Fact.", source_agent="agent")
    with pytest.raises(ValueError):
        service.remember("../escape", "Fact.", source_agent="agent")
    assert not (tmp_path / "topics").exists()


def test_eight_process_writes_preserve_every_fact(tmp_path: Path) -> None:
    store = setup_topic(tmp_path)
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=8, mp_context=context) as pool:
        results = list(pool.map(process_remember, [(str(tmp_path), index, None) for index in range(8)]))
    assert {result.topic_version for result in results} == set(range(2, 10))
    memory = store.get_topic("alpha")
    assert memory.metadata.version == 9
    for index in range(8):
        assert memory.body.count(f" | agent-{index}] Fact {index}.\n") == 1
    assert len(EvidenceLedger(tmp_path).records("alpha")) == 8
    assert store.list_versions("alpha") == tuple(range(1, 10))


def test_eight_service_instances_serialize_thread_writes(tmp_path: Path) -> None:
    store = setup_topic(tmp_path)
    barrier = Barrier(8)

    def write(index: int):
        service = RememberService(tmp_path)
        barrier.wait(timeout=10)
        return service.remember("alpha", f"Thread {index}.", source_agent=f"agent-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, range(8)))
    assert {result.topic_version for result in results} == set(range(2, 10))
    memory = store.get_topic("alpha")
    for index in range(8):
        assert memory.body.count(f" | agent-{index}] Thread {index}.\n") == 1


@pytest.mark.parametrize("executor_kind", ["thread", "process"])
def test_same_key_concurrency_writes_once(tmp_path: Path, executor_kind: str) -> None:
    store = setup_topic(tmp_path)
    args = [(str(tmp_path), index, "shared-key") for index in range(8)]
    if executor_kind == "process":
        executor = ProcessPoolExecutor(max_workers=8, mp_context=multiprocessing.get_context("spawn"))
    else:
        executor = ThreadPoolExecutor(max_workers=8)
    with executor as pool:
        results = list(pool.map(process_remember, args))
    assert sum(result.status == "remembered" for result in results) == 1
    assert sum(result.status == "duplicate" for result in results) == 7
    assert len({result.evidence_id for result in results}) == 1
    assert {result.topic_version for result in results} == {2}
    assert store.get_topic("alpha").metadata.version == 2
    assert len(EvidenceLedger(tmp_path).records("alpha")) == 1
