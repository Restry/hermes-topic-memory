from __future__ import annotations

import re
import shutil
import subprocess
from importlib import resources

import pytest

from test_studio_p0 import _StudioDom, _click_handler, _css_block, _function, _options


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


def _client_section(script: str, client: str) -> str:
    generator = _function(script, "buildClientConfig")
    match = re.search(rf'(?ms)^      case "{re.escape(client)}":.*?(?=^      (?:case |default:))', generator)
    assert match is not None, f"Missing client configuration: {client}"
    return match.group()


def test_keys_is_the_fourth_accessible_studio_tab(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    tabs = [node for node in studio_dom.ids["studio-tabs"].descendants() if node.attrs.get("role") == "tab"]
    assert [node.attrs.get("data-tab") for node in tabs] == ["topic", "ask", "logs", "keys"]
    tab = tabs[-1]
    assert tab.attrs.get("id") == "tab-keys"
    assert tab.attrs.get("type") == "button"
    assert tab.text_content().strip() == "接入与密钥"
    assert tab.attrs.get("aria-controls") == "panel-keys"
    assert tab.attrs.get("aria-selected") == "false" and tab.attrs.get("tabindex") == "-1"
    panel = studio_dom.ids["panel-keys"]
    assert panel.attrs.get("role") == "tabpanel"
    assert panel.attrs.get("aria-labelledby") == "tab-keys"
    assert panel.attrs.get("tabindex") == "0" and "hidden" in panel.attrs
    switch = _function(studio_assets["app.js"], "switchTab")
    assert '["topic", "ask", "logs", "keys"]' in switch
    assert "panel.hidden = nameOfPanel !== name" in switch
    assert 'if (name === "keys")' in switch and "renderIntegration()" in switch


def test_key_form_controls_are_labelled_and_offer_the_approved_permissions(studio_dom: _StudioDom) -> None:
    labels = {node.attrs.get("for"): node for node in studio_dom.nodes if node.tag == "label"}
    for identifier in ("key-name", "key-role", "key-topics", "key-expiry"):
        assert labels[identifier].text_content().strip()
        assert "sr-only" not in (labels[identifier].attrs.get("class") or "").split()
        control = studio_dom.ids[identifier]
        assert control.attrs.get("name")
        assert "key-create-error" in (control.attrs.get("aria-describedby") or "").split()
    assert "required" in studio_dom.ids["key-name"].attrs
    assert studio_dom.ids["key-name"].attrs.get("maxlength") == "80"
    assert studio_dom.ids["key-name"].attrs.get("autocomplete") == "off"
    assert _options(studio_dom.ids["key-role"]) == {
        "read": "只读召回", "write": "读写与 Dream", "admin": "完全管理",
    }
    assert _options(studio_dom.ids["key-expiry"]) == {
        "": "永不过期", "7": "7 天", "30": "30 天", "90": "90 天", "365": "365 天",
    }
    assert studio_dom.ids["key-topics"].attrs.get("list") == "key-topic-options"
    assert studio_dom.ids["key-topic-options"].tag == "datalist"
    assert "留空" in studio_dom.ids["key-topics-help"].text_content()
    assert "全部主题" in studio_dom.ids["key-topics-help"].text_content()
    assert studio_dom.ids["key-create-fields"].tag == "fieldset"
    assert studio_dom.ids["btn-key-create"].attrs.get("type") == "submit"


def test_scope_input_rejects_unknown_invalid_and_duplicate_ids_before_creation(studio_assets: dict[str, str]) -> None:
    script = studio_assets["app.js"]
    form = _function(script, "readKeyForm")
    assert '$("key-name").value.trim()' in form and "[...name].length > 80" in form
    assert "let allowedTopics = null" in form and "if (rawTopics)" in form
    assert "availableTopics = new Set(state.topics.map((topic) => topic.topic_id))" in form
    assert "!allowedTopics.length" in form and "!availableTopics.has(id)" in form
    assert "id.length > 128" in form and "^[a-z0-9]+(?:-[a-z0-9]+)*$" in form
    assert "new Set(allowedTopics).size !== allowedTopics.length" in form
    assert 'allowed_topics: allowedTopics' in form
    assert 'expires_in_days: expiry ? Number(expiry) : null' in form
    assert '["", "7", "30", "90", "365"].includes(expiry)' in form
    create = _function(script, "createKey")
    assert create.index("readKeyForm()") < create.index('api("/api/keys"')
    assert create.index("if (!payload) return") < create.index('api("/api/keys"')
    options = _function(script, "renderKeyTopicOptions")
    assert "option.value = topic.topic_id" in options and "option.textContent =" in options
    error = _function(script, "keyFormError")
    assert 'setAttribute("aria-invalid", "true")' in error and "$(id).focus()" in error


def test_nonadmin_keys_view_never_requests_key_management(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    note = studio_dom.ids["keys-permission-note"]
    assert "管理员权限" in note.text_content() and "接入配置" in note.text_content()
    for identifier in ("key-admin-create", "key-admin-list"):
        assert "hidden" in studio_dom.ids[identifier].attrs
        assert "hidden" in (studio_dom.ids[identifier].attrs.get("class") or "").split()
    script = studio_assets["app.js"]
    auth = _function(script, "applyAuth")
    assert '["read", "write", "admin"].includes(me.role) ? me.role : null' in auth
    assert "me.actor" in auth and "me.key_id" in auth
    assert "applyAuth(me)" in _function(script, "bootstrap")
    login = script[script.index('  $("token-form").addEventListener("submit"'):script.index('  $("btn-logout").addEventListener("click"')]
    assert 'api("/api/auth/me")' in login and "applyAuth(me)" in login
    controls = _function(script, "syncKeyControls")
    assert 'const admin = state.role === "admin"' in controls
    assert '$(id).hidden = !admin' in controls
    assert '$("keys-workspace").classList.toggle("integration-only", !admin)' in controls
    for name, route in (("refreshKeys", 'api("/api/keys"'), ("createKey", 'api("/api/keys"'), ("revokeKey", 'api("/api/keys/"')):
        body = _function(script, name)
        assert body.index('state.role !== "admin"') < body.index(route)
    assert len(re.findall(r'api\("/api/keys', script)) == 3


def test_read_principals_keep_recall_but_cannot_launch_dream_or_remember(studio_assets: dict[str, str]) -> None:
    script = studio_assets["app.js"]
    controls = _function(script, "updateTopicActions")
    assert 'state.role === "write" || state.role === "admin"' in controls
    for identifier in ("btn-toggle-remember", "btn-remember-submit", "btn-dream-preview", "btn-dream-apply", "btn-dream-commit-inline"):
        assert f'$("{identifier}").disabled = !canWrite' in controls
    assert 'state.role !== "write" && state.role !== "admin"' in _function(script, "runDream")
    assert 'state.role !== "write" && state.role !== "admin"' in _function(script, "requestDreamApply")
    remember = script[script.index('  $("remember-drawer").addEventListener("submit"'):script.index('  $("btn-dream-preview").addEventListener("click"')]
    assert remember.index('state.role !== "write" && state.role !== "admin"') < remember.index('api("/api/v1/remember"')
    assert "canWrite" not in _function(script, "submitQuestion")


def test_one_time_secret_starts_hidden_and_has_explicit_copy_warning_and_dismiss(studio_dom: _StudioDom) -> None:
    card = studio_dom.ids["key-secret-card"]
    assert "hidden" in card.attrs and "hidden" in (card.attrs.get("class") or "").split()
    assert card.attrs.get("aria-labelledby") == "key-secret-title"
    assert card.attrs.get("aria-describedby") == "key-secret-warning"
    assert "明文密钥仅显示这一次，关闭后无法再次查看" in studio_dom.ids["key-secret-warning"].text_content()
    value = studio_dom.ids["key-secret-value"]
    assert value.tag == "pre" and not value.text_content()
    assert value.attrs.get("tabindex") == "0" and value.attrs.get("aria-label")
    for identifier, label in (("btn-key-copy", "复制密钥"), ("btn-key-dismiss", "关闭并清除明文")):
        button = studio_dom.ids[identifier]
        assert button.tag == "button" and button.attrs.get("type") == "button"
        assert button.text_content().strip() == label


def test_secret_is_volatile_cleared_from_reveal_and_config_and_guarded_against_late_responses(studio_assets: dict[str, str]) -> None:
    script = studio_assets["app.js"]
    for forbidden in ("localStorage", "sessionStorage", "indexedDB", "console.", "setItem("):
        assert forbidden not in script
    clear = _function(script, "clearKeySecret")
    for statement in ('state.keys.secret = ""', 'state.keys.secretKeyId = null', '$("key-secret-value").textContent = ""', '$("integration-config").textContent = ""', '$("key-secret-card").hidden = true'):
        assert statement in clear
    assert "renderIntegration()" in clear and "syncKeyControls()" in clear
    assert "clearKeySecret({ restoreFocus: true })" in _click_handler(script, "btn-key-dismiss")
    session = _function(script, "clearKeySession")
    assert "clearKeySecret()" in session and "keys.controller.abort()" in session
    assert "keys.requestId += 1" in session and "keys.mutationRequestId += 1" in session
    assert "clearKeySession()" in _function(script, "showGate")
    logout = _click_handler(script, "btn-logout")
    assert logout.index("clearKeySession()") < logout.index('api("/api/sso/logout"')
    create = _function(script, "createKey")
    assert create.index("requestId !== keys.mutationRequestId") < create.index("keys.secret = res.secret")
    assert "keys.items.unshift(res.key)" in create
    assert "res.secret" not in _function(script, "renderKeyList")


def test_copy_controls_read_current_secret_and_current_client_at_click_time(studio_assets: dict[str, str]) -> None:
    script = studio_assets["app.js"]
    secret = _click_handler(script, "btn-key-copy")
    assert "navigator.clipboard.writeText(state.keys.secret)" in secret
    assert '$("key-secret-card").hidden' in secret
    config = _click_handler(script, "btn-config-copy")
    assert "const config = buildClientConfig()" in config
    assert "navigator.clipboard.writeText(config.text)" in config
    assert "catch" in secret and "catch" in config
    generator = _function(script, "buildClientConfig")
    assert "client = state.keys.client" in generator
    assert 'const token = state.keys.secret || "${TOPIC_MEMORY_TOKEN}"' in generator
    assert "state.token" not in generator


def test_integration_client_segments_have_roving_aria_and_arrow_navigation(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    tablist = studio_dom.ids["integration-clients"]
    assert tablist.attrs.get("role") == "tablist" and tablist.attrs.get("aria-label")
    tabs = [node for node in tablist.descendants() if node.attrs.get("role") == "tab"]
    assert [(node.attrs.get("data-client"), node.text_content().strip()) for node in tabs] == [
        ("hermes", "Hermes"), ("omp", "OMP (Pi)"), ("opencode", "OpenCode"), ("codex", "Codex"), ("curl", "curl"),
    ]
    assert sum(node.attrs.get("aria-selected") == "true" for node in tabs) == 1
    for tab in tabs:
        assert tab.attrs.get("type") == "button"
        assert tab.attrs.get("aria-controls") == "integration-config-panel"
        assert tab.attrs.get("tabindex") == ("0" if tab.attrs.get("aria-selected") == "true" else "-1")
    panel = studio_dom.ids["integration-config-panel"]
    assert panel.attrs.get("role") == "tabpanel" and panel.attrs.get("aria-labelledby") == "client-hermes"
    script = studio_assets["app.js"]
    switch = _function(script, "switchClient")
    assert 'setAttribute("aria-selected", String(active))' in switch
    assert "tab.tabIndex = active ? 0 : -1" in switch
    assert 'setAttribute("aria-labelledby", "client-" + client)' in switch
    handlers = script[script.index("  const clientTabs ="):script.index('  $("activity-filters").addEventListener')]
    for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
        assert f'e.key === "{key}"' in handlers
    assert "e.preventDefault()" in handlers
    assert 'switchClient(clientTabs[next].getAttribute("data-client"), { focus: true })' in handlers


def test_five_configs_use_current_origin_and_verified_native_formats(studio_assets: dict[str, str]) -> None:
    script = studio_assets["app.js"]
    generator = _function(script, "buildClientConfig")
    assert "const origin = window.location.origin" in generator and 'origin + "/mcp"' in generator
    hermes = _client_section(script, "hermes")
    assert '"mcp_servers:"' in hermes and '"    headers:"' in hermes
    assert 'JSON.stringify("Bearer " + token)' in hermes
    omp = _client_section(script, "omp")
    assert 'mcpServers: { topic_memory: { type: "http", url, headers:' in omp
    assert ".omp/mcp.json" in omp and ".pi/mcp.json" in omp and "无需桥接" in omp
    opencode = _client_section(script, "opencode")
    assert 'mcp: { servers: { topic_memory: { type: "remote", url, oauth: false' in opencode
    assert 'hasSecret ? token : "{env:TOPIC_MEMORY_TOKEN}"' in opencode
    codex = _client_section(script, "codex")
    assert '"[mcp_servers.topic_memory]"' in codex
    assert 'http_headers = { Authorization = ' in codex and 'JSON.stringify("Bearer " + token)' in codex
    assert 'bearer_token_env_var = "TOPIC_MEMORY_TOKEN"' in codex
    assert "Codex 不展开静态 ${TOPIC_MEMORY_TOKEN}" in codex
    curl = _client_section(script, "curl")
    assert 'origin + "/api/v1/topics"' in curl
    assert 'JSON.stringify("Authorization: Bearer " + token)' in curl
    assert "curl --fail-with-body --silent --show-error" in curl
    assert "${TOPIC_MEMORY_TOKEN}" in _function(script, "renderIntegration")


def test_key_list_metadata_is_localized_semantic_and_revocation_is_inline(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    assert studio_dom.ids["key-list"].tag == "ul" and studio_dom.ids["key-list"].attrs.get("aria-label")
    script = studio_assets["app.js"]
    listing = _function(script, "renderKeyList")
    for field in ("name", "prefix", "role", "allowed_topics", "status", "created_at", "last_used_at", "expires_at", "created_by", "key_id", "revoked_at"):
        assert f"record.{field}" in listing
    assert 'document.createElement("dl")' in listing and "makeTimestamp(record.created_at)" in listing
    for label in ("全部主题", "尚未使用", "永不过期", "确认吊销", "取消", "正在吊销…"):
        assert label in listing
    for label in ("活跃", "已吊销", "已过期"):
        assert label in script
    assert 'setAttribute("aria-expanded", String(keys.confirmKeyId === record.key_id))' in listing
    assert 'if (keys.confirmKeyId === record.key_id) trigger.setAttribute("aria-controls"' in listing
    assert 'setAttribute("role", "alert")' in listing
    assert "当前登录密钥" in listing
    assert "window.confirm" not in script and not re.search(r"\bconfirm\s*\(", script)
    revoke = _function(script, "revokeKey")
    assert 'keys.confirmKeyId !== keyId' in revoke
    assert 'encodeURIComponent(keyId) + "/revoke"' in revoke
    assert "if (keys.secretKeyId === keyId) clearKeySecret()" in revoke
    assert "if (state.keyId === keyId)" in revoke and "showGate()" in revoke
    assert "trigger.focus()" in _function(script, "cancelKeyRevoke")


def test_key_load_and_mutation_states_have_recovery_and_latest_response_guards(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    for identifier in ("keys-loading", "key-create-status"):
        assert studio_dom.ids[identifier].attrs.get("role") == "status"
        assert studio_dom.ids[identifier].attrs.get("aria-live") == "polite"
    for identifier in ("keys-error", "key-create-error"):
        assert studio_dom.ids[identifier].attrs.get("role") == "alert"
    assert "创建" in studio_dom.ids["keys-empty"].text_content()
    assert studio_dom.ids["key-list"].attrs.get("aria-busy") == "false"
    script = studio_assets["app.js"]
    loading = _function(script, "refreshKeys")
    assert "keys.controller.abort()" in loading and "signal: controller.signal" in loading
    assert "requestId !== keys.requestId" in loading and "Array.isArray(res.items)" in loading
    assert 'setAttribute("aria-busy", "true")' in loading and 'setAttribute("aria-busy", "false")' in loading
    assert "finally" in loading and '$("keys-error").classList.remove("hidden")' in loading
    for identifier in ("btn-keys-refresh", "btn-keys-retry"):
        assert "refreshKeys" in _click_handler(script, identifier)
    for name in ("createKey", "revokeKey"):
        mutation = _function(script, name)
        assert "requestId !== keys.mutationRequestId" in mutation and "finally" in mutation
    assert "keys.revokeError =" in _function(script, "revokeKey")


def test_primary_hierarchy_tokens_focus_and_narrow_wrapping_are_preserved(studio_dom: _StudioDom, studio_assets: dict[str, str]) -> None:
    for identifier in ("btn-dream-preview", "btn-dream-commit-inline"):
        classes = (studio_dom.ids[identifier].attrs.get("class") or "").split()
        assert "btn-purple-outline" in classes and "btn-purple" not in classes and "btn-primary" not in classes
    assert "btn-ghost" in (studio_dom.ids["btn-config-copy"].attrs.get("class") or "").split()
    controls = _function(studio_assets["app.js"], "syncKeyControls")
    assert '$("btn-key-create").classList.toggle("btn-primary", !hasSecret)' in controls
    assert '$("btn-key-create").classList.toggle("btn-ghost", hasSecret)' in controls
    assert '$("key-create-fields").disabled = !admin || busy || hasSecret' in controls
    topic = _function(studio_assets["app.js"], "updateTopicActions")
    assert 'classList.toggle("btn-primary", !inlinePrimary)' in topic
    css = studio_assets["styles.css"]
    without_root = re.sub(r"(?s):root\s*\{.*?\}", "", css, count=1)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", without_root)
    outline = _css_block(css, r"\.btn-purple-outline\b")
    assert "var(--purple-border)" in outline and "var(--purple-hover)" in outline
    assert "button:focus-visible" in css and "select:focus-visible" in css and '[tabindex="0"]:focus-visible' in css
    assert "overflow-wrap: anywhere" in _css_block(css, r"\.key-secret-value\b")
    assert "overflow-wrap: anywhere" in _css_block(css, r"\.code-box\b")
    assert "flex-wrap: wrap" in _css_block(css, r"\.integration-clients\b")
    narrow = _css_block(css, r"@media\s*\(max-width:\s*960px\)")
    assert ".keys-workspace { grid-template-columns: minmax(0, 1fr)" in narrow
    assert ".integration-clients .seg-btn { flex: 1 1 auto; min-width: 0" in narrow


def test_key_lifecycle_actions_remain_localized_in_the_activity_filter(studio_dom: _StudioDom) -> None:
    actions = _options(studio_dom.ids["activity-action"])
    assert actions["key_create"] == "创建密钥"
    assert actions["key_revoke"] == "吊销密钥"


def test_studio_p1_uses_only_safe_external_dom_code(studio_assets: dict[str, str]) -> None:
    script = studio_assets["app.js"]
    for boundary in ("inner", "outer"):
        assert boundary + "HTML" not in script
    for unsafe in ("insertAdjacentHTML", "document.write", "eval(", "new Function("):
        assert unsafe not in script
    assert "key_hash" not in script
    assert "document.createElement" in script and "textContent" in script
    assert not re.search(r"\.style\s*(?:\.|\[|=)", script)
    assert not re.search(r'\.setAttribute\(\s*[\"\'](?:style|on\w+)[\"\']', script)


def test_studio_p1_javascript_source_is_syntactically_valid() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the Studio source syntax contract")
    asset = resources.files("hermes_topic_memory").joinpath("studio_assets", "app.js")
    with resources.as_file(asset) as path:
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
