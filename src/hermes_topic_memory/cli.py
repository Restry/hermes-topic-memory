from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Sequence

from .cache import QuestionCache
from .dashboard import generate_dashboard
from .engine import RecallEngine
from .multi_recall import MultiRecallEngine, ReaderCapsuleSynthesizer, TopicSelection
from .evidence import EvidenceLedger
from .operations import apply_evidence_update
from .importer import import_markdown_directory
from .reader import OpenAICompatibleReader, Reader
from .query_history import QueryHistoryStore
from .router import TopicRegistry
from .store import FileTopicStore
from .validation import validate_data_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate a Hermes Topic Memory data root")
    parser.add_argument("--data-root", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="Create one canonical topic")
    initialize.add_argument("topic_id")
    initialize.add_argument("--display-name")
    initialize.add_argument("--alias", action="append", default=[])
    initialize.add_argument("--chat-id", action="append", default=[])
    initialize.add_argument("--body-file", required=True, type=Path)
    initialize.add_argument("--default", action="store_true")

    subparsers.add_parser("topics", help="List registered topics")

    recall = subparsers.add_parser("recall", help="Recall through the configured Reader")
    recall.add_argument("query")
    recall.add_argument("--topic")
    recall.add_argument("--chat-id")

    recall_multi = subparsers.add_parser(
        "recall-multi", help="Recall independently from one to eight selected topics"
    )
    recall_multi.add_argument("query")
    recall_multi.add_argument("--topic", action="append", required=True)
    recall_multi.add_argument("--reason", action="append", required=True)

    update = subparsers.add_parser("update", help="Apply an evidence-backed replacement")
    update.add_argument("topic")
    update.add_argument("--body-file", required=True, type=Path)
    update.add_argument("--evidence-id", required=True, action="append")
    update.add_argument("--reason", required=True)

    rollback = subparsers.add_parser("rollback", help="Restore an earlier body as a new version")
    rollback.add_argument("topic")
    rollback.add_argument("version", type=int)

    subparsers.add_parser("validate", help="Validate registry and canonical topic files")

    importer = subparsers.add_parser(
        "import-markdown", help="Import one topic per Markdown file from a directory"
    )
    importer.add_argument("--source-dir", required=True, type=Path)
    importer.add_argument("--dry-run", action="store_true")
    importer.add_argument("--replace", action="store_true")

    dashboard = subparsers.add_parser(
        "dashboard", help="Generate a self-contained metadata dashboard"
    )
    dashboard.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None, *, reader: Reader | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_root = args.data_root.expanduser().resolve()
    if args.command == "init":
        result = _init_topic(data_root, args)
    elif args.command == "topics":
        result = _topics(data_root)
    elif args.command == "recall":
        result = _recall(data_root, args, reader)
    elif args.command == "recall-multi":
        result = _recall_multi(data_root, args, reader)
    elif args.command == "update":
        result = _update(data_root, args)
    elif args.command == "rollback":
        result = _rollback(data_root, args)
    elif args.command == "import-markdown":
        result = import_markdown_directory(
            args.source_dir, data_root, dry_run=args.dry_run, replace=args.replace
        ).to_dict()
    elif args.command == "dashboard":
        result = generate_dashboard(data_root, args.output).to_dict()
    else:
        result = _validate(data_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _registry_path(data_root: Path) -> Path:
    return data_root / "topic-registry.json"


def _load_registry_value(data_root: Path) -> dict:
    path = _registry_path(data_root)
    if not path.is_file():
        raise ValueError(f"topic registry does not exist: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("topic registry root must be an object")
    TopicRegistry.from_dict(value)
    return value


def _init_topic(data_root: Path, args: argparse.Namespace) -> dict:
    body = args.body_file.read_text(encoding="utf-8")
    registry_path = _registry_path(data_root)
    if registry_path.exists():
        registry_value = _load_registry_value(data_root)
    else:
        registry_value = {"topics": []}
    topic = FileTopicStore(data_root).create_topic(
        args.topic_id,
        body,
        display_name=args.display_name or args.topic_id,
        aliases=tuple(args.alias),
    )
    registry_value["topics"].append(
        {
            "topic_id": args.topic_id,
            "display_name": args.display_name or args.topic_id,
            "aliases": list(args.alias),
            "chat_ids": list(args.chat_id),
        }
    )
    if args.default:
        registry_value["default_topic"] = args.topic_id
    TopicRegistry.from_dict(registry_value)
    _atomic_json_write(registry_path, registry_value)
    return {"status": "created", "topic_id": args.topic_id, "version": topic.metadata.version}


def _topics(data_root: Path) -> dict:
    value = _load_registry_value(data_root)
    registry = TopicRegistry.from_dict(value)
    store = FileTopicStore(data_root)
    topics = []
    for config in registry.topics:
        memory = store.get_topic(config.topic_id)
        topics.append(
            {
                "topic_id": config.topic_id,
                "display_name": config.display_name,
                "aliases": list(config.aliases),
                "chat_ids": list(config.chat_ids),
                "version": memory.metadata.version,
                "source_relative_path": config.source_relative_path,
            }
        )
    return {"default_topic": value.get("default_topic"), "topics": topics}


def _recall(data_root: Path, args: argparse.Namespace, reader: Reader | None) -> dict:
    registry = TopicRegistry.from_file(_registry_path(data_root))
    decision = registry.route(explicit_topic=args.topic, chat_id=args.chat_id)
    active_reader = reader or _reader_from_environment()
    result = RecallEngine(
        store=FileTopicStore(data_root),
        cache=QuestionCache(data_root),
        reader=active_reader,
    ).recall(decision.topic_id, args.query)
    response = {
        "topic_id": result.topic_id,
        "topic_version": result.topic_version,
        "answer": result.answer,
        "source": result.source,
    }
    if result.failure is not None:
        response["failure"] = {
            "code": result.failure.code,
            "message": result.failure.message,
            "attempts": result.failure.attempts,
        }
    return response


def _recall_multi(data_root: Path, args: argparse.Namespace, reader: Reader | None) -> dict:
    if len(args.topic) != len(args.reason):
        raise ValueError("--topic and --reason must be supplied the same number of times")
    if not 1 <= len(args.topic) <= 8:
        raise ValueError("recall-multi requires 1 to 8 topic/reason pairs")
    registry = TopicRegistry.from_file(_registry_path(data_root))
    active_reader = reader or _reader_from_environment()
    selections = tuple(
        TopicSelection(topic, reason)
        for topic, reason in zip(args.topic, args.reason, strict=True)
    )
    result = MultiRecallEngine(
        recall_engine=RecallEngine(
            store=FileTopicStore(data_root),
            cache=QuestionCache(data_root),
            reader=active_reader,
        ),
        registry=registry,
        synthesizer=ReaderCapsuleSynthesizer(active_reader),
        query_history=QueryHistoryStore(data_root),
    ).recall_multi(
        question=args.query,
        selections=selections,
        source="cli",
    )
    reasons: dict[str, str] = {}
    for selection in selections:
        topic_id = registry.resolve_name(selection.topic_id, context="selected topic")
        reasons.setdefault(topic_id, selection.reason)
    topic_results = []
    for item in result.topic_results:
        value = {
            "topic_id": item.topic_id,
            "topic_version": item.topic_version,
            "status": item.status,
            "answer": item.answer,
            "source": item.source,
            "match_kind": item.match_kind,
            "latency_ms": item.latency_ms,
            "reason": reasons[item.topic_id],
        }
        if item.failure is not None:
            value["failure"] = {
                "code": item.failure.code,
                "message": item.failure.message,
                "attempts": item.failure.attempts,
            }
        topic_results.append(value)
    response = {
        "question": result.question,
        "query_id": result.query_id,
        "final_answer": result.final_answer,
        "synthesis_source": result.synthesis_source,
        "scope_fingerprint": result.scope_fingerprint,
        "topic_results": topic_results,
    }
    if result.failure is not None:
        response["failure"] = {
            "code": result.failure.code,
            "message": result.failure.message,
            "attempts": result.failure.attempts,
        }
    return response


def _update(data_root: Path, args: argparse.Namespace) -> dict:
    registry = TopicRegistry.from_file(_registry_path(data_root))
    topic_id = registry.resolve_name(args.topic, context="update topic")
    result = apply_evidence_update(
        store=FileTopicStore(data_root),
        ledger=EvidenceLedger(data_root),
        topic_id=topic_id,
        body=args.body_file.read_text(encoding="utf-8"),
        evidence_ids=tuple(args.evidence_id),
        reason=args.reason,
    )
    return {
        "status": result.status,
        "topic_id": result.topic_id,
        "previous_version": result.previous_version,
        "current_version": result.current_version,
    }


def _rollback(data_root: Path, args: argparse.Namespace) -> dict:
    registry = TopicRegistry.from_file(_registry_path(data_root))
    topic_id = registry.resolve_name(args.topic, context="rollback topic")
    memory = FileTopicStore(data_root).rollback_topic(topic_id, args.version)
    return {"status": "rolled_back", "topic_id": topic_id, "version": memory.metadata.version}


def _validate(data_root: Path) -> dict:
    return {"status": "valid", "topics": validate_data_root(data_root)}


def _reader_from_environment() -> OpenAICompatibleReader:
    endpoint = os.environ.get("HERMES_TOPIC_MEMORY_READER_ENDPOINT", "")
    model = os.environ.get("HERMES_TOPIC_MEMORY_READER_MODEL", "")
    if not endpoint or not model:
        raise ValueError(
            "Reader requires HERMES_TOPIC_MEMORY_READER_ENDPOINT and "
            "HERMES_TOPIC_MEMORY_READER_MODEL"
        )
    return OpenAICompatibleReader(
        endpoint=endpoint,
        model=model,
        api_key=os.environ.get("HERMES_TOPIC_MEMORY_API_KEY"),
    )


def _atomic_json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
