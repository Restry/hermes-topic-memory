from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from hermes_topic_memory.cache import QuestionCache
from hermes_topic_memory.panel_app import PanelApplication, PanelResponse
from hermes_topic_memory.reader import ReaderRequest, ReaderResponse
from hermes_topic_memory.query_history import (
    QueryHistoryStore,
    QueryRecord,
    TopicQueryResult,
)
from hermes_topic_memory.store import FileTopicStore
from hermes_topic_memory.topic_admin import TopicAdminService

_AUTH_TOKEN = "panel-auth-token-DO-NOT-LEAK"
_ALPHA_BODY_V1 = "# Alpha\n\nALPHA_CANONICAL_PRIVATE_V1\n"
_ALPHA_BODY_V2 = "# Alpha\n\nALPHA_CANONICAL_PRIVATE_V2\n"
_BETA_BODY = "# Beta\n\nBETA_CANONICAL_PRIVATE\n"
_TRASH_BODY = "# Trash\n\nTRASH_CANONICAL_PRIVATE\n"
_READER_SECRET = "reader-api-key-DO-NOT-LEAK"


class _Reader:
    api_key = _READER_SECRET

    def __init__(self) -> None:
        self.planner_output = (
            '{"topics":[{"topic":"alpha","reason":"explicitly named"}]}'
        )
        self.requests: list[ReaderRequest] = []
        self.fail_topics: set[str] = set()
        self.no_fact_topics: set[str] = set()

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.requests.append(request)
        if "TOPIC PLANNING RULES" in request.prefix:
            return ReaderResponse(answer=self.planner_output, model="panel-planner")
        if "Synthesize only the labeled topic capsules" in request.prefix:
            labels = [
                line.removeprefix("LABEL:")
                for line in request.prefix.splitlines()
                if line.startswith("LABEL:")
            ]
            return ReaderResponse(
                answer="; ".join(f"{label} recalled" for label in labels),
                model="panel-synthesizer",
            )
        if _ALPHA_BODY_V2 in request.prefix:
            if "alpha" in self.fail_topics:
                raise RuntimeError("PRIVATE_ALPHA_BACKEND_FAILURE")
            if "alpha" in self.no_fact_topics:
                return ReaderResponse(
                    answer="no stored fact answers the question",
                    model="panel-reader",
                )
            return ReaderResponse(answer="alpha recalled fact", model="panel-reader")
        if _BETA_BODY in request.prefix:
            if "beta" in self.fail_topics:
                raise RuntimeError("PRIVATE_BETA_BACKEND_FAILURE")
            if "beta" in self.no_fact_topics:
                return ReaderResponse(
                    answer="no stored fact answers the question",
                    model="panel-reader",
                )
            return ReaderResponse(answer="beta recalled fact", model="panel-reader")
        raise AssertionError("unexpected Reader request")


@dataclass(frozen=True)
class PanelFixture:
    app: PanelApplication
    data_root: Path
    reader: _Reader


@pytest.fixture
def panel_fixture(tmp_path: Path) -> PanelFixture:
    data_root = tmp_path / "private-data"
    admin = TopicAdminService(data_root)
    admin.create_topic("alpha", "Alpha Topic", ("first",), _ALPHA_BODY_V1)
    FileTopicStore(data_root).update_topic("alpha", _ALPHA_BODY_V2)
    admin.create_topic("beta", "Beta Topic", ("second",), _BETA_BODY)
    admin.archive("beta", expected_version=1)
    admin.create_topic("discarded", "Discarded Topic", (), _TRASH_BODY)
    admin.trash("discarded", expected_version=1)

    metadata_path = data_root / "topics/alpha/metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["source_relative_path"] = str(tmp_path / "private-corpus/alpha.md")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    cache = QuestionCache(data_root)
    cache.put(
        question="alpha cached",
        answer="cached capsule",
        topic_id="alpha",
        topic_version=2,
        model="test",
        prompt_version="v1",
    )

    history = QueryHistoryStore(data_root)
    _append_query(
        history,
        query_id="alpha-old",
        created_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
        topic_results=(("alpha", "answered", "reader"),),
        source="hermes",
    )
    _append_query(
        history,
        query_id="alpha-no-fact",
        created_at=datetime(2026, 8, 9, 2, tzinfo=UTC),
        topic_results=(("alpha", "no_fact", "reader"),),
        source="cli",
    )
    _append_query(
        history,
        query_id="alpha-beta-multi",
        created_at=datetime(2026, 8, 10, 3, tzinfo=UTC),
        topic_results=(
            ("alpha", "answered", "cache"),
            ("beta", "error", "failure"),
        ),
        source="panel",
    )
    _append_query(
        history,
        query_id="beta-newest",
        created_at=datetime(2026, 8, 11, 4, tzinfo=UTC),
        topic_results=(("beta", "answered", "reader"),),
        source="panel",
    )
    reader = _Reader()
    return PanelFixture(
        app=PanelApplication(data_root, reader, _AUTH_TOKEN),
        data_root=data_root,
        reader=reader,
    )


def test_overview_contains_real_kpis_charts_catalog_and_no_canonical_body(
    panel_fixture: PanelFixture,
) -> None:
    response, payload = _dispatch(panel_fixture.app, "/api/overview")

    assert response.status == 200
    assert payload["kpis"]["active_topic_count"] == 1
    assert payload["kpis"]["archived_topic_count"] == 1
    assert payload["kpis"]["trashed_topic_count"] == 1
    assert payload["kpis"]["total_question_count"] == 4
    assert payload["kpis"]["active_cache_count"] == 1
    assert len(payload["charts"]["daily_questions"]) == 30
    assert payload["charts"]["topic_distribution"] == [
        {
            "topic_id": "alpha",
            "display_name": "Alpha Topic",
            "question_count": 3,
        },
        {
            "topic_id": "beta",
            "display_name": "Beta Topic",
            "question_count": 2,
        },
    ]
    assert {topic["topic_id"] for topic in payload["topics"]} == {"alpha", "beta"}
    assert payload["recent_questions"][0]["query_id"] == "beta-newest"
    _assert_no_body_or_secret(response.body, panel_fixture.data_root)


def test_topics_support_safe_search_status_sort_and_pagination(
    panel_fixture: PanelFixture,
) -> None:
    _, searched = _dispatch(panel_fixture.app, "/api/topics?q=first")
    assert [item["topic_id"] for item in searched["items"]] == ["alpha"]

    _, archived = _dispatch(panel_fixture.app, "/api/topics?status=archived")
    assert [item["topic_id"] for item in archived["items"]] == ["beta"]

    _, sorted_page = _dispatch(
        panel_fixture.app,
        "/api/topics?sort=question_count&order=desc&page=2&page_size=1",
    )
    assert sorted_page["total"] == 2
    assert sorted_page["page"] == 2
    assert sorted_page["page_size"] == 1
    assert [item["topic_id"] for item in sorted_page["items"]] == ["beta"]

    response, error = _dispatch(panel_fixture.app, "/api/topics?sort=body")
    assert response.status == 400
    assert error["error"]["code"] == "invalid_request"


def test_topic_detail_is_metadata_only_and_body_read_returns_fingerprint(
    panel_fixture: PanelFixture,
) -> None:
    detail_response, detail = _dispatch(panel_fixture.app, "/api/topics/alpha")

    assert detail_response.status == 200
    assert detail["topic"]["topic_id"] == "alpha"
    assert detail["topic"]["version"] == 2
    assert detail["topic"]["question_count"] == 3
    assert detail["topic"]["active_cache_count"] == 1
    assert "body" not in detail["topic"]
    assert detail["topic"]["source_relative_path"] is None
    _assert_no_body_or_secret(detail_response.body, panel_fixture.data_root)

    body_response, body = _dispatch(panel_fixture.app, "/api/topics/alpha/body")
    assert body_response.status == 200
    assert body == {
        "topic_id": "alpha",
        "version": 2,
        "body": _ALPHA_BODY_V2,
        "fingerprint": body["fingerprint"],
    }
    assert len(body["fingerprint"]) == 64
    assert _BETA_BODY not in body_response.body.decode("utf-8")
    assert _TRASH_BODY not in body_response.body.decode("utf-8")


def test_questions_filter_topic_status_source_date_and_single_multi(
    panel_fixture: PanelFixture,
) -> None:
    cases = {
        "topic=beta&status=error": ["alpha-beta-multi"],
        "source=cli": ["alpha-no-fact"],
        "date_from=2026-08-10&date_to=2026-08-10": ["alpha-beta-multi"],
        "scope=multi": ["alpha-beta-multi"],
        "scope=single&topic=beta": ["beta-newest"],
    }
    for query, expected_ids in cases.items():
        _, payload = _dispatch(panel_fixture.app, f"/api/questions?{query}")
        assert [item["query_id"] for item in payload["items"]] == expected_ids
        assert "session_ref" not in json.dumps(payload)

    _, topic_questions = _dispatch(
        panel_fixture.app,
        "/api/topics/alpha/questions?status=no_fact",
    )
    assert [item["query_id"] for item in topic_questions["items"]] == [
        "alpha-no-fact"
    ]


@pytest.mark.parametrize(
    "query",
    [
        "topic=../alpha",
        "status=unknown",
        "source=browser",
        "scope=all",
        "date_from=not-a-date",
        "date_from=2026-08-11&date_to=2026-08-10",
        "page=0",
        "page_size=1000",
    ],
)
def test_question_filter_validation_is_structured_400(
    panel_fixture: PanelFixture,
    query: str,
) -> None:
    response, payload = _dispatch(panel_fixture.app, f"/api/questions?{query}")
    assert response.status == 400
    assert payload["error"]["code"] == "invalid_request"
    assert str(panel_fixture.data_root) not in response.body.decode("utf-8")


def test_version_list_is_paginated_and_version_body_is_authenticated(
    panel_fixture: PanelFixture,
) -> None:
    _, versions = _dispatch(
        panel_fixture.app,
        "/api/topics/alpha/versions?page=1&page_size=1",
    )
    assert versions["total"] == 2
    assert versions["items"] == [
        {
            "version": 2,
            "current": True,
            "fingerprint": versions["items"][0]["fingerprint"],
        }
    ]
    assert len(versions["items"][0]["fingerprint"]) == 64
    assert _ALPHA_BODY_V1 not in json.dumps(versions)
    assert _ALPHA_BODY_V2 not in json.dumps(versions)

    _, version_body = _dispatch(
        panel_fixture.app,
        "/api/topics/alpha/versions/1/body",
    )
    assert version_body["topic_id"] == "alpha"
    assert version_body["version"] == 1
    assert version_body["body"] == _ALPHA_BODY_V1
    assert version_body["current"] is False
    assert len(version_body["fingerprint"]) == 64


def test_recycle_bin_lists_only_safe_tombstone_metadata(
    panel_fixture: PanelFixture,
) -> None:
    response, payload = _dispatch(panel_fixture.app, "/api/recycle-bin")

    assert response.status == 200
    assert payload["items"] == [
        {
            "topic_id": "discarded",
            "display_name": "Discarded Topic",
            "prior_state": "active",
            "created_at": payload["items"][0]["created_at"],
            "trashed_at": payload["items"][0]["trashed_at"],
        }
    ]
    _assert_no_body_or_secret(response.body, panel_fixture.data_root)


@pytest.mark.parametrize(
    "path",
    [
        "/api/overview",
        "/api/topics",
        "/api/topics/alpha",
        "/api/topics/alpha/body",
        "/api/topics/alpha/questions",
        "/api/topics/alpha/versions",
        "/api/topics/alpha/versions/1/body",
        "/api/questions",
        "/api/recycle-bin",
    ],
)
def test_all_metadata_and_body_routes_require_valid_authorization_without_data(
    panel_fixture: PanelFixture,
    path: str,
) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong-token"}):
        response = panel_fixture.app.dispatch("GET", path, headers=headers)
        payload = json.loads(response.body)
        assert response.status == 401
        assert payload == {
            "error": {
                "code": "unauthorized",
                "message": "Authentication required.",
            }
        }
        _assert_no_body_or_secret(response.body, panel_fixture.data_root)


def test_bad_authorization_uses_constant_time_comparison(
    panel_fixture: PanelFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def compare(left: str, right: str) -> bool:
        calls.append((left, right))
        return False

    monkeypatch.setattr("hermes_topic_memory.panel_app.secrets.compare_digest", compare)
    response = panel_fixture.app.dispatch(
        "GET",
        "/api/overview",
        headers={"Authorization": "Bearer wrong"},
    )

    assert response.status == 401
    assert calls == [("wrong", _AUTH_TOKEN)]
    assert _AUTH_TOKEN.encode("utf-8") not in response.body


def test_unknown_path_and_missing_resources_return_structured_404(
    panel_fixture: PanelFixture,
) -> None:
    for path in (
        "/api/unknown",
        "/api/topics/missing",
        "/api/topics/alpha/versions/99/body",
    ):
        response, payload = _dispatch(panel_fixture.app, path)
        assert response.status == 404
        assert payload["error"]["code"] == "not_found"
        _assert_no_body_or_secret(response.body, panel_fixture.data_root)


@pytest.mark.parametrize(
    "path",
    [
        "/api/topics/../beta/body",
        "/api/topics/%2e%2e/beta/body",
        "/api/topics/%252e%252e/beta/body",
        "/api/topics/alpha%2fbeta/body",
        "/api/topics/alpha\\..\\beta/body",
    ],
)
def test_plain_and_encoded_path_traversal_is_rejected_without_leakage(
    panel_fixture: PanelFixture,
    path: str,
) -> None:
    outside_secret = "ARBITRARY_OUTSIDE_FILE_PRIVATE"
    (panel_fixture.data_root.parent / "arbitrary-secret.txt").write_text(
        outside_secret,
        encoding="utf-8",
    )
    response, payload = _dispatch(panel_fixture.app, path)
    assert response.status == 400
    assert payload["error"]["code"] == "invalid_path"
    assert outside_secret.encode("utf-8") not in response.body
    _assert_no_body_or_secret(response.body, panel_fixture.data_root)


def test_internal_storage_error_is_structured_and_redacted(
    panel_fixture: PanelFixture,
) -> None:
    private_error_content = "MALFORMED_PRIVATE_HISTORY_CONTENT"
    (panel_fixture.data_root / "query-history/2026-09.jsonl").write_text(
        private_error_content,
        encoding="utf-8",
    )

    response, payload = _dispatch(panel_fixture.app, "/api/questions")

    assert response.status == 500
    assert payload == {
        "error": {
            "code": "internal_error",
            "message": "Request could not be completed.",
        }
    }
    assert private_error_content.encode("utf-8") not in response.body
    _assert_no_body_or_secret(response.body, panel_fixture.data_root)


def test_malformed_json_is_structured_400_before_route_dispatch(
    panel_fixture: PanelFixture,
) -> None:
    response = panel_fixture.app.dispatch(
        "GET",
        "/api/overview",
        headers=_headers(),
        body=b'{"broken":',
    )
    payload = json.loads(response.body)

    assert response.status == 400
    assert payload["error"]["code"] == "invalid_json"
    _assert_no_body_or_secret(response.body, panel_fixture.data_root)


def test_response_never_serializes_reader_key_token_absolute_root_or_neighbor_body(
    panel_fixture: PanelFixture,
) -> None:
    paths = [
        "/api/overview",
        "/api/topics",
        "/api/topics/alpha",
        "/api/topics/alpha/questions",
        "/api/topics/alpha/versions",
        "/api/questions",
        "/api/recycle-bin",
    ]
    payload = b"".join(
        panel_fixture.app.dispatch("GET", path, headers=_headers()).body
        for path in paths
    )
    _assert_no_body_or_secret(payload, panel_fixture.data_root)

    alpha_response = panel_fixture.app.dispatch(
        "GET", "/api/topics/alpha/body", headers=_headers()
    )
    assert json.loads(alpha_response.body)["body"] == _ALPHA_BODY_V2
    assert _BETA_BODY.encode() not in alpha_response.body
    assert _TRASH_BODY.encode() not in alpha_response.body
    assert _AUTH_TOKEN.encode() not in alpha_response.body
    assert _READER_SECRET.encode() not in alpha_response.body
    assert str(panel_fixture.data_root).encode() not in alpha_response.body


def test_recall_plan_is_authenticated_and_preserves_raw_question(
    panel_fixture: PanelFixture,
) -> None:
    raw_question = "  Alpha 在 2026-08-12 09:07:03+08:00 的事实？\n保持原样。  "

    unauthorized = panel_fixture.app.dispatch(
        "POST",
        "/api/recall/plan",
        body=json.dumps({"question": raw_question}, ensure_ascii=False).encode(),
    )
    response, payload = _post(
        panel_fixture.app, "/api/recall/plan", {"question": raw_question}
    )

    assert unauthorized.status == 401
    assert response.status == 200
    assert payload["question"] == raw_question
    assert payload["plan"] == {
        "mode": "agent",
        "topics": [
            {
                "topic": "alpha",
                "topic_id": "alpha",
                "display_name": "Alpha Topic",
                "reason": "explicitly named",
            }
        ],
    }
    assert payload["latency_ms"] >= 0
    assert panel_fixture.reader.requests[-1].question == raw_question
    _assert_no_body_or_secret(response.body, panel_fixture.data_root)


def test_recall_run_returns_plan_results_answer_versions_cache_latency_and_query(
    panel_fixture: PanelFixture,
) -> None:
    raw_question = "Compare alpha and beta at 2026-08-12T01:02:03Z exactly."
    panel_fixture.reader.planner_output = json.dumps(
        {
            "topics": [
                {"topic": "first", "reason": "alpha named"},
                {"topic": "second", "reason": "beta named"},
            ]
        }
    )

    response, payload = _post(
        panel_fixture.app, "/api/recall/run", {"question": raw_question}
    )

    assert response.status == 200
    assert payload["status"] == "answered"
    assert payload["question"] == raw_question
    assert payload["query_id"]
    assert payload["plan"]["mode"] == "agent"
    assert [item["topic_id"] for item in payload["topic_results"]] == [
        "alpha",
        "beta",
    ]
    assert [item["topic_version"] for item in payload["topic_results"]] == [2, 1]
    assert all(item["cache_status"] in {"hit", "miss"} for item in payload["topic_results"])
    assert "[Alpha Topic/alpha]" in payload["final_answer"]
    assert "[Beta Topic/beta]" in payload["final_answer"]
    assert set(payload["latency_ms"]) == {
        "planning",
        "recall",
        "topics",
        "synthesis",
        "total",
    }
    record = panel_fixture.app.history.get(payload["query_id"])
    assert record is not None
    assert record.question == raw_question
    assert record.source == "panel"
    assert [item.topic_id for item in record.selected_topics] == ["alpha", "beta"]
    assert panel_fixture.reader.requests[0].question == raw_question
    topic_requests = [
        request
        for request in panel_fixture.reader.requests
        if "CANONICAL MEMORY (verbatim UTF-8)" in request.prefix
    ]
    assert len(topic_requests) == 2
    assert all(request.question == raw_question for request in topic_requests)
    synthesis_request = panel_fixture.reader.requests[-1]
    assert raw_question in synthesis_request.prefix
    _assert_no_body_or_secret(response.body, panel_fixture.data_root)


def test_manual_recall_bypasses_planner_and_is_labeled_manual(
    panel_fixture: PanelFixture,
) -> None:
    before = len(panel_fixture.reader.requests)
    response, payload = _post(
        panel_fixture.app,
        "/api/recall/run",
        {
            "question": "Manual alpha question",
            "manual_topics": [{"topic": "first", "reason": "manual test scope"}],
        },
    )

    assert response.status == 200
    assert payload["plan"]["mode"] == "manual"
    assert payload["plan"]["topics"][0]["reason"] == "manual test scope"
    new_requests = panel_fixture.reader.requests[before:]
    assert new_requests
    assert all("TOPIC PLANNING RULES" not in request.prefix for request in new_requests)


@pytest.mark.parametrize(
    "manual_topics",
    [
        [],
        [{"topic": "missing", "reason": "manual"}],
        [
            {"topic": "alpha", "reason": "manual"},
            {"topic": "first", "reason": "duplicate"},
        ],
        [{"topic": "alpha", "reason": "manual"}] * 9,
    ],
)
def test_manual_recall_rejects_unsafe_scope_before_reader_calls(
    panel_fixture: PanelFixture, manual_topics: list[dict[str, str]]
) -> None:
    before_requests = len(panel_fixture.reader.requests)
    before_history = len(panel_fixture.app.history.list())

    response, payload = _post(
        panel_fixture.app,
        "/api/recall/run",
        {"question": "unsafe manual scope", "manual_topics": manual_topics},
    )

    assert response.status == 400
    assert payload["error"]["code"] == "invalid_recall_scope"
    assert len(panel_fixture.reader.requests) == before_requests
    assert len(panel_fixture.app.history.list()) == before_history
    _assert_no_body_or_secret(response.body, panel_fixture.data_root)


def test_malformed_planner_json_fails_before_topic_read(
    panel_fixture: PanelFixture,
) -> None:
    panel_fixture.reader.planner_output = "```json\nnot strict json\n```"
    before_history = len(panel_fixture.app.history.list())

    response, payload = _post(
        panel_fixture.app, "/api/recall/run", {"question": "alpha question"}
    )

    assert response.status == 422
    assert payload["error"]["code"] == "planning_failed"
    assert len(panel_fixture.reader.requests) == 1
    assert "TOPIC PLANNING RULES" in panel_fixture.reader.requests[0].prefix
    assert len(panel_fixture.app.history.list()) == before_history
    _assert_no_body_or_secret(response.body, panel_fixture.data_root)


def test_recall_rerun_uses_exact_original_question_current_versions_and_new_id(
    panel_fixture: PanelFixture,
) -> None:
    raw_question = "  exact 2026-08-12 17:45:09+08:00?\nKeep spacing.  "
    _, first = _post(
        panel_fixture.app,
        "/api/recall/run",
        {
            "question": raw_question,
            "manual_topics": [{"topic": "alpha", "reason": "manual rerun seed"}],
        },
    )
    panel_fixture.app.store.update_topic("alpha", _ALPHA_BODY_V2 + "new version\n")

    response, rerun = _post(
        panel_fixture.app, f"/api/recall/rerun/{first['query_id']}", {}
    )

    assert response.status == 200
    assert rerun["question"] == raw_question
    assert rerun["query_id"] != first["query_id"]
    assert rerun["plan"]["mode"] == "rerun"
    assert rerun["topic_results"][0]["topic_version"] == 3
    old_record = panel_fixture.app.history.get(first["query_id"])
    new_record = panel_fixture.app.history.get(rerun["query_id"])
    assert old_record is not None and new_record is not None
    assert old_record.question == raw_question == new_record.question
    assert old_record.selected_topics[0].topic_version == 2
    assert new_record.selected_topics[0].topic_version == 3
    assert new_record.source == "panel"


def test_failed_recall_is_recorded_as_real_panel_error_without_false_success(
    panel_fixture: PanelFixture,
) -> None:
    panel_fixture.reader.fail_topics.add("beta")

    response, payload = _post(
        panel_fixture.app,
        "/api/recall/run",
        {
            "question": "beta failure",
            "manual_topics": [{"topic": "beta", "reason": "manual failure test"}],
        },
    )

    assert response.status == 200
    assert payload["status"] == "error"
    assert payload["final_answer"] is None
    assert payload["failure"]["code"] == "no_answered_topics"
    assert payload["topic_results"][0]["status"] == "error"
    assert payload["topic_results"][0]["failure"]["code"] == "recall_error"
    record = panel_fixture.app.history.get(payload["query_id"])
    assert record is not None
    assert record.source == "panel"
    assert record.final_answer is None
    assert record.selected_topics[0].status == "error"
    assert b"PRIVATE_BETA_BACKEND_FAILURE" not in response.body


def test_no_fact_recall_is_durably_recorded_with_panel_source(
    panel_fixture: PanelFixture,
) -> None:
    panel_fixture.reader.no_fact_topics.add("alpha")

    response, payload = _post(
        panel_fixture.app,
        "/api/recall/run",
        {
            "question": "alpha has no matching fact",
            "manual_topics": [{"topic": "alpha", "reason": "manual no-fact test"}],
        },
    )

    assert response.status == 200
    assert payload["status"] == "no_fact"
    assert payload["final_answer"] is None
    assert payload["topic_results"][0]["status"] == "no_fact"
    record = panel_fixture.app.history.get(payload["query_id"])
    assert record is not None
    assert record.source == "panel"
    assert record.final_answer is None
    assert record.selected_topics[0].status == "no_fact"



def test_query_history_write_failure_cannot_report_no_fact_as_success(
    panel_fixture: PanelFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel_fixture.reader.no_fact_topics.add("alpha")

    def fail_append(_record: QueryRecord) -> QueryRecord:
        raise OSError("PRIVATE_QUERY_LEDGER_WRITE_FAILURE")

    monkeypatch.setattr(panel_fixture.app.history, "append", fail_append)
    response, payload = _post(
        panel_fixture.app,
        "/api/recall/run",
        {
            "question": "alpha ledger failure",
            "manual_topics": [{"topic": "alpha", "reason": "manual ledger test"}],
        },
    )

    assert response.status == 200
    assert payload["status"] == "error"
    assert payload["query_id"] is None
    assert payload["failure"]["code"] == "query_history_write_failed"
    assert b"PRIVATE_QUERY_LEDGER_WRITE_FAILURE" not in response.body


def test_recall_routes_reject_bad_auth_without_planning_or_private_data(
    panel_fixture: PanelFixture,
) -> None:
    before = len(panel_fixture.reader.requests)
    for target in (
        "/api/recall/plan",
        "/api/recall/run",
        "/api/recall/rerun/alpha-old",
    ):
        response = panel_fixture.app.dispatch(
            "POST",
            target,
            headers={"Authorization": "Bearer wrong-token"},
            body=b'{"question":"private input"}',
        )
        assert response.status == 401
        assert json.loads(response.body)["error"]["code"] == "unauthorized"
        _assert_no_body_or_secret(response.body, panel_fixture.data_root)
    assert len(panel_fixture.reader.requests) == before


def _dispatch(app: PanelApplication, path: str) -> tuple[PanelResponse, dict[str, Any]]:
    response = app.dispatch("GET", path, headers=_headers())
    return response, json.loads(response.body)


def _post(
    app: PanelApplication, path: str, payload: dict[str, Any]
) -> tuple[PanelResponse, dict[str, Any]]:
    response = app.dispatch(
        "POST",
        path,
        headers=_headers(),
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    return response, json.loads(response.body)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_AUTH_TOKEN}"}


def _assert_no_body_or_secret(payload: bytes, data_root: Path) -> None:
    for forbidden in (
        _ALPHA_BODY_V1,
        _ALPHA_BODY_V2,
        _BETA_BODY,
        _TRASH_BODY,
        _AUTH_TOKEN,
        _READER_SECRET,
        str(data_root),
        str(data_root.parent),
    ):
        assert forbidden.encode("utf-8") not in payload


def _append_query(
    history: QueryHistoryStore,
    *,
    query_id: str,
    created_at: datetime,
    topic_results: tuple[tuple[str, str, str], ...],
    source: str,
) -> None:
    results = tuple(
        TopicQueryResult(
            topic_id=topic_id,
            topic_version=2 if topic_id == "alpha" else 1,
            status=status,  # type: ignore[arg-type]
            answer=f"private capsule for {topic_id}" if status == "answered" else None,
            source=result_source,  # type: ignore[arg-type]
            match_kind="exact" if result_source == "cache" else None,
            latency_ms=1,
        )
        for topic_id, status, result_source in topic_results
    )
    history.append(
        QueryRecord.create(
            query_id=query_id,
            created_at=created_at,
            question=f"private question {query_id}",
            selected_topics=results,
            final_answer="private final answer" if any(item.status == "answered" for item in results) else None,
            selection_reasons={item.topic_id: "selected" for item in results},
            source=source,  # type: ignore[arg-type]
            session_ref="private-session-cookie",
            total_latency_ms=2,
        )
    )
