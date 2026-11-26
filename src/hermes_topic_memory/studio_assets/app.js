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
  };

  const $ = (id) => document.getElementById(id);
  let toastTimer = null;

  function toast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
  }

  function headers(extra = {}) {
    const h = { ...extra };
    if (state.token) h["Authorization"] = "Bearer " + state.token;
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

  function makePill(text, cls = "pill-gray") {
    const el = document.createElement("span");
    el.className = "pill " + cls;
    el.textContent = text;
    return el;
  }

  function clearNode(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function fmtChars(n) {
    if (!n) return "0 字";
    return n >= 10000 ? (n / 10000).toFixed(1) + "万字" : n + " 字";
  }

  // Safe DOM Markdown renderer (zero innerHTML!)
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

  function switchTab(name) {
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("active", t.getAttribute("data-tab") === name);
    });
    ["topic", "ask", "logs"].forEach((p) => {
      $("panel-" + p).classList.toggle("hidden", p !== name);
    });
  }

  function updateViewMode(mode) {
    state.viewMode = mode;
    $("btn-view-pretty").classList.toggle("active", mode === "pretty");
    $("btn-view-raw").classList.toggle("active", mode === "raw");
    $("detail-pretty").classList.toggle("hidden", mode !== "pretty");
    $("detail-raw").classList.toggle("hidden", mode !== "raw");
  }

  async function bootstrap() {
    try {
      const me = await api("/api/auth/me");
      state.ssoEnabled = Boolean(me.sso_enabled);
      if (state.ssoEnabled) $("sso-entry-wrap").classList.remove("hidden");
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
    const [overview, topicsRes, runtime] = await Promise.all([
      api("/api/overview"),
      api("/api/v1/topics"),
      api("/api/runtime").catch(() => null),
    ]);
    if (runtime) {
      $("runtime-model-badge").textContent = "模型: " + (runtime.reader_model || "-");
      const ep = runtime.reader_endpoint || "-";
      $("runtime-endpoint-badge").textContent = ep.replace(/^https?:\/\//, "");
      $("runtime-endpoint-badge").title =
        "Endpoint: " + ep + (runtime.allowed_openids_count ? " · 白名单已锁 (" + runtime.allowed_openids_count + "人)" : "");
    }
    const ov = overview.overview || overview;
    const topics = topicsRes.topics || [];
    state.topics = topics;
    state.charsMap = {};
    let totalChars = 0;
    for (const ct of ov.topics || []) {
      state.charsMap[ct.topic_id] = ct.chars || 0;
      totalChars += ct.chars || 0;
    }

    $("stat-topics").textContent = String(ov.active_topic_count ?? topics.length);
    $("stat-cache").textContent = String(ov.active_cache_count ?? 0);
    $("stat-queries").textContent = String(ov.questions_last_30_days ?? 0);
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

    let items = state.topics.filter((t) => {
      if (state.filter === "bound" && !(t.chat_ids && t.chat_ids.length)) return false;
      if (!q) return true;
      const hay = (t.display_name + " " + t.topic_id + " " + (t.summary || "")).toLowerCase();
      return hay.includes(q);
    });
    if (state.filter === "largest") {
      items = [...items].sort((a, b) => (state.charsMap[b.topic_id] || 0) - (state.charsMap[a.topic_id] || 0));
    }
    $("sidebar-count").textContent = String(items.length);

    for (const t of items) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "topic-item" + (t.topic_id === state.selectedId ? " active" : "");

      const top = document.createElement("div");
      top.className = "topic-item-top";
      const name = document.createElement("span");
      name.className = "topic-item-name";
      name.textContent = t.display_name || t.topic_id;
      top.appendChild(name);
      const chars = state.charsMap[t.topic_id];
      top.appendChild(makePill(chars ? fmtChars(chars) : ("v" + t.version), "pill-gray"));
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
    state.selectedId = topicId;
    renderTopicList();
    $("dream-box").classList.add("hidden");
    $("remember-drawer").classList.add("hidden");
    $("btn-toggle-remember").disabled = false;
    $("btn-dream-preview").disabled = false;
    $("btn-dream-apply").disabled = false;
    $("btn-copy-body").disabled = false;

    const topic = state.topics.find((t) => t.topic_id === topicId);
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
        api("/api/topics/" + encodeURIComponent(topicId) + "/versions?page_size=20").catch(() => ({ items: [] })),
      ]);
      state.currentBody = bodyRes.body || "";
      renderPrettyMarkdown($("detail-pretty"), state.currentBody);
      $("detail-raw").textContent = state.currentBody;
      meta.appendChild(makePill(fmtChars(state.currentBody.length), "pill-blue"));

      const sel = $("version-select");
      clearNode(sel);
      const currentOpt = document.createElement("option");
      currentOpt.value = "current";
      currentOpt.textContent = "当前版本 v" + (topic ? topic.version : 1);
      sel.appendChild(currentOpt);
      for (const v of versRes.items || []) {
        if (topic && v.version === topic.version) continue;
        const opt = document.createElement("option");
        opt.value = String(v.version);
        opt.textContent = "历史快照 v" + v.version;
        sel.appendChild(opt);
      }
      sel.disabled = sel.options.length <= 1;
    } catch (err) {
      $("detail-pretty").textContent = "读取失败: " + err.message;
      $("detail-raw").textContent = "读取失败: " + err.message;
    }
  }

  async function runDream(dryRun) {
    if (!state.selectedId) return;
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
        body: JSON.stringify({ topic: state.selectedId, dry_run: dryRun, force: true }),
      });
      $("dream-status-title").textContent =
        res.status === "dry_run"
          ? "Dream 预览完成（尚未写入）"
          : res.status === "consolidated"
          ? "Dream 已完成原子换版（旧版已归档可回滚）"
          : "Dream 状态: " + res.status;

      const m = $("dream-metrics");
      const pct = res.before_chars ? Math.max(0, Math.round((1 - res.after_chars / res.before_chars) * 100)) : 0;
      m.appendChild(makePill(res.before_chars + " → " + res.after_chars + " 字（压缩 " + pct + "%）", "pill-purple"));
      m.appendChild(makePill("剪噪 " + (res.pruned_count || 0), "pill-gray"));
      m.appendChild(makePill("合并 " + (res.merged_count || 0), "pill-green"));
      m.appendChild(makePill("解冲突 " + (res.conflicts_resolved || 0), "pill-blue"));

      if (pct > 0) {
        $("dream-bar-wrap").classList.remove("hidden");
        $("dream-bar-fill").style.width = Math.min(100, pct) + "%";
      }
      if (dryRun && res.status === "dry_run") {
        $("btn-dream-commit-inline").classList.remove("hidden");
      }

      const ul = $("dream-changes");
      for (const c of res.changes || []) {
        const li = document.createElement("li");
        li.textContent = c;
        ul.appendChild(li);
      }
      renderDiff($("dream-diff"), res.diff || res.error || "");
      if (!dryRun && res.status === "consolidated") {
        toast("Dream 换版成功，QA 缓存已自动刷新");
        await refreshAll();
      } else {
        await refreshLogs();
      }
    } catch (err) {
      $("dream-status-title").textContent = "Dream 失败: " + err.message;
    } finally {
      $("dream-spinner").classList.add("hidden");
      $("btn-dream-preview").disabled = false;
      $("btn-dream-apply").disabled = false;
    }
  }

  async function refreshLogs() {
    try {
      const [qRes, dRes] = await Promise.all([
        api("/api/questions?page_size=20"),
        api("/api/dreams?limit=20"),
      ]);
      const qItems = qRes.items || [];
      $("log-q-count").textContent = String(qItems.length);
      const qWrap = $("log-questions");
      clearNode(qWrap);
      if (!qItems.length) {
        const em = document.createElement("p");
        em.className = "muted small";
        em.textContent = "暂无召回流水。";
        qWrap.appendChild(em);
      }
      for (const q of qItems) {
        const card = document.createElement("div");
        card.className = "log-entry";
        const qt = document.createElement("div");
        qt.className = "log-entry-q";
        qt.textContent = q.question || "";
        const qa = document.createElement("div");
        qa.className = "log-entry-a";
        qa.textContent = q.final_answer || "（无匹配事实）";
        const meta = document.createElement("div");
        meta.className = "meta-row";
        meta.appendChild(makePill(q.source || "-", "pill-gray"));
        meta.appendChild(makePill((q.total_latency_ms || 0) + " ms", "pill-blue"));
        card.appendChild(qt);
        card.appendChild(qa);
        card.appendChild(meta);
        qWrap.appendChild(card);
      }

      const dItems = dRes.items || [];
      $("log-d-count").textContent = String(dItems.length);
      const dWrap = $("log-dreams");
      clearNode(dWrap);
      if (!dItems.length) {
        const em = document.createElement("p");
        em.className = "muted small";
        em.textContent = "暂无 Dream 巩固记录。";
        dWrap.appendChild(em);
      }
      for (const d of dItems) {
        const card = document.createElement("div");
        card.className = "log-entry";
        const dt = document.createElement("div");
        dt.className = "log-entry-q";
        dt.textContent = (d.topic_id || "-") + " · " + (d.status || "-");
        const da = document.createElement("div");
        da.className = "log-entry-a";
        da.textContent = d.before_chars + " → " + d.after_chars + " 字 · 剪噪 " + (d.pruned_count || 0) + " · 合并 " + (d.merged_count || 0) + " · 解冲突 " + (d.conflicts_resolved || 0);
        card.appendChild(dt);
        card.appendChild(da);
        dWrap.appendChild(card);
      }
    } catch (_) {}
  }

  async function submitQuestion(q) {
    if (!q) return;
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
      if (res.topic_id) {
        const jump = document.createElement("button");
        jump.type = "button";
        jump.className = "pill pill-blue topic-jump-btn";
        const matched = state.topics.find((t) => t.topic_id === res.topic_id);
        jump.textContent = "命中: " + (matched ? matched.display_name : res.topic_id) + " →";
        jump.addEventListener("click", () => {
          switchTab("topic");
          selectTopic(res.topic_id);
        });
        meta.appendChild(jump);
      }
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

  $("btn-refresh").addEventListener("click", async () => {
    await refreshAll();
    toast("已刷新最新状态");
  });

  $("topic-search").addEventListener("input", () => renderTopicList());
  document.querySelectorAll(".filter-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll(".filter-chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
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
      toast("复制失败，请手动选择文本");
    }
  });

  $("version-select").addEventListener("change", async (e) => {
    const val = e.target.value;
    if (!state.selectedId) return;
    if (val === "current") {
      await selectTopic(state.selectedId);
      return;
    }
    try {
      const res = await api("/api/topics/" + encodeURIComponent(state.selectedId) + "/versions/" + encodeURIComponent(val) + "/body");
      state.currentBody = res.body || "";
      renderPrettyMarkdown($("detail-pretty"), state.currentBody);
      $("detail-raw").textContent = state.currentBody;
      toast("正在查看历史快照 v" + val);
    } catch (err) {
      toast("读取历史版本失败: " + err.message);
    }
  });

  $("btn-toggle-remember").addEventListener("click", () => {
    $("remember-drawer").classList.toggle("hidden");
    if (!$("remember-drawer").classList.contains("hidden")) $("remember-input").focus();
  });
  $("btn-remember-cancel").addEventListener("click", () => {
    $("remember-drawer").classList.add("hidden");
    $("remember-input").value = "";
  });
  $("remember-drawer").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fact = ($("remember-input").value || "").trim();
    if (!fact || !state.selectedId) return;
    $("btn-remember-submit").disabled = true;
    try {
      const res = await api("/api/v1/remember", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic: state.selectedId, fact, source_agent: "studio-ui" }),
      });
      $("remember-input").value = "";
      $("remember-drawer").classList.add("hidden");
      toast("已写入记忆（v" + res.topic_version + "）");
      await refreshAll();
    } catch (err) {
      toast("写入失败: " + err.message);
    } finally {
      $("btn-remember-submit").disabled = false;
    }
  });

  $("btn-dream-preview").addEventListener("click", () => runDream(true));
  $("btn-dream-apply").addEventListener("click", () => runDream(false));
  $("btn-dream-commit-inline").addEventListener("click", () => runDream(false));
  $("btn-dream-close").addEventListener("click", () => $("dream-box").classList.add("hidden"));

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => switchTab(tab.getAttribute("data-tab")));
  });

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
