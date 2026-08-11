from __future__ import annotations

from pathlib import Path
import pytest

from hermes_topic_memory.cache import QuestionCache
from hermes_topic_memory.engine import RecallEngine
from hermes_topic_memory.multi_recall import MultiRecallEngine, TopicSelection
from hermes_topic_memory.query_history import QueryHistoryStore
from hermes_topic_memory.reader import ReaderRequest, ReaderResponse
from hermes_topic_memory.router import TopicRegistry
from hermes_topic_memory.store import FileTopicStore


ALPHA_BODY = """# Alpha

ALPHA_BODY_SENTINEL
Actual Alpha fact: the launch is Monday.
Distractor about Beta: the launch is Tuesday.
"""
BETA_BODY = """# Beta

BETA_BODY_SENTINEL
Actual Beta fact: the launch is Friday.
Distractor about Alpha: the launch is Sunday.
"""


class IsolationReader:
    def __init__(self) -> None:
        self.requests: list[ReaderRequest] = []
        self.topic_ids: list[str] = []

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.requests.append(request)
        if '"topic_id":"alpha"' in request.prefix:
            assert "ALPHA_BODY_SENTINEL" in request.prefix
            assert "BETA_BODY_SENTINEL" not in request.prefix
            self.topic_ids.append("alpha")
            return ReaderResponse(answer="answer from alpha: Monday", model="fake-reader")
        if '"topic_id":"beta"' in request.prefix:
            assert "BETA_BODY_SENTINEL" in request.prefix
            assert "ALPHA_BODY_SENTINEL" not in request.prefix
            self.topic_ids.append("beta")
            return ReaderResponse(answer="answer from beta: Friday", model="fake-reader")
        raise AssertionError("Reader request did not identify exactly one topic")


class RecordingSynthesizer:
    def __init__(self) -> None:
        self.last_prompt = ""

    def synthesize(self, request) -> ReaderResponse:
        self.last_prompt = request.prompt
        return ReaderResponse(
            answer="[Alpha/alpha] Monday; [Beta/beta] Friday.",
            model="fake-synthesizer",
        )


def make_multi_engine(
    tmp_path: Path,
) -> tuple[MultiRecallEngine, IsolationReader, RecordingSynthesizer]:
    store = FileTopicStore(tmp_path)
    store.create_topic("alpha", ALPHA_BODY, display_name="Alpha", aliases=("a",))
    store.create_topic("beta", BETA_BODY, display_name="Beta", aliases=("b",))
    registry = TopicRegistry.from_dict(
        {
            "topics": [
                {
                    "topic_id": "alpha",
                    "display_name": "Alpha",
                    "aliases": ["a"],
                    "chat_ids": [],
                },
                {
                    "topic_id": "beta",
                    "display_name": "Beta",
                    "aliases": ["b"],
                    "chat_ids": [],
                },
            ]
        }
    )
    reader = IsolationReader()
    synthesizer = RecordingSynthesizer()
    single = RecallEngine(
        store=store,
        cache=QuestionCache(tmp_path, jaccard_threshold=1.0),
        reader=reader,
    )
    return (
        MultiRecallEngine(
            recall_engine=single,
            registry=registry,
            synthesizer=synthesizer,
            query_history=QueryHistoryStore(tmp_path),
        ),
        reader,
        synthesizer,
    )


def test_multi_recall_isolates_reader_bodies_and_synthesizes_capsules_only(
    tmp_path: Path,
) -> None:
    engine, reader, synthesizer = make_multi_engine(tmp_path)

    result = engine.recall_multi(
        question="What do Alpha and Beta say?",
        selections=(
            TopicSelection("alpha", "named in question"),
            TopicSelection("beta", "named in question"),
        ),
        source="cli",
    )

    assert [item.topic_id for item in result.topic_results] == ["alpha", "beta"]
    assert reader.topic_ids == ["alpha", "beta"]
    assert "ALPHA_BODY_SENTINEL" not in synthesizer.last_prompt
    assert "BETA_BODY_SENTINEL" not in synthesizer.last_prompt
    assert "answer from alpha" in synthesizer.last_prompt
    assert "answer from beta" in synthesizer.last_prompt
    assert result.final_answer == "[Alpha/alpha] Monday; [Beta/beta] Friday."


class ScenarioReader:
    def __init__(self, outcomes: dict[str, object]) -> None:
        self.outcomes = outcomes
        self.requests: list[ReaderRequest] = []
        self.topic_ids: list[str] = []

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.requests.append(request)
        topic_id = next(
            (
                candidate
                for candidate in ("alpha", "beta")
                if f'"topic_id":"{candidate}"' in request.prefix
            ),
            None,
        )
        if topic_id is None:
            raise AssertionError("Reader request did not identify one scenario topic")
        self.topic_ids.append(topic_id)
        outcome = self.outcomes[topic_id]
        if isinstance(outcome, list):
            outcome = outcome.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return ReaderResponse(answer=str(outcome), model="scenario-reader")


class SequenceSynthesizer:
    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.requests = []

    def synthesize(self, request) -> ReaderResponse:
        self.requests.append(request)
        return ReaderResponse(
            answer=self.answers[len(self.requests) - 1],
            model="scenario-synthesizer",
        )


def build_scenario_engine(
    tmp_path: Path,
    *,
    outcomes: dict[str, object] | None = None,
    synthesis_answers: list[str] | None = None,
    registry_value: dict | None = None,
    bodies: dict[str, str] | None = None,
    cache: QuestionCache | None = None,
) -> tuple[
    MultiRecallEngine,
    ScenarioReader,
    SequenceSynthesizer,
    FileTopicStore,
    QuestionCache,
]:
    topic_bodies = bodies or {"alpha": ALPHA_BODY, "beta": BETA_BODY}
    store = FileTopicStore(tmp_path)
    for topic_id, body in topic_bodies.items():
        store.create_topic(
            topic_id,
            body,
            display_name=topic_id.title(),
            aliases=(topic_id[0],),
        )
    raw_registry = registry_value or {
        "topics": [
            {
                "topic_id": "alpha",
                "display_name": "Alpha",
                "aliases": ["a"],
                "chat_ids": [],
            },
            {
                "topic_id": "beta",
                "display_name": "Beta",
                "aliases": ["b"],
                "chat_ids": [],
            },
        ]
    }
    reader = ScenarioReader(
        outcomes
        or {
            "alpha": "answer from alpha: Monday",
            "beta": "answer from beta: Friday",
        }
    )
    synthesizer = SequenceSynthesizer(
        synthesis_answers
        or ["[Alpha/alpha] Monday; [Beta/beta] Friday."]
    )
    active_cache = cache or QuestionCache(tmp_path, jaccard_threshold=1.0)
    single = RecallEngine(store=store, cache=active_cache, reader=reader)
    multi = MultiRecallEngine(
        recall_engine=single,
        registry=TopicRegistry.from_dict(raw_registry),
        synthesizer=synthesizer,
        query_history=QueryHistoryStore(tmp_path),
    )
    return multi, reader, synthesizer, store, active_cache


def test_selection_count_rejects_zero_and_nine_before_reader_calls(tmp_path: Path) -> None:
    engine, reader, _synthesizer, _store, _cache = build_scenario_engine(tmp_path)

    with pytest.raises(ValueError, match="1 to 8"):
        engine.recall_multi(question="compare", selections=(), source="cli")
    with pytest.raises(ValueError, match="1 to 8"):
        engine.recall_multi(
            question="compare",
            selections=tuple(TopicSelection("alpha", f"reason {index}") for index in range(9)),
            source="cli",
        )

    assert reader.requests == []


def test_duplicate_aliases_collapse_by_topic_id_preserving_first_order_and_reason(
    tmp_path: Path,
) -> None:
    engine, reader, _synthesizer, _store, _cache = build_scenario_engine(tmp_path)

    result = engine.recall_multi(
        question="compare",
        selections=(
            TopicSelection("a", "first alpha reason"),
            TopicSelection("alpha", "duplicate reason ignored"),
            TopicSelection("b", "beta reason"),
        ),
        source="cli",
    )

    assert [item.topic_id for item in result.topic_results] == ["alpha", "beta"]
    assert reader.topic_ids == ["alpha", "beta"]
    record = QueryHistoryStore(tmp_path).get(result.query_id)
    assert record is not None
    assert dict(record.selection_reasons) == {
        "alpha": "first alpha reason",
        "beta": "beta reason",
    }


def test_unknown_or_conflicting_selection_fails_all_before_any_reader_call(
    tmp_path: Path,
) -> None:
    engine, reader, _synthesizer, _store, _cache = build_scenario_engine(tmp_path / "unknown")

    with pytest.raises(ValueError, match="unknown"):
        engine.recall_multi(
            question="compare",
            selections=(
                TopicSelection("alpha", "valid first"),
                TopicSelection("missing", "unknown second"),
            ),
            source="cli",
        )
    assert reader.requests == []

    conflicting_registry = {
        "topics": [
            {
                "topic_id": "alpha",
                "display_name": "Shared",
                "aliases": ["a"],
                "chat_ids": [],
            },
            {
                "topic_id": "beta",
                "display_name": "Shared",
                "aliases": ["b"],
                "chat_ids": [],
            },
        ]
    }
    conflict_engine, conflict_reader, *_ = build_scenario_engine(
        tmp_path / "conflict", registry_value=conflicting_registry
    )
    with pytest.raises(ValueError, match="ambiguous"):
        conflict_engine.recall_multi(
            question="compare",
            selections=(TopicSelection("Shared", "conflicting display name"),),
            source="cli",
        )
    assert conflict_reader.requests == []


def test_multi_recall_mixes_topic_version_isolated_cache_hit_and_reader_miss(
    tmp_path: Path,
) -> None:
    cache = QuestionCache(tmp_path, jaccard_threshold=1.0)
    question = "compare current status"
    cache.put(
        question=question,
        answer="cached answer from alpha",
        topic_id="alpha",
        topic_version=1,
        model="old-reader",
        prompt_version="v1",
    )
    engine, reader, synthesizer, _store, _cache = build_scenario_engine(
        tmp_path,
        cache=cache,
        synthesis_answers=["[Alpha/alpha] cached; [Beta/beta] Friday."],
    )

    result = engine.recall_multi(
        question=question,
        selections=(
            TopicSelection("alpha", "needed"),
            TopicSelection("beta", "needed"),
        ),
        source="cli",
    )

    assert [item.source for item in result.topic_results] == ["cache", "reader"]
    assert result.topic_results[0].match_kind == "exact"
    assert reader.topic_ids == ["beta"]
    assert "cached answer from alpha" in synthesizer.requests[0].prompt


def test_no_fact_is_labeled_without_erasing_an_answer(tmp_path: Path) -> None:
    engine, _reader, synthesizer, _store, _cache = build_scenario_engine(
        tmp_path,
        outcomes={
            "alpha": "answer from alpha: Monday",
            "beta": "No stored fact answers the question.",
        },
        synthesis_answers=["[Alpha/alpha] Monday; [Beta/beta] no_fact."],
    )

    result = engine.recall_multi(
        question="compare",
        selections=(
            TopicSelection("alpha", "needed"),
            TopicSelection("beta", "needed"),
        ),
        source="cli",
    )

    assert [item.status for item in result.topic_results] == ["answered", "no_fact"]
    assert result.topic_results[1].answer is None
    assert "STATUS:no_fact" in synthesizer.requests[0].prompt
    assert result.final_answer == "[Alpha/alpha] Monday; [Beta/beta] no_fact."


def test_operational_error_is_labeled_and_partial_synthesis_discloses_it(
    tmp_path: Path,
) -> None:
    engine, _reader, synthesizer, _store, _cache = build_scenario_engine(
        tmp_path,
        outcomes={
            "alpha": "answer from alpha: Monday",
            "beta": RuntimeError("private backend detail"),
        },
        synthesis_answers=["[Alpha/alpha] Monday; [Beta/beta] error."],
    )

    result = engine.recall_multi(
        question="compare",
        selections=(
            TopicSelection("alpha", "needed"),
            TopicSelection("beta", "needed"),
        ),
        source="cli",
    )

    assert [item.status for item in result.topic_results] == ["answered", "error"]
    assert result.topic_results[1].failure is not None
    assert result.topic_results[1].failure.code == "recall_error"
    assert "private backend detail" not in synthesizer.requests[0].prompt
    assert "STATUS:error" in synthesizer.requests[0].prompt
    assert result.final_answer == "[Alpha/alpha] Monday; [Beta/beta] error."


def test_all_operational_failures_skip_synthesis_and_still_record_statuses(
    tmp_path: Path,
) -> None:
    engine, _reader, synthesizer, _store, _cache = build_scenario_engine(
        tmp_path,
        outcomes={"alpha": RuntimeError("alpha down"), "beta": OSError("beta down")},
    )

    result = engine.recall_multi(
        question="compare",
        selections=(
            TopicSelection("alpha", "needed"),
            TopicSelection("beta", "needed"),
        ),
        source="cli",
    )

    assert result.final_answer is None
    assert result.synthesis_source == "none"
    assert result.failure is not None
    assert result.failure.code == "no_answered_topics"
    assert synthesizer.requests == []
    record = QueryHistoryStore(tmp_path).get(result.query_id)
    assert record is not None
    assert [item.status for item in record.selected_topics] == ["error", "error"]


def test_participating_topic_version_change_changes_scope_fingerprint(
    tmp_path: Path,
) -> None:
    engine, _reader, _synthesizer, store, _cache = build_scenario_engine(
        tmp_path,
        synthesis_answers=[
            "[Alpha/alpha] Monday; [Beta/beta] Friday.",
            "[Alpha/alpha] Monday; [Beta/beta] Friday.",
        ],
    )
    selections = (
        TopicSelection("alpha", "needed"),
        TopicSelection("beta", "needed"),
    )

    first = engine.recall_multi(question="compare", selections=selections, source="cli")
    store.update_topic("beta", BETA_BODY + "Version two fact.\n")
    second = engine.recall_multi(question="compare", selections=selections, source="cli")

    assert first.scope_fingerprint != second.scope_fingerprint
    assert [item.topic_version for item in first.topic_results] == [1, 1]
    assert [item.topic_version for item in second.topic_results] == [1, 2]


def test_multi_recall_appends_one_complete_query_record(tmp_path: Path) -> None:
    engine, _reader, _synthesizer, _store, _cache = build_scenario_engine(tmp_path)

    result = engine.recall_multi(
        question="compare",
        selections=(
            TopicSelection("alpha", "alpha named"),
            TopicSelection("beta", "beta named"),
        ),
        source="hermes",
        session_ref="raw-session-id",
    )

    records = QueryHistoryStore(tmp_path).list()
    assert len(records) == 1
    record = records[0]
    assert result.query_id == record.query_id
    assert record.question == "compare"
    assert [item.topic_id for item in record.selected_topics] == ["alpha", "beta"]
    assert [item.topic_version for item in record.selected_topics] == [1, 1]
    assert [item.status for item in record.selected_topics] == ["answered", "answered"]
    assert [item.source for item in record.selected_topics] == ["reader", "reader"]
    assert all(item.latency_ms >= 0 for item in record.selected_topics)
    assert record.final_answer == result.final_answer
    assert dict(record.selection_reasons) == {
        "alpha": "alpha named",
        "beta": "beta named",
    }
    assert record.source == "hermes"
    assert record.session_ref != "raw-session-id"
    assert record.session_ref is not None and len(record.session_ref) == 64
    assert record.total_latency_ms >= 0


def test_synthesis_answer_is_corrected_once_for_length_and_attribution(
    tmp_path: Path,
) -> None:
    engine, _reader, synthesizer, _store, _cache = build_scenario_engine(
        tmp_path,
        synthesis_answers=["x" * 201, "[Alpha/alpha] corrected."],
    )

    result = engine.recall_multi(
        question="alpha only",
        selections=(TopicSelection("alpha", "needed"),),
        source="cli",
    )

    assert result.final_answer == "[Alpha/alpha] corrected."
    assert result.failure is None
    assert len(synthesizer.requests) == 2
    assert synthesizer.requests[1].correction_of == "x" * 201

    failing_engine, _reader2, failing_synthesizer, *_ = build_scenario_engine(
        tmp_path / "failing",
        synthesis_answers=["unattributed", "still unattributed"],
    )
    failed = failing_engine.recall_multi(
        question="alpha only",
        selections=(TopicSelection("alpha", "needed"),),
        source="cli",
    )
    assert failed.final_answer is None
    assert failed.failure is not None
    assert failed.failure.code == "synthesis_missing_attribution"
    assert len(failing_synthesizer.requests) == 2


def test_exact_temporal_capsules_remain_isolated_per_selected_topic(tmp_path: Path) -> None:
    bodies = {
        "alpha": "# Alpha\n\n## 2026年6月28日\n- **16:30** ALPHA_TEMPORAL_EXACT.\n",
        "beta": "# Beta\n\n## 2026年6月28日\n- **16:30** BETA_TEMPORAL_EXACT.\n",
    }
    engine, reader, _synthesizer, _store, _cache = build_scenario_engine(
        tmp_path,
        bodies=bodies,
        outcomes={"alpha": "alpha exact", "beta": "beta exact"},
        synthesis_answers=["[Alpha/alpha] exact; [Beta/beta] exact."],
    )

    result = engine.recall_multi(
        question="Alpha 和 Beta 在2026年6月28日16:30各自发生了什么？",
        selections=(
            TopicSelection("alpha", "named"),
            TopicSelection("beta", "named"),
        ),
        source="cli",
    )

    assert result.final_answer == "[Alpha/alpha] exact; [Beta/beta] exact."
    assert "ALPHA_TEMPORAL_EXACT" in (reader.requests[0].evidence_capsule or "")
    assert "BETA_TEMPORAL_EXACT" not in (reader.requests[0].evidence_capsule or "")
    assert "BETA_TEMPORAL_EXACT" in (reader.requests[1].evidence_capsule or "")
    assert "ALPHA_TEMPORAL_EXACT" not in (reader.requests[1].evidence_capsule or "")
