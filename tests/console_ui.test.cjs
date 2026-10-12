const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.hidden = false;
    this.textContent = "";
    this.className = "";
    this.classes = new Set();
    this.style = {setProperty(name, value) { this[name] = value; }};
    this.classList = {
      contains: (name) => this.className.split(/\s+/).includes(name) || this.classes.has(name),
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
      toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name),
    };
    this.listeners = {};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name]; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelectorAll() { return this.children; }
  getBoundingClientRect() { return {left: 0, width: 100, height: 40}; }
  focus() { this.focused = true; }
  scrollIntoView() {}
}

function all(element, predicate) {
  return [element, ...element.children.flatMap((child) => all(child, predicate))].filter(predicate);
}

function setup() {
  const elements = new Map();
  const tabs = ["general", "rules", "commands", "hosts", "monitor"].map((name) => {
    const tab = new Element("button");
    tab.dataset.adminSection = name;
    tab.setAttribute("data-admin-section", name);
    return tab;
  });
  const panes = ["general", "rules", "commands", "hosts", "monitor"].map((name) => {
    const pane = new Element("section");
    pane.dataset.adminPane = name;
    return pane;
  });
  const catalogTabs = ["rule_actions", "agent_actions", "diagnostics"].map((name) => {
    const tab = new Element("button");
    tab.dataset.adminCatalog = name;
    tab.setAttribute("data-admin-catalog", name);
    return tab;
  });
  const catalogPanes = ["rule_actions", "agent_actions", "diagnostics"].map((name) => {
    const pane = new Element("section");
    pane.dataset.adminCatalogPane = name;
    return pane;
  });
  const mainTabs = new Element("div"); mainTabs.children = tabs;
  const subTabs = new Element("div"); subTabs.children = catalogTabs;
  elements.set("admin-tabs", mainTabs);
  elements.set("admin-command-tabs", subTabs);
  ["admin-content", "admin-restart-banner", "admin-summary", "admin-security", "admin-automation",
    "admin-rules", "admin-catalog-rule_actions", "admin-catalog-agent_actions",
    "admin-catalog-diagnostics", "admin-hosts", "admin-monitor-existing",
    "admin-host-target", "admin-host-credential", "admin-host-diagnostic-checks",
    "admin-host-result", "admin-monitor-mode", "notice", "notice-text"].forEach((id) => {
    const element = new Element("div");
    element.append(new Element("strong"));
    elements.set(id, element);
  });
  const document = {
    querySelector: () => null,
    createElement: (tag) => new Element(tag),
    createElementNS: (_, tag) => new Element(tag),
    getElementById: (id) => elements.get(id),
    querySelectorAll: (selector) => ({
      "[data-admin-section]": tabs,
      "[data-admin-pane]": panes,
      "[data-admin-catalog]": catalogTabs,
      "[data-admin-catalog-pane]": catalogPanes,
    })[selector] || [],
  };
  const source = fs.readFileSync(path.join(__dirname, "../src/web/console.js"), "utf8")
    .replace(/\ninitialize\(\);\s*$/, "\n");
  const context = {document, getComputedStyle: () => ({marginTop: "16px"}), localStorage: {getItem: () => null}, console};
  vm.runInNewContext(source + "\nglobalThis.uiTest = {renderOverview, renderOverviewContext, selectHost, renderAdminHostAccess, renderAdminSecurity, loadHosts, renderCredentials, changeCredential, createTrendPlot, trendStep, clearCredentialInputs, loadFavoriteTrends, createSummarySvg, trendCard, appendSnapshot, appendRecordDetail, activateAdminSection, activateAdminCatalog, bindAdminTablist, clearAdminView, formatValue, loadHostStatus, loadConversation, loadRecords, loadSessions, request, bindEvents, prepareDiagnostic, sendChat, refreshAll, loadSelectedTrend, METRIC_MAP, state};", context);
  return {ui: context.uiTest, tabs, panes, catalogTabs, catalogPanes, elements, context};
}

function setupOverview() {
  const env = setup();
  for (const id of ["overview-update", "overview-stats", "overview-hosts", "overview-host-scope", "overview-attention", "overview-recent", "favorite-trends", "open-auth"]) {
    env.elements.set(id, new Element("div"));
  }
  return env;
}

function contents(element) {
  return all(element, () => true).map((item) => item.textContent).join(" ");
}

test("disconnected overview never describes unloaded alerts or approvals as healthy zero", () => {
  const {ui, elements} = setupOverview();
  ui.renderOverview();
  const stats = elements.get("overview-stats").children;
  for (const index of [1, 2, 3]) {
    assert.equal(stats[index].children[1].textContent, "—");
    assert.ok(!stats[index].className.includes("good"));
  }
  assert.ok(contents(elements.get("overview-attention")).includes("连接后查看运行数据"));
  assert.ok(!contents(elements.get("overview-attention")).includes("暂无待审批或活动告警"));
});

test("overview separates live authorized-host alerts from current-host history", () => {
  const {ui, elements} = setupOverview();
  ui.state.connected = true;
  ui.state.hosts = [{host_id: "node-a", name: "A", enabled: true}, {host_id: "disabled", name: "Disabled", enabled: false}];
  ui.state.hostStatuses.set("node-a", {exporter: {state: "reachable"}, active_alerts: [{alertname: "LiveCPU", state: "firing", severity: "critical"}]});
  ui.state.hostStatuses.set("disabled", {exporter: {state: "reachable"}, active_alerts: [{alertname: "OldDisabled"}]});
  ui.state.hostStatuses.set("revoked", {active_alerts: [{alertname: "PrivateRevoked"}]});
  ui.state.history.alerts = [{alertname: "ResolvedHistory", status: "resolved"}];
  ui.renderOverview();
  const attention = contents(elements.get("overview-attention"));
  assert.ok(attention.includes("LiveCPU"));
  assert.ok(!attention.includes("ResolvedHistory"));
  assert.ok(!attention.includes("OldDisabled"));
  assert.ok(!attention.includes("PrivateRevoked"));
  assert.equal(elements.get("overview-stats").children[2].children[1].textContent, "1");
  assert.ok(contents(elements.get("overview-recent")).includes("ResolvedHistory"));
  assert.equal(elements.get("overview-hosts").children[0].tagName, "button");
  assert.equal(elements.get("overview-hosts").children[1].disabled, true);
});

test("incomplete alert queries remain unknown and do not claim all-clear", () => {
  for (const status of [{exporter: {state: "unreachable"}, active_alerts: []}, {exporter: {state: "reachable"}, alerts_state: "query_error", active_alerts: []}]) {
    const {ui, elements} = setupOverview();
    ui.state.connected = true;
    ui.state.hosts = [{host_id: "node-a", name: "A", enabled: true}];
    ui.state.hostStatuses.set("node-a", status);
    ui.renderOverview();
    assert.equal(elements.get("overview-stats").children[2].children[1].textContent, "—");
    assert.ok(contents(elements.get("overview-attention")).includes("数据未确认"));
    assert.ok(!contents(elements.get("overview-attention")).includes("暂无待审批或活动告警"));
  }
});

test("complete zero alerts are healthy; a failed refresh hides stale operational data", () => {
  const {ui, elements} = setupOverview();
  ui.state.connected = true;
  ui.state.hosts = [{host_id: "node-a", name: "A", enabled: true}];
  ui.state.hostStatuses.set("node-a", {exporter: {state: "reachable"}, active_alerts: []});
  ui.renderOverview();
  assert.equal(elements.get("overview-stats").children[2].children[1].textContent, "0");
  assert.ok(elements.get("overview-stats").children[2].className.includes("good"));
  ui.state.connected = false;
  ui.state.overviewError = "刷新失败，请检查连接后重试";
  ui.renderOverview();
  assert.equal(elements.get("overview-stats").children[2].children[1].textContent, "—");
  assert.ok(contents(elements.get("overview-hosts")).includes("暂不可用"));
  assert.ok(contents(elements.get("overview-update")).includes("刷新失败"));
});

test("favorite trends clear prior host pixels immediately and ignore superseded responses", async () => {
  const {ui, elements, context} = setupOverview();
  const box = elements.get("favorite-trends");
  box.append(new Element("old-host-chart"));
  ui.state.connected = true;
  ui.state.selectedHostId = "node-a";
  ui.state.prefs.favoriteMetrics = ["cpu.usage"];
  let complete;
  context.request = () => new Promise((done) => { complete = done; });
  const pending = ui.loadFavoriteTrends();
  assert.equal(all(box, (item) => item.tagName === "old-host-chart").length, 0);
  assert.equal(box.getAttribute("aria-busy"), "true");
  ui.state.selectedHostId = "node-b";
  const current = new Element("current-host-chart");
  box.replaceChildren(current);
  complete({trend: {points: 1, min: 1, max: 1, avg: 1, latest: 1}});
  await pending;
  assert.equal(box.children[0], current);
});

test("host selection removes prior trends before waiting for host APIs", async () => {
  const {ui, elements, context} = setupOverview();
  ui.state.connected = true;
  ui.state.hosts = [{host_id: "node-a", enabled: true}, {host_id: "node-b", enabled: true}];
  ui.state.selectedHostId = "node-a";
  let complete;
  context.loadHostStatus = () => new Promise((done) => { complete = done; });
  context.loadSessions = async () => {};
  context.loadRecords = async () => {};
  context.renderHostSelectors = context.renderHosts = context.setBusy = () => {};
  const pending = ui.selectHost("node-b");
  assert.ok(contents(elements.get("favorite-trends")).includes("正在加载当前主机"));
  assert.ok(contents(elements.get("overview-recent")).includes("正在加载当前主机"));
  ui.state.selectedHostId = "node-a";
  complete();
  await pending;
});

test("overview refresh preserves keyboard focus on a surviving host action", () => {
  const {ui, elements, context} = setupOverview();
  ui.state.connected = true;
  ui.state.hosts = [{host_id: "node-a", name: "A", enabled: true}];
  ui.renderOverview();
  context.document.activeElement = elements.get("overview-hosts").children[0];
  context.document.querySelectorAll = (selector) => selector === "#view-overview [data-overview-focus]" ? elements.get("overview-hosts").children : [];
  ui.renderOverview();
  assert.equal(elements.get("overview-hosts").children[0].focused, true);
});

test("equal and partially equal samples merge SVG markers without overlapping labels", () => {
  const {ui} = setup();
  const metric = {label: "CPU", unit: "%"};
  const equal = ui.createSummarySvg({min: 0.6, avg: 0.6, latest: 0.6, max: 0.6}, metric);
  assert.equal(all(equal, (item) => item.tagName === "circle").length, 1);
  assert.equal(all(equal, (item) => item.tagName === "text").length, 0);
  const partial = ui.createSummarySvg({min: 0, avg: 0.5, latest: 0.5, max: 1}, metric);
  assert.equal(all(partial, (item) => item.tagName === "circle").length, 3);
  const card = ui.trendCard(metric, {min: 0.6, avg: 0.6, latest: 0.6, max: 0.6, points: 12}, true);
  assert.equal(all(card, (item) => item.className === "range-value").length, 4);
  assert.equal(all(card, (item) => item.className === "range-stable").length, 1);
});

test("no-data interval does not render a range graphic", () => {
  const {ui} = setup();
  const card = ui.trendCard({label: "CPU", unit: "%"}, {points: 0}, true);
  assert.equal(all(card, (item) => item.className === "chart-empty").length, 1);
  assert.equal(all(card, (item) => item.tagName === "svg").length, 0);
});

test("long snapshot shows bounded preview and preserves complete text in disclosure", () => {
  const {ui} = setup();
  const main = new Element("div");
  const long = "<script>" + "x".repeat(300);
  ui.appendSnapshot(main, {cpu: long, memory: 34, disk: {used: 13}, extra: 1});
  const preview = all(main, (item) => item.className === "record-snapshot-chip");
  assert.equal(preview.length, 3);
  assert.ok(preview[0].textContent.length < 80);
  assert.equal(all(main, (item) => item.className === "record-snapshot-more")[0].textContent, "+1 项");
  assert.ok(all(main, (item) => item.tagName === "pre")[0].textContent.includes(long));
  assert.equal(all(main, (item) => item.tagName === "script").length, 0);
  const alert = new Element("div");
  ui.appendRecordDetail(alert, {kindKey: "alerts", detail: {summary: "CPU 告警", snapshot: {cpu: long}}, raw: {}});
  assert.equal(all(alert, (item) => item.className === "record-snapshot").length, 1);
  const detailText = all(alert, (item) => item.className === "record-field")[0];
  assert.ok(all(detailText, (item) => item.tagName === "strong")[0].textContent.includes("CPU 告警"));
  assert.ok(!all(detailText, (item) => item.tagName === "strong")[0].textContent.includes(long));
});

test("admin keyboard tabs preserve one draft across sections and catalogs", () => {
  const {ui, tabs, panes, catalogTabs, catalogPanes} = setup();
  const draft = {config: {rules: [{alertname: "Changed"}]}};
  ui.state.admin = draft;
  ui.bindAdminTablist("admin-tabs", "data-admin-section", ui.activateAdminSection);
  ui.bindAdminTablist("admin-command-tabs", "data-admin-catalog", ui.activateAdminCatalog);
  ui.activateAdminSection("general");
  let prevented = false;
  tabs[0].listeners.keydown({key: "ArrowRight", preventDefault() { prevented = true; }});
  assert.ok(prevented);
  assert.equal(ui.state.adminSection, "rules");
  assert.equal(panes.filter((pane) => !pane.hidden).length, 1);
  assert.equal(tabs[1].getAttribute("aria-selected"), "true");
  tabs[1].listeners.keydown({key: "End", preventDefault() {}});
  assert.equal(ui.state.adminSection, "monitor");
  ui.activateAdminSection("commands");
  catalogTabs[0].listeners.keydown({key: "ArrowRight", preventDefault() {}});
  assert.equal(ui.state.adminCatalog, "agent_actions");
  assert.equal(catalogPanes.filter((pane) => !pane.hidden).length, 1);
  assert.equal(ui.state.admin, draft);
  assert.equal(ui.state.admin.config.rules[0].alertname, "Changed");
});

test("losing administrator access clears previously rendered configuration", () => {
  const {ui, elements} = setup();
  ui.clearAdminView();
  assert.ok(elements.get("admin-content").classes.has("hidden"));
  assert.ok(elements.get("admin-restart-banner").classes.has("hidden"));
  assert.equal(elements.get("admin-summary").textContent, "管理员鉴权成功后加载配置");
  for (const id of ["admin-security", "admin-automation", "admin-rules", "admin-catalog-rule_actions",
    "admin-catalog-agent_actions", "admin-catalog-diagnostics", "admin-hosts",
    "admin-monitor-existing", "admin-host-target", "admin-host-credential",
    "admin-host-diagnostic-checks"]) {
    assert.equal(elements.get(id).children.length, 0);
  }
});

test("monitor host wizard does not discard unsaved admin draft", () => {
  const {ui, elements} = setup();
  ui.state.adminDirty = true;
  ui.state.adminSection = "rules";
  ui.activateAdminSection("monitor");
  assert.equal(ui.state.adminSection, "rules");
  assert.match(elements.get("notice-text").textContent, /先保存或放弃/);
});


test("missing metric values stay unknown instead of displaying zero", () => {
  const {ui} = setup();
  for (const value of [null, undefined, "", " "]) assert.equal(ui.formatValue(value, "%"), "—");
  assert.equal(ui.formatValue(0, "%"), "0.0%");
});

test("late host status is stored under the host that was requested", async () => {
  const {ui, context} = setup();
  let resolve;
  context.testRequest = () => new Promise((done) => { resolve = done; });
  vm.runInNewContext("request = testRequest; renderHosts = () => {}; renderHostDetail = () => {};", context);
  ui.state.selectedHostId = "node-a";
  ui.state.hosts = [{host_id: "node-a"}, {host_id: "node-b"}];
  const pending = ui.loadHostStatus();
  ui.state.selectedHostId = "node-b";
  resolve({host: {host_id: "node-a"}, metrics: {}});
  await pending;
  assert.equal(ui.state.hostStatuses.get("node-a").host.host_id, "node-a");
  assert.equal(ui.state.hostStatuses.has("node-b"), false);
});

test("late conversation responses do not overwrite the selected session", async () => {
  const {ui, context, elements} = setup();
  elements.set("chat-log", new Element("div"));
  elements.set("conversation-title", new Element("div"));
  elements.get("conversation-title").textContent = "Session B";
  let resolve;
  context.testRequest = () => new Promise((done) => { resolve = done; });
  vm.runInNewContext("request = testRequest; updateControls = () => {};", context);
  ui.state.selectedSessionId = "session-a";
  const pending = ui.loadConversation();
  ui.state.selectedSessionId = "session-b";
  resolve({session_id: "session-a", name: "Session A", messages: []});
  await pending;
  assert.equal(elements.get("conversation-title").textContent, "Session B");
});


test("records for a previously selected host do not overwrite current records", async () => {
  const {ui, context, elements} = setup();
  const limit = new Element("input"); limit.value = "50"; elements.set("record-limit", limit);
  const replies = [];
  context.testRequest = () => new Promise((resolve) => replies.push(resolve));
  vm.runInNewContext("request = testRequest; renderRecords = () => {};", context);
  ui.state.selectedHostId = "node-a";
  const pending = ui.loadRecords();
  ui.state.selectedHostId = "node-b";
  ui.state.history = {alerts: [{host_id: "node-b"}]};
  replies[0]({alerts: [{host_id: "node-a"}]});
  replies[1]({inspections: []}); replies[2]({executions: []});
  await pending;
  assert.equal(ui.state.history.alerts[0].host_id, "node-b");
});

test("session selection is reconciled with the current host", async () => {
  const {ui, context} = setup();
  context.testRequest = async () => [{session_id: "session-a", host_id: "node-a"}, {session_id: "session-b", host_id: "node-b"}];
  vm.runInNewContext("request = testRequest; renderSessions = () => {}; loadConversation = async () => {};", context);
  ui.state.selectedHostId = "node-b"; ui.state.selectedSessionId = "session-a";
  await ui.loadSessions();
  assert.equal(ui.state.selectedSessionId, "session-b");
});

test("requests cannot return prior identity data after token changes", async () => {
  const {ui, context} = setup();
  let token = "test-token-a", resolve;
  context.sessionStorage = {getItem: () => token};
  context.AbortController = AbortController;
  context.setTimeout = setTimeout; context.clearTimeout = clearTimeout;
  context.fetch = () => new Promise((done) => { resolve = done; });
  const pending = ui.request("/sessions");
  token = "test-token-b";
  resolve({ok: true, json: async () => [{session_id: "prior-identity-session"}]});
  await assert.rejects(pending, /连接已变化/);
});

test("disconnect clears loaded history and execution data", () => {
  const {ui, context, elements} = setup();
  context.sessionStorage = {removeItem: () => {}};
  context.document.addEventListener = () => {};
  context.document.getElementById = (id) => {
    if (!elements.has(id)) { const item = new Element("div"); item.close = () => {}; elements.set(id, item); }
    return elements.get(id);
  };
  vm.runInNewContext("updateConnection = () => {}; renderHostSelectors = () => {}; renderHosts = () => {}; renderSessions = () => {}; renderApprovals = () => {}; renderRecords = () => {}; renderOverview = () => {};", context);
  ui.bindEvents();
  ui.state.history = {alerts: [{alertname: "test-private-alert"}], remediations: [], advice: []};
  ui.state.inspections = [{host_id: "test-private-host"}];
  ui.state.executions = [{execution_id: "test-private-execution"}];
  elements.get("disconnect").listeners.click();
  assert.equal(ui.state.history.alerts.length, 0);
  assert.equal(ui.state.inspections.length, 0);
  assert.equal(ui.state.executions.length, 0);
});


test("rapid diagnostic clicks create only one session", async () => {
  const {ui, context, elements} = setup();
  const input = new Element("textarea"); input.value = ""; elements.set("chat-message", input);
  let resolve, calls = 0;
  context.testRequest = () => { calls++; return new Promise((done) => { resolve = done; }); };
  vm.runInNewContext("request = testRequest; navigate = () => {}; updateControls = () => {}; loadSessions = async () => {};", context);
  ui.state.selectedHostId = "node-a";
  const first = ui.prepareDiagnostic("diagnostic-one");
  const second = ui.prepareDiagnostic("diagnostic-two");
  assert.equal(calls, 1);
  resolve({session_id: "session-a", host_id: "node-a"});
  await Promise.all([first, second]);
  assert.equal(ui.state.selectedSessionId, "session-a");
  assert.equal(input.value, "diagnostic-one");
});

test("diagnostic creation cannot select a session after switching hosts", async () => {
  const {ui, context, elements} = setup();
  const input = new Element("textarea"); input.value = "host-b-draft"; elements.set("chat-message", input);
  let resolve;
  context.testRequest = () => new Promise((done) => { resolve = done; });
  vm.runInNewContext("request = testRequest; navigate = () => {}; updateControls = () => {}; loadSessions = async () => {};", context);
  ui.state.selectedHostId = "node-a";
  const pending = ui.prepareDiagnostic("host-a-prompt");
  ui.state.selectedHostId = "node-b"; ui.state.selectedSessionId = "session-b";
  resolve({session_id: "session-a", host_id: "node-a"});
  await pending;
  assert.equal(ui.state.selectedSessionId, "session-b");
  assert.equal(input.value, "host-b-draft");
});

test("late chat failure does not restore text into another session", async () => {
  const {ui, context, elements} = setup();
  const input = new Element("textarea"); input.value = "host-a-question"; elements.set("chat-message", input);
  let reject;
  context.testRequest = () => new Promise((_, fail) => { reject = fail; });
  context.sessionStorage = {getItem: () => "test-token"};
  vm.runInNewContext("request = testRequest; updateControls = () => {}; appendPendingTurn = () => ({}); finishPendingTurn = () => {};", context);
  ui.state.selectedSessionId = "session-a";
  const pending = ui.sendChat({preventDefault() {}});
  ui.state.selectedSessionId = "session-b"; input.value = "host-b-draft";
  reject(new Error("test network failure"));
  await pending;
  assert.equal(input.value, "host-b-draft");
});

test("network failure after token change is marked as an obsolete request", async () => {
  const {ui, context} = setup();
  let token = "test-token-a", reject;
  context.sessionStorage = {getItem: () => token};
  context.AbortController = AbortController;
  context.setTimeout = setTimeout; context.clearTimeout = clearTimeout;
  context.fetch = () => new Promise((_, fail) => { reject = fail; });
  const pending = ui.request("/admin/config");
  token = "test-token-b"; reject(new Error("test network failure"));
  await assert.rejects(pending, (error) => error.connectionChanged === true);
});


function setupBoundUi() {
  const result = setup(), {ui, context, elements} = result;
  context.document.addEventListener = () => {};
  context.document.getElementById = (id) => {
    if (!elements.has(id)) {
      const item = new Element("div"); item.value = ""; item.close = () => {}; item.showModal = () => {};
      elements.set(id, item);
    }
    return elements.get(id);
  };
  vm.runInNewContext("updateControls = () => {}; updateConnection = () => {}; renderHostSelectors = () => {}; renderHosts = () => {}; renderSessions = () => {}; renderApprovals = () => {}; renderRecords = () => {}; renderOverview = () => {};", context);
  ui.bindEvents();
  return result;
}

test("changing identity clears the prior administrator draft before reloading", async () => {
  const {ui, context, elements} = setupBoundUi();
  let token = "test-token-a";
  context.sessionStorage = {getItem: () => token, setItem: (_, value) => { token = value; }};
  vm.runInNewContext("refreshAll = async () => {};", context);
  ui.state.admin = {revision: "test-private-revision"}; ui.state.adminDirty = true;
  ui.state.confirmAction = () => {};
  ui.state.sessions = [{session_id: "session-a"}];
  context.document.getElementById("token").value = "test-token-b";
  await elements.get("auth-form").listeners.submit({preventDefault() {}});
  assert.equal(ui.state.admin, null);
  assert.equal(ui.state.adminDirty, false);
  assert.equal(ui.state.sessions.length, 0);
  assert.equal(ui.state.confirmAction, null);
});

test("delete confirmation keeps the session shown when the dialog opened", async () => {
  const {ui, context, elements} = setupBoundUi();
  const paths = [];
  context.testRequest = async (path) => { paths.push(path); return {}; };
  vm.runInNewContext("request = testRequest; loadSessions = async () => {}; loadApprovals = async () => {};", context);
  ui.state.sessions = [{session_id: "session-a", name: "A"}, {session_id: "session-b", name: "B"}];
  ui.state.selectedSessionId = "session-a";
  elements.get("delete-session").listeners.click();
  ui.state.selectedSessionId = "session-b";
  await ui.state.confirmAction();
  assert.equal(paths[0], "/sessions/session-a");
  assert.equal(ui.state.selectedSessionId, "session-b");
});


test("late trend failure cannot replace the current host trend", async () => {
  const {ui, context, elements} = setup();
  const box = new Element("div"); elements.set("host-trend", box);
  let reject;
  context.testRequest = () => new Promise((_, fail) => { reject = fail; });
  vm.runInNewContext("request = testRequest;", context);
  ui.state.selectedHostId = "node-a";
  const pending = ui.loadSelectedTrend();
  ui.state.selectedHostId = "node-b";
  const current = new Element("strong"); current.textContent = "host-b-trend";
  box.replaceChildren(current);
  reject(new Error("host-a-error"));
  await pending;
  assert.equal(box.children[0], current);
});

test("disconnect removes private conversation and host details from the DOM", () => {
  const {context, elements} = setupBoundUi();
  context.sessionStorage = {removeItem: () => {}};
  for (const id of ["chat-log", "host-metrics", "host-active-alerts", "host-trend"]) {
    context.document.getElementById(id).append(new Element("private-data"));
  }
  elements.get("disconnect").listeners.click();
  for (const id of ["chat-log", "host-metrics", "host-active-alerts", "host-trend"]) {
    assert.equal(all(elements.get(id), (item) => item.tagName === "private-data").length, 0);
  }
});



test("refresh blocked by an old token resumes for the current token", async () => {
  const {ui, context} = setup();
  let token = "test-token-a", finishHealth, healthCalls = 0;
  const loadedTokens = [];
  context.sessionStorage = {getItem: () => token};
  context.testHealth = () => ++healthCalls === 1 ? new Promise((done) => { finishHealth = done; }) : Promise.resolve();
  context.testHosts = async () => { loadedTokens.push(token); };
  vm.runInNewContext("loadHealth = testHealth; loadHosts = testHosts; loadAllHostStatuses = async () => {}; loadSessions = async () => {}; loadApprovals = async () => {}; loadRecords = async () => {}; loadAdminAccess = async () => {}; updateControls = () => {}; updateConnection = () => {}; renderOverview = () => {};", context);
  const pending = ui.refreshAll(false);
  token = "test-token-b";
  await ui.refreshAll(false);
  finishHealth();
  await pending;
  assert.deepEqual(loadedTokens, ["test-token-b"]);
  assert.equal(ui.state.busy.has("refresh"), false);
});


test("I/O trend values already expressed in MiB/s are not relabelled as bytes", () => {
  const {ui} = setup();
  for (const id of ["disk.read_bytes", "disk.write_bytes", "network.rx_bytes", "network.tx_bytes"]) {
    const metric = ui.METRIC_MAP.get(id);
    const card = ui.trendCard(metric, {min: 1, avg: 1, latest: 1, max: 1, points: 2}, true);
    const numbers = all(card, (item) => item.tagName === "strong");
    assert.equal(numbers[0].textContent, "1 MiB/s", id);
  }
});


test("real sampled curves use time, preserve gaps and switch devices", () => {
  const {ui} = setup();
  const summary = {step_seconds: 15, series: [
    {labels: {device: "eth0"}, values: [[100, 0], [115, 2], [130, null], [145, 1], [190, 3]]},
    {labels: {device: "eth1"}, values: [[100, 4], [115, 5]]},
  ]};
  const plot = ui.createTrendPlot(summary, {label: "接收", unit: "MiB/s"});
  const paths = all(plot, (item) => item.tagName === "path");
  assert.equal(paths.length, 2);
  assert.equal((paths[0].attributes.d.match(/M/g) || []).length, 3);
  assert.equal((paths[0].attributes.d.match(/L/g) || []).length, 1);
  const dots = all(plot, (item) => item.tagName === "circle");
  assert.notEqual(dots[0].attributes.cy, dots[1].attributes.cy);
  assert.notEqual(dots[0].attributes.cx, dots[1].attributes.cx);
  assert.ok(all(dots[0], (item) => item.tagName === "title")[0].textContent.includes("eth0"));
  const choices = all(plot, (item) => item.tagName === "input");
  choices[0].checked = false; choices[0].listeners.change();
  assert.equal(all(plot, (item) => item.tagName === "path").length, 1);
  assert.equal(ui.trendStep(60), "15s");
  assert.equal(ui.trendStep(1440), "360s");
});

test("only six curves start selected and series overflow is visible", () => {
  const {ui} = setup();
  const plot = ui.createTrendPlot({series_total: 40, truncated: true, series: Array.from({length: 8}, (_, i) => ({labels: {device: "d" + i}, values: [[100, i], [115, i + 1]]}))}, {label: "IO", unit: "%"});
  assert.equal(all(plot, (item) => item.tagName === "path").length, 6);
  assert.ok(all(plot, (item) => item.className === "safety-note")[0].textContent.includes("32"));
});

test("credential secrets and once-only output are cleared when administrator access is lost", () => {
  const {ui, elements, context} = setup();
  const password = new Element("input"), once = new Element("textarea");
  password.value = "ssh-secret"; once.value = "once-only-token";
  const original = context.document.querySelectorAll;
  context.document.querySelectorAll = (selector) => selector === "[data-credential-sensitive]" ? [password, once] : original(selector);
  ui.state.adminCredentials = {revision: "private-state"};
  ui.clearAdminView();
  assert.equal(password.value, ""); assert.equal(once.value, ""); assert.equal(ui.state.adminCredentials, null);
});

test("selected metric shows instant failure separately from a successful zero trend", async () => {
  const {ui, elements, context} = setup();
  elements.set("host-current-metric", new Element("div")); elements.set("host-trend", new Element("div"));
  ui.state.selectedHostId = "node-a";
  context.request = async (path) => { if (!path.includes("/trend?")) throw new Error("502 instant failure"); return {trend: {state: "ok", points: 2, min: 0, max: 0, avg: 0, latest: 0, step_seconds: 15, series: [{labels: {}, values: [[100, 0], [115, 0]]}]}}; };
  await ui.loadSelectedTrend();
  assert.ok(elements.get("host-current-metric").children[0].textContent.includes("即时查询失败"));
  assert.equal(all(elements.get("host-trend"), (item) => item.tagName === "path").length, 1);
});

test("old range response cannot replace newer selected trend", async () => {
  const {ui, elements, context} = setup();
  elements.set("host-trend", new Element("div")); ui.state.selectedHostId = "node-a";
  let finish;
  context.request = () => new Promise((resolve) => { finish = resolve; });
  const old = ui.loadSelectedTrend(); ui.state.prefs.trendMinutes = 360;
  const sentinel = new Element("p"); sentinel.textContent = "new range"; elements.get("host-trend").replaceChildren(sentinel);
  finish({trend: {points: 1, min: 1, max: 1, avg: 1, latest: 1}}); await old;
  assert.equal(elements.get("host-trend").children[0], sentinel);
});

test("favorite metric reports query errors rather than silently claiming no data", async () => {
  const {ui, elements, context} = setup();
  elements.set("favorite-trends", new Element("div")); ui.state.connected = true; ui.state.selectedHostId = "node-a"; ui.state.prefs.favoriteMetrics = ["disk.read_bytes"];
  context.request = async () => { throw new Error("502 monitor failed"); };
  await ui.loadFavoriteTrends();
  assert.ok(all(elements.get("favorite-trends"), (item) => item.textContent.includes("趋势查询失败")).length);
});


test("rotating current login token preserves once-only output when metadata refresh becomes unauthorized", async () => {
  const {ui, elements, context} = setup();
  elements.set("admin-token-once", new Element("textarea"));
  ui.state.isAdmin = true; ui.state.adminSection = "tokens"; ui.state.admin = {revision: "a".repeat(64)}; ui.state.adminCredentials = {revision: "a".repeat(64)};
  context.setBusy = () => {}; context.renderAdmin = () => {}; context.renderCredentials = () => {};
  context.request = async (path) => { if (path.startsWith("/admin/credentials/")) return {revision: "b".repeat(64), token_once: "new-login-token"}; const error = new Error("401 token revoked"); error.status = 401; throw error; };
  await ui.changeCredential("tokens/current/rotate", "POST", {});
  assert.equal(elements.get("admin-token-once").value, "new-login-token");
  assert.equal(ui.state.adminCredentials.token_once, undefined);
});


test("credential panes render only metadata and blank secret fields", () => {
  const {ui, elements} = setup();
  ["admin-tokens", "admin-ssh", "admin-model"].forEach((id) => elements.set(id, new Element("div")));
  ui.state.isAdmin = true; ui.state.admin = {config: {hosts: [{host_id: "node-a", name: "Node A", ssh_ref: "managed-key"}]}};
  ui.state.adminCredentials = {tokens: [{id: "deploy-id", identity: "admin", managed: false}, {id: "managed-id", identity: "alice", managed: true}], ssh: [{id: "managed-key", username: "operator", method: "private_key", managed: true}], model: {configured: true, managed: true, model: "deepseek-chat", base_url: "https://example.invalid", timeout: 30, max_retries: 2, max_tokens: 2048}};
  ui.renderCredentials();
  const fields = ["admin-tokens", "admin-ssh", "admin-model"].flatMap((id) => all(elements.get(id), (el) => el.getAttribute("data-credential-sensitive") === "true"));
  assert.ok(fields.length >= 5);
  assert.ok(fields.every((field) => !field.value));
  assert.ok(all(elements.get("admin-ssh"), (el) => el.tagName === "button" && el.textContent.includes("测试已保存的 SSH")).length);
  assert.equal(all(elements.get("admin-model"), (el) => el.tagName === "input" && el.type === "password")[0].value, "");
});


test("narrow sampled plots retain readable axes and distinct small values", () => {
  const {ui, context} = setup(); context.window = {innerWidth: 390};
  const plot = ui.createTrendPlot({series: [{labels: {}, values: [[100, 0], [115, 0]]}]}, {label: "IO", unit: "MiB/s"});
  assert.equal(all(plot, el => el.tagName === "svg")[0].getAttribute("viewBox"), "0 0 360 270");
  const labels = all(plot, el => el.tagName === "text").map(el=>el.textContent);
  assert.ok(labels.includes("MiB/s") && labels.includes("0.005") && labels.includes("-0.005"));
  context.window.innerWidth = 1500; plot.redrawTrend();
  assert.equal(all(plot, el => el.tagName === "svg")[0].getAttribute("viewBox"), "0 0 720 270");
});

function sshCredentialForm(ssh = []) {
  const result = setup(), {ui, elements, context} = result;
  ["admin-tokens", "admin-ssh", "admin-model"].forEach(id => elements.set(id, new Element("div")));
  ui.state.isAdmin = true; ui.state.adminSection = "ssh";
  ui.state.admin = {config: {hosts: []}};
  ui.state.adminCredentials = {tokens: [], ssh, model: {configured: false}};
  const saves = [];
  context.changeCredential = async (path, method, data) => saves.push({path, method, data});
  ui.renderCredentials();
  const box = elements.get("admin-ssh");
  all(box, el => el.tagName === "textarea")[0].value = "";
  const texts = all(box, el => el.tagName === "input" && el.type === "text");
  return {...result, saves, id: texts[0], username: texts[1],
    password: all(box, el => el.type === "password")[0],
    key: all(box, el => el.type === "file")[0],
    known: all(box, el => el.tagName === "textarea")[0],
    method: all(box, el => el.tagName === "select")[0],
    save: all(box, el => el.tagName === "button" && el.textContent === "保存 SSH 凭据")[0]};
}

test("new password credential explains missing host trust before sending and preserves input", async () => {
  const form = sshCredentialForm();
  form.id.value = "new-password"; form.username.value = "operator"; form.password.value = "local-test-password";
  await form.save.listeners.click();
  assert.equal(form.saves.length, 0);
  assert.equal(form.known.focused, true);
  assert.equal(form.password.value, "local-test-password");
  assert.match(form.elements.get("notice-text").textContent, /known_hosts/);
});

test("password credential accepts trusted hosts without requiring a private key", async () => {
  const form = sshCredentialForm();
  form.id.value = "new-password"; form.username.value = "operator";
  form.password.value = "local-test-password"; form.known.value = "trusted-host-key";
  await form.save.listeners.click();
  assert.equal(form.saves.length, 1);
  assert.equal(form.saves[0].data.method, "password");
  assert.equal(form.saves[0].data.password, "local-test-password");
  assert.equal(form.saves[0].data.known_hosts, "trusted-host-key");
  assert.equal(Object.hasOwn(form.saves[0].data, "private_key"), false);
});

test("existing password credential can preserve blank password and known hosts", async () => {
  const form = sshCredentialForm([{id: "existing", username: "operator", method: "password", managed: true}]);
  form.id.value = "existing"; form.username.value = "operator"; form.known.value = "   ";
  await form.save.listeners.click();
  assert.equal(form.saves.length, 1);
  assert.equal(Object.hasOwn(form.saves[0].data, "known_hosts"), false);
  assert.equal(Object.hasOwn(form.saves[0].data, "password"), false);
});

test("new private key credential still requires its key file", async () => {
  const form = sshCredentialForm();
  form.id.value = "new-key"; form.username.value = "operator"; form.known.value = "trusted-host-key";
  form.method.value = "private_key";
  await form.save.listeners.click();
  assert.equal(form.saves.length, 0);
  assert.equal(form.key.focused, true);
});

test("notice retains the original content slot and ignores its scrolling vertical position", () => {
  const {ui, elements, context} = setup();
  const anchor = new Element("div"), notice = elements.get("notice");
  anchor.getBoundingClientRect = () => ({left: 290, width: 1110, top: -650});
  const header = new Element("header");
  header.getBoundingClientRect = () => ({height: 92});
  context.document.querySelector = () => header;
  context.getComputedStyle = () => ({marginTop: "16px"});
  elements.set("notice-anchor", anchor);
  context.showNotice("test message", "success");
  assert.equal(notice.style.left, "290px");
  assert.equal(notice.style.width, "1110px");
  assert.equal(notice.style["--notice-top"], "108px");
  assert.equal(anchor.hidden, false);
  assert.equal(anchor.style.height, "40px");
  context.hideNotice();
  assert.equal(anchor.hidden, true);
});


test("host access editor binds identity grants, keeps admin all-host controls read-only", () => {
  const {ui, elements} = setup();
  elements.set("admin-host-access", new Element("div"));
  ui.state.admin = {revision: "a".repeat(64), available_identities: ["admin", "alice", "bob"], config: {
    settings: {admin_identities: ["admin"], host_access: {alice: ["node-a"]}},
    hosts: [{host_id: "node-a", name: "A", enabled: true}, {host_id: "node-b", name: "B", enabled: true}]
  }};
  ui.renderAdminHostAccess();
  const cards = elements.get("admin-host-access").children;
  const adminInputs = all(cards[0], (e) => e.tagName === "input");
  assert.equal(adminInputs.length, 2);
  assert.ok(adminInputs.every((e) => e.checked && e.disabled && e.dataset.roleReadonly === "true"));
  const aliceInputs = all(cards[1], (e) => e.tagName === "input");
  assert.ok(aliceInputs[0].checked);
  assert.equal(aliceInputs[1].checked, false);
  aliceInputs[1].checked = true; aliceInputs[1].listeners.change();
  assert.deepEqual(Array.from(ui.state.admin.config.settings.host_access.alice), ["node-a", "node-b"]);
  assert.ok(ui.state.adminDirty);
  const bobInputs = all(cards[2], (e) => e.tagName === "input");
  assert.ok(bobInputs.every((e) => !e.checked));
  const revoke = all(cards[1], (e) => e.tagName === "button")[0];
  revoke.listeners.click();
  assert.equal(ui.state.admin.config.settings.host_access.alice.length, 0);
});

test("host access panel is cleared when administrator access is lost", () => {
  const {ui, elements} = setup();
  elements.set("admin-host-access", new Element("div"));
  elements.get("admin-host-access").append(new Element("input"));
  ui.clearAdminView();
  assert.equal(elements.get("admin-host-access").children.length, 0);
});

test("revoked host refresh removes cached data and selected conversation", async () => {
  const {ui, context, elements} = setup();
  elements.set("chat-log", new Element("div")); elements.get("chat-log").append(new Element("p"));
  ui.state.selectedHostId = "node-a"; ui.state.selectedSessionId = "private";
  ui.state.hosts = [{host_id: "node-a", enabled: true}];
  ui.state.sessions = [{session_id: "private", host_id: "node-a"}];
  ui.state.hostStatuses.set("node-a", {metrics: {cpu: 99}});
  ui.state.history.alerts = [{host_id: "node-a"}];
  context.testRequest = async () => [];
  vm.runInNewContext("request = testRequest; renderHostSelectors = () => {};", context);
  await ui.loadHosts();
  assert.equal(ui.state.selectedHostId, "");
  assert.equal(ui.state.selectedSessionId, "");
  assert.equal(ui.state.hostStatuses.size, 0);
  assert.equal(ui.state.sessions.length, 0);
  assert.equal(ui.state.history.alerts.length, 0);
  assert.equal(elements.get("chat-log").children.length, 0);
});

test("late status response cannot restore a revoked host cache", async () => {
  const {ui, context} = setup();
  let resolve;
  context.testRequest = () => new Promise((done) => { resolve = done; });
  vm.runInNewContext("request = testRequest;", context);
  ui.state.selectedHostId = "node-a"; ui.state.hosts = [{host_id: "node-a"}];
  const pending = ui.loadHostStatus();
  ui.state.hosts = []; ui.state.selectedHostId = "";
  resolve({host: {host_id: "node-a"}, metrics: {cpu: 99}});
  await pending;
  assert.equal(ui.state.hostStatuses.has("node-a"), false);
});


test("stale approver is visible and removable without merging Alice and alice", () => {
  const {ui, elements, context} = setup();
  ui.state.admin = {revision: "a".repeat(64), available_identities: ["admin", "Alice"], config: {
    settings: {admin_identities: ["admin"], approver_identities: ["admin", "alice"],
      approval_mode: "approvers_only", alert_identity: "", host_access: {}}, hosts: []
  }};
  context.Option = function (label, value) {
    const option = new Element("option"); option.textContent = label; option.value = value; return option;
  };
  vm.runInNewContext("showNotice = text => { globalThis.lastNotice = text; };", context);
  ui.renderAdminSecurity();
  const container = elements.get("admin-security");
  const stale = all(container, (e) => e.tagName === "label" &&
    e.children.some((c) => c.textContent.startsWith("alice（")))[0];
  assert.ok(stale);
  assert.ok(stale.children[0].textContent.includes("Alice"));
  const input = all(stale, (e) => e.tagName === "input")[0];
  assert.equal(input.checked, true);
  const active = all(container, (e) => e.tagName === "label" &&
    e.children.some((c) => c.textContent === "Alice"))[0];
  assert.equal(all(active, (e) => e.tagName === "input")[0].checked, false);
  assert.deepEqual(Array.from(ui.state.admin.config.settings.approver_identities), ["admin", "alice"]);
  input.checked = false; input.listeners.change();
  assert.deepEqual(Array.from(ui.state.admin.config.settings.approver_identities), ["admin"]);
  input.checked = true; input.listeners.change();
  assert.equal(input.checked, false);
  assert.ok(context.lastNotice.includes("区分大小写"));
  assert.deepEqual(Array.from(ui.state.admin.config.settings.approver_identities), ["admin"]);
});

test("host grants for Alice and alice remain separate exact identities", () => {
  const {ui, elements} = setup();
  elements.set("admin-host-access", new Element("div"));
  ui.state.admin = {revision: "a".repeat(64), available_identities: ["admin", "Alice", "alice"], config: {
    settings: {admin_identities: ["admin"], host_access: {alice: []}},
    hosts: [{host_id: "node-a", name: "A", enabled: true}]
  }};
  ui.renderAdminHostAccess();
  const card = elements.get("admin-host-access").children.find((e) =>
    e.children.some((c) => c.textContent === "Alice"));
  const input = all(card, (e) => e.tagName === "input")[0];
  input.checked = true; input.listeners.change();
  assert.deepEqual(Array.from(ui.state.admin.config.settings.host_access.Alice), ["node-a"]);
  assert.deepEqual(Array.from(ui.state.admin.config.settings.host_access.alice), []);
});
