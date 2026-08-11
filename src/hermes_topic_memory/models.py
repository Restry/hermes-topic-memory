from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TopicMetadata:
    topic_id: str
    display_name: str
    aliases: tuple[str, ...]
    version: int
    content_fingerprint: str
    created_at: str
    updated_at: str
    source_policy: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "version": self.version,
            "content_fingerprint": self.content_fingerprint,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_policy": self.source_policy,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TopicMetadata:
        return cls(
            topic_id=str(value["topic_id"]),
            display_name=str(value["display_name"]),
            aliases=tuple(str(alias) for alias in value["aliases"]),
            version=int(value["version"]),
            content_fingerprint=str(value["content_fingerprint"]),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
            source_policy=str(value["source_policy"]),
        )


@dataclass(frozen=True, slots=True)
class TopicMemory:
    metadata: TopicMetadata
    body: str
