(() => {
  "use strict";

  const state = {
    token: "",
    authMode: null,
    ssoEnabled: false,
    topics: [],
    selectedId: null,
  };

  const $ = (id) => document.getElementById(id);

  function headers(extra = {}) {
    const h = { ...extra };
    if (state.token) {
      h["Authorization"] = "Bearer " + state.token;
    }
    return h;
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      credentials: "same-origin",
      ...options,
      headers: headers(options.headers || {}),
    });
    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (_) { data = {}; }
    if (!res.ok) {
      const msg = (data && data.error && data.error.message) || ("HTTP " + res.status);
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function makeBadge(text, cls = "badge-gray") {
    const el = document.createElement("span");
    el.className = "badge " + cls;
    el.textContent = text;
    return el;
  }

  function clearNode(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  async function bootstrap() {
    try {
      const me = await api("/api/auth/me");
      state.ssoEnabled = Boolean(me.sso_enabled);
      if (state.ssoEnabled) {
        $("sso-entry-wrap").classList.remove("hidden");
      }
      if (me.authenticated) {
        state.authMode = me.mode;
        $("user-badge").textContent =
          me.mode === "sso"
            ? ((me.user && me.user.nickname) || "微信已登录")
            : "密钥模式";
        showShell();
        await refreshAll();
        return;
      }
    } catch (_) {}
    showGate();
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
    const [overview, topicsRes] = await Promise.all([
      api("/api/overview"),
      api("/api/v1/topics"),
    ]);
    const ov = overview.overview || overview;
    const topics = topicsRes.topics || [];
    state.topics = topics;

    $("stat-topics").textContent = String(ov.active_topic_count ?? topics.length);
    $("stat-cache").textContent = String(ov.active_cache_count ?? 0);
    $("stat-queries").textContent = String(ov.questions_last_30_days ?? 0);

    // Sum chars from catalog topics if available
    const totalChars = (ov.topics || []).reduce((acc, t) => acc + (t.chars || 0), 0);
    $("stat-chars").textContent = totalChars
      ? (totalChars >= 10000 ? (totalChars / 10000).toFixed(1) + " 万" : String(totalChars))
      : "-";

    renderTopicList();
    if (!state.selectedId && topics.length > 0) {
      await selectTopic(topics[0].topic_id);
    } else if (state.selectedId) {
      await selectTopic(state.selectedId);
    }
    await refreshLogs();
  }

  function renderTopicList() {
    const q = ($("topic-search").value || "").trim().toLowerCase();
    const list = $("topic-list");
    clearNode(list);
    for (const t of state.topics) {
      const hay = (t.display_name + " " + t.topic_id + " " + (t.summary || "")).toLowerCase();
      if (q && !hay.includes(q)) continue;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "topic-item" + (t.topic_id === state.selectedId ? " active" : "");

      const top = document.createElement("div");
      top.className = "topic-item-top";
      const name = document.createElement("span");
      name.className = "topic-item-name";
      name.textContent = t.display_name || t.topic_id;
      top.appendChild(name);
      top.appendChild(makeBadge("v" + t.version, "badge-soft"));
      btn.appendChild(top);

      const sub = document.createElement("div");
      sub.className = "topic-item-sub";
      sub.textContent = t.summary || t.topic_id;
      btn.appendChild(sub);

      btn.addEventListener("click", () => selectTopic(t.topic_id));
      list.appendChild(btn);
    }
  }

  async function selectTopic(topicId) {
    state.selectedId = topicId;
    renderTopicList();
    $("dream-box").classList.add("hidden");
    $("btn-dream-preview").disabled = false;
    $("btn-dream-apply").disabled = false;

    const topic = state.topics.find((t) => t.topic_id === topicId);
    $("detail-title").textContent = topic ? (topic.display_name || topic.topic_id) : topicId;
    $("detail-summary").textContent = (topic && topic.summary) ? topic.summary : "暂无目录摘要（运行一次 Dream 将自动生成）";

    const meta = $("detail-meta");
    clearNode(meta);
    if (topic) {
      meta.appendChild(makeBadge("ID: " + topic.topic_id, "badge-gray"));
      meta.appendChild(makeBadge("版本 v" + topic.version, "badge-soft"));
      if ((topic.chat_ids || []).length) meta.appendChild(makeBadge("飞书群 ×" + topic.chat_ids.length, "badge-green"));
      if ((topic.repo_urls || []).length) meta.appendChild(makeBadge("Repo ×" + topic.repo_urls.length, "badge-purple"));
      if ((topic.workspace_paths || []).length) meta.appendChild(makeBadge("目录 ×" + topic.workspace_paths.length, "badge-gray"));
    }

    $("detail-body").textContent = "加载中...";
    try {
      const bodyRes = await api("/api/topics/" + encodeURIComponent(topicId) + "/body");
      const text = bodyRes.body || "";
      $("detail-body").textContent = text;
      meta.appendChild(makeBadge(text.length + " 字", "badge-gray"));
    } catch (err) {
      $("detail-body").textContent = "读取失败: " + err.message;
    }
  }

  async function runDream(dryRun) {
    if (!state.selectedId) return;
    $("btn-dream-preview").disabled = true;
    $("btn-dream-apply").disabled = true;
    $("dream-box").classList.remove("hidden");
    $("dream-status-title").textContent = dryRun ? "正在运行 Dream 预览（不改库）..." : "正在运行 Dream 并原子换版...";
    clearNode($("dream-metrics"));
    clearNode($("dream-changes"));
    $("dream-diff").textContent = "";
    try {
      const res = await api("/api/v1/dream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic: state.selectedId, dry_run: dryRun, force: true }),
      });
      $("dream-status-title").textContent =
        res.status === "dry_run"
          ? "Dream 预览完成（尚未写入）"
          : res.status === "consolidated"
          ? "Dream 已完成原子换版"
          : "Dream 状态: " + res.status;

      const m = $("dream-metrics");
      const pct = res.before_chars ? Math.round((1 - res.after_chars / res.before_chars) * 100) : 0;
      m.appendChild(makeBadge(res.before_chars + " → " + res.after_chars + " 字（压缩 " + pct + "%）", "badge-purple"));
      m.appendChild(makeBadge("剪噪 " + (res.pruned_count || 0), "badge-gray"));
      m.appendChild(makeBadge("合并 " + (res.merged_count || 0), "badge-green"));
      m.appendChild(makeBadge("解冲突 " + (res.conflicts_resolved || 0), "badge-soft"));

      const ul = $("dream-changes");
      for (const c of res.changes || []) {
        const li = document.createElement("li");
        li.textContent = c;
        ul.appendChild(li);
      }
      $("dream-diff").textContent = res.diff || (res.error || "无正文变更。");
      if (!dryRun && res.status === "consolidated") {
        await refreshAll();
      } else {
        await refreshLogs();
      }
    } catch (err) {
      $("dream-status-title").textContent = "Dream 失败: " + err.message;
    } finally {
      $("btn-dream-preview").disabled = false;
      $("btn-dream-apply").disabled = false;
    }
  }

  async function refreshLogs() {
    try {
      const [qRes, dRes] = await Promise.all([
        api("/api/questions?limit=20"),
        api("/api/dreams?limit=20"),
      ]);
      const qWrap = $("log-questions");
      clearNode(qWrap);
      for (const q of qRes.items || []) {
        const card = document.createElement("div");
        card.className = "log-entry";
        const qt = document.createElement("div");
        qt.className = "log-entry-q";
        qt.textContent = q.question || "";
        const qa = document.createElement("div");
        qa.className = "log-entry-a";
        qa.textContent = q.final_answer || "(无事实)";
        const meta = document.createElement("div");
        meta.className = "meta-row";
        meta.appendChild(makeBadge(q.source || "-", "badge-gray"));
        meta.appendChild(makeBadge((q.total_latency_ms || 0) + " ms", "badge-soft"));
        card.appendChild(qt);
        card.appendChild(qa);
        card.appendChild(meta);
        qWrap.appendChild(card);
      }

      const dWrap = $("log-dreams");
      clearNode(dWrap);
      for (const d of dRes.items || []) {
        const card = document.createElement("div");
        card.className = "log-entry";
        const dt = document.createElement("div");
        dt.className = "log-entry-q";
        dt.textContent = (d.topic_id || "-") + " · " + (d.status || "-");
        const da = document.createElement("div");
        da.className = "log-entry-a";
        da.textContent = d.before_chars + " → " + d.after_chars + " 字 | 剪噪 " + (d.pruned_count || 0) + " · 合并 " + (d.merged_count || 0) + " · 解冲突 " + (d.conflicts_resolved || 0);
        card.appendChild(dt);
        card.appendChild(da);
        dWrap.appendChild(card);
      }
    } catch (_) {}
  }

  // Event wiring
  $("token-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("auth-error").classList.add("hidden");
    state.token = ($("token-input").value || "").trim();
    try {
      await api("/api/overview");
      state.authMode = "token";
      $("user-badge").textContent = "密钥模式";
      showShell();
      await refreshAll();
    } catch (err) {
      state.token = "";
      $("auth-error").textContent = "密钥校验失败：" + err.message;
      $("auth-error").classList.remove("hidden");
    }
  });

  $("btn-logout").addEventListener("click", async () => {
    state.token = "";
    if (state.authMode === "sso") {
      try { await api("/api/sso/logout", { method: "POST" }); } catch (_) {}
    }
    window.location.href = "/?token_login=1";
  });

  $("btn-refresh").addEventListener("click", () => refreshAll());
  $("topic-search").addEventListener("input", () => renderTopicList());
  $("btn-dream-preview").addEventListener("click", () => runDream(true));
  $("btn-dream-apply").addEventListener("click", () => runDream(false));
  $("btn-dream-close").addEventListener("click", () => $("dream-box").classList.add("hidden"));

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const name = tab.getAttribute("data-tab");
      ["topic", "ask", "logs"].forEach((p) => {
        $("panel-" + p).classList.toggle("hidden", p !== name);
      });
    });
  });

  $("ask-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = ($("ask-input").value || "").trim();
    if (!q) return;
    $("btn-ask-submit").disabled = true;
    $("ask-result").classList.remove("hidden");
    clearNode($("ask-meta"));
    $("ask-capsule").textContent = "正在自动匹配主题并读取记忆...";
    const t0 = performance.now();
    try {
      const res = await api("/api/v1/recall", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q }),
      });
      const ms = Math.round(performance.now() - t0);
      const m = $("ask-meta");
      m.appendChild(makeBadge("命中主题: " + (res.topic_id || "-"), "badge-soft"));
      m.appendChild(makeBadge("路由: " + (res.route_source || "-"), "badge-purple"));
      m.appendChild(makeBadge("来源: " + (res.source || "-"), "badge-green"));
      m.appendChild(makeBadge(ms + " ms", "badge-gray"));
      $("ask-capsule").textContent = res.capsule || "（当前记忆库中没有找到支持回答该问题的事实）";
      await refreshLogs();
    } catch (err) {
      $("ask-capsule").textContent = "查询失败: " + err.message;
    } finally {
      $("btn-ask-submit").disabled = false;
    }
  });

  bootstrap();
})();
