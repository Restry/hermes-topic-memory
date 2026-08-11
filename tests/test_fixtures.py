from __future__ import annotations

import hashlib
import json
from pathlib import Path

from hermes_topic_memory.router import TopicRegistry
from hermes_topic_memory.store import FileTopicStore

FIXTURE_ROOT = Path(__file__).parents[1] / "examples" / "data"
TOPIC_IDS = {"gsk-d360", "mindmemos", "growlog", "global"}
REQUIRED_SCENARIOS = {
    "current",
    "historical",
    "superseding",
    "expected-empty",
    "leakage",
}


def test_registry_defines_exactly_four_unambiguous_synthetic_topics() -> None:
    registry_path = FIXTURE_ROOT / "topic-registry.json"
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    registry = TopicRegistry.from_dict(raw)

    assert {topic.topic_id for topic in registry.topics} == TOPIC_IDS
    assert raw["default_topic"] == "global"
    assert all(topic["synthetic"] is True for topic in raw["topics"])
    assert registry.route().topic_id == "global"


def test_each_fixture_is_versioned_sanitized_canonical_text() -> None:
    store = FileTopicStore(FIXTURE_ROOT)

    for topic_id in TOPIC_IDS:
        memory = store.get_topic(topic_id)
        body = memory.body
        assert memory.metadata.topic_id == topic_id
        assert memory.metadata.version >= 2
        assert memory.metadata.source_policy == "synthetic-fixture-only"
        assert memory.metadata.content_fingerprint == hashlib.sha256(
            body.encode("utf-8")
        ).hexdigest()
        assert "Fixture classification: sanitized synthetic data." in body
        assert "## Current facts" in body
        assert "## Historical facts" in body
        assert "## Superseding facts" in body
        assert "## Overlapping distractors" in body
        assert "Harbor" in body
        assert store.read_version(topic_id, 1) != body
        assert store.list_versions(topic_id) == (1, memory.metadata.version)


def test_evaluation_questions_cover_isolation_and_answer_constraints() -> None:
    questions_path = FIXTURE_ROOT.parent / "evaluation" / "questions.jsonl"
    questions = [
        json.loads(line)
        for line in questions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert len(questions) == 20
    assert len({item["id"] for item in questions}) == len(questions)
    for topic_id in TOPIC_IDS:
        topic_questions = [item for item in questions if item["expected_topic"] == topic_id]
        assert {item["scenario"] for item in topic_questions} == REQUIRED_SCENARIOS
        assert sum(item["expected_empty"] for item in topic_questions) == 2
        leakage = next(item for item in topic_questions if item["scenario"] == "leakage")
        assert leakage["expected_empty"] is True
        assert leakage["forbidden_answer_contains"]

    for item in questions:
        assert item["expected_topic"] in TOPIC_IDS
        assert isinstance(item["question"], str) and item["question"]
        assert isinstance(item["expected_answer_contains"], list)
        assert isinstance(item["forbidden_answer_contains"], list)
        if item["expected_empty"]:
            assert item["expected_answer_contains"] == []
        else:
            assert item["expected_answer_contains"]
