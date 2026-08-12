from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

TopicState = Literal["active", "archived", "trashed"]


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
    state: TopicState = "active"
    source_relative_path: str | None = None
    source_sha256: str | None = None
    source_bytes: int | None = None
    source_chars: int | None = None
    source_lines: int | None = None
    imported_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "topic_id": self.topic_id,
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "version": self.version,
            "content_fingerprint": self.content_fingerprint,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_policy": self.source_policy,
            "state": self.state,
        }
        optional = {
            "source_relative_path": self.source_relative_path,
            "source_sha256": self.source_sha256,
            "source_bytes": self.source_bytes,
            "source_chars": self.source_chars,
            "source_lines": self.source_lines,
            "imported_at": self.imported_at,
        }
        value.update((key, item) for key, item in optional.items() if item is not None)
        return value

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
            state=_topic_state(value.get("state", "active")),
            source_relative_path=_optional_str(value.get("source_relative_path")),
            source_sha256=_optional_str(value.get("source_sha256")),
            source_bytes=_optional_int(value.get("source_bytes")),
            source_chars=_optional_int(value.get("source_chars")),
            source_lines=_optional_int(value.get("source_lines")),
            imported_at=_optional_str(value.get("imported_at")),
        )


@dataclass(frozen=True, slots=True)
class TopicMemory:
    metadata: TopicMetadata
    body: str



def _topic_state(value: Any) -> TopicState:
    if value not in ("active", "archived", "trashed"):
        raise ValueError(f"invalid topic state: {value!r}")
    return value

def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
