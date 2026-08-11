from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from hermes_topic_memory.cache import QuestionCache
from hermes_topic_memory.dashboard import generate_dashboard
from hermes_topic_memory.query_history import (
    QueryHistoryStore,
    QueryRecord,
    TopicQueryResult,
)
from hermes_topic_memory.store import FileTopicStore


def test_dashboard_is_chinese_metadata_only_readable_and_escapes_hostile_values(
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
    QuestionCache(data_root).put(
        question="stale status",
        answer="stale cached",
        topic_id="alpha",
        topic_version=1,
        model="test",
        prompt_version="v1",
    )
    history = QueryHistoryStore(data_root)
    history.append(_query_record("dashboard-1", ("alpha",)))
    history.append(_query_record("dashboard-2", ("alpha", "beta")))
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
    assert result.total_question_count == 2
    assert result.output_bytes == len(output.read_bytes())
    assert '&lt;script&gt;alert(&quot;' in html
    assert '<script>alert("title")</script>' not in html
    assert 'folder/<wbr>&lt;script &quot;' in html
    assert "escaped preview" not in html
    assert "a" * 160 not in html
    assert "PRIVATE-END-MARKER" not in html
    assert "short private body" not in html
    assert "DASHBOARD-PRIVATE-QUESTION" not in html
    assert "DASHBOARD-PRIVATE-ANSWER" not in html
    assert '<html lang="zh-CN">' in html
    assert "主题记忆库" in html
    assert "此页面只包含元数据" in html
    assert "源文档正文保留在私有记忆库中，不会写入此 HTML" in html
    assert 'aria-label="记忆库汇总"' in html
    assert "主题数" in html and "全库版本数" in html and "活跃缓存条目数" in html
    assert "历史提问数" in html
    assert "大小分布" in html and 'aria-label="按主题字符数分组"' in html
    assert 'id="topic-filter"' in html
    assert 'placeholder="标题、主题 ID 或源路径"' in html
    assert 'id="topic-sort"' in html
    assert "标题 A–Z" in html
    assert "字符数从多到少" in html and "字符数从少到多" in html
    assert "显示标题" in html and "源路径" in html and "更新时间" in html
    assert "查看元数据" in html and "源文件 SHA-256" in html
    assert "没有匹配的主题" in html and "2 个主题" in html
    assert "未启用 JavaScript" in html
    assert '<code class="sha" title="完整 SHA-256：' + ("a" * 64) in html
    assert 'aria-label="完整 SHA-256：' + ("a" * 64) in html
    assert ">aaaaaaaaaaaa…</code>" in html
    assert ".sha { display: inline-block; max-width: 100%; white-space: nowrap; }" in html
    assert ".table-wrap { overflow-x: auto;" in html
    assert "table { width: 100%; min-width: 96rem;" in html
    assert "<wbr>" in html
    assert ".preview" not in html
    assert "Topic Memory Library" not in html
    assert "Largest first" not in html and "View metadata" not in html
    assert "linear-gradient" not in html


def _query_record(query_id: str, topic_ids: tuple[str, ...]) -> QueryRecord:
    topic_results = tuple(
        TopicQueryResult(
            topic_id=topic_id,
            topic_version=2 if topic_id == "alpha" else 1,
            status="answered",
            answer=f"{topic_id} capsule",
            source="reader",
            match_kind=None,
            latency_ms=1,
        )
        for topic_id in topic_ids
    )
    return QueryRecord.create(
        query_id=query_id,
        question=f"DASHBOARD-PRIVATE-QUESTION-{query_id}",
        created_at=datetime(2026, 8, 11, tzinfo=UTC),
        selected_topics=topic_results,
        final_answer=f"DASHBOARD-PRIVATE-ANSWER-{query_id}",
        selection_reasons={topic_id: "named" for topic_id in topic_ids},
        source="cli",
        session_ref=None,
        total_latency_ms=2,
    )
