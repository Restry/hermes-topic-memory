from __future__ import annotations

from pathlib import Path

import pytest

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


TEMPORAL_BODY = """# Project Archive

## 2026年6月27日

### Phase 2

- **16:30** Previous-day event.

## 2026年6月28日

### Phase 3

- **16:14** Nearby setup.
- **16:15** Nearby rule.
- **16:30** Exact completion.
- **16:45** Later note.
"""


def make_temporal_engine(
    tmp_path: Path, reader: SequenceReader
) -> tuple[RecallEngine, QuestionCache]:
    store = FileTopicStore(tmp_path)
    store.create_topic("alpha", TEMPORAL_BODY, display_name="Alpha")
    cache = QuestionCache(tmp_path, jaccard_threshold=0.8)
    return RecallEngine(store=store, cache=cache, reader=reader), cache


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


@pytest.mark.parametrize(
    "question",
    [
        "6月28日下午4:30做了什么？",
        "6月28日下午 4:30做了什么？",
        "6月28日16:30做了什么？",
        "6月28日四点半做了什么？",
    ],
)
def test_exact_temporal_forms_select_only_the_1630_record_with_heading_context(
    tmp_path: Path, question: str
) -> None:
    reader = SequenceReader(["Exact completion."])
    engine, _cache = make_temporal_engine(tmp_path, reader)

    result = engine.recall("alpha", question)

    assert result.answer == "Exact completion."
    capsule = reader.requests[0].evidence_capsule
    assert capsule is not None
    assert "DATE HEADING: ## 2026年6月28日" in capsule
    assert "SECTION HEADING: ### Phase 3" in capsule
    assert "[EXACT] - **16:30** Exact completion." in capsule
    assert "[CONTEXT] - **16:14** Nearby setup." in capsule
    assert "[EXACT] - **16:14**" not in capsule
    assert "Previous-day event" not in capsule
    assert "AUTHORITATIVE" in reader.requests[0].prompt
    assert "must never replace an [EXACT] timestamp record" in reader.requests[0].prompt


def test_non_temporal_question_preserves_full_topic_reader_behavior(tmp_path: Path) -> None:
    reader = SequenceReader(["Archive answer."])
    engine, _cache = make_temporal_engine(tmp_path, reader)

    engine.recall("alpha", "What happened in this archive?")

    request = reader.requests[0]
    assert request.evidence_capsule is None
    assert TEMPORAL_BODY in request.prefix


def test_temporal_recall_bypasses_fuzzy_cache_entry_for_rewritten_date(
    tmp_path: Path,
) -> None:
    reader = SequenceReader(["Exact completion."])
    engine, cache = make_temporal_engine(tmp_path, reader)
    rewritten = "项目2024年6月28日下午4:30做了什么"
    raw = "项目6月28日下午4:30做了什么"
    cache.put(
        question=rewritten,
        answer="Nearby setup at 16:14.",
        topic_id="alpha",
        topic_version=1,
        model="old-reader",
        prompt_version="v1",
    )
    assert cache.lookup(raw, topic_id="alpha", topic_version=1) is not None

    result = engine.recall("alpha", raw)

    assert result.answer == "Exact completion."
    assert result.source == "reader"
    assert len(reader.requests) == 1
    assert cache.records("alpha")[-1].question == raw