(() => {
  "use strict";

  const state = {
    token: "",
    authMode: null,
    role: null,
    actor: "",
    keyId: null,
    activeTab: "topic",
    ssoEnabled: false,
    topics: [],
    charsMap: {},
    filter: "all",
    selectedId: null,
    currentBody: "",
    viewMode: "pretty",
    overviewRequestId: 0,
    topicRequestId: 0,
    dreamBusy: false,
    rememberBusy: false,
    dreamConfirmTopicId: null,
    dreamConfirmTrigger: null,
    dreamReportsRequestId: 0,
    dreamReportsController: null,
    activities: {
      page: 0,
      pageSize: 30,
      total: 0,
      items: [],
      seenIds: new Set(),
      filters: { action: "", source: "", status: "", q: "" },
      requestId: 0,
      controller: null,
      loading: false,
      failedReset: true,
    },
    keys: {
      items: [],
      loaded: false,
      loading: false,
      requestId: 0,
      controller: null,
      mutationRequestId: 0,
      creating: false,
      secret: "",
      secretKeyId: null,
      confirmKeyId: null,
      revokingKeyId: null,
      revokeError: "",
      client: "hermes",
    },
  };

  const $ = (id) => document.getElementById(id);
  const activityLabels = {
    action: optionLabels("activity-action"),
    source: optionLabels("activity-source"),
    status: optionLabels("activity-status"),
  };
  const activityStatusClasses = {
    ok: "pill-green", no_fact: "pill-gray", duplicate: "pill-gray",
    skipped: "pill-amber", error: "pill-red", denied: "pill-red",
  };
  const keyRoleLabels = optionLabels("key-role");
  const keyRoleClasses = { read: "pill-blue", write: "pill-purple", admin: "pill-amber" };
  const keyStatusLabels = { active: "活跃", revoked: "已吊销", expired: "已过期" };
  const keyStatusClasses = { active: "pill-green", revoked: "pill-gray", expired: "pill-amber" };
  const dreamStatusLabels = {
    consolidated: "已换版", no_change: "无变更", dry_run: "预览未写入",
    skipped_no_new_evidence: "无新证据", aborted_concurrent_write: "并发写入已中止", error: "失败",
  };
  const dateTimeFormat = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  let toastTimer = null;
  let toastHasError = false;

  function toast(msg, { error = false } = {}) {
    if (toastHasError && !error) return;
    const el = $("toast");
    $("toast-message").textContent = msg;
    el.classList.toggle("toast-error", error);
    el.classList.remove("hidden");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = null;
    toastHasError = error;
    if (!error) toastTimer = setTimeout(dismissToast, 2600);
  }

  function dismissToast() {
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = null;
    toastHasError = false;
    $("toast").classList.add("hidden");
  }

  function headers(extra = {}) {
    const h = { ...extra, "X-Topic-Memory-Source": "studio" };
    if (state.token) h["Authorization"] = "Bearer " + state.token;
    return h;
  }

  async function api(path, options = {}) {
    let res;
    try {
      res = await fetch(path, {
        credentials: "same-origin",
        ...options,
        headers: headers(options.headers || {}),
      });
    } catch (err) {
      if (err.name === "AbortError") throw err;
      throw new Error("无法连接服务，请重试");
    }
    if (!res.ok) {
      const msg = res.status === 401
        ? "访问授权已失效，请重新登录（HTTP 401）"
        : "请求失败（HTTP " + res.status + "）";
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    try {
      const text = await res.text();
      return text ? JSON.parse(text) : {};
    } catch (err) {
      if (err.name === "AbortError") throw err;
      throw new Error("服务返回的数据无法读取，请重试");
    }
  }

  function makePill(text, cls = "pill-gray") {
    const el = document.createElement("span");
    el.className = "pill " + cls;
    el.textContent = text;
    return el;
  }

  function optionLabels(id) {
    const labels = {};
    for (const option of $(id).options) {
      if (option.value) labels[option.value] = option.textContent;
    }
    return labels;
  }

  function makeTimestamp(value) {
    const time = document.createElement("time");
    const date = new Date(value || "");
    if (Number.isNaN(date.getTime())) {
      time.textContent = "时间未知";
    } else {
      time.dateTime = date.toISOString();
      time.textContent = dateTimeFormat.format(date);
    }
    return time;
  }

  function makeTopicJump(topicId, prefix = "主题: ") {
    const jump = document.createElement("button");
    jump.type = "button";
    jump.className = "pill pill-blue topic-jump-btn";
    const topic = state.topics.find((item) => item.topic_id === topicId);
    const name = topic ? (topic.display_name || topicId) : topicId;
    jump.textContent = prefix + name + (name !== topicId ? " · " + topicId : "") + (topic ? " →" : "（当前不可用）");
    jump.setAttribute("aria-label", topic ? "查看主题 " + name + "（" + topicId + "）" : "主题 " + topicId + " 当前不可用");
    jump.disabled = !topic;
    jump.addEventListener("click", async () => {
      switchTab("topic", { focus: true });
      if (await selectTopic(topicId) && state.selectedId === topicId) $("panel-topic").focus();
    });
    return jump;
  }

  function endpointLabel(value) {
    if (!value) return "-";
    try {
      const url = new URL(value, window.location.origin);
      if (url.protocol !== "https:" && url.protocol !== "http:") return "Endpoint 已配置";
      url.username = "";
      url.password = "";
      url.search = "";
      url.hash = "";
      return url.host + url.pathname;
    } catch (_) {
      return "Endpoint 已配置";
    }
  }

  function clearNode(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function fmtChars(n) {
    if (!n) return "0 字";
    return n >= 10000 ? (n / 10000).toFixed(1) + "万字" : n + " 字";
  }

  // Render Markdown using text nodes and semantic elements.
  function appendFormattedInline(parent, text) {
    const parts = text.split(/(`[^`]+`)/g);
    for (const part of parts) {
      if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
        const code = document.createElement("code");
        code.className = "md-code";
        code.textContent = part.slice(1, -1);
        parent.appendChild(code);
      } else {
        parent.appendChild(document.createTextNode(part));
      }
    }
  }

  function renderPrettyMarkdown(container, rawText) {
    clearNode(container);
    if (!rawText) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "（当前主题暂无正文内容）";
      container.appendChild(empty);
      return;
    }
    const lines = rawText.split(/\r?\n/);
    for (const line of lines) {
      const s = line.trim();
      if (!s) continue;
      if (s.startsWith("### ")) {
        const h = document.createElement("h4");
        h.className = "md-h3";
        h.textContent = s.slice(4);
        container.appendChild(h);
      } else if (s.startsWith("## ")) {
        const h = document.createElement("h3");
        h.className = "md-h2";
        h.textContent = s.slice(3);
        container.appendChild(h);
      } else if (s.startsWith("# ")) {
        const h = document.createElement("h2");
        h.className = "md-h1";
        h.textContent = s.slice(2);
        container.appendChild(h);
      } else if (s.startsWith("- ") || s.startsWith("* ")) {
        const row = document.createElement("div");
        row.className = "md-li";
        let body = s.slice(2).trim();
        const dateMatch = body.match(/^\[(\d{4}-\d{2}-\d{2}(?:\s*\|\s*[^\]]+)?)\]\s*(.*)$/);
        if (dateMatch) {
          const badge = document.createElement("span");
          badge.className = "md-date";
          badge.textContent = dateMatch[1];
          row.appendChild(badge);
          body = dateMatch[2];
        }
        const txt = document.createElement("span");
        appendFormattedInline(txt, body);
        row.appendChild(txt);
        container.appendChild(row);
      } else {
        const p = document.createElement("p");
        p.className = "md-p";
        appendFormattedInline(p, s);
        container.appendChild(p);
      }
    }
  }

  function renderDiff(container, diffText) {
    clearNode(container);
    if (!diffText) {
      const el = document.createElement("div");
      el.className = "diff-line muted";
      el.textContent = "无正文变更。";
      container.appendChild(el);
      return;
    }
    for (const line of diffText.split(/\r?\n/)) {
      const div = document.createElement("div");
      div.className = "diff-line";
      if (line.startsWith("+") && !line.startsWith("+++")) div.classList.add("diff-add");
      else if (line.startsWith("-") && !line.startsWith("---")) div.classList.add("diff-del");
      else if (line.startsWith("@@")) div.classList.add("diff-hunk");
      div.textContent = line || " ";
      container.appendChild(div);
    }
  }

  function switchTab(name, { focus = false } = {}) {
    if (name !== "topic") clearDreamConfirmation();
    state.activeTab = name;
    if (name !== "keys" && !state.keys.revokingKeyId) cancelKeyRevoke({ restoreFocus: false });
    document.querySelectorAll(".tab").forEach((tab) => {
      const active = tab.getAttribute("data-tab") === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    ["topic", "ask", "logs", "keys"].forEach((nameOfPanel) => {
      const panel = $("panel-" + nameOfPanel);
      panel.classList.toggle("hidden", nameOfPanel !== name);
      panel.hidden = nameOfPanel !== name;
    });
    if (focus) $("tab-" + name).focus();
    if (name === "keys") {
      renderIntegration();
      if (state.role === "admin" && !state.keys.loaded && !state.keys.loading) refreshKeys();
    }
  }

  function updateViewMode(mode) {
    state.viewMode = mode;
    $("btn-view-pretty").classList.toggle("active", mode === "pretty");
    $("btn-view-raw").classList.toggle("active", mode === "raw");
    $("btn-view-pretty").setAttribute("aria-pressed", String(mode === "pretty"));
    $("btn-view-raw").setAttribute("aria-pressed", String(mode === "raw"));
    $("detail-pretty").classList.toggle("hidden", mode !== "pretty");
    $("detail-raw").classList.toggle("hidden", mode !== "raw");
  }

  async function bootstrap() {
    let me;
    try {
      me = await api("/api/auth/me");
    } catch (err) {
      showGate();
      $("auth-error").textContent = "无法确认登录状态：" + err.message;
      $("auth-error").classList.remove("hidden");
      return;
    }
    state.ssoEnabled = Boolean(me.sso_enabled);
    if (state.ssoEnabled) $("sso-entry-wrap").classList.remove("hidden");
    if (!me.authenticated) {
      showGate();
      return;
    }
    applyAuth(me);
    showShell();
    try {
      if (!await refreshAll()) toast("部分数据未能加载，请查看错误提示并刷新重试", { error: true });
    } catch (err) {
      toast("数据加载失败：" + err.message, { error: true });
    }
  }

  function applyAuth(me) {
    clearKeySession();
    state.authMode = me.mode;
    state.role = ["read", "write", "admin"].includes(me.role) ? me.role : null;
    state.actor = typeof me.actor === "string" ? me.actor : "";
    state.keyId = typeof me.key_id === "string" ? me.key_id : null;
    const identity = me.mode === "sso"
      ? ((me.user && me.user.nickname) || "微信已登录")
      : me.mode === "env_token" ? "环境管理密钥" : "API 密钥";
    $("user-badge").textContent = identity + " · " + (keyRoleLabels[state.role] || "权限未知");
    $("user-badge").title = state.actor;
    syncKeyControls();
    updateTopicActions();
    renderIntegration();
  }

  function updateTopicActions() {
    const canWrite = state.role === "write" || state.role === "admin";
    const hasTopic = Boolean(state.selectedId);
    $("btn-toggle-remember").disabled = !canWrite || !hasTopic || state.rememberBusy;
    $("btn-remember-submit").disabled = !canWrite || !hasTopic || state.rememberBusy;
    $("btn-dream-preview").disabled = !canWrite || !hasTopic || state.dreamBusy;
    $("btn-dream-apply").disabled = !canWrite || !hasTopic || state.dreamBusy;
    $("btn-dream-commit-inline").disabled = !canWrite || !hasTopic || state.dreamBusy;
    $("btn-dream-confirm").disabled = !canWrite || state.dreamBusy || !state.dreamConfirmTopicId;
    $("btn-toggle-remember").title = canWrite ? "追加一条长期记忆" : "记忆写入需要读写或管理权限";
    $("btn-dream-preview").title = canWrite ? "预览 Dream，不修改记忆" : "Dream 需要读写或管理权限";
    $("btn-dream-apply").title = canWrite ? "确认后应用 Dream 换版" : "Dream 需要读写或管理权限";
    const inlinePrimary = Boolean(state.dreamConfirmTopicId) || !$("remember-drawer").classList.contains("hidden");
    $("btn-dream-apply").classList.toggle("btn-primary", !inlinePrimary);
    $("btn-dream-apply").classList.toggle("btn-ghost", inlinePrimary);
  }

  function syncKeyControls() {
    const admin = state.role === "admin";
    const keys = state.keys;
    const hasSecret = admin && Boolean(keys.secret);
    const busy = keys.creating || Boolean(keys.revokingKeyId);
    for (const id of ["key-admin-create", "key-admin-list"]) {
      $(id).classList.toggle("hidden", !admin);
      $(id).hidden = !admin;
    }
    $("keys-permission-note").classList.toggle("hidden", admin);
    $("keys-permission-note").hidden = admin;
    $("keys-workspace").classList.toggle("integration-only", !admin);
    $("key-secret-card").classList.toggle("hidden", !hasSecret);
    $("key-secret-card").hidden = !hasSecret;
    $("key-create-fields").disabled = !admin || busy || hasSecret;
    $("btn-key-create").disabled = !admin || busy || hasSecret;
    $("btn-key-create").classList.toggle("btn-primary", !hasSecret);
    $("btn-key-create").classList.toggle("btn-ghost", hasSecret);
    $("btn-key-create").textContent = keys.creating ? "正在创建…" : hasSecret ? "保存明文后继续创建" : "创建密钥";
    $("btn-key-copy").disabled = !hasSecret;
    $("btn-keys-refresh").disabled = keys.loading || busy;
    $("btn-keys-retry").disabled = keys.loading || busy;
  }

  function clearKeySecret({ restoreFocus = false } = {}) {
    state.keys.secret = "";
    state.keys.secretKeyId = null;
    $("key-secret-value").textContent = "";
    $("key-secret-card").classList.add("hidden");
    $("key-secret-card").hidden = true;
    $("integration-config").textContent = "";
    renderIntegration();
    syncKeyControls();
    if (restoreFocus && state.activeTab === "keys") {
      if (state.role === "admin") $("key-name").focus();
      else $("client-" + state.keys.client).focus();
    }
  }

  function clearKeySession() {
    const keys = state.keys;
    keys.requestId += 1;
    keys.mutationRequestId += 1;
    if (keys.controller) keys.controller.abort();
    keys.controller = null;
    keys.items.length = 0;
    keys.loaded = false;
    keys.loading = false;
    keys.creating = false;
    keys.confirmKeyId = null;
    keys.revokingKeyId = null;
    keys.revokeError = "";
    clearKeySecret();
    clearNode($("key-list"));
    clearNode($("key-topic-options"));
    $("key-list").setAttribute("aria-busy", "false");
    $("key-count").textContent = "0 把";
    $("keys-loading").textContent = "";
    $("keys-error").classList.add("hidden");
    $("keys-error-message").textContent = "";
    $("keys-empty").classList.add("hidden");
    $("key-create-form").reset();
    $("key-create-status").textContent = "";
    clearKeyFormErrors();
  }

  function renderKeyTopicOptions() {
    const options = $("key-topic-options");
    clearNode(options);
    if (state.role !== "admin") return;
    for (const topic of state.topics) {
      const option = document.createElement("option");
      option.value = topic.topic_id;
      option.textContent = topic.display_name || topic.topic_id;
      options.appendChild(option);
    }
  }

  function clearKeyFormErrors() {
    for (const id of ["key-name", "key-role", "key-topics", "key-expiry"]) {
      $(id).removeAttribute("aria-invalid");
    }
    $("key-create-error").textContent = "";
    $("key-create-error").classList.add("hidden");
  }

  function keyFormError(id, message) {
    $(id).setAttribute("aria-invalid", "true");
    $("key-create-error").textContent = message;
    $("key-create-error").classList.remove("hidden");
    $(id).focus();
    return null;
  }

  function readKeyForm() {
    clearKeyFormErrors();
    const name = $("key-name").value.trim();
    if (!name || [...name].length > 80 || /[\u0000-\u001f\u007f-\u009f]/.test(name)) {
      return keyFormError("key-name", "请输入 1–80 字的名称，不允许控制字符。");
    }
    const role = $("key-role").value;
    if (!["read", "write", "admin"].includes(role)) return keyFormError("key-role", "请选择有效权限。");
    const rawTopics = $("key-topics").value.trim();
    let allowedTopics = null;
    if (rawTopics) {
      allowedTopics = rawTopics.split(/[,，\s]+/).filter(Boolean);
      const availableTopics = new Set(state.topics.map((topic) => topic.topic_id));
      if (!allowedTopics.length || allowedTopics.some((id) => id.length > 128 || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(id) || !availableTopics.has(id))) {
        return keyFormError("key-topics", "请选择当前可用的主题 ID，多个 ID 用逗号分隔；可刷新全部数据后重试。");
      }
      if (new Set(allowedTopics).size !== allowedTopics.length) return keyFormError("key-topics", "主题 ID 不应重复，请移除重复项。");
    }
    const expiry = $("key-expiry").value;
    if (!["", "7", "30", "90", "365"].includes(expiry)) return keyFormError("key-expiry", "请选择有效的有效期。");
    return { name, role, allowed_topics: allowedTopics, expires_in_days: expiry ? Number(expiry) : null };
  }

  function appendKeyMetadata(list, label, value) {
    const group = document.createElement("div");
    const term = document.createElement("dt");
    const detail = document.createElement("dd");
    term.textContent = label;
    if (typeof value === "string") detail.textContent = value;
    else detail.appendChild(value);
    group.appendChild(term);
    group.appendChild(detail);
    list.appendChild(group);
  }

  function renderKeyList() {
    const list = $("key-list");
    const keys = state.keys;
    clearNode(list);
    if (state.role !== "admin") return;
    for (const record of keys.items) {
      const entry = document.createElement("li");
      entry.className = "key-entry";
      const head = document.createElement("div");
      head.className = "key-entry-head";
      const title = document.createElement("div");
      title.className = "key-entry-title";
      const name = document.createElement("h4");
      name.textContent = record.name;
      title.appendChild(name);
      title.appendChild(makePill(record.prefix + "…", "pill-mono"));
      title.appendChild(makePill(keyRoleLabels[record.role] || "未知权限", keyRoleClasses[record.role] || "pill-gray"));
      title.appendChild(makePill(keyStatusLabels[record.status] || "未知状态", keyStatusClasses[record.status] || "pill-gray"));
      head.appendChild(title);
      const trigger = document.createElement("button");
      trigger.id = "key-revoke-trigger-" + record.key_id;
      trigger.type = "button";
      trigger.className = "btn btn-danger-outline btn-xs";
      trigger.textContent = record.status === "revoked" ? "已吊销" : "吊销";
      trigger.setAttribute("aria-label", "吊销密钥 " + record.name);
      if (keys.confirmKeyId === record.key_id) trigger.setAttribute("aria-controls", "key-revoke-confirm-" + record.key_id);
      trigger.setAttribute("aria-expanded", String(keys.confirmKeyId === record.key_id));
      trigger.disabled = record.status === "revoked" || keys.loading || keys.creating || Boolean(keys.revokingKeyId);
      trigger.addEventListener("click", () => requestKeyRevoke(record.key_id));
      head.appendChild(trigger);
      entry.appendChild(head);
      const scope = document.createElement("div");
      scope.className = "key-scope";
      scope.setAttribute("aria-label", "允许的主题");
      if (record.allowed_topics === null) scope.appendChild(makePill("全部主题"));
      else for (const topicId of record.allowed_topics || []) scope.appendChild(makePill("主题: " + topicId));
      entry.appendChild(scope);
      const metadata = document.createElement("dl");
      metadata.className = "key-metadata";
      appendKeyMetadata(metadata, "创建时间", makeTimestamp(record.created_at));
      appendKeyMetadata(metadata, "最近使用", record.last_used_at ? makeTimestamp(record.last_used_at) : "尚未使用");
      appendKeyMetadata(metadata, "到期时间", record.expires_at ? makeTimestamp(record.expires_at) : "永不过期");
      appendKeyMetadata(metadata, "创建者", record.created_by || "未知");
      appendKeyMetadata(metadata, "密钥 ID", record.key_id);
      if (record.revoked_at) appendKeyMetadata(metadata, "吊销时间", makeTimestamp(record.revoked_at));
      entry.appendChild(metadata);
      if (keys.confirmKeyId === record.key_id) {
        const confirm = document.createElement("div");
        confirm.id = "key-revoke-confirm-" + record.key_id;
        confirm.className = "key-revoke-confirm";
        confirm.setAttribute("role", "group");
        confirm.setAttribute("aria-label", "确认吊销密钥 " + record.name);
        const warning = document.createElement("p");
        warning.textContent = "确认吊销「" + record.name + "」？使用此密钥的客户端将立即失去访问权限，无法恢复。"
          + (state.keyId === record.key_id ? " 这也是你的当前登录密钥，吊销后需要重新登录。" : "");
        confirm.appendChild(warning);
        const actions = document.createElement("div");
        actions.className = "action-row";
        const commit = document.createElement("button");
        commit.id = "key-revoke-commit-" + record.key_id;
        commit.type = "button";
        commit.className = "btn btn-danger-outline btn-sm";
        commit.textContent = keys.revokingKeyId === record.key_id ? "正在吊销…" : "确认吊销";
        commit.disabled = Boolean(keys.revokingKeyId);
        commit.addEventListener("click", () => revokeKey(record.key_id));
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "btn btn-ghost btn-sm";
        cancel.textContent = "取消";
        cancel.disabled = Boolean(keys.revokingKeyId);
        cancel.addEventListener("click", () => cancelKeyRevoke());
        actions.appendChild(commit);
        actions.appendChild(cancel);
        confirm.appendChild(actions);
        if (keys.revokeError) {
          const error = document.createElement("p");
          error.className = "key-revoke-error";
          error.setAttribute("role", "alert");
          error.textContent = keys.revokeError;
          confirm.appendChild(error);
        }
        entry.appendChild(confirm);
      }
      list.appendChild(entry);
    }
    $("key-count").textContent = keys.items.length + " 把";
    $("keys-empty").classList.toggle("hidden", keys.items.length !== 0 || keys.loading || !keys.loaded);
  }

  async function refreshKeys() {
    if (state.role !== "admin") return true;
    const keys = state.keys;
    if (keys.creating || keys.revokingKeyId) return true;
    const requestId = ++keys.requestId;
    if (keys.controller) keys.controller.abort();
    const controller = new AbortController();
    keys.controller = controller;
    keys.loading = true;
    keys.confirmKeyId = null;
    keys.revokeError = "";
    $("key-list").setAttribute("aria-busy", "true");
    $("keys-loading").textContent = "正在加载 API 密钥…";
    $("keys-error").classList.add("hidden");
    syncKeyControls();
    renderKeyList();
    try {
      const res = await api("/api/keys", { signal: controller.signal });
      if (requestId !== keys.requestId || state.role !== "admin") return false;
      if (!Array.isArray(res.items)) throw new Error("密钥列表格式异常，请重试");
      keys.items = res.items;
      keys.loaded = true;
      $("keys-loading").textContent = "已加载 " + keys.items.length + " 把密钥。";
      return true;
    } catch (err) {
      if (requestId !== keys.requestId || err.name === "AbortError") return false;
      if (err.status === 401) {
        showGate();
        $("auth-error").textContent = "登录已失效，请重新登录。";
        $("auth-error").classList.remove("hidden");
        return false;
      }
      $("keys-error-message").textContent = err.status === 403
        ? "当前凭证没有管理密钥的权限，请联系管理员或重新登录。"
        : "密钥列表加载失败：" + err.message;
      $("keys-error").classList.remove("hidden");
      $("keys-loading").textContent = "";
      return false;
    } finally {
      if (requestId === keys.requestId && state.role === "admin") {
        keys.loading = false;
        keys.controller = null;
        $("key-list").setAttribute("aria-busy", "false");
        syncKeyControls();
        renderKeyList();
      }
    }
  }

  async function createKey() {
    if (state.role !== "admin" || state.keys.creating || state.keys.revokingKeyId || state.keys.secret) return;
    const payload = readKeyForm();
    if (!payload) return;
    const keys = state.keys;
    const requestId = ++keys.mutationRequestId;
    keys.requestId += 1;
    if (keys.controller) keys.controller.abort();
    keys.controller = null;
    keys.loading = false;
    keys.confirmKeyId = null;
    keys.creating = true;
    $("key-create-status").textContent = "正在创建密钥，请勿重复提交…";
    $("key-list").setAttribute("aria-busy", "true");
    syncKeyControls();
    renderKeyList();
    try {
      const res = await api("/api/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (requestId !== keys.mutationRequestId || state.role !== "admin") return;
      if (!res.key || typeof res.key.key_id !== "string" || typeof res.secret !== "string" || !/^htm_[A-Za-z0-9_-]{32}$/.test(res.secret)) {
        throw new Error("创建响应不完整，请刷新密钥列表核对结果后再操作");
      }
      keys.secret = res.secret;
      keys.secretKeyId = res.key.key_id;
      const existingIndex = keys.items.findIndex((record) => record.key_id === res.key.key_id);
      if (existingIndex !== -1) keys.items.splice(existingIndex, 1);
      keys.items.unshift(res.key);
      keys.creating = false;
      $("key-secret-value").textContent = keys.secret;
      $("key-create-form").reset();
      $("key-create-status").textContent = "密钥已创建。请保存一次性明文，再关闭卡片。";
      syncKeyControls();
      renderIntegration();
      renderKeyList();
      if (state.activeTab === "keys") $("btn-key-copy").focus();
      refreshKeys();
      refreshLogs();
    } catch (err) {
      if (requestId !== keys.mutationRequestId) return;
      if (err.status === 401) {
        showGate();
        $("auth-error").textContent = "登录已失效，请重新登录。";
        $("auth-error").classList.remove("hidden");
        return;
      }
      $("key-create-status").textContent = "";
      $("key-create-error").textContent = "密钥创建失败：" + err.message + "。请检查表单后重试。";
      $("key-create-error").classList.remove("hidden");
    } finally {
      if (requestId === keys.mutationRequestId && state.role === "admin") {
        keys.creating = false;
        $("key-list").setAttribute("aria-busy", String(keys.loading));
        syncKeyControls();
        renderKeyList();
      }
    }
  }

  function requestKeyRevoke(keyId) {
    const keys = state.keys;
    if (state.role !== "admin" || state.activeTab !== "keys" || keys.loading || keys.creating || keys.revokingKeyId) return;
    const record = keys.items.find((item) => item.key_id === keyId);
    if (!record || record.status === "revoked") return;
    keys.confirmKeyId = keyId;
    keys.revokeError = "";
    renderKeyList();
    $("key-revoke-commit-" + keyId).focus();
  }

  function cancelKeyRevoke({ restoreFocus = true } = {}) {
    const keys = state.keys;
    const keyId = keys.confirmKeyId;
    if (!keyId || keys.revokingKeyId) return;
    keys.confirmKeyId = null;
    keys.revokeError = "";
    renderKeyList();
    if (restoreFocus && state.activeTab === "keys") {
      const trigger = $("key-revoke-trigger-" + keyId);
      if (trigger) trigger.focus();
    }
  }

  async function revokeKey(keyId) {
    const keys = state.keys;
    if (state.role !== "admin" || keys.confirmKeyId !== keyId || keys.creating || keys.revokingKeyId) return;
    const record = keys.items.find((item) => item.key_id === keyId);
    if (!record || record.status === "revoked") return;
    const requestId = ++keys.mutationRequestId;
    keys.requestId += 1;
    if (keys.controller) keys.controller.abort();
    keys.controller = null;
    keys.loading = false;
    keys.revokingKeyId = keyId;
    keys.revokeError = "";
    let completed = false;
    $("key-list").setAttribute("aria-busy", "true");
    syncKeyControls();
    renderKeyList();
    try {
      const res = await api("/api/keys/" + encodeURIComponent(keyId) + "/revoke", { method: "POST" });
      if (requestId !== keys.mutationRequestId || state.role !== "admin") return;
      if (!res.key || res.key.key_id !== keyId || res.key.status !== "revoked") throw new Error("吊销响应不完整，请刷新密钥列表核对结果");
      const index = keys.items.findIndex((item) => item.key_id === keyId);
      if (index !== -1) keys.items[index] = res.key;
      keys.confirmKeyId = null;
      completed = true;
      if (keys.secretKeyId === keyId) clearKeySecret();
      if (state.keyId === keyId) {
        showGate();
        $("auth-error").textContent = "当前登录密钥已吊销，请使用其他密钥或微信重新登录。";
        $("auth-error").classList.remove("hidden");
        return;
      }
      $("keys-loading").textContent = "密钥已吊销，使用此密钥的客户端已失去访问权限。";
      refreshLogs();
    } catch (err) {
      if (requestId !== keys.mutationRequestId) return;
      if (err.status === 401) {
        showGate();
        $("auth-error").textContent = "登录已失效，请重新登录。";
        $("auth-error").classList.remove("hidden");
        return;
      }
      keys.revokeError = "吊销失败：" + err.message + "。可重试或取消。";
    } finally {
      if (requestId === keys.mutationRequestId && state.role === "admin") {
        keys.revokingKeyId = null;
        $("key-list").setAttribute("aria-busy", "false");
        syncKeyControls();
        renderKeyList();
        if (state.activeTab === "keys") {
          const target = completed ? $("btn-keys-refresh") : $("key-revoke-commit-" + keyId);
          if (target) target.focus();
        }
      }
    }
  }

  function buildClientConfig(client = state.keys.client) {
    const origin = window.location.origin;
    const url = origin + "/mcp";
    const token = state.keys.secret || "${TOPIC_MEMORY_TOKEN}";
    const hasSecret = Boolean(state.keys.secret);
    switch (client) {
      case "hermes":
        // https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/
        return {
          text: ["mcp_servers:", "  topic_memory:", "    url: " + JSON.stringify(url), "    headers:", "      Authorization: " + JSON.stringify("Bearer " + token)].join("\n"),
          instructions: "合并到 ~/.hermes/config.yaml 的 mcp_servers，重启 Hermes。${TOPIC_MEMORY_TOKEN} 会按环境变量展开。",
        };
      case "omp":
        // https://github.com/can1357/oh-my-pi/blob/main/docs/mcp-config.md
        return {
          text: JSON.stringify({ mcpServers: { topic_memory: { type: "http", url, headers: { Authorization: "Bearer " + token } } } }, null, 2),
          instructions: "合并到 ~/.omp/agent/mcp.json 或项目 .omp/mcp.json；原生 HTTP，无需桥接。${TOPIC_MEMORY_TOKEN} 会按环境变量展开。独立 Pi 使用 ~/.pi/agent/mcp.json 或 .pi/mcp.json。",
        };
      case "opencode":
        // https://opencode.ai/docs/mcp-servers/ — mcp.servers is also supported by released 1.19.6.
        return {
          text: JSON.stringify({ mcp: { servers: { topic_memory: { type: "remote", url, oauth: false, headers: { Authorization: "Bearer " + (hasSecret ? token : "{env:TOPIC_MEMORY_TOKEN}") } } } } }, null, 2),
          instructions: "合并到 opencode.json 的 mcp.servers；关闭 OAuth，使用 Bearer 密钥。OpenCode 原生环境变量语法是 {env:TOPIC_MEMORY_TOKEN}，不是 ${TOPIC_MEMORY_TOKEN}。",
        };
      case "codex":
        // https://developers.openai.com/codex/mcp — static headers are literal, env settings name raw-token variables.
        return {
          text: ["[mcp_servers.topic_memory]", "url = " + JSON.stringify(url), hasSecret ? "http_headers = { Authorization = " + JSON.stringify("Bearer " + token) + " }" : 'bearer_token_env_var = "TOPIC_MEMORY_TOKEN"'].join("\n"),
          instructions: hasSecret
            ? "合并到 ~/.codex/config.toml。本次明文通过 http_headers 传递；不要提交到版本库。长期使用可改为 bearer_token_env_var = \"TOPIC_MEMORY_TOKEN\"。"
            : "合并到 ~/.codex/config.toml，并设置 TOPIC_MEMORY_TOKEN 环境变量为原始密钥（不要带 Bearer 前缀）。Codex 不展开静态 ${TOPIC_MEMORY_TOKEN}，此配置使用原生 bearer_token_env_var。",
        };
      case "curl":
        return {
          text: ["curl --fail-with-body --silent --show-error \\", "  " + JSON.stringify(origin + "/api/v1/topics") + " \\", "  -H " + JSON.stringify("Authorization: Bearer " + token)].join("\n"),
          instructions: hasSecret
            ? "在终端运行，列出这把密钥可见的主题。命令包含本次明文密钥，注意终端历史与共享屏幕。"
            : "设置 TOPIC_MEMORY_TOKEN 环境变量后在终端运行；shell 会展开 ${TOPIC_MEMORY_TOKEN}，返回这把密钥可见的主题。",
        };
      default:
        throw new Error("不支持的接入客户端");
    }
  }

  function renderIntegration() {
    const config = buildClientConfig();
    $("integration-config").textContent = config.text;
    $("integration-instructions").textContent = config.instructions;
    $("integration-token-note").textContent = state.keys.secret
      ? "当前配置含本次新建的明文密钥。关闭上方卡片或退出后将恢复 ${TOPIC_MEMORY_TOKEN} 占位方案；请勿提交到版本库。"
      : "当前配置使用 ${TOPIC_MEMORY_TOKEN} 占位方案，不包含你的登录密钥。按客户端说明设置环境变量，再复制配置。";
  }

  function switchClient(client, { focus = false } = {}) {
    if (!["hermes", "omp", "opencode", "codex", "curl"].includes(client)) return;
    state.keys.client = client;
    document.querySelectorAll("#integration-clients [role=tab]").forEach((tab) => {
      const active = tab.getAttribute("data-client") === client;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    $("integration-config-panel").setAttribute("aria-labelledby", "client-" + client);
    renderIntegration();
    if (focus) $("client-" + client).focus();
  }

  function showGate() {
    state.token = "";
    state.role = null;
    state.actor = "";
    state.keyId = null;
    state.authMode = null;
    clearKeySession();
    $("app-shell").classList.add("hidden");
    $("auth-gate").classList.remove("hidden");
  }

  function showShell() {
    $("auth-gate").classList.add("hidden");
    $("app-shell").classList.remove("hidden");
  }

  async function refreshAll() {
    const requestId = ++state.overviewRequestId;
    const [overview, topicsRes, runtime] = await Promise.all([
      api("/api/overview"),
      api("/api/v1/topics"),
      api("/api/runtime").catch(() => null),
    ]);
    if (requestId !== state.overviewRequestId) return false;
    if (runtime) {
      $("runtime-model-badge").textContent = "模型: " + (runtime.reader_model || "-");
      const endpoint = endpointLabel(runtime.reader_endpoint);
      $("runtime-endpoint-badge").textContent = endpoint;
      $("runtime-endpoint-badge").title = "Endpoint: " + endpoint
        + (runtime.allowed_openids_count ? " · 白名单已锁 (" + runtime.allowed_openids_count + "人)" : "");
    } else {
      $("runtime-model-badge").textContent = "模型信息暂不可用";
      $("runtime-endpoint-badge").textContent = "刷新后重试";
      $("runtime-endpoint-badge").title = "模型信息加载失败，请刷新全部数据重试";
    }
    const ov = overview.overview || overview;
    const kpis = ov.kpis || ov;
    const topics = topicsRes.topics || [];
    state.topics = topics;
    renderKeyTopicOptions();
    state.charsMap = {};
    let fallbackChars = 0;
    for (const ct of ov.topics || []) {
      state.charsMap[ct.topic_id] = ct.chars || 0;
      fallbackChars += ct.chars || 0;
    }
    const totalChars = kpis.total_chars ?? fallbackChars;

    $("stat-topics").textContent = String(kpis.active_topic_count ?? topics.length);
    $("stat-cache").textContent = String(kpis.active_cache_count ?? 0);
    $("stat-queries").textContent = String(kpis.questions_last_30_days ?? 0);
    $("stat-chars").textContent = totalChars >= 10000
      ? (totalChars / 10000).toFixed(1) + " 万"
      : String(totalChars);

    renderTopicList();
    const nextTopic = topics.find((topic) => topic.topic_id === state.selectedId) || topics[0];
    if (!nextTopic) {
      state.selectedId = null;
      state.currentBody = "";
      state.topicRequestId += 1;
      clearDreamConfirmation();
      $("dream-box").classList.add("hidden");
      $("remember-drawer").classList.add("hidden");
      $("btn-toggle-remember").setAttribute("aria-expanded", "false");
      $("detail-title").textContent = "请选择左侧主题";
      $("detail-summary").textContent = "暂无主题。可由 Agent 创建主题后刷新。";
      $("detail-version-pill").classList.add("hidden");
      clearNode($("detail-meta"));
      clearNode($("version-select"));
      $("version-select").disabled = true;
      $("detail-pretty").textContent = "主题创建后，可在这里查看记忆。";
      $("detail-raw").textContent = "";
      $("btn-copy-body").disabled = true;
      updateTopicActions();
    }
    const [detailReady, logsReady, reportsReady, keysReady] = await Promise.all([
      nextTopic ? selectTopic(nextTopic.topic_id) : true,
      refreshLogs(),
      $("dream-reports-view").open ? refreshDreamReports() : true,
      state.activeTab === "keys" && state.role === "admin" ? refreshKeys() : true,
    ]);
    return requestId === state.overviewRequestId && runtime !== null && detailReady && logsReady && reportsReady && keysReady;
  }

  function renderTopicList() {
    const q = ($("topic-search").value || "").trim().toLowerCase();
    const list = $("topic-list");
    clearNode(list);

    let items = state.topics.filter((t) => {
      if (state.filter === "bound" && !(t.chat_ids && t.chat_ids.length)) return false;
      if (!q) return true;
      const hay = (t.display_name + " " + t.topic_id + " " + (t.summary || "")).toLowerCase();
      return hay.includes(q);
    });
    if (state.filter === "largest") {
      items.sort((a, b) => (state.charsMap[b.topic_id] || 0) - (state.charsMap[a.topic_id] || 0));
    }
    $("sidebar-count").textContent = String(items.length);
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "muted small";
      empty.textContent = q || state.filter !== "all"
        ? "没有匹配主题。清空搜索或切换到“全部”。"
        : "暂无主题。可由 Agent 写入或创建主题后刷新。";
      list.appendChild(empty);
    }

    for (const t of items) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "topic-item" + (t.topic_id === state.selectedId ? " active" : "");
      btn.setAttribute("aria-pressed", String(t.topic_id === state.selectedId));

      const top = document.createElement("div");
      top.className = "topic-item-top";
      const name = document.createElement("span");
      name.className = "topic-item-name";
      name.textContent = t.display_name || t.topic_id;
      top.appendChild(name);
      const chars = state.charsMap[t.topic_id];
      top.appendChild(makePill(chars !== undefined ? fmtChars(chars) : ("v" + t.version), "pill-gray"));
      btn.appendChild(top);

      const sub = document.createElement("div");
      sub.className = "topic-item-sub";
      sub.textContent = t.summary || t.topic_id;
      btn.appendChild(sub);

      btn.addEventListener("click", () => {
        switchTab("topic");
        selectTopic(t.topic_id);
      });
      list.appendChild(btn);
    }
  }

  async function selectTopic(topicId) {
    const requestId = ++state.topicRequestId;
    const changed = state.selectedId !== topicId;
    state.selectedId = topicId;
    state.currentBody = "";
    clearDreamConfirmation();
    renderTopicList();
    if (changed) {
      $("dream-box").classList.add("hidden");
      $("remember-input").value = "";
    }
    $("remember-drawer").classList.add("hidden");
    $("btn-toggle-remember").setAttribute("aria-expanded", "false");
    updateTopicActions();
    $("btn-copy-body").disabled = true;
    $("version-select").disabled = true;
    clearNode($("version-select"));
    $("panel-topic").setAttribute("aria-busy", "true");

    const topic = state.topics.find((item) => item.topic_id === topicId);
    $("detail-title").textContent = topic ? (topic.display_name || topic.topic_id) : topicId;
    const vPill = $("detail-version-pill");
    vPill.textContent = "v" + (topic ? topic.version : 1);
    vPill.classList.remove("hidden");
    $("detail-summary").textContent = (topic && topic.summary) ? topic.summary : "暂无目录摘要（运行一次 Dream 将自动生成）";

    const meta = $("detail-meta");
    clearNode(meta);
    if (topic) {
      meta.appendChild(makePill(topic.topic_id, "pill-mono"));
      if ((topic.chat_ids || []).length) meta.appendChild(makePill("飞书群 ×" + topic.chat_ids.length, "pill-green"));
      if ((topic.repo_urls || []).length) meta.appendChild(makePill("Repo ×" + topic.repo_urls.length, "pill-purple"));
      if ((topic.workspace_paths || []).length) meta.appendChild(makePill("目录 ×" + topic.workspace_paths.length, "pill-amber"));
    }

    $("detail-pretty").textContent = "加载中...";
    $("detail-raw").textContent = "加载中...";
    try {
      const [bodyRes, versRes] = await Promise.all([
        api("/api/topics/" + encodeURIComponent(topicId) + "/body"),
        api("/api/topics/" + encodeURIComponent(topicId) + "/versions?page_size=20").catch(() => null),
      ]);
      if (requestId !== state.topicRequestId || state.selectedId !== topicId) return false;
      state.currentBody = bodyRes.body || "";
      renderPrettyMarkdown($("detail-pretty"), state.currentBody);
      $("detail-raw").textContent = state.currentBody;
      $("btn-copy-body").disabled = false;
      meta.appendChild(makePill(fmtChars(state.currentBody.length), "pill-blue"));

      const sel = $("version-select");
      const currentOpt = document.createElement("option");
      currentOpt.value = "current";
      currentOpt.textContent = "当前版本 v" + (topic ? topic.version : 1);
      sel.appendChild(currentOpt);
      if (versRes) {
        for (const version of versRes.items || []) {
          if (topic && version.version === topic.version) continue;
          const opt = document.createElement("option");
          opt.value = String(version.version);
          opt.textContent = "历史快照 v" + version.version;
          sel.appendChild(opt);
        }
      } else {
        meta.appendChild(makePill("历史版本暂不可用，刷新后重试", "pill-red"));
      }
      sel.title = versRes ? "查看历史版本" : "历史版本加载失败，请刷新全部数据重试";
      sel.disabled = sel.options.length <= 1;
      return Boolean(versRes);
    } catch (err) {
      if (requestId !== state.topicRequestId || state.selectedId !== topicId) return false;
      $("detail-pretty").textContent = "读取失败: " + err.message + "。请刷新全部数据重试。";
      $("detail-raw").textContent = $("detail-pretty").textContent;
      return false;
    } finally {
      if (requestId === state.topicRequestId) $("panel-topic").setAttribute("aria-busy", "false");
    }
  }

  function requestDreamApply(triggerId) {
    if (!state.selectedId || state.dreamBusy || (state.role !== "write" && state.role !== "admin")) return;
    clearDreamConfirmation();
    $("remember-drawer").classList.add("hidden");
    $("btn-toggle-remember").setAttribute("aria-expanded", "false");
    const topic = state.topics.find((item) => item.topic_id === state.selectedId);
    const name = topic ? (topic.display_name || topic.topic_id) : state.selectedId;
    state.dreamConfirmTopicId = state.selectedId;
    state.dreamConfirmTrigger = triggerId;
    $("dream-confirm-topic").textContent = name + "（" + state.selectedId + "）";
    $("dream-apply-confirm").classList.remove("hidden");
    $("btn-dream-apply").setAttribute("aria-expanded", "true");
    $("btn-dream-commit-inline").setAttribute("aria-expanded", "true");
    updateTopicActions();
    $("btn-dream-confirm").focus();
  }

  function clearDreamConfirmation({ restoreFocus = false } = {}) {
    const triggerId = state.dreamConfirmTrigger;
    state.dreamConfirmTopicId = null;
    state.dreamConfirmTrigger = null;
    $("dream-confirm-topic").textContent = "";
    $("dream-apply-confirm").classList.add("hidden");
    $("btn-dream-apply").setAttribute("aria-expanded", "false");
    $("btn-dream-commit-inline").setAttribute("aria-expanded", "false");
    updateTopicActions();
    if (restoreFocus && triggerId) $(triggerId).focus();
  }

  async function runDream(dryRun, topicId = state.selectedId) {
    if (!topicId || state.dreamBusy || (state.role !== "write" && state.role !== "admin") || (!dryRun && topicId !== state.selectedId)) return;
    state.dreamBusy = true;
    clearDreamConfirmation();
    $("dream-box").classList.remove("hidden");
    $("dream-spinner").classList.remove("hidden");
    $("btn-dream-commit-inline").classList.add("hidden");
    $("dream-bar-wrap").classList.add("hidden");
    $("dream-status-title").textContent = dryRun ? "正在运行 Dream 巩固预览（不改库）..." : "正在运行 Dream 并原子换版...";
    clearNode($("dream-metrics"));
    clearNode($("dream-changes"));
    clearNode($("dream-diff"));
    try {
      const res = await api("/api/v1/dream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic: topicId, dry_run: dryRun, force: true }),
      });
      if (state.selectedId === topicId) {
        $("dream-status-title").textContent = res.status === "dry_run"
          ? "Dream 预览完成（尚未写入）"
          : res.status === "consolidated"
          ? "Dream 已完成原子换版（旧版已归档可回滚）"
          : "Dream 状态: " + (dreamStatusLabels[res.status] || "未知状态");
        const metrics = $("dream-metrics");
        const pct = res.before_chars ? Math.max(0, Math.round((1 - res.after_chars / res.before_chars) * 100)) : 0;
        metrics.appendChild(makePill(res.before_chars + " → " + res.after_chars + " 字（压缩 " + pct + "%）", "pill-purple"));
        metrics.appendChild(makePill("剪噪 " + (res.pruned_count || 0), "pill-gray"));
        metrics.appendChild(makePill("合并 " + (res.merged_count || 0), "pill-green"));
        metrics.appendChild(makePill("解冲突 " + (res.conflicts_resolved || 0), "pill-blue"));
        if (pct > 0) {
          $("dream-bar-wrap").classList.remove("hidden");
          $("dream-progress").value = Math.min(100, pct);
        }
        if (dryRun && res.status === "dry_run") $("btn-dream-commit-inline").classList.remove("hidden");
        for (const change of res.changes || []) {
          const li = document.createElement("li");
          li.textContent = change;
          $("dream-changes").appendChild(li);
        }
        renderDiff($("dream-diff"), res.diff || "");
      }
      if (!dryRun && res.status === "consolidated") {
        toast("主题 " + topicId + " Dream 换版成功，QA 缓存已自动刷新");
        await refreshAll();
      } else {
        await Promise.all([refreshLogs(), $("dream-reports-view").open ? refreshDreamReports() : true]);
      }
    } catch (err) {
      if (state.selectedId === topicId) $("dream-status-title").textContent = "Dream 失败: " + err.message;
      await Promise.all([refreshLogs(), $("dream-reports-view").open ? refreshDreamReports() : true]);
    } finally {
      state.dreamBusy = false;
      $("dream-spinner").classList.add("hidden");
      updateTopicActions();
    }
  }

  function appendActivities(items) {
    const list = $("activity-list");
    for (const record of items) {
      if (record.event_id && state.activities.seenIds.has(record.event_id)) continue;
      if (record.event_id) state.activities.seenIds.add(record.event_id);
      state.activities.items.push(record);
      const entry = document.createElement("li");
      entry.className = "activity-entry";
      const heading = document.createElement("div");
      heading.className = "activity-entry-head";
      heading.appendChild(makeTimestamp(record.created_at));
      heading.appendChild(makePill(activityLabels.action[record.action] || "未知操作", "pill-purple"));
      heading.appendChild(makePill(activityLabels.status[record.status] || "未知状态", activityStatusClasses[record.status] || "pill-gray"));
      const meta = document.createElement("div");
      meta.className = "activity-meta";
      const actor = document.createElement("span");
      actor.className = "activity-actor";
      actor.textContent = "操作者: " + (record.actor || "anonymous");
      meta.appendChild(actor);
      meta.appendChild(makePill("来源: " + (activityLabels.source[record.source] || "未知来源")));
      if (record.topic_id) meta.appendChild(makeTopicJump(record.topic_id));
      else meta.appendChild(makePill("全局操作"));
      meta.appendChild(makePill((record.latency_ms || 0) + " ms", "pill-mono"));
      const summary = document.createElement("p");
      summary.className = "activity-summary";
      summary.textContent = record.summary || "无补充说明。";
      entry.appendChild(heading);
      entry.appendChild(meta);
      entry.appendChild(summary);
      list.appendChild(entry);
    }
  }

  async function refreshLogs({ reset = true } = {}) {
    const activity = state.activities;
    const requestId = ++activity.requestId;
    if (activity.controller) activity.controller.abort();
    const controller = new AbortController();
    activity.controller = controller;
    activity.loading = true;
    activity.failedReset = reset;
    const page = reset ? 1 : activity.page + 1;
    if (reset) {
      activity.page = 0;
      activity.total = 0;
      activity.items.length = 0;
      activity.seenIds.clear();
      clearNode($("activity-list"));
      $("activity-count").textContent = "0 条";
      $("activity-page-status").textContent = "显示 0 条";
      $("btn-activity-more").classList.add("hidden");
    }
    $("activity-list").setAttribute("aria-busy", "true");
    $("activity-loading").textContent = reset ? "正在加载运行流水…" : "正在加载更多流水…";
    $("activity-empty").classList.add("hidden");
    $("btn-activity-retry").disabled = true;
    $("btn-activity-more").disabled = true;
    const params = new URLSearchParams({ page: String(page), page_size: String(activity.pageSize) });
    for (const [key, value] of Object.entries(activity.filters)) {
      if (value) params.set(key, value);
    }
    try {
      const res = await api("/api/activities?" + params.toString(), { signal: controller.signal });
      if (requestId !== state.activities.requestId) return false;
      if (!Array.isArray(res.items)) throw new Error("流水数据格式异常，请重试");
      activity.page = res.page || page;
      activity.total = res.total ?? res.items.length;
      appendActivities(res.items);
      $("activity-count").textContent = activity.total + " 条";
      $("activity-page-status").textContent = "显示 " + activity.items.length + " / " + activity.total + " 条";
      $("activity-empty").classList.toggle("hidden", activity.items.length !== 0);
      $("btn-activity-more").classList.toggle("hidden", activity.page * activity.pageSize >= activity.total);
      $("activity-error").classList.add("hidden");
      $("activity-loading").textContent = reset ? "最新流水已加载。" : "下一页流水已加载。";
      return true;
    } catch (err) {
      if (requestId !== state.activities.requestId || err.name === "AbortError") return false;
      $("activity-error-message").textContent = "流水加载失败：" + err.message;
      $("activity-error").classList.remove("hidden");
      $("activity-loading").textContent = "";
      return false;
    } finally {
      if (requestId === state.activities.requestId) {
        activity.loading = false;
        activity.controller = null;
        $("activity-list").setAttribute("aria-busy", "false");
        $("btn-activity-retry").disabled = false;
        $("btn-activity-more").disabled = false;
      }
    }
  }

  function applyActivityFilters() {
    state.activities.filters = {
      action: $("activity-action").value,
      source: $("activity-source").value,
      status: $("activity-status").value,
      q: $("activity-search").value.trim(),
    };
    $("activity-search").value = state.activities.filters.q;
    return refreshLogs();
  }

  function renderDreamReports(items) {
    const list = $("log-dreams");
    clearNode(list);
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "muted small";
      empty.textContent = "暂无 Dream 报告。可在主题页运行一次预览。";
      list.appendChild(empty);
    }
    for (const report of items) {
      const entry = document.createElement("article");
      entry.className = "log-entry";
      entry.setAttribute("aria-label", "Dream 报告: " + (report.topic_id || "全局"));
      const heading = document.createElement("div");
      heading.className = "activity-entry-head";
      heading.appendChild(makeTimestamp(report.created_at));
      if (report.topic_id) heading.appendChild(makeTopicJump(report.topic_id));
      heading.appendChild(makePill(dreamStatusLabels[report.status] || "未知状态", report.status === "error" ? "pill-red" : "pill-purple"));
      const metrics = document.createElement("p");
      metrics.className = "log-entry-a";
      metrics.textContent = "v" + (report.before_version || 0) + " → v" + (report.after_version || 0)
        + " · " + (report.before_chars || 0) + " → " + (report.after_chars || 0) + " 字"
        + " · 证据 " + (report.evidence_count || 0) + " · 剪噪 " + (report.pruned_count || 0)
        + " · 合并 " + (report.merged_count || 0) + " · 解冲突 " + (report.conflicts_resolved || 0);
      const detail = document.createElement("details");
      detail.className = "dream-report";
      const toggle = document.createElement("summary");
      toggle.textContent = "查看变更说明与正文差异";
      const content = document.createElement("div");
      content.className = "dream-report-content";
      const summary = document.createElement("p");
      summary.className = "activity-summary";
      summary.textContent = report.summary || (report.status === "error" ? "本次 Dream 失败，可在主题页重试。" : "无补充说明。");
      content.appendChild(summary);
      const changes = document.createElement("ul");
      changes.className = "change-list";
      for (const change of report.changes || []) {
        const li = document.createElement("li");
        li.textContent = change;
        changes.appendChild(li);
      }
      content.appendChild(changes);
      const diff = document.createElement("div");
      diff.className = "diff-view";
      diff.setAttribute("role", "region");
      diff.setAttribute("aria-label", "主题 " + (report.topic_id || "未知") + " Dream 正文差异");
      diff.tabIndex = 0;
      renderDiff(diff, report.diff || "");
      content.appendChild(diff);
      detail.appendChild(toggle);
      detail.appendChild(content);
      entry.appendChild(heading);
      entry.appendChild(metrics);
      entry.appendChild(detail);
      list.appendChild(entry);
    }
  }

  async function refreshDreamReports() {
    const requestId = ++state.dreamReportsRequestId;
    if (state.dreamReportsController) state.dreamReportsController.abort();
    const controller = new AbortController();
    state.dreamReportsController = controller;
    $("dream-reports-loading").textContent = "正在加载 Dream 详细报告…";
    $("log-dreams").setAttribute("aria-busy", "true");
    $("btn-dream-reports-refresh").disabled = true;
    $("btn-dream-reports-retry").disabled = true;
    try {
      const res = await api("/api/dreams?limit=20", { signal: controller.signal });
      if (requestId !== state.dreamReportsRequestId) return false;
      if (!Array.isArray(res.items)) throw new Error("Dream 报告格式异常，请重试");
      $("log-d-count").textContent = String(res.items.length);
      renderDreamReports(res.items);
      $("dream-reports-error").classList.add("hidden");
      $("dream-reports-loading").textContent = "Dream 详细报告已加载。";
      return true;
    } catch (err) {
      if (requestId !== state.dreamReportsRequestId || err.name === "AbortError") return false;
      $("dream-reports-error-message").textContent = "Dream 报告加载失败：" + err.message;
      $("dream-reports-error").classList.remove("hidden");
      $("dream-reports-loading").textContent = "";
      return false;
    } finally {
      if (requestId === state.dreamReportsRequestId) {
        state.dreamReportsController = null;
        $("log-dreams").setAttribute("aria-busy", "false");
        $("btn-dream-reports-refresh").disabled = false;
        $("btn-dream-reports-retry").disabled = false;
      }
    }
  }

  async function submitQuestion(q) {
    if (!q || $("btn-ask-submit").disabled) return;
    $("btn-ask-submit").disabled = true;
    const thread = $("ask-thread");
    const item = document.createElement("div");
    item.className = "ask-item";
    const qEl = document.createElement("div");
    qEl.className = "ask-item-q";
    qEl.textContent = q;
    const meta = document.createElement("div");
    meta.className = "meta-row";
    const cap = document.createElement("div");
    cap.className = "capsule-box";
    cap.setAttribute("role", "status");
    cap.setAttribute("aria-live", "polite");
    cap.textContent = "正在自动扫描主题目录并读取记忆...";
    item.appendChild(qEl);
    item.appendChild(meta);
    item.appendChild(cap);
    thread.prepend(item);

    const t0 = performance.now();
    try {
      const res = await api("/api/v1/recall", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q }),
      });
      const ms = Math.round(performance.now() - t0);
      if (res.topic_id) meta.appendChild(makeTopicJump(res.topic_id, "命中: "));
      meta.appendChild(makePill("路由: " + (res.route_source || "-"), "pill-purple"));
      meta.appendChild(makePill("来源: " + (res.source || "-"), "pill-green"));
      meta.appendChild(makePill(ms + " ms", "pill-gray"));
      cap.textContent = res.capsule || "（当前记忆库中没有找到支持回答该问题的事实）";
      await refreshLogs();
    } catch (err) {
      cap.textContent = "查询失败: " + err.message;
    } finally {
      $("btn-ask-submit").disabled = false;
    }
  }

  // Event wiring
  $("toast-close").addEventListener("click", dismissToast);
  $("token-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("auth-error").classList.add("hidden");
    const button = e.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true;
    state.token = ($("token-input").value || "").trim();
    try {
      const me = await api("/api/auth/me");
      if (!me.authenticated) throw new Error("密钥未获授权，请检查后重试");
      applyAuth(me);
      $("token-input").value = "";
      showShell();
    } catch (err) {
      state.token = "";
      $("auth-error").textContent = "密钥校验失败：" + err.message;
      $("auth-error").classList.remove("hidden");
      return;
    } finally {
      button.disabled = false;
    }
    try {
      if (!await refreshAll()) toast("部分数据未能加载，请查看错误提示并刷新重试", { error: true });
    } catch (err) {
      toast("数据加载失败：" + err.message, { error: true });
    }
  });

  $("btn-logout").addEventListener("click", async () => {
    state.token = "";
    const mode = state.authMode;
    state.role = null;
    state.actor = "";
    state.keyId = null;
    state.authMode = null;
    $("token-input").value = "";
    clearKeySession();
    if (mode === "sso") {
      try { await api("/api/sso/logout", { method: "POST" }); } catch (_) {}
    }
    window.location.href = "/?token_login=1";
  });

  $("btn-refresh").addEventListener("click", async () => {
    $("btn-refresh").disabled = true;
    try {
      if (await refreshAll()) toast("已刷新最新状态");
      else toast("部分数据未能刷新，请查看错误提示并重试", { error: true });
    } catch (err) {
      toast("刷新失败：" + err.message, { error: true });
    } finally {
      $("btn-refresh").disabled = false;
    }
  });

  $("topic-search").addEventListener("input", () => renderTopicList());
  document.querySelectorAll(".filter-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll(".filter-chip").forEach((item) => {
        item.classList.toggle("active", item === chip);
        item.setAttribute("aria-pressed", String(item === chip));
      });
      state.filter = chip.getAttribute("data-filter") || "all";
      renderTopicList();
    });
  });

  $("btn-view-pretty").addEventListener("click", () => updateViewMode("pretty"));
  $("btn-view-raw").addEventListener("click", () => updateViewMode("raw"));

  $("btn-copy-body").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(state.currentBody);
      toast("已复制全文到剪贴板");
    } catch (_) {
      toast("复制失败，请手动选择文本", { error: true });
    }
  });

  $("version-select").addEventListener("change", async (e) => {
    const val = e.target.value;
    const topicId = state.selectedId;
    if (!topicId) return;
    if (val === "current") {
      await selectTopic(topicId);
      return;
    }
    const requestId = ++state.topicRequestId;
    state.currentBody = "";
    $("btn-copy-body").disabled = true;
    $("detail-pretty").textContent = "正在读取历史快照…";
    $("detail-raw").textContent = "正在读取历史快照…";
    $("panel-topic").setAttribute("aria-busy", "true");
    try {
      const res = await api("/api/topics/" + encodeURIComponent(topicId) + "/versions/" + encodeURIComponent(val) + "/body");
      if (requestId !== state.topicRequestId || state.selectedId !== topicId) return;
      state.currentBody = res.body || "";
      renderPrettyMarkdown($("detail-pretty"), state.currentBody);
      $("detail-raw").textContent = state.currentBody;
      $("btn-copy-body").disabled = false;
      toast("正在查看历史快照 v" + val);
    } catch (err) {
      if (requestId !== state.topicRequestId || state.selectedId !== topicId) return;
      $("detail-pretty").textContent = "读取历史版本失败: " + err.message + "。可切回当前版本或刷新重试。";
      $("detail-raw").textContent = $("detail-pretty").textContent;
      toast("读取历史版本失败: " + err.message, { error: true });
    } finally {
      if (requestId === state.topicRequestId) $("panel-topic").setAttribute("aria-busy", "false");
    }
  });

  $("btn-toggle-remember").addEventListener("click", () => {
    if (state.role !== "write" && state.role !== "admin") return;
    clearDreamConfirmation();
    $("remember-drawer").classList.toggle("hidden");
    const expanded = !$("remember-drawer").classList.contains("hidden");
    $("btn-toggle-remember").setAttribute("aria-expanded", String(expanded));
    updateTopicActions();
    if (expanded) $("remember-input").focus();
  });
  $("btn-remember-cancel").addEventListener("click", () => {
    $("remember-drawer").classList.add("hidden");
    $("remember-input").value = "";
    $("btn-toggle-remember").setAttribute("aria-expanded", "false");
    updateTopicActions();
    $("btn-toggle-remember").focus();
  });
  $("remember-drawer").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fact = ($("remember-input").value || "").trim();
    const topicId = state.selectedId;
    if (!fact || !topicId || state.rememberBusy || (state.role !== "write" && state.role !== "admin")) return;
    state.rememberBusy = true;
    updateTopicActions();
    try {
      const res = await api("/api/v1/remember", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic: topicId, fact, source_agent: "studio-ui" }),
      });
      if (state.selectedId === topicId) {
        $("remember-input").value = "";
        $("remember-drawer").classList.add("hidden");
        $("btn-toggle-remember").setAttribute("aria-expanded", "false");
        updateTopicActions();
      }
      toast("已写入主题 " + topicId + " 的记忆（v" + res.topic_version + "）");
      await refreshAll();
    } catch (err) {
      toast("写入失败: " + err.message, { error: true });
    } finally {
      state.rememberBusy = false;
      updateTopicActions();
    }
  });

  $("btn-dream-preview").addEventListener("click", () => runDream(true));
  $("btn-dream-apply").addEventListener("click", () => requestDreamApply("btn-dream-apply"));
  $("btn-dream-commit-inline").addEventListener("click", () => requestDreamApply("btn-dream-commit-inline"));
  $("btn-dream-confirm").addEventListener("click", () => {
    const topicId = state.dreamConfirmTopicId;
    if (!topicId || topicId !== state.selectedId) {
      clearDreamConfirmation();
      return;
    }
    clearDreamConfirmation();
    runDream(false, topicId);
  });
  $("btn-dream-cancel").addEventListener("click", () => clearDreamConfirmation({ restoreFocus: true }));
  $("dream-apply-confirm").addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      clearDreamConfirmation({ restoreFocus: true });
    }
  });
  $("btn-dream-close").addEventListener("click", () => {
    clearDreamConfirmation();
    $("dream-box").classList.add("hidden");
  });

  const tabs = Array.from(document.querySelectorAll(".tab"));
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => switchTab(tab.getAttribute("data-tab")));
    tab.addEventListener("keydown", (e) => {
      let next;
      if (e.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (e.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
      else if (e.key === "Home") next = 0;
      else if (e.key === "End") next = tabs.length - 1;
      else return;
      e.preventDefault();
      switchTab(tabs[next].getAttribute("data-tab"), { focus: true });
    });
  });

  $("key-create-form").addEventListener("submit", (e) => {
    e.preventDefault();
    createKey();
  });
  for (const id of ["key-name", "key-role", "key-topics", "key-expiry"]) {
    $(id).addEventListener("input", clearKeyFormErrors);
    $(id).addEventListener("change", clearKeyFormErrors);
  }
  $("btn-keys-refresh").addEventListener("click", refreshKeys);
  $("btn-keys-retry").addEventListener("click", refreshKeys);
  $("btn-key-dismiss").addEventListener("click", () => {
    clearKeySecret({ restoreFocus: true });
    $("key-create-status").textContent = "明文已从页面清除，仍可创建新密钥。";
  });
  $("btn-key-copy").addEventListener("click", async () => {
    if (!state.keys.secret || $("key-secret-card").hidden) return;
    try {
      await navigator.clipboard.writeText(state.keys.secret);
      toast("密钥已复制，请存放在安全位置");
    } catch (_) {
      toast("复制密钥失败，请手动选择明文；不要在保存前关闭卡片", { error: true });
    }
  });
  $("btn-config-copy").addEventListener("click", async () => {
    const config = buildClientConfig();
    try {
      await navigator.clipboard.writeText(config.text);
      toast("已复制当前客户端配置");
    } catch (_) {
      toast("复制配置失败，请手动选择下方配置文本", { error: true });
    }
  });
  const clientTabs = Array.from(document.querySelectorAll("#integration-clients [role=tab]"));
  clientTabs.forEach((tab, index) => {
    tab.addEventListener("click", () => switchClient(tab.getAttribute("data-client")));
    tab.addEventListener("keydown", (e) => {
      let next;
      if (e.key === "ArrowRight") next = (index + 1) % clientTabs.length;
      else if (e.key === "ArrowLeft") next = (index - 1 + clientTabs.length) % clientTabs.length;
      else if (e.key === "Home") next = 0;
      else if (e.key === "End") next = clientTabs.length - 1;
      else return;
      e.preventDefault();
      switchClient(clientTabs[next].getAttribute("data-client"), { focus: true });
    });
  });

  $("activity-filters").addEventListener("submit", (e) => {
    e.preventDefault();
    applyActivityFilters();
  });
  ["activity-action", "activity-source", "activity-status"].forEach((id) => {
    $(id).addEventListener("change", applyActivityFilters);
  });
  $("btn-activity-reset").addEventListener("click", () => {
    $("activity-filters").reset();
    applyActivityFilters();
  });
  $("btn-activity-retry").addEventListener("click", () => refreshLogs({ reset: state.activities.failedReset }));
  $("btn-activity-more").addEventListener("click", () => {
    if (!state.activities.loading) refreshLogs({ reset: false });
  });
  $("dream-reports-view").addEventListener("toggle", () => {
    if ($("dream-reports-view").open) refreshDreamReports();
  });
  $("btn-dream-reports-refresh").addEventListener("click", refreshDreamReports);
  $("btn-dream-reports-retry").addEventListener("click", refreshDreamReports);

  document.querySelectorAll(".starter-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const q = chip.getAttribute("data-q") || "";
      $("ask-input").value = q;
      submitQuestion(q);
    });
  });

  $("ask-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = ($("ask-input").value || "").trim();
    if (!q) return;
    $("ask-input").value = "";
    await submitQuestion(q);
  });

  bootstrap();
})();
