from __future__ import annotations

import json
import logging
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


def configured_mafu_provider(
    tmp_path: Path, *, reader=None
) -> tuple[HermesTopicMemoryProvider, Path, Path]:
    hermes_home = tmp_path / "mafu-profile"
    data_root = hermes_home / "topic-memory" / "data"
    store = FileTopicStore(data_root)
    store.create_topic(
        "dm-mafu",
        "# 马夫\n\nMafu is the user's Feishu assistant.\n",
        aliases=("马夫", "mafu"),
    )
    (data_root / "topic-registry.json").write_text(
        json.dumps(
            {
                "topics": [
                    {
                        "topic_id": "dm-mafu",
                        "display_name": "马夫",
                        "aliases": ["mafu"],
                        "chat_ids": [],
                    }
                ]
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    provider = HermesTopicMemoryProvider(reader=reader or TopicReader())
    provider.save_config(
        {
            "data_root": "topic-memory/data",
            "reader_endpoint": "https://reader.invalid/v1",
            "reader_model": "fake-reader",
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
    update_schema = schemas["topic_memory_update"]
    update_description = update_schema["description"].casefold()
    update_properties = update_schema["parameters"]["properties"]
    body_description = update_properties["body"]["description"].casefold()
    append_description = update_properties["append_text"]["description"].casefold()

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
    assert update_schema["parameters"]["required"] == ["reason"]
    assert "body" in update_properties and "append_text" in update_properties
    assert update_properties["append_text"]["maxLength"] > 0
    assert "current user turn" in update_description
    assert "exactly one" in update_description
    assert "intentional complete replacement" in body_description
    assert "minimal user-requested markdown addition" in append_description
    assert "no invented facts" in append_description
    for instruction in (prompt, update_description):
        assert "must use append_text" in instruction
        assert "must not use topic_memory_recall" in instruction
        assert "request, expose, summarize, or reconstruct" in instruction


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

def test_current_turn_prefetch_synchronously_recalls_named_topic_without_queue(
    tmp_path: Path,
) -> None:
    reader = TopicReader()
    provider, hermes_home, _data_root = configured_mafu_provider(
        tmp_path, reader=reader
    )
    provider.initialize("fresh-session", hermes_home=str(hermes_home))
    query = "你跟我讲讲马夫是用来做什么的？"

    assert provider._session_topics == {}
    provider.on_turn_start(1, query, session_id="fresh-session")
    prefetched = provider.prefetch(query, session_id="fresh-session")

    assert prefetched == "[Topic Memory: dm-mafu v1]\nanswer from dm-mafu"
    assert [request.question for request in reader.requests] == [query]
    provider.shutdown()


def test_current_turn_prefetch_uses_existing_topic_cache_without_reader(
    tmp_path: Path,
) -> None:
    reader = TopicReader()
    provider, hermes_home, data_root = configured_mafu_provider(
        tmp_path, reader=reader
    )
    query = "你跟我讲讲马夫是用来做什么的？"
    QuestionCache(data_root).put(
        question=query,
        answer="cached mafu answer",
        topic_id="dm-mafu",
        topic_version=1,
        model="fake-reader",
        prompt_version="v1",
    )
    provider.initialize("fresh-session", hermes_home=str(hermes_home))

    provider.on_turn_start(1, query, session_id="fresh-session")
    prefetched = provider.prefetch(query, session_id="fresh-session")

    assert prefetched == "[Topic Memory: dm-mafu v1]\ncached mafu answer"
    assert reader.requests == []
    provider.shutdown()


def test_current_turn_prefetch_rejects_unmentioned_unknown_and_ambiguous_topics(
    tmp_path: Path,
) -> None:
    reader = TopicReader()
    provider, hermes_home, _data_root = configured_provider(tmp_path, reader=reader)
    provider.initialize("session-a", hermes_home=str(hermes_home))
    queries = (
        "今天天气怎么样？",
        "What is archived for the Missing Project?",
        "Compare the Alpha and Beta project archives.",
    )

    for turn_number, query in enumerate(queries, start=1):
        provider.on_turn_start(turn_number, query, session_id="session-a")
        assert provider.prefetch(query, session_id="session-a") == ""

    assert reader.requests == []
    provider.shutdown()


def test_successful_current_turn_prefetch_binds_follow_up_without_cross_session_leak(
    tmp_path: Path,
) -> None:
    reader = TopicReader()
    provider, hermes_home, _data_root = configured_provider(tmp_path, reader=reader)
    provider.initialize("session-a", hermes_home=str(hermes_home))
    alpha_query = "What is archived for Alpha?"
    beta_query = "What is archived for Beta?"
    follow_up = "继续说说它的职责。"

    provider.on_turn_start(1, alpha_query, session_id="session-a")
    assert "answer from alpha" in provider.prefetch(
        alpha_query, session_id="session-a"
    )
    provider.on_turn_start(2, beta_query, session_id="session-a")
    assert "answer from beta" in provider.prefetch(beta_query, session_id="session-a")
    provider.on_turn_start(3, follow_up, session_id="session-a")
    assert "answer from beta" in provider.prefetch(follow_up, session_id="session-a")

    assert provider._session_topics == {"session-a": "beta"}
    assert [request.question for request in reader.requests] == [
        alpha_query,
        beta_query,
        follow_up,
    ]

    provider.on_turn_start(1, follow_up, session_id="session-b")
    assert provider.prefetch(follow_up, session_id="session-b") == ""
    assert len(reader.requests) == 3
    provider.shutdown()


def test_current_turn_prefetch_failure_is_logged_and_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    provider, hermes_home, _data_root = configured_mafu_provider(tmp_path)
    provider.initialize("fresh-session", hermes_home=str(hermes_home))
    query = "你跟我讲讲马夫是用来做什么的？"

    def fail_recall(topic_id: str, question: str) -> None:
        raise RuntimeError(f"reader unavailable for {topic_id}: {question}")

    monkeypatch.setattr(provider._engine, "recall", fail_recall)
    provider.on_turn_start(1, query, session_id="fresh-session")
    with caplog.at_level(logging.DEBUG, logger="plugin.hermes_topic_memory"):
        assert provider.prefetch(query, session_id="fresh-session") == ""

    assert "Topic-memory current-turn prefetch failed" in caplog.text
    provider.shutdown()


def test_ready_background_prefetch_is_consumed_without_duplicate_sync_recall(
    tmp_path: Path,
) -> None:
    reader = BlockingReader()
    provider, hermes_home, _data_root = configured_mafu_provider(
        tmp_path, reader=reader
    )
    provider.initialize("fresh-session", hermes_home=str(hermes_home))
    query = "你跟我讲讲马夫是用来做什么的？"
    key = ("fresh-session", query)

    provider.queue_prefetch(query, session_id="fresh-session")
    assert reader.started.wait(timeout=1)
    reader.release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with provider._lock:
            if key in provider._prefetch_results:
                break
        time.sleep(0.01)
    else:
        raise AssertionError("background prefetch did not become ready")

    prefetched = provider.prefetch(query, session_id="fresh-session")
    assert prefetched == "[Topic Memory: dm-mafu v1]\nanswer from dm-mafu"
    assert [request.question for request in reader.requests] == [query]
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
                "topic": "alpha",
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
    assert update["evidence_ids"] == [evidence.evidence_id]
    assert update["evidence_source"] == "supplied"
    assert update["update_mode"] == "replace"
    assert "body" not in update and "append_text" not in update

    unknown = json.loads(provider.handle_tool_call("unknown", {}))
    assert unknown == {"error": "unknown_tool"}
    provider.shutdown()


def test_same_turn_append_without_evidence_ids_preserves_body_and_audits(
    tmp_path: Path,
) -> None:
    reader = TopicReader()
    provider, hermes_home, data_root = configured_provider(tmp_path, reader=reader)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")
    store = FileTopicStore(data_root)
    old_body = store.get_topic("alpha").body
    append_text = "## Subjects\n\n- Information technology"
    raw_user_turn = "Please add information technology to Alpha."
    provider.on_turn_start(1, raw_user_turn, session_id="session-a")

    update = json.loads(
        provider.handle_tool_call(
            "topic_memory_update",
            {
                "topic": "alpha",
                "append_text": append_text,
                "reason": "The current user explicitly requested this addition.",
            },
            session_id="session-a",
        )
    )

    expected_body = old_body + "\n" + append_text + "\n"
    assert update["status"] == "updated"
    assert update["update_mode"] == "append"
    assert (update["previous_version"], update["current_version"]) == (1, 2)
    assert update["evidence_source"] == "current_turn"
    assert len(update["evidence_ids"]) == 1
    assert "body" not in update and "append_text" not in update and "reason" not in update
    assert reader.requests == []
    records = EvidenceLedger(data_root).records("alpha")
    assert [record.evidence_id for record in records] == update["evidence_ids"]
    assert [(record.session_id, record.user_text, record.assistant_text) for record in records] == [
        ("session-a", raw_user_turn, "")
    ]
    memory = store.get_topic("alpha")
    assert memory.metadata.version == 2
    assert memory.body.encode("utf-8").startswith(old_body.encode("utf-8"))
    assert memory.body == expected_body
    assert store.get_topic("beta").body == "# Beta\n\nBeta canonical fact.\n"
    audit = json.loads(
        (data_root / "update-proposals" / "alpha.jsonl").read_text(encoding="utf-8")
    )
    assert audit["status"] == "applied"
    assert audit["evidence_ids"] == update["evidence_ids"]
    assert (audit["previous_version"], audit["current_version"]) == (1, 2)
    assert audit["proposed_body"] == expected_body
    provider.shutdown()

@pytest.mark.parametrize(
    ("old_body", "append_text", "expected_suffix"),
    [
        (
            "# Alpha\n\nNo terminal newline.",
            "\n## Added\n\n- item\n\n",
            "\n\n## Added\n\n- item\n",
        ),
        (
            "# Alpha\n\nOne terminal newline.\n",
            "## Added\n\n- item",
            "\n## Added\n\n- item\n",
        ),
        (
            "# Alpha\n\nBlank line already.\n\n",
            "\n## Added\n\n- item\n",
            "## Added\n\n- item\n",
        ),
    ],
)
def test_append_newline_boundary_is_deterministic(
    tmp_path: Path,
    old_body: str,
    append_text: str,
    expected_suffix: str,
) -> None:
    provider, hermes_home, data_root = configured_provider(tmp_path)
    store = FileTopicStore(data_root)
    initial = store.update_topic("alpha", old_body)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")
    provider.on_turn_start(1, "Add one item to Alpha.", session_id="session-a")

    result = json.loads(
        provider.handle_tool_call(
            "topic_memory_update",
            {
                "topic": "alpha",
                "append_text": append_text,
                "reason": "The user requested the item.",
            },
            session_id="session-a",
        )
    )

    memory = store.get_topic("alpha")
    assert result["update_mode"] == "append"
    assert (result["previous_version"], result["current_version"]) == (
        initial.metadata.version,
        initial.metadata.version + 1,
    )
    assert memory.body.encode("utf-8").startswith(old_body.encode("utf-8"))
    assert memory.body == old_body + expected_suffix
    provider.shutdown()


@pytest.mark.parametrize(
    "update_args",
    [
        {"reason": "No content argument."},
        {
            "body": "# Alpha\n\nReplacement.\n",
            "append_text": "- Addition",
            "reason": "Conflicting content arguments.",
        },
    ],
)
def test_update_requires_exactly_one_body_or_append_text(
    tmp_path: Path,
    update_args: dict[str, str],
) -> None:
    provider, hermes_home, data_root = configured_provider(tmp_path)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")
    provider.on_turn_start(1, "Change Alpha.", session_id="session-a")

    result = json.loads(
        provider.handle_tool_call(
            "topic_memory_update",
            {"topic": "alpha", **update_args},
            session_id="session-a",
        )
    )

    assert result == {"error": "invalid_update_arguments"}
    assert FileTopicStore(data_root).get_topic("alpha").metadata.version == 1
    assert EvidenceLedger(data_root).records("alpha") == ()
    provider.shutdown()


def test_empty_and_oversized_appends_fail_without_writes(tmp_path: Path) -> None:
    provider, hermes_home, data_root = configured_provider(tmp_path)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")
    provider.on_turn_start(1, "Add a bounded item to Alpha.", session_id="session-a")
    update_schema = {
        schema["name"]: schema for schema in provider.get_tool_schemas()
    }["topic_memory_update"]
    max_append_chars = update_schema["parameters"]["properties"]["append_text"][
        "maxLength"
    ]

    for append_text in ("", " \n\t", "x" * (max_append_chars + 1)):
        result = json.loads(
            provider.handle_tool_call(
                "topic_memory_update",
                {
                    "topic": "alpha",
                    "append_text": append_text,
                    "reason": "The user requested this addition.",
                },
                session_id="session-a",
            )
        )
        assert result == {"error": "invalid_update_arguments"}

    assert FileTopicStore(data_root).get_topic("alpha").metadata.version == 1
    assert EvidenceLedger(data_root).records("alpha") == ()
    assert not (data_root / "update-proposals" / "alpha.jsonl").exists()
    provider.shutdown()


def test_duplicate_append_has_specific_error_and_no_partial_write(tmp_path: Path) -> None:
    reader = TopicReader()
    provider, hermes_home, data_root = configured_provider(tmp_path, reader=reader)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")
    append_text = "- Information technology"
    args = {
        "topic": "alpha",
        "append_text": append_text,
        "reason": "The user requested this subject.",
    }
    provider.on_turn_start(1, "Add information technology.", session_id="session-a")
    first = json.loads(
        provider.handle_tool_call("topic_memory_update", args, session_id="session-a")
    )
    provider.on_turn_start(2, "Add information technology again.", session_id="session-a")

    duplicate = json.loads(
        provider.handle_tool_call("topic_memory_update", args, session_id="session-a")
    )

    assert first["status"] == "updated"
    assert duplicate == {"error": "append_duplicate"}
    memory = FileTopicStore(data_root).get_topic("alpha")
    assert memory.metadata.version == 2
    assert memory.body.count(append_text) == 1
    assert len(EvidenceLedger(data_root).records("alpha")) == 1
    audit_lines = (data_root / "update-proposals" / "alpha.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(audit_lines) == 1
    assert reader.requests == []
    provider.shutdown()


def test_append_uses_session_topic_and_invalidates_its_stale_cache(tmp_path: Path) -> None:
    reader = TopicReader()
    provider, hermes_home, data_root = configured_provider(tmp_path, reader=reader)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")
    store = FileTopicStore(data_root)
    old_alpha = store.get_topic("alpha")
    old_beta = store.get_topic("beta")
    before = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "status", "topic": "beta"},
            session_id="session-a",
        )
    )
    provider.on_turn_start(1, "Add the new Beta item.", session_id="session-a")

    update = json.loads(
        provider.handle_tool_call(
            "topic_memory_update",
            {
                "append_text": "- New Beta item",
                "reason": "The current user requested this Beta addition.",
            },
            session_id="session-a",
        )
    )
    provider.on_turn_start(2, "", session_id="session-a")
    after = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "status"},
            session_id="session-a",
        )
    )

    assert before["source"] == after["source"] == "reader"
    assert (before["topic_version"], after["topic_version"]) == (1, 2)
    assert update["topic_id"] == "beta"
    assert update["update_mode"] == "append"
    assert len(reader.requests) == 2
    assert store.get_topic("alpha") == old_alpha
    beta = store.get_topic("beta")
    assert beta.body.encode("utf-8").startswith(old_beta.body.encode("utf-8"))
    assert beta.metadata.version == old_beta.metadata.version + 1
    assert [record.topic_version for record in QuestionCache(data_root).records("beta")] == [
        1,
        2,
    ]
    provider.shutdown()


def test_update_failures_are_specific_and_leave_no_partial_update(tmp_path: Path) -> None:
    provider, hermes_home, data_root = configured_provider(tmp_path)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")
    replacement = "# Alpha\n\nCandidate replacement.\n"

    no_raw_turn = json.loads(
        provider.handle_tool_call(
            "topic_memory_update",
            {"topic": "alpha", "body": replacement, "reason": "Requested now."},
            session_id="session-a",
        )
    )
    assert no_raw_turn == {"error": "current_turn_evidence_unavailable"}

    provider.on_turn_start(1, "Keep the current Alpha body.", session_id="session-a")
    unchanged = json.loads(
        provider.handle_tool_call(
            "topic_memory_update",
            {
                "topic": "alpha",
                "body": "# Alpha\n\nAlpha canonical fact.\n",
                "reason": "No actual content change.",
            },
            session_id="session-a",
        )
    )
    assert unchanged == {"error": "body_unchanged"}

    missing_evidence = json.loads(
        provider.handle_tool_call(
            "topic_memory_update",
            {
                "topic": "alpha",
                "body": replacement,
                "evidence_ids": ["missing-evidence"],
                "reason": "Claims an unavailable source.",
            },
            session_id="session-a",
        )
    )
    assert missing_evidence == {"error": "evidence_not_found"}

    invalid = json.loads(
        provider.handle_tool_call(
            "topic_memory_update",
            {"topic": "alpha", "body": "", "reason": "Invalid empty body."},
            session_id="session-a",
        )
    )
    assert invalid == {"error": "invalid_update_arguments"}
    memory = FileTopicStore(data_root).get_topic("alpha")
    assert memory.metadata.version == 1
    assert memory.body == "# Alpha\n\nAlpha canonical fact.\n"
    assert EvidenceLedger(data_root).records("alpha") == ()
    provider.shutdown()


def test_ambiguous_update_topic_fails_before_evidence_or_body_write(tmp_path: Path) -> None:
    provider, hermes_home, data_root = configured_provider(tmp_path)
    registry_path = data_root / "topic-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    for topic in registry["topics"]:
        if topic["topic_id"] in {"alpha", "beta"}:
            topic["display_name"] = "Shared Archive"
    registry_path.write_text(json.dumps(registry) + "\n", encoding="utf-8")
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")
    provider.on_turn_start(1, "Update the shared archive.", session_id="session-a")

    result = json.loads(
        provider.handle_tool_call(
            "topic_memory_update",
            {
                "topic": "Shared Archive",
                "body": "# Shared\n\nChanged.\n",
                "reason": "User requested a change.",
            },
            session_id="session-a",
        )
    )

    assert result == {"error": "topic_ambiguous"}
    assert EvidenceLedger(data_root).records("alpha") == ()
    assert EvidenceLedger(data_root).records("beta") == ()
    assert FileTopicStore(data_root).get_topic("alpha").metadata.version == 1
    assert FileTopicStore(data_root).get_topic("beta").metadata.version == 1
    provider.shutdown()


def test_update_without_safely_resolved_topic_fails_closed(tmp_path: Path) -> None:
    provider, hermes_home, data_root = configured_provider(tmp_path)
    registry_path = data_root / "topic-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry.pop("default_topic")
    registry["topics"] = [
        topic for topic in registry["topics"] if topic["topic_id"] != "global"
    ]
    for topic in registry["topics"]:
        topic["chat_ids"] = []
    registry_path.write_text(json.dumps(registry) + "\n", encoding="utf-8")
    provider.save_config(
        {
            "data_root": "topic-memory/data",
            "reader_endpoint": "https://reader.invalid/v1",
            "reader_model": "fake-reader",
        },
        str(hermes_home),
    )
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="unknown-chat")
    provider.on_turn_start(1, "继续记录这个事实", session_id="session-a")

    result = json.loads(
        provider.handle_tool_call(
            "topic_memory_update",
            {"body": "# Unknown\n\nChanged.\n", "reason": "User requested it."},
            session_id="session-a",
        )
    )

    assert result == {"error": "topic_unresolved"}
    assert EvidenceLedger(data_root).records("alpha") == ()
    assert EvidenceLedger(data_root).records("beta") == ()
    provider.shutdown()


def test_unexpected_update_failure_logs_redacted_type_and_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, hermes_home, _data_root = configured_provider(tmp_path)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")
    private_turn = "private current-turn evidence"
    private_body = "# Alpha\n\nprivate replacement body\n"
    provider.on_turn_start(1, private_turn, session_id="session-a")

    def fail_append(*args, **kwargs):
        raise RuntimeError(f"storage failed for {private_turn} and {private_body}")

    monkeypatch.setattr(provider._ledger, "append_turn", fail_append)
    with caplog.at_level(logging.WARNING, logger="plugin.hermes_topic_memory"):
        result = json.loads(
            provider.handle_tool_call(
                "topic_memory_update",
                {
                    "topic": "alpha",
                    "body": private_body,
                    "reason": "private reason",
                },
                session_id="session-a",
            )
        )

    assert result == {"error": "operation_failed"}
    log_text = caplog.text
    assert "RuntimeError" in log_text
    assert "storage failed for" in log_text
    assert private_turn not in log_text
    assert private_body not in log_text
    provider.shutdown()


def test_topic_switch_updates_and_syncs_only_newly_explicit_topic(tmp_path: Path) -> None:
    updater = RecordingUpdater()
    provider, hermes_home, data_root = configured_provider(tmp_path, updater=updater)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")
    switched = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "Switch the active archive.", "topic": "beta"},
            session_id="session-a",
        )
    )
    assert switched["topic_id"] == "beta"
    raw_user_turn = "继续加入这个新事实"
    provider.on_turn_start(2, raw_user_turn, session_id="session-a")

    update = json.loads(
        provider.handle_tool_call(
            "topic_memory_update",
            {
                "body": "# Beta\n\nBeta canonical fact.\n\nNew explicit fact.\n",
                "reason": "The current user authorized the new fact.",
            },
            session_id="session-a",
        )
    )
    provider.sync_turn(raw_user_turn, "Applied.", session_id="session-a")
    provider.on_pre_compress([])

    assert update["topic_id"] == "beta"
    assert update["evidence_source"] == "current_turn"
    alpha = FileTopicStore(data_root).get_topic("alpha")
    beta = FileTopicStore(data_root).get_topic("beta")
    assert (alpha.metadata.version, beta.metadata.version) == (1, 2)
    assert alpha.body == "# Alpha\n\nAlpha canonical fact.\n"
    assert beta.body.endswith("New explicit fact.\n")
    assert EvidenceLedger(data_root).records("alpha") == ()
    assert [record.user_text for record in EvidenceLedger(data_root).records("beta")] == [
        raw_user_turn,
        raw_user_turn,
    ]
    assert updater.calls[-1][0] == "beta"
    provider.shutdown()


def test_successful_query_and_explicit_recall_bind_follow_up_sync_to_latest_topic(
    tmp_path: Path,
) -> None:
    updater = RecordingUpdater()
    provider, hermes_home, data_root = configured_provider(tmp_path, updater=updater)
    provider.initialize("session-a", hermes_home=str(hermes_home), chat_id="chat-alpha")

    query_switch = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "What is archived for Beta?"},
            session_id="session-a",
        )
    )
    assert query_switch["topic_id"] == "beta"
    provider.sync_turn("继续记录这个事实", "ack", session_id="session-a")
    provider.on_pre_compress([])

    explicit_switch = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "current status", "topic": "alpha"},
            session_id="session-a",
        )
    )
    assert explicit_switch["topic_id"] == "alpha"
    provider.sync_turn("再记录一个事实", "ack", session_id="session-a")
    provider.on_pre_compress([])

    assert [record.user_text for record in EvidenceLedger(data_root).records("beta")] == [
        "继续记录这个事实"
    ]
    assert [record.user_text for record in EvidenceLedger(data_root).records("alpha")] == [
        "再记录一个事实"
    ]
    assert [topic_id for topic_id, _evidence_ids in updater.calls] == ["beta", "alpha"]
    provider.shutdown()


def test_meta_follow_up_recall_keeps_the_supplied_archive_question(
    tmp_path: Path,
) -> None:
    reader = TopicReader()
    provider, hermes_home, data_root = configured_mafu_provider(tmp_path, reader=reader)
    provider.initialize("session-a", hermes_home=str(hermes_home))
    archive_question = "你跟我讲讲马夫是用来做什么的？"

    provider.on_turn_start(1, archive_question, session_id="session-a")
    initial = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": archive_question, "topic": "dm-mafu"},
            session_id="session-a",
        )
    )
    provider.on_turn_start(2, "你为什么不调用记忆插件啊？", session_id="session-a")
    recalled = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": archive_question, "topic": "dm-mafu"},
            session_id="session-a",
        )
    )

    assert initial["source"] == "reader"
    assert recalled["source"] == "cache"
    assert [request.question for request in reader.requests] == [archive_question]
    assert [record.question for record in QuestionCache(data_root).records("dm-mafu")] == [
        archive_question
    ]
    provider.shutdown()


def test_same_turn_named_topic_recall_routes_and_remembers_session_topic(
    tmp_path: Path,
) -> None:
    reader = TopicReader()
    provider, hermes_home, _data_root = configured_mafu_provider(
        tmp_path, reader=reader
    )
    provider.initialize("session-a", hermes_home=str(hermes_home))
    archive_question = "你跟我讲讲马夫是用来做什么的？"

    provider.on_turn_start(1, archive_question, session_id="session-a")
    first = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": archive_question},
            session_id="session-a",
        )
    )
    provider.on_turn_start(2, "再说说它目前的职责。", session_id="session-a")
    follow_up = json.loads(
        provider.handle_tool_call(
            "topic_memory_recall",
            {"query": "再说说它目前的职责。"},
            session_id="session-a",
        )
    )

    assert [first["topic_id"], follow_up["topic_id"]] == ["dm-mafu", "dm-mafu"]
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
    assert provider._routed_archive_queries == {}
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
    assert provider._routed_archive_queries == {}

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
