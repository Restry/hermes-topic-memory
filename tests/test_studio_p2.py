from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.client import HTTPConnection
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from hermes_topic_memory.panel_app import PanelApplication
from hermes_topic_memory.panel_server import create_panel_server
from hermes_topic_memory.reader import ReaderRequest, ReaderResponse
from hermes_topic_memory.topic_admin import TopicAdminService
from test_studio_p0 import _StudioDom, _css_block, _function, _options


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


def test_core_recycle_is_the_fifth_accessible_tab(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    tabs = [node for node in studio_dom.ids["studio-tabs"].descendants() if node.attrs.get("role") == "tab"]
    assert [node.attrs.get("data-tab") for node in tabs] == ["topic", "ask", "logs", "keys", "admin"]
    tab = tabs[4]
    assert tab.attrs["id"] == "tab-admin"
    assert tab.attrs["type"] == "button"
    assert tab.text_content().strip() == "Core 与回收站"
    assert tab.attrs["aria-controls"] == "panel-admin"
    assert tab.attrs["aria-selected"] == "false" and tab.attrs["tabindex"] == "-1"
    panel = studio_dom.ids["panel-admin"]
    assert panel.attrs["role"] == "tabpanel" and panel.attrs["aria-labelledby"] == "tab-admin"
    assert panel.attrs["tabindex"] == "0" and "hidden" in panel.attrs
    script = studio_assets["app.js"]
    switch = _function(script, "switchTab")
    assert '["topic", "ask", "logs", "keys", "admin"]' in switch
    assert 'name === "admin"' in switch and "refreshAdmin()" in switch
    keyboard = script[script.index("  const tabs ="):]
    for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
        assert f'e.key === "{key}"' in keyboard
    assert "tabs.length" in keyboard and "{ focus: true }" in keyboard


def test_new_management_fields_have_real_labels(studio_dom: _StudioDom) -> None:
    assert not studio_dom.duplicate_ids
    labels = {node.attrs.get("for"): node for node in studio_dom.nodes if node.tag == "label"}
    identifiers = (
        "topic-create-id", "topic-create-name", "topic-create-aliases", "topic-create-body",
        "topic-editor-body", "topic-binding-type", "topic-binding-value", "core-editor-body", "core-version-select",
    )
    for identifier in identifiers:
        control = studio_dom.ids[identifier]
        assert control.tag in {"input", "textarea", "select"}
        assert labels[identifier].text_content().strip(), "Placeholders must not replace labels"
        assert control.attrs.get("name")
    for identifier in ("topic-create-id", "topic-create-name", "topic-create-body"):
        assert "required" in studio_dom.ids[identifier].attrs
    assert "required" not in studio_dom.ids["topic-create-aliases"].attrs
    assert studio_dom.ids["topic-editor-body"].tag == "textarea"
    assert studio_dom.ids["core-editor-body"].tag == "textarea"
    assert studio_dom.ids["core-editor-body"].attrs.get("maxlength") != "4000", "HTML limits UTF-16 units, not Unicode characters"


def test_sidebar_create_and_topic_management_controls_are_inline(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    sidebar = next(node for node in studio_dom.nodes if "sidebar-head" in (node.attrs.get("class") or "").split())
    assert studio_dom.ids["btn-topic-create"] in list(sidebar.descendants())
    assert "+ 新建主题" in studio_dom.ids["btn-topic-create"].text_content()
    for identifier in ("topic-create-form", "topic-editor-form", "topic-bind-form"):
        assert studio_dom.ids[identifier].tag == "form"
    controls = {
        "btn-topic-edit": "编辑正文", "btn-topic-save": "保存修改", "btn-topic-edit-cancel": "取消",
        "btn-topic-rollback": "回滚到此版本", "btn-topic-bindings": "绑定管理",
        "btn-topic-archive": "归档", "btn-topic-restore": "恢复", "btn-topic-trash": "回收站",
    }
    for identifier, text in controls.items():
        button = studio_dom.ids[identifier]
        assert button.tag == "button" and text in button.text_content()
    toolbar = next(node for node in studio_dom.nodes if "reader-toolbar" in (node.attrs.get("class") or "").split())
    assert studio_dom.ids["btn-topic-edit"] in list(toolbar.descendants())
    assert _options(studio_dom.ids["topic-binding-type"]).keys() == {"chat_id", "repo_url", "workspace_path"}
    assert studio_dom.ids["topic-bindings-list"].attrs.get("aria-label")
    for trigger, region in (
        ("btn-topic-create", "topic-create-form"), ("btn-topic-edit", "topic-editor-form"),
        ("btn-topic-bindings", "topic-bindings-panel"), ("btn-topic-rollback", "topic-rollback-confirm"),
        ("btn-topic-trash", "topic-trash-confirm"),
    ):
        assert studio_dom.ids[trigger].attrs.get("aria-controls") == region
        assert studio_dom.ids[trigger].attrs.get("aria-expanded") == "false"
    combined = studio_assets["index.html"] + studio_assets["app.js"]
    assert "确认将主题回滚至 v" in combined and "将生成新版本，历史保留" in combined
    for identifier in (
        "btn-topic-create-submit", "btn-topic-create-cancel", "btn-topic-rollback-confirm", "btn-topic-rollback-cancel",
        "btn-topic-bind", "btn-topic-trash-confirm", "btn-topic-trash-cancel",
    ):
        assert studio_dom.ids[identifier].tag == "button"


def test_core_and_recycle_have_inline_edit_rollback_and_readonly_surfaces(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    for identifier in ("core-state", "core-body", "recycle-list", "recycle-empty", "admin-permission-note"):
        assert identifier in studio_dom.ids
    assert "初始化 Core / WhoAmI" in studio_dom.ids["btn-core-initialize"].text_content()
    assert studio_dom.ids["core-editor-form"].tag == "form"
    for identifier in ("btn-core-edit", "btn-core-save", "btn-core-edit-cancel", "btn-core-rollback-confirm", "btn-core-rollback-cancel"):
        assert studio_dom.ids[identifier].tag == "button"
    assert studio_dom.ids["btn-core-rollback"].attrs.get("aria-controls") == "core-rollback-confirm"
    assert studio_dom.ids["recycle-list"].tag in {"ul", "ol"}
    assert studio_dom.ids["recycle-list"].attrs.get("aria-label")
    assert "管理权限" in studio_dom.ids["admin-permission-note"].text_content()
    script = studio_assets["app.js"]
    for route in ("/api/core-memory", "/api/core-memory/body", "/api/core-memory/versions", "/api/recycle-bin"):
        assert route in script
    assert 'api("/api/core-memory/" + action' in _function(script, "mutateCore")
    assert "mutateCore(editor.mode, payload)" in _function(script, "saveCoreEdit")
    assert 'beginCoreEdit("initialize")' in script and 'beginCoreEdit("replace")' in script
    assert 'mutateCore("rollback",' in _function(script, "confirmCoreRollback")
    assert "trashed_at" in script and "prior_state" in script
    assert "recycle-delete-name-" in script
    assert 'document.createElement("label")' in script
    assert "data-recycle-restore" in script and "data-recycle-delete" in script


def _css_declarations(css: str, selector: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return "\n".join(
        declarations for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if selector in [part.strip() for part in selectors.split(",")]
    )


def test_typography_uses_tabular_counts_and_a_readable_prose_measure(studio_assets: dict[str, str]) -> None:
    css = studio_assets["styles.css"]
    for selector in (".stat-pill strong", ".sidebar-count", ".pill", "time", ".md-date"):
        assert re.search(r"font-variant-numeric\s*:\s*tabular-nums", _css_declarations(css, selector)), selector
    prose = [
        declarations for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if ".pretty-body" in selectors and re.search(r"max-width\s*:\s*72ch", declarations)
    ]
    assert prose
    assert any(re.search(r"overflow-wrap\s*:\s*break-word", declarations) for declarations in prose)


def test_mobile_controls_prevent_ios_input_zoom(studio_assets: dict[str, str]) -> None:
    mobile = _css_block(studio_assets["styles.css"], r"@media\s*\(max-width:\s*640px\)")
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", mobile)
    for control in ("input", "textarea", "select"):
        assert any(control in selectors and re.search(r"font-size\s*:\s*16px", body) for selectors, body in rules), control


def test_transitions_are_explicit_short_and_press_feedback_respects_motion(studio_assets: dict[str, str]) -> None:
    css = studio_assets["styles.css"]
    assert not re.search(r"transition(?:-property)?\s*:\s*all\b", css)
    for declaration in re.findall(r"\btransition\s*:\s*([^;{}]+)", css):
        for amount, unit in re.findall(r"([\d.]+)(ms|s)\b", declaration):
            assert float(amount) * (1 if unit == "ms" else 1000) <= 150
    active = _css_block(css, r"@media\s*\(prefers-reduced-motion:\s*no-preference\)")
    for selector in (".btn:active:not(:disabled)", ".filter-chip:active", ".starter-chip:active"):
        assert selector in active
    assert re.search(r"transform\s*:\s*scale\(0\.96\)", active)
    reduced = _css_block(css, r"@media\s*\(prefers-reduced-motion:\s*reduce\)")
    assert "transform: none !important" in reduced
    assert "textarea:focus-visible" in css


def test_svg_strokes_and_strict_dom_remain_consistent(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    icons = [node for node in studio_dom.nodes if node.tag == "svg"]
    assert icons and all(node.attrs.get("stroke-width") == "1.75" for node in icons)
    script = studio_assets["app.js"]
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "window.confirm", "window.prompt"):
        assert unsafe not in script
    assert "document.createElement" in script and "textContent" in script


def test_studio_p2_javascript_passes_node_syntax_check() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the Studio syntax contract")
    asset = resources.files("hermes_topic_memory").joinpath("studio_assets", "app.js")
    with resources.as_file(asset) as path:
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr


def test_admin_mutation_entries_and_auth_teardown_are_role_guarded(studio_assets: dict[str, str]) -> None:
    script = studio_assets["app.js"]
    for name in ("submitTopicMutation", "saveCoreEdit", "mutateCore", "confirmCoreRollback", "mutateRecycle"):
        assert 'state.role !== "admin"' in _function(script, name), name
    for name in ("syncTopicAdminControls", "syncAdminControls"):
        assert 'state.role === "admin"' in _function(script, name), name
    for name in ("applyAuth", "showGate"):
        body = _function(script, name)
        assert "clearTopicAdminSession()" in body and "clearAdminSession()" in body
    creation = re.search(r'(?ms)^  \$\("topic-create-form"\)\.addEventListener\("submit",.*?^  \}\);', script)
    assert creation is not None and 'state.role !== "admin"' in creation.group()
    assert "expected_version" in _function(script, "submitTopicMutation")
    assert "expected_fingerprint" in _function(script, "submitTopicMutation")
    assert "expected_version" in _function(script, "saveCoreEdit")
    assert "expected_fingerprint" in _function(script, "saveCoreEdit")
    assert "item.trashed_at" in _function(script, "mutateRecycle")


class _NoAdminReader:
    def read(self, request: ReaderRequest) -> ReaderResponse:
        raise AssertionError("Studio administration must not require a model call")


_TOKEN = "studio-p2-test-only-token"


@dataclass
class _StudioHttp:
    app: PanelApplication
    connection: HTTPConnection

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None, *, token: str = _TOKEN) -> tuple[int, Any]:
        headers = {"Authorization": "Bearer " + token, "X-Topic-Memory-Source": "studio"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        self.connection.request(method, path, body=json.dumps(payload) if payload is not None else None, headers=headers)
        response = self.connection.getresponse()
        body = response.read()
        value = json.loads(body) if (response.getheader("Content-Type") or "").startswith("application/json") else body.decode("utf-8")
        return response.status, value

    def body(self, path: str) -> dict[str, Any]:
        status, value = self.request("GET", path)
        assert status == 200, value
        return value


@pytest.fixture
def studio_http(tmp_path: Path) -> Iterator[_StudioHttp]:
    TopicAdminService(tmp_path).create_topic("seed", "示例主题", (), "# 示例\n- 现有记忆。\n")
    app = PanelApplication(tmp_path, _NoAdminReader(), _TOKEN, ui_mode="studio")
    server = create_panel_server(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        yield _StudioHttp(app, connection)
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "The ephemeral HTTP server must terminate cleanly"


def _preconditions(body: dict[str, Any]) -> dict[str, Any]:
    return {"expected_version": body["version"], "expected_fingerprint": body["fingerprint"]}


def test_actual_studio_admin_loader_includes_archived_topics_over_http(
    studio_http: _StudioHttp, studio_assets: dict[str, str],
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to run the real Studio loader")
    studio_http.app.admin.create_topic("archived", "已归档主题", (), "Archived body.")
    studio_http.app.admin.archive("archived", expected_version=1)
    loader = _function(studio_assets["app.js"], "loadAdminTopicListing")
    harness = """
const state = { role: "admin", topicAdmin: { sessionId: 1 } };
const routes = [];
async function api(path) {
  routes.push(path);
  const response = await fetch(process.env.STUDIO_URL + path, {
    headers: { Authorization: "Bearer " + process.env.STUDIO_TOKEN }
  });
  const value = await response.json();
  if (!response.ok) throw new Error(JSON.stringify(value));
  return value;
}
""" + loader + """
loadAdminTopicListing().then(items => {
  process.stdout.write(JSON.stringify({ items, routes }));
}).catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", harness],
        env={"STUDIO_URL": f"http://127.0.0.1:{studio_http.connection.port}", "STUDIO_TOKEN": _TOKEN},
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["routes"] == ["/api/topics?page=1&page_size=100"]
    assert {item["topic_id"]: item["status"] for item in output["items"]} == {"seed": "active", "archived": "archived"}


def test_creation_cancel_cannot_interrupt_inflight_write_or_readback(studio_assets: dict[str, str]) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise Studio cancellation")
    script = studio_assets["app.js"]
    controls = _function(script, "syncTopicAdminControls")
    assert '$("btn-topic-create-cancel").disabled = !canAdmin || state.topicCreate.busy' in controls
    harness = """
const assert = require("node:assert/strict");
const state = {
  role: "admin", selectedId: "existing", topicAdmin: { sessionId: 1, snapshot: null },
  topicCreate: { requestId: 1, open: true, busy: true, createdId: "new" }
};
let resets = 0;
const $ = () => ({ reset() { resets += 1; }, focus() {} });
function topicAdminStatus() {}
function syncTopicAdminControls() {}
function showGate() { throw new Error("Unexpected authentication reset"); }
let finishRead;
const readPending = new Promise(resolve => { finishRead = resolve; });
async function refreshAll({ preferredTopicId }) {
  await readPending;
  state.selectedId = preferredTopicId;
  state.topicAdmin.snapshot = { version: 1 };
  return true;
}
""" + _function(script, "cancelTopicCreate") + "\n" + _function(script, "finishTopicCreate") + """
(async () => {
  cancelTopicCreate();
  assert.equal(state.topicCreate.requestId, 1);
  assert.equal(state.topicCreate.open, true);
  assert.equal(resets, 0);
  state.topicCreate.busy = false;
  const completion = finishTopicCreate(1, 1, "new");
  assert.equal(state.topicCreate.busy, true);
  cancelTopicCreate();
  assert.equal(state.topicCreate.requestId, 1);
  assert.equal(state.topicCreate.open, true);
  assert.equal(state.selectedId, "existing");
  finishRead();
  await completion;
  assert.equal(state.selectedId, "new");
  assert.equal(state.topicCreate.open, false);
  assert.equal(state.topicCreate.busy, false);
  assert.equal(resets, 1);
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("character_count", (4000, 4001))
def test_core_editor_counts_unicode_characters_against_real_http_limit(
    studio_http: _StudioHttp, studio_assets: dict[str, str], character_count: int,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the Studio Core editor")
    assert studio_http.request("POST", "/api/core-memory/initialize", {"body": "Initial Core body."})[0] == 201
    baseline = studio_http.body("/api/core-memory/body")
    body = "🧠" * 2001 + "汉" * (character_count - 2001)
    script = studio_assets["app.js"]
    harness = """
const baseline = JSON.parse(process.env.CORE_BASELINE);
const input = { value: process.env.CORE_BODY };
const state = { role: "admin", admin: { coreBusy: false, editor: { mode: "replace", pending: false, baseline } } };
const $ = () => input;
const output = { sent: false, status: null, error: "" };
let completion = Promise.resolve();
function setAdminError(id, message) { output.error = message; }
function mutateCore(action, payload) {
  output.sent = true;
  completion = fetch(process.env.STUDIO_URL + "/api/core-memory/" + action, {
    method: "POST", headers: { Authorization: "Bearer " + process.env.STUDIO_TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }).then(response => { output.status = response.status; return response.text(); });
}
""" + _function(script, "coreCharacterCount") + "\n" + _function(script, "saveCoreEdit") + """
saveCoreEdit();
completion.then(() => { process.stdout.write(JSON.stringify(output)); }).catch(error => {
  process.stderr.write(String(error)); process.exitCode = 1;
});
"""
    result = subprocess.run(
        [node, "-e", harness],
        env={
            "STUDIO_URL": f"http://127.0.0.1:{studio_http.connection.port}", "STUDIO_TOKEN": _TOKEN,
            "CORE_BASELINE": json.dumps(baseline), "CORE_BODY": body,
        },
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    readback = studio_http.body("/api/core-memory/body")
    if character_count == 4000:
        assert output == {"sent": True, "status": 200, "error": ""}
        assert readback["body"] == body and readback["version"] == 2
    else:
        assert output["sent"] is False and "4000" in output["error"]
        assert readback == baseline


def test_studio_admin_topic_and_core_lifecycle_over_real_http(studio_http: _StudioHttp, tmp_path: Path) -> None:
    http = studio_http
    status, html = http.request("GET", "/?token_login=1")
    assert status == 200 and 'data-tab="admin"' in html and "/studio/app.js" in html
    assert http.body("/api/core-memory")["core_memory"]["present"] is False
    assert http.request("GET", "/api/core-memory/body")[0] == 404
    assert http.request("GET", "/api/core-memory/versions")[0] == 404

    topic_id, display_name = "studio-topic", "Studio 管理主题"
    path = "/api/topics/" + topic_id
    original = "# Studio 主题\n- 初始事实。\n"
    edited = "# Studio 主题\n- 已修改的事实。\n"
    status, created = http.request("POST", "/api/topics", {
        "topic_id": topic_id, "display_name": display_name, "aliases": ["studio-alias"], "body": original,
    })
    assert status == 201 and created["topic"]["version"] == 1
    current = http.body(path + "/body")
    assert current["body"] == original
    status, replaced = http.request("POST", path + "/replace", {"body": edited, **_preconditions(current)})
    assert status == 200 and replaced["topic"]["version"] == 2
    current = http.body(path + "/body")
    assert current["body"] == edited
    assert [item["version"] for item in http.body(path + "/versions")["items"]] == [2, 1]
    status, rolled = http.request("POST", path + "/rollback", {"target_version": 1, **_preconditions(current)})
    assert status == 200 and rolled["topic"]["version"] == 3
    assert http.body(path + "/body")["body"] == original
    assert http.body(path + "/versions/2/body")["body"] == edited

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for kind, value in (("chat_id", "studio-chat"), ("repo_url", "https://github.com/Studio/P2.git"), ("workspace_path", str(workspace))):
        current = http.body(path + "/body")
        status, bound = http.request("POST", "/api/v1/topics/bind", {"topic": topic_id, kind: value, **_preconditions(current)})
        assert status == 200 and bound["topic"]["topic_id"] == topic_id
    detail = http.body(path)["topic"]
    assert detail["chat_ids"] == ["studio-chat"]
    assert detail["repo_urls"] == ["studio/p2"]
    assert detail["workspace_paths"] == [str(workspace)]
    assert detail["version"] == 3

    current = http.body(path + "/body")
    status, archived = http.request("POST", path + "/archive", _preconditions(current))
    assert status == 200 and archived["topic"]["state"] == "archived"
    listed = http.body("/api/topics")["items"]
    assert next(item for item in listed if item["topic_id"] == topic_id)["status"] == "archived"
    assert topic_id not in {item["topic_id"] for item in http.body("/api/v1/topics")["topics"]}
    assert http.body(path)["topic"]["chat_ids"] == ["studio-chat"]
    status, trashed = http.request("POST", path + "/trash", _preconditions(http.body(path + "/body")))
    assert status == 200 and trashed["item"]["prior_state"] == "archived"
    recycle_item = next(item for item in http.body("/api/recycle-bin")["items"] if item["topic_id"] == topic_id)
    assert recycle_item["trashed_at"] == trashed["item"]["trashed_at"]
    assert http.request("GET", path + "/body")[0] == 404
    status, restored = http.request("POST", "/api/recycle-bin/" + topic_id + "/restore", {"trashed_at": recycle_item["trashed_at"]})
    assert status == 200 and restored["topic"]["state"] == "archived"
    assert http.body(path + "/body")["body"] == original
    status, restored = http.request("POST", path + "/restore", _preconditions(http.body(path + "/body")))
    assert status == 200 and restored["topic"]["state"] == "active"
    assert topic_id in {item["topic_id"] for item in http.body("/api/v1/topics")["topics"]}

    status, trashed = http.request("POST", path + "/trash", _preconditions(http.body(path + "/body")))
    assert status == 200
    recycle_path = "/api/recycle-bin/" + topic_id
    delete_payload = {"trashed_at": trashed["item"]["trashed_at"], "confirmation": topic_id}
    status, wrong_name = http.request("POST", recycle_path + "/delete", delete_payload)
    assert status == 409 and wrong_name["error"]["code"] == "confirmation_mismatch"
    assert any(item["topic_id"] == topic_id for item in http.body("/api/recycle-bin")["items"])
    status, deleted = http.request("POST", recycle_path + "/delete", {**delete_payload, "confirmation": display_name})
    assert status == 200 and deleted["result"]["status"] == "permanently_deleted"
    assert all(item["topic_id"] != topic_id for item in http.body("/api/recycle-bin")["items"])

    core_original = "# Core / WhoAmI\n- 我们使用真实版本历史。\n"
    core_edited = "# Core / WhoAmI\n- 编辑后的核心记忆。\n"
    status, initialized = http.request("POST", "/api/core-memory/initialize", {"body": core_original})
    assert status == 201 and initialized["core_memory"]["present"] is True
    current = http.body("/api/core-memory/body")
    status, replaced = http.request("POST", "/api/core-memory/replace", {"body": core_edited, **_preconditions(current)})
    assert status == 200 and replaced["core_memory"]["version"] == 2
    current = http.body("/api/core-memory/body")
    assert current["body"] == core_edited
    assert [item["version"] for item in http.body("/api/core-memory/versions")["items"]] == [2, 1]
    status, rolled = http.request("POST", "/api/core-memory/rollback", {
        "target_version": 1, "confirmation": "Core / WhoAmI", **_preconditions(current),
    })
    assert status == 200 and rolled["core_memory"]["version"] == 3
    assert http.body("/api/core-memory/body")["body"] == core_original
    assert http.body("/api/core-memory/versions/2/body")["body"] == core_edited


def test_binding_rejects_stale_version_and_fingerprint_without_domain_effects(studio_http: _StudioHttp) -> None:
    http = studio_http
    path = "/api/topics/seed/body"
    stale = http.body(path)
    assert http.request("POST", "/api/topics/seed/replace", {"body": "Updated canonical body.", **_preconditions(stale)})[0] == 200
    status, value = http.request("POST", "/api/v1/topics/bind", {"topic": "seed", "chat_id": "must-not-bind", **_preconditions(stale)})
    assert status == 409 and value["error"]["code"] == "concurrent_update"
    current = http.body(path)
    status, value = http.request("POST", "/api/v1/topics/bind", {
        "topic": "seed", "chat_id": "must-not-bind", "expected_version": current["version"], "expected_fingerprint": "0" * 64,
    })
    assert status == 409 and value["error"]["code"] == "concurrent_update"
    assert http.body("/api/topics/seed")["topic"]["chat_ids"] == []
    assert http.body(path) == current
    status, bound = http.request("POST", "/api/v1/topics/bind", {"topic": "seed", "chat_id": "safe-binding", **_preconditions(current)})
    assert status == 200 and bound["topic"]["chat_ids"] == ["safe-binding"]
    assert http.body(path) == current, "Binding must not generate a canonical body version"


@pytest.mark.parametrize("guards", (
    {"expected_version": 1}, {"expected_fingerprint": "0" * 64},
    {"expected_version": 0, "expected_fingerprint": "0" * 64},
    {"expected_version": True, "expected_fingerprint": "0" * 64},
    {"expected_version": "1", "expected_fingerprint": "0" * 64},
    {"expected_version": 1, "expected_fingerprint": "INVALID"},
))
def test_binding_preconditions_must_be_a_valid_complete_pair(studio_http: _StudioHttp, guards: dict[str, Any]) -> None:
    status, value = studio_http.request("POST", "/api/v1/topics/bind", {"topic": "seed", "chat_id": "must-not-bind", **guards})
    assert status == 400 and value["error"]["code"] == "invalid_request"
    assert studio_http.body("/api/topics/seed")["topic"]["chat_ids"] == []


@pytest.mark.parametrize("role", ("read", "write"))
def test_read_write_principals_keep_core_reads_without_admin_mutations(studio_http: _StudioHttp, role: str) -> None:
    http = studio_http
    status, issued = http.request("POST", "/api/keys", {"name": "studio-" + role, "role": role})
    assert status == 201
    token = issued["secret"]
    status, me = http.request("GET", "/api/auth/me", token=token)
    assert status == 200 and me["role"] == role
    assert http.request("GET", "/api/core-memory", token=token)[0] == 200
    assert http.request("GET", "/api/recycle-bin", token=token)[0] == 403
    for path, payload in (
        ("/api/topics", {"topic_id": "forbidden", "display_name": "禁止创建", "aliases": [], "body": "Forbidden."}),
        ("/api/topics/seed/replace", {"body": "Forbidden.", **_preconditions(http.body("/api/topics/seed/body"))}),
        ("/api/core-memory/initialize", {"body": "Forbidden Core."}),
    ):
        status, denied = http.request("POST", path, payload, token=token)
        assert status == 403 and denied["error"]["code"] == "forbidden"
    status, result = http.request("POST", "/api/v1/topics/bind", {"topic": "seed", "chat_id": "legacy-write-binding"}, token=token)
    assert status == (200 if role == "write" else 403)
    if role == "write":
        assert result["topic"]["chat_ids"] == ["legacy-write-binding"]
