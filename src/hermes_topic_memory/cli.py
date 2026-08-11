from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Sequence

from .cache import QuestionCache
from .engine import RecallEngine
from .evidence import EvidenceLedger
from .operations import apply_evidence_update
from .reader import OpenAICompatibleReader, Reader
from .router import TopicRegistry
from .store import FileTopicStore


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

    update = subparsers.add_parser("update", help="Apply an evidence-backed replacement")
    update.add_argument("topic")
    update.add_argument("--body-file", required=True, type=Path)
    update.add_argument("--evidence-id", required=True, action="append")
    update.add_argument("--reason", required=True)

    rollback = subparsers.add_parser("rollback", help="Restore an earlier body as a new version")
    rollback.add_argument("topic")
    rollback.add_argument("version", type=int)

    subparsers.add_parser("validate", help="Validate registry and canonical topic files")
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
    elif args.command == "update":
        result = _update(data_root, args)
    elif args.command == "rollback":
        result = _rollback(data_root, args)
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
    registry = TopicRegistry.from_file(_registry_path(data_root))
    store = FileTopicStore(data_root)
    registered = {topic.topic_id for topic in registry.topics}
    for topic_id in registered:
        store.get_topic(topic_id)
    topic_dirs = {
        path.name
        for path in store.topics_dir.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    } if store.topics_dir.is_dir() else set()
    if topic_dirs != registered:
        raise ValueError("registry and canonical topic directories differ")
    return {"status": "valid", "topics": len(registered)}


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
