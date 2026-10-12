const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag, doc) { this.tagName = tag; this.ownerDocument = doc; this.children = []; this.attributes = {}; this.listeners = {}; this.className = ""; this.ownText = ""; }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map((child) => child.textContent).join(""); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ""; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  querySelector(tag) { return descendants(this).find((item) => item !== this && item.tagName === tag) || null; }
  focus() { this.focused = true; }
  select() { this.selected = true; }
}
function descendants(item) { return [item, ...item.children.flatMap(descendants)]; }
function setup() {
  const doc = {createElement: (tag) => new Element(tag, doc), createTextNode: (text) => { const item = new Element("#text", doc); item.textContent = text; return item; }};
  const copied = [];
  const context = vm.createContext({document: doc, URL, navigator: {clipboard: {writeText: async (text) => copied.push(text)}}});
  for (const file of ["vendor/marked.umd.js", "markdown.js"]) vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/web", file), "utf8"), context);
  const render = (source) => { const body = doc.createElement("div"); context.ConsoleMarkdown.render(body, source); return body; };
  return {context, doc, copied, render};
}

test("common Markdown formats retain structure, nesting and table alignment", () => {
  const {render} = setup();
  const body = render("# 诊断\n\n**重点**、*建议*、~~旧结论~~、`df -h`\n\n> 仅用于诊断\n\n3. 一级\n   - 二级\n\n---\n\n| 指标 | 数值 |\n| :-- | --: |\n| CPU | 0 |\n\n- [x] 已检查\n");
  const elements = descendants(body);
  for (const tag of ["h3", "strong", "em", "del", "code", "blockquote", "ol", "ul", "hr", "table", "thead", "tbody", "th", "td"]) assert.ok(elements.some((item) => item.tagName === tag), tag);
  assert.equal(elements.find((item) => item.tagName === "ol").start, 3);
  assert.ok(elements.some((item) => item.tagName === "td" && item.className === "align-right" && item.textContent === "0"));
  const checkbox = elements.find((item) => item.tagName === "input");
  assert.equal(checkbox.checked, true); assert.equal(checkbox.disabled, true);
  assert.ok(!elements.some((item) => ["h1", "h2"].includes(item.tagName)));
});

test("raw HTML, images and attributes never create executable or fetching elements", () => {
  const {render} = setup();
  const source = '<script>alert(1)</script>\n\n<svg onload="alert(2)"></svg>\n\n<img src="https://tracker.invalid/x" onerror="alert(3)">\n\n![remote](https://tracker.invalid/y)\n\n[link](https://example.com "onclick=alert(4)")';
  const body = render(source), elements = descendants(body);
  assert.ok(body.textContent.includes("<script>"));
  assert.ok(body.textContent.includes("[图片：remote]"));
  assert.ok(!elements.some((item) => ["script", "svg", "img", "iframe", "style"].includes(item.tagName)));
  assert.ok(elements.every((item) => !Object.keys(item.attributes).some((name) => /^on|^style$|^src$/.test(name))));
});

test("only absolute HTTP(S) links without credentials or control characters are active", () => {
  const {render} = setup();
  const source = '[safe](https://example.com/a?q=1&x=2) [bad](javascript:alert(1)) [encoded](jav&#x61;script:alert(1)) [data](data:text/html,hi) [file](file:///x) [relative](/admin) [credentials](https://user:pass@example.com) [newline](https://ex&#10;ample.com) [ref][r]\n\n[r]: http://example.org';
  const links = descendants(render(source)).filter((item) => item.tagName === "a");
  assert.equal(links.length, 2);
  for (const link of links) { assert.match(link.href, /^https?:\/\//); assert.equal(link.target, "_blank"); assert.equal(link.rel, "noopener noreferrer"); }
});

test("escaped punctuation, entities and line breaks are readable without HTML parsing", () => {
  const {render} = setup();
  const body = render('AT&amp;T &lt;node&gt; &#20013;文 \\*literal\\*\n第二行');
  assert.ok(body.textContent.includes("AT&T <node> 中文 *literal*"));
  assert.ok(descendants(body).some((item) => item.tagName === "br"));
});

test("code and original-message copy preserve exact source and never execute it", async () => {
  const {render, copied} = setup();
  const source = "~~~sh\nprintf '<script>x</script>'\n  echo $HOME\n~~~";
  const body = render(source);
  const buttons = descendants(body).filter((item) => item.tagName === "button");
  await buttons[0].listeners.click(); await buttons[1].listeners.click();
  assert.deepEqual(copied, ["printf '<script>x</script>'\n  echo $HOME", source]);
  assert.equal(descendants(body).filter((item) => item.tagName === "script").length, 0);
  assert.equal(descendants(body).find((item) => item.tagName === "pre").tabIndex, 0);
});

test("clipboard rejection offers an accessible selected manual copy field", async () => {
  const {render, context} = setup(); context.navigator.clipboard.writeText = async () => { throw new Error("denied"); };
  const body = render("**原文**"), button = descendants(body).find((item) => item.tagName === "button");
  await button.listeners.click();
  const field = descendants(body).find((item) => item.tagName === "textarea");
  assert.equal(field.value, "**原文**"); assert.ok(field.readOnly && field.focused && field.selected);
  assert.ok(field.attributes["aria-label"]); assert.equal(button.disabled, false);
});

test("missing parser, parser failure, oversized or excessively nested replies fall back without losing text", () => {
  for (const mode of ["missing", "throw", "size", "depth", "nodes"]) {
    const {render, context} = setup();
    let source = "# 完整原文\n**保留**";
    if (mode === "missing") context.marked = null;
    if (mode === "throw") context.marked = {lexer: () => { throw new Error("invalid"); }};
    if (mode === "size") source = "长".repeat(65537);
    if (mode === "depth") source = "> ".repeat(40) + "嵌套";
    if (mode === "nodes") source = "*x* ".repeat(3000);
    const body = render(source);
    assert.ok(body.className.includes("markdown-plain"), mode);
    assert.equal(body.children[0].textContent, source, mode);
  }
});
