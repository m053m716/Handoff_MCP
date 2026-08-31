import {
  $, asList, escapeHtml, fmtTime, setEmpty, todoDisplayStatus,
} from "../dom.js";
import {
  saveTodoScopeFilter,
  todoScopeFilterAll,
  todoScopeFilterUnspecified,
  todoStatusFilters,
} from "../state.js";

const TODO_PRIORITIES = ["P0", "P1", "P2", "P3", "P4"];
const TODO_REF_FIELDS = [
  ["code_paths", "Code"],
  ["symbol_refs", "Symbols"],
  ["route_refs", "Routes"],
  ["doc_keys", "Docs"],
  ["test_refs", "Checks"],
  ["search_queries", "Search"],
];
const VALIDATION_TODO_SCOPES = new Set(["on-device-validation", "user-validation"]);

function renderComplexityBadge(todo) {
  const isCompleted = (todo.status || "").match(/^(done|dropped)$/);
  const complexity = isCompleted ? todo.actual_complexity : todo.planned_complexity;
  if (!complexity) return "";
  const label = isCompleted ? "actual" : "pred";
  const className = isCompleted ? "complexity-badge actual" : "complexity-badge predicted";
  return `<span class="${className}" title="${isCompleted ? "Actual" : "Predicted"} complexity">${escapeHtml(complexity)} <span class="label">${label}</span></span>`;
}

function renderTodoRefs(todo) {
  const groups = TODO_REF_FIELDS.map(([field, label]) => {
    const values = asList(todo[field]);
    if (values.length === 0) return "";
    const shown = values.slice(0, 4).map((value) => `<code>${escapeHtml(value)}</code>`).join(" ");
    const more = values.length > 4 ? ` <span class="muted">+${values.length - 4}</span>` : "";
    return `<div class="todo-ref-group"><span>${escapeHtml(label)}</span>${shown}${more}</div>`;
  }).filter(Boolean);
  return groups.length ? `<div class="todo-refs">${groups.join("")}</div>` : "";
}

function todoNeedsDocRefs(todo) {
  const advisories = asList(todo.reference_advisories);
  if (advisories.some((item) => item.includes("doc_keys"))) return true;
  const hasRefs = ["code_paths", "symbol_refs", "route_refs", "test_refs"]
    .some((field) => asList(todo[field]).length > 0);
  return hasRefs && asList(todo.doc_keys).length === 0;
}

function todoIsValidation(todo) {
  if (VALIDATION_TODO_SCOPES.has(todo?.app_scope || "")) return true;
  return asList(todo?.tags).some((tag) => ["validation", "on-device-validation"].includes(String(tag).trim().toLowerCase()));
}

// A todo is "queued" once the orchestration enqueue path has serialized it into
// the queue but it has not yet been pruned/dispatched. Queued rows render greyed
// (see the .is-queued rule in styles/04-documentation.css) and the wake-ping
// radial action gates on this state.
export function todoIsQueued(todo) {
  return (todo?.status || "") === "queued";
}

function todoDocSearchQuery(todo) {
  const parts = [
    todo.title,
    ...asList(todo.route_refs),
    ...asList(todo.symbol_refs),
    ...asList(todo.code_paths),
    ...asList(todo.search_queries),
  ].map((part) => String(part || "").trim()).filter(Boolean);
  return parts.join(" ").replace(/\s+/g, " ").slice(0, 220) || todo.todo_key || "documentation";
}

function renderTodoAdvisories(todo) {
  const advisories = asList(todo.reference_advisories);
  if (advisories.length === 0) return "";
  return `
    <div class="todo-advisories" role="list" aria-label="Todo advisory checklist">
      ${advisories.map((item) => `<div role="listitem">${escapeHtml(item)}</div>`).join("")}
    </div>
  `;
}

function renderTodoRelated(todo) {
  const related = asList(todo.related_todos);
  if (related.length === 0) return "";
  return `
    <div class="todo-related" role="list" aria-label="Related todos">
      <div class="todo-related-label">Related</div>
      ${related.slice(0, 4).map((item) => {
        const meta = [item.priority, item.app_scope || "unspecified", item.relation_kind || item.relation_source]
          .filter(Boolean).join(" ");
        const rawReason = item.relation_reason || item.group_title || item.group_key || "";
        const reason = rawReason.length > 180 ? `${rawReason.slice(0, 177).trimEnd()}...` : rawReason;
        return `
          <div class="todo-related-item" role="listitem">
            <code>${escapeHtml(item.todo_key || "")}</code>
            <span>${escapeHtml(item.title || "")}</span>
            <span class="muted">${escapeHtml(meta)}</span>
            ${reason ? `<span class="muted">${escapeHtml(reason)}</span>` : ""}
          </div>
        `;
      }).join("")}
      ${related.length > 4 ? `<div class="muted">+${related.length - 4} more related</div>` : ""}
    </div>
  `;
}

function renderGroupPosition(todo, isOpen, error = "") {
  const groupKey = todo.group_key || "";
  const position = Number(todo.group_position);
  const size = Number(todo.group_size);
  if (!groupKey || !Number.isInteger(position) || !Number.isInteger(size) || size < 2) return "";
  const label = `Todo ${position} of ${size} in group ${groupKey}`;
  const editor = isOpen ? `
    <div class="todo-group-order-editor" role="group" aria-label="Reorder ${escapeHtml(groupKey)}">
      <span class="muted">Move</span>
      <button type="button" data-action="move-group-first" data-group-key="${escapeHtml(groupKey)}" data-todo-key="${escapeHtml(todo.todo_key || "")}"${position === 1 ? " disabled" : ""}>First</button>
      <button type="button" data-action="move-group-up" data-group-key="${escapeHtml(groupKey)}" data-todo-key="${escapeHtml(todo.todo_key || "")}"${position === 1 ? " disabled" : ""}>Up</button>
      <button type="button" data-action="move-group-down" data-group-key="${escapeHtml(groupKey)}" data-todo-key="${escapeHtml(todo.todo_key || "")}"${position === size ? " disabled" : ""}>Down</button>
      <button type="button" data-action="move-group-last" data-group-key="${escapeHtml(groupKey)}" data-todo-key="${escapeHtml(todo.todo_key || "")}"${position === size ? " disabled" : ""}>Last</button>
    </div>` : "";
  return `<div class="todo-group-position"><button class="group-position-badge" type="button" data-action="open-group-order" data-group-key="${escapeHtml(groupKey)}" data-todo-key="${escapeHtml(todo.todo_key || "")}" aria-label="${escapeHtml(label)}" aria-expanded="${isOpen ? "true" : "false"}">${position}/${size}</button>${editor}${error ? `<div class="form-error">${escapeHtml(error)}</div>` : ""}</div>`;
}

export class TodoPanel {
  constructor({
    state,
    api,
    app,
    onRefresh,
    onSearchDocs,
    onCopyPrompt,
    onQueue,
    onWake,
    documentRef = globalThis.document,
    windowRef = globalThis.window,
  } = {}) {
    this.state = state;
    this.api = api;
    this.app = app;
    this.onRefresh = onRefresh;
    this.onSearchDocs = onSearchDocs;
    this.onCopyPrompt = onCopyPrompt;
    this.onQueue = onQueue;
    this.onWake = onWake;
    this.document = documentRef;
    this.window = windowRef;
    this.expandedTodoKeys = new Set();
    this.contextMenu = null;
    this.contextMenuReturnFocus = null;
    this.state.groupOrderKey = this.state.groupOrderKey || null;
    this.state.groupOrderTodoKey = this.state.groupOrderTodoKey || null;
    this.state.groupOrderError = this.state.groupOrderError || "";
    this.state.reorderingGroupKey = this.state.reorderingGroupKey || null;
    this.bindContextMenuDismissal();
  }

  bindContextMenuDismissal() {
    if (this.document?.addEventListener) {
      this.document.addEventListener("click", (event) => {
        if (!this.contextMenu?.element?.contains?.(event.target)) this.closeContextMenu();
      });
      this.document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape" || !this.contextMenu) return;
        event.preventDefault?.();
        this.closeContextMenu();
      });
    }
    if (this.window?.addEventListener) {
      const dismiss = () => this.closeContextMenu();
      this.window.addEventListener("scroll", dismiss, { passive: true });
      this.window.addEventListener("resize", dismiss);
    }
  }

  todoRowActions(todo) {
    const todoKey = todo.todo_key || "";
    if (!todoKey) return [];
    const needsDocRefs = todoNeedsDocRefs(todo);
    const hasDocRefs = asList(todo.doc_keys).length > 0;
    const queueBusy = this.state.queueBusyKey === todoKey;
    return [
      {
        action: "queue-dispatch",
        label: "Queue & dispatch",
        ariaLabel: `Queue and dispatch todo ${todoKey}`,
        available: true,
        disabled: queueBusy,
      },
      {
        action: "queue-stage",
        label: "Enqueue only",
        ariaLabel: `Enqueue todo ${todoKey} without dispatching`,
        available: true,
        disabled: queueBusy,
      },
      { action: "queue-todo", label: "Queue", ariaLabel: `Queue todo ${todoKey}`, available: true },
      { action: "wake-orchestrator", label: "Wake", ariaLabel: `Wake orchestrator for ${todoKey}`, available: todo.status === "queued" },
      { action: "search-todo-docs", label: "Search Docs", ariaLabel: `Search docs for ${todoKey}`, available: needsDocRefs },
      { action: "copy-todo-next-instruction", label: "Copy Prompt", ariaLabel: `Copy prompt for ${todoKey}`, available: needsDocRefs || hasDocRefs },
      { action: "copy-todo-validation-failed", label: "Validation Failed", ariaLabel: `Report validation failure for ${todoKey}`, available: todoIsValidation(todo) },
    ].filter((item) => item.available);
  }

  renderTodoRowActions(todo) {
    const todoKey = todo.todo_key || "";
    if (!todoKey) return "";
    return `
      <div class="row-actions todo-row-actions">
        ${this.todoRowActions(todo).map((item) => `<button type="button" data-action="${item.action}" data-todo-key="${escapeHtml(todoKey)}" aria-label="${escapeHtml(item.ariaLabel)}"${item.disabled ? " disabled" : ""}>${item.label}</button>`).join("")}
      </div>
    `;
  }

  normalizeScopeFilter(filter) { return filter || todoScopeFilterAll; }

  scopeLabel(scope) { return scope || "unspecified"; }

  scopeFilterDescription(filter) {
    if (filter === todoScopeFilterUnspecified) return "the unspecified scope filter";
    if (filter && filter !== todoScopeFilterAll) return `scope "${filter}"`;
    return "the current filter";
  }

  todoScopeOptions(currentScope) {
    const scopes = ((this.state.latest && this.state.latest.doc_scopes) || [])
      .map((scope) => scope.scope).filter(Boolean);
    const options = ["", ...scopes];
    if (currentScope && !options.includes(currentScope)) options.push(currentScope);
    return options;
  }

  filterScopeOptions(todos) {
    const docScopes = ((this.state.latest && this.state.latest.doc_scopes) || [])
      .map((scope) => scope.scope).filter(Boolean);
    const todoScopes = asList(todos).map((todo) => todo.app_scope || "").filter(Boolean);
    const options = [todoScopeFilterAll, todoScopeFilterUnspecified];
    [...docScopes, ...todoScopes].forEach((scope) => {
      if (!options.includes(scope)) options.push(scope);
    });
    const current = this.normalizeScopeFilter(this.state.todoScopeFilter);
    if (current !== todoScopeFilterAll && !options.includes(current)) options.push(current);
    return options;
  }

  renderScopeFilter(todos) {
    const select = $("todoScopeFilter", this.document);
    if (!select) return;
    const current = this.normalizeScopeFilter(this.state.todoScopeFilter);
    const options = this.filterScopeOptions(todos);
    select.innerHTML = options.map((option) => {
      const label = option === todoScopeFilterAll ? "All scopes"
        : option === todoScopeFilterUnspecified ? "Unspecified" : this.scopeLabel(option);
      return `<option value="${escapeHtml(option)}">${escapeHtml(label)}</option>`;
    }).join("");
    select.value = options.includes(current) ? current : todoScopeFilterAll;
    if (select.value !== current) {
      this.state.persistTodoScopeFilter(select.value);
    }
  }

  matchesScopeFilter(todo, filter) {
    const appScope = todo.app_scope || "";
    if (filter === todoScopeFilterAll) return true;
    if (filter === todoScopeFilterUnspecified) return !appScope;
    return appScope === filter;
  }

  filteredTodos(todos) {
    const filter = this.normalizeScopeFilter(this.state.todoScopeFilter);
    return asList(todos).filter((todo) => this.matchesScopeFilter(todo, filter));
  }

  renderPruneConfirm(todo) {
    const todoKey = todo.todo_key || "";
    const draft = this.state.pruneDrafts[todoKey] || {};
    const status = draft.status === "dropped" ? "dropped" : "done";
    const detail = draft.detail || "";
    const isPruning = this.state.pruningTodoKey === todoKey;
    const error = this.state.pruneTodoKey === todoKey && this.state.pruneError
      ? `<div class="form-error">${escapeHtml(this.state.pruneError)}</div>` : "";
    return `
      <tr class="todo-prune-row" data-todo-key="${escapeHtml(todoKey)}">
        <td colspan="5">
          <form class="todo-prune-form" data-todo-key="${escapeHtml(todoKey)}">
            <div class="todo-prune-summary"><strong>${escapeHtml(todoKey)}</strong><span class="muted">${escapeHtml(todo.title || "")}</span></div>
            <input name="detail" placeholder="Detail" autocomplete="off" value="${escapeHtml(detail)}" aria-label="Todo prune detail">
            <select name="status" aria-label="Todo prune status">
              <option value="done"${status === "done" ? " selected" : ""}>Done</option>
              <option value="dropped"${status === "dropped" ? " selected" : ""}>Dropped</option>
            </select>
            <div class="todo-prune-actions">
              <button class="danger" type="submit"${isPruning ? " disabled" : ""}>Confirm</button>
              <button type="button" data-action="cancel-prune" data-todo-key="${escapeHtml(todoKey)}"${isPruning ? " disabled" : ""}>Cancel</button>
            </div>
            ${error}
          </form>
        </td>
      </tr>
    `;
  }

  renderPriorityControl(todo) {
    const todoKey = todo.todo_key || "";
    const priority = todo.priority || "P2";
    const isOpen = this.state.priorityTodoKey === todoKey;
    const isUpdating = this.state.updatingPriorityKey === todoKey;
    const error = isOpen && this.state.priorityError
      ? `<div class="form-error priority-error">${escapeHtml(this.state.priorityError)}</div>` : "";
    const options = TODO_PRIORITIES.map((item) =>
      `<option value="${escapeHtml(item)}"${item === priority ? " selected" : ""}>${escapeHtml(item)}</option>`).join("");
    return `
      <div class="priority-control" data-todo-key="${escapeHtml(todoKey)}">
        <button class="badge priority-chip" type="button" data-action="open-priority" data-todo-key="${escapeHtml(todoKey)}" aria-label="Change priority for ${escapeHtml(todoKey)}"${isUpdating ? " disabled" : ""}>${escapeHtml(priority)}</button>
        ${isOpen ? `<div class="priority-dropdown"><select class="priority-select" data-action="change-priority" data-todo-key="${escapeHtml(todoKey)}" aria-label="Priority for ${escapeHtml(todoKey)}"${isUpdating ? " disabled" : ""}>${options}</select>${error}</div>` : ""}
      </div>
    `;
  }

  renderScopeControl(todo) {
    const todoKey = todo.todo_key || "";
    const appScope = todo.app_scope || "";
    const isOpen = this.state.scopeTodoKey === todoKey;
    const isUpdating = this.state.updatingScopeKey === todoKey;
    const error = isOpen && this.state.scopeError
      ? `<div class="form-error scope-error">${escapeHtml(this.state.scopeError)}</div>` : "";
    const options = this.todoScopeOptions(appScope).map((item) =>
      `<option value="${escapeHtml(item)}"${item === appScope ? " selected" : ""}>${escapeHtml(this.scopeLabel(item))}</option>`).join("");
    return `
      <div class="scope-control" data-todo-key="${escapeHtml(todoKey)}">
        <button class="badge scope-chip" type="button" data-action="open-scope" data-todo-key="${escapeHtml(todoKey)}" aria-label="Change scope for ${escapeHtml(todoKey)}"${isUpdating ? " disabled" : ""}>${escapeHtml(this.scopeLabel(appScope))}</button>
        ${isOpen ? `<div class="scope-dropdown"><select class="scope-select" data-action="change-scope" data-todo-key="${escapeHtml(todoKey)}" aria-label="Scope for ${escapeHtml(todoKey)}"${isUpdating ? " disabled" : ""}>${options}</select>${error}</div>` : ""}
      </div>
    `;
  }

  todoLane(todo) {
    return VALIDATION_TODO_SCOPES.has(todo.app_scope || "") ? "validation" : "agent";
  }

  renderTodoRow(todo) {
    const todoKey = todo.todo_key || "";
    const todoStatus = todo.status || "";
    const displayStatus = todoDisplayStatus(todo);
    const isPruneOpen = this.state.pruneTodoKey === todoKey;
    const rowClasses = ["todo-row"];
    if (isPruneOpen) rowClasses.push("todo-selected");
    if (todoStatus) rowClasses.push(`status-${todoStatus}`);
    if (todoIsQueued(todo)) rowClasses.push("is-queued");
    if (todo.is_stale) rowClasses.push("status-stale");
    if ((todo.reference_advisories || []).length) rowClasses.push("has-advisory");
    if ((todo.related_todos || []).length) rowClasses.push("has-related");
    const isExpanded = this.expandedTodoKeys.has(todoKey);
    const groupKey = todo.group_key || "";
    const groupOpen = this.state.groupOrderKey === groupKey
      && this.state.groupOrderTodoKey === todoKey
      && Boolean(groupKey);
    const summaryLabel = `${todoKey}, ${todo.title || "untitled"}, priority ${todo.priority || "P2"}, status ${displayStatus || "unspecified"}, scope ${this.scopeLabel(todo.app_scope)}, updated ${fmtTime(todo.updated_at) || "unspecified"}`;
    const detailLabel = `${isExpanded ? "Hide" : "Show"} details for ${summaryLabel}`;
    const updatedLabel = fmtTime(todo.updated_at) || "unspecified";
    return `
      <tr class="${escapeHtml(rowClasses.join(" "))}" title="Updated ${escapeHtml(updatedLabel)}" data-todo-key="${escapeHtml(todoKey)}" data-todo-status="${escapeHtml(todoStatus)}" data-todo-display-status="${escapeHtml(displayStatus)}" data-todo-stale="${todo.is_stale ? "true" : "false"}" data-todo-advisory="${(todo.reference_advisories || []).length ? "true" : "false"}" data-group-key="${escapeHtml(groupKey)}"
      ><td>${this.renderPriorityControl(todo)}</td><td><div class="todo-key-line"><div><strong>${escapeHtml(todoKey)}</strong><div class="muted">${escapeHtml(displayStatus)}</div>${renderGroupPosition(todo, groupOpen, groupOpen ? this.state.groupOrderError : "")}</div><button class="icon-button danger-icon" type="button" data-action="open-prune" data-todo-key="${escapeHtml(todoKey)}" aria-label="Prune todo ${escapeHtml(todoKey)}" title="Prune todo"><span aria-hidden="true">&#128465;</span></button></div></td>
      <td>${this.renderScopeControl(todo)}</td><td><details class="todo-disclosure" data-todo-key="${escapeHtml(todoKey)}"${isExpanded ? " open" : ""}>
        <summary class="todo-summary" data-action="toggle-todo" data-todo-key="${escapeHtml(todoKey)}" data-summary-label="${escapeHtml(summaryLabel)}" aria-label="${escapeHtml(detailLabel)}">
          <span class="todo-summary-title">${escapeHtml(todo.title || "(untitled)")}</span>
          <span class="todo-disclosure-state todo-disclosure-collapsed">Show details</span>
          <span class="todo-disclosure-state todo-disclosure-expanded">Hide details</span>
        </summary>
        <div class="todo-disclosure-body">
          ${todo.detail ? `<div class="todo-detail-text">${escapeHtml(todo.detail)}</div>` : ""}
          ${renderComplexityBadge(todo)}${renderTodoRefs(todo)}${renderTodoAdvisories(todo)}${renderTodoRelated(todo)}${this.renderTodoRowActions(todo)}
        </div>
      </details></td></tr>
      ${isPruneOpen ? this.renderPruneConfirm(todo) : ""}
    `;
  }

  renderTodoClusters(rows) {
    const clusters = [];
    const grouped = new Map();
    rows.forEach((todo, index) => {
      const groupKey = todo.group_key || "";
      if (!groupKey) {
        clusters.push({ groupKey: "", firstIndex: index, rows: [todo] });
        return;
      }
      let cluster = grouped.get(groupKey);
      if (!cluster) {
        cluster = { groupKey, title: todo.group_title || groupKey, firstIndex: index, rows: [] };
        grouped.set(groupKey, cluster);
        clusters.push(cluster);
      }
      cluster.rows.push(todo);
    });
    clusters.sort((left, right) => left.firstIndex - right.firstIndex || left.groupKey.localeCompare(right.groupKey));
    return clusters.map((cluster) => {
      const ordered = cluster.groupKey
        ? [...cluster.rows].sort((left, right) => Number(left.group_position || 0) - Number(right.group_position || 0))
        : cluster.rows;
      const heading = cluster.groupKey
        ? `<tr class="todo-group-heading"><th colspan="4"><span>${escapeHtml(cluster.title)}</span><code>${escapeHtml(cluster.groupKey)}</code></th></tr>`
        : "";
      const className = cluster.groupKey ? "todo-group-body" : "todo-ungrouped-body";
      return `<tbody class="${className}" data-group-key="${escapeHtml(cluster.groupKey)}">${heading}${ordered.map((todo) => this.renderTodoRow(todo)).join("")}</tbody>`;
    }).join("");
  }

  renderTodoLane(key, title, rows) {
    const headingId = `todo-lane-${key}-heading`;
    const emptyText = key === "validation"
      ? "No human / device validation todos in this result."
      : "No agent todos in this result.";
    return `
      <section class="todo-lane" data-todo-lane="${key}" aria-labelledby="${headingId}">
        <div class="todo-lane-head">
          <h3 id="${headingId}">${title}</h3>
          <span class="muted" data-todo-lane-count="${key}">${rows.length}</span>
        </div>
        <div class="bounded-table-wrap todo-lane-scroll" tabindex="0" role="region" aria-labelledby="${headingId}">
          <table>
            <thead><tr>
              <th class="col-priority">Priority</th>
              <th class="col-task">Todo</th>
              <th class="col-agent-sm">Scope</th>
              <th class="todo-title-col">Title / details</th>
            </tr></thead>
            ${this.renderTodoClusters(rows)}
          </table>
          ${rows.length === 0 ? `<div class="todo-lane-empty muted">${emptyText}</div>` : ""}
        </div>
      </section>
    `;
  }

  renderTodos(todos, totalTodos) {
    const rows = asList(todos);
    const total = Number.isFinite(totalTodos) ? totalTodos : rows.length;
    const filter = this.normalizeScopeFilter(this.state.todoScopeFilter);
    const status = this.state.todoStatusFilter || "open";
    const filtered = filter !== todoScopeFilterAll || this.state.todoPriorityFilter || status !== "open";
    const count = $("todoCount", this.document);
    const empty = $("todoEmpty", this.document);
    const body = $("openTodos", this.document);
    if (count) count.textContent = filtered || this.state.todoNextCursor ? `${rows.length} of ${total} ${status}` : `${rows.length} ${status}`;
    if (empty) empty.textContent = total === 0 ? "No open todos recorded." : `No open todos match ${this.scopeFilterDescription(filter)}.`;
    setEmpty("todoEmpty", rows.length === 0, this.document);
    if (!body) return;
    const lanes = { agent: [], validation: [] };
    rows.forEach((todo) => lanes[this.todoLane(todo)].push(todo));
    body.innerHTML = [
      this.renderTodoLane("agent", "Agent TODOs", lanes.agent),
      this.renderTodoLane("validation", "Human / device validation TODOs", lanes.validation),
    ].join("");
  }

  renderCurrent() {
    this.closeContextMenu({ restoreFocus: false });
    const todos = [
      ...((this.state.latest && this.state.latest.open_todos) || []),
      ...this.validationSupplementRows(),
    ];
    this.renderScopeFilter(todos);
    const total = Number(this.state.latest?.open_todos_meta?.total);
    this.renderTodos(this.filteredTodos(todos), Number.isFinite(total) ? total : todos.length);
    const loadMore = $("todoLoadMore", this.document);
    if (loadMore) loadMore.hidden = !this.state.todoNextCursor;
  }

  validationSupplementRows() {
    const isDefaultView = this.normalizeScopeFilter(this.state.todoScopeFilter) === todoScopeFilterAll
      && (this.state.todoStatusFilter || "open") === "open"
      && !this.state.todoPriorityFilter;
    if (!isDefaultView) return [];
    const existing = new Set(
      asList(this.state.latest && this.state.latest.open_todos)
        .map((todo) => todo.todo_key)
        .filter(Boolean),
    );
    return asList(this.state.todoValidationTodos).filter((todo) => {
      if (!todo.todo_key || existing.has(todo.todo_key)) return false;
      existing.add(todo.todo_key);
      return true;
    });
  }

  findOpenTodo(todoKey) {
    return ((this.state.latest && this.state.latest.open_todos) || []).find((todo) => todo.todo_key === todoKey);
  }

  findRenderedTodo(todoKey) {
    return [
      ...((this.state.latest && this.state.latest.open_todos) || []),
      ...this.validationSupplementRows(),
    ].find((todo) => todo.todo_key === todoKey);
  }

  isContextMenuRow(row) {
    if (!row?.dataset?.todoKey) return false;
    return !["done", "dropped"].includes(row.dataset.todoStatus || "");
  }

  handleContextMenu(event) {
    const row = event.target.closest?.("tr.todo-row[data-todo-key]");
    if (!this.isContextMenuRow(row)) return;
    event.preventDefault?.();
    const x = Number.isFinite(event.clientX) ? event.clientX : Number(event.pageX) || 0;
    const y = Number.isFinite(event.clientY) ? event.clientY : Number(event.pageY) || 0;
    this.openContextMenu(row.dataset.todoKey, x, y, row);
  }

  contextMenuButtons(menu) {
    return Array.from(menu?.querySelectorAll?.("button:not([disabled])") || menu?.buttons || []);
  }

  handleContextMenuKeydown(event, menu) {
    const buttons = this.contextMenuButtons(menu);
    if (buttons.length === 0) return;
    if (event.key === "Escape") {
      event.preventDefault?.();
      this.closeContextMenu();
      return;
    }
    const movement = {
      ArrowRight: 1,
      ArrowDown: 1,
      ArrowLeft: -1,
      ArrowUp: -1,
    }[event.key];
    if (movement || event.key === "Home" || event.key === "End") {
      event.preventDefault?.();
      const current = buttons.indexOf(event.target);
      const next = event.key === "Home"
        ? 0
        : event.key === "End"
          ? buttons.length - 1
          : (Math.max(0, current) + movement + buttons.length) % buttons.length;
      buttons[next]?.focus?.();
    }
  }

  openContextMenu(todoKey, x, y, row) {
    const todo = this.findRenderedTodo(todoKey);
    const host = this.document?.body || $("openTodos", this.document);
    if (!todo || !host || !this.document?.createElement) return;
    const previousMenu = this.contextMenu?.element;
    this.closeContextMenu({ restoreFocus: false });
    const menu = this.document.createElement("div");
    const actions = this.todoRowActions(todo);
    const radialSize = actions.length >= 3 ? 240 : actions.length === 2 ? 216 : 184;
    const margin = 8;
    const viewportWidth = Number(this.window?.innerWidth) || Number(this.document?.documentElement?.clientWidth) || 1024;
    const viewportHeight = Number(this.window?.innerHeight) || Number(this.document?.documentElement?.clientHeight) || 768;
    const radial = viewportWidth >= radialSize + margin * 2 && viewportHeight >= radialSize + margin * 2;
    menu.className = `todo-context-menu has-${actions.length}-actions${radial ? "" : " is-linear"}`;
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", `Actions for ${todoKey}`);
    menu.dataset.todoKey = todoKey;
    menu.style.position = "fixed";
    menu.style["--todo-context-menu-size"] = `${radialSize}px`;
    menu.innerHTML = actions.map((item, index) => {
      return `<button type="button" role="menuitem" data-menu-index="${index}" data-action="${item.action}" data-todo-key="${escapeHtml(todoKey)}" aria-label="${escapeHtml(item.ariaLabel)}">${item.label}</button>`;
    }).join("");
    menu.addEventListener?.("click", (menuEvent) => this.handleClick(menuEvent));
    menu.addEventListener?.("keydown", (menuEvent) => this.handleContextMenuKeydown(menuEvent, menu));
    host.appendChild(menu);
    const rect = menu.getBoundingClientRect?.();
    const width = rect?.width || (radial ? radialSize : 196);
    const height = rect?.height || (radial ? radialSize : Math.max(52, actions.length * 38 + 14));
    const left = Math.max(margin, Math.min(x - width / 2, viewportWidth - width - margin));
    const top = Math.max(margin, Math.min(y - height / 2, viewportHeight - height - margin));
    menu.style.position = "fixed";
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(top)}px`;
    const active = this.document.activeElement;
    this.contextMenuReturnFocus = active && active !== this.document.body && !previousMenu?.contains?.(active)
      ? active
      : row?.querySelector?.(".todo-summary");
    this.contextMenu = { element: menu, todoKey };
    menu.querySelector?.("button:not([disabled])")?.focus?.();
  }

  closeContextMenu({ restoreFocus = true } = {}) {
    const menu = this.contextMenu;
    if (!menu) return;
    this.contextMenu = null;
    menu.element?.remove?.();
    const returnFocus = this.contextMenuReturnFocus;
    this.contextMenuReturnFocus = null;
    if (restoreFocus && returnFocus?.isConnected !== false) returnFocus?.focus?.();
  }

  focusSelector(selector, datasetKey, todoKey) {
    this.window.setTimeout(() => {
      const element = Array.from(this.document.querySelectorAll(selector))
        .find((candidate) => candidate.dataset[datasetKey] === todoKey
          || candidate.closest?.("[data-todo-key]")?.dataset[datasetKey] === todoKey);
      element?.focus();
    }, 0);
  }

  openPrune(todoKey) {
    if (!todoKey) return;
    this.state.pruneTodoKey = todoKey;
    this.state.pruneError = "";
    this.state.pruneDrafts[todoKey] = this.state.pruneDrafts[todoKey] || { detail: "", status: "done" };
    this.renderCurrent();
    this.focusSelector(".todo-prune-row input[name='detail']", "todoKey", todoKey);
  }

  openPriority(todoKey) {
    if (!todoKey) return;
    this.state.priorityTodoKey = this.state.priorityTodoKey === todoKey ? null : todoKey;
    this.state.scopeTodoKey = null;
    this.state.priorityError = "";
    this.renderCurrent();
    this.focusSelector(".priority-select", "todoKey", todoKey);
  }

  openScope(todoKey) {
    if (!todoKey) return;
    this.state.scopeTodoKey = this.state.scopeTodoKey === todoKey ? null : todoKey;
    this.state.priorityTodoKey = null;
    this.state.scopeError = "";
    this.renderCurrent();
    this.focusSelector(".scope-select", "todoKey", todoKey);
  }

  toggleTodoDetails(todoKey, summary) {
    const details = summary?.closest?.("details");
    if (!todoKey || !details) return;
    this.window.setTimeout(() => {
      if (details.open) this.expandedTodoKeys.add(todoKey);
      else this.expandedTodoKeys.delete(todoKey);
      const summaryLabel = summary.dataset.summaryLabel || `todo ${todoKey}`;
      summary.setAttribute("aria-label", `${details.open ? "Hide" : "Show"} details for ${summaryLabel}`);
    }, 0);
  }

  async load({ append = false } = {}) {
    const generation = this.app.beginRequest("todos");
    const scope = this.normalizeScopeFilter(this.state.todoScopeFilter);
    try {
      const data = await this.api.todos({
        status: this.state.todoStatusFilter || "open",
        priority: this.state.todoPriorityFilter,
        appScope: scope !== todoScopeFilterAll && scope !== todoScopeFilterUnspecified ? scope : "",
        cursor: append ? this.state.todoNextCursor : "",
        limit: 20,
      });
      if (!this.app.isCurrentRequest("todos", generation)) return;
      this.state.todoValidationTodos = [];
      const previous = append ? ((this.state.latest && this.state.latest.open_todos) || []) : [];
      if (!this.state.latest) this.state.latest = {};
      this.state.latest.open_todos = previous.concat(asList(data.todos));
      this.state.latest.open_todos_meta = data;
      this.state.todoNextCursor = data.next_cursor || null;
      if (!append && scope === todoScopeFilterAll && this.state.todoStatusFilter === "open" && !this.state.todoPriorityFilter) {
        await this.loadValidationLanes();
      }
      this.renderCurrent();
    } catch (err) {
      const empty = $("todoEmpty", this.document);
      if (empty) { empty.textContent = err.message || String(err); empty.hidden = false; }
    }
  }

  async loadValidationLanes() {
    const generation = this.app.beginRequest("todo-validation-lanes");
    const results = await Promise.all(["on-device-validation", "user-validation"].map(async (appScope) => {
      try {
        return await this.api.todos({ status: "open", appScope, limit: 20 });
      } catch {
        return { todos: [] };
      }
    }));
    if (!this.app.isCurrentRequest("todo-validation-lanes", generation)) return;
    this.state.todoValidationTodos = results.flatMap((result) => asList(result.todos));
  }

  handleScopeFilterChange(event) {
    this.state.persistTodoScopeFilter(this.normalizeScopeFilter(event.target.value));
    if (this.state.todoScopeFilter === todoScopeFilterUnspecified) this.renderCurrent();
    else this.load();
  }

  handleStatusFilterChange(event) {
    this.state.todoStatusFilter = todoStatusFilters.includes(event.target.value) ? event.target.value : "open";
    this.state.todoNextCursor = null;
    this.load();
  }

  handlePriorityFilterChange(event) {
    this.state.todoPriorityFilter = TODO_PRIORITIES.includes(event.target.value) ? event.target.value : "";
    this.state.todoNextCursor = null;
    this.load();
  }

  async updatePriority(todoKey, priority) {
    const todo = this.findOpenTodo(todoKey);
    if (!todo || !TODO_PRIORITIES.includes(priority)) return;
    if (todo.priority === priority) {
      this.state.priorityTodoKey = null;
      this.state.priorityError = "";
      this.renderCurrent();
      return;
    }
    this.state.updatingPriorityKey = todoKey;
    this.state.priorityTodoKey = todoKey;
    this.state.priorityError = "";
    this.renderCurrent();
    try {
      const data = await this.api.updateTodoPriority(todoKey, priority);
      if (data.todo) Object.assign(todo, data.todo);
    } catch (err) {
      this.state.updatingPriorityKey = null;
      this.state.priorityError = err.message || String(err);
      this.renderCurrent();
      return;
    }
    this.state.updatingPriorityKey = null;
    this.state.priorityTodoKey = null;
    this.state.priorityError = "";
    this.renderCurrent();
    await this.refreshSafely();
  }

  async updateScope(todoKey, appScope) {
    const todo = this.findOpenTodo(todoKey);
    const scope = appScope || "";
    if (!todo || !this.todoScopeOptions(todo.app_scope || "").includes(scope)) return;
    if ((todo.app_scope || "") === scope) {
      this.state.scopeTodoKey = null;
      this.state.scopeError = "";
      this.renderCurrent();
      return;
    }
    this.state.updatingScopeKey = todoKey;
    this.state.scopeTodoKey = todoKey;
    this.state.scopeError = "";
    this.renderCurrent();
    try {
      const data = await this.api.updateTodoScope(todoKey, scope);
      if (data.todo) Object.assign(todo, data.todo);
    } catch (err) {
      this.state.updatingScopeKey = null;
      this.state.scopeError = err.message || String(err);
      this.renderCurrent();
      return;
    }
    this.state.updatingScopeKey = null;
    this.state.scopeTodoKey = null;
    this.state.scopeError = "";
    this.renderCurrent();
    await this.refreshSafely();
  }

  cancelPrune(todoKey) {
    if (this.state.pruneTodoKey === todoKey) this.state.pruneTodoKey = null;
    this.state.pruneError = "";
    this.state.pruningTodoKey = null;
    delete this.state.pruneDrafts[todoKey];
    this.renderCurrent();
  }

  syncPruneDraft(form) {
    const todoKey = form.dataset.todoKey;
    if (!todoKey) return;
    this.state.pruneDrafts[todoKey] = {
      detail: form.elements.detail.value,
      status: form.elements.status.value,
    };
  }

  async prune(event) {
    event.preventDefault();
    const form = event.target;
    const todoKey = form.dataset.todoKey;
    if (!todoKey) return;
    this.syncPruneDraft(form);
    const draft = this.state.pruneDrafts[todoKey] || {};
    this.state.pruningTodoKey = todoKey;
    this.state.pruneError = "";
    this.renderCurrent();
    try {
      await this.api.pruneTodo({ todo_key: todoKey, status: draft.status || "done", actor: "dashboard", detail: (draft.detail || "").trim() });
    } catch (err) {
      this.state.pruningTodoKey = null;
      this.state.pruneTodoKey = todoKey;
      this.state.pruneError = err.message || String(err);
      this.renderCurrent();
      return;
    }
    delete this.state.pruneDrafts[todoKey];
    this.state.pruneTodoKey = null;
    this.state.pruningTodoKey = null;
    this.expandedTodoKeys.delete(todoKey);
    if (this.state.latest && Array.isArray(this.state.latest.open_todos)) {
      this.state.latest.open_todos = this.state.latest.open_todos.filter((todo) => todo.todo_key !== todoKey);
    }
    this.renderCurrent();
    await this.refreshSafely();
  }

  async refreshSafely() {
    try { await this.onRefresh(); }
    catch (err) { const updated = $("updatedAt", this.document); if (updated) updated.textContent = err.message || String(err); }
  }

  handleClick(event) {
    const button = event.target.closest?.("[data-action]");
    if (!button) return;
    const todoKey = button.dataset.todoKey || button.closest("tr")?.dataset.todoKey;
    switch (button.dataset.action) {
      case "toggle-todo": this.toggleTodoDetails(todoKey, button); break;
      case "open-prune": this.openPrune(todoKey); break;
      case "cancel-prune": this.cancelPrune(todoKey); break;
      case "open-priority": this.openPriority(todoKey); break;
      case "open-scope": this.openScope(todoKey); break;
      case "search-todo-docs": this.onSearchDocs(todoKey); break;
      case "copy-todo-next-instruction": this.onCopyPrompt(todoKey, button); break;
      case "copy-todo-validation-failed": this.onCopyPrompt(todoKey, button, "validation_failed"); break;
      case "queue-dispatch": this.onQueue(todoKey, button, "dispatch"); break;
      case "queue-stage": this.onQueue(todoKey, button, "enqueue"); break;
      case "queue-todo": this.onQueue(todoKey, button); break;
      case "wake-orchestrator": this.onWake(todoKey, button); break;
      case "open-group-order": {
        const isOpen = this.state.groupOrderKey === button.dataset.groupKey
          && this.state.groupOrderTodoKey === todoKey;
        this.state.groupOrderKey = isOpen ? null : button.dataset.groupKey;
        this.state.groupOrderTodoKey = isOpen ? null : todoKey;
        this.state.groupOrderError = "";
        this.renderCurrent();
        break;
      }
      case "move-group-first": this.moveGroupMember(button.dataset.groupKey, todoKey, "first"); break;
      case "move-group-up": this.moveGroupMember(button.dataset.groupKey, todoKey, "up"); break;
      case "move-group-down": this.moveGroupMember(button.dataset.groupKey, todoKey, "down"); break;
      case "move-group-last": this.moveGroupMember(button.dataset.groupKey, todoKey, "last"); break;
      default: break;
    }
    if (button.closest?.(".todo-context-menu")) this.closeContextMenu();
  }

  async moveGroupMember(groupKey, todoKey, action) {
    if (!groupKey || !todoKey || this.state.reorderingGroupKey) return;
    this.state.reorderingGroupKey = groupKey;
    this.state.groupOrderError = "";
    this.renderCurrent();
    try {
      const current = await this.api.todoGroup(groupKey);
      const members = [...asList(current.group?.members)].sort((left, right) => Number(left.group_position) - Number(right.group_position));
      const index = members.findIndex((member) => member.todo_key === todoKey);
      if (index < 0) throw new Error(`Todo ${todoKey} is not in group ${groupKey}.`);
      const [member] = members.splice(index, 1);
      const target = action === "first" ? 0 : action === "last" ? members.length : action === "up" ? Math.max(0, index - 1) : Math.min(members.length, index + 1);
      members.splice(target, 0, member);
      const result = await this.api.reorderTodoGroup({ group_key: groupKey, todo_keys: members.map((item) => item.todo_key), actor: "dashboard" });
      const updatedMembers = asList(result.group?.members);
      const allRows = asList(this.state.latest?.open_todos).concat(asList(this.state.todoValidationTodos));
      const positions = new Map(updatedMembers.map((item) => [item.todo_key, item.group_position]));
      allRows.forEach((todo) => {
        if (todo.group_key === groupKey && positions.has(todo.todo_key)) todo.group_position = positions.get(todo.todo_key);
      });
      this.state.groupOrderKey = null;
      this.state.groupOrderTodoKey = null;
      const announcement = $("todoGroupAnnouncement", this.document);
      if (announcement) announcement.textContent = `${todoKey} moved ${action} in ${groupKey}.`;
      this.renderCurrent();
      await this.refreshSafely();
    } catch (err) {
      this.state.groupOrderError = err.message || String(err);
      this.renderCurrent();
    } finally {
      this.state.reorderingGroupKey = null;
    }
  }

  handleDraftChange(event) {
    const form = event.target.closest?.(".todo-prune-form");
    if (form) this.syncPruneDraft(form);
  }

  handleChange(event) {
    const prioritySelect = event.target.closest?.('[data-action="change-priority"]');
    if (prioritySelect) this.updatePriority(prioritySelect.dataset.todoKey, prioritySelect.value);
    const scopeSelect = event.target.closest?.('[data-action="change-scope"]');
    if (scopeSelect) this.updateScope(scopeSelect.dataset.todoKey, scopeSelect.value);
    this.handleDraftChange(event);
  }
}

export { TODO_PRIORITIES, todoDocSearchQuery };
