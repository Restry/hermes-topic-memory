from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable
from urllib.request import Request, urlopen

from .models import TopicMemory

STABLE_SYSTEM_INSTRUCTION = (
    "You are a topic-memory Reader. Answer only from the canonical topic memory "
    "below. If the memory does not contain the answer, say that no stored fact "
    "answers the question. Return at most 200 characters."
)


@dataclass(frozen=True, slots=True)
class ReaderUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ReaderRequest:
    prefix: str
    question: str
    correction_of: str | None = None

    @property
    def prompt(self) -> str:
        prompt = f"{self.prefix}QUESTION:\n{self.question}"
        if self.correction_of is not None:
            prompt += (
                "\n\nCORRECTION REQUIRED:\nThe previous answer exceeded the "
                "200-character limit. Return a grounded replacement of at most "
                f"200 characters.\nPREVIOUS ANSWER:\n{self.correction_of}"
            )
        return prompt


@dataclass(frozen=True, slots=True)
class ReaderResponse:
    answer: str
    model: str | None = None
    usage: ReaderUsage | None = None


@runtime_checkable
class Reader(Protocol):
    def read(self, request: ReaderRequest) -> ReaderResponse: ...


class ReaderClientError(RuntimeError):
    """Raised when an OpenAI-compatible response is malformed."""


Transport = Callable[[Request, float], bytes]


class OpenAICompatibleReader:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 60.0,
        transport: Transport | None = None,
    ) -> None:
        if not endpoint or not model:
            raise ValueError("endpoint and model are required")
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._transport = transport or _urlopen_transport

    def read(self, request: ReaderRequest) -> ReaderResponse:
        url = (
            self.endpoint
            if self.endpoint.endswith("/chat/completions")
            else f"{self.endpoint}/chat/completions"
        )
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": request.prompt}],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        raw = self._transport(Request(url, data=payload, headers=headers), self.timeout)
        try:
            value = json.loads(raw)
            choice = value["choices"][0]
            answer = _message_text(choice["message"]["content"])
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ReaderClientError("invalid OpenAI-compatible Reader response") from error
        return ReaderResponse(
            answer=answer,
            model=str(value.get("model", self.model)),
            usage=_parse_usage(value.get("usage")),
        )


def build_stable_prefix(memory: TopicMemory) -> str:
    metadata = json.dumps(
        memory.metadata.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"SYSTEM INSTRUCTION:\n{STABLE_SYSTEM_INSTRUCTION}\n\n"
        f"TOPIC METADATA:\n{metadata}\n\n"
        "CANONICAL MEMORY (verbatim UTF-8):\n"
        f"{memory.body}"
        "\nEND CANONICAL MEMORY\n\n"
    )


def _parse_usage(value: Any) -> ReaderUsage | None:
    if not isinstance(value, dict):
        return None
    details = value.get("prompt_tokens_details")
    cached_tokens = details.get("cached_tokens") if isinstance(details, dict) else None
    return ReaderUsage(
        input_tokens=_optional_int(value.get("prompt_tokens")),
        output_tokens=_optional_int(value.get("completion_tokens")),
        cached_input_tokens=_optional_int(cached_tokens),
    )


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [
            part.get("text")
            for part in value
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        if all(isinstance(part, str) for part in parts):
            return "".join(parts)
    raise ValueError("message content is not text")


def _urlopen_transport(request: Request, timeout: float) -> bytes:
    with urlopen(request, timeout=timeout) as response:
        return response.read()
