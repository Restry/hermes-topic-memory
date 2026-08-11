from __future__ import annotations

from pathlib import Path

from .router import TopicRegistry
from .store import FileTopicStore


def validate_data_root(data_root: str | Path) -> int:
    """Validate registry entries against canonical topic directories and bodies."""
    root = Path(data_root)
    registry = TopicRegistry.from_file(root / "topic-registry.json")
    store = FileTopicStore(root)
    registered = {topic.topic_id for topic in registry.topics}
    for topic_id in registered:
        store.get_topic(topic_id)
    topic_dirs = (
        {
            path.name
            for path in store.topics_dir.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        }
        if store.topics_dir.is_dir()
        else set()
    )
    if topic_dirs != registered:
        raise ValueError("registry and canonical topic directories differ")
    return len(registered)
