from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hermes_topic_memory.cache import QuestionCache
from hermes_topic_memory.catalog import TopicCatalog
from hermes_topic_memory.query_history import (
    QueryHistoryStore,
    QueryRecord,
    TopicQueryResult,
    QueryHistoryCorruptionError,
)
from hermes_topic_memory.router import TopicRegistry
from hermes_topic_memory.store import FileTopicStore, TopicCorruptionError
from hermes_topic_memory.topic_admin import TopicAdminService

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
    assert snapshot.total_chars == sum(
        len(FileTopicStore(catalog_data).get_topic(topic.topic_id).body)
        for topic in snapshot.topics
    )
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
    assert public["total_chars"] == snapshot.total_chars
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
        "chars": len("ALPHA_PRIVATE_BODY version two"),
    }
    assert "ALPHA_PRIVATE_BODY" not in serialized
    assert "BETA_PRIVATE_BODY" not in serialized
    assert "GAMMA_PRIVATE_BODY" not in serialized
    assert "/private/corpus/beta.md" not in serialized
    assert str(tmp_path.resolve()) not in serialized
    assert "data_root" not in public
    assert all("source_relative_path" not in topic for topic in public["topics"])


def test_catalog_counts_active_topics_and_reports_archived_status(
    catalog_data: Path,
) -> None:
    registry_path = catalog_data / "topic-registry.json"
    value = json.loads(registry_path.read_text(encoding="utf-8"))
    value["topics"][1]["state"] = "archived"
    registry_path.write_text(json.dumps(value), encoding="utf-8")
    FileTopicStore(catalog_data).set_topic_state(
        "beta", "archived", expected_state="active"
    )

    snapshot = TopicCatalog(catalog_data).snapshot(now=_FIXED_NOW)

    assert snapshot.active_topic_count == 2
    assert snapshot.topic("alpha").status == "active"
    assert snapshot.topic("beta").status == "archived"
    assert snapshot.total_chars == sum(topic.chars for topic in snapshot.topics)


def test_catalog_chars_track_current_unicode_bodies_and_exclude_trash(
    tmp_path: Path,
) -> None:
    admin = TopicAdminService(tmp_path)
    active_body = "中文记忆\n😀 active\n"
    archived_body = "归档内容\n"
    admin.create_topic("active", "Active", (), active_body)
    admin.create_topic("archived", "Archived", (), archived_body)
    admin.archive("archived", expected_version=1)
    admin.create_topic("discarded", "Discarded", (), "not retained\n")
    admin.trash("discarded", expected_version=1)

    snapshot = TopicCatalog(tmp_path).snapshot(now=_FIXED_NOW)
    assert snapshot.active_topic_count == 1
    assert snapshot.topic("active").to_public_dict()["chars"] == len(active_body)
    assert snapshot.topic("archived").chars == len(archived_body)
    assert snapshot.total_chars == len(active_body) + len(archived_body)
    assert snapshot.to_public_dict()["total_chars"] == snapshot.total_chars
    assert {topic.topic_id for topic in snapshot.topics} == {"active", "archived"}

    updated_body = active_body + "追加 durable fact\n"
    FileTopicStore(tmp_path).update_topic("active", updated_body)
    updated = TopicCatalog(tmp_path).snapshot(now=_FIXED_NOW)
    assert updated.topic("active").chars == len(updated_body)
    assert updated.total_chars == len(updated_body) + len(archived_body)

    trashed_active = admin.trash("active", expected_version=2)
    retained = TopicCatalog(tmp_path).snapshot(now=_FIXED_NOW)
    assert retained.total_chars == len(archived_body)
    admin.restore_from_trash(
        "active", expected_trashed_at=trashed_active.trashed_at
    )
    assert TopicCatalog(tmp_path).snapshot(now=_FIXED_NOW).total_chars == (
        len(updated_body) + len(archived_body)
    )

    admin.trash("active", expected_version=2)
    admin.trash("archived", expected_version=1)
    empty = TopicCatalog(tmp_path).snapshot(now=_FIXED_NOW)
    assert empty.total_chars == 0
    assert empty.active_topic_count == 0
    assert empty.to_public_dict()["topics"] == []



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


def _write_changed(path: Path, content: str | bytes) -> None:
    previous = path.stat()
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))


def test_catalog_cache_hits_across_instances_without_decoding(
    catalog_data: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = TopicCatalog(catalog_data).snapshot(now=_FIXED_NOW)
    alias = tmp_path / "data-alias"
    alias.symlink_to(catalog_data, target_is_directory=True)
    validations = []
    validate = TopicCatalog._validate_topic_directories

    def validate_directories(self, registry):
        validations.append(registry)
        validate(self, registry)

    def unexpected_decode(*args, **kwargs):
        pytest.fail("unchanged snapshot must not decode or rebuild its inputs")

    monkeypatch.setattr(TopicCatalog, "_validate_topic_directories", validate_directories)
    monkeypatch.setattr(TopicCatalog, "_build_snapshot", unexpected_decode)
    monkeypatch.setattr(TopicCatalog, "_read_metadata", unexpected_decode)
    monkeypatch.setattr(TopicCatalog, "_canonical_size", unexpected_decode)
    monkeypatch.setattr(QueryHistoryStore, "list", unexpected_decode)
    monkeypatch.setattr(QuestionCache, "records", unexpected_decode)
    monkeypatch.setattr(TopicRegistry, "from_file", unexpected_decode)

    assert TopicCatalog(alias).snapshot(now=_FIXED_NOW) is snapshot
    assert len(validations) == 1


def test_catalog_cache_invalidates_query_append_rewrite_and_delete(
    catalog_data: Path,
) -> None:
    catalog = TopicCatalog(catalog_data)
    initial = catalog.snapshot(now=_FIXED_NOW)
    _append_query(
        QueryHistoryStore(catalog_data),
        query_id="new-query",
        created_at=_FIXED_NOW,
        topic_ids=("alpha",),
    )
    appended = catalog.snapshot(now=_FIXED_NOW)
    assert appended.total_question_count == initial.total_question_count + 1
    assert appended.topic("alpha").question_count == 4

    path = catalog_data / "query-history" / "2026-08.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    record = next(record for record in records if record["query_id"] == "new-query")
    record["selected_topics"][0]["topic_id"] = "gamma"
    record["selection_reasons"] = {"gamma": "named"}
    _write_changed(path, "".join(json.dumps(record) + "\n" for record in records))
    rewritten = catalog.snapshot(now=_FIXED_NOW)
    assert rewritten.topic("alpha").question_count == 3
    assert rewritten.topic("gamma").question_count == 2

    path.unlink()
    removed = catalog.snapshot(now=_FIXED_NOW)
    assert removed.total_question_count == 3
    assert removed.questions_last_30_days == 2


def test_catalog_cache_invalidates_cache_append_rewrite_and_delete(
    catalog_data: Path,
) -> None:
    catalog = TopicCatalog(catalog_data)
    initial = catalog.snapshot(now=_FIXED_NOW)
    QuestionCache(catalog_data).put(
        question="another alpha question",
        answer="another answer",
        topic_id="alpha",
        topic_version=2,
        model="test",
        prompt_version="v1",
    )
    appended = catalog.snapshot(now=_FIXED_NOW)
    assert appended.active_cache_count == initial.active_cache_count + 1
    path = catalog_data / "qa-cache" / "alpha.jsonl"
    # Same-size, in-place version changes must be visible without directory changes.
    content = path.read_text(encoding="utf-8")
    _write_changed(path, content.replace('"topic_version":2', '"topic_version":1'))
    assert catalog.snapshot(now=_FIXED_NOW).topic("alpha").active_cache_count == 0
    path.unlink()
    assert catalog.snapshot(now=_FIXED_NOW).active_cache_count == 1


def test_catalog_cache_invalidates_body_registry_metadata_and_versions(
    catalog_data: Path,
) -> None:
    catalog = TopicCatalog(catalog_data)
    initial = catalog.snapshot(now=_FIXED_NOW)
    body = "更新后的中文正文\n😀\n"
    FileTopicStore(catalog_data).update_topic("alpha", body)
    updated = catalog.snapshot(now=_FIXED_NOW)
    assert updated.topic("alpha").chars == len(body)
    assert updated.topic("alpha").version == 3
    assert updated.topic("alpha").active_cache_count == 0
    assert updated.total_chars == initial.total_chars - initial.topic("alpha").chars + len(body)

    registry = catalog_data / "topic-registry.json"
    content = registry.read_text(encoding="utf-8").replace('"Alpha"', '"AlphX"')
    _write_changed(registry, content)
    assert catalog.snapshot(now=_FIXED_NOW).topic("alpha").display_name == "AlphX"
    metadata = catalog_data / "topics" / "alpha" / "metadata.json"
    value = json.loads(metadata.read_text(encoding="utf-8"))
    value["imported_at"] = "2026-08-11T00:00:00Z"
    _write_changed(metadata, json.dumps(value))
    assert catalog.snapshot(now=_FIXED_NOW).topic("alpha").imported_at == value["imported_at"]

    version = catalog_data / "topics" / "alpha" / "versions" / "7-memory.md"
    version.write_text("historical content", encoding="utf-8")
    assert catalog.snapshot(now=_FIXED_NOW).topic("alpha").version_count == 4
    version.unlink()
    assert catalog.snapshot(now=_FIXED_NOW).topic("alpha").version_count == 3


def test_catalog_cache_invalidates_archive_trash_and_both_restores(
    catalog_data: Path,
) -> None:
    catalog = TopicCatalog(catalog_data)
    initial = catalog.snapshot(now=_FIXED_NOW)
    admin = TopicAdminService(catalog_data)
    admin.archive("alpha", expected_version=2)
    archived = catalog.snapshot(now=_FIXED_NOW)
    assert archived.topic("alpha").status == "archived"
    assert archived.active_topic_count == 2
    admin.restore("alpha")
    assert catalog.snapshot(now=_FIXED_NOW).topic("alpha").status == "active"
    trashed = admin.trash("alpha", expected_version=2)
    absent = catalog.snapshot(now=_FIXED_NOW)
    assert {topic.topic_id for topic in absent.topics} == {"beta", "gamma"}
    assert absent.total_chars == initial.total_chars - initial.topic("alpha").chars
    admin.restore_from_trash("alpha", expected_trashed_at=trashed.trashed_at)
    restored = catalog.snapshot(now=_FIXED_NOW)
    assert restored.total_chars == initial.total_chars
    assert restored.active_cache_count == initial.active_cache_count


def test_catalog_cache_rolls_over_at_beijing_midnight(
    catalog_data: Path,
) -> None:
    catalog = TopicCatalog(catalog_data)
    before = datetime(2026, 8, 11, 15, 59, 59, tzinfo=UTC)
    same_day = catalog.snapshot(now=_FIXED_NOW)
    assert catalog.snapshot(now=before) is same_day
    after = catalog.snapshot(now=datetime(2026, 8, 11, 16, tzinfo=UTC))
    assert after.daily_questions[-1].day == "2026-08-12"
    assert after.daily_questions[0].day == "2026-07-14"
    assert after.questions_last_30_days == same_day.questions_last_30_days - 1
    with pytest.raises(ValueError, match="timezone-aware"):
        catalog.snapshot(now=datetime(2026, 8, 11))


def test_catalog_cache_isolates_topic_scopes_and_mixed_queries(
    catalog_data: Path,
) -> None:
    catalog = TopicCatalog(catalog_data)
    full = catalog.snapshot(now=_FIXED_NOW)
    alpha_scope = frozenset({"alpha"})
    beta_scope = frozenset({"beta"})
    alpha = catalog.snapshot(now=_FIXED_NOW, allowed_topics=alpha_scope)
    beta = catalog.snapshot(now=_FIXED_NOW, allowed_topics=beta_scope)
    empty = catalog.snapshot(now=_FIXED_NOW, allowed_topics=frozenset())
    assert [topic.topic_id for topic in alpha.topics] == ["alpha"]
    assert alpha.total_question_count == 2
    assert alpha.topic("alpha").question_count == 2
    assert alpha.active_cache_count == 1
    assert alpha.total_chars == full.topic("alpha").chars
    assert [topic.topic_id for topic in beta.topics] == ["beta"]
    assert beta.total_question_count == 1
    assert empty.topics == ()
    assert empty.total_question_count == empty.active_cache_count == empty.total_chars == 0
    assert TopicCatalog(catalog_data).snapshot(now=_FIXED_NOW) is full
    assert TopicCatalog(catalog_data).snapshot(now=_FIXED_NOW, allowed_topics=alpha_scope) is alpha
    _append_query(
        QueryHistoryStore(catalog_data),
        query_id="scoped-new",
        created_at=_FIXED_NOW,
        topic_ids=("beta",),
    )
    assert catalog.snapshot(now=_FIXED_NOW, allowed_topics=beta_scope).total_question_count == 2
    assert catalog.snapshot(now=_FIXED_NOW).total_question_count == 6
    assert catalog.snapshot(now=_FIXED_NOW, allowed_topics=alpha_scope).total_question_count == 2


def test_catalog_cache_keeps_resolved_roots_isolated(
    catalog_data: Path, tmp_path: Path,
) -> None:
    first = TopicCatalog(catalog_data).snapshot(now=_FIXED_NOW)
    other = tmp_path / "other"
    TopicAdminService(other).create_topic("only", "Only", (), "other root body")
    second = TopicCatalog(other).snapshot(now=_FIXED_NOW)
    assert [topic.topic_id for topic in second.topics] == ["only"]
    assert second.total_question_count == 0
    assert TopicCatalog(catalog_data).snapshot(now=_FIXED_NOW) is first


@pytest.mark.parametrize("directory", ("qa-cache", "cache", "query-history"))
def test_catalog_cache_tracks_nested_membership_writes_and_shapes(
    catalog_data: Path, directory: str,
) -> None:
    parent = catalog_data / directory / "nested"
    parent.mkdir(parents=True)
    file = parent / "entry"
    file.write_text("first", encoding="utf-8")
    catalog = TopicCatalog(catalog_data)
    initial = catalog.snapshot(now=_FIXED_NOW)
    parent_stat = parent.stat()
    root_stat = parent.parent.stat()
    _write_changed(file, "other")
    rewritten = catalog.snapshot(now=_FIXED_NOW)
    assert rewritten is not initial
    assert rewritten == initial
    # Preserve parent timestamps: explicit membership must still detect removal.
    file.unlink()
    os.utime(parent, ns=(parent_stat.st_atime_ns, parent_stat.st_mtime_ns))
    os.utime(parent.parent, ns=(root_stat.st_atime_ns, root_stat.st_mtime_ns))
    removed = catalog.snapshot(now=_FIXED_NOW)
    assert removed is not rewritten
    file.mkdir()
    reshaped = catalog.snapshot(now=_FIXED_NOW)
    assert reshaped is not removed
    child = file / "new-entry"
    child.write_text("new cache entry", encoding="utf-8")
    assert catalog.snapshot(now=_FIXED_NOW) is not reshaped


def test_catalog_concurrent_callers_share_one_aggregate_build(
    catalog_data: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = threading.Barrier(8)
    builds = []
    build = TopicCatalog._build_snapshot

    def counted_build(self, registry, last_day, allowed_topics):
        builds.append(allowed_topics)
        return build(self, registry, last_day, allowed_topics)

    def read_snapshot(_):
        ready.wait(timeout=10)
        return TopicCatalog(catalog_data).snapshot(now=_FIXED_NOW)

    monkeypatch.setattr(TopicCatalog, "_build_snapshot", counted_build)
    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(pool.map(read_snapshot, range(8)))
    assert len(builds) == 1
    assert all(snapshot is snapshots[0] for snapshot in snapshots)
    assert snapshots[0].total_question_count == 5


def test_catalog_does_not_publish_a_concurrent_old_read_under_new_signature(
    catalog_data: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = TopicCatalog._build_snapshot
    read_complete = threading.Event()
    write_complete = threading.Event()
    builds = []

    def interrupted_build(self, registry, last_day, allowed_topics):
        result = build(self, registry, last_day, allowed_topics)
        builds.append(result)
        if len(builds) == 1:
            read_complete.set()
            assert write_complete.wait(timeout=10)
        return result

    monkeypatch.setattr(TopicCatalog, "_build_snapshot", interrupted_build)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(TopicCatalog(catalog_data).snapshot, now=_FIXED_NOW)
        try:
            assert read_complete.wait(timeout=10)
            _append_query(
                QueryHistoryStore(catalog_data),
                query_id="during-build",
                created_at=_FIXED_NOW,
                topic_ids=("alpha",),
            )
        finally:
            write_complete.set()
        result = pending.result(timeout=10)
    assert len(builds) == 2
    assert builds[0].total_question_count == 5
    assert result.total_question_count == 6
    assert TopicCatalog(catalog_data).snapshot(now=_FIXED_NOW) is result


def test_catalog_hit_rechecks_signature_after_directory_validation(
    catalog_data: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = TopicCatalog(catalog_data)
    initial = catalog.snapshot(now=_FIXED_NOW)
    validate = TopicCatalog._validate_topic_directories
    changed = False

    def write_during_validation(self, registry):
        nonlocal changed
        validate(self, registry)
        if not changed:
            changed = True
            _append_query(
                QueryHistoryStore(catalog_data),
                query_id="during-hit",
                created_at=_FIXED_NOW,
                topic_ids=("alpha",),
            )

    monkeypatch.setattr(TopicCatalog, "_validate_topic_directories", write_during_validation)
    result = catalog.snapshot(now=_FIXED_NOW)
    assert result.total_question_count == initial.total_question_count + 1


@pytest.mark.parametrize(
    ("relative", "content", "error", "message"),
    (
        ("topics/alpha/metadata.json", "{}", TopicCorruptionError, "invalid metadata"),
        ("topics/alpha/memory.md", "tampered body", TopicCorruptionError, "does not match"),
        ("topics/alpha/memory.md", b"\xff", TopicCorruptionError, "invalid canonical body"),
        ("topic-registry.json", "{}", ValueError, "topics must be a list"),
        ("query-history/2026-08.jsonl", "{}\n", QueryHistoryCorruptionError, "invalid query history"),
        ("qa-cache/alpha.jsonl", "{}\n", ValueError, "invalid cache record"),
    ),
)
def test_catalog_warm_cache_still_reports_corruption_and_does_not_cache_failures(
    catalog_data: Path, relative: str, content: str | bytes,
    error: type[Exception], message: str,
) -> None:
    catalog = TopicCatalog(catalog_data)
    initial = catalog.snapshot(now=_FIXED_NOW)
    path = catalog_data / relative
    original = path.read_bytes()
    _write_changed(path, content)
    for _ in range(2):
        with pytest.raises(error, match=message):
            TopicCatalog(catalog_data).snapshot(now=_FIXED_NOW)
    _write_changed(path, original)
    assert catalog.snapshot(now=_FIXED_NOW) == initial


@pytest.mark.parametrize("relative", ("topics/alpha/metadata.json", "topics/alpha/memory.md"))
@pytest.mark.parametrize("replacement", ("missing", "directory"))
def test_catalog_warm_cache_detects_missing_files_and_file_directory_changes(
    catalog_data: Path, relative: str, replacement: str,
) -> None:
    catalog = TopicCatalog(catalog_data)
    catalog.snapshot(now=_FIXED_NOW)
    path = catalog_data / relative
    path.unlink()
    if replacement == "directory":
        path.mkdir()
    with pytest.raises(TopicCorruptionError):
        catalog.snapshot(now=_FIXED_NOW)


def test_catalog_warm_cache_rejects_orphan_and_missing_topic_directories(
    catalog_data: Path,
) -> None:
    catalog = TopicCatalog(catalog_data)
    catalog.snapshot(now=_FIXED_NOW, allowed_topics=frozenset({"alpha"}))
    initial = catalog.snapshot(now=_FIXED_NOW)
    orphan = catalog_data / "topics" / "orphan"
    orphan.mkdir()
    with pytest.raises(ValueError, match="canonical topic directories differ"):
        catalog.snapshot(now=_FIXED_NOW, allowed_topics=frozenset({"alpha"}))
    orphan.rmdir()
    assert catalog.snapshot(now=_FIXED_NOW) == initial
    alpha = catalog_data / "topics" / "alpha"
    alpha.rename(catalog_data / "moved-alpha")
    with pytest.raises(ValueError, match="canonical topic directories differ"):
        catalog.snapshot(now=_FIXED_NOW)


def test_catalog_warm_cache_preserves_metadata_identity_and_state_validation(
    catalog_data: Path,
) -> None:
    catalog = TopicCatalog(catalog_data)
    initial = catalog.snapshot(now=_FIXED_NOW)
    path = catalog_data / "topics" / "alpha" / "metadata.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["topic_id"] = "beta"
    _write_changed(path, json.dumps(value))
    with pytest.raises(TopicCorruptionError, match="metadata topic mismatch"):
        catalog.snapshot(now=_FIXED_NOW)
    value["topic_id"] = "alpha"
    value["state"] = "archived"
    _write_changed(path, json.dumps(value))
    with pytest.raises(TopicCorruptionError, match="registry and metadata state differ"):
        catalog.snapshot(now=_FIXED_NOW)
    value["state"] = "active"
    _write_changed(path, json.dumps(value))
    assert catalog.snapshot(now=_FIXED_NOW) == initial


def test_catalog_scoped_cache_is_bounded_and_evicted_views_rebuild(
    catalog_data: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = TopicCatalog(catalog_data)
    initial = catalog.snapshot(now=_FIXED_NOW, allowed_topics=frozenset({"alpha"}))
    # The public scope space is unbounded even for a small registry.
    for index in range(32):
        scope = frozenset({"alpha", f"unknown-{index}"})
        assert catalog.snapshot(now=_FIXED_NOW, allowed_topics=scope) == initial
    builds = []
    build = TopicCatalog._build_snapshot

    def counted_build(self, registry, last_day, allowed_topics):
        builds.append(allowed_topics)
        return build(self, registry, last_day, allowed_topics)

    monkeypatch.setattr(TopicCatalog, "_build_snapshot", counted_build)
    rebuilt = catalog.snapshot(now=_FIXED_NOW, allowed_topics=frozenset({"alpha"}))
    assert rebuilt == initial
    assert rebuilt is not initial
    assert len(builds) == 1


def test_catalog_cache_detects_same_stat_atomic_registry_replacement(
    catalog_data: Path,
) -> None:
    catalog = TopicCatalog(catalog_data)
    initial = catalog.snapshot(now=_FIXED_NOW)
    registry = catalog_data / "topic-registry.json"
    previous = registry.stat()
    replacement = catalog_data / "replacement-registry.json"
    content = registry.read_text(encoding="utf-8").replace('"Alpha"', '"AlphX"')
    replacement.write_text(content, encoding="utf-8")
    os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    replacement.replace(registry)
    assert registry.stat().st_size == previous.st_size
    assert registry.stat().st_mtime_ns == previous.st_mtime_ns
    replaced = catalog.snapshot(now=_FIXED_NOW)
    assert replaced.topic("alpha").display_name == "AlphX"
    assert initial.topic("alpha").display_name == "Alpha"


def test_catalog_concurrent_scopes_do_not_mix_views(
    catalog_data: Path,
) -> None:
    scopes = [None, frozenset({"alpha"}), frozenset({"beta"}), frozenset()] * 2
    ready = threading.Barrier(len(scopes))

    def read_scope(scope):
        ready.wait(timeout=10)
        return TopicCatalog(catalog_data).snapshot(now=_FIXED_NOW, allowed_topics=scope)

    with ThreadPoolExecutor(max_workers=len(scopes)) as pool:
        snapshots = list(pool.map(read_scope, scopes))
    expected = [({"alpha", "beta", "gamma"}, 5), ({"alpha"}, 2), ({"beta"}, 1), (set(), 0)]
    for snapshot, (topic_ids, query_count) in zip(snapshots, expected * 2, strict=True):
        assert {topic.topic_id for topic in snapshot.topics} == topic_ids
        assert snapshot.total_question_count == query_count
    assert all(snapshots[index] is snapshots[index + 4] for index in range(4))


@pytest.mark.parametrize("relative", ("topic-registry.json", "query-history", "qa-cache"))
def test_catalog_cache_detects_top_level_file_directory_changes(
    catalog_data: Path, relative: str,
) -> None:
    catalog = TopicCatalog(catalog_data)
    initial = catalog.snapshot(now=_FIXED_NOW)
    path = catalog_data / relative
    original = catalog_data / f"saved-{relative}"
    path.rename(original)
    if relative == "topic-registry.json":
        path.mkdir()
        with pytest.raises(IsADirectoryError):
            catalog.snapshot(now=_FIXED_NOW)
        path.rmdir()
    else:
        path.write_text("not a directory", encoding="utf-8")
        reshaped = catalog.snapshot(now=_FIXED_NOW)
        if relative == "query-history":
            assert reshaped.total_question_count == 0
        else:
            assert reshaped.active_cache_count == 0
        path.unlink()
    original.rename(path)
    assert catalog.snapshot(now=_FIXED_NOW) == initial


def test_catalog_root_date_cache_is_bounded_and_evicted_dates_rebuild(
    catalog_data: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = TopicCatalog(catalog_data)
    initial = catalog.snapshot(now=_FIXED_NOW)
    for offset in range(1, 33):
        catalog.snapshot(now=_FIXED_NOW + timedelta(days=offset))
    builds = []
    build = TopicCatalog._build_snapshot

    def counted_build(self, registry, last_day, allowed_topics):
        builds.append(last_day)
        return build(self, registry, last_day, allowed_topics)

    monkeypatch.setattr(TopicCatalog, "_build_snapshot", counted_build)
    rebuilt = catalog.snapshot(now=_FIXED_NOW)
    assert rebuilt == initial
    assert rebuilt is not initial
    assert builds == [_FIXED_NOW.date()]
