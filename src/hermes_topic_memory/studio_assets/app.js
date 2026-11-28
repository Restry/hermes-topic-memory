(() => {
  "use strict";

  const state = {
    token: "",
    authMode: null,
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
    document.querySelectorAll(".tab").forEach((tab) => {
      const active = tab.getAttribute("data-tab") === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    ["topic", "ask", "logs"].forEach((nameOfPanel) => {
      const panel = $("panel-" + nameOfPanel);
      panel.classList.toggle("hidden", nameOfPanel !== name);
      panel.hidden = nameOfPanel !== name;
    });
    if (focus) $("tab-" + name).focus();
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
    state.authMode = me.mode;
    $("user-badge").textContent = me.mode === "sso"
      ? ((me.user && me.user.nickname) || "微信已登录")
      : "密钥模式";
    showShell();
    try {
      if (!await refreshAll()) toast("部分数据未能加载，请查看错误提示并刷新重试", { error: true });
    } catch (err) {
      toast("数据加载失败：" + err.message, { error: true });
    }
  }

  function showGate() {
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
      ["btn-toggle-remember", "btn-dream-preview", "btn-dream-apply", "btn-copy-body"].forEach((id) => { $(id).disabled = true; });
    }
    const [detailReady, logsReady, reportsReady] = await Promise.all([
      nextTopic ? selectTopic(nextTopic.topic_id) : true,
      refreshLogs(),
      $("dream-reports-view").open ? refreshDreamReports() : true,
    ]);
    return requestId === state.overviewRequestId && runtime !== null && detailReady && logsReady && reportsReady;
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
    $("btn-toggle-remember").disabled = false;
    $("btn-dream-preview").disabled = state.dreamBusy;
    $("btn-dream-apply").disabled = state.dreamBusy;
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
    if (!state.selectedId || state.dreamBusy) return;
    clearDreamConfirmation();
    const topic = state.topics.find((item) => item.topic_id === state.selectedId);
    const name = topic ? (topic.display_name || topic.topic_id) : state.selectedId;
    state.dreamConfirmTopicId = state.selectedId;
    state.dreamConfirmTrigger = triggerId;
    $("dream-confirm-topic").textContent = name + "（" + state.selectedId + "）";
    $("dream-apply-confirm").classList.remove("hidden");
    $("btn-dream-apply").setAttribute("aria-expanded", "true");
    $("btn-dream-commit-inline").setAttribute("aria-expanded", "true");
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
    if (restoreFocus && triggerId) $(triggerId).focus();
  }

  async function runDream(dryRun, topicId = state.selectedId) {
    if (!topicId || state.dreamBusy || (!dryRun && topicId !== state.selectedId)) return;
    state.dreamBusy = true;
    clearDreamConfirmation();
    $("btn-dream-preview").disabled = true;
    $("btn-dream-apply").disabled = true;
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
      $("btn-dream-preview").disabled = !state.selectedId;
      $("btn-dream-apply").disabled = !state.selectedId;
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
      await api("/api/overview");
      state.authMode = "token";
      $("token-input").value = "";
      $("user-badge").textContent = "密钥模式";
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
    if (state.authMode === "sso") {
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
    clearDreamConfirmation();
    $("remember-drawer").classList.toggle("hidden");
    const expanded = !$("remember-drawer").classList.contains("hidden");
    $("btn-toggle-remember").setAttribute("aria-expanded", String(expanded));
    if (expanded) $("remember-input").focus();
  });
  $("btn-remember-cancel").addEventListener("click", () => {
    $("remember-drawer").classList.add("hidden");
    $("remember-input").value = "";
    $("btn-toggle-remember").setAttribute("aria-expanded", "false");
    $("btn-toggle-remember").focus();
  });
  $("remember-drawer").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fact = ($("remember-input").value || "").trim();
    const topicId = state.selectedId;
    if (!fact || !topicId || $("btn-remember-submit").disabled) return;
    $("btn-remember-submit").disabled = true;
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
      }
      toast("已写入主题 " + topicId + " 的记忆（v" + res.topic_version + "）");
      await refreshAll();
    } catch (err) {
      toast("写入失败: " + err.message, { error: true });
    } finally {
      $("btn-remember-submit").disabled = false;
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
