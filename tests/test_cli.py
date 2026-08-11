from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_topic_memory.cli import main
from hermes_topic_memory.evidence import EvidenceLedger
from hermes_topic_memory.query_history import QueryHistoryStore
from hermes_topic_memory.reader import ReaderRequest, ReaderResponse
from hermes_topic_memory.store import FileTopicStore


class FixedReader:
    def read(self, request: ReaderRequest) -> ReaderResponse:
        return ReaderResponse(answer="grounded CLI answer", model="cli-fake")


class MultiCliReader:
    def __init__(self) -> None:
        self.requests: list[ReaderRequest] = []

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.requests.append(request)
        if '"topic_id":"alpha"' in request.prefix:
            return ReaderResponse(answer="alpha CLI capsule", model="cli-reader")
        if '"topic_id":"beta"' in request.prefix:
            return ReaderResponse(answer="beta CLI capsule", model="cli-reader")
        assert "Alpha private body" not in request.prefix
        assert "Beta private body" not in request.prefix
        return ReaderResponse(
            answer="[Alpha/alpha] alpha CLI capsule; [Beta/beta] beta CLI capsule",
            model="cli-synthesizer",
        )


def invoke(capsys, argv: list[str], *, reader=None) -> dict:
    assert main(argv, reader=reader) == 0
    output = capsys.readouterr().out
    return json.loads(output)


def test_cli_init_topics_validate_and_recall(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "data"
    body_file = tmp_path / "alpha.md"
    body_file.write_text("# Alpha\n\nCanonical CLI fact.\n", encoding="utf-8")
    base = ["--data-root", str(data_root)]

    created = invoke(
        capsys,
        base
        + [
            "init",
            "alpha",
            "--display-name",
            "Alpha",
            "--alias",
            "a",
            "--chat-id",
            "chat-alpha",
            "--body-file",
            str(body_file),
            "--default",
        ],
    )
    assert created == {"status": "created", "topic_id": "alpha", "version": 1}

    topics = invoke(capsys, base + ["topics"])
    assert topics["default_topic"] == "alpha"
    assert topics["topics"][0]["aliases"] == ["a"]

    validation = invoke(capsys, base + ["validate"])
    assert validation == {"status": "valid", "topics": 1}

    recall = invoke(
        capsys,
        base + ["recall", "what is current?", "--topic", "a"],
        reader=FixedReader(),
    )
    assert recall["answer"] == "grounded CLI answer"
    assert recall["topic_id"] == "alpha"


def test_cli_evidence_backed_update_and_rollback(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "data"
    old_body = tmp_path / "old.md"
    new_body = tmp_path / "new.md"
    old_body.write_text("# Alpha\n\nOld fact.\n", encoding="utf-8")
    new_body.write_text("# Alpha\n\nNew verified fact.\n", encoding="utf-8")
    base = ["--data-root", str(data_root)]
    invoke(capsys, base + ["init", "alpha", "--body-file", str(old_body), "--default"])
    evidence = EvidenceLedger(data_root).append_turn("alpha", "cli-session", "new fact", "ack")

    updated = invoke(
        capsys,
        base
        + [
            "update",
            "alpha",
            "--body-file",
            str(new_body),
            "--evidence-id",
            evidence.evidence_id,
            "--reason",
            "Completed turn supersedes the old fact.",
        ],
    )
    assert updated["status"] == "updated"
    assert updated["current_version"] == 2

    rolled_back = invoke(capsys, base + ["rollback", "alpha", "1"])
    assert rolled_back == {"status": "rolled_back", "topic_id": "alpha", "version": 3}
    assert FileTopicStore(data_root).get_topic("alpha").body == old_body.read_text(encoding="utf-8")


def test_cli_import_dry_run_import_and_dashboard(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.md").write_text("# One\n\nBody.\n", encoding="utf-8")
    data_root = tmp_path / "data"
    base = ["--data-root", str(data_root)]

    dry_run = invoke(
        capsys,
        base + ["import-markdown", "--source-dir", str(source), "--dry-run"],
    )
    assert dry_run["status"] == "dry-run"
    assert dry_run["topic_count"] == 1
    assert not data_root.exists()

    imported = invoke(
        capsys, base + ["import-markdown", "--source-dir", str(source)]
    )
    assert imported["status"] == "imported"
    assert imported["topic_count"] == 1
    assert "topic_ids" not in imported

    output = tmp_path / "dashboard.html"
    dashboard = invoke(capsys, base + ["dashboard", "--output", str(output)])
    assert dashboard["status"] == "generated"
    assert dashboard["topic_count"] == 1
    assert dashboard["output_bytes"] == len(output.read_bytes())


def test_cli_recall_multi_uses_paired_topics_and_returns_structured_json(
    tmp_path: Path, capsys
) -> None:
    data_root = tmp_path / "data"
    store = FileTopicStore(data_root)
    store.create_topic(
        "alpha", "# Alpha\n\nAlpha private body.\n", display_name="Alpha", aliases=("a",)
    )
    store.create_topic(
        "beta", "# Beta\n\nBeta private body.\n", display_name="Beta", aliases=("b",)
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
        + "\n",
        encoding="utf-8",
    )
    base = ["--data-root", str(data_root)]
    reader = MultiCliReader()

    result = invoke(
        capsys,
        base
        + [
            "recall-multi",
            "Compare Alpha and Beta.",
            "--topic",
            "a",
            "--reason",
            "Alpha is named",
            "--topic",
            "b",
            "--reason",
            "Beta is named",
        ],
        reader=reader,
    )

    assert result["question"] == "Compare Alpha and Beta."
    assert result["final_answer"] == (
        "[Alpha/alpha] alpha CLI capsule; [Beta/beta] beta CLI capsule"
    )
    assert isinstance(result["query_id"], str) and result["query_id"]
    assert len(result["scope_fingerprint"]) == 64
    assert [item["topic_id"] for item in result["topic_results"]] == ["alpha", "beta"]
    assert [item["topic_version"] for item in result["topic_results"]] == [1, 1]
    assert [item["source"] for item in result["topic_results"]] == ["reader", "reader"]
    assert all("body" not in item for item in result["topic_results"])
    records = QueryHistoryStore(data_root).list()
    assert len(records) == 1
    assert records[0].source == "cli"
    assert dict(records[0].selection_reasons) == {
        "alpha": "Alpha is named",
        "beta": "Beta is named",
    }

    with pytest.raises(ValueError, match="same number"):
        main(
            base
            + [
                "recall-multi",
                "Compare Alpha and Beta.",
                "--topic",
                "alpha",
                "--topic",
                "beta",
                "--reason",
                "only one reason",
            ],
            reader=reader,
        )
