(() => {
  "use strict";

  const SESSION_KEY = "hermes-topic-memory.panel-session";
  const SVG_NS = "http://www.w3.org/2000/svg";
  const numberFormat = new Intl.NumberFormat("zh-CN");
  const timeFormat = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

  const elements = {
    gate: document.getElementById("session-gate"),
    form: document.getElementById("session-form"),
    tokenInput: document.getElementById("session-token"),
    connectButton: document.getElementById("connect-session"),
    sessionError: document.getElementById("session-error"),
    shell: document.getElementById("panel-shell"),
    clearSession: document.getElementById("clear-session"),
    refreshOverview: document.getElementById("refresh-overview"),
    resetSession: document.getElementById("reset-session"),
    loading: document.getElementById("loading-state"),
    error: document.getElementById("error-state"),
    errorMessage: document.getElementById("error-message"),
    retry: document.getElementById("retry-overview"),
    empty: document.getElementById("empty-state"),
    refreshEmpty: document.getElementById("refresh-empty"),
    dashboard: document.getElementById("dashboard-data"),
    snapshotStatus: document.getElementById("snapshot-status"),
    secondaryCounts: document.getElementById("secondary-counts"),
    kpiActive: document.getElementById("kpi-active"),
    kpiTotal: document.getElementById("kpi-total"),
    kpiCache: document.getElementById("kpi-cache"),
    kpi30d: document.getElementById("kpi-30d"),
    activityChart: document.getElementById("activity-chart"),
    activityDescription: document.getElementById("activity-chart-desc"),
    activitySummary: document.getElementById("activity-summary"),
    activityTable: document.getElementById("activity-table-body"),
    distributionChart: document.getElementById("topic-distribution"),
    distributionDescription: document.getElementById("distribution-chart-desc"),
    distributionSummary: document.getElementById("distribution-summary"),
    distributionTable: document.getElementById("distribution-table-body"),
    recentList: document.getElementById("recent-list"),
    recentEmpty: document.getElementById("recent-empty"),
    search: document.getElementById("topic-search"),
    statusFilter: document.getElementById("topic-status-filter"),
    sort: document.getElementById("topic-sort"),
    topicTable: document.getElementById("topic-table-body"),
    catalogEmpty: document.getElementById("catalog-empty"),
    catalogCount: document.getElementById("catalog-count"),
  };

  const state = { token: "", snapshot: null, topics: [] };

  class ApiError extends Error {
    constructor(status, message) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }

  function setHidden(element, hidden) {
    element.hidden = hidden;
  }

  function setButtonLoading(button, loading, loadingLabel, idleLabel) {
    button.disabled = loading;
    button.setAttribute("aria-busy", String(loading));
    button.textContent = loading ? loadingLabel : idleLabel;
  }

  function showSessionGate(message = "") {
    state.token = "";
    state.snapshot = null;
    state.topics = [];
    setHidden(elements.shell, true);
    setHidden(elements.gate, false);
    elements.sessionError.textContent = message;
    setHidden(elements.sessionError, !message);
    elements.tokenInput.disabled = false;
    window.requestAnimationFrame(() => elements.tokenInput.focus());
  }

  function showShellState(name) {
    setHidden(elements.gate, true);
    setHidden(elements.shell, false);
    setHidden(elements.loading, name !== "loading");
    setHidden(elements.error, name !== "error");
    setHidden(elements.empty, name !== "empty");
    setHidden(elements.dashboard, name !== "ready");
  }

  async function readErrorMessage(response) {
    try {
      const payload = await response.json();
      if (payload && payload.error && typeof payload.error.message === "string") {
        return payload.error.message;
      }
    } catch (_error) {
      return "服务返回了无法解析的错误。";
    }
    return "服务拒绝了请求。";
  }

  async function requestOverview(token) {
    const response = await fetch("/api/overview", {
      method: "GET",
      cache: "no-store",
      credentials: "same-origin",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) {
      throw new ApiError(response.status, await readErrorMessage(response));
    }
    const snapshot = await response.json();
    if (!snapshot || !snapshot.kpis || !snapshot.charts || !Array.isArray(snapshot.topics)) {
      throw new ApiError(502, "概览响应缺少必要字段。请检查服务版本。\n");
    }
    return snapshot;
  }

  async function connectSession(token, restoring = false) {
    const candidate = String(token || "");
    elements.tokenInput.value = "";
    if (!candidate.trim()) {
      showSessionGate("请输入访问令牌。\n");
      return;
    }
    elements.sessionError.textContent = "";
    setHidden(elements.sessionError, true);
    elements.tokenInput.disabled = true;
    setButtonLoading(elements.connectButton, true, restoring ? "恢复中…" : "连接中…", "连接");
    showShellState("loading");
    try {
      const snapshot = await requestOverview(candidate);
      state.token = candidate;
      sessionStorage.setItem(SESSION_KEY, candidate);
      renderSnapshot(snapshot);
    } catch (error) {
      sessionStorage.removeItem(SESSION_KEY);
      const message = error instanceof ApiError && error.status === 401
        ? "令牌无效或已失效，请重新输入。"
        : "无法连接 Panel API，请检查服务后重试。";
      showSessionGate(message);
    } finally {
      elements.tokenInput.disabled = false;
      setButtonLoading(elements.connectButton, false, "连接中…", "连接");
    }
  }

  async function refreshOverview() {
    if (!state.token) {
      showSessionGate("会话已失效，请重新输入令牌。\n");
      return;
    }
    showShellState("loading");
    setButtonLoading(elements.retry, true, "重试中…", "重试概览");
    setButtonLoading(elements.refreshEmpty, true, "载入中…", "重新载入");
    setButtonLoading(elements.refreshOverview, true, "刷新中…", "刷新快照");
    try {
      renderSnapshot(await requestOverview(state.token));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        sessionStorage.removeItem(SESSION_KEY);
        showSessionGate("会话已失效，请重新输入令牌。\n");
      } else {
        elements.errorMessage.textContent = error instanceof ApiError
          ? `${error.message} 请检查服务状态后重试。`
          : "网络请求失败。请检查服务状态后重试。";
        showShellState("error");
      }
    } finally {
      setButtonLoading(elements.retry, false, "重试中…", "重试概览");
      setButtonLoading(elements.refreshEmpty, false, "载入中…", "重新载入");
      setButtonLoading(elements.refreshOverview, false, "刷新中…", "刷新快照");
    }
  }

  function renderSnapshot(snapshot) {
    state.snapshot = snapshot;
    state.topics = snapshot.topics.slice();
    renderKpis(snapshot.kpis);
    renderActivity(snapshot.charts.daily_questions || []);
    renderDistribution(snapshot.charts.topic_distribution || []);
    renderRecent(snapshot.recent_questions || []);
    renderCatalog();
    elements.snapshotStatus.textContent = `已鉴权快照 / ${timeFormat.format(new Date())}`;
    const isEmpty = snapshot.topics.length === 0 && Number(snapshot.kpis.total_question_count) === 0;
    showShellState(isEmpty ? "empty" : "ready");
  }

  function renderKpis(kpis) {
    elements.kpiActive.textContent = formatCount(kpis.active_topic_count);
    elements.kpiTotal.textContent = formatCount(kpis.total_question_count);
    elements.kpiCache.textContent = formatCount(kpis.active_cache_count);
    elements.kpi30d.textContent = formatCount(kpis.questions_last_30_days);
    elements.secondaryCounts.textContent = `已归档 ${formatCount(kpis.archived_topic_count)} / 回收站 ${formatCount(kpis.trashed_topic_count)}`;
  }

  function renderActivity(days) {
    const svg = elements.activityChart;
    const title = svg.querySelector("title");
    const description = elements.activityDescription;
    svg.replaceChildren(title, description);
    elements.activityTable.replaceChildren();

    const values = days.map((day) => Math.max(0, Number(day.question_count) || 0));
    const total = values.reduce((sum, value) => sum + value, 0);
    const peak = Math.max(0, ...values);
    const peakIndex = values.indexOf(peak);
    const summary = days.length === 0
      ? "暂无 30 天活动数据。"
      : `30 天共 ${formatCount(total)} 次提问，单日峰值 ${formatCount(peak)} 次${peak > 0 ? `（${formatDay(days[peakIndex].day)}）` : ""}。`;
    elements.activitySummary.textContent = summary;
    description.textContent = `${summary} 折线从左至右按北京时间日期排列。`;

    for (const day of days) {
      const row = document.createElement("tr");
      appendCell(row, formatDay(day.day), true);
      appendCell(row, formatCount(day.question_count));
      elements.activityTable.append(row);
    }

    if (days.length === 0) {
      appendSvgText(svg, 360, 132, "暂无活动数据", { anchor: "middle" });
      return;
    }

    const width = 720;
    const height = 260;
    const left = 46;
    const right = 18;
    const top = 22;
    const bottom = 38;
    const chartWidth = width - left - right;
    const chartHeight = height - top - bottom;
    const ceiling = Math.max(4, Math.ceil(peak / 4) * 4);

    for (let step = 0; step <= 4; step += 1) {
      const y = top + (chartHeight * step) / 4;
      svg.append(svgElement("line", { x1: left, y1: y, x2: width - right, y2: y, class: "grid-line" }));
      appendSvgText(svg, left - 9, y + 3, formatCount(Math.round(ceiling * (1 - step / 4))), { anchor: "end" });
    }
    svg.append(svgElement("line", { x1: left, y1: top + chartHeight, x2: width - right, y2: top + chartHeight, class: "axis-line" }));

    const points = days.map((day, index) => {
      const x = left + (chartWidth * index) / Math.max(1, days.length - 1);
      const y = top + chartHeight - ((Number(day.question_count) || 0) / ceiling) * chartHeight;
      return { x, y, day };
    });
    const path = points.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(" ");
    svg.append(svgElement("path", { d: path, class: "activity-line" }));
    points.forEach((point) => {
      if (Number(point.day.question_count) > 0) {
        const circle = svgElement("circle", { cx: point.x, cy: point.y, r: 3.5, class: "activity-point" });
        const pointTitle = document.createElementNS(SVG_NS, "title");
        pointTitle.textContent = `${formatDay(point.day.day)}：${formatCount(point.day.question_count)} 次`;
        circle.append(pointTitle);
        svg.append(circle);
      }
    });
    [0, Math.floor((days.length - 1) / 2), days.length - 1].forEach((index) => {
      appendSvgText(svg, points[index].x, height - 13, formatDay(days[index].day), { anchor: index === 0 ? "start" : index === days.length - 1 ? "end" : "middle" });
    });
  }

  function renderDistribution(distribution) {
    const svg = elements.distributionChart;
    const title = svg.querySelector("title");
    const description = elements.distributionDescription;
    svg.replaceChildren(title, description);
    elements.distributionTable.replaceChildren();

    const sorted = distribution.slice().sort((a, b) => Number(b.question_count) - Number(a.question_count) || String(a.display_name).localeCompare(String(b.display_name), "zh-CN"));
    const total = sorted.reduce((sum, topic) => sum + Math.max(0, Number(topic.question_count) || 0), 0);
    const shown = sorted.slice(0, 8);
    const max = Math.max(1, ...shown.map((topic) => Number(topic.question_count) || 0));
    const summary = sorted.length === 0
      ? "暂无主题分布数据。"
      : `${formatCount(sorted.length)} 个主题共计 ${formatCount(total)} 次主题参与；图中显示前 ${formatCount(shown.length)} 项。`;
    elements.distributionSummary.textContent = summary;
    description.textContent = summary;

    for (const topic of sorted) {
      const row = document.createElement("tr");
      appendCell(row, `${topic.display_name} / ${topic.topic_id}`, true);
      appendCell(row, formatCount(topic.question_count));
      elements.distributionTable.append(row);
    }

    if (shown.length === 0) {
      appendSvgText(svg, 280, 160, "暂无分布数据", { anchor: "middle" });
      return;
    }

    const left = 150;
    const width = 560;
    const usable = width - left - 48;
    const rowHeight = 36;
    shown.forEach((topic, index) => {
      const y = 28 + index * rowHeight;
      const count = Math.max(0, Number(topic.question_count) || 0);
      const barWidth = (count / max) * usable;
      appendSvgText(svg, left - 10, y + 12, truncate(topic.display_name, 18), { anchor: "end" });
      svg.append(svgElement("rect", { x: left, y, width: usable, height: 15, class: "distribution-track" }));
      svg.append(svgElement("rect", { x: left, y, width: barWidth, height: 15, class: "distribution-bar" }));
      appendSvgText(svg, Math.min(width - 4, left + barWidth + 7), y + 12, formatCount(count), { anchor: "start" });
    });
  }

  function renderRecent(items) {
    elements.recentList.replaceChildren();
    setHidden(elements.recentEmpty, items.length > 0);
    for (const item of items) {
      const row = document.createElement("li");
      row.className = "activity-item";
      const time = document.createElement("time");
      time.dateTime = String(item.created_at || "");
      time.textContent = formatTime(item.created_at);
      const question = document.createElement("p");
      question.className = "activity-question";
      question.textContent = String(item.question || "（无问题文本）");
      const meta = document.createElement("div");
      meta.className = "activity-scope";
      const scope = item.scope === "multi" ? "多主题" : "单主题";
      const topics = Array.isArray(item.topic_ids) ? item.topic_ids.join(" + ") : "—";
      meta.textContent = `${scope} / ${topics} / ${String(item.source || "unknown")}`;
      row.append(time, question, meta);
      elements.recentList.append(row);
    }
  }

  function renderCatalog() {
    const query = elements.search.value.trim().toLocaleLowerCase("zh-CN");
    const status = elements.statusFilter.value;
    const topics = state.topics.filter((topic) => {
      const matchesStatus = status === "all" || topic.status === status;
      const aliases = Array.isArray(topic.aliases) ? topic.aliases : [];
      const haystack = [topic.display_name, topic.topic_id, ...aliases].join(" ").toLocaleLowerCase("zh-CN");
      return matchesStatus && (!query || haystack.includes(query));
    });
    sortTopics(topics, elements.sort.value);

    elements.topicTable.replaceChildren();
    for (const topic of topics) {
      const row = document.createElement("tr");
      const identity = document.createElement("td");
      identity.className = "topic-identity";
      const name = document.createElement("strong");
      name.textContent = String(topic.display_name || topic.topic_id || "未命名主题");
      const metadata = document.createElement("small");
      const aliases = Array.isArray(topic.aliases) && topic.aliases.length ? ` / ${topic.aliases.join(", ")}` : "";
      metadata.textContent = `${String(topic.topic_id || "—")}${aliases}`;
      identity.append(name, metadata);
      row.append(identity);
      appendCell(row, formatCount(topic.question_count));
      appendCell(row, formatCount(topic.active_cache_count));
      appendCell(row, `v${formatCount(topic.version)}`);
      appendCell(row, formatTime(topic.updated_at));
      const statusCell = document.createElement("td");
      const statusText = document.createElement("span");
      statusText.className = "status-text";
      const dot = document.createElement("span");
      dot.className = `status-dot ${topic.status === "active" ? "status-dot--ok" : "status-dot--warn"}`;
      dot.setAttribute("aria-hidden", "true");
      statusText.append(dot, document.createTextNode(topic.status === "active" ? "活跃" : "已归档"));
      statusCell.append(statusText);
      row.append(statusCell);
      elements.topicTable.append(row);
    }
    elements.catalogCount.textContent = `显示 ${formatCount(topics.length)} / ${formatCount(state.topics.length)} 个主题`;
    setHidden(elements.catalogEmpty, topics.length > 0);
    setHidden(elements.topicTable.parentElement, topics.length === 0);
  }

  function sortTopics(topics, mode) {
    const titleOrder = (a, b) => String(a.display_name).localeCompare(String(b.display_name), "zh-CN");
    const comparators = {
      "question-desc": (a, b) => Number(b.question_count) - Number(a.question_count) || titleOrder(a, b),
      "updated-desc": (a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at) || titleOrder(a, b),
      "version-desc": (a, b) => Number(b.version) - Number(a.version) || titleOrder(a, b),
      "title-asc": titleOrder,
    };
    topics.sort(comparators[mode] || titleOrder);
  }

  function appendCell(row, value, header = false) {
    const cell = document.createElement(header ? "th" : "td");
    if (header) {
      cell.scope = "row";
    }
    cell.textContent = String(value);
    row.append(cell);
  }

  function svgElement(name, attributes) {
    const element = document.createElementNS(SVG_NS, name);
    for (const [key, value] of Object.entries(attributes)) {
      element.setAttribute(key, String(value));
    }
    return element;
  }

  function appendSvgText(svg, x, y, text, options = {}) {
    const node = svgElement("text", { x, y, "text-anchor": options.anchor || "start" });
    node.textContent = String(text);
    svg.append(node);
  }

  function formatCount(value) {
    const number = Number(value);
    return numberFormat.format(Number.isFinite(number) ? number : 0);
  }

  function formatDay(value) {
    const match = String(value || "").match(/^\d{4}-(\d{2})-(\d{2})$/);
    return match ? `${match[1]}/${match[2]}` : String(value || "—");
  }

  function formatTime(value) {
    const date = new Date(String(value || ""));
    return Number.isNaN(date.getTime()) ? "时间未知" : timeFormat.format(date);
  }

  function truncate(value, length) {
    const text = String(value || "");
    return text.length > length ? `${text.slice(0, length - 1)}…` : text;
  }

  function clearSession() {
    sessionStorage.removeItem(SESSION_KEY);
    elements.tokenInput.value = "";
    showSessionGate("会话已从浏览器中清除。\n");
  }

  function switchView(viewName) {
    const supported = ["home", "recall", "recent", "recycle"];
    const next = supported.includes(viewName) ? viewName : "home";
    document.querySelectorAll("[data-panel]").forEach((panel) => {
      setHidden(panel, panel.dataset.panel !== next);
    });
    document.querySelectorAll("[data-view]").forEach((link) => {
      const active = link.dataset.view === next;
      link.classList.toggle("is-active", active);
      if (active) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  }

  elements.form.addEventListener("submit", (event) => {
    event.preventDefault();
    void connectSession(elements.tokenInput.value);
  });
  elements.retry.addEventListener("click", () => { void refreshOverview(); });
  elements.refreshEmpty.addEventListener("click", () => { void refreshOverview(); });
  elements.refreshOverview.addEventListener("click", () => { void refreshOverview(); });
  elements.clearSession.addEventListener("click", clearSession);
  elements.resetSession.addEventListener("click", clearSession);
  elements.search.addEventListener("input", renderCatalog);
  elements.statusFilter.addEventListener("change", renderCatalog);
  elements.sort.addEventListener("change", renderCatalog);
  document.querySelectorAll("[data-view]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const view = link.dataset.view || "home";
      history.replaceState(null, "", `#${view}`);
      switchView(view);
    });
  });

  switchView(location.hash.slice(1) || "home");
  const savedToken = sessionStorage.getItem(SESSION_KEY);
  if (savedToken) {
    void connectSession(savedToken, true);
  } else {
    showSessionGate();
  }
})();
