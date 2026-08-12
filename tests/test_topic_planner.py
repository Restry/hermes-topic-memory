from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from hermes_topic_memory.multi_recall import TopicSelection
from hermes_topic_memory.reader import ReaderRequest, ReaderResponse
from hermes_topic_memory.router import TopicRegistry
from hermes_topic_memory.topic_planner import PlanningError, TopicPlanner


_ALPHA_BODY_SENTINEL = "ALPHA_CANONICAL_BODY_MUST_NOT_REACH_PLANNER"
_PRIVATE_ANSWER_SENTINEL = "PRIVATE_ANSWER_MUST_NOT_REACH_PLANNER"


@dataclass
class _PlannerModel:
    output: str

    def __post_init__(self) -> None:
        self.requests: list[ReaderRequest] = []

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.requests.append(request)
        return ReaderResponse(answer=self.output, model="planner-test")


def _registry(*, ambiguous_display: bool = False) -> TopicRegistry:
    beta_display = "Shared" if ambiguous_display else "Beta Topic"
    alpha_display = "Shared" if ambiguous_display else "Alpha Topic"
    return TopicRegistry.from_dict(
        {
            "default_topic": "alpha",
            "topics": [
                {
                    "topic_id": "alpha",
                    "display_name": alpha_display,
                    "aliases": ["first"],
                    "chat_ids": ["private-chat-alpha"],
                    "source_relative_path": "private/alpha.md",
                },
                {
                    "topic_id": "beta",
                    "display_name": beta_display,
                    "aliases": ["second"],
                    "chat_ids": ["private-chat-beta"],
                    "source_relative_path": "private/beta.md",
                },
            ],
        }
    )


def test_planner_uses_only_raw_question_and_public_routing_metadata() -> None:
    raw_question = "  Alpha 在 2026-08-12 09:07:03+08:00 说了什么？\n不要改写。  "
    model = _PlannerModel(
        json.dumps(
            {
                "topics": [
                    {"topic": "first", "reason": "问题明确提到 Alpha"},
                    {"topic": "beta", "reason": "需要 Beta 对照"},
                ]
            },
            ensure_ascii=False,
        )
    )
    planner = TopicPlanner(registry=_registry(), model=model)

    plan = planner.plan(raw_question)

    assert plan.question == raw_question
    assert plan.mode == "agent"
    assert plan.selections == (
        TopicSelection("alpha", "问题明确提到 Alpha"),
        TopicSelection("beta", "需要 Beta 对照"),
    )
    assert len(model.requests) == 1
    request = model.requests[0]
    assert request.question == raw_question
    assert '"topic_id":"alpha"' in request.prefix
    assert '"display_name":"Alpha Topic"' in request.prefix
    assert '"aliases":["first"]' in request.prefix
    assert "minimum sufficient" in request.prefix
    assert "1 to 8" in request.prefix
    assert "never add global unless the original question requires it" in request.prefix
    for forbidden in (
        _ALPHA_BODY_SENTINEL,
        _PRIVATE_ANSWER_SENTINEL,
        "private-chat-alpha",
        "private/alpha.md",
        '"default_topic"',
    ):
        assert forbidden not in request.prompt


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("not-json", "strict JSON"),
        ('{"topics":[]}', "1 to 8"),
        (
            json.dumps(
                {
                    "topics": [
                        {"topic": f"topic-{index}", "reason": "too broad"}
                        for index in range(9)
                    ]
                }
            ),
            "1 to 8",
        ),
        ('{"topics":[{"topic":"missing","reason":"unknown"}]}', "unknown"),
        (
            '{"topics":[{"topic":"alpha","reason":"one"},'
            '{"topic":"first","reason":"same resolved topic"}]}',
            "duplicate",
        ),
        ('{"topics":[{"topic":"alpha","reason":""}]}', "reason"),
        (
            '{"topics":[{"topic":"alpha","reason":"valid","extra":true}]}',
            "strict JSON",
        ),
        ('{"topics":[{"topic":"alpha","reason":"valid"}],"extra":true}', "strict JSON"),
        (
            '{"topics":[{"topic":"alpha","reason":"valid"}],"topics":[]}',
            "strict JSON",
        ),
    ],
)
def test_planner_rejects_malformed_unsafe_or_non_strict_output(
    output: str, message: str
) -> None:
    model = _PlannerModel(output)
    planner = TopicPlanner(registry=_registry(), model=model)

    with pytest.raises(PlanningError, match=message):
        planner.plan("raw question")


def test_planner_rejects_ambiguous_display_name() -> None:
    model = _PlannerModel(
        '{"topics":[{"topic":"Shared","reason":"ambiguous display"}]}'
    )
    planner = TopicPlanner(registry=_registry(ambiguous_display=True), model=model)

    with pytest.raises(PlanningError, match="ambiguous"):
        planner.plan("raw question")


def test_manual_override_bypasses_model_and_is_explicitly_labeled() -> None:
    model = _PlannerModel("must not be read")
    planner = TopicPlanner(registry=_registry(), model=model)
    raw_question = "When exactly at 2026-08-12T01:02:03Z?"

    plan = planner.manual(
        question=raw_question,
        selections=(TopicSelection("first", "manual"),),
    )

    assert plan.question == raw_question
    assert plan.mode == "manual"
    assert plan.selections == (TopicSelection("alpha", "manual"),)
    assert model.requests == []


@pytest.mark.parametrize(
    "selections",
    [
        (),
        tuple(TopicSelection("alpha", "manual") for _ in range(9)),
        (TopicSelection("missing", "manual"),),
        (
            TopicSelection("alpha", "manual"),
            TopicSelection("first", "manual duplicate"),
        ),
    ],
)
def test_manual_override_rejects_unsafe_scope_before_model_call(
    selections: tuple[TopicSelection, ...],
) -> None:
    model = _PlannerModel("must not be read")
    planner = TopicPlanner(registry=_registry(), model=model)

    with pytest.raises(PlanningError):
        planner.manual(question="raw question", selections=selections)

    assert model.requests == []
