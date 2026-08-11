from __future__ import annotations

import json
from pathlib import Path

from hermes_topic_memory.cli import main
from hermes_topic_memory.evidence import EvidenceLedger
from hermes_topic_memory.reader import ReaderRequest, ReaderResponse
from hermes_topic_memory.store import FileTopicStore


class FixedReader:
    def read(self, request: ReaderRequest) -> ReaderResponse:
        return ReaderResponse(answer="grounded CLI answer", model="cli-fake")


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
