(() => {
  "use strict";

  const SESSION_KEY = "hermes-topic-memory.panel-session";
  const SVG_NS = "http://www.w3.org/2000/svg";
  const PAGE_SIZE = 6;
  const DETAIL_TABS = ["overview", "questions", "ask", "body", "versions", "settings"];
  const PRIMARY_VIEWS = ["home", "recall", "recent", "recycle", "detail"];
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

  const elements = Object.fromEntries(
    Array.from(document.querySelectorAll("[id]"), (element) => [element.id, element]),
  );

  const state = {
    token: "",
    snapshot: null,
    topics: [],
    view: "home",
    detail: {
      topicId: "",
      topic: null,
      tab: "overview",
      returnFocus: null,
      body: null,
      questionsPage: 1,
      questionsLoaded: false,
      versionsPage: 1,
      versions: [],
      versionsLoaded: false,
      versionBodies: {},
    },
    recall: {
      running: false,
      queryId: "",
      previousQueryId: "",
      result: null,
      suggestedTopic: null,
    },
    recent: {
      page: 1,
      pageSize: PAGE_SIZE,
      total: 0,
      loaded: false,
    },
  };

  class ApiError extends Error {
    constructor(status, message) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }

  function byId(id) {
    return elements[id];
  }

  function setHidden(element, hidden) {
    if (element) {
      element.hidden = hidden;
    }
  }

  function clear(element) {
    element.replaceChildren();
  }

  function textNode(value) {
    return document.createTextNode(String(value ?? ""));
  }

  function create(tag, className = "", text = "") {
    const element = document.createElement(tag);
    if (className) {
      element.className = className;
    }
    if (text !== "") {
      element.textContent = String(text);
    }
    return element;
  }

  function setButtonLoading(button, loading, loadingLabel, idleLabel) {
    button.disabled = loading;
    button.setAttribute("aria-busy", String(loading));
    button.textContent = loading ? loadingLabel : idleLabel;
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

  async function api(path, options = {}) {
    if (!state.token) {
      throw new ApiError(401, "会话已失效。");
    }
    const method = options.method || "GET";
    const headers = { Authorization: `Bearer ${state.token}` };
    const request = {
      method,
      cache: "no-store",
      credentials: "same-origin",
      headers,
    };
    if (Object.hasOwn(options, "body")) {
      headers["Content-Type"] = "application/json";
      request.body = JSON.stringify(options.body);
    }
    let response;
    try {
      response = await fetch(path, request);
    } catch (_error) {
      throw new ApiError(0, "无法连接 Panel API。");
    }
    if (!response.ok) {
      const message = await readErrorMessage(response);
      if (response.status === 401) {
        sessionStorage.removeItem(SESSION_KEY);
        showSessionGate("会话已失效，请重新输入令牌。\n");
      }
      throw new ApiError(response.status, message);
    }
    try {
      return await response.json();
    } catch (_error) {
      throw new ApiError(502, "服务返回了无法解析的数据。");
    }
  }

  function showSessionGate(message = "") {
    state.token = "";
    state.snapshot = null;
    state.topics = [];
    setHidden(byId("panel-shell"), true);
    setHidden(byId("session-gate"), false);
    byId("session-error").textContent = message;
    setHidden(byId("session-error"), !message);
    byId("session-token").disabled = false;
    window.requestAnimationFrame(() => byId("session-token").focus());
  }

  function showShellState(name) {
    setHidden(byId("session-gate"), true);
    setHidden(byId("panel-shell"), false);
    setHidden(byId("loading-state"), name !== "loading");
    setHidden(byId("error-state"), name !== "error");
    setHidden(byId("empty-state"), name !== "empty");
    setHidden(byId("dashboard-data"), name !== "ready");
  }

  async function connectSession(token, restoring = false) {
    const candidate = String(token || "");
    byId("session-token").value = "";
    if (!candidate.trim()) {
      showSessionGate("请输入访问令牌。\n");
      return;
    }
    byId("session-error").textContent = "";
    setHidden(byId("session-error"), true);
    byId("session-token").disabled = true;
    setButtonLoading(byId("connect-session"), true, restoring ? "恢复中…" : "连接中…", "连接");
    setHidden(byId("session-gate"), true);
    setHidden(byId("panel-shell"), false);
    switchView("home");
    showShellState("loading");
    state.token = candidate;
    try {
      const snapshot = await api("/api/overview");
      sessionStorage.setItem(SESSION_KEY, candidate);
      renderSnapshot(snapshot);
      await routeFromHash({ replaceInvalid: true });
    } catch (error) {
      sessionStorage.removeItem(SESSION_KEY);
      const message = error instanceof ApiError && error.status === 401
        ? "令牌无效或已失效，请重新输入。"
        : "无法连接 Panel API，请检查服务后重试。";
      showSessionGate(message);
    } finally {
      byId("session-token").disabled = false;
      setButtonLoading(byId("connect-session"), false, "连接中…", "连接");
    }
  }

  async function refreshOverview() {
    if (!state.token) {
      showSessionGate("会话已失效，请重新输入令牌。\n");
      return;
    }
    showShellState("loading");
    setButtonLoading(byId("retry-overview"), true, "重试中…", "重试概览");
    setButtonLoading(byId("refresh-empty"), true, "载入中…", "重新载入");
    setButtonLoading(byId("refresh-overview"), true, "刷新中…", "刷新快照");
    try {
      renderSnapshot(await api("/api/overview"));
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        byId("error-message").textContent = error instanceof ApiError
          ? `${error.message} 请检查服务状态后重试。`
          : "网络请求失败。请检查服务状态后重试。";
        showShellState("error");
      }
    } finally {
      setButtonLoading(byId("retry-overview"), false, "重试中…", "重试概览");
      setButtonLoading(byId("refresh-empty"), false, "载入中…", "重新载入");
      setButtonLoading(byId("refresh-overview"), false, "刷新中…", "刷新快照");
    }
  }

  function renderSnapshot(snapshot) {
    if (!snapshot || !snapshot.kpis || !snapshot.charts || !Array.isArray(snapshot.topics)) {
      throw new ApiError(502, "概览响应缺少必要字段。请检查服务版本。");
    }
    state.snapshot = snapshot;
    state.topics = snapshot.topics.slice();
    renderKpis(snapshot.kpis);
    renderActivity(snapshot.charts.daily_questions || []);
    renderDistribution(snapshot.charts.topic_distribution || []);
    renderRecentActivity(snapshot.recent_questions || []);
    renderCatalog();
    populateTopicSelectors();
    byId("snapshot-status").textContent = `已鉴权快照 / ${timeFormat.format(new Date())}`;
    const isEmpty = snapshot.topics.length === 0 && Number(snapshot.kpis.total_question_count) === 0;
    showShellState(isEmpty ? "empty" : "ready");
  }

  function renderKpis(kpis) {
    byId("kpi-active").textContent = formatCount(kpis.active_topic_count);
    byId("kpi-total").textContent = formatCount(kpis.total_question_count);
    byId("kpi-cache").textContent = formatCount(kpis.active_cache_count);
    byId("kpi-30d").textContent = formatCount(kpis.questions_last_30_days);
    byId("secondary-counts").textContent = `已归档 ${formatCount(kpis.archived_topic_count)} / 回收站 ${formatCount(kpis.trashed_topic_count)}`;
  }

  function renderActivity(days) {
    const svg = byId("activity-chart");
    const title = svg.querySelector("title");
    const description = byId("activity-chart-desc");
    svg.replaceChildren(title, description);
    clear(byId("activity-table-body"));
    const values = days.map((day) => Math.max(0, Number(day.question_count) || 0));
    const total = values.reduce((sum, value) => sum + value, 0);
    const peak = Math.max(0, ...values);
    const peakIndex = values.indexOf(peak);
    const summary = days.length === 0
      ? "暂无 30 天活动数据。"
      : `30 天共 ${formatCount(total)} 次提问，单日峰值 ${formatCount(peak)} 次${peak > 0 ? `（${formatDay(days[peakIndex].day)}）` : ""}。`;
    byId("activity-summary").textContent = summary;
    description.textContent = `${summary} 折线从左至右按北京时间日期排列。`;
    days.forEach((day) => {
      const row = create("tr");
      appendCell(row, formatDay(day.day), true);
      appendCell(row, formatCount(day.question_count));
      byId("activity-table-body").append(row);
    });
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
    const points = days.map((day, index) => ({
      x: left + (chartWidth * index) / Math.max(1, days.length - 1),
      y: top + chartHeight - ((Number(day.question_count) || 0) / ceiling) * chartHeight,
      day,
    }));
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
    const svg = byId("topic-distribution");
    const title = svg.querySelector("title");
    const description = byId("distribution-chart-desc");
    svg.replaceChildren(title, description);
    clear(byId("distribution-table-body"));
    const sorted = distribution.slice().sort((a, b) => Number(b.question_count) - Number(a.question_count) || String(a.display_name).localeCompare(String(b.display_name), "zh-CN"));
    const total = sorted.reduce((sum, topic) => sum + Math.max(0, Number(topic.question_count) || 0), 0);
    const shown = sorted.slice(0, 8);
    const max = Math.max(1, ...shown.map((topic) => Number(topic.question_count) || 0));
    const summary = sorted.length === 0 ? "暂无主题分布数据。" : `${formatCount(sorted.length)} 个主题共计 ${formatCount(total)} 次主题参与；图中显示前 ${formatCount(shown.length)} 项。`;
    byId("distribution-summary").textContent = summary;
    description.textContent = summary;
    sorted.forEach((topic) => {
      const row = create("tr");
      appendCell(row, `${topic.display_name} / ${topic.topic_id}`, true);
      appendCell(row, formatCount(topic.question_count));
      byId("distribution-table-body").append(row);
    });
    if (shown.length === 0) {
      appendSvgText(svg, 280, 160, "暂无分布数据", { anchor: "middle" });
      return;
    }
    const left = 150;
    const width = 560;
    const usable = width - left - 48;
    shown.forEach((topic, index) => {
      const y = 28 + index * 36;
      const count = Math.max(0, Number(topic.question_count) || 0);
      const barWidth = (count / max) * usable;
      appendSvgText(svg, left - 10, y + 12, truncate(topic.display_name, 18), { anchor: "end" });
      svg.append(svgElement("rect", { x: left, y, width: usable, height: 15, class: "distribution-track" }));
      svg.append(svgElement("rect", { x: left, y, width: barWidth, height: 15, class: "distribution-bar" }));
      appendSvgText(svg, Math.min(width - 4, left + barWidth + 7), y + 12, formatCount(count));
    });
  }

  function renderRecentActivity(items) {
    clear(byId("recent-list"));
    setHidden(byId("recent-empty"), items.length > 0);
    items.forEach((item) => {
      const row = create("li", "activity-item");
      const time = create("time", "", formatTime(item.created_at));
      time.dateTime = String(item.created_at || "");
      const question = create("p", "activity-question", item.question || "（无问题文本）");
      const topics = Array.isArray(item.topic_ids) ? item.topic_ids.join(" + ") : "—";
      const scope = item.scope === "multi" ? "多主题" : "单主题";
      const meta = create("div", "activity-scope", `${scope} / ${topics} / ${item.source || "unknown"}`);
      row.append(time, question, meta);
      byId("recent-list").append(row);
    });
  }

  function renderCatalog() {
    const query = byId("topic-search").value.trim().toLocaleLowerCase("zh-CN");
    const status = byId("topic-status-filter").value;
    const topics = state.topics.filter((topic) => {
      const aliases = Array.isArray(topic.aliases) ? topic.aliases : [];
      const haystack = [topic.display_name, topic.topic_id, ...aliases].join(" ").toLocaleLowerCase("zh-CN");
      return (status === "all" || topic.status === status) && (!query || haystack.includes(query));
    });
    sortTopics(topics, byId("topic-sort").value);
    clear(byId("topic-table-body"));
    topics.forEach((topic) => {
      const row = create("tr", "catalog-row");
      row.tabIndex = 0;
      row.setAttribute("aria-label", `打开主题 ${topic.display_name || topic.topic_id}`);
      const identity = create("td", "topic-identity");
      const name = create("strong", "", topic.display_name || topic.topic_id || "未命名主题");
      const aliases = Array.isArray(topic.aliases) && topic.aliases.length ? ` / ${topic.aliases.join(", ")}` : "";
      const metadata = create("small", "", `${topic.topic_id || "—"}${aliases}`);
      identity.append(name, metadata);
      row.append(identity);
      appendCell(row, formatCount(topic.question_count));
      appendCell(row, formatCount(topic.active_cache_count));
      appendCell(row, `v${formatCount(topic.version)}`);
      appendCell(row, formatTime(topic.updated_at));
      const statusCell = create("td");
      statusCell.append(statusLabel(topic.status === "active" ? "answered" : "no_fact", topic.status === "active" ? "活跃" : "已归档"));
      row.append(statusCell);
      row.addEventListener("click", () => navigateToTopic(topic.topic_id, "overview", row));
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          navigateToTopic(topic.topic_id, "overview", row);
        }
      });
      byId("topic-table-body").append(row);
    });
    byId("catalog-count").textContent = `显示 ${formatCount(topics.length)} / ${formatCount(state.topics.length)} 个主题`;
    setHidden(byId("catalog-empty"), topics.length > 0);
    setHidden(byId("topic-table-body").parentElement, topics.length === 0);
  }

  function populateTopicSelectors() {
    const selectors = [byId("manual-topic-select"), byId("recent-topic-filter")];
    selectors.forEach((select) => {
      const previous = Array.from(select.selectedOptions, (option) => option.value);
      const placeholder = select.id === "recent-topic-filter" ? create("option", "", "全部主题") : null;
      if (placeholder) {
        placeholder.value = "";
        select.replaceChildren(placeholder);
      } else {
        clear(select);
      }
      state.topics.forEach((topic) => {
        const option = create("option", "", `${topic.display_name} / ${topic.topic_id}`);
        option.value = topic.topic_id;
        option.selected = previous.includes(topic.topic_id);
        select.append(option);
      });
    });
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

  function navigateToTopic(topicId, tab = "overview", trigger = null) {
    if (trigger) {
      state.detail.returnFocus = trigger;
    }
    setHash(`topic/${encodeURIComponent(topicId)}/${DETAIL_TABS.includes(tab) ? tab : "overview"}`);
  }

  async function openTopicDetail(topicId, tab = "overview") {
    const validTopic = state.topics.some((topic) => topic.topic_id === topicId);
    if (!validTopic) {
      setHash("home", true);
      switchView("home");
      return;
    }
    const changedTopic = state.detail.topicId !== topicId;
    if (changedTopic) {
      state.detail.topicId = topicId;
      state.detail.topic = null;
      state.detail.body = null;
      state.detail.questionsPage = 1;
      state.detail.questionsLoaded = false;
      state.detail.versionsPage = 1;
      state.detail.versions = [];
      state.detail.versionsLoaded = false;
      state.detail.versionBodies = {};
    }
    switchView("detail");
    setHidden(byId("topic-detail-loading"), false);
    setHidden(byId("topic-detail-error"), true);
    setHidden(byId("topic-detail-content"), true);
    if (!state.detail.topic) {
      try {
        const payload = await api(`/api/topics/${encodeURIComponent(topicId)}`);
        state.detail.topic = payload.topic;
        renderTopicDetail(payload.topic);
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 401)) {
          byId("topic-detail-error-message").textContent = `${errorMessage(error)} 请返回目录或重试。`;
          setHidden(byId("topic-detail-loading"), true);
          setHidden(byId("topic-detail-error"), false);
        }
        return;
      }
    } else {
      renderTopicDetail(state.detail.topic);
    }
    setHidden(byId("topic-detail-loading"), true);
    setHidden(byId("topic-detail-content"), false);
    await selectDetailTab(tab, { updateHash: false });
    window.requestAnimationFrame(() => byId("topic-detail-title").focus());
  }

  function renderTopicDetail(topic) {
    byId("topic-detail-title").textContent = topic.display_name || topic.topic_id;
    byId("topic-detail-subtitle").textContent = `${topic.topic_id} / v${formatCount(topic.version)} / ${topic.state || "unknown"}`;
    renderMetadata(byId("topic-overview-grid"), topicMetadata(topic));
    renderMetadata(byId("topic-settings-metadata"), [
      ["显示名称", topic.display_name],
      ["Topic ID", topic.topic_id],
      ["别名", aliasText(topic.aliases)],
      ["状态", topic.state],
      ["来源策略", topic.source_policy],
      ["相对来源", topic.source_relative_path || "未记录"],
      ["当前版本", `v${formatCount(topic.version)}`],
      ["更新时间", formatTime(topic.updated_at)],
    ]);
    const chip = topicChip(topic.topic_id, "当前上下文");
    byId("topic-ask-context").replaceChildren(chip);
    void loadTopicOverviewActivity();
  }

  function topicMetadata(topic) {
    return [
      ["Topic ID", topic.topic_id],
      ["别名", aliasText(topic.aliases)],
      ["当前版本", `v${formatCount(topic.version)}`],
      ["版本数", formatCount(topic.version_count)],
      ["正文大小", `${formatCount(topic.source_bytes)} bytes / ${formatCount(topic.source_chars)} chars / ${formatCount(topic.source_lines)} lines`],
      ["历史提问", formatCount(topic.question_count)],
      ["有效缓存", formatCount(topic.active_cache_count)],
      ["状态", topic.state],
      ["创建时间", formatTime(topic.created_at)],
      ["更新时间", formatTime(topic.updated_at)],
      ["来源策略", topic.source_policy],
      ["内容指纹", topic.content_fingerprint || "—"],
    ];
  }

  async function loadTopicOverviewActivity() {
    const container = byId("topic-overview-activity");
    container.replaceChildren(create("div", "compact-state", "正在读取近期参与记录…"));
    const topicId = state.detail.topicId;
    try {
      const payload = await api(`/api/topics/${encodeURIComponent(topicId)}/questions?page=1&page_size=3`);
      if (state.detail.topicId !== topicId) {
        return;
      }
      if (!payload.items.length) {
        container.replaceChildren(create("div", "compact-state", "暂无涉及此主题的提问。完成一次召回后会在这里出现。"));
        return;
      }
      renderQueryCards(payload.items, container, { compact: true });
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        container.replaceChildren(create("div", "state-panel state-panel--error compact-state", `${errorMessage(error)} 可切换到“提问记录”重试。`));
      }
    }
  }

  async function selectDetailTab(tab, options = {}) {
    const next = DETAIL_TABS.includes(tab) ? tab : "overview";
    state.detail.tab = next;
    document.querySelectorAll("[data-detail-tab]").forEach((button) => {
      const active = button.dataset.detailTab === next;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll("[data-detail-panel]").forEach((panel) => {
      setHidden(panel, panel.dataset.detailPanel !== next);
    });
    if (options.updateHash !== false) {
      history.replaceState(null, "", `#topic/${encodeURIComponent(state.detail.topicId)}/${next}`);
    }
    if (next === "questions" && !state.detail.questionsLoaded) {
      await loadTopicQuestions(1);
    } else if (next === "body" && !state.detail.body) {
      await loadTopicBody();
    } else if (next === "versions" && !state.detail.versionsLoaded) {
      await loadVersions(1);
    }
  }

  async function loadTopicQuestions(page = state.detail.questionsPage) {
    const topicId = state.detail.topicId;
    state.detail.questionsPage = page;
    state.detail.questionsLoaded = false;
    showInlineState(byId("topic-question-state"), "LOADING / QUESTIONS", "正在读取主题提问记录…");
    clear(byId("topic-question-list"));
    clear(byId("topic-question-pagination"));
    try {
      const payload = await api(`/api/topics/${encodeURIComponent(topicId)}/questions?page=${page}&page_size=${PAGE_SIZE}`);
      if (state.detail.topicId !== topicId) {
        return;
      }
      state.detail.questionsLoaded = true;
      byId("topic-question-count").textContent = `共 ${formatCount(payload.total)} 条`;
      if (!payload.items.length) {
        showInlineState(byId("topic-question-state"), "EMPTY / QUESTIONS", "暂无提问。通过召回实验室询问后，记录会出现在这里。");
        return;
      }
      setHidden(byId("topic-question-state"), true);
      renderQueryCards(payload.items, byId("topic-question-list"));
      renderPagination(byId("topic-question-pagination"), payload, (nextPage) => { void loadTopicQuestions(nextPage); });
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        showRetryState(byId("topic-question-state"), "ERROR / QUESTIONS", `${errorMessage(error)} 请检查服务后重试。`, "重试提问记录", () => { void loadTopicQuestions(page); });
      }
    }
  }

  async function loadTopicBody() {
    if (state.detail.body || !state.detail.topicId || state.detail.tab !== "body") {
      return;
    }
    const button = byId("load-topic-body");
    setButtonLoading(button, true, "读取中…", "重新读取正文");
    showInlineState(byId("topic-body-state"), "LOADING / BODY", "正在读取已鉴权规范正文…");
    setHidden(byId("topic-body-content"), true);
    const topicId = state.detail.topicId;
    try {
      const payload = await api(`/api/topics/${encodeURIComponent(topicId)}/body`);
      if (state.detail.topicId !== topicId) {
        return;
      }
      state.detail.body = payload;
      byId("topic-body-content").textContent = String(payload.body || "");
      setHidden(byId("topic-body-content"), false);
      showInlineState(byId("topic-body-state"), "READY / BODY", `已读取 v${formatCount(payload.version)} / 指纹 ${payload.fingerprint || "—"}`);
      button.textContent = "重新读取正文";
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        showRetryState(byId("topic-body-state"), "ERROR / BODY", `${errorMessage(error)} 正文未显示，请重试。`, "重试正文", () => { void loadTopicBody(); });
      }
    } finally {
      setButtonLoading(button, false, "读取中…", state.detail.body ? "重新读取正文" : "读取当前正文");
    }
  }

  async function loadVersions(page = state.detail.versionsPage) {
    const topicId = state.detail.topicId;
    state.detail.versionsPage = page;
    state.detail.versionsLoaded = false;
    showInlineState(byId("topic-version-state"), "LOADING / VERSIONS", "正在读取版本列表…");
    clear(byId("topic-version-list"));
    clear(byId("topic-version-pagination"));
    try {
      const payload = await api(`/api/topics/${encodeURIComponent(topicId)}/versions?page=${page}&page_size=${PAGE_SIZE}`);
      if (state.detail.topicId !== topicId) {
        return;
      }
      state.detail.versionsLoaded = true;
      state.detail.versions = payload.items.slice();
      byId("topic-version-count").textContent = `共 ${formatCount(payload.total)} 个版本`;
      if (!payload.items.length) {
        showInlineState(byId("topic-version-state"), "EMPTY / VERSIONS", "暂无版本记录。");
        populateCompareSelectors([]);
        return;
      }
      setHidden(byId("topic-version-state"), true);
      payload.items.forEach((version) => {
        const row = create("div", "version-row");
        const label = create("strong", "", `v${formatCount(version.version)}${version.current ? " / 当前" : ""}`);
        const fingerprint = create("code", "", version.fingerprint || "—");
        const button = create("button", "button", "查看正文");
        button.type = "button";
        button.addEventListener("click", () => { void loadVersionBody(version.version, button); });
        row.append(label, fingerprint, button);
        byId("topic-version-list").append(row);
      });
      populateCompareSelectors(payload.items);
      renderPagination(byId("topic-version-pagination"), payload, (nextPage) => { void loadVersions(nextPage); });
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        showRetryState(byId("topic-version-state"), "ERROR / VERSIONS", `${errorMessage(error)} 请重试版本列表。`, "重试版本", () => { void loadVersions(page); });
      }
    }
  }

  function populateCompareSelectors(versions) {
    [byId("compare-version-a"), byId("compare-version-b")].forEach((select, index) => {
      clear(select);
      versions.forEach((version, versionIndex) => {
        const option = create("option", "", `v${formatCount(version.version)}${version.current ? " / 当前" : ""}`);
        option.value = String(version.version);
        option.selected = index === 0 ? versionIndex === Math.min(1, versions.length - 1) : versionIndex === 0;
        select.append(option);
      });
      select.disabled = versions.length === 0;
    });
    byId("compare-versions").disabled = versions.length === 0;
  }

  async function readVersionBody(version) {
    const key = String(version);
    if (state.detail.versionBodies[key]) {
      return state.detail.versionBodies[key];
    }
    const payload = await api(`/api/topics/${encodeURIComponent(state.detail.topicId)}/versions/${encodeURIComponent(key)}/body`);
    state.detail.versionBodies[key] = payload;
    return payload;
  }

  async function loadVersionBody(version, button) {
    setButtonLoading(button, true, "读取中…", "查看正文");
    byId("topic-version-body").textContent = "正在读取所选版本…";
    try {
      const payload = await readVersionBody(version);
      byId("topic-version-body").textContent = String(payload.body || "");
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        byId("topic-version-body").textContent = `${errorMessage(error)} 请重新选择版本。`;
      }
    } finally {
      setButtonLoading(button, false, "读取中…", "查看正文");
    }
  }

  async function compareVersions() {
    const button = byId("compare-versions");
    const versionA = byId("compare-version-a").value;
    const versionB = byId("compare-version-b").value;
    if (!versionA || !versionB) {
      return;
    }
    setButtonLoading(button, true, "读取比较中…", "读取并比较");
    byId("topic-version-compare").replaceChildren(create("div", "compact-state", "正在读取两个版本…"));
    try {
      const first = await readVersionBody(versionA);
      const second = versionA === versionB ? first : await readVersionBody(versionB);
      const firstColumn = create("div", "compare-column");
      const secondColumn = create("div", "compare-column");
      const firstTitle = create("strong", "", `v${formatCount(first.version)}${first.current ? " / 当前" : ""}`);
      const secondTitle = create("strong", "", `v${formatCount(second.version)}${second.current ? " / 当前" : ""}`);
      const firstBody = create("pre", "", first.body || "");
      const secondBody = create("pre", "", second.body || "");
      firstColumn.append(firstTitle, firstBody);
      secondColumn.append(secondTitle, secondBody);
      byId("topic-version-compare").replaceChildren(firstColumn, secondColumn);
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        byId("topic-version-compare").replaceChildren(create("div", "state-panel state-panel--error compact-state", `${errorMessage(error)} 请重试比较。`));
      }
    } finally {
      setButtonLoading(button, false, "读取比较中…", "读取并比较");
    }
  }

  function closeTopicDetail() {
    const returnFocus = state.detail.returnFocus;
    setHash("home");
    window.requestAnimationFrame(() => {
      if (returnFocus && returnFocus.isConnected) {
        returnFocus.focus();
      } else {
        byId("topic-search").focus();
      }
    });
  }

  function continueAsking(event) {
    event.preventDefault();
    const question = byId("topic-ask-question").value;
    if (!question.trim()) {
      byId("topic-ask-question").focus();
      return;
    }
    state.recall.suggestedTopic = state.detail.topic ? {
      topic_id: state.detail.topic.topic_id,
      display_name: state.detail.topic.display_name,
    } : null;
    byId("recall-question").value = question;
    byId("manual-override").checked = false;
    setHidden(byId("manual-controls"), true);
    setHash("recall");
    window.requestAnimationFrame(() => byId("recall-form").requestSubmit());
  }

  async function submitRecall(event) {
    if (event) {
      event.preventDefault();
    }
    if (state.recall.running) {
      return;
    }
    const question = byId("recall-question").value;
    if (!question.trim()) {
      showInlineState(byId("recall-state"), "INPUT / REQUIRED", "请输入需要召回的问题。示例问题也会走同一提交路径。");
      byId("recall-question").focus();
      return;
    }
    const manual = byId("manual-override").checked;
    const selected = Array.from(byId("manual-topic-select").selectedOptions, (option) => option.value);
    if (manual && (selected.length < 1 || selected.length > 8 || new Set(selected).size !== selected.length)) {
      showInlineState(byId("recall-state"), "INPUT / MANUAL SCOPE", "仅测试手动覆盖必须选择 1–8 个不重复主题。未发送请求。");
      byId("manual-topic-select").focus();
      return;
    }
    const body = { question };
    if (manual) {
      body.manual_topics = selected.map((topicId) => ({ topic: topicId, reason: "Panel 仅测试手动覆盖" }));
    }
    state.recall.running = true;
    setButtonLoading(byId("recall-submit"), true, "召回中…", "运行召回");
    setHidden(byId("recall-output"), true);
    const suggestion = state.recall.suggestedTopic;
    const context = suggestion
      ? `当前界面上下文为 ${suggestion.display_name} / ${suggestion.topic_id}；Agent 仍会独立规划并可添加其他主题。`
      : "Agent 正在规划真实主题范围并执行召回。";
    showInlineState(byId("recall-state"), "LOADING / RECALL", context);
    try {
      const result = await api("/api/recall/run", { method: "POST", body });
      state.recall.previousQueryId = state.recall.queryId;
      renderRecallResult(result);
      state.recent.loaded = false;
      state.recall.suggestedTopic = null;
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        showRetryState(byId("recall-state"), "ERROR / RECALL", `${errorMessage(error)} 问题仍保留，可直接重试。`, "重试召回", () => { byId("recall-form").requestSubmit(); });
      }
    } finally {
      state.recall.running = false;
      setButtonLoading(byId("recall-submit"), false, "召回中…", "运行召回");
    }
  }

  function renderRecallResult(result) {
    state.recall.result = result;
    state.recall.queryId = String(result.query_id || "");
    byId("recall-query-id").textContent = state.recall.queryId || "—";
    byId("recall-previous-query-id").textContent = state.recall.previousQueryId || "—";
    byId("recall-plan-mode").textContent = result.plan && result.plan.mode === "manual" ? "仅测试手动范围 / 已绕过 Agent" : result.plan && result.plan.mode === "rerun" ? "历史范围 / 当前版本重跑" : "Agent 实际规划";
    clear(byId("recall-plan"));
    const plannedTopics = result.plan && Array.isArray(result.plan.topics) ? result.plan.topics : [];
    plannedTopics.forEach((topic) => byId("recall-plan").append(topicChip(topic.display_name || topic.topic_id, topic.reason || "未提供理由")));
    clear(byId("recall-topic-results"));
    const topicResults = Array.isArray(result.topic_results) ? result.topic_results : [];
    topicResults.forEach((item) => {
      const card = create("article", "topic-result");
      const header = create("div", "result-header");
      const title = create("h3", "", `${displayNameFor(item.topic_id)} / v${formatCount(item.topic_version)}`);
      header.append(title, statusLabel(item.status));
      const source = create("p", "result-meta", `缓存 ${item.cache_status === "hit" ? "命中" : "未命中"} / 来源 ${item.source || "unknown"} / ${formatCount(item.latency_ms)} ms`);
      const reason = create("p", "result-meta", `选择理由：${item.reason || "未提供"}`);
      const answer = create("div", "answer-text", item.answer || failureText(item) || "未返回事实胶囊。");
      card.append(header, source, reason, answer);
      byId("recall-topic-results").append(card);
    });
    const finalSource = result.final && result.final.source ? result.final.source : result.synthesis_source;
    byId("recall-final-source").textContent = `状态 ${statusText(result.status)} / 综合来源 ${finalSource || "无"}`;
    byId("recall-final-answer").textContent = result.final_answer || failureText(result) || "没有可综合的已回答主题。";
    renderLatency(result.latency_ms || {});
    showInlineState(byId("recall-state"), result.status === "answered" ? "READY / ANSWERED" : result.status === "no_fact" ? "READY / NO FACT" : "READY / ERROR", `Query ID ${state.recall.queryId || "未知"} 已完成；以下全部来自本次真实响应。`);
    setHidden(byId("recall-output"), false);
  }

  function renderLatency(latency) {
    clear(byId("recall-latency"));
    const topicLatency = latency.topics && typeof latency.topics === "object"
      ? Object.entries(latency.topics).map(([topic, value]) => `${topic} ${formatCount(value)} ms`).join(" / ") || "0 ms"
      : "0 ms";
    [
      ["规划", `${formatCount(latency.planning)} ms`],
      ["召回", `${formatCount(latency.recall)} ms`],
      ["逐主题", topicLatency],
      ["综合", `${formatCount(latency.synthesis)} ms`],
      ["总计", `${formatCount(latency.total)} ms`],
    ].forEach(([label, value]) => {
      const wrapper = create("div");
      wrapper.append(create("dt", "", label), create("dd", "", value));
      byId("recall-latency").append(wrapper);
    });
  }

  async function rerunRecall() {
    if (!state.recall.queryId || state.recall.running) {
      return;
    }
    await rerunQuery(state.recall.queryId, byId("rerun-recall"));
  }

  async function rerunQuery(queryId, button) {
    const idleLabel = button.textContent;
    state.recall.running = true;
    setButtonLoading(button, true, "重跑中…", idleLabel);
    try {
      const result = await api(`/api/recall/rerun/${encodeURIComponent(queryId)}`, { method: "POST", body: {} });
      state.recall.previousQueryId = queryId;
      renderRecallResult(result);
      state.recent.loaded = false;
      setHash("recall");
      window.requestAnimationFrame(() => byId("recall-title").focus());
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        switchView("recall");
        showRetryState(byId("recall-state"), "ERROR / RERUN", `${errorMessage(error)} 原 Query ID ${queryId} 仍保留。`, "重试重跑", () => { void rerunQuery(queryId, byId("rerun-recall")); });
      }
    } finally {
      state.recall.running = false;
      setButtonLoading(button, false, "重跑中…", idleLabel);
    }
  }

  async function loadRecentQuestions(page = state.recent.page) {
    state.recent.page = page;
    state.recent.loaded = false;
    showInlineState(byId("recent-question-state"), "LOADING / HISTORY", "正在读取全局提问历史…");
    clear(byId("recent-question-list"));
    clear(byId("recent-pagination"));
    const params = new URLSearchParams({ page: String(page), page_size: String(state.recent.pageSize) });
    const fields = {
      topic: byId("recent-topic-filter").value,
      scope: byId("recent-scope-filter").value,
      cache: byId("recent-cache-filter").value,
      status: byId("recent-status-filter").value,
      source: byId("recent-source-filter").value,
      date_from: byId("recent-date-from").value,
      date_to: byId("recent-date-to").value,
    };
    if (fields.date_from && fields.date_to && fields.date_from > fields.date_to) {
      showInlineState(byId("recent-question-state"), "INPUT / DATE RANGE", "起始日期不能晚于结束日期。请修正后重新应用筛选。" );
      return;
    }
    Object.entries(fields).forEach(([name, value]) => {
      if (value) {
        params.set(name, value);
      }
    });
    try {
      const payload = await api(`/api/questions?${params.toString()}`);
      state.recent.loaded = true;
      state.recent.total = Number(payload.total) || 0;
      if (!payload.items.length) {
        showInlineState(byId("recent-question-state"), "EMPTY / HISTORY", "当前筛选没有记录。通过 Hermes、Panel 或 CLI 完成一次召回后，原问题与结果会出现在这里；也可清除筛选。" );
        return;
      }
      setHidden(byId("recent-question-state"), true);
      renderQueryCards(payload.items, byId("recent-question-list"));
      renderPagination(byId("recent-pagination"), payload, (nextPage) => { void loadRecentQuestions(nextPage); });
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        showRetryState(byId("recent-question-state"), "ERROR / HISTORY", `${errorMessage(error)} 筛选条件已保留；检查服务后重试。`, "重试历史", () => { void loadRecentQuestions(page); });
      }
    }
  }

  function renderQueryCards(items, container, options = {}) {
    clear(container);
    items.forEach((record) => {
      const card = create("article", "query-card");
      const header = create("div", "query-card-header");
      const time = create("time", "", formatTime(record.created_at));
      time.dateTime = String(record.created_at || "");
      const overallStatus = queryStatus(record);
      header.append(time, statusLabel(overallStatus));
      const question = create("p", "query-question", record.question || "（无问题文本）");
      const meta = create("p", "query-meta", `${record.scope === "multi" ? "多主题" : "单主题"} / 来源 ${record.source || "unknown"} / ${formatCount(record.total_latency_ms)} ms / Query ${record.query_id || "—"}`);
      const chips = create("div", "chip-list");
      const results = Array.isArray(record.selected_topics) ? record.selected_topics : [];
      results.forEach((item) => {
        const cache = item.source === "cache" ? "cache hit" : "cache miss";
        chips.append(topicChip(item.topic_id, `v${formatCount(item.topic_version)} / ${statusText(item.status)} / ${cache} / ${item.source || "unknown"}`));
      });
      const answer = create("div", "query-answer", record.final_answer || "没有最终答案。");
      card.append(header, question, meta, chips, answer);
      if (!options.compact) {
        const actions = create("div", "query-actions");
        const rerun = create("button", "button", "按当前版本重跑");
        rerun.type = "button";
        rerun.addEventListener("click", () => { void rerunQuery(record.query_id, rerun); });
        actions.append(rerun);
        card.append(actions);
      }
      container.append(card);
    });
  }

  function renderPagination(container, payload, onPage) {
    clear(container);
    const page = Number(payload.page) || 1;
    const pageSize = Number(payload.page_size) || PAGE_SIZE;
    const total = Number(payload.total) || 0;
    const pages = Math.max(1, Math.ceil(total / pageSize));
    const previous = create("button", "button button--quiet", "上一页");
    previous.type = "button";
    previous.disabled = page <= 1;
    previous.addEventListener("click", () => onPage(page - 1));
    const summary = create("p", "", `第 ${formatCount(page)} / ${formatCount(pages)} 页 · 共 ${formatCount(total)} 条`);
    const next = create("button", "button button--quiet", "下一页");
    next.type = "button";
    next.disabled = page >= pages;
    next.addEventListener("click", () => onPage(page + 1));
    container.append(previous, summary, next);
  }

  function renderMetadata(container, entries) {
    const list = create("dl", "metadata-grid");
    entries.forEach(([label, value]) => {
      const item = create("div", "metadata-item");
      item.append(create("dt", "", label), create("dd", "", value ?? "—"));
      list.append(item);
    });
    container.replaceChildren(...list.childNodes);
  }

  function topicChip(topic, reason = "") {
    const chip = create("span", "topic-chip");
    chip.append(create("strong", "", topic));
    if (reason) {
      chip.append(create("small", "", reason));
    }
    return chip;
  }

  function statusLabel(status, override = "") {
    const normalized = ["answered", "no_fact", "error"].includes(status) ? status : "error";
    const label = create("span", `status-label status-label--${normalized}`);
    const dot = create("span", `status-dot ${normalized === "answered" ? "status-dot--ok" : normalized === "no_fact" ? "status-dot--warn" : "status-dot--bad"}`);
    dot.setAttribute("aria-hidden", "true");
    label.append(dot, textNode(override || statusText(normalized)));
    return label;
  }

  function showInlineState(element, kicker, message) {
    const marker = create("span", "state-kicker", kicker);
    const text = create("p", "", message);
    element.replaceChildren(marker, text);
    element.classList.remove("state-panel--error");
    setHidden(element, false);
  }

  function showRetryState(element, kicker, message, label, retry) {
    showInlineState(element, kicker, message);
    element.classList.add("state-panel--error");
    const actions = create("div", "state-actions");
    const button = create("button", "button", label);
    button.type = "button";
    button.addEventListener("click", retry);
    actions.append(button);
    element.append(actions);
  }

  function switchView(viewName) {
    const next = PRIMARY_VIEWS.includes(viewName) ? viewName : "home";
    const leavingDetail = state.view === "detail" && next !== "detail";
    state.view = next;
    document.querySelectorAll("[data-panel]").forEach((panel) => {
      setHidden(panel, panel.dataset.panel !== next);
    });
    document.querySelectorAll("[data-view]").forEach((link) => {
      const active = link.dataset.view === next || (next === "detail" && link.dataset.view === "home");
      link.classList.toggle("is-active", active);
      if (active) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
    if (leavingDetail && next === "home") {
      const returnFocus = state.detail.returnFocus;
      window.requestAnimationFrame(() => {
        if (returnFocus && returnFocus.isConnected) {
          returnFocus.focus();
        }
      });
    }
  }

  function parseHash() {
    const raw = location.hash.replace(/^#/, "");
    if (!raw) {
      return { view: "home" };
    }
    const parts = raw.split("/");
    if (parts[0] === "topic" && parts.length >= 2) {
      let topicId;
      try {
        topicId = decodeURIComponent(parts[1]);
      } catch (_error) {
        return { view: "home", invalid: true };
      }
      if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(topicId)) {
        return { view: "home", invalid: true };
      }
      return { view: "detail", topicId, tab: DETAIL_TABS.includes(parts[2]) ? parts[2] : "overview" };
    }
    if (["home", "recall", "recent", "recycle"].includes(parts[0]) && parts.length === 1) {
      return { view: parts[0] };
    }
    return { view: "home", invalid: true };
  }

  async function routeFromHash(options = {}) {
    const route = parseHash();
    if (route.invalid && options.replaceInvalid !== false) {
      setHash("home", true);
    }
    if (!state.token) {
      return;
    }
    if (route.view === "detail") {
      await openTopicDetail(route.topicId, route.tab);
      return;
    }
    switchView(route.view);
    if (route.view === "recent" && !state.recent.loaded) {
      await loadRecentQuestions(1);
    }
    if (route.view === "recall") {
      window.requestAnimationFrame(() => byId("recall-title").focus());
    } else if (route.view === "recent") {
      window.requestAnimationFrame(() => byId("questions-title").focus());
    }
  }

  function setHash(value, replace = false) {
    const next = `#${value}`;
    if (location.hash === next) {
      void routeFromHash({ replaceInvalid: false });
      return;
    }
    if (replace) {
      history.replaceState(null, "", next);
      void routeFromHash({ replaceInvalid: false });
    } else {
      location.hash = value;
    }
  }

  function clearSession() {
    sessionStorage.removeItem(SESSION_KEY);
    byId("session-token").value = "";
    showSessionGate("会话已从浏览器中清除。\n");
  }

  function queryStatus(record) {
    if (record.final_answer) {
      return "answered";
    }
    const results = Array.isArray(record.selected_topics) ? record.selected_topics : [];
    return results.some((item) => item.status === "error") ? "error" : "no_fact";
  }

  function statusText(status) {
    return status === "answered" ? "已回答" : status === "no_fact" ? "无事实" : status === "error" ? "错误" : String(status || "未知");
  }

  function failureText(value) {
    return value && value.failure && typeof value.failure.message === "string" ? value.failure.message : "";
  }

  function errorMessage(error) {
    return error instanceof ApiError ? error.message : "请求失败。";
  }

  function displayNameFor(topicId) {
    const topic = state.topics.find((item) => item.topic_id === topicId);
    return topic ? `${topic.display_name} / ${topicId}` : topicId;
  }

  function aliasText(aliases) {
    return Array.isArray(aliases) && aliases.length ? aliases.join("、") : "无";
  }

  function appendCell(row, value, header = false) {
    const cell = create(header ? "th" : "td", "", value);
    if (header) {
      cell.scope = "row";
    }
    row.append(cell);
  }

  function svgElement(name, attributes) {
    const element = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
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

  byId("session-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void connectSession(byId("session-token").value);
  });
  byId("retry-overview").addEventListener("click", () => { void refreshOverview(); });
  byId("refresh-empty").addEventListener("click", () => { void refreshOverview(); });
  byId("refresh-overview").addEventListener("click", () => { void refreshOverview(); });
  byId("clear-session").addEventListener("click", clearSession);
  byId("reset-session").addEventListener("click", clearSession);
  byId("topic-search").addEventListener("input", renderCatalog);
  byId("topic-status-filter").addEventListener("change", renderCatalog);
  byId("topic-sort").addEventListener("change", renderCatalog);
  byId("close-topic-detail").addEventListener("click", closeTopicDetail);
  byId("retry-topic-detail").addEventListener("click", () => {
    state.detail.topic = null;
    void openTopicDetail(state.detail.topicId, state.detail.tab);
  });
  byId("load-topic-body").addEventListener("click", () => {
    state.detail.body = null;
    void loadTopicBody();
  });
  byId("compare-versions").addEventListener("click", () => { void compareVersions(); });
  byId("topic-ask-form").addEventListener("submit", continueAsking);
  byId("recall-form").addEventListener("submit", (event) => { void submitRecall(event); });
  byId("manual-override").addEventListener("change", () => {
    setHidden(byId("manual-controls"), !byId("manual-override").checked);
  });
  byId("rerun-recall").addEventListener("click", () => { void rerunRecall(); });
  byId("recent-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    void loadRecentQuestions(1);
  });
  byId("reset-recent-filters").addEventListener("click", () => {
    byId("recent-filters").reset();
    void loadRecentQuestions(1);
  });
  byId("recall-examples").querySelectorAll("[data-example]").forEach((button) => {
    button.addEventListener("click", () => {
      byId("recall-question").value = button.dataset.example || "";
      byId("recall-form").requestSubmit();
    });
  });
  document.querySelectorAll("[data-detail-tab]").forEach((button) => {
    button.addEventListener("click", () => { void selectDetailTab(button.dataset.detailTab); });
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        return;
      }
      event.preventDefault();
      const buttons = Array.from(document.querySelectorAll("[data-detail-tab]"));
      const index = buttons.indexOf(button);
      const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : event.key === "ArrowRight" ? (index + 1) % buttons.length : (index - 1 + buttons.length) % buttons.length;
      buttons[nextIndex].focus();
      void selectDetailTab(buttons[nextIndex].dataset.detailTab);
    });
  });
  document.querySelectorAll("[data-view]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      setHash(link.dataset.view || "home");
    });
  });
  window.addEventListener("hashchange", () => { void routeFromHash({ replaceInvalid: true }); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.view === "detail") {
      event.preventDefault();
      closeTopicDetail();
    }
  });

  switchView("home");
  const savedToken = sessionStorage.getItem(SESSION_KEY);
  if (savedToken) {
    void connectSession(savedToken, true);
  } else {
    showSessionGate();
  }
})();
