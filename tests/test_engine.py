from __future__ import annotations

from pathlib import Path

from hermes_topic_memory.cache import QuestionCache
from hermes_topic_memory.engine import RecallEngine
from hermes_topic_memory.reader import ReaderRequest, ReaderResponse, ReaderUsage
from hermes_topic_memory.store import FileTopicStore


class SequenceReader:
    def __init__(self, answers: list[str], *, with_usage: bool = False) -> None:
        self.answers = answers
        self.with_usage = with_usage
        self.requests: list[ReaderRequest] = []

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.requests.append(request)
        answer = self.answers[len(self.requests) - 1]
        usage = ReaderUsage(10, 2, 8) if self.with_usage else None
        return ReaderResponse(answer=answer, model="fake-reader", usage=usage)


def make_engine(tmp_path: Path, reader: SequenceReader) -> tuple[RecallEngine, FileTopicStore]:
    store = FileTopicStore(tmp_path)
    store.create_topic("alpha", "# Alpha\n\nCanonical fact.\n", display_name="Alpha")
    cache = QuestionCache(tmp_path, jaccard_threshold=1.0)
    return RecallEngine(store=store, cache=cache, reader=reader), store


def test_recall_uses_byte_stable_prefix_and_caches_success(tmp_path: Path) -> None:
    reader = SequenceReader(["First grounded answer.", "Second grounded answer."], with_usage=True)
    engine, _store = make_engine(tmp_path, reader)

    first = engine.recall("alpha", "first unique question")
    second = engine.recall("alpha", "second unrelated prompt")
    cached = engine.recall("alpha", " FIRST unique question!!! ")

    assert first.answer == "First grounded answer."
    assert second.answer == "Second grounded answer."
    assert cached.answer == first.answer
    assert cached.source == "cache"
    assert len(reader.requests) == 2
    assert reader.requests[0].prefix.encode() == reader.requests[1].prefix.encode()
    assert reader.requests[0].prompt.startswith(reader.requests[0].prefix)
    assert first.usages == (ReaderUsage(10, 2, 8),)


def test_overlength_answer_gets_one_correction_then_succeeds(tmp_path: Path) -> None:
    reader = SequenceReader(["x" * 201, "Corrected answer."])
    engine, _store = make_engine(tmp_path, reader)

    result = engine.recall("alpha", "question")

    assert result.answer == "Corrected answer."
    assert result.failure is None
    assert len(reader.requests) == 2
    assert reader.requests[1].correction_of == "x" * 201
    assert reader.requests[1].prefix == reader.requests[0].prefix


def test_second_overlength_answer_returns_structured_failure_without_cache(
    tmp_path: Path,
) -> None:
    reader = SequenceReader(["x" * 201, "y" * 201])
    engine, _store = make_engine(tmp_path, reader)

    result = engine.recall("alpha", "too long")

    assert result.answer is None
    assert result.source == "failure"
    assert result.failure is not None
    assert result.failure.code == "answer_too_long"
    assert result.failure.attempts == 2
    assert len(reader.requests) == 2
    assert engine.cache.lookup("too long", topic_id="alpha", topic_version=1) is None


def test_topic_update_changes_prefix_and_invalidates_only_stale_topic_cache(
    tmp_path: Path,
) -> None:
    reader = SequenceReader(["Version one answer.", "Version two answer."])
    engine, store = make_engine(tmp_path, reader)
    engine.recall("alpha", "status")

    store.update_topic("alpha", "# Alpha\n\nNew canonical fact.\n")
    current = engine.recall("alpha", "status")

    assert current.answer == "Version two answer."
    assert len(reader.requests) == 2
    assert reader.requests[0].prefix != reader.requests[1].prefix
