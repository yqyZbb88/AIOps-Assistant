"use strict";
/* Marked supplies tokens only. All message DOM is built from this allowlist;
 * model output is never interpreted as HTML, CSS or executable code. */
(function (root) {
  const MAX_SOURCE = 65536;
  const MAX_NODES = 5000;
  const MAX_DEPTH = 32;
  const ENTITIES = {amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: "\u00a0"};

  function decodeText(value) {
    return String(value || "").replace(/&(#x[\da-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, function (entity, name) {
      if (name[0] !== "#") return ENTITIES[name.toLowerCase()];
      const code = name[1].toLowerCase() === "x" ? parseInt(name.slice(2), 16) : Number(name.slice(1));
      return code > 0 && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff) ? String.fromCodePoint(code) : "\ufffd";
    });
  }

  function safeLink(value) {
    const href = decodeText(value).trim();
    if (!/^https?:\/\//i.test(href) || /[\u0000-\u0020\u007f-\u009f]/.test(href)) return null;
    try {
      const url = new URL(href);
      return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch (_) { return null; }
  }

  function element(doc, tag, className, text) {
    const item = doc.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  }

  function copyControl(doc, text, label) {
    const group = element(doc, "div", "markdown-copy");
    const button = element(doc, "button", "secondary", label);
    button.type = "button";
    const status = element(doc, "span", "copy-status");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    group.append(button, status);
    button.addEventListener("click", async function () {
      button.disabled = true;
      try {
        if (!root.navigator || !root.navigator.clipboard) throw new Error("clipboard unavailable");
        await root.navigator.clipboard.writeText(text);
        status.textContent = "已复制";
      } catch (_) {
        status.textContent = "自动复制不可用，请复制下方已选文字。";
        let fallback = group.querySelector("textarea");
        if (!fallback) {
          fallback = element(doc, "textarea", "copy-fallback");
          fallback.readOnly = true;
          fallback.rows = 3;
          fallback.setAttribute("aria-label", label + "：手动复制内容");
          group.append(fallback);
        }
        fallback.value = text;
        fallback.focus();
        fallback.select();
      } finally { button.disabled = false; }
    });
    return group;
  }

  function renderTokens(doc, parent, tokens, budget, depth) {
    if (depth > MAX_DEPTH) throw new Error("Markdown nesting limit");
    for (const token of tokens || []) {
      if (++budget.nodes > MAX_NODES) throw new Error("Markdown node limit");
      const nested = function (target, children) { renderTokens(doc, target, children, budget, depth + 1); };
      let item;
      switch (token.type) {
        case "space": case "def": continue;
        case "heading":
          item = element(doc, "h" + Math.min(6, Math.max(3, token.depth + 2)));
          nested(item, token.tokens); break;
        case "paragraph": case "blockquote": case "strong": case "em": case "del":
          item = element(doc, {paragraph: "p", blockquote: "blockquote", strong: "strong", em: "em", del: "del"}[token.type]);
          nested(item, token.tokens); break;
        case "text":
          if (token.tokens) { nested(parent, token.tokens); continue; }
          parent.append(doc.createTextNode(decodeText(token.text))); continue;
        case "escape":
          parent.append(doc.createTextNode(token.text)); continue;
        case "html":
          // Keep HTML visible as authored text, including tags and attributes.
          parent.append(doc.createTextNode(token.raw || token.text || "")); continue;
        case "codespan": item = element(doc, "code", "", token.text); break;
        case "br": case "hr": item = element(doc, token.type); break;
        case "checkbox":
          item = element(doc, "input"); item.type = "checkbox";
          item.checked = Boolean(token.checked); item.disabled = true;
          item.setAttribute("aria-label", token.checked ? "已完成" : "未完成"); break;
        case "link": {
          const href = safeLink(token.href);
          item = element(doc, href ? "a" : "span");
          if (href) {
            item.href = href; item.target = "_blank"; item.rel = "noopener noreferrer";
            item.title = "在新标签页打开";
          }
          nested(item, token.tokens); break;
        }
        case "image":
          item = element(doc, "span", "markdown-image", "[图片：" + decodeText(token.text || "无说明") + "]"); break;
        case "list":
          item = element(doc, token.ordered ? "ol" : "ul");
          if (token.ordered && Number.isSafeInteger(token.start) && token.start > 0) item.start = token.start;
          for (const entry of token.items) {
            if (++budget.nodes > MAX_NODES) throw new Error("Markdown node limit");
            const li = element(doc, "li", entry.task ? "markdown-task" : "");
            nested(li, entry.tokens); item.append(li);
          }
          break;
        case "code": {
          item = element(doc, "section", "markdown-code");
          const head = element(doc, "div", "markdown-code-head");
          const language = String(token.lang || "").trim().split(/\s+/)[0].slice(0, 32);
          head.append(element(doc, "span", "", language || "代码"), copyControl(doc, token.text, "复制代码"));
          const pre = element(doc, "pre"); pre.tabIndex = 0;
          pre.setAttribute("aria-label", (language || "代码") + "代码块，可横向滚动");
          pre.append(element(doc, "code", "", token.text)); item.append(head, pre); break;
        }
        case "table": {
          item = element(doc, "div", "markdown-table"); item.tabIndex = 0;
          item.setAttribute("role", "region"); item.setAttribute("aria-label", "回复中的表格，可横向滚动");
          const table = element(doc, "table"), head = element(doc, "thead"), body = element(doc, "tbody");
          const row = function (cells, isHeader) {
            const tr = element(doc, "tr");
            for (const cell of cells) {
              if (++budget.nodes > MAX_NODES) throw new Error("Markdown node limit");
              const td = element(doc, isHeader ? "th" : "td");
              if (isHeader) td.setAttribute("scope", "col");
              if (["left", "center", "right"].includes(cell.align)) td.className = "align-" + cell.align;
              nested(td, cell.tokens); tr.append(td);
            }
            return tr;
          };
          head.append(row(token.header, true));
          for (const cells of token.rows) body.append(row(cells, false));
          table.append(head, body); item.append(table); break;
        }
        default: parent.append(doc.createTextNode(token.raw || token.text || "")); continue;
      }
      parent.append(item);
    }
  }

  function render(target, source) {
    const text = source == null ? "" : String(source);
    const doc = target.ownerDocument || root.document;
    target.className = "message-body markdown-body";
    try {
      if (text.length > MAX_SOURCE || !root.marked) throw new Error("Markdown unavailable");
      const content = element(doc, "div", "markdown-content");
      renderTokens(doc, content, root.marked.lexer(text, {gfm: true, breaks: true, pedantic: false}), {nodes: 0}, 0);
      target.replaceChildren(content, copyControl(doc, text, "复制原文"));
    } catch (_) {
      target.className = "message-body markdown-body markdown-plain";
      target.replaceChildren(element(doc, "div", "markdown-content", text), copyControl(doc, text, "复制原文"));
    }
  }

  root.ConsoleMarkdown = Object.freeze({render: render});
})(globalThis);
