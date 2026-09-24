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
    this.classList = {
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
  const context = {document, localStorage: {getItem: () => null}, console};
  vm.runInNewContext(source + "\nglobalThis.uiTest = {createSummarySvg, trendCard, appendSnapshot, appendRecordDetail, activateAdminSection, activateAdminCatalog, bindAdminTablist, clearAdminView, state};", context);
  return {ui: context.uiTest, tabs, panes, catalogTabs, catalogPanes, elements};
}

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
