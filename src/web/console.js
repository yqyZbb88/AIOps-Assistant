"use strict";
/* 控制台是公开静态空壳；所有运维数据和变更必须经过同源 API。
 * Token 只放 sessionStorage（标签页关闭会清除本地副本，不撤销服务端 Token），浏览器持久化只保存非敏感偏好。
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
  ["cpu.usage", "CPU 使用率", "%"], ["cpu.load1", "每核 1 分钟负载", "load/core"],
  ["cpu.iowait", "CPU I/O 等待", "%"], ["memory.usage", "内存使用率", "%"],
  ["memory.available", "可用内存", "B"], ["memory.swap_usage", "Swap 使用率", "%"],
  ["disk.usage", "磁盘使用率", "%"], ["disk.read_bytes", "磁盘读取", "MiB/s"],
  ["disk.write_bytes", "磁盘写入", "MiB/s"], ["disk.io_util", "磁盘 I/O 利用率", "%"],
  ["network.rx_bytes", "网络接收", "MiB/s"], ["network.tx_bytes", "网络发送", "MiB/s"],
  ["network.rx_errors", "网络接收错误", "errors/s"], ["network.tx_errors", "网络发送错误", "errors/s"],
  ["system.process_count", "进程数量", "processes"], ["system.uptime", "运行时间", "s"],
  ["alerts.active_count", "活动告警", "alerts"],
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
// admin 是 GET /admin/config 的完整可编辑草稿；adminDirty 只跟踪本标签页的修改，
// 真正的冲突仍由服务端 revision 校验。主机向导独立保存，进入前必须处理这份草稿。
const state = {
  prefs: loadPreferences(), view: "overview", hosts: [], hostStatuses: new Map(),
  selectedHostId: "", sessions: [], selectedSessionId: "", approvals: [],
  history: {alerts: [], remediations: [], advice: []}, inspections: [], executions: [],
  health: null, connected: false, busy: new Set(), refreshTimer: null,
  overviewUpdatedAt: null, overviewError: "",
  confirmAction: null, selectedMetricId: "cpu.usage",
  isAdmin: false, admin: null, adminDirty: false,
  adminSection: "general", adminCatalog: "rule_actions",
  hostOptions: null, hostVerification: "", adminCredentials: null, trendGeneration: 0, favoriteGeneration: 0, credentialGeneration: 0,
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
  // 这只是展示偏好校验，不参与服务端鉴权或主机授权。
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
  if (value === null || value === undefined || (typeof value === "string" && !value.trim())) return "—";
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
  // 超时是浏览器放弃等待，不代表服务端写入已取消；调用方不可据此盲目重试审批/保存。
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
    // 已断开或换身份的响应不能重新填入当前标签页。
    if (authenticated && token !== sessionStorage.getItem(TOKEN_KEY)) {
      const error = new Error("连接已变化，已忽略旧请求结果");
      error.connectionChanged = true;
      throw error;
    }
    if (!response.ok) {
      const error = new Error(String(response.status) + " " + (data.detail || response.statusText));
      error.status = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (authenticated && token !== sessionStorage.getItem(TOKEN_KEY)) {
      const obsolete = new Error("连接已变化，已忽略旧请求结果");
      obsolete.connectionChanged = true;
      throw obsolete;
    }
    if (error.name === "AbortError") throw new Error("请求超时，请检查服务或网络状态");
    throw error;
  } finally { clearTimeout(timer); }
}

function updateNoticeLayout() {
  const notice = $("notice"), anchor = $("notice-anchor");
  if (!notice || !anchor) return;
  anchor.hidden = notice.classList.contains("hidden");
  if (anchor.hidden) return;
  const header = document.querySelector(".topbar");
  const bounds = anchor.getBoundingClientRect();
  const gap = parseFloat(getComputedStyle(anchor).marginTop) || 0;
  const top = (header ? header.getBoundingClientRect().height : 0) + gap;
  // Measure the original content-area slot, never its scrolling vertical position.
  notice.style.setProperty("--notice-top", top + "px");
  notice.style.left = bounds.left + "px";
  notice.style.width = bounds.width + "px";
  notice.style.right = "auto";
  anchor.style.height = notice.getBoundingClientRect().height + "px";
}

function showNotice(text, tone) {
  $("notice-text").textContent = text;
  $("notice").className = "notice " + (tone || "");
  updateNoticeLayout();
}

function hideNotice() { $("notice").className = "notice hidden"; updateNoticeLayout(); }

function setBusy(key, active) {
  if (active) state.busy.add(key); else state.busy.delete(key);
  updateControls();
}

function applyPreferences() {
  document.documentElement.dataset.theme = state.prefs.theme;
  document.documentElement.dataset.density = state.prefs.density;
  document.querySelector(".app-shell").classList.toggle("sidebar-collapsed", state.prefs.sidebarCollapsed);
  $("sidebar-toggle").classList.toggle("is-collapsed", state.prefs.sidebarCollapsed);
  $("sidebar-toggle").setAttribute("aria-label", state.prefs.sidebarCollapsed ? "展开导航" : "收起导航");
  $("sidebar-toggle").setAttribute("aria-expanded", String(!state.prefs.sidebarCollapsed));
  $("sidebar-toggle").title = state.prefs.sidebarCollapsed ? "展开导航" : "收起导航";
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
  updateNoticeLayout();
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
  if (view !== "admin") clearCredentialInputs();
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
  $("refresh").disabled = state.busy.has("refresh");
  $("refresh").setAttribute("aria-busy", String(state.busy.has("refresh")));
  $("refresh").setAttribute("aria-label", state.busy.has("refresh") ? "正在刷新数据" : "刷新当前数据");
  renderOverviewContext();
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
      control.disabled = state.busy.has("admin") || control.dataset.roleReadonly === "true";
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
  const previousHost = state.selectedHostId;
  state.hosts = await request("/hosts?include_disabled=true");
  const visible = new Set(state.hosts.map(function (host) { return host.host_id; }));
  state.hostStatuses.forEach(function (_, id) { if (!visible.has(id)) state.hostStatuses.delete(id); });
  state.sessions = state.sessions.filter(function (item) { return visible.has(item.host_id); });
  state.approvals = state.approvals.filter(function (item) { return visible.has(item.host_id); });
  ["alerts", "remediations", "advice"].forEach(function (key) {
    state.history[key] = state.history[key].filter(function (item) { return visible.has(item.host_id); });
  });
  state.inspections = state.inspections.filter(function (item) { return visible.has(item.host_id); });
  state.executions = state.executions.filter(function (item) { return visible.has(item.host_id); });
  const enabled = state.hosts.filter(function (host) { return host.enabled; });
  const candidates = [state.selectedHostId, state.prefs.defaultHost, enabled[0] && enabled[0].host_id];
  state.selectedHostId = candidates.find(function (id) {
    return id && enabled.some(function (host) { return host.host_id === id; });
  }) || "";
  if (previousHost && previousHost !== state.selectedHostId) {
    state.selectedSessionId = "";
    state.trendGeneration++; state.favoriteGeneration++;
    ["chat-log", "host-metrics", "host-active-alerts", "host-detail-badges",
     "host-trend", "favorite-trends", "host-current-metric"].forEach(function (id) {
      if ($(id)) $(id).replaceChildren();
    });
    if ($("chat-message")) $("chat-message").value = "";
    if ($("conversation-title")) $("conversation-title").textContent = "\u8bf7\u9009\u62e9\u6216\u65b0\u5efa\u4f1a\u8bdd";
  }
  renderHostSelectors();
}

async function loadSessions() {
  state.sessions = await request("/sessions?limit=100");
  if (state.selectedSessionId && !state.sessions.some(function (item) { return item.session_id === state.selectedSessionId && item.host_id === state.selectedHostId; })) {
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
    if (error.connectionChanged) return;
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
  if ($("host-current-metric")) $("host-current-metric").replaceChildren();
  state.trendGeneration++; state.favoriteGeneration++; state.credentialGeneration++;
  clearCredentialInputs(); state.adminCredentials = null;
  ["admin-ssh", "admin-tokens", "admin-model", "admin-host-access"].forEach(function (id) { if ($(id)) $(id).replaceChildren(); });
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
  const hostId = state.selectedHostId;
  if (!hostId) return;
  try {
    const status = await request("/hosts/" + encodeURIComponent(hostId) + "/status");
    if (!state.hosts.some(function (host) { return host.host_id === hostId; })) return;
    state.hostStatuses.set(hostId, status);
  } catch (error) {
    if (error.connectionChanged) return;
    state.hostStatuses.set(hostId, {exporter: {state: "query_error"}, metrics: {}, active_alerts: [], error: error.message});
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
      if (error.connectionChanged) return;
      state.hostStatuses.set(host.host_id, {exporter: {state: "query_error"}, metrics: {}, active_alerts: [], error: error.message});
    }
  }));
  renderHosts();
  renderHostDetail();
}

async function loadRecords() {
  const limit = Number($("record-limit").value || 50);
  const hostId = state.selectedHostId;
  const suffix = hostId ? "&host_id=" + encodeURIComponent(hostId) : "";
  const results = await Promise.all([
    request("/alerts/history?limit=" + limit + suffix),
    request("/inspections/recent?limit=" + limit + suffix),
    request("/executions?limit=" + limit + suffix),
  ]);
  if (hostId !== state.selectedHostId) return;
  state.history = results[0];
  state.inspections = results[1].inspections || [];
  state.executions = results[2].executions || [];
  renderRecords();
}

async function refreshAll(showMessage) {
  // 自动刷新不应覆盖管理员正在编辑的草稿；loadAdminAccess 会保留 dirty 草稿并提示 revision 冲突。
  if (state.busy.has("refresh")) return;
  const token = sessionStorage.getItem(TOKEN_KEY);
  setBusy("refresh", true);
  await loadHealth();
  if (token !== sessionStorage.getItem(TOKEN_KEY)) {
    setBusy("refresh", false);
    if (sessionStorage.getItem(TOKEN_KEY)) await refreshAll(false);
    return;
  }
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
    if (token !== sessionStorage.getItem(TOKEN_KEY)) return;
    updateConnection("online", "已连接");
    state.overviewUpdatedAt = new Date();
    state.overviewError = "";
    renderOverview();
    if (state.view === "hosts") await loadSelectedTrend();
    if (showMessage !== false) showNotice("数据已刷新 · " + new Date().toLocaleTimeString(), "success");
  } catch (error) {
    if (error.connectionChanged || token !== sessionStorage.getItem(TOKEN_KEY)) return;
    updateConnection("error", error.status === 401 ? "Token 无效" : "连接异常");
    state.overviewError = error.status === 401 ? "连接已失效，请重新连接" : "刷新失败，请检查连接后重试";
    renderOverview();
    showNotice(error.status === 401 ? "Token 无效或已失效，请重新连接。" : error.message, "error");
  } finally {
    setBusy("refresh", false);
    // 换身份时正在执行的旧刷新会占用 busy；释放后重新加载当前身份。
    if (token !== sessionStorage.getItem(TOKEN_KEY) && sessionStorage.getItem(TOKEN_KEY)) await refreshAll(false);
  }
}

async function selectHost(hostId) {
  if (!state.hosts.some(function (host) { return host.host_id === hostId && host.enabled; })) return;
  state.selectedHostId = hostId;
  state.selectedSessionId = "";
  state.favoriteGeneration++;
  replace("favorite-trends", node("div", "chart-empty", "正在加载当前主机趋势…"));
  $("favorite-trends").setAttribute("aria-busy", "true");
  replace("overview-recent", node("div", "chart-empty", "正在加载当前主机动态…"));
  renderHostSelectors();
  renderHosts();
  setBusy("host", true);
  try {
    await Promise.all([loadHostStatus(), loadSessions(), loadRecords()]);
    if (!state.connected || state.selectedHostId !== hostId) return;
    renderOverview();
    if (state.view === "hosts") await loadSelectedTrend();
  } catch (error) {
    if (error.connectionChanged || state.selectedHostId !== hostId) return;
    showNotice(error.message, "error");
    ["favorite-trends", "overview-recent"].forEach(function (id) {
      replace(id, overviewState("主机数据加载失败", "请刷新重试；当前不显示上一主机的数据。"));
    });
    $("favorite-trends").setAttribute("aria-busy", "false");
  }
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

function statCard(label, value, tone, detail) {
  const card = node("div", "stat-card " + (tone || ""));
  card.append(node("span", "", label), node("strong", "", value));
  if (detail) card.append(node("small", "stat-detail", detail));
  return card;
}

function renderOverviewContext() {
  const update = $("overview-update");
  if (!update) return;
  const updated = state.overviewUpdatedAt ? "摘要更新 " + state.overviewUpdatedAt.toLocaleTimeString() : "尚未完成刷新";
  if (state.busy.has("refresh")) update.textContent = "正在刷新运行数据…";
  else if (state.overviewError) update.textContent = state.overviewError + " · " + updated;
  else if (state.connected) {
    const interval = state.prefs.refreshSeconds ? "每 " + state.prefs.refreshSeconds + " 秒自动刷新" : "手动刷新";
    update.textContent = updated + " · " + interval;
  } else update.textContent = "未连接 · 连接后加载运行数据";
  update.className = state.overviewError ? "overview-update error" : "overview-update";
  const host = currentHost();
  document.querySelectorAll(".overview-selected-host").forEach(function (label) {
    label.textContent = !host && state.connected && label.id === "overview-recent-scope"
      ? "范围：全部授权主机的历史记录"
      : "当前主机：" + (host ? host.name + " · " + host.host_id : "尚未选择");
  });
}

function overviewState(title, detail, actionLabel, action) {
  const box = node("div", "overview-state");
  box.append(node("strong", "", title), node("p", "", detail));
  if (action) {
    const button = node("button", "text-button", actionLabel);
    button.type = "button";
    button.addEventListener("click", action);
    box.append(button);
  }
  return box;
}

function renderOverview() {
  const focused = document.activeElement;
  const focusKey = focused && focused.dataset.overviewFocus;
  renderOverviewContext();
  const enabled = state.hosts.filter(function (host) { return host.enabled; });
  const reachable = enabled.filter(function (host) {
    const status = state.hostStatuses.get(host.host_id);
    return status && status.exporter && status.exporter.state === "reachable";
  }).length;
  // 活动告警取授权启用主机的实时状态，历史告警只进入最近动态。
  // Exporter 不可达时后端不查告警，因此空数组不能视为已确认无告警。
  const missingAlerts = enabled.filter(function (host) {
    const status = state.hostStatuses.get(host.host_id);
    return !status || !status.exporter || status.exporter.state !== "reachable" || status.alerts_state === "query_error";
  }).length;
  const activeAlerts = enabled.reduce(function (sum, host) {
    const status = state.hostStatuses.get(host.host_id);
    return sum + (status && Array.isArray(status.active_alerts) ? status.active_alerts.length : 0);
  }, 0);
  const ready = state.health && state.health.ready;
  const known = state.connected;
  const coverage = known && enabled.length > 0;
  let alertValue = "—", alertTone = "", alertDetail = "连接后查询";
  if (coverage) {
    alertValue = missingAlerts && !activeAlerts ? "—" : activeAlerts + (missingAlerts ? "+" : "");
    alertTone = missingAlerts || activeAlerts ? "warn" : "good";
    alertDetail = missingAlerts ? missingAlerts + " 台主机告警数据未确认" : "实时查询 · 含触发中与待触发";
  } else if (known) alertDetail = "暂无启用主机";
  const stats = $("overview-stats");
  stats.replaceChildren(
    statCard("平台就绪", !state.health ? "—" : ready ? "就绪" : "未就绪",
      !state.health ? "" : ready ? "good" : "bad", "公开就绪探针 · 独立于身份连接"),
    statCard("Exporter 可达", coverage ? reachable + " / " + enabled.length : "—",
      coverage ? reachable === enabled.length ? "good" : "warn" : "",
      known ? enabled.length ? "可达 / 已启用主机" : "暂无启用主机" : "连接后查询"),
    statCard("活动告警", alertValue, alertTone, alertDetail),
    statCard("待审批", known ? state.approvals.length : "—",
      known ? state.approvals.length ? "warn" : "good" : "", known ? "当前身份可见的申请" : "连接后查询")
  );
  if (!known) {
    const title = state.overviewError ? "运行数据暂不可用" : "连接后查看运行数据";
    const detail = state.overviewError ? "本次查询未完成，当前不显示缓存数据作为实时结果。" : "使用 API Token 连接，查看你有权访问的主机与待办。";
    ["overview-hosts", "overview-attention", "favorite-trends", "overview-recent"].forEach(function (id) {
      const connect = id === "overview-hosts" ? function () { $("open-auth").click(); } : null;
      replace(id, overviewState(title, detail, state.overviewError ? "重新连接" : "连接 Assistant", connect));
    });
    $("overview-host-scope").textContent = "全部授权主机";
    $("favorite-trends").setAttribute("aria-busy", "false");
    return;
  }
  const hostBox = $("overview-hosts");
  hostBox.replaceChildren();
  $("overview-host-scope").textContent = "已登记 " + state.hosts.length + " 台 · 已启用 " + enabled.length + " 台" + (state.hosts.length > 6 ? " · 显示前 6 台" : "");
  if (!state.hosts.length) hostBox.append(overviewState("暂无可见主机", "请联系管理员登记主机并授予访问权限。"));
  state.hosts.slice(0, 6).forEach(function (host) {
    const status = state.hostStatuses.get(host.host_id);
    const exporter = !host.enabled ? "disabled" : status && status.exporter ? status.exporter.state : "unknown";
    const row = node("button", "mini-host");
    row.type = "button";
    row.disabled = !host.enabled;
    row.dataset.overviewFocus = "host:" + host.host_id;
    row.setAttribute("aria-label", "查看主机 " + host.name + "（" + host.host_id + "）");
    row.addEventListener("click", async function () { await selectHost(host.host_id); navigate("hosts"); });
    const main = node("span", "mini-host-main");
    main.append(node("strong", "", host.name), node("span", "", host.host_id));
    const values = node("span", "mini-host-metrics");
    values.append(node("span", "", "CPU " + formatValue(metricValue(status, "cpu.usage"), "%")), node("span", "", "MEM " + formatValue(metricValue(status, "memory.usage"), "%")));
    const labels = {reachable: "可达", unreachable: "不可达", query_error: "查询失败", unknown: "未确认", no_data: "无数据", disabled: "已停用"};
    row.append(main, values, badge(labels[exporter] || "未确认", exporter));
    hostBox.append(row);
  });
  const attention = $("overview-attention");
  attention.replaceChildren();
  const items = state.approvals.map(function (item) {
    return {title: "待审批 · " + (item.host_id || "未知主机"), detail: item.user || "未认证申请人", tone: "pending", label: "待审批", view: "approvals", focusKey: "approval:" + item.approval_id};
  });
  enabled.forEach(function (host) {
    const status = state.hostStatuses.get(host.host_id);
    (status && status.active_alerts || []).forEach(function (alert, index) {
      items.push({title: alert.alertname || "活动告警", detail: host.name + " · " + (alert.summary || host.host_id), tone: alert.severity === "critical" ? "critical" : "warning", label: alert.state === "pending" ? "待触发" : "触发中", hostId: host.host_id, focusKey: "alert:" + host.host_id + ":" + alert.alertname + ":" + index});
    });
  });
  if (missingAlerts) attention.append(overviewState("部分主机告警数据未确认", missingAlerts + " 台主机不可达或查询失败，请在主机详情检查采集状态。"));
  if (!items.length && !missingAlerts) attention.append(overviewState("暂无待审批或活动告警", enabled.length ? "已完成当前启用主机的活动告警查询，历史事件可在运维记录中查看。" : "当前没有启用主机；登记并启用主机后可查看活动告警。"));
  items.slice(0, 6).forEach(function (item) {
    const row = node("div", "attention-item");
    const copy = node("div", "record-main");
    copy.append(node("strong", "", item.title), node("span", "", item.detail));
    const action = node("button", "text-button", item.view ? "查看审批" : "查看主机");
    action.type = "button";
    action.dataset.overviewFocus = item.focusKey;
    action.setAttribute("aria-label", action.textContent + "：" + item.title);
    action.addEventListener("click", async function () {
      if (item.hostId) await selectHost(item.hostId);
      navigate(item.view || "hosts");
    });
    const aside = node("div", "attention-actions");
    aside.append(badge(item.label, item.tone), action);
    row.append(copy, aside);
    attention.append(row);
  });
  if (items.length > 6) attention.append(node("p", "panel-description", "显示前 6 项；完整申请见审批，完整活动告警见各主机详情。"));
  renderRecentOverview();
  if (state.connected && state.selectedHostId) loadFavoriteTrends();
  else replace("favorite-trends", overviewState("尚未选择主机", "选择已启用主机后查看收藏指标的真实趋势。", "查看主机", function () { navigate("hosts"); }));
  // 定时刷新会重建摘要行；只恢复仍然可见的同一操作，避免键盘焦点跳回页面起点。
  if (focusKey) {
    const replacement = Array.from(document.querySelectorAll("#view-overview [data-overview-focus]"))
      .find(function (button) { return button.dataset.overviewFocus === focusKey && !button.disabled; });
    if (replacement) replacement.focus({preventScroll: true});
  }
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
  // 兼容旧版摘要响应；有 series 时使用 createTrendPlot 绘制真实时间采样。
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
  if (!summary || !summary.points) card.append(node("div", "chart-empty", summary && summary.state === "not_applicable" ? "不适用：该主机没有 Swap" : "趋势：该时间范围内暂无数据"));
  else {
    card.append(node("p", "range-caption", summary.points + " 个采样 · " + (summary.series ? "真实时间序列" : "区间摘要")), summary.series ? createTrendPlot(summary, metric) : createSummarySvg(summary, metric));
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
  const box = $("favorite-trends"), hostId = state.selectedHostId, minutes = state.prefs.trendMinutes;
  const generation = ++state.favoriteGeneration;
  if (!hostId) {
    box.setAttribute("aria-busy", "false");
    return replace("favorite-trends", empty("请选择主机"));
  }
  // 切换主机或范围时先移除旧曲线，避免新标题下显示上一查询的采样。
  box.setAttribute("aria-busy", "true");
  replace("favorite-trends", node("div", "chart-empty", "正在加载收藏指标趋势…"));
  const cards = await Promise.all(state.prefs.favoriteMetrics.map(async function (id) {
    const metric = METRIC_MAP.get(id);
    try {
      const data = await request("/hosts/" + encodeURIComponent(hostId) + "/metrics/" + encodeURIComponent(id) + "/trend?minutes=" + minutes + "&step=" + trendStep(minutes));
      return trendCard(metric, data.trend, false);
    } catch (error) {
      const card = node("div", "trend-card");
      const message = node("p", "trend-error", "趋势查询失败。请检查监控服务连接后，使用顶部刷新重试。");
      const details = node("details", "record-json");
      details.append(node("summary", "", "查看错误详情"), node("pre", "", error.message));
      card.append(node("strong", "", metric.label), message, details);
      return card;
    }
  }));
  if (generation !== state.favoriteGeneration || hostId !== state.selectedHostId || minutes !== state.prefs.trendMinutes || !state.connected) return;
  box.replaceChildren.apply(box, cards);
  box.setAttribute("aria-busy", "false");
}

function trendStep(minutes) { return Math.max(15, Math.ceil(minutes * 60 / 240)) + "s"; }

async function loadSelectedTrend() {
  const hostId = state.selectedHostId, metricId = state.selectedMetricId, minutes = state.prefs.trendMinutes;
  const generation = ++state.trendGeneration, metric = METRIC_MAP.get(metricId);
  if (!hostId) { if ($("host-current-metric")) $("host-current-metric").replaceChildren(); return replace("host-trend", empty("请选择主机")); }
  const stale = function () { return generation !== state.trendGeneration || hostId !== state.selectedHostId || metricId !== state.selectedMetricId || minutes !== state.prefs.trendMinutes; };
  replace("host-trend", node("div", "chart-empty", "正在加载趋势…"));
  const base = "/hosts/" + encodeURIComponent(hostId) + "/metrics/" + encodeURIComponent(metricId);
  // 两项独立更新，不因任一请求失败掩盖另一项状态。
  const instant = $("host-current-metric") ? request(base).then(function (data) {
    if (stale()) return;
    const box = $("host-current-metric"); box.replaceChildren(node("strong", "", "即时查询"));
    if (!data.samples || !data.samples.length) box.append(node("p", "", data.state === "not_applicable" ? "不适用：该主机没有 Swap" : "无数据"));
    (data.samples || []).forEach(function (sample) { box.append(node("p", "", seriesLabel(sample.labels || {}) + " · " + formatValue(sample.value, metric.unit))); });
  }).catch(function (error) { if (!stale() && !error.connectionChanged) $("host-current-metric").replaceChildren(empty("即时查询失败：" + error.message)); }) : Promise.resolve();
  try {
    const data = await request(base + "/trend?minutes=" + minutes + "&step=" + trendStep(minutes));
    if (!stale()) replace("host-trend", trendCard(metric, data.trend, true));
  } catch (error) {
    if (!error.connectionChanged && !stale()) replace("host-trend", empty("趋势查询失败：" + error.message));
  }
  await instant;
}

function seriesLabel(labels) {
  const entries = Object.entries(labels).filter(function (entry) { return !["job", "instance", "__name__"].includes(entry[0]); });
  return entries.length ? entries.map(function (entry) { return entry[0] + "=" + entry[1]; }).join(" · ") : "主机";
}

function createTrendPlot(summary, metric) {
  const wrapper = node("div", "time-series"), choices = node("div", "chip-row"), canvas = node("div", "plot-canvas");
  const curves = summary.series || [], selected = new Set(curves.slice(0, 6).map(function (_, i) { return i; }));
  const colors = ["#3b82f6", "#f59e0b", "#10b981", "#ec4899", "#8b5cf6", "#06b6d4"];
  function draw() {
    const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
    const plotWidth = (wrapper.clientWidth && wrapper.clientWidth < 450) || (typeof window !== "undefined" && window.innerWidth < 600) ? 360 : 720;
    const right = plotWidth - 26;
    svg.setAttribute("viewBox", "0 0 " + plotWidth + " 270"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", metric.label + " 时间序列，单位 " + metric.unit);
    const values = [], times = [];
    curves.forEach(function (curve, i) { if (selected.has(i)) (curve.values || []).forEach(function (point) { times.push(Number(point[0])); if (point[1] !== null && Number.isFinite(Number(point[1]))) values.push(Number(point[1])); }); });
    if (!values.length) { canvas.replaceChildren(empty("所选设备没有有效采样")); return; }
    let lo = Math.min.apply(null, values), hi = Math.max.apply(null, values);
    const margin = Math.max((hi - lo) * 0.12, Math.abs(hi) * 0.02, 0.01); lo -= margin; hi += margin;
    const begin = Math.min.apply(null, times), end = Math.max.apply(null, times);
    const x = function (t) { return 82 + (t - begin) / (end - begin || 1) * (right - 82); };
    const y = function (v) { return 226 - (v - lo) / (hi - lo) * 196; };
    function part(tag, attrs, text) { const el = document.createElementNS(ns, tag); Object.entries(attrs).forEach(function (a) { el.setAttribute(a[0], String(a[1])); }); if (text !== undefined) el.textContent = text; svg.append(el); return el; }
    part("text", {x: 82, y: 17, fill: "currentColor", "font-size": 12}, metric.unit);
    for (let i = 0; i <= 4; i++) {
      const v = lo + (hi - lo) * i / 4;
      part("line", {x1: 82, x2: right, y1: y(v), y2: y(v), class: "trend-grid"});
      part("text", {x: 76, y: y(v) + 4, "text-anchor": "end", fill: "currentColor", "font-size": 11}, Math.abs(v) < 1e-12 ? "0" : Number(v.toPrecision(3)).toLocaleString(undefined, {maximumSignificantDigits: 3}));
      const t = begin + (end - begin) * i / 4;
      part("text", {x: x(t), y: 252, "text-anchor": "middle", fill: "currentColor", "font-size": 11}, new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}));
    }
    curves.forEach(function (curve, i) {
      if (!selected.has(i)) return;
      let path = "", previous = null;
      (curve.values || []).forEach(function (point) {
        const t = Number(point[0]), v = point[1];
        if (v === null || !Number.isFinite(Number(v))) { previous = null; return; }
        const gap = previous === null || (summary.step_seconds && t - previous > summary.step_seconds * 1.5);
        path += (gap ? "M" : "L") + x(t).toFixed(2) + "," + y(Number(v)).toFixed(2) + " "; previous = t;
        const dot = part("circle", {cx: x(t), cy: y(Number(v)), r: 2.4, fill: colors[i % colors.length]});
        const title = document.createElementNS(ns, "title"); title.textContent = seriesLabel(curve.labels || {}) + " · " + new Date(t * 1000).toLocaleString() + " · " + formatValue(v, metric.unit); dot.append(title);
      });
      part("path", {d: path.trim(), fill: "none", stroke: colors[i % colors.length], "stroke-width": 2});
    });
    canvas.replaceChildren(svg);
  }
  curves.forEach(function (curve, i) {
    const label = node("label", "chip"), checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = selected.has(i);
    checkbox.addEventListener("change", function () { if (checkbox.checked) selected.add(i); else selected.delete(i); draw(); });
    label.setAttribute("style", "color:" + colors[i % colors.length]); label.append(checkbox, node("span", "", seriesLabel(curve.labels || {}))); choices.append(label);
  });
  wrapper.append(choices, canvas);
  if (summary.truncated) wrapper.append(node("p", "safety-note", "共 " + summary.series_total + " 条序列，仅返回前 32 条；请缩小采集范围。"));
  wrapper.redrawTrend = draw;
  draw(); return wrapper;
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
  const sessionId = state.selectedSessionId;
  if (!sessionId) {
    log.replaceChildren(empty("选择已有会话，或为当前主机新建会话。"));
    $("conversation-title").textContent = "请选择或新建会话";
    updateControls();
    return;
  }
  try {
    const session = await request("/sessions/" + encodeURIComponent(sessionId));
    // 旧会话的成功或失败响应都不能覆盖后来选中的会话。
    if (sessionId !== state.selectedSessionId) return;
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
    if (error.connectionChanged) return;
    if (sessionId !== state.selectedSessionId) return;
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

async function deleteCurrentSession(session) {
  if (!session || state.busy.has("session")) return;
  setBusy("session", true);
  try {
    await request("/sessions/" + encodeURIComponent(session.session_id), {method: "DELETE"});
    if (state.selectedSessionId === session.session_id) state.selectedSessionId = "";
    showNotice("会话已删除，关联的未决审批已作废", "success");
    await Promise.all([loadSessions(), loadApprovals()]);
  } catch (error) { showNotice(error.message, "error"); }
  finally { setBusy("session", false); }
}

async function ensureSession() {
  if (state.selectedSessionId) return state.selectedSessionId;
  const hostId = state.selectedHostId;
  const created = await request("/sessions", {method: "POST", body: {name: "快捷诊断", host_id: hostId}});
  // 创建成功仍属于请求时的主机；用户切换选择后不抢回旧会话。
  if (hostId === state.selectedHostId && !state.selectedSessionId) {
    state.selectedSessionId = created.session_id;
    await loadSessions();
  }
  return created.session_id;
}

async function prepareDiagnostic(prompt) {
  if (!state.selectedHostId) return showNotice("请先选择主机", "error");
  if (state.busy.has("session") || state.busy.has("chat")) return;
  const hostId = state.selectedHostId;
  setBusy("session", true);
  navigate("assistant");
  try {
    const sessionId = await ensureSession();
    if (hostId !== state.selectedHostId || sessionId !== state.selectedSessionId) return;
    $("chat-message").value = prompt;
    $("chat-message").focus();
    showNotice("诊断问题已填入，确认后发送", "success");
  } catch (error) {
    if (!error.connectionChanged && hostId === state.selectedHostId) showNotice(error.message, "error");
  } finally { setBusy("session", false); }
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
  const sessionId = state.selectedSessionId;
  const token = sessionStorage.getItem(TOKEN_KEY);
  const isCurrent = () => sessionId === state.selectedSessionId && token === sessionStorage.getItem(TOKEN_KEY);
  setBusy("chat", true);
  $("chat-message").value = "";
  const pending = appendPendingTurn(content);
  let succeeded = false;
  showNotice("Assistant 正在处理，请稍候…");
  try {
    const result = await request("/chat", {method: "POST", body: {session_id: sessionId, message: content}, timeout: 120000});
    succeeded = true;
    if (!isCurrent()) return;
    finishPendingTurn(pending, result.reply || "回答已完成", false);
    showNotice(result.reply && result.reply.includes("审批") ? "已产生待审批请求" : "回答已完成", "success");
  } catch (error) {
    if (!isCurrent() || error.connectionChanged) return;
    const restored = !$("chat-message").value;
    if (restored) $("chat-message").value = content;
    finishPendingTurn(pending, error.message + (restored ? "。输入内容已恢复。" : "。当前输入已保留。"), true);
    showNotice(error.message, "error");
  } finally {
    setBusy("chat", false);
    if (succeeded && token === sessionStorage.getItem(TOKEN_KEY)) {
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
  const available = state.admin.available_identities;
  const identities = Array.from(new Set(available.concat(settings[key] || [])));
  identities.forEach(function (identity) {
    const active = available.includes(identity);
    const label = node("label", "admin-check");
    const input = document.createElement("input");
    input.type = "checkbox"; input.checked = (settings[key] || []).includes(identity);
    input.addEventListener("change", function () {
      if (input.checked && !active) {
        input.checked = false;
        return showNotice("该身份无有效 Token，请先配置同名 Token。身份名区分大小写。", "error");
      }
      const next = (settings[key] || []).filter(function (item) { return item !== identity; });
      if (input.checked) next.push(identity);
      if (required && !next.length) { input.checked = true; return showNotice("至少保留一名管理员", "error"); }
      settings[key] = next;
      markAdminDirty(); renderAdminHostAccess();
    });
    const matches = active ? [] : available.filter(function (name) { return name.toLowerCase() === identity.toLowerCase(); });
    const description = active ? identity : identity + "（无有效 Token；身份名区分大小写" + (matches.length ? "，当前可用：" + matches.join("、") : "") + "）";
    label.append(node("span", "", description), input); checks.append(label);
  });
  if (!state.admin.available_identities.length) checks.append(empty("当前 Token 映射中没有可用身份"));
  group.append(checks); container.append(group);
}

function renderAdminSecurity() {
  const container = $("admin-security");
  container.replaceChildren();
  const settings = state.admin.config.settings;
  container.append(
    node("p", "admin-help", "业务接口始终验证 Token 身份，会话仅归创建者所有。"),
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

function renderAdminHostAccess() {
  const container = $("admin-host-access");
  if (!container || !state.admin) return;
  container.replaceChildren();
  const settings = state.admin.config.settings;
  const access = settings.host_access || (settings.host_access = {});
  const identities = Array.from(new Set(state.admin.available_identities.concat(Object.keys(access)))).sort();
  identities.forEach(function (identity) {
    const card = node("article", "admin-card");
    const admin = settings.admin_identities.includes(identity);
    card.append(node("strong", "", identity + (admin ? " · 管理员（全部主机）" : "")));
    if (!state.admin.available_identities.includes(identity)) card.append(node("p", "admin-help", "当前无有效 Token；保留授权，恢复同名身份后生效。"));
    const checks = node("div", "admin-checks");
    state.admin.config.hosts.forEach(function (host) {
      const label = node("label", "admin-check");
      const input = document.createElement("input");
      input.type = "checkbox"; input.disabled = admin; input.dataset.roleReadonly = String(admin);
      input.checked = admin || (access[identity] || []).includes(host.host_id);
      input.addEventListener("change", function () {
        const ids = access[identity] || [];
        access[identity] = input.checked ? Array.from(new Set(ids.concat(host.host_id))) : ids.filter(function (id) { return id !== host.host_id; });
        markAdminDirty();
      });
      label.append(node("span", "", host.name + " · " + host.host_id + (host.enabled ? "" : "（已禁用）")), input);
      checks.append(label);
    });
    if (!admin) {
      const revoke = node("button", "secondary danger-outline", "撤销全部主机授权");
      revoke.type = "button";
      revoke.addEventListener("click", function () { access[identity] = []; markAdminDirty(); renderAdminHostAccess(); });
      card.append(revoke);
    }
    card.append(checks); container.append(card);
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
  renderAdminHosts(); renderAdminHostAccess();
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
  if (["monitor", "ssh", "tokens", "model"].includes(section) && state.adminDirty && state.adminSection !== section) {
    showNotice("进入新增主机向导前，请先保存或放弃当前管理草稿。", "error");
    return;
  }
  if (state.adminSection !== section) clearCredentialInputs();
  state.adminSection = ["general", "rules", "commands", "hosts", "access", "monitor", "ssh", "tokens", "model"].includes(section) ? section : "general";
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
  if (state.isAdmin && ["ssh", "tokens", "model"].includes(state.adminSection)) loadAdminCredentials();
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

function clearConnectionData() {
  // 身份变化时丢弃旧运维数据、草稿和确认回调；展示偏好与正在执行的请求各自保留。
  state.hosts = []; state.hostStatuses.clear(); state.sessions = []; state.approvals = [];
  state.history = {alerts: [], remediations: [], advice: []};
  state.inspections = []; state.executions = [];
  state.overviewUpdatedAt = null; state.overviewError = "";
  state.isAdmin = false; state.admin = null; state.adminDirty = false;
  state.selectedHostId = ""; state.selectedSessionId = ""; state.confirmAction = null;
  $("chat-message").value = "";
  $("chat-log").replaceChildren(empty("连接后选择或新建会话。"));
  $("conversation-title").textContent = "请选择或新建会话";
  $("host-detail-title").textContent = "请选择主机";
  $("active-alert-count").textContent = "0";
  ["host-detail-badges", "host-metrics", "host-active-alerts"].forEach(function (id) { $(id).replaceChildren(); });
  ["host-trend", "favorite-trends"].forEach(function (id) { replace(id, empty("请选择主机")); });
  ["confirm-dialog", "session-dialog"].forEach(function (id) { if ($(id).open) $(id).close(); });
  $("admin-nav").classList.add("hidden");
  clearAdminView();
  updateConnection("offline", "未连接");
  renderHostSelectors(); renderHosts(); renderSessions(); renderApprovals(); renderRecords(); renderOverview();
}

function bindEvents() {
  if (typeof ResizeObserver === "function") {
    // One observer follows header wrapping, sidebar transitions and notice wrapping.
    const observer = new ResizeObserver(updateNoticeLayout);
    [$("notice-anchor"), $("notice"), document.querySelector(".topbar")].forEach(function (element) {
      if (element) observer.observe(element);
    });
  }
  if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    // 一个页面级 resize 监听，按当前 DOM 重绘；移除卡片后不会遗留监听器或观察器。
    window.addEventListener("resize", function () { updateNoticeLayout(); document.querySelectorAll(".time-series").forEach(function (plot) { if (plot.redrawTrend) plot.redrawTrend(); }); });
  }

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
    if (token !== sessionStorage.getItem(TOKEN_KEY)) clearConnectionData();
    sessionStorage.setItem(TOKEN_KEY, token);
    $("token").value = "";
    $("auth-dialog").close();
    await refreshAll(true);
  });
  $("disconnect").addEventListener("click", function () {
    sessionStorage.removeItem(TOKEN_KEY);
    clearConnectionData();
    $("auth-dialog").close();
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
    if (session) openConfirm({title: "删除会话", message: "将删除“" + sessionLabel(session) + "”及其消息，并作废关联未决审批。", submitLabel: "确认删除", danger: true, reason: false, action: function () { return deleteCurrentSession(session); }});
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


// 凭据页面只接收白名单元信息；输入和一次性 Token 只存在 DOM，不进入偏好或草稿。
function clearCredentialInputs() {
  document.querySelectorAll("[data-credential-sensitive]").forEach(function (input) { input.value = ""; input.textContent = ""; });
}

function credentialInput(type, value, sensitive) {
  const input = node("input"); input.type = type || "text"; input.value = value ?? ""; input.autocomplete = "off";
  if (sensitive) input.setAttribute("data-credential-sensitive", "true");
  return input;
}
function credentialButton(label, action) {
  const button = node("button", "secondary", label); button.type = "button"; button.addEventListener("click", action); return button;
}
async function loadAdminCredentials() {
  if (!state.isAdmin || state.busy.has("admin")) return;
  if (state.adminCredentials && state.admin && state.adminCredentials.revision === state.admin.revision) return;
  try {
    const generation = ++state.credentialGeneration;
    const result = await request("/admin/credentials");
    if (!state.isAdmin || generation !== state.credentialGeneration) return;
    state.adminCredentials = result; renderCredentials();
  } catch (error) { if (!error.connectionChanged) showNotice(error.message, "error"); }
}
async function changeCredential(path, method, data) {
  if (!state.isAdmin || state.busy.has("admin")) return;
  if (state.adminDirty) return showNotice("请先保存或放弃管理草稿", "error");
  const revision = state.adminCredentials && state.adminCredentials.revision;
  if (!revision) return;
  clearCredentialInputs(); state.credentialGeneration++; setBusy("admin", true);
  try {
    const result = await request("/admin/credentials/" + path, {method: method, body: {revision: revision, data: data}, timeout: 30000});
    state.adminCredentials = result; state.hostOptions = null; state.hostVerification = "";
    // 一次性明文不存入 state，不被后续刷新复制。
    const once = result.token_once; delete result.token_once;
    let refreshError = null;
    try { state.admin = await request("/admin/config"); }
    catch (error) { if (error.connectionChanged) return; refreshError = error; }
    // 轮换当前登录 Token 后，GET 已失效；仍须显示刚生成的一次性明文。
    if (!state.isAdmin) return;
    if (state.admin) state.admin.revision = result.revision;
    state.adminDirty = false;
    renderAdmin(); renderCredentials();
    if (once && state.adminSection === "tokens") {
      const output = $("admin-token-once"); output.value = once;
      showNotice(refreshError ? "Token 已生成，请立即复制并重新连接；当前连接已失效或刷新失败" : "Token 已生成，请立即复制；离开页面即清除", "success");
    } else showNotice(refreshError ? "配置已保存，状态刷新失败；请重新连接或重载" : "配置已保存并生效", "success");
  } catch (error) {
    if (!error.connectionChanged) showNotice(error.status === 409 ? "配置已变化或存在运行操作/未决审批，请重载后重试。" : error.message, "error");
    state.adminCredentials = null;
  } finally { setBusy("admin", false); }
}
function renderCredentials() {
  const data = state.adminCredentials; if (!data || !state.isAdmin) return;
  const tokenBox = $("admin-tokens"), sshBox = $("admin-ssh"), modelBox = $("admin-model");
  if (!tokenBox || !sshBox || !modelBox) return;
  clearCredentialInputs(); tokenBox.replaceChildren(); sshBox.replaceChildren(); modelBox.replaceChildren();
  const identity = credentialInput("text"); identity.maxLength = 128;
  tokenBox.append(node("p", "admin-help", "按身份名生成 Token。角色在安全与开关中授权；部署管理员和 Webhook 凭据只读保留。"), adminLabel("身份名", identity), credentialButton("生成用户 Token", function () { changeCredential("tokens", "POST", {identity: identity.value}); }));
  const once = node("textarea"); once.id = "admin-token-once"; once.readOnly = true; once.rows = 2; once.autocomplete = "off"; once.setAttribute("data-credential-sensitive", "true");
  tokenBox.append(adminLabel("一次性 Token（请立即复制）", once), credentialButton("清除明文", clearCredentialInputs));
  data.tokens.forEach(function (item) {
    const row = node("div", "admin-card"); row.append(node("strong", "", item.identity + (item.managed ? " · 页面管理" : " · 部署只读")));
    if (item.managed) row.append(credentialButton("轮换", function () { changeCredential("tokens/" + encodeURIComponent(item.id) + "/rotate", "POST", {}); }), credentialButton("撤销", function () { changeCredential("tokens/" + encodeURIComponent(item.id), "DELETE", {}); }));
    tokenBox.append(row);
  });
  const id = credentialInput("text"), username = credentialInput("text"), method = node("select"), password = credentialInput("password", "", true), key = credentialInput("file", "", true), known = node("textarea");
  id.maxLength = 63; username.maxLength = 128; known.rows = 5; known.maxLength = 131072; known.setAttribute("data-credential-sensitive", "true");
  ["password", "private_key"].forEach(function (value) { const option = node("option", "", value === "password" ? "密码" : "上传私钥（无口令）"); option.value = value; method.append(option); }); method.value = "password";
  const passwordLabel = adminLabel("密码（更新时留空保留）", password), keyLabel = adminLabel("上传私钥（更新时不上传则保留）", key);
  keyLabel.hidden = true; method.addEventListener("change", function () { password.value = ""; key.value = ""; passwordLabel.hidden = method.value !== "password"; keyLabel.hidden = method.value !== "private_key"; });
  sshBox.append(node("p", "admin-help", "密码或私钥用于登录；known_hosts 用于验证服务器身份，两种认证方式都需要。新建时请粘贴已核验的 known_hosts，更新时可留空保留。连接测试只验证认证，不授予诊断或写权限。部署凭据只读。"), adminLabel("凭据 ID", id), adminLabel("用户名", username), adminLabel("认证方式", method), passwordLabel, keyLabel, adminLabel("可信 known_hosts（新建必填，更新留空保留）", known));
  sshBox.append(credentialButton("保存 SSH 凭据", async function () {
    const update = {username: username.value.trim(), method: method.value}, profile = id.value.trim();
    const existing = data.ssh.find(function (item) { return item.managed && item.id === profile; });
    if (!profile) { showNotice("请输入 SSH 凭据 ID", "error"); id.focus(); return; }
    if (!update.username) { showNotice("请输入 SSH 用户名", "error"); username.focus(); return; }
    if (!known.value.trim() && !existing) {
      showNotice("新建 SSH 凭据必须填写可信 known_hosts，用于验证服务器身份；密码和私钥认证都需要。", "error");
      known.focus(); return;
    }
    if (known.value.trim()) update.known_hosts = known.value;
    if (method.value === "password") {
      if (!password.value && (!existing || existing.method !== "password")) {
        showNotice("请输入 SSH 密码", "error"); password.focus(); return;
      }
      if (password.value) update.password = password.value;
    }
    if (method.value === "private_key" && !(key.files && key.files[0]) && (!existing || existing.method !== "private_key")) {
      showNotice("请选择无口令的 SSH 私钥文件", "error"); key.focus(); return;
    }
    if (method.value === "private_key" && key.files && key.files[0]) {
      if (key.files[0].size > 131072) return showNotice("私钥最长 128 KiB", "error");
      const selectedFile = key.files[0]; update.private_key = await selectedFile.text();
      if (!state.isAdmin || state.adminSection !== "ssh" || key.files[0] !== selectedFile) return;
    }
    await changeCredential("ssh/" + encodeURIComponent(profile), "PUT", update);
  }));
  data.ssh.forEach(function (item) {
    const row = node("div", "admin-card"); row.append(node("strong", "", item.id + " · " + item.username + " · " + item.method + (item.managed ? "" : " · 部署只读")));
    if (item.managed) row.append(credentialButton("编辑", function () { clearCredentialInputs(); id.value = item.id; username.value = item.username; method.value = item.method; passwordLabel.hidden = method.value !== "password"; keyLabel.hidden = method.value !== "private_key"; }), credentialButton("删除", function () { changeCredential("ssh/" + encodeURIComponent(item.id), "DELETE", {}); }));
    sshBox.append(row);
  });
  // 已登记主机修改关联，目标地址仍由后端登记表决定。
  ((state.admin && state.admin.config.hosts) || []).forEach(function (host) {
    const row = node("div", "admin-card"), select = node("select");
    const placeholder = node("option", "", "选择凭据"); placeholder.value = ""; select.append(placeholder);
    data.ssh.forEach(function (item) { const option = node("option", "", item.id + " · " + item.username); option.value = item.id; select.append(option); }); select.value = host.ssh_ref || "";
    row.append(node("strong", "", host.name + " · " + host.host_id), adminLabel("SSH 凭据关联", select), credentialButton("更新关联", function () { changeCredential("hosts/" + encodeURIComponent(host.host_id) + "/ssh", "PUT", {credential_id: select.value}); })); row.append(credentialButton("测试已保存的 SSH 连接", async function () {
      if (state.busy.has("admin")) return; clearCredentialInputs(); setBusy("admin", true);
      try { const result = await request("/admin/credentials/hosts/" + encodeURIComponent(host.host_id) + "/ssh/test", {method: "POST", timeout: 30000}); showNotice(result.detail, "success"); }
      catch (error) { if (!error.connectionChanged) showNotice(error.message, "error"); }
      finally { setBusy("admin", false); }
    })); sshBox.append(row);
  });
  const cfg = data.model, apiKey = credentialInput("password", "", true), mode = node("select"), fields = {};
  [["keep", "保留密钥"], ["replace", "替换密钥"], ["clear", "明确清除密钥"]].forEach(function (entry) { const option = node("option", "", entry[1]); option.value = entry[0]; mode.append(option); }); mode.value = "keep";
  mode.addEventListener("change", function () { apiKey.value = ""; });
  modelBox.append(node("p", "admin-help", "OpenAI 兼容接口 · " + (cfg.configured ? "密钥已配置" : "密钥未配置") + (cfg.managed ? " · 页面管理" : " · 沿用部署环境") + "。Base URL 填接口根地址；Agent 需要模型支持工具调用。保存不发起模型请求。"), adminLabel("密钥操作", mode), adminLabel("新 API Key", apiKey));
  [["base_url", "Base URL", "text"], ["model", "模型名", "text"], ["timeout", "超时（秒）", "number"], ["max_retries", "重试次数", "number"], ["max_tokens", "最大输出长度", "number"]].forEach(function (entry) { const input = credentialInput(entry[2], cfg[entry[0]]); fields[entry[0]] = input; modelBox.append(adminLabel(entry[1], input)); });
  const tokenParam = node("select");
  ["max_tokens", "max_completion_tokens"].forEach(function (value) {
    const option = node("option", "", value); option.value = value; tokenParam.append(option);
  });
  tokenParam.value = cfg.token_limit_param || "max_tokens";
  fields.token_limit_param = tokenParam;
  modelBox.append(adminLabel("高级：输出长度参数", tokenParam));
  modelBox.append(credentialButton("保存模型配置", function () {
    const update = {key_action: mode.value}; Object.entries(fields).forEach(function (entry) { update[entry[0]] = entry[1].value; }); if (mode.value === "replace") update.api_key = apiKey.value;
    changeCredential("model", "PUT", update);
  }), credentialButton("测试已保存的模型连接", async function () {
    if (state.busy.has("admin")) return; clearCredentialInputs(); setBusy("admin", true);
    try { const result = await request("/admin/credentials/model/test", {method: "POST", timeout: 20000}); showNotice(result.detail, "success"); }
    catch (error) { if (!error.connectionChanged) showNotice(error.message, "error"); }
    finally { setBusy("admin", false); }
  }), credentialButton("重新加载凭据状态", function () { state.adminCredentials = null; loadAdminCredentials(); }));
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
