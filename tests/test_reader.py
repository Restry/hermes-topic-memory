from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request

from hermes_topic_memory.models import TopicMemory, TopicMetadata
from hermes_topic_memory.reader import (
    OpenAICompatibleReader,
    ReaderRequest,
    build_stable_prefix,
)


def topic_memory() -> TopicMemory:
    metadata = TopicMetadata(
        topic_id="alpha",
        display_name="Alpha",
        aliases=("a",),
        version=7,
        content_fingerprint="abc123",
        created_at="2026-08-11T00:00:00Z",
        updated_at="2026-08-11T01:00:00Z",
        source_policy="verified-evidence-only",
    )
    return TopicMemory(metadata=metadata, body="# Alpha\n\nExact UTF-8 fact: café.\n")


def test_stable_prefix_is_byte_identical_and_question_is_only_appended() -> None:
    memory = topic_memory()

    first_prefix = build_stable_prefix(memory)
    second_prefix = build_stable_prefix(memory)
    first = ReaderRequest(prefix=first_prefix, question="First question?")
    second = ReaderRequest(prefix=second_prefix, question="Second question?")

    assert first_prefix.encode("utf-8") == second_prefix.encode("utf-8")
    assert memory.body.encode("utf-8") in first_prefix.encode("utf-8")
    assert first.prompt.encode("utf-8").startswith(first_prefix.encode("utf-8"))
    assert second.prompt.encode("utf-8").startswith(first_prefix.encode("utf-8"))
    assert first.prompt.removeprefix(first_prefix) == "QUESTION:\nFirst question?"


def test_openai_compatible_reader_records_optional_usage_without_network() -> None:
    captured: dict[str, object] = {}

    def transport(request: Request, timeout: float) -> bytes:
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data or b"{}")
        return json.dumps(
            {
                "model": "reader-model",
                "choices": [{"message": {"content": "Grounded answer."}}],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 100},
                },
            }
        ).encode()

    reader = OpenAICompatibleReader(
        endpoint="http://reader.invalid/v1",
        model="reader-model",
        api_key="test-key",
        timeout=9,
        transport=transport,
    )
    response = reader.read(ReaderRequest(prefix="stable-prefix\n", question="status?"))

    assert response.answer == "Grounded answer."
    assert response.usage is not None
    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 4
    assert response.usage.cached_input_tokens == 100
    assert captured["url"] == "http://reader.invalid/v1/chat/completions"
    assert captured["authorization"] == "Bearer test-key"
    assert captured["timeout"] == 9
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["messages"][0]["content"] == "stable-prefix\nQUESTION:\nstatus?"


def test_openai_compatible_reader_allows_missing_usage() -> None:
    def transport(_request: Request, _timeout: float) -> bytes:
        return b'{"choices":[{"message":{"content":"answer"}}]}'

    response = OpenAICompatibleReader(
        endpoint="http://reader.invalid/v1",
        model="reader-model",
        transport=transport,
    ).read(ReaderRequest(prefix="prefix", question="question"))

    assert response.answer == "answer"
    assert response.usage is None
