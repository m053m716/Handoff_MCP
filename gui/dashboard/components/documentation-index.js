import { $, asList, appPath, escapeHtml, fmtTime } from "../dom.js";

export class DocumentationIndexPanel {
  constructor({ documentRef = globalThis.document, onUseScope = () => {}, onOpenScope = () => {} } = {}) {
    this.document = documentRef;
    this.onUseScope = onUseScope;
    this.onOpenScope = onOpenScope;
  }

  docScopePath(scope) {
    return appPath(`/docs/scope/${encodeURIComponent(scope)}`);
  }

  render(scopes = [], drift = null) {
    const docs = scopes.reduce((sum, scope) => sum + Number(scope.doc_count || 0), 0);
    const chunks = scopes.reduce((sum, scope) => sum + Number(scope.chunk_count || 0), 0);
    const count = $("docsCount", this.document);
    if (count) count.textContent = `${docs} docs, ${chunks} chunks`;
    const rows = $("docScopes", this.document);
    if (rows) rows.innerHTML = scopes.map((scope) => `
      <tr data-doc-scope="${escapeHtml(scope.scope)}">
        <td><strong>${escapeHtml(scope.scope)}</strong></td>
        <td>
          ${escapeHtml(scope.title)}
          <div class="doc-scope-description muted" title="${escapeHtml(scope.description)}">${escapeHtml(scope.description)}</div>
        </td>
        <td>${escapeHtml(scope.doc_count)}</td>
        <td>${escapeHtml(scope.chunk_count)}</td>
        <td>
          <div class="row-actions">
            <button type="button" data-doc-scope-action="use-scope" data-doc-scope="${escapeHtml(scope.scope)}">Use</button>
            <button type="button" data-doc-scope-action="open-scope" data-doc-scope="${escapeHtml(scope.scope)}">Open</button>
            <a class="button-link" href="${this.docScopePath(scope.scope)}" target="_blank" rel="noreferrer">JSON</a>
          </div>
        </td>
      </tr>
    `).join("");
    this.renderScopeOptions(scopes);
    this.renderDrift(drift);
  }

  renderDocs(scopes = [], drift = null) {
    this.render(scopes, drift);
  }

  renderScopeOptions(scopes = []) {
    const select = $("docScope", this.document);
    if (!select) return;
    const current = select.value;
    const options = scopes.map((scope) => scope.scope).filter(Boolean);
    select.innerHTML = [
      '<option value="">All scopes</option>',
      ...options.map((scope) => `<option value="${escapeHtml(scope)}">${escapeHtml(scope)}</option>`),
    ].join("");
    if (options.includes(current) || current === "") select.value = current;
  }

  renderDocScopeOptions(scopes = []) {
    this.renderScopeOptions(scopes);
  }

  renderDrift(drift) {
    const element = $("docsDriftWarnings", this.document);
    if (!element) return;
    const advisories = asList(drift?.advisories);
    const count = Number(drift?.advisory_count || advisories.length);
    if (advisories.length === 0) {
      element.hidden = true;
      element.innerHTML = "";
      return;
    }
    const suggestions = asList(drift?.suggested_todos);
    const todo = suggestions[0] || null;
    const truncated = Number(drift?.truncated_count || 0);
    element.hidden = false;
    element.innerHTML = `
      <div class="docs-drift-head">
        <div>
          <strong>${escapeHtml(count)} docs drift ${count === 1 ? "advisory" : "advisories"}</strong>
          <div class="muted">Scope ${escapeHtml(drift?.app_scope || "mcp-server")} - latest index ${escapeHtml(fmtTime(drift?.latest_indexed_at) || "not indexed")}</div>
        </div>
        <code>python -m mcp.server --index-docs --doc-scope ${escapeHtml(drift?.app_scope || "mcp-server")}</code>
      </div>
      <div class="docs-drift-list">
        ${advisories.map((item) => {
          const meta = [
            item.path ? `<code>${escapeHtml(item.path)}</code>` : "",
            item.doc_key ? `<code>${escapeHtml(item.doc_key)}</code>` : "",
            item.git_status ? `<span>${escapeHtml(item.git_status)}</span>` : "",
            item.source_mtime ? `<span>source ${escapeHtml(fmtTime(item.source_mtime))}</span>` : "",
            item.indexed_at ? `<span>indexed ${escapeHtml(fmtTime(item.indexed_at))}</span>` : "",
          ].filter(Boolean).join(" ");
          const paths = asList(item.paths).map((path) => `<code>${escapeHtml(path)}</code>`).join(" ");
          return `
            <div class="docs-drift-item ${escapeHtml(item.level || "info")}">
              <div>${escapeHtml(item.message || item.type || "Docs drift advisory")}</div>
              ${meta || paths ? `<div class="doc-meta">${meta}${paths ? ` ${paths}` : ""}</div>` : ""}
              ${item.suggestion ? `<div class="muted">${escapeHtml(item.suggestion)}</div>` : ""}
            </div>
          `;
        }).join("")}
      </div>
      ${truncated ? `<div class="muted docs-drift-more">+${escapeHtml(truncated)} more advisory item${truncated === 1 ? "" : "s"}</div>` : ""}
      ${todo ? `<div class="docs-drift-todo">Suggested follow-up: <code>${escapeHtml(todo.todo_key)}</code> <span class="muted">${escapeHtml(todo.title || "")}</span></div>` : ""}
    `;
  }

  renderDocsDrift(drift) {
    this.renderDrift(drift);
  }

  handleClick(event) {
    const button = event.target.closest("[data-doc-scope-action]");
    if (!button) return;
    const scope = button.dataset.docScope || "";
    if (button.dataset.docScopeAction === "open-scope") this.onOpenScope(scope);
    else this.onUseScope(scope);
  }

  handleDocScopeClick(event) {
    this.handleClick(event);
  }
}
