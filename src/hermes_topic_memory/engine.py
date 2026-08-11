from __future__ import annotations

from dataclasses import dataclass

from .cache import QuestionCache
from .reader import Reader, ReaderRequest, ReaderUsage, build_stable_prefix
from .store import FileTopicStore
from .temporal import build_temporal_evidence_capsule


@dataclass(frozen=True, slots=True)
class RecallFailure:
    code: str
    message: str
    attempts: int


@dataclass(frozen=True, slots=True)
class RecallResult:
    topic_id: str
    topic_version: int
    answer: str | None
    source: str
    similarity: float | None = None
    usages: tuple[ReaderUsage, ...] = ()
    failure: RecallFailure | None = None


class RecallEngine:
    def __init__(
        self,
        *,
        store: FileTopicStore,
        cache: QuestionCache,
        reader: Reader,
        prompt_version: str = "v1",
    ) -> None:
        self.store = store
        self.cache = cache
        self.reader = reader
        self.prompt_version = prompt_version

    def recall(self, topic_id: str, question: str) -> RecallResult:
        if not question.strip():
            raise ValueError("question must not be empty")
        memory = self.store.get_topic(topic_id)
        evidence_capsule = build_temporal_evidence_capsule(memory.body, question)
        topic_version = memory.metadata.version
        cached = self.cache.lookup(
            question,
            topic_id=topic_id,
            topic_version=topic_version,
            exact_only=evidence_capsule is not None,
        )
        if cached is not None:
            return RecallResult(
                topic_id=topic_id,
                topic_version=topic_version,
                answer=cached.answer,
                source="cache",
                similarity=cached.similarity,
            )

        prefix = build_stable_prefix(memory)
        usages: list[ReaderUsage] = []
        first_request = ReaderRequest(
            prefix=prefix, question=question, evidence_capsule=evidence_capsule
        )
        response = self.reader.read(first_request)
        if response.usage is not None:
            usages.append(response.usage)
        answer = response.answer.strip()

        failure_code = _invalid_answer_code(answer)
        attempts = 1
        if failure_code is not None:
            correction = ReaderRequest(
                prefix=prefix,
                question=question,
                correction_of=response.answer,
                evidence_capsule=evidence_capsule,
            )
            response = self.reader.read(correction)
            attempts = 2
            if response.usage is not None:
                usages.append(response.usage)
            answer = response.answer.strip()
            failure_code = _invalid_answer_code(answer)

        if failure_code is not None:
            message = (
                "Reader answer exceeded 200 characters after one correction"
                if failure_code == "answer_too_long"
                else "Reader returned an empty answer after one correction"
            )
            return RecallResult(
                topic_id=topic_id,
                topic_version=topic_version,
                answer=None,
                source="failure",
                usages=tuple(usages),
                failure=RecallFailure(failure_code, message, attempts),
            )

        self.cache.put(
            question=question,
            answer=answer,
            topic_id=topic_id,
            topic_version=topic_version,
            model=response.model or "unknown",
            prompt_version=self.prompt_version,
        )
        return RecallResult(
            topic_id=topic_id,
            topic_version=topic_version,
            answer=answer,
            source="reader",
            usages=tuple(usages),
        )


def _invalid_answer_code(answer: str) -> str | None:
    if not answer:
        return "answer_empty"
    if len(answer) > 200:
        return "answer_too_long"
    return None
