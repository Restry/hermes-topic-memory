from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from hermes_topic_memory import cli
from hermes_topic_memory.cache import QuestionCache
from hermes_topic_memory.dream import DreamConsolidator
from hermes_topic_memory.engine import RecallEngine
from hermes_topic_memory.panel_app import PanelApplication
from hermes_topic_memory.panel_server import TopicMemoryPanelServer
from hermes_topic_memory.reader import ReaderRequest, ReaderResponse
from hermes_topic_memory.remember import RememberService
from hermes_topic_memory.router import TopicRegistry
from hermes_topic_memory.store import FileTopicStore
from hermes_topic_memory.topic_admin import TopicAdminService


class _SmartStubModel:
    """Deterministic test model supporting Dream JSON, TopicPlanner JSON, and Reader capsules."""

    def __init__(self, *, on_dream_hook=None) -> None:
        self.calls: list[ReaderRequest] = []
        self._on_dream_hook = on_dream_hook

    def read(self, request: ReaderRequest) -> ReaderResponse:
        self.calls.append(request)
        if "DREAM MEMORY CONSOLIDATION RULES:" in request.prefix:
            if self._on_dream_hook is not None:
                self._on_dream_hook()
            payload = {
                "action": "consolidate",
                "summary": "Azure 代理与内网服务部署规范",
                "new_body": (
                    "# Azure Infra\n"
                    "- [2026-11-25] 生产代理已切换至 Hysteria2（旧配置已废弃）。\n"
                    "- 仅保留经过验证的长期运维决策。\n"
                ),
                "changes": [
                    "合并重复代理条目",
                    "删除过时旧协议记录",
                    "剔除一次性构建播报",
                ],
                "pruned_count": 1,
                "merged_count": 1,
                "conflicts_resolved": 1,
            }
            return ReaderResponse(
                answer=json.dumps(payload, ensure_ascii=False),
                model="stub-dream",
            )
        if "TOPIC PLANNING RULES:" in request.prefix:
            if "和" in request.question:
                return ReaderResponse(
                    answer=json.dumps(
                        {
                            "topics": [
                                {"topic": "infra", "reason": "代理与部署"},
                                {"topic": "veloiq", "reason": "业务系统"},
                            ]
                        },
                        ensure_ascii=False,
                    ),
                    model="stub-planner",
                )
            if "代理" in request.question:
                # Verify catalog summary was surfaced to the planner!
                assert "Azure 代理" in request.prefix or "infra" in request.prefix
                return ReaderResponse(
                    answer=json.dumps(
                        {"topics": [{"topic": "infra", "reason": "匹配代理摘要"}]},
                        ensure_ascii=False,
                    ),
                    model="stub-planner",
                )
            return ReaderResponse(answer="not-json", model="stub-planner")
        if "LABELED TOPIC CAPSULES:" in request.prefix:
            return ReaderResponse(
                answer="[Azure Infra/infra] 使用 Hysteria2；[VeloIQ/veloiq] 跑在 SQL Server。",
                model="stub-synth",
            )
        if "Hysteria2" in request.prefix:
            return ReaderResponse(
                answer="生产代理已切换至 Hysteria2。",
                model="stub-reader",
            )
        if "SQL Server" in request.prefix:
            return ReaderResponse(
                answer="VeloIQ 本地开发固定使用 SQL Server。",
                model="stub-reader",
            )
        return ReaderResponse(
            answer="no stored fact answers the question",
            model="stub-reader",
        )


def _seed_workspace(root: Path) -> None:
    admin = TopicAdminService(root)
    admin.create_topic(
        "infra",
        "Azure Infra",
        ("infra",),
        (
            "# Azure Infra\n"
            "- 旧代理使用 Shadowsocks。\n"
            "- 昨天构建成功了。\n"
        ),
    )
    admin.create_topic(
        "veloiq",
        "VeloIQ",
        ("veloiq",),
        "# VeloIQ\n- 本地开发固定使用 SQL Server。\n",
    )
    RememberService(root).remember(
        "infra",
        "生产代理已切换至 Hysteria2。",
        source_agent="omp",
        idempotency_key="ev-1",
    )


def test_dream_dry_run_then_commit_updates_body_cache_and_summary(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    model = _SmartStubModel()
    store = FileTopicStore(tmp_path)

    # Warm QA cache on version 2 before dreaming.
    cache = QuestionCache(tmp_path)
    before_recall = RecallEngine(store=store, cache=cache, reader=model).recall(
        "infra", "代理用什么协议？"
    )
    assert before_recall.topic_version == 2
    assert before_recall.source == "reader"

    consolidator = DreamConsolidator(tmp_path, model)

    # 1. Dry-run previews diff without modifying canonical memory or registry.
    preview = consolidator.dream_topic("infra", dry_run=True)
    assert preview.status == "dry_run"
    assert preview.before_version == 2
    assert preview.after_version == 2
    assert preview.pruned_count == 1
    assert preview.conflicts_resolved == 1
    assert "Hysteria2" in preview.diff
    assert store.get_topic("infra").metadata.version == 2

    # 2. Real commit atomically bumps version, archives v2, and refreshes catalog summary.
    committed = consolidator.dream_topic("infra", dry_run=False)
    assert committed.status == "consolidated"
    assert committed.before_version == 2
    assert committed.after_version == 3
    assert committed.summary == "Azure 代理与内网服务部署规范"
    assert store.get_topic("infra").metadata.version == 3
    assert "Shadowsocks" not in store.get_topic("infra").body

    reg = TopicRegistry.from_file(tmp_path / "topic-registry.json")
    infra_cfg = next(t for t in reg.topics if t.topic_id == "infra")
    assert infra_cfg.summary == "Azure 代理与内网服务部署规范"

    # Old v2 QA cache must not be served for v3.
    after_recall = RecallEngine(store=store, cache=cache, reader=model).recall(
        "infra", "代理用什么协议？"
    )
    assert after_recall.topic_version == 3
    assert after_recall.source == "reader"

    # 3. Second dream without new evidence is skipped with 0 LLM calls.
    calls_before = len(model.calls)
    skipped = consolidator.dream_topic("infra")
    assert skipped.status == "skipped_no_new_evidence"
    assert len(model.calls) == calls_before

    # 4. Forced dream runs even without new evidence (no_change if body identical).
    forced = consolidator.dream_topic("infra", force=True)
    assert forced.status == "no_change"
    assert len(model.calls) == calls_before + 1


def test_dream_aborts_safely_on_concurrent_remember(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)

    def _inject_concurrent_write() -> None:
        RememberService(tmp_path).remember(
            "infra",
            "紧急新事实：端口改为 8443。",
            source_agent="hermes",
            idempotency_key="concurrent-1",
        )

    model = _SmartStubModel(on_dream_hook=_inject_concurrent_write)
    report = DreamConsolidator(tmp_path, model).dream_topic("infra")
    assert report.status == "aborted_concurrent_write"
    assert report.before_version == 2
    assert report.after_version == 3
    # Newly remembered fact must remain intact!
    assert "端口改为 8443" in FileTopicStore(tmp_path).get_topic("infra").body


def test_context_free_recall_auto_routes_via_catalog_and_dream_http_cli(
    tmp_path: Path, capsys: Any
) -> None:
    _seed_workspace(tmp_path)
    model = _SmartStubModel()

    # Run dream via CLI first so catalog summary is populated.
    rc = cli.main(
        ["--data-root", str(tmp_path), "dream", "--topic", "infra"],
        reader=model,
    )
    assert rc == 0
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out["status"] == "consolidated"

    app = PanelApplication(tmp_path, model, auth_token="tok-dream")
    srv = TopicMemoryPanelServer(("127.0.0.1", 0), app)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"

        def req(method: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            r = urllib.request.Request(
                base + path,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer tok-dream",
                },
                method=method,
            )
            try:
                with urllib.request.urlopen(r, timeout=5) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read().decode("utf-8"))

        # 1. Context-free single-topic recall (no topic / chat_id / repo / workspace_path!)
        st, r1 = req("POST", "/api/v1/recall", {"query": "上次配的代理是什么协议？"})
        assert st == 200, r1
        assert r1["topic_id"] == "infra"
        assert r1["route_source"] == "catalog_planner"
        assert "Hysteria2" in r1["capsule"]

        # 2. Context-free multi-topic recall via MCP tools/call
        st, mcp_r = req(
            "POST",
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "recall",
                    "arguments": {"query": "代理和数据库分别用什么？"},
                },
            },
        )
        assert st == 200
        payload = mcp_r["result"]["structuredContent"]
        assert payload["route_source"] == "catalog_planner"
        assert payload["status"] == "answered"
        assert len(payload["selected_topics"]) == 2
        assert "Hysteria2" in payload["capsule"]

        # 3. Trigger dream via REST /api/v1/dream
        st, d_res = req("POST", "/api/v1/dream", {"dry_run": True})
        assert st == 200, d_res
        assert d_res["total_topics"] == 2
        assert d_res["skipped"] == 1  # infra already dreamed cleanly!
    finally:
        srv.shutdown()
        srv.server_close()
