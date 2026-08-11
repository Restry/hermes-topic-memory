from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hermes_topic_memory.cache import QuestionCache
from hermes_topic_memory.catalog import TopicCatalog
from hermes_topic_memory.query_history import (
    QueryHistoryStore,
    QueryRecord,
    TopicQueryResult,
)
from hermes_topic_memory.store import FileTopicStore

_BEIJING = ZoneInfo("Asia/Shanghai")
_FIXED_NOW = datetime(2026, 8, 11, 12, tzinfo=_BEIJING)


@pytest.fixture
def catalog_data(tmp_path: Path) -> Path:
    data_root = tmp_path / "data"
    store = FileTopicStore(data_root)
    store.create_topic(
        "alpha",
        "ALPHA_PRIVATE_BODY version one",
        display_name="Alpha",
        aliases=("a",),
        source_relative_path="alpha.md",
    )
    store.update_topic("alpha", "ALPHA_PRIVATE_BODY version two")
    store.create_topic(
        "beta",
        "BETA_PRIVATE_BODY",
        display_name="Beta",
        aliases=("b",),
        source_relative_path="/private/corpus/beta.md",
    )
    store.create_topic(
        "gamma",
        "GAMMA_PRIVATE_BODY",
        display_name="Gamma",
        aliases=("g",),
        source_relative_path="gamma.md",
    )
    (data_root / "topic-registry.json").write_text(
        json.dumps(
            {
                "topics": [
                    {
                        "topic_id": "alpha",
                        "display_name": "Alpha",
                        "aliases": ["a"],
                        "chat_ids": [],
                        "source_relative_path": "alpha.md",
                    },
                    {
                        "topic_id": "beta",
                        "display_name": "Beta",
                        "aliases": ["b"],
                        "chat_ids": [],
                        "source_relative_path": "/private/corpus/beta.md",
                    },
                    {
                        "topic_id": "gamma",
                        "display_name": "Gamma",
                        "aliases": ["g"],
                        "chat_ids": [],
                        "source_relative_path": "gamma.md",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cache = QuestionCache(data_root)
    cache.put(
        question="alpha current",
        answer="current alpha answer",
        topic_id="alpha",
        topic_version=2,
        model="test",
        prompt_version="v1",
    )
    cache.put(
        question="alpha stale",
        answer="stale alpha answer",
        topic_id="alpha",
        topic_version=1,
        model="test",
        prompt_version="v1",
    )
    cache.put(
        question="beta current",
        answer="current beta answer",
        topic_id="beta",
        topic_version=1,
        model="test",
        prompt_version="v1",
    )

    history = QueryHistoryStore(data_root)
    _append_query(
        history,
        query_id="q1",
        created_at=datetime(2026, 8, 10, 16, 30, tzinfo=UTC),
        topic_ids=("alpha",),
    )
    _append_query(
        history,
        query_id="q2",
        created_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
        topic_ids=("alpha", "beta"),
    )
    _append_query(
        history,
        query_id="q3",
        created_at=datetime(2026, 7, 12, 16, tzinfo=UTC),
        topic_ids=("gamma",),
    )
    _append_query(
        history,
        query_id="q4",
        created_at=datetime(2026, 7, 20, 3, tzinfo=UTC),
        topic_ids=("beta",),
    )
    _append_query(
        history,
        query_id="q5",
        created_at=datetime(2026, 7, 12, 15, 59, 59, tzinfo=UTC),
        topic_ids=("alpha",),
    )
    return data_root


def test_catalog_snapshot_counts_queries_topics_and_only_current_cache(
    catalog_data: Path,
) -> None:
    snapshot = TopicCatalog(catalog_data).snapshot(now=_FIXED_NOW)

    assert snapshot.active_topic_count == 3
    assert snapshot.total_question_count == 5
    assert snapshot.active_cache_count == 2
    assert snapshot.questions_last_30_days == 4
    assert snapshot.topic("alpha").question_count == 3
    assert snapshot.topic("alpha").active_cache_count == 1
    assert snapshot.topic("alpha").version == 2
    assert snapshot.topic("beta").question_count == 2
    assert snapshot.topic("beta").active_cache_count == 1
    assert snapshot.topic("gamma").question_count == 1
    assert snapshot.topic("gamma").active_cache_count == 0
    assert all(topic.status == "active" for topic in snapshot.topics)


def test_catalog_uses_beijing_calendar_days_and_zero_fills_exactly_30(
    catalog_data: Path,
) -> None:
    snapshot = TopicCatalog(catalog_data).snapshot(now=_FIXED_NOW)

    assert len(snapshot.daily_questions) == 30
    assert snapshot.daily_questions[0].day == "2026-07-13"
    assert snapshot.daily_questions[-1].day == "2026-08-11"
    counts = {item.day: item.question_count for item in snapshot.daily_questions}
    assert counts["2026-07-13"] == 1
    assert counts["2026-07-20"] == 1
    assert counts["2026-08-10"] == 1
    assert counts["2026-08-11"] == 1
    assert sum(counts.values()) == 4
    assert sum(count == 0 for count in counts.values()) == 26


def test_catalog_public_dict_has_no_bodies_or_absolute_private_paths(
    catalog_data: Path,
    tmp_path: Path,
) -> None:
    snapshot = TopicCatalog(catalog_data).snapshot(now=_FIXED_NOW)

    public = snapshot.to_public_dict()
    serialized = json.dumps(public, ensure_ascii=False)

    assert public["active_topic_count"] == 3
    assert public["total_question_count"] == 5
    assert len(public["topics"]) == 3
    assert public["topics"][0] == {
        "topic_id": "alpha",
        "display_name": "Alpha",
        "aliases": ["a"],
        "version": 2,
        "updated_at": snapshot.topic("alpha").updated_at,
        "status": "active",
        "question_count": 3,
        "active_cache_count": 1,
    }
    assert "ALPHA_PRIVATE_BODY" not in serialized
    assert "BETA_PRIVATE_BODY" not in serialized
    assert "GAMMA_PRIVATE_BODY" not in serialized
    assert "/private/corpus/beta.md" not in serialized
    assert str(tmp_path.resolve()) not in serialized
    assert "data_root" not in public
    assert all("source_relative_path" not in topic for topic in public["topics"])


def _append_query(
    history: QueryHistoryStore,
    *,
    query_id: str,
    created_at: datetime,
    topic_ids: tuple[str, ...],
) -> None:
    results = tuple(
        TopicQueryResult(
            topic_id=topic_id,
            topic_version=1,
            status="answered",
            answer=f"{topic_id} capsule",
            source="reader",
            match_kind=None,
            latency_ms=1,
        )
        for topic_id in topic_ids
    )
    history.append(
        QueryRecord.create(
            query_id=query_id,
            question=f"PRIVATE QUESTION {query_id}",
            created_at=created_at,
            selected_topics=results,
            final_answer=f"PRIVATE ANSWER {query_id}",
            selection_reasons={topic_id: "named" for topic_id in topic_ids},
            source="panel",
            session_ref=None,
            total_latency_ms=2,
        )
    )
