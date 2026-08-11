from __future__ import annotations

from pathlib import Path

from hermes_topic_memory.cache import QuestionCache


def test_normalized_exact_hash_hit_survives_reload(tmp_path: Path) -> None:
    cache = QuestionCache(tmp_path, jaccard_threshold=0.8)
    cache.put(
        question="What is Alpha?",
        answer="Alpha is current.",
        topic_id="alpha",
        topic_version=3,
        model="reader-test",
        prompt_version="v1",
    )

    reloaded = QuestionCache(tmp_path, jaccard_threshold=0.8)
    hit = reloaded.lookup("  WHAT is alpha!!! ", topic_id="alpha", topic_version=3)

    assert hit is not None
    assert hit.answer == "Alpha is current."
    assert hit.match_kind == "exact"
    assert hit.similarity == 1.0


def test_deterministic_jaccard_hit_and_threshold_rejection(tmp_path: Path) -> None:
    cache = QuestionCache(tmp_path, jaccard_threshold=0.7)
    cache.put(
        question="where is the alpha service deployed",
        answer="Alpha runs in region one.",
        topic_id="alpha",
        topic_version=1,
        model="reader-test",
        prompt_version="v1",
    )

    hit = cache.lookup(
        "where is alpha service deployed now", topic_id="alpha", topic_version=1
    )
    miss = cache.lookup("alpha owner", topic_id="alpha", topic_version=1)

    assert hit is not None
    assert hit.match_kind == "jaccard"
    assert hit.similarity == 5 / 7
    assert miss is None


def test_cache_is_strictly_isolated_by_topic(tmp_path: Path) -> None:
    cache = QuestionCache(tmp_path, jaccard_threshold=0.5)
    cache.put(
        question="shared question",
        answer="alpha answer",
        topic_id="alpha",
        topic_version=1,
        model="reader-test",
        prompt_version="v1",
    )

    assert cache.lookup("shared question", topic_id="beta", topic_version=1) is None


def test_topic_version_change_ignores_only_that_topics_stale_records(tmp_path: Path) -> None:
    cache = QuestionCache(tmp_path)
    for topic_id in ("alpha", "beta"):
        cache.put(
            question="status",
            answer=f"{topic_id} status",
            topic_id=topic_id,
            topic_version=1,
            model="reader-test",
            prompt_version="v1",
        )

    assert cache.lookup("status", topic_id="alpha", topic_version=2) is None
    beta_hit = cache.lookup("status", topic_id="beta", topic_version=1)
    assert beta_hit is not None
    assert beta_hit.answer == "beta status"


def test_cache_file_is_append_only_jsonl_with_required_metadata(tmp_path: Path) -> None:
    cache = QuestionCache(tmp_path)
    first = cache.put(
        question="first question",
        answer="first answer",
        topic_id="alpha",
        topic_version=1,
        model="reader-a",
        prompt_version="prompt-v2",
    )
    cache.put(
        question="second question",
        answer="second answer",
        topic_id="alpha",
        topic_version=1,
        model="reader-a",
        prompt_version="prompt-v2",
    )

    records = cache.records("alpha")
    assert len(records) == 2
    assert records[0] == first
    assert first.question_hash
    assert first.created_at
    assert first.similarity == 1.0
    assert (tmp_path / "qa-cache" / "alpha.jsonl").read_text().count("\n") == 2
