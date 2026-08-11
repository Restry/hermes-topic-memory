from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

HERMES_AGENT_ROOT = Path.home() / ".hermes" / "hermes-agent"
if HERMES_AGENT_ROOT.is_dir():
    sys.path.insert(0, str(HERMES_AGENT_ROOT))

memory_provider = pytest.importorskip("agent.memory_provider")

from hermes_topic_memory.evidence import EvidenceLedger
from hermes_topic_memory.cache import QuestionCache
from hermes_topic_memory.importer import import_markdown_directory
from hermes_topic_memory.reader import ReaderRequest, ReaderResponse
from hermes_topic_memory.store import FileTopicStore
from hermes_topic_memory.updater import UpdateProposal
from plugin.hermes_topic_memory import HermesTopicMemoryProvider, register


class TopicReader:
    def __init__(self) -> None:
        self.requests: list[ReaderRequest] = []

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.requests.append(request)
        marker = '"topic_id":"'
        topic_id = request.prefix.split(marker, 1)[1].split('"', 1)[0]
        return ReaderResponse(answer=f"answer from {topic_id}", model="fake-reader")


class ExactAnswerReader(TopicReader):
    def __init__(self, answer: str) -> None:
        super().__init__()
        self.answer = answer

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.requests.append(request)
        return ReaderResponse(answer=self.answer, model="fake-reader")


class BlockingReader(TopicReader):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.started.set()
        assert self.release.wait(timeout=2)
        return super().read(request)


class RecordingUpdater:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def propose(self, memory, evidence):
        evidence_ids = tuple(record.evidence_id for record in evidence)
        self.calls.append((memory.metadata.topic_id, evidence_ids))
        return UpdateProposal(
            action="no_change",
            topic_id=memory.metadata.topic_id,
            evidence_ids=evidence_ids,
            reason="No canonical change required.",
        )


class RegistrationContext:
    def __init__(self) -> None:
        self.provider = None

    def register_memory_provider(self, provider) -> None:
        self.provider = provider


def make_profile(tmp_path: Path, *, name: str = "profile") -> tuple[Path, Path]:
    hermes_home = tmp_path / name
    data_root = hermes_home / "topic-memory" / "data"
    store = FileTopicStore(data_root)
    store.create_topic("alpha", "# Alpha\n\nAlpha canonical fact.\n", aliases=("a",))
    store.create_topic("beta", "# Beta\n\nBeta canonical fact.\n", aliases=("b",))
    store.create_topic("global", "# Global\n\nGlobal canonical fact.\n")
    (data_root / "topic-registry.json").write_text(
        json.dumps(
            {
                "default_topic": "global",
                "topics": [
                    {
                        "topic_id": "alpha",
                        "display_name": "Alpha",
                        "aliases": ["a"],
                        "chat_ids": ["chat-alpha"],
                    },
                    {
                        "topic_id": "beta",
                        "display_name": "Beta",
                        "aliases": ["b"],
                        "chat_ids": ["chat-beta"],
                    },
                    {
                        "topic_id": "global",
                        "display_name": "Global",
                        "aliases": [],
                        "chat_ids": [],
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return hermes_home, data_root


def configured_provider(
    tmp_path: Path,
    *,
    reader=None,
    updater=None,
    name: str = "profile",
) -> tuple[HermesTopicMemoryProvider, Path, Path]:
    hermes_home, data_root = make_profile(tmp_path, name=name)
    provider = HermesTopicMemoryProvider(reader=reader or TopicReader(), updater=updater)
    provider.save_config(
        {
            "data_root": "topic-memory/data",
            "reader_endpoint": "https://reader.invalid/v1",
            "reader_model": "fake-reader",
            "default_topic": "global",
            "batch_size": 8,
        },
        str(hermes_home),
    )
    return provider, hermes_home, data_root


def wait_for_prefetch(provider, query: str, session_id: str) -> str:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        value = provider.prefetch(query, session_id=session_id)
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("background prefetch did not complete")


def test_real_contract_registration_and_metadata() -> None:
    assert issubclass(HermesTopicMemoryProvider, memory_provider.MemoryProvider)
    provider = HermesTopicMemoryProvider(reader=TopicReader())
    assert provider.name == "hermes_topic_memory"
    assert provider.is_available() is True

    ctx = RegistrationContext()
    register(ctx)
    assert isinstance(ctx.provider, HermesTopicMemoryProvider)

    plugin_dir = Path(__file__).parents[1] / "plugin" / "hermes_topic_memory"
    assert (plugin_dir / "plugin.yaml").is_file()
    assert (plugin_dir / "README.md").is_file()

def test_prompt_and_tool_descriptions_route_named_archives_to_recall() -> None:
    provider = HermesTopicMemoryProvider(reader=TopicReader())
    schemas = {schema["name"]: schema for schema in provider.get_tool_schemas()}
    prompt = provider.system_prompt_block().casefold()
    recall_description = schemas["topic_memory_recall"]["description"].casefold()
    topics_description = schemas["topic_memory_topics"]["description"].casefold()

    query_description = schemas["topic_memory_recall"]["parameters"]["properties"]["query"][
        "description"
    ].casefold()
    assert "named project or topic archive" in prompt
    assert "must use topic_memory_recall" in prompt
    assert "topic_memory_topics is for discovery only" in prompt
    assert "named project or topic archive" in recall_description
    assert "discovery only" in topics_description
    assert "not for archive recall" in topics_description
    assert "verbatim" in prompt
    assert "never invent or rewrite dates" in prompt
    assert "verbatim" in recall_description
    assert "never invent or rewrite dates" in query_description
    for instruction in (prompt, recall_description):
        assert "successful recall" in instruction
        assert "return only verbatim_answer exactly as received" in instruction
        assert "never add, remove, infer, normalize, or alter" in instruction
        assert "dates, times, or years" in instruction


def test_successful_recall_exposes_exact_verbatim_relay_contract(tmp_path: Path) -> None:
    expected = "2031-07-08 09:45，Phase 3 archived (commit abc1234)."
    provider, hermes_home, _data_root = configured_provider(
        tmp_path, reader=ExactAnswerReader(expected)
    )
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")

    results = [
        json.loads(
            provider.handle_tool_call(
                "topic_memory_recall",
                {"query": "What happened on 2031-07-08 at 09:45?", "topic": "alpha"},
                session_id="session-a",
            )
        )
        for _ in range(2)
    ]

    assert [result["source"] for result in results] == ["reader", "cache"]
    for result in results:
        assert result["answer"] == expected
        assert result["verbatim_answer"] == expected
        assert result["response_policy"] == "copy_verbatim_answer_exactly"
    provider.shutdown()


def test_failed_recall_result_keeps_existing_shape(tmp_path: Path) -> None:
    provider, hermes_home, _data_root = configured_provider(
        tmp_path, reader=ExactAnswerReader("")
    )
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")

    result = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "What is archived for Alpha?", "topic": "alpha"},
            session_id="session-a",
        )
    )

    assert result == {
        "answer": None,
        "failure": {
            "attempts": 2,
            "code": "answer_empty",
            "message": "Reader returned an empty answer after one correction",
        },
        "source": "failure",
        "topic_id": "alpha",
        "topic_version": 1,
    }
    provider.shutdown()


def test_profile_safe_initialize_static_prompt_and_secret_safe_config(tmp_path: Path) -> None:
    provider, hermes_home, data_root = configured_provider(tmp_path)
    provider.save_config(
        {
            "data_root": "topic-memory/data",
            "reader_endpoint": "https://reader.invalid/v1",
            "reader_model": "fake-reader",
            "api_key": "must-not-be-written",
        },
        str(hermes_home),
    )
    config_path = hermes_home / "topic-memory" / "config.json"
    saved = config_path.read_text(encoding="utf-8")
    assert "must-not-be-written" not in saved
    assert "api_key" not in saved

    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")
    assert provider.system_prompt_block() == provider.system_prompt_block()
    assert str(hermes_home) not in provider.system_prompt_block()
    assert provider.backup_paths() == []
    assert provider._data_root == data_root
    assert (Path.home() / ".hermes" / "topic-memory" / "data").resolve() != data_root.resolve()
    provider.shutdown()

def test_background_prefetch_is_non_blocking_and_session_topic_isolation(tmp_path: Path) -> None:
    reader = BlockingReader()
    provider, hermes_home, _data_root = configured_provider(tmp_path, reader=reader)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")

    provider.queue_prefetch("status", session_id="session-a")
    assert reader.started.wait(timeout=1)
    assert provider.prefetch("status", session_id="session-a") == ""
    assert provider.prefetch("status", session_id="other-session") == ""

    reader.release.set()
    alpha = wait_for_prefetch(provider, "status", "session-a")
    assert "answer from alpha" in alpha
    assert "beta" not in alpha

    provider.on_session_switch("session-b", chat_id="chat-beta", reset=True)
    provider.queue_prefetch("status", session_id="session-b")
    beta = wait_for_prefetch(provider, "status", "session-b")
    assert "answer from beta" in beta
    assert provider.prefetch("status", session_id="session-a") == ""
    provider.shutdown()


def test_sync_turn_returns_before_io_and_hooks_consolidate_current_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = RecordingUpdater()
    provider, hermes_home, data_root = configured_provider(tmp_path, updater=updater)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")

    started = threading.Event()
    release = threading.Event()
    original_append = provider._ledger.append_turn

    def blocking_append(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2)
        return original_append(*args, **kwargs)

    monkeypatch.setattr(provider._ledger, "append_turn", blocking_append)
    provider.sync_turn("new fact", "ack", session_id="session-a")
    assert started.wait(timeout=1)
    assert EvidenceLedger(data_root).records("alpha") == ()

    release.set()
    summary = provider.on_pre_compress([])
    assert isinstance(summary, str)
    assert len(EvidenceLedger(data_root).records("alpha")) == 1
    assert updater.calls and updater.calls[0][0] == "alpha"

    provider.sync_turn("second fact", "ack", session_id="session-a")
    provider.on_session_end([])
    assert len(EvidenceLedger(data_root).records("alpha")) == 2
    assert len(updater.calls) == 2
    provider.shutdown()


def test_tools_recall_topics_and_evidence_backed_explicit_update(tmp_path: Path) -> None:
    provider, hermes_home, data_root = configured_provider(tmp_path)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")
    assert [schema["name"] for schema in provider.get_tool_schemas()] == [
        "topic_memory_recall",
        "topic_memory_update",
        "topic_memory_topics",
    ]

    topics = json.loads(provider.handle_tool_call("topic_memory_topics", {}))
    assert {item["topic_id"] for item in topics["topics"]} == {"alpha", "beta", "global"}
    recall = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall", {"query": "status", "topic": "b"}, session_id="session-a"
        )
    )
    assert recall["topic_id"] == "beta"
    assert recall["answer"] == "answer from beta"

    evidence = EvidenceLedger(data_root).append_turn("alpha", "session-a", "replace fact", "ok")
    update = json.loads(
        provider.handle_tool_call(
            "topic_memory_update",
            {
                "body": "# Alpha\n\nUpdated verified fact.\n",
                "evidence_ids": [evidence.evidence_id],
                "reason": "The completed turn supersedes the old fact.",
            },
            session_id="session-a",
        )
    )
    assert update["status"] == "updated"
    assert update["topic_id"] == "alpha"
    assert FileTopicStore(data_root).get_topic("alpha").metadata.version == 2

    unknown = json.loads(provider.handle_tool_call("unknown", {}))
    assert unknown == {"error": "unknown_tool"}
    provider.shutdown()

def test_recall_query_topic_overrides_session_but_explicit_topic_wins(tmp_path: Path) -> None:
    provider, hermes_home, _data_root = configured_provider(tmp_path)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")

    session_recall = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall", {"query": "current status"}, session_id="session-a"
        )
    )
    query_switch = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "What is archived for Beta?"},
            session_id="session-a",
        )
    )
    explicit = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "What is archived for Beta?", "topic": "a"},
            session_id="session-a",
        )
    )

    assert session_recall["topic_id"] == "alpha"
    assert query_switch["topic_id"] == "beta"
    assert query_switch["answer"] == "answer from beta"
    assert explicit["topic_id"] == "alpha"
    provider.shutdown()


def test_turn_start_raw_question_is_canonical_for_reader_and_cache(
    tmp_path: Path,
) -> None:
    reader = TopicReader()
    provider, hermes_home, data_root = configured_provider(tmp_path, reader=reader)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")
    raw = "Alpha 项目6月28日下午4:30做了什么？请基于主题记忆回答。"
    rewritten = "Alpha 项目2024年6月28日下午4:30完成了哪些工作？"

    provider.on_turn_start(1, raw)
    first = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall", {"query": rewritten}, session_id="session-a"
        )
    )
    cached = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall", {"query": rewritten}, session_id="session-a"
        )
    )

    assert first["source"] == "reader"
    assert cached["source"] == "cache"
    assert [request.question for request in reader.requests] == [raw]
    records = QuestionCache(data_root).records("alpha")
    assert [record.question for record in records] == [raw]
    assert "2024" not in records[0].normalized_question

    provider.on_session_switch("session-b", chat_id="chat-beta", reset=True)
    assert provider._raw_turn_questions == {}
    direct_query = "What is archived for Beta?"
    direct = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall", {"query": direct_query}, session_id="session-b"
        )
    )
    assert direct["topic_id"] == "beta"
    assert reader.requests[-1].question == direct_query

    provider.shutdown()
    assert provider._raw_turn_questions == {}

def test_recall_query_ambiguous_or_unknown_topic_never_uses_session_fallback(
    tmp_path: Path,
) -> None:
    reader = TopicReader()
    provider, hermes_home, _data_root = configured_provider(tmp_path, reader=reader)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")

    ambiguous = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "Compare the Alpha and Beta project archives."},
            session_id="session-a",
        )
    )
    unknown = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "What is archived for the Missing Project?"},
            session_id="session-a",
        )
    )

    assert ambiguous == {"error": "invalid_request"}
    assert unknown == {"error": "invalid_request"}
    assert reader.requests == []
    provider.shutdown()

def test_imported_aliases_switch_topics_within_one_provider_session(tmp_path: Path) -> None:
    hermes_home = tmp_path / "profile"
    data_root = hermes_home / "topic-memory" / "data"
    source = tmp_path / "source"
    alpha_source = source / "alpha" / "🧭 北斗 (beidou) — 话题记忆.md"
    beta_source = source / "dm" / "🐎 马夫 (mafu) — 话题记忆.md"
    alpha_source.parent.mkdir(parents=True)
    beta_source.parent.mkdir(parents=True)
    alpha_source.write_text("# 北斗\n\nAlpha-only archive.\n", encoding="utf-8")
    beta_source.write_text("# 马夫\n\nBeta-only archive.\n", encoding="utf-8")
    imported = import_markdown_directory(source, data_root)
    assert set(imported.topic_ids) == {"alpha-beidou", "dm-mafu"}

    registry_path = data_root / "topic-registry.json"
    registry_value = json.loads(registry_path.read_text(encoding="utf-8"))
    for item in registry_value["topics"]:
        item["chat_ids"] = ["chat-alpha"] if item["topic_id"] == "alpha-beidou" else []
    registry_path.write_text(
        json.dumps(registry_value, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    reader = TopicReader()
    provider = HermesTopicMemoryProvider(reader=reader)
    provider.save_config(
        {
            "data_root": "topic-memory/data",
            "reader_endpoint": "https://reader.invalid/v1",
            "reader_model": "fake-reader",
        },
        str(hermes_home),
    )
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")

    first = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall", {"query": "current status"}, session_id="session-a"
        )
    )
    switched = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "你帮我查一下马夫这个项目6月28号下午4:30做了什么"},
            session_id="session-a",
        )
    )
    switched_back = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "What is in the beidou project archive?"},
            session_id="session-a",
        )
    )

    assert [first["topic_id"], switched["topic_id"], switched_back["topic_id"]] == [
        "alpha-beidou",
        "dm-mafu",
        "alpha-beidou",
    ]
    assert first["answer"] == "answer from alpha-beidou"
    assert switched["answer"] == "answer from dm-mafu"
    assert switched_back["answer"] == "answer from alpha-beidou"
    provider.shutdown()


def test_imported_51_topic_provider_has_no_default_or_cross_topic_fallback(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "profile"
    data_root = hermes_home / "topic-memory" / "data"
    source = tmp_path / "source"
    source.mkdir()
    for index in range(51):
        (source / f"document-{index:02d}.md").write_text(
            f"# Document {index}\n\nIsolated fact {index}.\n", encoding="utf-8"
        )
    imported = import_markdown_directory(source, data_root)
    registry_path = data_root / "topic-registry.json"
    registry_value = json.loads(registry_path.read_text(encoding="utf-8"))
    assert "default_topic" not in registry_value
    for index, item in enumerate(registry_value["topics"]):
        item["chat_ids"] = [f"chat-{index}"]
    registry_path.write_text(json.dumps(registry_value) + "\n", encoding="utf-8")

    reader = TopicReader()
    provider = HermesTopicMemoryProvider(reader=reader)
    provider.save_config(
        {
            "data_root": "topic-memory/data",
            "reader_endpoint": "https://reader.invalid/v1",
            "reader_model": "fake-reader",
        },
        str(hermes_home),
    )
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-0")

    implicit = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall", {"query": "status"}, session_id="session-a"
        )
    )
    explicit = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "status", "topic": imported.topic_ids[-1]},
            session_id="session-a",
        )
    )
    missing = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "status"},
            session_id="unknown-session",
            chat_id="unknown-chat",
        )
    )

    assert implicit["topic_id"] == imported.topic_ids[0]
    assert explicit["topic_id"] == imported.topic_ids[-1]
    assert implicit["topic_id"] != explicit["topic_id"]
    assert missing == {"error": "invalid_request"}
    provider.shutdown()


def test_config_schema_marks_only_api_key_secret_and_non_primary_context_skips_writes(
    tmp_path: Path,
) -> None:
    provider, hermes_home, data_root = configured_provider(tmp_path)
    schema = provider.get_config_schema()
    secret_fields = [field for field in schema if field.get("secret")]
    assert secret_fields == [
        next(field for field in schema if field["key"] == "api_key")
    ]
    assert secret_fields[0]["env_var"] == "HERMES_TOPIC_MEMORY_API_KEY"

    provider.initialize(
        "subagent-session",
        hermes_home=str(hermes_home),
        chat_id="chat-alpha",
        agent_context="subagent",
    )
    provider.sync_turn("private transient prompt", "answer", session_id="subagent-session")
    provider.shutdown()
    assert EvidenceLedger(data_root).records("alpha") == ()
