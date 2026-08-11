from __future__ import annotations

import dataclasses
import hashlib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

import hermes_topic_memory.query_history as query_history
from hermes_topic_memory.query_history import (
    QueryHistoryCorruptionError,
    QueryHistoryStore,
    QueryRecord,
    TopicQueryResult,
)


def _record(
    *,
    query_id: str = "query-1",
    created_at: datetime = datetime(2026, 8, 11, 8, tzinfo=UTC),
    question: str = "compare alpha and beta",
    topic_ids: tuple[str, ...] = ("alpha", "beta"),
    session_ref: str | None = "raw-session-id",
) -> QueryRecord:
    selected_topics = tuple(
        TopicQueryResult(
            topic_id=topic_id,
            topic_version=index + 2,
            status="answered" if topic_id == "alpha" else "no_fact",
            answer="A" if topic_id == "alpha" else None,
            source="cache" if topic_id == "alpha" else "reader",
            match_kind="exact" if topic_id == "alpha" else None,
            latency_ms=index + 3,
        )
        for index, topic_id in enumerate(topic_ids)
    )
    return QueryRecord.create(
        query_id=query_id,
        question=question,
        created_at=created_at,
        selected_topics=selected_topics,
        final_answer="A only",
        selection_reasons={topic_id: "named" for topic_id in topic_ids},
        source="panel",
        session_ref=session_ref,
        total_latency_ms=14,
    )


def test_query_record_round_trip_preserves_topic_versions_and_statuses(
    tmp_path: Path,
) -> None:
    store = QueryHistoryStore(tmp_path)
    record = _record()

    saved = store.append(record)

    assert saved == record
    assert store.get(saved.query_id) == saved
    assert store.list(topic_id="beta") == (saved,)
    assert saved.selected_topics == (
        TopicQueryResult("alpha", 2, "answered", "A", "cache", "exact", 3),
        TopicQueryResult("beta", 3, "no_fact", None, "reader", None, 4),
    )


def test_append_uses_utc_month_compact_mode_0600_lock_and_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flock_operations: list[int] = []
    fsynced_descriptors: list[int] = []
    real_flock = query_history.fcntl.flock
    real_fsync = query_history.os.fsync

    def tracking_flock(descriptor: int, operation: int) -> None:
        flock_operations.append(operation)
        real_flock(descriptor, operation)

    def tracking_fsync(descriptor: int) -> None:
        fsynced_descriptors.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(query_history.fcntl, "flock", tracking_flock)
    monkeypatch.setattr(query_history.os, "fsync", tracking_fsync)
    store = QueryHistoryStore(tmp_path)

    store.append(
        _record(created_at=datetime(2026, 8, 31, 23, 59, tzinfo=UTC))
    )

    month_path = tmp_path / "query-history" / "2026-08.jsonl"
    payload = month_path.read_bytes()
    assert month_path.is_file()
    assert stat.S_IMODE(month_path.stat().st_mode) == 0o600
    assert payload.endswith(b"\n")
    assert payload.count(b"\n") == 1
    assert b": " not in payload
    assert flock_operations == [query_history.fcntl.LOCK_EX, query_history.fcntl.LOCK_UN]
    assert len(fsynced_descriptors) >= 2


def test_append_is_append_only(tmp_path: Path) -> None:
    store = QueryHistoryStore(tmp_path)
    store.append(_record(query_id="first"))
    month_path = tmp_path / "query-history" / "2026-08.jsonl"
    first_payload = month_path.read_bytes()

    store.append(
        _record(
            query_id="second",
            created_at=datetime(2026, 8, 11, 9, tzinfo=UTC),
        )
    )

    final_payload = month_path.read_bytes()
    assert final_payload.startswith(first_payload)
    assert final_payload.count(b"\n") == 2


def test_list_paginates_newest_first_deterministically(tmp_path: Path) -> None:
    store = QueryHistoryStore(tmp_path)
    oldest = store.append(
        _record(
            query_id="oldest",
            created_at=datetime(2026, 7, 31, 23, 59, tzinfo=UTC),
            topic_ids=("alpha",),
        )
    )
    middle = store.append(
        _record(
            query_id="middle",
            created_at=datetime(2026, 8, 1, 0, tzinfo=UTC),
            topic_ids=("beta",),
        )
    )
    newest = store.append(
        _record(
            query_id="newest",
            created_at=datetime(2026, 9, 1, 0, tzinfo=UTC),
            topic_ids=("alpha",),
        )
    )

    assert store.list() == (newest, middle, oldest)
    assert store.list(limit=2) == (newest, middle)
    assert store.list(limit=1, offset=1) == (middle,)
    assert store.list(topic_id="alpha") == (newest, oldest)
    assert store.list(limit=2) == (newest, middle)


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: _record(question=" \n\t"),
            "question must contain",
        ),
        (
            lambda: QueryRecord.create(
                question="question",
                selected_topics=(
                    TopicQueryResult("Bad/Topic", 1, "answered", "A", "reader", None, 1),
                ),
                final_answer="A",
                selection_reasons={"Bad/Topic": "named"},
                source="cli",
                session_ref=None,
                total_latency_ms=1,
            ),
            "invalid topic id",
        ),
        (
            lambda: QueryRecord.create(
                question="question",
                selected_topics=(
                    TopicQueryResult("alpha", 1, "answered", "A", "reader", None, 1),
                    TopicQueryResult("alpha", 1, "answered", "A", "reader", None, 1),
                ),
                final_answer="A",
                selection_reasons={"alpha": "named"},
                source="cli",
                session_ref=None,
                total_latency_ms=1,
            ),
            "duplicate topic id",
        ),
        (
            lambda: TopicQueryResult(
                "alpha", 1, "answered", "x" * 201, "reader", None, 1
            ),
            "answer must contain",
        ),
        (
            lambda: QueryRecord.create(
                question="question",
                selected_topics=(
                    TopicQueryResult("alpha", 1, "answered", "A", "reader", None, 1),
                ),
                final_answer="x" * 201,
                selection_reasons={"alpha": "named"},
                source="cli",
                session_ref=None,
                total_latency_ms=1,
            ),
            "final answer must contain",
        ),
    ],
)
def test_invalid_or_body_sized_fields_fail(factory: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()  # type: ignore[operator]


def test_raw_session_identifier_is_hashed_before_persistence(tmp_path: Path) -> None:
    store = QueryHistoryStore(tmp_path)
    raw_session_ref = "private-session-cookie-value"

    saved = store.append(_record(session_ref=raw_session_ref))

    expected_hash = hashlib.sha256(raw_session_ref.encode("utf-8")).hexdigest()
    assert saved.session_ref == expected_hash
    assert raw_session_ref.encode("utf-8") not in (
        tmp_path / "query-history" / "2026-08.jsonl"
    ).read_bytes()

    unsafe_record = dataclasses.replace(saved, query_id="unsafe", session_ref="raw")
    with pytest.raises(ValueError, match="session reference must be hashed"):
        store.append(unsafe_record)


def test_malformed_jsonl_error_names_file_and_line_without_content(
    tmp_path: Path,
) -> None:
    history_dir = tmp_path / "query-history"
    history_dir.mkdir()
    month_path = history_dir / "2026-08.jsonl"
    neighbor = _record(
        query_id="neighbor",
        question="TOP-SECRET-NEIGHBOR-QUESTION",
        session_ref=None,
    )
    valid_line = json.dumps(
        neighbor.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    month_path.write_text(
        valid_line + "\n" + '{"malformed":"DO-NOT-LEAK"' + "\n" + valid_line + "\n",
        encoding="utf-8",
    )

    with pytest.raises(QueryHistoryCorruptionError) as caught:
        QueryHistoryStore(tmp_path).list()

    assert str(caught.value) == "invalid query history record 2026-08.jsonl:2"
    assert "DO-NOT-LEAK" not in str(caught.value)
    assert "TOP-SECRET-NEIGHBOR-QUESTION" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
