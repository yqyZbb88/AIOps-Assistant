"use strict";
/* 控制台是公开静态空壳；所有运维数据和变更必须经过同源 API。
 * Token 只放 sessionStorage（标签页关闭即失效），浏览器持久化只保存非敏感偏好。
 * DOM 创建统一走 node()/textContent，禁止把 API 文本拼成 HTML。
 */

const TOKEN_KEY = "aiops_console_token";
const PREF_KEY = "aiops_console_preferences_v1";
const VIEW_META = {
  overview: ["OPERATIONS OVERVIEW", "运行总览"],
  hosts: ["HOSTS & METRICS", "主机详情"],
  assistant: ["ASSISTANT", "问答与诊断"],
  approvals: ["HUMAN IN THE LOOP", "人工审批"],
  records: ["AUDIT & HISTORY", "运维记录"],
  admin: ["SYSTEM ADMINISTRATION", "系统管理"],
  settings: ["PREFERENCES", "个性化设置"],
};
const METRICS = [
  ["cpu.usage", "CPU 使用率", "%"], ["cpu.load1", "1 分钟负载", ""],
  ["cpu.iowait", "CPU I/O 等待", "%"], ["memory.usage", "内存使用率", "%"],
  ["memory.available", "可用内存", "B"], ["memory.swap_usage", "Swap 使用率", "%"],
  ["disk.usage", "磁盘使用率", "%"], ["disk.read_bytes", "磁盘读取", "B/s"],
  ["disk.write_bytes", "磁盘写入", "B/s"], ["disk.io_util", "磁盘 I/O 利用率", "%"],
  ["network.rx_bytes", "网络接收", "B/s"], ["network.tx_bytes", "网络发送", "B/s"],
  ["network.rx_errors", "网络接收错误", ""], ["network.tx_errors", "网络发送错误", ""],
  ["system.process_count", "进程数量", ""], ["system.uptime", "运行时间", "s"],
  ["alerts.active_count", "活动告警", ""],
].map(function (item) { return {id: item[0], label: item[1], unit: item[2]}; });
const METRIC_MAP = new Map(METRICS.map(function (metric) { return [metric.id, metric]; }));
const WIDGETS = [
  {id: "hosts", label: "主机状态"}, {id: "attention", label: "需要关注"},
  {id: "trends", label: "收藏指标趋势"}, {id: "recent", label: "最近运维动态"},
];
const DEFAULT_PREFS = Object.freeze({
  theme: "system", density: "comfortable", sidebarCollapsed: false,
  startView: "overview", defaultHost: "", refreshSeconds: 30, trendMinutes: 60,
  favoriteMetrics: ["cpu.usage", "memory.usage", "disk.usage"],
  widgetOrder: ["hosts", "attention", "trends", "recent"], hiddenWidgets: [],
});
// 单页视图共享的内存状态：host_id、session_id、审批 id 均来自服务端，不接受自由目标地址。
const state = {
  prefs: loadPreferences(), view: "overview", hosts: [], hostStatuses: new Map(),
  selectedHostId: "", sessions: [], selectedSessionId: "", approvals: [],
  history: {alerts: [], remediations: [], advice: []}, inspections: [], executions: [],
  health: null, connected: false, busy: new Set(), refreshTimer: null,
  confirmAction: null, selectedMetricId: "cpu.usage",
  isAdmin: false, admin: null, adminDirty: false,
  adminSection: "general", adminCatalog: "rule_actions",
  hostOptions: null, hostVerification: "",
};
const $ = function (id) { return document.getElementById(id); };

function cloneDefaults() {
  return {
    theme: DEFAULT_PREFS.theme, density: DEFAULT_PREFS.density,
    sidebarCollapsed: DEFAULT_PREFS.sidebarCollapsed, startView: DEFAULT_PREFS.startView,
    defaultHost: DEFAULT_PREFS.defaultHost, refreshSeconds: DEFAULT_PREFS.refreshSeconds,
    trendMinutes: DEFAULT_PREFS.trendMinutes,
    favoriteMetrics: DEFAULT_PREFS.favoriteMetrics.slice(),
    widgetOrder: DEFAULT_PREFS.widgetOrder.slice(), hiddenWidgets: [],
  };
}

function normalizePreferences(raw) {
  // localStorage 可被用户手工改坏；只恢复已知枚举、范围和指标 ID，其余回到安全默认值。
  const next = cloneDefaults();
  if (!raw || typeof raw !== "object") return next;
  if (["system", "dark", "light"].includes(raw.theme)) next.theme = raw.theme;
  if (["comfortable", "compact"].includes(raw.density)) next.density = raw.density;
  if (typeof raw.sidebarCollapsed === "boolean") next.sidebarCollapsed = raw.sidebarCollapsed;
  if (Object.hasOwn(VIEW_META, raw.startView) && !["settings", "admin"].includes(raw.startView)) next.startView = raw.startView;
  if (typeof raw.defaultHost === "string" && raw.defaultHost.length <= 128) next.defaultHost = raw.defaultHost;
  if ([0, 15, 30, 60, 300].includes(Number(raw.refreshSeconds))) next.refreshSeconds = Number(raw.refreshSeconds);
  if ([15, 60, 360, 1440].includes(Number(raw.trendMinutes))) next.trendMinutes = Number(raw.trendMinutes);
  if (Array.isArray(raw.favoriteMetrics)) {
    const favorites = Array.from(new Set(raw.favoriteMetrics.filter(function (id) { return METRIC_MAP.has(id); }))).slice(0, 6);
    if (favorites.length) next.favoriteMetrics = favorites;
  }
  if (Array.isArray(raw.widgetOrder)) {
    const valid = Array.from(new Set(raw.widgetOrder.filter(function (id) { return WIDGETS.some(function (item) { return item.id === id; }); })));
    WIDGETS.forEach(function (item) { if (!valid.includes(item.id)) valid.push(item.id); });
    next.widgetOrder = valid;
  }
  if (Array.isArray(raw.hiddenWidgets)) {
    next.hiddenWidgets = Array.from(new Set(raw.hiddenWidgets.filter(function (id) { return WIDGETS.some(function (item) { return item.id === id; }); })));
  }
  return next;
}

function loadPreferences() {
  try { return normalizePreferences(JSON.parse(localStorage.getItem(PREF_KEY) || "null")); }
  catch (_) { return cloneDefaults(); }
}

function savePreferences() {
  localStorage.setItem(PREF_KEY, JSON.stringify(state.prefs));
  applyPreferences();
}

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = String(text);
  return item;
}

function replace(id, child) {
  $(id).replaceChildren(child);
}

function empty(text) { return node("div", "empty-state", text); }

function formatTime(value) {
  if (!value) return "时间未知";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString();
}

function formatValue(value, unit) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (unit === "%") return number.toFixed(1) + "%";
  if (unit === "B" || unit === "B/s") {
    const suffix = unit === "B/s" ? "/s" : "";
    if (Math.abs(number) >= 1073741824) return (number / 1073741824).toFixed(1) + " GiB" + suffix;
    if (Math.abs(number) >= 1048576) return (number / 1048576).toFixed(1) + " MiB" + suffix;
    if (Math.abs(number) >= 1024) return (number / 1024).toFixed(1) + " KiB" + suffix;
  }
  if (unit === "s") {
    if (number >= 86400) return (number / 86400).toFixed(1) + " 天";
    if (number >= 3600) return (number / 3600).toFixed(1) + " 小时";
  }
  return number.toLocaleString(undefined, {maximumFractionDigits: 2}) + (unit ? " " + unit : "");
}

function safeDetail(value) {
  if (value === null || value === undefined || value === "") return "";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function statusTone(value) {
  const text = String(value || "").toLowerCase();
  if (["ok", "up", "reachable", "running", "succeeded", "completed", "final"].includes(text)) return "good";
  if (["failed", "error", "down", "unreachable", "query_error", "critical", "unknown"].includes(text)) return "bad";
  if (["pending", "pending_approval", "warning", "no_data", "disabled"].includes(text)) return "warn";
  return "neutral";
}

function badge(text, value) { return node("span", "badge " + statusTone(value), text); }

async function request(path, options) {
  // 统一请求边界：只用相对路径、默认带 Bearer、禁缓存且有超时；401/403 留给调用方展示。
  const opts = options || {};
  const authenticated = opts.authenticated !== false;
  const token = sessionStorage.getItem(TOKEN_KEY);
  if (authenticated && !token) throw new Error("请先连接 Assistant");
  const controller = new AbortController();
  const timer = setTimeout(function () { controller.abort(); }, opts.timeout || 15000);
  const headers = {Accept: "application/json"};
  if (authenticated) headers.Authorization = "Bearer " + token;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  try {
    const response = await fetch(path, {
      method: opts.method || "GET", headers: headers, cache: "no-store",
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body), signal: controller.signal,
    });
    let data = null;
    try { data = await response.json(); } catch (_) { data = {detail: "响应不是 JSON"}; }
    if (!response.ok) {
      const error = new Error(String(response.status) + " " + (data.detail || response.statusText));
      error.status = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("请求超时，请检查服务或网络状态");
    throw error;
  } finally { clearTimeout(timer); }
}

function showNotice(text, tone) {
  $("notice-text").textContent = text;
  $("notice").className = "notice " + (tone || "");
}

function hideNotice() { $("notice").className = "notice hidden"; }

function setBusy(key, active) {
  if (active) state.busy.add(key); else state.busy.delete(key);
  updateControls();
}

function applyPreferences() {
  document.documentElement.dataset.theme = state.prefs.theme;
  document.documentElement.dataset.density = state.prefs.density;
  document.querySelector(".app-shell").classList.toggle("sidebar-collapsed", state.prefs.sidebarCollapsed);
  $("sidebar-toggle").textContent = state.prefs.sidebarCollapsed ? "›" : "‹";
  $("sidebar-toggle").setAttribute("aria-label", state.prefs.sidebarCollapsed ? "展开导航" : "收起导航");
  const grid = document.querySelector(".dashboard-grid");
  state.prefs.widgetOrder.forEach(function (id) {
    const widget = grid.querySelector('[data-widget="' + id + '"]');
    if (widget) {
      widget.hidden = state.prefs.hiddenWidgets.includes(id);
      grid.append(widget);
    }
  });
  $("overview-range").textContent = "最近 " + rangeLabel(state.prefs.trendMinutes);
  syncSettingsForm();
  scheduleRefresh();
}

function rangeLabel(minutes) {
  if (minutes < 60) return minutes + " 分钟";
  if (minutes === 60) return "1 小时";
  return (minutes / 60) + " 小时";
}

function scheduleRefresh() {
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = null;
  if (state.prefs.refreshSeconds > 0) {
    state.refreshTimer = setInterval(function () {
      if (state.connected && state.busy.size === 0 && !document.hidden) refreshAll(false);
    }, state.prefs.refreshSeconds * 1000);
  }
}

function navigate(view) {
  if (!Object.hasOwn(VIEW_META, view)) view = "overview";
  if (view === "admin" && !state.isAdmin) view = "overview";
  state.view = view;
  document.querySelectorAll("[data-view-panel]").forEach(function (panel) {
    const active = panel.dataset.viewPanel === view;
    panel.hidden = !active;
    panel.classList.toggle("active", active);
  });
  document.querySelectorAll("[data-view]").forEach(function (button) {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  $("view-eyebrow").textContent = VIEW_META[view][0];
  $("view-title").textContent = VIEW_META[view][1];
  $("session-menu").classList.add("hidden");
  if (view === "hosts" && state.connected) loadSelectedTrend();
  if (view === "settings") renderSettings();
  if (view === "admin") renderAdmin();
}

function updateConnection(status, label) {
  state.connected = status === "online";
  $("connection-dot").className = "connection-dot " + status;
  $("connection-label").textContent = label;
  $("auth-state").textContent = status === "online" ? "已连接" : "连接";
  updateControls();
}

function updateControls() {
  const hostReady = state.connected && Boolean(state.selectedHostId);
  const sessionReady = hostReady && Boolean(state.selectedSessionId);
  const sending = state.busy.has("chat");
  $("global-host").disabled = !state.connected || state.busy.has("refresh");
  $("new-session").disabled = !hostReady;
  $("session-menu-button").disabled = !sessionReady;
  $("chat-message").disabled = !sessionReady || sending;
  $("send-message").disabled = !sessionReady || sending;
  $("favorite-metric").disabled = !hostReady;
  if ($("admin-save")) {
    $("admin-save").disabled = !state.isAdmin || !state.adminDirty || state.busy.has("admin");
    $("admin-reload").disabled = !state.isAdmin || state.busy.has("admin");
    $("admin-apply").disabled = !state.isAdmin || state.adminDirty || !state.admin || !state.admin.restart_required || state.busy.has("admin");
    document.querySelectorAll("#admin-content input, #admin-content select, #admin-content textarea, #admin-content button").forEach(function (control) {
      control.disabled = state.busy.has("admin");
    });
  }
  document.querySelectorAll(".diagnostic-prompt").forEach(function (button) {
    button.disabled = !hostReady || sending;
  });
}

function currentHost() {
  return state.hosts.find(function (host) { return host.host_id === state.selectedHostId; }) || null;
}

function currentSession() {
  return state.sessions.find(function (item) { return item.session_id === state.selectedSessionId; }) || null;
}

async function loadHealth() {
  try { state.health = await request("/ready", {authenticated: false, timeout: 8000}); }
  catch (error) { state.health = {ready: false, error: error.message, components: {}}; }
}

async function loadHosts() {
  state.hosts = await request("/hosts?include_disabled=true");
  const enabled = state.hosts.filter(function (host) { return host.enabled; });
  const candidates = [state.selectedHostId, state.prefs.defaultHost, enabled[0] && enabled[0].host_id];
  state.selectedHostId = candidates.find(function (id) {
    return id && enabled.some(function (host) { return host.host_id === id; });
  }) || "";
  renderHostSelectors();
}

async function loadSessions() {
  state.sessions = await request("/sessions?limit=100");
  if (state.selectedSessionId && !state.sessions.some(function (item) { return item.session_id === state.selectedSessionId; })) {
    state.selectedSessionId = "";
  }
  const hostSessions = state.sessions.filter(function (item) { return item.host_id === state.selectedHostId; });
  if (!state.selectedSessionId && hostSessions.length) state.selectedSessionId = hostSessions[0].session_id;
  renderSessions();
  await loadConversation();
}

async function loadApprovals() {
  state.approvals = await request("/approvals");
  renderApprovals();
}

async function loadAdminAccess() {
  try {
    const snapshot = await request("/admin/config", {timeout: 20000});
    const refreshDraft = !state.adminDirty || !state.admin;
    if (refreshDraft) {
      if (!state.admin || snapshot.revision !== state.admin.revision) {
        state.hostOptions = null; state.hostVerification = "";
      }
      state.admin = snapshot;
    }
    state.isAdmin = true;
    $("admin-nav").classList.remove("hidden");
    if (refreshDraft) renderAdmin();
    else if (snapshot.revision !== state.admin.revision) {
      $("admin-summary").textContent = "磁盘配置已变化；当前草稿保存时将发生冲突，请先重载";
    }
  } catch (error) {
    state.isAdmin = false;
    state.admin = null;
    state.adminDirty = false;
    $("admin-nav").classList.add("hidden");
    clearAdminView();
    if (state.view === "admin") navigate("overview");
    if (error.status !== 403) throw error;
  }
}

function clearAdminView() {
  state.hostOptions = null; state.hostVerification = "";
  $("admin-content").classList.add("hidden");
  $("admin-restart-banner").classList.add("hidden");
  $("admin-summary").classList.add("empty-state");
  $("admin-summary").classList.remove("admin-dirty");
  $("admin-summary").textContent = "管理员鉴权成功后加载配置";
  ["admin-security", "admin-automation", "admin-rules", "admin-catalog-rule_actions",
    "admin-catalog-agent_actions", "admin-catalog-diagnostics", "admin-hosts"].forEach(function (id) { $(id).replaceChildren(); });
  ["admin-monitor-existing", "admin-host-target", "admin-host-credential",
    "admin-host-diagnostic-checks"].forEach(function (id) { $(id).replaceChildren(); });
  $("admin-host-result").textContent = "";
  $("admin-monitor-mode").textContent = "";
}

async function loadHostStatus() {
  if (!state.selectedHostId) return;
  try {
    const status = await request("/hosts/" + encodeURIComponent(state.selectedHostId) + "/status");
    state.hostStatuses.set(state.selectedHostId, status);
  } catch (error) {
    state.hostStatuses.set(state.selectedHostId, {exporter: {state: "query_error"}, metrics: {}, active_alerts: [], error: error.message});
  }
  renderHosts();
  renderHostDetail();
}

async function loadAllHostStatuses() {
  await Promise.all(state.hosts.filter(function (host) { return host.enabled; }).map(async function (host) {
    try {
      const status = await request("/hosts/" + encodeURIComponent(host.host_id) + "/status");
      state.hostStatuses.set(host.host_id, status);
    } catch (error) {
      state.hostStatuses.set(host.host_id, {exporter: {state: "query_error"}, metrics: {}, active_alerts: [], error: error.message});
    }
  }));
  renderHosts();
  renderHostDetail();
}

async function loadRecords() {
  const limit = Number($("record-limit").value || 50);
  const suffix = state.selectedHostId ? "&host_id=" + encodeURIComponent(state.selectedHostId) : "";
  const results = await Promise.all([
    request("/alerts/history?limit=" + limit + suffix),
    request("/inspections/recent?limit=" + limit + suffix),
    request("/executions?limit=" + limit + suffix),
  ]);
  state.history = results[0];
  state.inspections = results[1].inspections || [];
  state.executions = results[2].executions || [];
  renderRecords();
}

async function refreshAll(showMessage) {
  // 自动刷新不应覆盖管理员正在编辑的草稿；loadAdminAccess 会保留 dirty 草稿并提示 revision 冲突。
  if (state.busy.has("refresh")) return;
  setBusy("refresh", true);
  await loadHealth();
  if (!sessionStorage.getItem(TOKEN_KEY)) {
    state.isAdmin = false; state.admin = null; state.adminDirty = false;
    state.hostOptions = null; state.hostVerification = "";
    $("admin-nav").classList.add("hidden"); clearAdminView();
    if (state.view === "admin") navigate("overview");
    updateConnection("offline", "未连接");
    renderOverview();
    setBusy("refresh", false);
    return;
  }
  try {
    await loadHosts();
    await Promise.all([loadAllHostStatuses(), loadSessions(), loadApprovals(), loadRecords(), loadAdminAccess()]);
    updateConnection("online", "已连接");
    renderOverview();
    if (state.view === "hosts") await loadSelectedTrend();
    if (showMessage !== false) showNotice("数据已刷新 · " + new Date().toLocaleTimeString(), "success");
  } catch (error) {
    updateConnection("error", error.status === 401 ? "Token 无效" : "连接异常");
    showNotice(error.status === 401 ? "Token 无效或已失效，请重新连接。" : error.message, "error");
  } finally { setBusy("refresh", false); }
}

async function selectHost(hostId) {
  if (!state.hosts.some(function (host) { return host.host_id === hostId && host.enabled; })) return;
  state.selectedHostId = hostId;
  state.selectedSessionId = "";
  renderHostSelectors();
  renderHosts();
  setBusy("host", true);
  try {
    await Promise.all([loadHostStatus(), loadSessions(), loadRecords()]);
    renderOverview();
    if (state.view === "hosts") await loadSelectedTrend();
  } catch (error) { showNotice(error.message, "error"); }
  finally { setBusy("host", false); }
}

function renderHostSelectors() {
  const selectors = [$("global-host"), $("pref-default-host")];
  selectors.forEach(function (select, index) {
    const current = index === 0 ? state.selectedHostId : state.prefs.defaultHost;
    select.replaceChildren();
    if (index === 1) select.append(new Option("首台可用主机", ""));
    state.hosts.filter(function (host) { return host.enabled; }).forEach(function (host) {
      select.append(new Option(host.name + " · " + host.host_id, host.host_id));
    });
    select.value = current;
  });
  $("global-host").disabled = !state.connected && state.hosts.length === 0;
}

function metricValue(status, metricId) {
  const samples = status && status.metrics && status.metrics[metricId] && status.metrics[metricId].samples;
  if (!Array.isArray(samples) || !samples.length) return null;
  return Math.max.apply(null, samples.map(function (sample) { return Number(sample.value); }).filter(Number.isFinite));
}

function renderHosts() {
  $("host-count").textContent = String(state.hosts.length);
  const list = $("host-list");
  list.replaceChildren();
  if (!state.hosts.length) {
    list.append(empty("暂无登记主机"));
    return;
  }
  state.hosts.forEach(function (host) {
    const status = state.hostStatuses.get(host.host_id);
    const exporter = status && status.exporter ? status.exporter.state : (host.enabled ? "unknown" : "disabled");
    const button = node("button", "selection-item" + (host.host_id === state.selectedHostId ? " active" : ""));
    button.type = "button";
    button.disabled = !host.enabled;
    const copy = node("span", "selection-copy");
    copy.append(node("strong", "", host.name), node("span", "", host.host_id));
    button.append(copy, badge(exporter, exporter));
    button.addEventListener("click", function () { selectHost(host.host_id); });
    list.append(button);
  });
  list.classList.remove("empty");
}

function renderHostDetail() {
  const host = currentHost();
  const status = state.hostStatuses.get(state.selectedHostId);
  $("host-detail-title").textContent = host ? host.name + " · " + host.host_id : "请选择主机";
  const badges = $("host-detail-badges");
  badges.replaceChildren();
  if (!host) return;
  const exporter = status && status.exporter ? status.exporter.state : "unknown";
  badges.append(badge("Exporter " + exporter, exporter), badge(host.ssh_configured ? "SSH 已配置" : "SSH 关闭", host.ssh_configured ? "ok" : "disabled"));
  const metrics = $("host-metrics");
  metrics.replaceChildren();
  ["cpu.usage", "memory.usage", "disk.usage"].forEach(function (id) {
    const meta = METRIC_MAP.get(id);
    const card = node("div", "metric-card");
    card.append(node("span", "", meta.label), node("strong", "", formatValue(metricValue(status, id), meta.unit)));
    metrics.append(card);
  });
  const alerts = status && Array.isArray(status.active_alerts) ? status.active_alerts : [];
  $("active-alert-count").textContent = String(alerts.length);
  renderAlertItems($("host-active-alerts"), alerts, "当前没有活动告警");
  $("metric-select").value = state.selectedMetricId;
  updateFavoriteButton();
}

function renderAlertItems(container, alerts, emptyText) {
  container.replaceChildren();
  if (!alerts.length) {
    container.append(empty(emptyText));
    return;
  }
  alerts.forEach(function (alert) {
    const item = node("article", "record-item");
    const icon = node("span", "record-kind", "!");
    const main = node("div", "record-main");
    main.append(node("strong", "", alert.alertname || "告警"), node("span", "", (alert.summary || alert.instance || "") + " · " + (alert.state || alert.status || "unknown")));
    item.append(icon, main, badge(alert.severity || alert.state || "unknown", alert.severity || alert.state));
    container.append(item);
  });
}

function statCard(label, value, tone) {
  const card = node("div", "stat-card " + (tone || ""));
  card.append(node("span", "", label), node("strong", "", value));
  return card;
}

function renderOverview() {
  const enabled = state.hosts.filter(function (host) { return host.enabled; });
  const reachable = enabled.filter(function (host) {
    const status = state.hostStatuses.get(host.host_id);
    return status && status.exporter && status.exporter.state === "reachable";
  }).length;
  const activeAlerts = Array.from(state.hostStatuses.values()).reduce(function (sum, item) {
    return sum + (Array.isArray(item.active_alerts) ? item.active_alerts.length : 0);
  }, 0);
  const stats = $("overview-stats");
  stats.replaceChildren(
    statCard("平台就绪", state.health && state.health.ready ? "是" : "否", state.health && state.health.ready ? "good" : "bad"),
    statCard("Exporter 可达", reachable + " / " + enabled.length, reachable === enabled.length && enabled.length ? "good" : "warn"),
    statCard("活动告警", activeAlerts, activeAlerts ? "warn" : "good"),
    statCard("待审批", state.approvals.length, state.approvals.length ? "warn" : "good")
  );
  const hostBox = $("overview-hosts");
  hostBox.replaceChildren();
  if (!state.hosts.length) hostBox.append(empty("连接后加载主机"));
  state.hosts.slice(0, 6).forEach(function (host) {
    const status = state.hostStatuses.get(host.host_id);
    const exporter = status && status.exporter ? status.exporter.state : (host.enabled ? "unknown" : "disabled");
    const row = node("div", "mini-host");
    const main = node("div", "mini-host-main");
    main.append(node("strong", "", host.name), node("span", "", host.host_id));
    const values = node("div", "mini-host-metrics");
    values.append(node("span", "", "CPU " + formatValue(metricValue(status, "cpu.usage"), "%")), node("span", "", "MEM " + formatValue(metricValue(status, "memory.usage"), "%")));
    row.append(main, values, badge(exporter, exporter));
    hostBox.append(row);
  });
  const attention = $("overview-attention");
  attention.replaceChildren();
  const items = state.approvals.map(function (item) {
    return {title: "待审批 · " + (item.host_id || "未知主机"), detail: item.user || "未认证申请人", tone: "pending"};
  }).concat(state.history.alerts.slice(0, 5).map(function (item) {
    return {title: item.alertname || "告警", detail: item.host_id || item.instance || "未知主机", tone: item.status || item.severity};
  })).slice(0, 6);
  if (!items.length) attention.append(empty("当前没有需要处理的项目"));
  items.forEach(function (item) {
    const row = node("div", "attention-item");
    const copy = node("div", "record-main");
    copy.append(node("strong", "", item.title), node("span", "", item.detail));
    row.append(copy, badge(item.tone || "unknown", item.tone));
    attention.append(row);
  });
  renderRecentOverview();
  if (state.connected && state.selectedHostId) loadFavoriteTrends();
}

function renderRecentOverview() {
  const box = $("overview-recent");
  box.replaceChildren();
  const records = buildRecords().slice(0, 6);
  if (!records.length) {
    box.append(empty("暂无运维动态"));
    return;
  }
  records.forEach(function (record) {
    const row = node("div", "activity-item");
    const copy = node("div", "record-main");
    copy.append(node("strong", "", record.title), node("span", "", record.kind + " · " + formatTime(record.time)));
    row.append(copy, badge(record.status || "记录", record.status));
    box.append(row);
  });
}

function createSummarySvg(summary, metric) {
  // 接口只给 min/avg/latest/max 摘要，不给采样时间轴；不能伪造折线走势。
  // 相同数值合并为一个圆点，文字值放在图下独立卡片以避免零范围时重叠。
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 360 96");
  svg.setAttribute("class", "trend-svg range-svg");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", metric.label + "指标区间摘要");
  const entries = [["最低", summary.min], ["平均", summary.avg], ["当前", summary.latest], ["最高", summary.max]]
    .map(function (item) { return [item[0], Number(item[1])]; })
    .filter(function (item) { return Number.isFinite(item[1]); });
  if (!entries.length) return svg;
  const values = entries.map(function (item) { return item[1]; });
  const low = Math.min.apply(null, values), high = Math.max.apply(null, values);
  const x = function (value) { return low === high ? 180 : 36 + ((value - low) / (high - low)) * 288; };
  const baseline = document.createElementNS(ns, "line");
  baseline.setAttribute("x1", "36"); baseline.setAttribute("x2", "324"); baseline.setAttribute("y1", "48"); baseline.setAttribute("y2", "48"); baseline.setAttribute("class", "trend-grid"); svg.append(baseline);
  const range = document.createElementNS(ns, "line");
  range.setAttribute("x1", String(x(low))); range.setAttribute("x2", String(x(high))); range.setAttribute("y1", "48"); range.setAttribute("y2", "48"); range.setAttribute("class", "trend-line"); svg.append(range);
  const markers = [];
  entries.forEach(function (item) {
    let marker = markers.find(function (candidate) { return candidate.value === item[1]; });
    if (!marker) { marker = {value: item[1], labels: []}; markers.push(marker); }
    marker.labels.push(item[0]);
  });
  markers.forEach(function (marker) {
    const circle = document.createElementNS(ns, "circle");
    circle.setAttribute("cx", String(x(marker.value))); circle.setAttribute("cy", "48");
    circle.setAttribute("r", marker.labels.includes("当前") ? "6" : "4");
    circle.setAttribute("class", "trend-dot");
    const title = document.createElementNS(ns, "title");
    title.textContent = marker.labels.join("、") + " " + formatValue(marker.value, metric.unit);
    circle.append(title); svg.append(circle);
  });
  return svg;
}

function trendCard(metric, summary, large) {
  const card = node("div", large ? "" : "trend-card");
  if (!large) {
    const head = node("div", "trend-card-head");
    head.append(node("strong", "", metric.label), node("span", "", summary && summary.points ? summary.points + " 个采样" : "暂无数据"));
    card.append(head);
  }
  if (!summary || !summary.points) card.append(node("div", "chart-empty", "该时间范围内暂无数据"));
  else {
    card.append(node("p", "range-caption", summary.points + " 个采样 · 所选时间范围的区间摘要"), createSummarySvg(summary, metric));
    const values = node("div", "range-values");
    [["最低", summary.min], ["平均", summary.avg], ["当前", summary.latest], ["最高", summary.max]].forEach(function (item) {
      const stat = node("div", "range-value");
      stat.append(node("span", "", item[0]), node("strong", "", formatValue(item[1], metric.unit)));
      values.append(stat);
    });
    card.append(values);
    if ([summary.min, summary.avg, summary.latest, summary.max].every(function (value) { return Number(value) === Number(summary.min); })) {
      card.append(node("p", "range-stable", "数值稳定：最小、平均、当前和最大值相同"));
    }
  }
  return card;
}

async function loadFavoriteTrends() {
  const box = $("favorite-trends");
  const hostId = state.selectedHostId;
  if (!hostId) return replace("favorite-trends", empty("请选择主机"));
  const cards = await Promise.all(state.prefs.favoriteMetrics.map(async function (id) {
    const metric = METRIC_MAP.get(id);
    try {
      const data = await request("/hosts/" + encodeURIComponent(hostId) + "/metrics/" + encodeURIComponent(id) + "/trend?minutes=" + state.prefs.trendMinutes + "&step=" + trendStep(state.prefs.trendMinutes));
      return trendCard(metric, data.trend, false);
    } catch (error) { return trendCard(metric, null, false); }
  }));
  if (hostId !== state.selectedHostId) return;
  box.replaceChildren.apply(box, cards);
}

function trendStep(minutes) {
  if (minutes <= 15) return "1m";
  if (minutes <= 60) return "5m";
  if (minutes <= 360) return "15m";
  return "1h";
}

async function loadSelectedTrend() {
  const hostId = state.selectedHostId, metricId = state.selectedMetricId;
  if (!hostId) return replace("host-trend", empty("请选择主机"));
  replace("host-trend", node("div", "chart-empty", "正在加载趋势…"));
  try {
    const data = await request("/hosts/" + encodeURIComponent(hostId) + "/metrics/" + encodeURIComponent(metricId) + "/trend?minutes=" + state.prefs.trendMinutes + "&step=" + trendStep(state.prefs.trendMinutes));
    if (hostId !== state.selectedHostId || metricId !== state.selectedMetricId) return;
    replace("host-trend", trendCard(METRIC_MAP.get(metricId), data.trend, true));
  } catch (error) { replace("host-trend", empty(error.message)); }
}

function updateFavoriteButton() {
  const favorite = state.prefs.favoriteMetrics.includes(state.selectedMetricId);
  $("favorite-metric").textContent = favorite ? "移出收藏" : "加入收藏";
}

function sessionLabel(item) {
  return item.name || item.title || "会话 " + item.session_id.slice(0, 8);
}

function renderSessions() {
  const host = currentHost();
  const list = $("session-list");
  list.replaceChildren();
  $("conversation-host").textContent = host ? host.name + " · " + host.host_id : "NO HOST";
  const sessions = state.sessions.filter(function (item) { return item.host_id === state.selectedHostId; });
  if (!host) list.append(empty("请先选择主机"));
  else if (!sessions.length) list.append(empty("该主机暂无会话"));
  sessions.forEach(function (item) {
    const button = node("button", "selection-item" + (item.session_id === state.selectedSessionId ? " active" : ""));
    button.type = "button";
    const copy = node("span", "selection-copy");
    copy.append(node("strong", "", sessionLabel(item)), node("span", "", item.message_count + " 条消息 · " + formatTime(item.updated_at)));
    button.append(copy);
    if (item.pending_approvals) button.append(badge(item.pending_approvals + " 待审批", "pending"));
    button.addEventListener("click", function () { selectSession(item.session_id); });
    list.append(button);
  });
  $("conversation-title").textContent = currentSession() ? sessionLabel(currentSession()) : "请选择或新建会话";
  updateControls();
}

async function selectSession(sessionId) {
  state.selectedSessionId = sessionId;
  renderSessions();
  await loadConversation();
}

async function loadConversation() {
  const log = $("chat-log");
  if (!state.selectedSessionId) {
    log.replaceChildren(empty("选择已有会话，或为当前主机新建会话。"));
    $("conversation-title").textContent = "请选择或新建会话";
    updateControls();
    return;
  }
  try {
    const session = await request("/sessions/" + encodeURIComponent(state.selectedSessionId));
    $("conversation-title").textContent = sessionLabel(session);
    log.replaceChildren();
    if (!session.messages || !session.messages.length) log.append(empty("会话已创建，可以开始提问或诊断。"));
    (session.messages || []).forEach(function (item) {
      const article = node("article", "chat-message " + (item.role === "user" ? "user" : "assistant"));
      const meta = node("div", "message-meta");
      meta.append(node("span", "", (item.role === "user" ? "你" : "Assistant") + " · " + formatTime(item.created_at)));
      if (item.status !== "final") meta.append(badge(item.status, item.status));
      article.append(meta, node("div", "message-body", item.content));
      log.append(article);
    });
    log.scrollTop = log.scrollHeight;
  } catch (error) {
    if (error.status === 404) {
      state.selectedSessionId = "";
      await loadSessions();
    } else log.replaceChildren(empty(error.message));
  }
  updateControls();
}

function openSessionDialog(mode) {
  const host = currentHost(), session = currentSession();
  if (!host) return showNotice("请先选择主机", "error");
  $("session-mode").value = mode;
  $("session-dialog-title").textContent = mode === "rename" ? "重命名会话" : "新建会话";
  $("session-dialog-host").textContent = "绑定主机：" + host.name + " · " + host.host_id;
  $("session-name").value = mode === "rename" && session ? (session.name || "") : "";
  $("session-dialog").showModal();
  $("session-name").focus();
}

async function submitSession(event) {
  event.preventDefault();
  if (state.busy.has("session")) return;
  setBusy("session", true);
  const mode = $("session-mode").value;
  const name = $("session-name").value.trim();
  try {
    if (mode === "rename") {
      await request("/sessions/" + encodeURIComponent(state.selectedSessionId), {method: "PATCH", body: {name: name}});
      showNotice("会话名称已更新", "success");
    } else {
      const created = await request("/sessions", {method: "POST", body: {name: name || null, host_id: state.selectedHostId}});
      state.selectedSessionId = created.session_id;
      showNotice("已为 " + created.host_id + " 创建会话", "success");
    }
    $("session-dialog").close();
    await loadSessions();
  } catch (error) { showNotice(error.message, "error"); }
  finally { setBusy("session", false); }
}

async function deleteCurrentSession() {
  const session = currentSession();
  if (!session) return;
  setBusy("session", true);
  try {
    await request("/sessions/" + encodeURIComponent(session.session_id), {method: "DELETE"});
    state.selectedSessionId = "";
    showNotice("会话已删除，关联的未决审批已作废", "success");
    await Promise.all([loadSessions(), loadApprovals()]);
  } catch (error) { showNotice(error.message, "error"); }
  finally { setBusy("session", false); }
}

async function ensureSession() {
  if (state.selectedSessionId) return state.selectedSessionId;
  const created = await request("/sessions", {method: "POST", body: {name: "快捷诊断", host_id: state.selectedHostId}});
  state.selectedSessionId = created.session_id;
  await loadSessions();
  return created.session_id;
}

async function prepareDiagnostic(prompt) {
  if (!state.selectedHostId) return showNotice("请先选择主机", "error");
  navigate("assistant");
  try {
    await ensureSession();
    $("chat-message").value = prompt;
    $("chat-message").focus();
    showNotice("诊断问题已填入，确认后发送", "success");
  } catch (error) { showNotice(error.message, "error"); }
}

function appendPendingTurn(content) {
  // 先在本地回显用户消息与加载动画，再等待完整 /chat 响应；这不是服务端流式输出。
  const log = $("chat-log");
  if (log.querySelector(".empty-state")) log.replaceChildren();
  const user = node("article", "chat-message user optimistic");
  user.append(node("div", "message-meta", "你 · 刚刚"), node("div", "message-body", content));
  const pending = node("article", "chat-message assistant pending");
  const meta = node("div", "message-meta", "Assistant · 正在处理");
  const body = node("div", "message-body typing");
  body.append(node("span", "", "正在分析"));
  const dots = node("span", "typing-dots");
  dots.setAttribute("aria-label", "加载中");
  dots.append(node("i"), node("i"), node("i"));
  body.append(dots);
  pending.append(meta, body);
  log.append(user, pending);
  log.scrollTop = log.scrollHeight;
  return pending;
}

function finishPendingTurn(pending, text, failed) {
  pending.classList.remove("pending");
  pending.classList.toggle("failed", Boolean(failed));
  const meta = pending.querySelector(".message-meta");
  const body = pending.querySelector(".message-body");
  meta.textContent = failed ? "Assistant · 请求失败" : "Assistant · 刚刚";
  body.className = "message-body";
  body.replaceChildren(document.createTextNode(text));
  $("chat-log").scrollTop = $("chat-log").scrollHeight;
}

async function sendChat(event) {
  event.preventDefault();
  const content = $("chat-message").value.trim();
  if (!content || !state.selectedSessionId || state.busy.has("chat")) return;
  setBusy("chat", true);
  $("chat-message").value = "";
  const pending = appendPendingTurn(content);
  let succeeded = false;
  showNotice("Assistant 正在处理，请稍候…");
  try {
    const result = await request("/chat", {method: "POST", body: {session_id: state.selectedSessionId, message: content}, timeout: 120000});
    succeeded = true;
    finishPendingTurn(pending, result.reply || "回答已完成", false);
    showNotice(result.reply && result.reply.includes("审批") ? "已产生待审批请求" : "回答已完成", "success");
  } catch (error) {
    $("chat-message").value = content;
    finishPendingTurn(pending, error.message + "。输入内容已恢复，可再次发送。", true);
    showNotice(error.message, "error");
  } finally {
    setBusy("chat", false);
    if (succeeded) {
      await Promise.all([loadSessions(), loadApprovals(), loadRecords()]);
      renderOverview();
    }
  }
}

function renderApprovals() {
  const list = $("approval-list");
  list.replaceChildren();
  $("nav-approval-count").textContent = String(state.approvals.length);
  $("nav-approval-count").classList.toggle("hidden", !state.approvals.length);
  if (!state.approvals.length) {
    list.append(empty("当前没有待审批操作"));
    return;
  }
  state.approvals.forEach(function (item) {
    const card = node("article", "approval-card");
    const head = node("div", "approval-card-head");
    const heading = node("div");
    heading.append(node("p", "panel-kicker", "PENDING APPROVAL"), node("h3", "", item.host_id || "未知主机"));
    head.append(heading, badge("待决策", "pending"));
    const meta = node("div", "approval-meta");
    [["申请人", item.user || "未认证"], ["申请时间", formatTime(item.created_at)], ["会话", item.session_id || "无"], ["目标", item.target_summary || item.host_id || "未知"]].forEach(function (entry) {
      const cell = node("div"); cell.append(node("span", "", entry[0]), node("strong", "", entry[1])); meta.append(cell);
    });
    card.append(head, meta);
    (item.actions || []).forEach(function (action) {
      const box = node("div", "action-box");
      box.append(node("code", "", action.name || "action"), node("div", "subtle", action.description || "受控写操作"), node("pre", "action-args", safeDetail(action.args || {})));
      card.append(box);
    });
    const actions = node("div", "approval-actions");
    const reject = node("button", "secondary danger-outline", "拒绝");
    const approve = node("button", "", "批准并执行");
    reject.type = approve.type = "button";
    reject.addEventListener("click", function () { confirmApproval(item, "reject"); });
    approve.addEventListener("click", function () { confirmApproval(item, "approve"); });
    actions.append(reject, approve);
    card.append(actions);
    list.append(card);
  });
}

function openConfirm(config) {
  state.confirmAction = config.action;
  $("confirm-title").textContent = config.title;
  $("confirm-message").textContent = config.message;
  $("confirm-submit").textContent = config.submitLabel || "确认";
  $("confirm-submit").className = config.danger ? "danger-outline" : "";
  $("confirm-reason-wrap").hidden = !config.reason;
  $("confirm-reason").value = "";
  $("confirm-dialog").showModal();
}

function confirmApproval(item, decision) {
  openConfirm({
    title: decision === "approve" ? "批准并执行" : "拒绝操作",
    message: "主机 " + (item.host_id || "未知") + " · " + (item.actions || []).map(function (action) { return action.name; }).join("、"),
    submitLabel: decision === "approve" ? "确认批准" : "确认拒绝",
    danger: decision === "reject", reason: true,
    action: function () { return decideApproval(item.approval_id, decision, $("confirm-reason").value); },
  });
}

async function decideApproval(id, decision, reason) {
  if (state.busy.has("approval")) return;
  setBusy("approval", true);
  try {
    const result = await request("/approvals/" + encodeURIComponent(id), {method: "POST", body: {action: decision, reason: reason}});
    $("confirm-dialog").close();
    showNotice((decision === "approve" ? "已批准：" : "已拒绝：") + result.reply, "success");
    await Promise.all([loadApprovals(), loadSessions(), loadRecords()]);
    renderOverview();
  } catch (error) {
    $("confirm-dialog").close();
    showNotice(error.status === 404 ? "审批已处理或已过期，列表已刷新" : error.message, "error");
    await loadApprovals();
  } finally { setBusy("approval", false); }
}

function buildRecords() {
  const records = [];
  (state.history.alerts || []).forEach(function (item) {
    records.push({kind: "告警", kindKey: "alerts", icon: "!", title: item.alertname || "告警", host: item.host_id || item.instance || "未知主机", status: item.status || item.severity || "unknown", time: item.received_at || item.starts_at || item.startsAt, detail: item.summary || item.description || "", raw: item});
  });
  (state.history.remediations || []).forEach(function (item) {
    records.push({kind: "规则处置", kindKey: "executions", icon: "R", title: item.matched_rule || item.action || "规则处置", host: item.host_id || "未知主机", status: item.status || "unknown", time: item.executed_at, detail: item.detail || "", raw: item});
  });
  (state.history.advice || []).forEach(function (item) {
    records.push({kind: "AI 建议", kindKey: "advice", icon: "AI", title: item.alertname || "告警分析", host: item.host_id || "未知主机", status: "completed", time: item.analyzed_at, detail: item.advice || "", raw: item});
  });
  state.inspections.forEach(function (item) {
    records.push({kind: "异常巡检", kindKey: "inspections", icon: "I", title: item.level || "异常", host: item.host_id || "未知主机", status: item.level || "unknown", time: item.checked_at, detail: item.error_text || item.findings || item.findings_json || "", raw: item});
  });
  state.executions.forEach(function (item) {
    records.push({kind: item.channel === "agent" ? "Agent 执行" : "规则执行", kindKey: "executions", icon: "E", title: item.action_name || "动作", host: item.host_id || "未知主机", status: item.status || "unknown", time: item.finished_at || item.started_at || item.created_at, detail: item.detail || "", raw: item});
  });
  records.sort(function (left, right) { return String(right.time || "").localeCompare(String(left.time || "")); });
  return records;
}

function statusMatches(value, filter) {
  if (filter === "all") return true;
  const status = String(value || "").toLowerCase();
  if (filter === "pending") return status.includes("pending") || status === "running";
  if (filter === "succeeded") return ["succeeded", "completed", "ok"].includes(status);
  return status.includes(filter);
}

function parsedDetail(value) {
  if (typeof value !== "string") return value;
  try { return JSON.parse(value); } catch (_) { return value; }
}

function addRecordField(container, label, value) {
  if (value === undefined || value === null || value === "") return;
  const field = node("div", "record-field");
  field.append(node("span", "", label), node("strong", "", safeDetail(value)));
  container.append(field);
}

function appendSnapshot(main, snapshot) {
  // 默认只展示有界预览，完整快照仍可展开；全部内容都作为文本节点渲染。
  if (snapshot === undefined || snapshot === null || snapshot === "") return;
  const entries = Array.isArray(snapshot) ? snapshot.map(function (value, index) { return [String(index + 1), value]; })
    : typeof snapshot === "object" ? Object.entries(snapshot) : [["值", snapshot]];
  const box = node("div", "record-snapshot");
  box.append(node("strong", "record-snapshot-title", "指标快照 · " + entries.length + " 项"));
  const preview = node("div", "record-snapshot-preview");
  entries.slice(0, 3).forEach(function (item) {
    const value = item[1] && typeof item[1] === "object"
      ? (Array.isArray(item[1]) ? "列表 " + item[1].length + " 项" : "对象 " + Object.keys(item[1]).length + " 项")
      : String(item[1]);
    preview.append(node("span", "record-snapshot-chip", item[0] + "：" + (value.length > 64 ? value.slice(0, 64) + "…" : value)));
  });
  if (entries.length > 3) preview.append(node("span", "record-snapshot-more", "+" + (entries.length - 3) + " 项"));
  box.append(preview);
  const disclosure = node("details", "record-json record-snapshot-detail");
  disclosure.append(node("summary", "", "展开指标快照"), node("pre", "", safeDetail(snapshot)));
  box.append(disclosure); main.append(box);
}

function appendRecordDetail(main, record) {
  const detail = parsedDetail(record.detail);
  const raw = record.raw || {};
  const snapshot = parsedDetail(raw.snapshot ?? raw.snapshot_json ?? (detail && typeof detail === "object" ? (detail.snapshot ?? detail.snapshot_json) : ""));
  if (record.kindKey === "inspections") {
    const fields = node("div", "record-fields");
    const findings = parsedDetail(raw.findings || raw.findings_json || detail);
    addRecordField(fields, "异常明细", findings);
    addRecordField(fields, "错误", raw.error_text);
    if (fields.childElementCount) main.append(fields);
  } else if (detail !== "" && detail !== undefined && detail !== null) {
    const fields = node("div", "record-fields");
    const compactDetail = detail && typeof detail === "object" && !Array.isArray(detail)
      ? Object.fromEntries(Object.entries(detail).filter(function (item) { return !["snapshot", "snapshot_json"].includes(item[0]); })) : detail;
    if (compactDetail !== "" && (typeof compactDetail !== "object" || Object.keys(compactDetail).length)) {
      addRecordField(fields, "详情", compactDetail);
      main.append(fields);
    }
  }
  appendSnapshot(main, snapshot);
  const disclosure = node("details", "record-json");
  disclosure.append(node("summary", "", "查看原始 JSON"), node("pre", "", safeDetail(record.raw || record.detail)));
  main.append(disclosure);
}

function renderRecords() {
  const kind = $("record-kind").value;
  const status = $("record-status").value;
  const records = buildRecords().filter(function (item) {
    return (kind === "all" || item.kindKey === kind) && statusMatches(item.status, status);
  });
  $("record-count").textContent = String(records.length);
  const list = $("record-list");
  list.replaceChildren();
  if (!records.length) {
    list.classList.add("empty");
    list.append(empty(state.connected ? "当前筛选条件下没有记录" : "连接后加载记录"));
    return;
  }
  list.classList.remove("empty");
  records.forEach(function (record) {
    const item = node("article", "record-item");
    const main = node("div", "record-main");
    main.append(node("strong", "", record.title), node("span", "", record.kind + " · " + record.host));
    appendRecordDetail(main, record);
    const side = node("div", "record-side");
    side.append(badge(record.status || "unknown", record.status), node("div", "", formatTime(record.time)));
    item.append(node("span", "record-kind", record.icon), main, side);
    list.append(item);
  });
}

function syncSettingsForm() {
  if (!$("pref-theme")) return;
  $("pref-theme").value = state.prefs.theme;
  $("pref-density").value = state.prefs.density;
  $("pref-sidebar").checked = state.prefs.sidebarCollapsed;
  $("pref-start-view").value = state.prefs.startView;
  $("pref-default-host").value = state.prefs.defaultHost;
  $("pref-refresh").value = String(state.prefs.refreshSeconds);
  $("pref-range").value = String(state.prefs.trendMinutes);
}

function renderSettings() {
  syncSettingsForm();
  const metrics = $("metric-preferences");
  metrics.replaceChildren();
  METRICS.forEach(function (metric) {
    const label = node("label", "check-item");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = state.prefs.favoriteMetrics.includes(metric.id);
    input.addEventListener("change", function () {
      const favorites = state.prefs.favoriteMetrics.slice();
      if (input.checked && favorites.length >= 6) {
        input.checked = false;
        return showNotice("收藏指标最多 6 项", "error");
      }
      state.prefs.favoriteMetrics = input.checked ? favorites.concat(metric.id) : favorites.filter(function (id) { return id !== metric.id; });
      if (!state.prefs.favoriteMetrics.length) {
        state.prefs.favoriteMetrics = [metric.id];
        input.checked = true;
      }
      savePreferences();
      if (state.connected) loadFavoriteTrends();
    });
    label.append(node("span", "", metric.label), input);
    metrics.append(label);
  });
  const widgets = $("widget-preferences");
  widgets.replaceChildren();
  state.prefs.widgetOrder.forEach(function (id, index) {
    const meta = WIDGETS.find(function (item) { return item.id === id; });
    const row = node("div", "widget-pref");
    const choice = node("label", "widget-pref-choice");
    const visible = document.createElement("input");
    visible.type = "checkbox";
    visible.checked = !state.prefs.hiddenWidgets.includes(id);
    visible.setAttribute("aria-label", "显示" + meta.label);
    visible.addEventListener("change", function () {
      state.prefs.hiddenWidgets = visible.checked ? state.prefs.hiddenWidgets.filter(function (item) { return item !== id; }) : state.prefs.hiddenWidgets.concat(id);
      savePreferences();
    });
    const up = node("button", "secondary", "↑"), down = node("button", "secondary", "↓");
    up.type = down.type = "button"; up.disabled = index === 0; down.disabled = index === state.prefs.widgetOrder.length - 1;
    up.setAttribute("aria-label", "上移" + meta.label); down.setAttribute("aria-label", "下移" + meta.label);
    up.addEventListener("click", function () { moveWidget(index, -1); });
    down.addEventListener("click", function () { moveWidget(index, 1); });
    choice.append(node("span", "", meta.label), visible);
    const actions = node("div", "widget-pref-actions");
    actions.append(up, down);
    row.append(choice, actions);
    widgets.append(row);
  });
}

function moveWidget(index, delta) {
  const target = index + delta;
  if (target < 0 || target >= state.prefs.widgetOrder.length) return;
  const order = state.prefs.widgetOrder.slice();
  const item = order[index]; order[index] = order[target]; order[target] = item;
  state.prefs.widgetOrder = order;
  savePreferences();
  renderSettings();
}

function markAdminDirty() {
  state.adminDirty = true;
  const summary = $("admin-summary");
  if (summary) {
    summary.classList.add("admin-dirty");
    summary.textContent = "有未保存修改 · revision " + state.admin.revision.slice(0, 12);
  }
}

function adminLabel(text, control, className) {
  const label = node("label", className || "");
  label.append(node("span", "", text), control);
  return label;
}

function adminInput(value, onInput, options) {
  const opts = options || {};
  const input = document.createElement(opts.multiline ? "textarea" : "input");
  if (!opts.multiline) input.type = opts.type || "text";
  if (opts.multiline) input.rows = opts.rows || 2;
  input.value = value === undefined || value === null ? "" : String(value);
  if (opts.readOnly) input.readOnly = true;
  input.addEventListener("input", function () { onInput(input.value, input); markAdminDirty(); });
  return input;
}

function adminSelect(options, value, onChange) {
  const select = document.createElement("select");
  options.forEach(function (option) {
    select.append(new Option(option.label, option.value));
  });
  select.value = value === undefined || value === null ? "" : String(value);
  select.addEventListener("change", function () { onChange(select.value); markAdminDirty(); });
  return select;
}

function adminToggle(title, description, checked, onChange) {
  const label = node("label", "switch-row");
  const copy = node("span");
  copy.append(node("strong", "", title), node("small", "", description));
  const input = document.createElement("input");
  input.type = "checkbox"; input.checked = Boolean(checked);
  input.addEventListener("change", function () { onChange(input.checked); markAdminDirty(); });
  label.append(copy, input);
  return label;
}

function renderIdentityGroup(container, title, key, required) {
  const group = node("div", "admin-params");
  group.append(node("strong", "", title));
  const checks = node("div", "admin-checks");
  const settings = state.admin.config.settings;
  state.admin.available_identities.forEach(function (identity) {
    const label = node("label", "admin-check");
    const input = document.createElement("input");
    input.type = "checkbox"; input.checked = (settings[key] || []).includes(identity);
    input.addEventListener("change", function () {
      const next = (settings[key] || []).filter(function (item) { return item !== identity; });
      if (input.checked) next.push(identity);
      if (required && !next.length) { input.checked = true; return showNotice("至少保留一名管理员", "error"); }
      settings[key] = next;
      markAdminDirty();
    });
    label.append(node("span", "", identity), input); checks.append(label);
  });
  if (!state.admin.available_identities.length) checks.append(empty("当前 Token 映射中没有可用身份"));
  group.append(checks); container.append(group);
}

function renderAdminSecurity() {
  const container = $("admin-security");
  container.replaceChildren();
  const settings = state.admin.config.settings;
  container.append(
    adminToggle("普通接口鉴权", "关闭后普通业务接口匿名开放；管理接口仍强制 Token。", settings.auth_enabled, function (value) { settings.auth_enabled = value; }),
    adminToggle("LLM 分析", "控制告警 AI 分析。", settings.llm_enabled, function (value) { settings.llm_enabled = value; }),
    adminToggle("聊天 Agent", "关闭后聊天使用关键词降级能力。", settings.agent_enabled, function (value) { settings.agent_enabled = value; }),
    adminToggle("周期巡检", "重启后启动或停止巡检调度。", settings.inspection_enabled, function (value) { settings.inspection_enabled = value; })
  );
  container.append(adminLabel("审批模式", adminSelect([
    {value: "owner", label: "仅申请人"}, {value: "approvers_only", label: "指定审批人"},
  ], settings.approval_mode, function (value) { settings.approval_mode = value; })));
  const alertOptions = [{value: "", label: "不限制（不推荐）"}].concat(state.admin.available_identities.map(function (identity) { return {value: identity, label: identity}; }));
  container.append(adminLabel("告警入口身份", adminSelect(alertOptions, settings.alert_identity, function (value) { settings.alert_identity = value; })));
  renderIdentityGroup(container, "审批人", "approver_identities", false);
  renderIdentityGroup(container, "最高权限管理员", "admin_identities", true);
}

function renderAdminAutomation() {
  const container = $("admin-automation");
  container.replaceChildren();
  const config = state.admin.config;
  container.append(adminToggle("自动处置总开关", "关闭时所有规则仍可匹配和审计，但不会自动执行。", config.automation.enabled, function (value) { config.automation.enabled = value; }));
  [
    ["rule_actions", "规则动作目录"], ["agent_actions", "Agent 写动作目录"], ["diagnostics", "只读诊断目录"],
  ].forEach(function (entry) {
    const catalog = config.catalogs[entry[0]];
    container.append(adminToggle(entry[1], "重启后整体启用或关闭该目录。", catalog.enabled, function (value) { catalog.enabled = value; }));
  });
}

function commandParamEditor(command, param, index, catalogName) {
  const box = node("div", "admin-card");
  const grid = node("div", "admin-grid");
  const name = adminInput(param.name || "", function (value) { param.name = value; });
  const type = adminSelect([{value: "str", label: "字符串"}, {value: "int", label: "整数"}], param.type || "str", function (value) { param.type = value; renderAdminCatalog(catalogName); });
  const constraint = adminInput(param.type === "int" ? (param.min ?? "") : (param.pattern || ""), function (value) {
    if (param.type === "int") { if (value === "") delete param.min; else param.min = Number(value); }
    else param.pattern = value;
  }, {type: param.type === "int" ? "number" : "text"});
  const maximum = adminInput(param.type === "int" ? (param.max ?? "") : (param.default ?? ""), function (value) {
    if (param.type === "int") { if (value === "") delete param.max; else param.max = Number(value); }
    else { if (value === "") delete param.default; else param.default = value; }
  }, {type: param.type === "int" ? "number" : "text"});
  const description = adminInput(param.description || "", function (value) { param.description = value; });
  const remove = node("button", "secondary danger-outline", "删除参数");
  remove.type = "button";
  remove.addEventListener("click", function () { command.params.splice(index, 1); markAdminDirty(); renderAdminCatalog(catalogName); });
  grid.append(
    adminLabel("参数名", name), adminLabel("类型", type),
    adminLabel(param.type === "int" ? "最小值" : "全匹配正则", constraint),
    adminLabel(param.type === "int" ? "最大值" : "默认值（可空）", maximum),
    adminLabel("说明", description, "wide")
  );
  box.append(grid, remove);
  return box;
}

function renderAdminCatalog(name) {
  const container = $("admin-catalog-" + name);
  if (!container || !state.admin) return;
  container.replaceChildren();
  const catalog = state.admin.config.catalogs[name];
  if (!catalog.commands.length) container.append(empty("目录中尚无命令"));
  catalog.commands.forEach(function (command, index) {
    const card = node("article", "admin-card");
    const head = node("div", "admin-card-head");
    head.append(node("strong", "", command.key || "未命名命令"));
    const actions = node("div", "admin-card-actions");
    const remove = node("button", "secondary danger-outline", "删除");
    remove.type = "button";
    remove.addEventListener("click", function () { catalog.commands.splice(index, 1); markAdminDirty(); renderAdmin(); });
    actions.append(remove); head.append(actions);
    const grid = node("div", "admin-grid");
    const key = adminInput(command.key || "", function (value) { command.key = value; }, {});
    key.addEventListener("change", renderAdminRules);
    const enabled = adminToggle("启用命令", "禁用后不注册为可调用工具。", command.enabled, function (value) { command.enabled = value; });
    const summary = adminInput(command.summary || "", function (value) { command.summary = value; });
    const template = adminInput(command.template || "", function (value) { command.template = value; }, {multiline: true, rows: 2});
    grid.append(adminLabel("命令键", key), enabled, adminLabel("说明", summary, "wide"), adminLabel("命令模板", template, "wide"));
    if (name !== "diagnostics") grid.append(adminToggle("使用 sudo", "仅管理员可信写命令可启用。", command.sudo, function (value) { command.sudo = value; }));
    const params = node("div", "admin-params");
    const paramsHead = node("div", "admin-card-head");
    paramsHead.append(node("strong", "", "参数约束"));
    const addParam = node("button", "secondary", "新增参数");
    addParam.type = "button";
    addParam.addEventListener("click", function () { command.params.push({name: "param", type: "str", pattern: "^[A-Za-z0-9_.:@-]{1,128}$", description: ""}); markAdminDirty(); renderAdminCatalog(name); });
    paramsHead.append(addParam); params.append(paramsHead);
    command.params.forEach(function (param, paramIndex) { params.append(commandParamEditor(command, param, paramIndex, name)); });
    if (!command.params.length) params.append(node("p", "admin-help", "无参数命令；模板中不能出现占位符。"));
    grid.append(params); card.append(head, grid); container.append(card);
  });
}

function ruleActionCommand(key) {
  return state.admin.config.catalogs.rule_actions.commands.find(function (command) { return command.key === key; });
}

function newRuleId() {
  return "rule_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

function renderAdminRules() {
  const container = $("admin-rules");
  if (!container || !state.admin) return;
  container.replaceChildren();
  const config = state.admin.config;
  const actionOptions = config.catalogs.rule_actions.commands.map(function (command) { return {value: command.key, label: command.key}; });
  const hostOptions = [{value: "", label: "未选择（仅非自动规则）"}].concat(config.hosts.map(function (host) { return {value: host.host_id, label: host.name + " · " + host.host_id}; }));
  if (!config.rules.length) container.append(empty("尚未配置告警规则"));
  config.rules.forEach(function (rule, index) {
    const card = node("article", "admin-card");
    const head = node("div", "admin-card-head");
    const title = node("div"); title.append(node("strong", "", rule.alertname || "未命名规则"), node("p", "admin-help", rule.id));
    const actions = node("div", "admin-card-actions");
    const copy = node("button", "secondary", "复制"), remove = node("button", "secondary danger-outline", "删除");
    copy.type = remove.type = "button";
    copy.addEventListener("click", function () { const clone = JSON.parse(JSON.stringify(rule)); clone.id = newRuleId(); clone.auto_execute = false; config.rules.splice(index + 1, 0, clone); markAdminDirty(); renderAdminRules(); });
    remove.addEventListener("click", function () { config.rules.splice(index, 1); markAdminDirty(); renderAdminRules(); });
    actions.append(copy, remove); head.append(title, actions);
    const grid = node("div", "admin-grid");
    const alertname = adminInput(rule.alertname || "", function (value) { rule.alertname = value; });
    const action = adminSelect(actionOptions, rule.action, function (value) { rule.action = value; rule.params = {}; renderAdminRules(); });
    const host = adminSelect(hostOptions, rule.host_id || "", function (value) { rule.host_id = value; });
    const description = adminInput(rule.description || "", function (value) { rule.description = value; });
    grid.append(adminLabel("Alertmanager alertname", alertname), adminLabel("规则动作", action), adminLabel("目标主机", host), adminLabel("说明", description));
    grid.append(adminToggle("启用规则", "关闭后不参与告警匹配。", rule.enabled, function (value) { rule.enabled = value; }));
    grid.append(adminToggle("自动执行", "同时受自动处置总开关和主机授权约束。", rule.auto_execute, function (value) { rule.auto_execute = value; }));
    const command = ruleActionCommand(rule.action);
    const params = node("div", "admin-params");
    params.append(node("strong", "", "动作参数"));
    (command ? command.params : []).forEach(function (param) {
      const input = adminInput(rule.params[param.name] ?? param.default ?? "", function (value) {
        rule.params[param.name] = param.type === "int" && value !== "" ? Number(value) : value;
      }, {type: param.type === "int" ? "number" : "text"});
      params.append(adminLabel(param.name + (param.description ? " · " + param.description : ""), input));
    });
    if (!command || !command.params.length) params.append(node("p", "admin-help", command ? "该动作不需要参数。" : "请先选择有效的规则动作。"));
    grid.append(params); card.append(head, grid); container.append(card);
  });
}

function renderAdminHosts() {
  const container = $("admin-hosts");
  container.replaceChildren();
  const config = state.admin.config;
  config.hosts.forEach(function (host) {
    const card = node("article", "admin-card");
    const head = node("div", "admin-card-head");
    head.append(node("strong", "", host.name + " · " + host.host_id), badge(host.enabled ? "启用" : "停用", host.enabled ? "ok" : "disabled"));
    card.append(head);
    [["diagnostics", "只读诊断"], ["rule_actions", "规则动作"], ["agent_actions", "Agent 动作"]].forEach(function (entry) {
      const group = node("div", "admin-params"); group.append(node("strong", "", entry[1]));
      const checks = node("div", "admin-checks");
      config.catalogs[entry[0]].commands.forEach(function (command) {
        const label = node("label", "admin-check");
        const input = document.createElement("input"); input.type = "checkbox"; input.checked = host.allow[entry[0]].includes(command.key);
        input.addEventListener("change", function () {
          host.allow[entry[0]] = input.checked ? host.allow[entry[0]].concat(command.key) : host.allow[entry[0]].filter(function (key) { return key !== command.key; });
          markAdminDirty();
        });
        label.append(node("span", "", command.key + (command.enabled ? "" : "（已禁用）")), input); checks.append(label);
      });
      if (!checks.childElementCount) checks.append(node("p", "admin-help", "目录为空"));
      group.append(checks); card.append(group);
    });
    container.append(card);
  });
}

function renderAdmin() {
  // 草稿始终在 state.admin.config 中；分区切换仅切换可见 DOM，不单独保存某个分区。
  if (!state.isAdmin || !state.admin) return;
  $("admin-content").classList.remove("hidden");
  const status = state.admin.restart_required ? "磁盘配置与运行配置不同。确认没有需重启的字段后，点击“应用磁盘配置”。" : "当前磁盘配置已生效。";
  $("admin-restart-banner").textContent = status;
  $("admin-restart-banner").classList.toggle("hidden", !state.admin.restart_required);
  $("admin-summary").classList.remove("empty-state");
  $("admin-summary").classList.toggle("admin-dirty", state.adminDirty);
  $("admin-summary").textContent = (state.adminDirty ? "有未保存修改 · " : "配置已加载 · ") + "revision " + state.admin.revision.slice(0, 12) + " · " + state.admin.available_identities.length + " 个可用身份";
  renderAdminSecurity(); renderAdminAutomation(); renderAdminRules();
  ["rule_actions", "agent_actions", "diagnostics"].forEach(renderAdminCatalog);
  renderAdminHosts();
  activateAdminSection(state.adminSection);
  activateAdminCatalog(state.adminCatalog);
}

function renderHostOptions() {
  const options = state.hostOptions;
  if (!options) return;
  $("admin-monitor-mode").textContent = options.mode === "managed"
    ? "自管 Compose Prometheus：可选已发现目标，也可新增 Exporter 主机:端口。首次启用文件发现需更新一次本地 Prometheus 配置。"
    : "外部 Prometheus：只能登记当前已发现的目标。";
  if (options.warning) $("admin-monitor-mode").textContent += " " + options.warning;
  const list = $("admin-monitor-existing"); list.replaceChildren();
  options.hosts.forEach(function (host) {
    list.append(node("div", "admin-card", host.name + " · " + host.host_id + " · " + host.job + "/" + host.instance));
  });
  const target = $("admin-host-target"); target.replaceChildren();
  options.targets.forEach(function (item, index) {
    target.append(new Option(item.job + " / " + item.instance + " · " + (item.health === "up" ? "可达" : "up=0"), String(index)));
  });
  if (options.mode === "managed") target.append(new Option("输入新 Exporter 目标", "new"));
  if (!target.options.length) target.append(new Option("暂无已发现目标", ""));
  const credential = $("admin-host-credential"); credential.replaceChildren(new Option("不配置 SSH", ""));
  options.credentials.forEach(function (id) { credential.append(new Option(id, id)); });
  const checks = $("admin-host-diagnostic-checks"); checks.replaceChildren();
  options.diagnostics.forEach(function (key) {
    const input = document.createElement("input"); input.type = "checkbox"; input.value = key;
    input.disabled = !state.hostVerification;
    checks.append(adminLabel(key, input, "admin-check"));
  });
  updateHostTargetFields();
}

function updateHostTargetFields() {
  const options = state.hostOptions;
  if (!options) return;
  const choice = $("admin-host-target").value;
  $("admin-host-address-row").hidden = choice !== "new";
  const selected = options.targets[Number(choice)];
  const eligible = choice === "new" || Boolean(selected && selected.ssh_eligible);
  $("admin-host-credential").disabled = !eligible;
  if (!eligible) $("admin-host-credential").value = "";
  $("admin-host-ssh-check").disabled = !eligible || !Boolean(options.credentials.length);
}

function updateHostDiagnosticChoices() {
  $("admin-host-diagnostic-checks").querySelectorAll("input[type=checkbox]").forEach(function (input) {
    input.disabled = !state.hostVerification;
    if (!state.hostVerification) input.checked = false;
  });
}

async function loadHostOptions() {
  if (!state.isAdmin || state.adminDirty || state.busy.has("host-options")) return;
  state.busy.add("host-options");
  $("admin-monitor-mode").textContent = "加载 Prometheus 目标中…";
  try {
    const options = await request("/admin/hosts/options", {timeout: 20000});
    if (!state.isAdmin || !sessionStorage.getItem(TOKEN_KEY)) return;
    state.hostOptions = options;
    renderHostOptions();
  } catch (error) {
    $("admin-monitor-mode").textContent = error.message;
  } finally { state.busy.delete("host-options"); }
}

function collectHostDraft() {
  const choice = $("admin-host-target").value;
  const selected = choice === "new" ? null : state.hostOptions.targets[Number(choice)];
  return {
    host_id: $("admin-host-id").value.trim(), name: $("admin-host-name").value.trim(),
    job: selected ? selected.job : "", instance: selected ? selected.instance : "",
    exporter_address: choice === "new" ? $("admin-host-address").value.trim() : "",
    credential_id: $("admin-host-credential").value,
    ssh_port: Number($("admin-host-ssh-port").value),
    diagnostics: Array.from($("admin-host-diagnostic-checks").querySelectorAll("input:checked")).map(function (input) { return input.value; }),
    verification_id: state.hostVerification,
  };
}

async function hostWizardAction(kind) {
  if (!state.hostOptions || !state.admin || state.adminDirty || state.busy.has("admin")) return;
  setBusy("admin", true);
  try {
    const path = {preview: "preview", check: "ssh-check", add: "add"}[kind];
    const result = await request("/admin/hosts/" + path, {
      method: "POST", timeout: kind === "check" ? 20000 : 30000,
      body: {revision: state.hostOptions.revision, draft: collectHostDraft()},
    });
    if (kind === "preview") {
      $("admin-host-result").textContent = result.name + " · " + result.job + "/" + result.instance
        + " · " + (result.exporter_status === "up" ? "Exporter 可达" : "Exporter 不可达")
        + (result.warning ? " · " + result.warning : "");
    } else if (kind === "check") {
      state.hostVerification = result.verification_id;
      updateHostDiagnosticChoices();
      $("admin-host-result").textContent = "SSH 主机密钥与认证检查通过；未执行命令。";
    } else {
      state.hostVerification = "";
      $("admin-host-result").textContent = result.message || (result.existing ? "主机已存在，内容相同" : "主机已登记");
      state.admin = await request("/admin/config");
      state.adminDirty = false;
      await loadHosts();
      renderHosts(); renderAdmin();
      state.hostOptions = null;
      await loadHostOptions();
      showNotice(result.existing ? "相同主机已登记" : "新主机已登记并生效", "success");
    }
  } catch (error) {
    $("admin-host-result").textContent = error.message;
    showNotice(error.message, "error");
  } finally { setBusy("admin", false); }
}

function activateAdminSection(section, moveFocus) {
  if (section === "monitor" && state.adminDirty && state.adminSection !== "monitor") {
    showNotice("进入新增主机向导前，请先保存或放弃当前管理草稿。", "error");
    return;
  }
  state.adminSection = ["general", "rules", "commands", "hosts", "monitor"].includes(section) ? section : "general";
  document.querySelectorAll("[data-admin-section]").forEach(function (tab) {
    const active = tab.dataset.adminSection === state.adminSection;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    if (active && moveFocus) { tab.focus(); tab.scrollIntoView({block: "nearest", inline: "nearest"}); }
  });
  document.querySelectorAll("[data-admin-pane]").forEach(function (pane) {
    pane.hidden = pane.dataset.adminPane !== state.adminSection;
  });
  if (state.adminSection === "monitor" && !state.hostOptions) loadHostOptions();
}

function activateAdminCatalog(catalog, moveFocus) {
  state.adminCatalog = ["rule_actions", "agent_actions", "diagnostics"].includes(catalog) ? catalog : "rule_actions";
  document.querySelectorAll("[data-admin-catalog]").forEach(function (tab) {
    const active = tab.dataset.adminCatalog === state.adminCatalog;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    if (active && moveFocus) { tab.focus(); tab.scrollIntoView({block: "nearest", inline: "nearest"}); }
  });
  document.querySelectorAll("[data-admin-catalog-pane]").forEach(function (pane) {
    pane.hidden = pane.dataset.adminCatalogPane !== state.adminCatalog;
  });
}

function bindAdminTablist(id, attribute, activate) {
  const tabs = Array.from($(id).querySelectorAll("[" + attribute + "]"));
  tabs.forEach(function (tab, index) {
    tab.addEventListener("click", function () { activate(tab.getAttribute(attribute)); });
    tab.addEventListener("keydown", function (event) {
      let next = index;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else return;
      event.preventDefault(); activate(tabs[next].getAttribute(attribute), true);
    });
  });
}

async function reloadAdminConfig() {
  if (state.busy.has("admin")) return;
  setBusy("admin", true);
  try {
    state.adminDirty = false;
    state.admin = await request("/admin/config", {timeout: 20000});
    state.hostOptions = null; state.hostVerification = "";
    renderAdmin(); showNotice("已重新加载磁盘配置", "success");
  } catch (error) { showNotice(error.message, "error"); }
  finally { setBusy("admin", false); }
}

async function saveAdminConfig() {
  // 一次提交整份结构化草稿；服务端 revision/校验/备份/写回是唯一配置写入边界。
  // 保存事务只有在新快照完成校验、写盘并切换后才返回成功。
  if (!state.admin || !state.adminDirty || state.busy.has("admin")) return;
  setBusy("admin", true);
  try {
    const result = await request("/admin/config", {method: "PUT", timeout: 30000, body: {revision: state.admin.revision, config: state.admin.config}});
    state.admin = result; state.adminDirty = false;
    state.hostOptions = null; state.hostVerification = "";
    renderAdmin();
    const files = (result.changed_files || []).join("、") || "无文件变化";
    showNotice("配置已保存并生效：" + files, "success");
  } catch (error) {
    showNotice(error.status === 409 ? "配置已被其他操作修改，请先重载。" : error.message, "error");
  } finally { setBusy("admin", false); }
}

async function applyAdminDiskConfig() {
  if (!state.admin || state.adminDirty || state.busy.has("admin")) return;
  setBusy("admin", true);
  try {
    state.admin = await request("/admin/config/apply", {method: "POST", timeout: 30000, body: {revision: state.admin.revision}});
    renderAdmin(); showNotice("磁盘配置已应用到当前进程。", "success");
  } catch (error) {
    showNotice(error.status === 409 ? "配置已变化或业务操作尚未结束，请重载后重试。" : error.message, "error");
  } finally { setBusy("admin", false); }
}

function addAdminCommand(name) {
  const catalog = state.admin.config.catalogs[name];
  let index = catalog.commands.length + 1;
  let key = "new_command_" + index;
  while (catalog.commands.some(function (command) { return command.key === key; })) { index += 1; key = "new_command_" + index; }
  catalog.commands.push({key: key, enabled: true, summary: "", template: "echo ok", sudo: false, params: []});
  markAdminDirty(); renderAdmin();
}

function initializeMetricSelect() {
  $("metric-select").replaceChildren();
  METRICS.forEach(function (metric) { $("metric-select").append(new Option(metric.label, metric.id)); });
  $("metric-select").value = state.selectedMetricId;
}

function toggleSelectedFavorite() {
  const favorites = state.prefs.favoriteMetrics.slice();
  if (favorites.includes(state.selectedMetricId)) state.prefs.favoriteMetrics = favorites.filter(function (id) { return id !== state.selectedMetricId; });
  else {
    if (favorites.length >= 6) return showNotice("收藏指标最多 6 项", "error");
    state.prefs.favoriteMetrics = favorites.concat(state.selectedMetricId);
  }
  if (!state.prefs.favoriteMetrics.length) state.prefs.favoriteMetrics = [state.selectedMetricId];
  savePreferences();
  updateFavoriteButton();
  renderSettings();
  loadFavoriteTrends();
}

function bindEvents() {
  document.querySelectorAll("[data-view]").forEach(function (button) { button.addEventListener("click", function () { navigate(button.dataset.view); }); });
  document.querySelectorAll("[data-go]").forEach(function (button) { button.addEventListener("click", function () { navigate(button.dataset.go); }); });
  document.querySelectorAll(".diagnostic-prompt").forEach(function (button) { button.addEventListener("click", function () { prepareDiagnostic(button.dataset.prompt); }); });
  document.querySelectorAll(".dialog-close").forEach(function (button) { button.addEventListener("click", function () { button.closest("dialog").close(); }); });
  $("sidebar-toggle").addEventListener("click", function () { state.prefs.sidebarCollapsed = !state.prefs.sidebarCollapsed; savePreferences(); });
  $("notice-close").addEventListener("click", hideNotice);
  $("refresh").addEventListener("click", function () { refreshAll(true); });
  $("open-auth").addEventListener("click", function () { $("token").value = ""; $("auth-dialog").showModal(); $("token").focus(); });
  $("auth-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    const token = $("token").value.trim();
    if (!token) return;
    sessionStorage.setItem(TOKEN_KEY, token);
    $("token").value = "";
    $("auth-dialog").close();
    await refreshAll(true);
  });
  $("disconnect").addEventListener("click", function () {
    sessionStorage.removeItem(TOKEN_KEY);
    state.hosts = []; state.hostStatuses.clear(); state.sessions = []; state.approvals = [];
    state.isAdmin = false; state.admin = null; state.adminDirty = false;
    state.selectedHostId = ""; state.selectedSessionId = "";
    $("auth-dialog").close();
    $("admin-nav").classList.add("hidden");
    clearAdminView();
    updateConnection("offline", "未连接");
    renderHostSelectors(); renderHosts(); renderSessions(); renderApprovals(); renderRecords(); renderOverview();
    showNotice("Token 已从当前标签页清除", "success");
  });
  $("global-host").addEventListener("change", function (event) { selectHost(event.target.value); });
  $("metric-select").addEventListener("change", function (event) { state.selectedMetricId = event.target.value; updateFavoriteButton(); loadSelectedTrend(); });
  $("favorite-metric").addEventListener("click", toggleSelectedFavorite);
  $("new-session").addEventListener("click", function () { openSessionDialog("create"); });
  $("rename-session").addEventListener("click", function () { $("session-menu").classList.add("hidden"); openSessionDialog("rename"); });
  $("delete-session").addEventListener("click", function () {
    $("session-menu").classList.add("hidden");
    const session = currentSession();
    if (session) openConfirm({title: "删除会话", message: "将删除“" + sessionLabel(session) + "”及其消息，并作废关联未决审批。", submitLabel: "确认删除", danger: true, reason: false, action: deleteCurrentSession});
  });
  $("session-menu-button").addEventListener("click", function () { $("session-menu").classList.toggle("hidden"); });
  $("session-form").addEventListener("submit", submitSession);
  $("chat-form").addEventListener("submit", sendChat);
  $("confirm-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    if (!state.confirmAction) return;
    const action = state.confirmAction; state.confirmAction = null;
    await action();
    if ($("confirm-dialog").open) $("confirm-dialog").close();
  });
  ["record-kind", "record-status"].forEach(function (id) { $(id).addEventListener("change", renderRecords); });
  $("record-limit").addEventListener("change", function () { if (state.connected) loadRecords(); });
  $("admin-reload").addEventListener("click", function () {
    if (!state.adminDirty) return reloadAdminConfig();
    openConfirm({title: "放弃未保存修改", message: "将重新读取磁盘配置，当前页面上的修改会丢失。", submitLabel: "放弃并重载", danger: true, reason: false, action: reloadAdminConfig});
  });
  $("admin-save").addEventListener("click", saveAdminConfig);
  $("admin-apply").addEventListener("click", applyAdminDiskConfig);
  $("admin-host-target").addEventListener("change", function () { state.hostVerification = ""; updateHostTargetFields(); updateHostDiagnosticChoices(); });
  ["input", "change"].forEach(function (eventName) {
    $("admin-host-form").addEventListener(eventName, function (event) {
      if (event.target.type === "checkbox") return;
      state.hostVerification = ""; updateHostDiagnosticChoices();
    });
  });
  $("admin-host-preview").addEventListener("click", function () { hostWizardAction("preview"); });
  $("admin-host-ssh-check").addEventListener("click", function () { hostWizardAction("check"); });
  $("admin-host-refresh").addEventListener("click", function () { state.hostOptions = null; state.hostVerification = ""; loadHostOptions(); });
  $("admin-host-form").addEventListener("submit", function (event) { event.preventDefault(); hostWizardAction("add"); });
  bindAdminTablist("admin-tabs", "data-admin-section", activateAdminSection);
  bindAdminTablist("admin-command-tabs", "data-admin-catalog", activateAdminCatalog);
  $("admin-add-rule").addEventListener("click", function () {
    if (!state.admin) return;
    const actions = state.admin.config.catalogs.rule_actions.commands;
    const hosts = state.admin.config.hosts;
    state.admin.config.rules.push({
      id: newRuleId(), enabled: true, alertname: "NewAlert", action: actions[0] ? actions[0].key : "",
      host_id: hosts[0] ? hosts[0].host_id : "", params: {}, auto_execute: false, description: "",
    });
    markAdminDirty(); renderAdminRules();
  });
  document.querySelectorAll(".admin-add-command").forEach(function (button) {
    button.addEventListener("click", function () { if (state.admin) addAdminCommand(button.dataset.catalog); });
  });
  $("pref-theme").addEventListener("change", function (event) { state.prefs.theme = event.target.value; savePreferences(); });
  $("pref-density").addEventListener("change", function (event) { state.prefs.density = event.target.value; savePreferences(); });
  $("pref-sidebar").addEventListener("change", function (event) { state.prefs.sidebarCollapsed = event.target.checked; savePreferences(); });
  $("pref-start-view").addEventListener("change", function (event) { state.prefs.startView = event.target.value; savePreferences(); });
  $("pref-default-host").addEventListener("change", function (event) { state.prefs.defaultHost = event.target.value; savePreferences(); });
  $("pref-refresh").addEventListener("change", function (event) { state.prefs.refreshSeconds = Number(event.target.value); savePreferences(); });
  $("pref-range").addEventListener("change", function (event) { state.prefs.trendMinutes = Number(event.target.value); savePreferences(); if (state.connected) { loadFavoriteTrends(); loadSelectedTrend(); } });
  $("reset-preferences").addEventListener("click", function () {
    openConfirm({title: "恢复默认设置", message: "将清除当前浏览器中的控制台偏好，不会清除 Token 或服务端数据。", submitLabel: "恢复默认", danger: true, reason: false, action: function () {
      localStorage.removeItem(PREF_KEY); state.prefs = cloneDefaults(); savePreferences(); renderSettings(); navigate("overview"); showNotice("个性化设置已恢复默认", "success");
    }});
  });
  document.addEventListener("click", function (event) {
    if (!event.target.closest(".menu-wrap")) $("session-menu").classList.add("hidden");
  });
}

function initialize() {
  initializeMetricSelect();
  bindEvents();
  applyPreferences();
  navigate(state.prefs.startView);
  renderHosts(); renderSessions(); renderApprovals(); renderRecords(); renderOverview();
  refreshAll(false);
}

initialize();
