import {
  $, appPath, asList, escapeHtml, fallbackCopyText, fmtTime, jumpToElement,
  showCopyButtonFeedback,
} from "../dom.js";
import { renderDocTree } from "./documentation-renderers.js";

function quoted(value) {
  return JSON.stringify(String(value ?? ""));
}

export class DocumentationWorkbench {
  constructor({
    state,
    api,
    app,
    documentRef = globalThis.document,
    windowRef = globalThis.window,
    navigatorRef = globalThis.navigator,
    findOpenTodo = () => null,
    loadDashboard = async () => {},
  } = {}) {
    this.state = state;
    this.api = api;
    this.app = app;
    this.document = documentRef;
    this.window = windowRef;
    this.navigator = navigatorRef;
    this.findOpenTodo = findOpenTodo;
    this.loadDashboard = loadDashboard;
    this.copyingTodoKeys = new Set();
  }

  resourceScheme() {
    return this.state.latest?.project?.resource_scheme || "mudra-mcp";
  }

  docResourceUri(docKey) {
    return `${this.resourceScheme()}://doc/${encodeURIComponent(docKey)}`;
  }

  docSearchResourceUri(query) {
    return `${this.resourceScheme()}://docs/search/${encodeURIComponent(query)}`;
  }

  docHttpPath(docKey) {
    return appPath(`/docs/doc?key=${encodeURIComponent(docKey)}`);
  }

  docRefValues(item) {
    const values = [item.doc_key].filter(Boolean);
    if (item.chunk_id !== undefined && item.chunk_id !== null && item.chunk_id !== "") {
      values.push(`chunk_id:${item.chunk_id}`);
    }
    return values;
  }

  docAppendToken(item) {
    return `${this.state.docTodoTargetKey || ""}\n${this.docRefValues(item).join("\n")}`;
  }

  renderDocAppendButton(item) {
    const token = this.docAppendToken(item);
    const isAppending = Boolean(this.state.docAppendingRefKey && this.state.docAppendingRefKey === token);
    const hasTarget = Boolean(this.state.docTodoTargetKey);
    const title = hasTarget ? `Append refs to ${this.state.docTodoTargetKey}` : "Select an open todo first";
    return `
      <button type="button" data-doc-action="append-todo-ref" data-doc-key="${escapeHtml(item.doc_key)}" data-chunk-id="${escapeHtml(item.chunk_id ?? "")}" title="${escapeHtml(title)}"${!hasTarget || isAppending ? " disabled" : ""}>
        ${isAppending ? "Appending" : "Append"}
      </button>
    `;
  }

  renderDocResultMeta(item) {
    const chunkId = item.chunk_id;
    const chunkMeta = chunkId !== undefined && chunkId !== null && chunkId !== ""
      ? `<code>chunk ${escapeHtml(chunkId)}</code>`
      : item.chunk_count !== undefined && item.chunk_count !== null
        ? `<code>${escapeHtml(item.chunk_count)} chunk${Number(item.chunk_count) === 1 ? "" : "s"}</code>`
        : "";
    return `<div class="doc-meta"><code>${escapeHtml(item.doc_key)}</code>${chunkMeta}${item.source_path ? `<code>${escapeHtml(item.source_path)}</code>` : ""}</div>`;
  }

  todoDocRefsText(item) {
    return `doc_keys: ${JSON.stringify(this.docRefValues(item))}`;
  }

  docSearchPrompt(scope, query) {
    const scopePart = scope ? `app_scope=${quoted(scope)}, ` : "";
    return [
      `Search docs first with mudra_doc_search(${scopePart}query=${quoted(query)}).`,
      "Use mudra_doc_get only for returned doc_key or chunk_id targets when snippets are not enough.",
      "Carry relevant doc_keys into the todo or handoff.",
      `MCP resource: ${this.docSearchResourceUri(query)}`,
    ].join(" ");
  }

  docEditHandoffText(item) {
    const sourcePath = item.source_path || "(manual or inferred source)";
    return [
      `Doc handoff for ${item.doc_key}`,
      `Scope: ${item.app_scope || ""}`,
      `Read: mudra_doc_get(doc_key=${quoted(item.doc_key)})`,
      `Source path: ${sourcePath}`,
      `HTTP: ${this.docHttpPath(item.doc_key)}`,
      `MCP resource: ${this.docResourceUri(item.doc_key)}`,
      `Todo refs: ${this.todoDocRefsText(item)}`,
      `After editing source docs, reindex ${item.app_scope || "the affected scope"} and carry updated doc_keys into todos or handoffs.`,
    ].join("\n");
  }

  setStatus(message, isError = false) {
    const status = $("docSearchStatus", this.document);
    if (!status) return;
    status.textContent = message || this.state.docCopyStatus || "";
    status.classList.toggle("error-text", Boolean(isError));
  }

  renderTodoTargetOptions(todos) {
    const select = $("docTodoTarget", this.document);
    if (!select) return;
    const openTodos = asList(todos).filter((todo) => todo.todo_key && todo.is_open !== false);
    if (this.state.docTodoTargetKey && !openTodos.some((todo) => todo.todo_key === this.state.docTodoTargetKey)) this.state.docTodoTargetKey = "";
    select.innerHTML = [
      '<option value="">Todo target</option>',
      ...openTodos.map((todo) => {
        const label = [todo.todo_key, todo.priority || "", todo.app_scope || "unspecified"].filter(Boolean).join(" - ");
        return `<option value="${escapeHtml(todo.todo_key)}">${escapeHtml(label)}</option>`;
      }),
    ].join("");
    select.value = this.state.docTodoTargetKey || "";
  }

  renderDocTodoTargetOptions(todos) {
    this.renderTodoTargetOptions(todos);
  }

  renderTodoHints(todos) {
    const element = $("docTodoHints", this.document);
    if (!element) return;
    const candidates = asList(todos).filter((todo) => this.todoNeedsDocRefs(todo) || todo.is_stale);
    if (candidates.length === 0) { element.innerHTML = ""; return; }
    const shown = candidates.slice(0, 5);
    element.innerHTML = `
      <div class="doc-hint-summary">
        <strong>${escapeHtml(candidates.length)} todo${candidates.length === 1 ? "" : "s"} need docs attention</strong>
        <div class="doc-hint-list">
          ${shown.map((todo) => `
            <div class="doc-hint-item" data-todo-key="${escapeHtml(todo.todo_key || "")}">
              <span><strong>${escapeHtml(todo.todo_key || "")}</strong> <span class="muted">${escapeHtml(todo.app_scope || "unspecified")}</span></span>
              <div class="row-actions">
                <button type="button" data-doc-action="search-todo-docs" data-todo-key="${escapeHtml(todo.todo_key || "")}">Search</button>
                <button type="button" data-doc-action="copy-todo-next-instruction" data-todo-key="${escapeHtml(todo.todo_key || "")}">Copy Prompt</button>
              </div>
            </div>
          `).join("")}
        </div>
      </div>
    `;
  }

  renderDocTodoHints(todos) {
    this.renderTodoHints(todos);
  }

  todoNeedsDocRefs(todo) {
    const advisories = asList(todo.reference_advisories);
    if (advisories.some((item) => item.includes("doc_keys"))) return true;
    return ["code_paths", "symbol_refs", "route_refs", "test_refs"].some((field) => asList(todo[field]).length > 0)
      && asList(todo.doc_keys).length === 0;
  }

  renderResults() {
    const results = this.state.docResults || [];
    const empty = $("docSearchEmpty", this.document);
    if (empty) empty.hidden = results.length !== 0;
    const target = $("docResults", this.document);
    if (!target) return;
    target.innerHTML = results.map((item) => {
      const isSelected = this.state.docDetail?.document?.doc_key === item.doc_key;
      return `
        <li class="${isSelected ? "doc-result selected" : "doc-result"}" data-doc-key="${escapeHtml(item.doc_key)}" data-chunk-id="${escapeHtml(item.chunk_id)}">
          <div class="doc-result-head">
            <button class="link-button doc-title" type="button" data-doc-action="open-doc" data-doc-key="${escapeHtml(item.doc_key)}" data-chunk-id="${escapeHtml(item.chunk_id)}">${escapeHtml(item.title)}</button>
            <span class="badge">${escapeHtml(item.app_scope)}</span>
          </div>
          ${this.renderDocResultMeta(item)}
          <div class="doc-snippet">${escapeHtml(item.snippet)}</div>
          <div class="doc-actions">
            <button type="button" data-doc-action="open-doc" data-doc-key="${escapeHtml(item.doc_key)}" data-chunk-id="${escapeHtml(item.chunk_id)}">Open</button>
            <a class="button-link" href="${this.docHttpPath(item.doc_key)}" target="_blank" rel="noreferrer">JSON</a>
            <button type="button" data-doc-action="copy-doc-key" data-doc-key="${escapeHtml(item.doc_key)}" data-chunk-id="${escapeHtml(item.chunk_id)}">Copy Key</button>
            <button type="button" data-doc-action="copy-todo-ref" data-doc-key="${escapeHtml(item.doc_key)}" data-chunk-id="${escapeHtml(item.chunk_id)}">Todo Ref</button>
            ${this.renderDocAppendButton(item)}
            <button type="button" data-doc-action="copy-resource" data-doc-key="${escapeHtml(item.doc_key)}">Resource</button>
            <button type="button" data-doc-action="copy-edit-handoff" data-doc-key="${escapeHtml(item.doc_key)}" data-chunk-id="${escapeHtml(item.chunk_id)}">Handoff</button>
          </div>
        </li>
      `;
    }).join("");
  }

  renderDetail() {
    const target = $("docDetail", this.document);
    if (!target) return;
    if (this.state.docDetailLoadingKey) {
      target.innerHTML = `<div class="doc-detail-state muted">Loading ${escapeHtml(this.state.docDetailLoadingKey)}</div>`;
      return;
    }
    if (this.state.docDetailError) {
      target.innerHTML = `<div class="doc-detail-state form-error">${escapeHtml(this.state.docDetailError)}</div>`;
      return;
    }
    const doc = this.state.docDetail?.document;
    if (!doc) { target.innerHTML = `<div class="doc-detail-state muted">Select a result.</div>`; return; }
    const item = { doc_key: doc.doc_key, app_scope: doc.app_scope, title: doc.title, source_path: doc.source_path, chunk_id: this.state.docDetailChunkId };
    const chunks = asList(doc.chunks).slice(0, 8);
    target.innerHTML = `
      <div class="doc-detail-head"><div><div class="doc-title">${escapeHtml(doc.title)}</div><div class="muted">${escapeHtml(doc.app_scope)} - ${escapeHtml(doc.source_type)} - ${escapeHtml(doc.updated_at)}</div></div><span class="badge">${doc.truncated ? "truncated" : "full"}</span></div>
      <div class="doc-meta stacked"><code>${escapeHtml(doc.doc_key)}</code>${doc.source_path ? `<code>${escapeHtml(doc.source_path)}</code>` : ""}<code>${escapeHtml(this.docResourceUri(doc.doc_key))}</code></div>
      <div class="doc-actions">
        <a class="button-link" href="${this.docHttpPath(doc.doc_key)}" target="_blank" rel="noreferrer">JSON</a>
        <button type="button" data-doc-action="copy-doc-key" data-doc-key="${escapeHtml(doc.doc_key)}" data-chunk-id="${escapeHtml(this.state.docDetailChunkId || "")}">Copy Key</button>
        <button type="button" data-doc-action="copy-todo-ref" data-doc-key="${escapeHtml(doc.doc_key)}" data-chunk-id="${escapeHtml(this.state.docDetailChunkId || "")}">Todo Ref</button>
        ${this.renderDocAppendButton(item)}
        <button type="button" data-doc-action="copy-resource" data-doc-key="${escapeHtml(doc.doc_key)}">Resource</button>
        <button type="button" data-doc-action="copy-edit-handoff" data-doc-key="${escapeHtml(doc.doc_key)}" data-chunk-id="${escapeHtml(this.state.docDetailChunkId || "")}">Handoff</button>
      </div>
      ${chunks.length ? `<div class="doc-chunks">${chunks.map((chunk) => `<button class="${String(chunk.chunk_id) === String(this.state.docDetailChunkId) ? "selected" : ""}" type="button" data-doc-action="copy-chunk-id" data-chunk-id="${escapeHtml(chunk.chunk_id)}">${escapeHtml(chunk.chunk_id)} ${escapeHtml(chunk.heading || "")}</button>`).join("")}</div>` : ""}
      ${renderDocTree(doc)}
    `;
  }

  render() {
    this.renderResults();
    this.renderDetail();
  }

  renderDocResults() {
    this.renderResults();
  }

  renderDocDetail() {
    this.renderDetail();
  }

  findDocResult(docKey, chunkId = "") {
    return (this.state.docResults || []).find((item) => item.doc_key === docKey && (!chunkId || String(item.chunk_id) === String(chunkId))) || {
      doc_key: docKey,
      chunk_id: chunkId,
      app_scope: this.state.docDetail?.document?.app_scope || $("docScope", this.document)?.value || "",
      title: this.state.docDetail?.document?.title || docKey,
      source_path: this.state.docDetail?.document?.source_path || "",
    };
  }

  async copyText(text, label = "Copied", sourceButton = null) {
    if (!text) return;
    try {
      if (this.navigator?.clipboard?.writeText) await this.navigator.clipboard.writeText(text);
      else fallbackCopyText(text, this.document);
      this.state.docCopyStatus = label;
      this.setStatus(label);
      showCopyButtonFeedback(sourceButton, this.window);
    } catch (err) {
      this.state.docCopyStatus = "";
      this.setStatus(err.message || String(err), true);
    }
    if (this.state.docCopyTimer) this.window.clearTimeout(this.state.docCopyTimer);
    this.state.docCopyTimer = this.window.setTimeout(() => {
      this.state.docCopyStatus = "";
      this.setStatus(this.state.docSearchError, Boolean(this.state.docSearchError));
    }, 1800);
  }

  async runSearch() {
    const generation = this.app.beginRequest("doc-search");
    const query = $("docQuery", this.document)?.value.trim() || "";
    if (!query) { this.setStatus("Enter a search query.", true); return; }
    const appScope = $("docScope", this.document)?.value || "";
    const limit = Number($("docLimit", this.document)?.value) || 8;
    const body = { query, limit };
    if (appScope) body.app_scope = appScope;
    this.state.docSearchError = "";
    this.state.docResults = [];
    this.state.docDetail = null;
    this.state.docDetailChunkId = "";
    this.state.docDetailError = "";
    this.render();
    this.setStatus("Searching...");
    try {
      const data = await this.api.searchDocs(body);
      if (!this.app.isCurrentRequest("doc-search", generation)) return;
      this.state.docResults = data.results || [];
      this.setStatus(`${this.state.docResults.length} result${this.state.docResults.length === 1 ? "" : "s"}`);
    } catch (err) {
      this.state.docSearchError = err.message || String(err);
      this.setStatus(this.state.docSearchError, true);
    }
    this.render();
  }

  runDocSearch() {
    return this.runSearch();
  }

  async openDetail(docKey, chunkId = "") {
    if (!docKey) return;
    const generation = this.app.beginRequest("doc-detail");
    this.state.docDetailLoadingKey = docKey;
    this.state.docDetailChunkId = chunkId || "";
    this.state.docDetailError = "";
    this.render();
    try {
      const data = await this.api.docDetail(docKey);
      if (!this.app.isCurrentRequest("doc-detail", generation)) return;
      if (!data.found) throw new Error(`No document found for ${docKey}`);
      this.state.docDetail = data;
      this.state.docDetailError = "";
    } catch (err) {
      this.state.docDetail = null;
      this.state.docDetailError = err.message || String(err);
    }
    this.state.docDetailLoadingKey = "";
    this.render();
  }

  openDocDetail(docKey, chunkId = "") {
    return this.openDetail(docKey, chunkId);
  }

  scopeDocumentResult(doc) {
    const chunkCount = Number(doc.chunk_count || 0);
    return {
      doc_key: doc.doc_key, app_scope: doc.app_scope, title: doc.title || doc.doc_key,
      source_path: doc.source_path || "", source_type: doc.source_type || "", updated_at: doc.updated_at || "",
      chunk_id: "", chunk_count: chunkCount,
      snippet: doc.summary || [doc.source_path, chunkCount ? `${chunkCount} chunk${chunkCount === 1 ? "" : "s"}` : "", doc.updated_at ? `updated ${fmtTime(doc.updated_at)}` : ""].filter(Boolean).join(" - "),
    };
  }

  async openScope(scope) {
    if (!scope) return;
    const generation = this.app.beginRequest("doc-scope");
    const select = $("docScope", this.document);
    const scopeOptions = Array.from(select?.options || []).map((option) => option.value);
    if (scopeOptions.includes(scope) && select) select.value = scope;
    this.state.docSearchError = "";
    this.state.docResults = [];
    this.state.docDetail = null;
    this.state.docDetailChunkId = "";
    this.state.docDetailError = "";
    this.render();
    this.setStatus(`Opening ${scope}...`);
    jumpToElement($("docSearchSection", this.document));
    try {
      const data = await this.api.docScope(scope);
      if (!this.app.isCurrentRequest("doc-scope", generation)) return;
      this.state.docResults = asList(data.documents).map((doc) => this.scopeDocumentResult(doc));
      this.setStatus(`${scope}: ${this.state.docResults.length} doc${this.state.docResults.length === 1 ? "" : "s"}`);
    } catch (err) {
      this.state.docSearchError = err.message || String(err);
      this.setStatus(this.state.docSearchError, true);
    }
    this.render();
  }

  openDocScope(scope) {
    return this.openScope(scope);
  }

  useScope(scope) {
    const select = $("docScope", this.document);
    if (select) select.value = scope || "";
    if ($( "docQuery", this.document)?.value.trim()) this.runSearch();
    else {
      $("docQuery", this.document)?.focus();
      jumpToElement($("docSearchSection", this.document));
    }
  }

  todoDocSearchQuery(todo) {
    const parts = [
      todo.title,
      ...asList(todo.route_refs),
      ...asList(todo.symbol_refs),
      ...asList(todo.code_paths),
      ...asList(todo.search_queries),
    ].map((part) => String(part || "").trim()).filter(Boolean);
    return parts.join(" ").replace(/\s+/g, " ").slice(0, 220) || todo.todo_key || "documentation";
  }

  searchTodoDocs(todoKey) {
    const todo = this.findOpenTodo(todoKey);
    if (!todo) return;
    const scope = todo.app_scope || "";
    const select = $("docScope", this.document);
    const scopeOptions = Array.from(select?.options || []).map((option) => option.value);
    if (scopeOptions.includes(scope) && select) select.value = scope;
    this.state.docTodoTargetKey = todoKey;
    this.renderTodoTargetOptions((this.state.latest && this.state.latest.open_todos) || []);
    const query = $("docQuery", this.document);
    if (query) query.value = this.todoDocSearchQuery(todo);
    jumpToElement($("docSearchSection", this.document));
    this.runSearch();
  }

  async copyTodoNextInstruction(todoKey, sourceButton = null, variant = "") {
    const todo = this.findOpenTodo(todoKey);
    if (!todo || this.copyingTodoKeys.has(todoKey)) return;
    this.copyingTodoKeys.add(todoKey);
    this.setStatus(`Building prompt for ${todoKey}...`);
    try {
      const data = await this.api.nextInstruction(todoKey, 3, variant);
      await this.copyText(data.instruction || "", "Instruction copied", sourceButton);
    } catch (err) { this.setStatus(err.message || String(err), true); }
    finally { this.copyingTodoKeys.delete(todoKey); }
  }

  handleTargetChange(event) {
    this.state.docTodoTargetKey = event.target.value || "";
    this.render();
  }

  handleDocTodoTargetChange(event) {
    this.handleTargetChange(event);
  }

  async appendDocRefToTodo(item) {
    const todoKey = this.state.docTodoTargetKey || "";
    if (!todoKey) { this.setStatus("Select an open todo before appending.", true); return; }
    const todo = this.findOpenTodo(todoKey);
    if (!todo) {
      this.state.docTodoTargetKey = "";
      this.renderTodoTargetOptions((this.state.latest && this.state.latest.open_todos) || []);
      this.setStatus("Selected todo is no longer open.", true);
      this.render();
      return;
    }
    const docKeys = this.docRefValues(item);
    if (docKeys.length === 0) return;
    const token = this.docAppendToken(item);
    this.state.docAppendingRefKey = token;
    this.setStatus(`Appending refs to ${todoKey}...`);
    this.render();
    try {
      const data = await this.api.appendDocReferences({ todo_key: todoKey, doc_keys: docKeys, actor: "dashboard" });
      if (data.todo) Object.assign(todo, data.todo);
      const appendedCount = asList(data.appended).length;
      this.setStatus(appendedCount ? `Appended ${appendedCount} ref${appendedCount === 1 ? "" : "s"} to ${todoKey}` : `Refs already on ${todoKey}`);
    } catch (err) {
      this.state.docAppendingRefKey = "";
      this.setStatus(err.message || String(err), true);
      this.render();
      return;
    }
    this.state.docAppendingRefKey = "";
    this.state.docResults = this.state.docResults.filter((result) => this.docAppendToken(result) !== token);
    this.render();
    try { await this.loadDashboard(); }
    catch (err) { const updated = $("updatedAt", this.document); if (updated) updated.textContent = err.message || String(err); }
  }

  copyCurrentSearchPrompt(event) {
    const query = $("docQuery", this.document)?.value.trim() || "";
    if (!query) { this.setStatus("Enter a search query.", true); return; }
    this.copyText(this.docSearchPrompt($("docScope", this.document)?.value || "", query), "Search prompt copied", event?.currentTarget || null);
  }

  copyCurrentDocSearchPrompt(event) {
    return this.copyCurrentSearchPrompt(event);
  }

  handleSearchSubmit(event) {
    event.preventDefault();
    return this.runSearch();
  }

  handleClick(event) {
    const button = event.target.closest("[data-doc-action]");
    if (!button) return;
    const action = button.dataset.docAction;
    const docKey = button.dataset.docKey || button.closest("[data-doc-key]")?.dataset.docKey || "";
    const chunkId = button.dataset.chunkId || button.closest("[data-chunk-id]")?.dataset.chunkId || "";
    const item = this.findDocResult(docKey, chunkId);
    if (action === "open-doc") this.openDetail(docKey, chunkId);
    else if (action === "copy-doc-key") this.copyText(docKey, "doc_key copied", button);
    else if (action === "copy-todo-ref") this.copyText(this.todoDocRefsText(item), "Todo ref copied", button);
    else if (action === "append-todo-ref") this.appendDocRefToTodo(item);
    else if (action === "copy-resource") this.copyText(this.docResourceUri(docKey), "Resource copied", button);
    else if (action === "copy-edit-handoff") this.copyText(this.docEditHandoffText(item), "Handoff copied", button);
    else if (action === "copy-chunk-id") this.copyText(`chunk_id:${chunkId}`, "Chunk copied", button);
    else if (action === "search-todo-docs") this.searchTodoDocs(button.dataset.todoKey || "");
    else if (action === "copy-todo-next-instruction") this.copyTodoNextInstruction(button.dataset.todoKey || "", button);
  }

  handleDocWorkbenchClick(event) {
    this.handleClick(event);
  }
}
