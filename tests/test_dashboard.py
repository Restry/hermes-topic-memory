from __future__ import annotations

import json
from pathlib import Path

from hermes_topic_memory.cache import QuestionCache
from hermes_topic_memory.dashboard import generate_dashboard
from hermes_topic_memory.store import FileTopicStore


def test_dashboard_uses_real_store_facts_escapes_hostile_values_and_omits_full_bodies(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    store = FileTopicStore(data_root)
    long_body = "<b>escaped preview</b>\n" + ("a" * 300) + "PRIVATE-END-MARKER"
    alpha = store.create_topic(
        "alpha",
        long_body,
        display_name='<script>alert("title")</script>',
        source_relative_path='folder/<script "x">.md',
        source_sha256="a" * 64,
        source_bytes=len(long_body.encode("utf-8")),
        source_chars=len(long_body),
        source_lines=2,
        imported_at="2026-08-11T00:00:00Z",
    )
    store.create_topic(
        "beta",
        "short private body",
        display_name="Beta",
        source_relative_path="beta.md",
        source_sha256="b" * 64,
        source_bytes=18,
        source_chars=18,
        source_lines=1,
        imported_at="2026-08-11T00:00:00Z",
    )
    store.update_topic("alpha", long_body + "\nupdated")
    (data_root / "topic-registry.json").write_text(
        json.dumps(
            {
                "topics": [
                    {
                        "topic_id": "alpha",
                        "display_name": alpha.metadata.display_name,
                        "aliases": [],
                        "chat_ids": [],
                        "source_relative_path": alpha.metadata.source_relative_path,
                    },
                    {
                        "topic_id": "beta",
                        "display_name": "Beta",
                        "aliases": [],
                        "chat_ids": [],
                        "source_relative_path": "beta.md",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    QuestionCache(data_root).put(
        question="status",
        answer="cached",
        topic_id="alpha",
        topic_version=2,
        model="test",
        prompt_version="v1",
    )
    output = tmp_path / "dashboard.html"

    result = generate_dashboard(data_root, output)
    html = output.read_text(encoding="utf-8")

    actual_alpha = long_body + "\nupdated"
    assert result.topic_count == 2
    assert result.total_chars == len(actual_alpha) + len("short private body")
    assert result.total_bytes == len(actual_alpha.encode("utf-8")) + len(
        "short private body".encode("utf-8")
    )
    assert result.version_count == 3
    assert result.cache_entry_count == 1
    assert result.output_bytes == len(output.read_bytes())
    assert '&lt;script&gt;alert(&quot;' in html
    assert '<script>alert("title")</script>' not in html
    assert 'folder/&lt;script &quot;' in html
    assert "&lt;b&gt;escaped preview&lt;/b&gt;" in html
    assert "PRIVATE-END-MARKER" not in html
    assert "short private body" not in html
    assert 'id="topic-filter"' in html
    assert 'id="topic-sort"' in html
    assert "Largest first" in html and "Smallest first" in html and "Title A–Z" in html
    assert "<details" in html and "Size distribution" in html
    assert "metadata-only" in html
    assert "linear-gradient" not in html
