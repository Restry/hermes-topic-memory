from __future__ import annotations

import threading
from collections.abc import Iterator
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from urllib.request import urlopen

import pytest

from hermes_topic_memory.panel_app import PanelApplication
from hermes_topic_memory.panel_server import create_panel_server
from hermes_topic_memory.topic_admin import TopicAdminService

_TOKEN = "asset-test-token-DO-NOT-RENDER"
_PRIVATE_BODY = "ASSET_TEST_PRIVATE_CANONICAL_BODY"
_ASSET_NAMES = ("index.html", "styles.css", "app.js")


class _ContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.label_targets: set[str] = set()
        self.fields: set[str] = set()
        self.nav_labels: list[str] = []
        self.captions: list[str] = []
        self._capture: str | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        if identifier := values.get("id"):
            self.ids.add(identifier)
        if tag == "label" and (target := values.get("for")):
            self.label_targets.add(target)
        if field := values.get("data-field"):
            self.fields.add(field)
        if tag == "a" and values.get("data-view"):
            self._capture = "nav"
        elif tag == "caption":
            self._capture = "caption"

    def handle_endtag(self, tag: str) -> None:
        if tag in {"a", "caption"}:
            self._capture = None

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._capture == "nav":
            self.nav_labels.append(text)
        elif self._capture == "caption":
            self.captions.append(text)


def _asset_text(name: str) -> str:
    return (
        resources.files("hermes_topic_memory")
        .joinpath("panel_assets", name)
        .read_text(encoding="utf-8")
    )


@pytest.fixture
def running_panel(tmp_path: Path) -> Iterator[str]:
    data_root = tmp_path / "data"
    TopicAdminService(data_root).create_topic(
        "alpha", "Alpha Topic", ("first",), _PRIVATE_BODY
    )
    application = PanelApplication(data_root, object(), _TOKEN)
    server = create_panel_server(application, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_panel_assets_are_package_resources_without_external_runtime_assets() -> None:
    package_root = resources.files("hermes_topic_memory").joinpath("panel_assets")
    for name in _ASSET_NAMES:
        assert package_root.joinpath(name).is_file()

    html = _asset_text("index.html")
    combined = "\n".join(_asset_text(name) for name in _ASSET_NAMES).lower()
    assert 'href="/assets/styles.css"' in html
    assert 'src="/assets/app.js"' in html
    assert 'src="http' not in combined
    assert 'href="http' not in combined
    assert "url(http" not in combined
    assert 'fetch("http' not in combined
    assert "//cdn" not in combined


def test_home_dom_has_shell_dashboard_catalog_and_accessible_controls() -> None:
    html = _asset_text("index.html")
    parser = _ContractParser()
    parser.feed(html)

    assert {
        "session-gate",
        "session-token",
        "panel-shell",
        "primary-navigation",
        "home-view",
        "kpi-strip",
        "activity-chart",
        "activity-table",
        "topic-distribution",
        "recent-activity",
        "topic-catalog",
        "topic-search",
        "topic-status-filter",
        "topic-sort",
        "loading-state",
        "empty-state",
        "error-state",
        "retry-overview",
    } <= parser.ids
    assert ["首页", "多主题召回", "最近提问", "回收站"] == parser.nav_labels
    assert {
        "session-token",
        "topic-search",
        "topic-status-filter",
        "topic-sort",
    } <= parser.label_targets
    assert {
        "display_name",
        "question_count",
        "active_cache_count",
        "version",
        "updated_at",
        "status",
    } <= parser.fields
    assert parser.captions
    assert _PRIVATE_BODY not in html
    assert _TOKEN not in html
    assert "/Users/" not in html


def test_styles_define_approved_tokens_states_motion_and_mobile_layout() -> None:
    css = _asset_text("styles.css")

    for token in (
        "--bg: #0d0d0d",
        "--panel: #131313",
        "--panel2: #1a1a1a",
        "--ink: #e8e8e8",
        "--sub: #8a8a8a",
        "--accent: #e55a3f",
        "JetBrains Mono",
    ):
        assert token in css
    assert ":hover" in css
    assert ":focus-visible" in css
    assert ":disabled" in css
    assert "prefers-reduced-motion: reduce" in css
    assert "@media (max-width: 640px)" in css


def test_app_uses_one_authenticated_overview_snapshot_and_safe_dom_updates() -> None:
    script = _asset_text("app.js")

    assert script.count("fetch(path, request)") == 1
    assert "Authorization" in script
    assert "sessionStorage" in script
    assert "localStorage" not in script
    assert "innerHTML" not in script
    assert "textContent" in script
    assert "try" in script
    assert "catch" in script
    assert "finally" in script
    assert "console." not in script


def test_task10_dom_exposes_integrated_detail_recall_and_history_views() -> None:
    html = _asset_text("index.html")
    parser = _ContractParser()
    parser.feed(html)

    assert {
        "topic-detail-view",
        "close-topic-detail",
        "topic-detail-tabs",
        "topic-overview-panel",
        "topic-questions-panel",
        "topic-ask-panel",
        "topic-body-panel",
        "topic-versions-panel",
        "topic-settings-panel",
        "load-topic-body",
        "topic-body-content",
        "topic-version-list",
        "topic-version-body",
        "topic-version-compare",
        "recall-view",
        "recall-form",
        "recall-question",
        "recall-examples",
        "manual-override",
        "manual-topic-select",
        "recall-plan",
        "recall-topic-results",
        "recall-final",
        "recall-query-id",
        "recall-previous-query-id",
        "recent-view",
        "recent-filters",
        "recent-topic-filter",
        "recent-scope-filter",
        "recent-cache-filter",
        "recent-status-filter",
        "recent-source-filter",
        "recent-date-from",
        "recent-date-to",
        "recent-question-list",
        "recent-pagination",
    } <= parser.ids
    assert {
        "recall-question",
        "manual-override",
        "manual-topic-select",
        "recent-topic-filter",
        "recent-scope-filter",
        "recent-cache-filter",
        "recent-status-filter",
        "recent-source-filter",
        "recent-date-from",
        "recent-date-to",
    } <= parser.label_targets
    assert "仅测试" in html
    assert "Agent" in html


def test_task10_script_uses_real_api_paths_lazy_body_and_recoverable_actions() -> None:
    script = _asset_text("app.js")

    assert script.count("const state = {") == 1
    assert script.count("async function api(") == 1
    for contract in (
        "/api/topics/",
        "/questions?",
        "/body",
        "/versions?",
        "/api/questions?",
        "/api/recall/run",
        "/api/recall/rerun/",
        "loadTopicBody",
        "loadVersionBody",
        "submitRecall",
        "rerunRecall",
        "loadRecentQuestions",
        'event.key === "Escape"',
        "returnFocus",
    ):
        assert contract in script
    assert "Promise.all" not in script
    assert "innerHTML" not in script
    assert "localStorage" not in script
    assert "console." not in script


def test_task10_visual_corrections_remove_numbered_scaffolding_and_side_stripes() -> None:
    html = _asset_text("index.html")
    css = _asset_text("styles.css")

    for marker in (
        "SESSION / 01",
        "01 / SIGNALS",
        "02 / ACTIVITY",
        "03 / DISTRIBUTION",
        "04 / RECENT",
        "05 / CATALOG",
    ):
        assert marker not in html
    assert "border-left: 3px" not in css
    assert ".kpi::before" in css
    assert "width: 2px" in css
    assert ".state-panel--error" in css


@pytest.mark.parametrize(
    ("path", "content_type"),
    (
        ("/", "text/html"),
        ("/assets/styles.css", "text/css"),
        ("/assets/app.js", "text/javascript"),
    ),
)
def test_server_serves_assets_with_safe_headers_and_correct_mime(
    running_panel: str, path: str, content_type: str
) -> None:
    with urlopen(f"{running_panel}{path}", timeout=2) as response:
        body = response.read()
        headers = response.headers

    assert response.status == 200
    assert headers.get_content_type() == content_type
    assert headers.get_content_charset() == "utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Content-Security-Policy"] == (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'"
    )
    assert _TOKEN.encode() not in body
    assert _PRIVATE_BODY.encode() not in body
