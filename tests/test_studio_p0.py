from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from importlib import resources

import pytest


@dataclass
class _Element:
    tag: str
    attrs: dict[str, str | None]
    children: list[_Element] = field(default_factory=list)
    text: str = ""

    def descendants(self) -> Iterator[_Element]:
        for child in self.children:
            yield child
            yield from child.descendants()

    def text_content(self) -> str:
        return self.text + "".join(child.text_content() for child in self.children)


class _StudioDom(HTMLParser):
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.root = _Element("document", {})
        self.stack = [self.root]
        self.nodes: list[_Element] = []
        self.ids: dict[str, _Element] = {}
        self.duplicate_ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Element(tag, dict(attrs))
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if identifier := node.attrs.get("id"):
            if identifier in self.ids:
                self.duplicate_ids.add(identifier)
            self.ids[identifier] = node
        if tag not in self._VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].text += data


@pytest.fixture(scope="module")
def studio_assets() -> dict[str, str]:
    root = resources.files("hermes_topic_memory").joinpath("studio_assets")
    return {
        name: root.joinpath(name).read_text(encoding="utf-8")
        for name in ("index.html", "styles.css", "app.js")
    }


@pytest.fixture(scope="module")
def studio_dom(studio_assets: dict[str, str]) -> _StudioDom:
    parser = _StudioDom()
    parser.feed(studio_assets["index.html"])
    parser.close()
    return parser


def _function(script: str, name: str) -> str:
    match = re.search(rf"(?ms)^  (?:async )?function {re.escape(name)}\b.*?^  \}}", script)
    assert match is not None, f"Missing Studio function: {name}"
    return match.group()


def _click_handler(script: str, identifier: str) -> str:
    match = re.search(
        rf'(?m)^  \$\("{re.escape(identifier)}"\)\.addEventListener\("click",[^\n]*',
        script,
    )
    assert match is not None, f"Missing click handler: {identifier}"
    if not match.group().rstrip().endswith("{"):
        return match.group()
    tail = script[match.end():]
    close = re.search(r"(?m)^  \}\);", tail)
    assert close is not None
    return match.group() + tail[:close.end()]


def _css_block(css: str, marker: str) -> str:
    match = re.search(marker, css)
    assert match is not None, f"Missing CSS contract: {marker}"
    start = css.index("{", match.end())
    depth = 1
    for index in range(start + 1, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1:index]
    raise AssertionError(f"Unclosed CSS contract: {marker}")


def _options(node: _Element) -> dict[str, str]:
    return {
        child.attrs.get("value") or "": child.text_content().strip()
        for child in node.children if child.tag == "option"
    }


def test_studio_controls_have_associated_labels_and_decorative_svg_is_hidden(studio_dom: _StudioDom) -> None:
    assert not studio_dom.duplicate_ids
    labels = {
        node.attrs.get("for"): node
        for node in studio_dom.nodes if node.tag == "label"
    }
    required = {
        "token-input", "topic-search", "remember-input", "ask-input", "version-select",
        "activity-action", "activity-source", "activity-status", "activity-search",
    }
    assert required <= labels.keys()
    for node in studio_dom.nodes:
        if node.tag in {"input", "select"} and node.attrs.get("type") != "hidden":
            label = labels[node.attrs["id"]]
            assert label.text_content().strip(), "A placeholder is not an associated label"
    for identifier in ("activity-action", "activity-source", "activity-status", "activity-search"):
        assert "sr-only" not in (labels[identifier].attrs.get("class") or "").split()
    refresh = studio_dom.ids["btn-refresh"]
    assert refresh.tag == "button"
    assert refresh.attrs.get("aria-label") == "刷新全部数据"
    icons = [node for node in studio_dom.nodes if node.tag == "svg"]
    assert icons
    assert all(node.attrs.get("aria-hidden") == "true" for node in icons)


def test_studio_tabs_have_panel_relationships_and_roving_keyboard_contract(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    tablist = studio_dom.ids["studio-tabs"]
    assert tablist.attrs.get("role") == "tablist"
    assert tablist.attrs.get("aria-label")
    tabs = [node for node in tablist.descendants() if node.attrs.get("role") == "tab"]
    assert len(tabs) == 5
    assert sum(tab.attrs.get("aria-selected") == "true" for tab in tabs) == 1
    for tab in tabs:
        assert tab.tag == "button" and tab.text_content().strip()
        active = tab.attrs.get("aria-selected") == "true"
        assert tab.attrs.get("tabindex") == ("0" if active else "-1")
        panel = studio_dom.ids[tab.attrs["aria-controls"]]
        assert panel.attrs.get("role") == "tabpanel"
        assert panel.attrs.get("aria-labelledby") == tab.attrs["id"]
        assert ("hidden" in panel.attrs) is not active
    script = studio_assets["app.js"]
    switch = _function(script, "switchTab")
    assert 'setAttribute("aria-selected", String(active))' in switch
    assert "tab.tabIndex = active ? 0 : -1" in switch
    assert "panel.hidden =" in switch
    for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
        assert f'e.key === "{key}"' in script
    assert "e.preventDefault()" in script
    assert "{ focus: true }" in script


def test_focus_visible_uses_primary_outline_for_every_control(studio_assets: dict[str, str]) -> None:
    css = studio_assets["styles.css"]
    focus_rules = [
        (selectors, declarations)
        for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if "button:focus-visible" in selectors
    ]
    assert focus_rules
    selectors, declarations = focus_rules[-1]
    for selector in ("button:focus-visible", "a:focus-visible", "input:focus-visible", "select:focus-visible", "summary:focus-visible"):
        assert selector in selectors
    assert re.search(r"outline\s*:\s*2px\s+solid\s+var\(--primary\)", declarations)
    assert re.search(r"outline-offset\s*:\s*2px", declarations)


def test_reduced_motion_disables_animation_transition_and_transforms(studio_assets: dict[str, str]) -> None:
    reduced = _css_block(studio_assets["styles.css"], r"@media\s*\(prefers-reduced-motion:\s*reduce\)")
    assert "*::before" in reduced and "*::after" in reduced
    for property_name in ("animation", "transition", "transform"):
        assert re.search(rf"{property_name}\s*:\s*none\s*!important", reduced)
    assert re.search(r"scroll-behavior\s*:\s*auto", reduced)


def test_narrow_layout_keeps_stats_models_filters_and_diffs_visible(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    for class_name in ("stats-strip", "runtime-pills"):
        containers = [node for node in studio_dom.nodes if class_name in (node.attrs.get("class") or "").split()]
        assert containers
        assert all("hidden" not in (node.attrs.get("class") or "").split() and "hidden" not in node.attrs for node in containers)
    css = studio_assets["styles.css"]
    for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if re.search(r"\.(?:stats-strip|runtime-pills)\b", selectors):
            assert not re.search(r"display\s*:\s*none", declarations)
    mobile = _css_block(css, r"@media\s*\(max-width:\s*960px\)")
    assert "flex-wrap: wrap" in _css_block(mobile, r"\.topbar\s*")
    assert "display: flex" in _css_block(mobile, r"\.stats-strip\s*")
    assert "display: flex" in _css_block(mobile, r"\.runtime-pills\s*")
    assert "flex-wrap: wrap" in _css_block(css, r"\.activity-filters\s*")
    assert "flex-wrap: wrap" in _css_block(css, r"\.activity-entry-head,\s*\.activity-meta\s*")
    diff = _css_block(css, r"\.diff-line\s*")
    assert "white-space: pre-wrap" in diff and "overflow-wrap: anywhere" in diff


def test_overview_reads_nested_kpis_and_preserves_character_map_for_sorting(studio_assets: dict[str, str]) -> None:
    script = studio_assets["app.js"]
    refresh = _function(script, "refreshAll")
    assert "const kpis = ov.kpis || ov;" in refresh
    for identifier, key in (
        ("stat-topics", "active_topic_count"),
        ("stat-cache", "active_cache_count"),
        ("stat-queries", "questions_last_30_days"),
    ):
        assert re.search(rf'\$\("{identifier}"\)\.textContent\s*=\s*String\(kpis\.{key}\s*\?\?', refresh)
    assert "kpis.total_chars ?? fallbackChars" in refresh
    assert "for (const ct of ov.topics || [])" in refresh
    assert "state.charsMap[ct.topic_id] = ct.chars || 0" in refresh
    assert "fallbackChars += ct.chars || 0" in refresh
    assert '$("stat-chars").textContent = totalChars' in refresh
    sidebar = _function(script, "renderTopicList")
    assert 'state.filter === "largest"' in sidebar
    assert "state.charsMap[b.topic_id]" in sidebar and "state.charsMap[a.topic_id]" in sidebar
    assert "chars !== undefined ? fmtChars(chars)" in sidebar


def test_activity_filters_cover_public_actions_sources_and_localized_statuses(studio_dom: _StudioDom) -> None:
    actions = _options(studio_dom.ids["activity-action"])
    assert set(actions) == {
        "", "recall", "remember", "dream", "bind_topic", "list_topics", "whoami",
        "topic_create", "topic_edit", "topic_append", "topic_rollback", "topic_archive",
        "topic_restore", "topic_trash", "topic_delete", "core_init", "core_edit",
        "core_rollback", "auth_failed", "sso_login", "sso_logout",
        "key_create", "key_revoke",
    }
    assert all(label and label != value for value, label in actions.items() if value)
    assert set(_options(studio_dom.ids["activity-source"])) == {"", "mcp", "rest", "panel", "studio", "cli", "system"}
    assert _options(studio_dom.ids["activity-status"]) == {
        "": "全部状态", "ok": "成功", "no_fact": "无匹配事实", "duplicate": "重复",
        "skipped": "已跳过", "error": "失败", "denied": "拒绝访问",
    }
    for identifier, name in (("activity-action", "action"), ("activity-source", "source"), ("activity-status", "status"), ("activity-search", "q")):
        assert studio_dom.ids[identifier].attrs.get("name") == name
    assert studio_dom.ids["activity-list"].tag == "ol"
    assert studio_dom.ids["activity-list"].attrs.get("aria-label")
    assert studio_dom.ids["activity-loading"].attrs.get("role") == "status"
    assert studio_dom.ids["activity-error"].attrs.get("role") == "alert"
    error_children = {node.attrs.get("id") for node in studio_dom.ids["activity-error"].descendants()}
    assert {"activity-error-message", "btn-activity-retry"} <= error_children
    assert studio_dom.ids["btn-activity-retry"].text_content().strip() == "重试"
    assert studio_dom.ids["btn-activity-more"].attrs.get("type") == "button"


def test_activity_loading_has_recovery_pagination_reset_and_latest_request_guards(studio_assets: dict[str, str]) -> None:
    script = studio_assets["app.js"]
    load = _function(script, "refreshLogs")
    assert 'api("/api/activities?"' in load
    assert "/api/questions" not in script
    assert "new URLSearchParams" in load and "page_size:" in load
    assert "reset ? 1 : activity.page + 1" in load
    assert "activity.items.length = 0" in load and "activity.seenIds.clear()" in load
    assert "new AbortController()" in load and "activity.controller.abort()" in load
    assert "signal: controller.signal" in load
    assert "requestId !== state.activities.requestId" in load
    assert "requestId === state.activities.requestId" in load
    assert 'err.name === "AbortError"' in load
    assert not re.search(r"catch\s*\([^)]*\)\s*\{\s*\}", load)
    assert '$("activity-error-message").textContent =' in load
    assert '$("activity-error").classList.remove("hidden")' in load
    assert load.index("appendActivities(res.items)") < load.index('$("activity-error").classList.add("hidden")')
    reset = _click_handler(script, "btn-activity-reset")
    assert '$("activity-filters").reset()' in reset and "applyActivityFilters()" in reset
    assert "state.activities.failedReset" in _click_handler(script, "btn-activity-retry")
    assert "reset: false" in _click_handler(script, "btn-activity-more")
    filters = _function(script, "applyActivityFilters")
    for identifier in ("activity-action", "activity-source", "activity-status", "activity-search"):
        assert f'$("{identifier}").value' in filters
    rows = _function(script, "appendActivities")
    for key in ("created_at", "action", "status", "actor", "source", "topic_id", "latency_ms", "summary"):
        assert f"record.{key}" in rows
    assert "record.meta" not in rows and "record.question" not in rows and "record.fact" not in rows


def test_dream_detail_remains_an_accessible_secondary_view(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    view = studio_dom.ids["dream-reports-view"]
    assert view.tag == "details"
    assert any(node.tag == "summary" and node.text_content().strip() for node in view.children)
    assert studio_dom.ids["dream-reports-error"].attrs.get("role") == "alert"
    script = studio_assets["app.js"]
    assert 'api("/api/dreams?limit=20"' in _function(script, "refreshDreamReports")
    render = _function(script, "renderDreamReports")
    for key in ("before_version", "after_version", "before_chars", "after_chars", "evidence_count", "summary", "changes", "diff"):
        assert f"report.{key}" in render
    assert 'document.createElement("details")' in render
    assert 'document.createElement("summary")' in render
    assert 'diff.setAttribute("role", "region")' in render and "diff.tabIndex = 0" in render
    assert 'renderDiff(diff, report.diff || "")' in render


def test_dream_overwrite_requires_topic_named_confirmation_and_clears_stale_state(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    confirm = studio_dom.ids["dream-apply-confirm"]
    assert confirm.attrs.get("role") == "region"
    assert confirm.attrs.get("aria-labelledby") == "dream-confirm-title"
    assert confirm.attrs.get("aria-describedby") == "dream-confirm-description"
    assert studio_dom.ids["btn-dream-confirm"].text_content().strip() == "确认应用 Dream 换版"
    assert studio_dom.ids["btn-dream-cancel"].text_content().strip() == "取消"
    script = studio_assets["app.js"]
    for identifier in ("btn-dream-apply", "btn-dream-commit-inline"):
        handler = _click_handler(script, identifier)
        assert "requestDreamApply(" in handler
        assert "runDream(" not in handler and "api(" not in handler
    request = _function(script, "requestDreamApply")
    assert '$("dream-confirm-topic").textContent =' in request and "state.selectedId" in request
    assert "state.dreamConfirmTopicId = state.selectedId" in request
    assert "runDream(" not in request and "api(" not in request
    apply = _click_handler(script, "btn-dream-confirm")
    assert "state.dreamConfirmTopicId" in apply and "topicId !== state.selectedId" in apply
    assert "runDream(false, topicId)" in apply
    clear = _function(script, "clearDreamConfirmation")
    assert "state.dreamConfirmTopicId = null" in clear
    assert '$("dream-apply-confirm").classList.add("hidden")' in clear
    assert "clearDreamConfirmation()" in _function(script, "selectTopic")
    assert "clearDreamConfirmation({ restoreFocus: true })" in _click_handler(script, "btn-dream-cancel")


def test_toast_live_region_keeps_close_control_and_errors_do_not_auto_dismiss(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    toast_node = studio_dom.ids["toast"]
    assert toast_node.attrs.get("role") == "status"
    assert toast_node.attrs.get("aria-live") == "polite"
    assert toast_node.attrs.get("aria-atomic") == "true"
    assert {"toast-message", "toast-close"} <= {node.attrs.get("id") for node in toast_node.descendants()}
    close = studio_dom.ids["toast-close"]
    assert close.tag == "button" and close.attrs.get("aria-label") == "关闭通知"
    script = studio_assets["app.js"]
    notification = _function(script, "toast")
    assert '$("toast-message").textContent = msg' in notification
    assert "el.textContent" not in notification and "clearNode(" not in notification
    assert re.search(r"if\s*\(!error\)\s+toastTimer\s*=\s*setTimeout", notification)
    assert "toastHasError && !error" in notification
    assert "dismissToast" in _click_handler(script, "toast-close")
    assert "clearNode(" not in _function(script, "dismissToast")


def test_studio_uses_secret_safe_dom_external_code_and_source_header(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    scripts = [node for node in studio_dom.nodes if node.tag == "script"]
    assert scripts and all(node.attrs.get("src") and not node.text_content().strip() for node in scripts)
    assert any(node.attrs.get("src") == "/studio/app.js" for node in scripts)
    assert any(node.tag == "link" and node.attrs.get("href") == "/studio/styles.css" for node in studio_dom.nodes)
    assert not any(node.tag == "style" for node in studio_dom.nodes)
    for node in studio_dom.nodes:
        assert "style" not in node.attrs
        assert not any(name.lower().startswith("on") for name in node.attrs)
    script = studio_assets["app.js"]
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function("):
        assert unsafe not in script
    assert not re.search(r"\.style\s*(?:\.|\[|=)", script)
    assert not re.search(r'\.setAttribute\(\s*[\"\'](?:style|on\w+)[\"\']', script)
    assert not re.search(r"textContent\s*=\s*state\.token", script)
    assert "localStorage" not in script
    api = _function(script, "api")
    assert "headers: headers(options.headers || {})" in api
    assert "data.error" not in api and "error.message" not in api
    headers = _function(script, "headers")
    assert '"X-Topic-Memory-Source": "studio"' in headers
    assert 'h["Authorization"] = "Bearer " + state.token' in headers
    endpoint = _function(script, "endpointLabel")
    for field_name in ("username", "password", "search", "hash"):
        assert f'url.{field_name} = ""' in endpoint
    assert '$("dream-progress").value =' in _function(script, "runDream")
