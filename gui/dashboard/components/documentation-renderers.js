import { escapeHtml } from "../dom.js";

export function docSourceLooksYaml(doc) {
  const source = [doc.doc_key, doc.title, doc.source_path].join(" ").toLowerCase();
  if (/\.(ya?ml)(\s|$)/.test(source)) return true;
  const content = String(doc.content || "");
  if (/^#{1,6}\s+/m.test(content)) return false;
  const keyLines = content.split(/\r?\n/)
    .filter((line) => /^\s*(?:-\s+)?[A-Za-z0-9_.-][^:]*:\s*.*$/.test(line)).length;
  return keyLines >= 4;
}

export function stripSyntheticDocHeading(content, doc) {
  const lines = String(content || "").split(/\r?\n/);
  if (lines.length < 2) return String(content || "");
  const match = lines[0].match(/^(#{1,6})\s+(.+?)\s*$/);
  if (!match) return String(content || "");
  const heading = match[2].trim();
  const sourceName = String(doc.source_path || "").split(/[\\/]/).pop();
  const generatedHeadings = [doc.title, sourceName, doc.doc_key].filter(Boolean);
  if (!generatedHeadings.includes(heading)) return String(content || "");
  let start = 1;
  while (start < lines.length && !lines[start].trim()) start += 1;
  return lines.slice(start).join("\n");
}

export function buildMarkdownDocTree(content, fallbackTitle) {
  const root = { title: fallbackTitle || "Document", level: 0, body: [], children: [] };
  const stack = [root];
  let sawHeading = false;
  String(content || "").split(/\r?\n/).forEach((line) => {
    const match = line.match(/^(#{1,6})\s+(.+?)\s*$/);
    if (match) {
      sawHeading = true;
      const level = match[1].length;
      const title = match[2].trim();
      const current = stack[stack.length - 1];
      const currentBody = (current.body || []).join("\n").trim();
      if (current.level === level && current.title === title && !currentBody && current.children.length === 0) return;
      while (stack.length > 1 && stack[stack.length - 1].level >= level) stack.pop();
      const node = { title, level, body: [], children: [] };
      stack[stack.length - 1].children.push(node);
      stack.push(node);
      return;
    }
    stack[stack.length - 1].body.push(line);
  });
  if (!sawHeading && root.body.join("\n").trim()) {
    root.children.push({ title: root.title, level: 1, body: root.body, children: [] });
    root.body = [];
  }
  return root;
}

export function yamlNodeFromLine(text) {
  const isListItem = text.startsWith("- ");
  const value = isListItem ? text.slice(2).trim() : text;
  const match = value.match(/^([^:#][^:]*):(?:\s*(.*))?$/);
  if (!match) return { title: isListItem ? `- ${value}` : value, body: [] };
  const key = match[1].trim();
  const scalar = String(match[2] || "").trim();
  return { title: isListItem ? `- ${key}` : key, body: scalar ? [scalar] : [] };
}

export function buildYamlDocTree(content, fallbackTitle) {
  const root = { title: fallbackTitle || "Document", level: 0, body: [], children: [] };
  const stack = [{ indent: -1, node: root }];
  String(content || "").split(/\r?\n/).forEach((line) => {
    if (!line.trim()) return;
    const expanded = line.replace(/\t/g, "  ");
    const indent = expanded.match(/^ */)[0].length;
    const text = expanded.trim();
    if (text === "---" || text === "...") return;
    if (text.startsWith("#")) {
      stack[stack.length - 1].node.body.push(text);
      return;
    }
    while (stack.length > 1 && indent <= stack[stack.length - 1].indent) stack.pop();
    const parsed = yamlNodeFromLine(text);
    const node = { title: parsed.title, level: stack.length, body: parsed.body, children: [] };
    stack[stack.length - 1].node.children.push(node);
    stack.push({ indent, node });
  });
  if (root.children.length === 0) {
    root.body = String(content || "").split(/\r?\n/);
    root.children.push({ title: root.title, level: 1, body: root.body, children: [] });
    root.body = [];
  }
  return root;
}

export function renderDocInline(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

export function renderDocBodyLines(lines) {
  const parts = [];
  let paragraph = [];
  let listItems = [];
  let listType = "ul";
  let codeLines = [];
  let inCode = false;
  const flushParagraph = () => {
    const text = paragraph.join(" ").trim();
    if (text) parts.push(`<p>${renderDocInline(text)}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (listItems.length) parts.push(`<${listType}>${listItems.map((item) => `<li>${renderDocInline(item)}</li>`).join("")}</${listType}>`);
    listItems = [];
  };
  const flushCode = () => {
    if (codeLines.length) parts.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
    codeLines = [];
  };

  String((lines || []).join("\n")).split(/\r?\n/).forEach((line) => {
    const trimmed = line.trim();
    if (trimmed.startsWith("```")) {
      if (inCode) { inCode = false; flushCode(); }
      else { flushParagraph(); flushList(); inCode = true; }
      return;
    }
    if (inCode) { codeLines.push(line); return; }
    if (!trimmed) { flushParagraph(); flushList(); return; }
    const bullet = trimmed.match(/^[-*]\s+(.+)$/);
    const ordered = trimmed.match(/^\d+\.\s+(.+)$/);
    if (bullet || ordered) {
      flushParagraph();
      const nextType = ordered ? "ol" : "ul";
      if (listItems.length && listType !== nextType) flushList();
      listType = nextType;
      listItems.push((bullet || ordered)[1]);
      return;
    }
    flushList();
    paragraph.push(trimmed);
  });
  flushParagraph();
  flushList();
  if (inCode) flushCode();
  return parts.length ? `<div class="doc-node-body">${parts.join("")}</div>` : "";
}

export function docNodeMeta(node) {
  const childCount = (node.children || []).length;
  const lineCount = (node.body || []).filter((line) => String(line || "").trim()).length;
  if (childCount && lineCount) return `${childCount} nodes, ${lineCount} lines`;
  if (childCount) return `${childCount} node${childCount === 1 ? "" : "s"}`;
  if (lineCount) return `${lineCount} line${lineCount === 1 ? "" : "s"}`;
  return "";
}

export function renderDocTreeNode(node, depth = 1) {
  const body = renderDocBodyLines(node.body || []);
  const children = (node.children || []).map((child) => renderDocTreeNode(child, depth + 1)).join("");
  const meta = docNodeMeta(node);
  return `
    <details class="doc-tree-node doc-tree-depth-${Math.min(depth, 6)}"${depth <= 2 ? " open" : ""}>
      <summary>
        <span class="doc-node-title">${escapeHtml(node.title || "Untitled")}</span>
        ${meta ? `<span class="doc-node-meta">${escapeHtml(meta)}</span>` : ""}
      </summary>
      <div class="doc-tree-children">
        ${body}
        ${children}
      </div>
    </details>
  `;
}

export function renderDocTree(doc) {
  const content = stripSyntheticDocHeading(doc.content || "", doc);
  const isYaml = docSourceLooksYaml({ ...doc, content });
  const tree = isYaml ? buildYamlDocTree(content, doc.title) : buildMarkdownDocTree(content, doc.title);
  const nodes = tree.children.length
    ? tree.children.map((node) => renderDocTreeNode(node)).join("")
    : renderDocBodyLines(tree.body || []);
  return `
    <div class="doc-readable">
      <div class="doc-tree ${isYaml ? "yaml-tree" : "markdown-tree"}">
        ${nodes || `<div class="empty doc-tree-empty">No readable sections found.</div>`}
      </div>
      <details class="doc-raw">
        <summary>Raw indexed content</summary>
        <pre class="doc-content-preview">${escapeHtml(doc.content || "")}</pre>
      </details>
    </div>
  `;
}
