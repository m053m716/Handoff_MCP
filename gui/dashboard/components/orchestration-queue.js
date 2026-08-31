import {
  $, asList, escapeHtml, fallbackCopyText, fmtTime, setEmpty, showCopyButtonFeedback,
} from "../dom.js";

const TERMINAL = new Set(["completed", "failed", "cancelled", "stale", "dropped"]);
// A terminal row the owner can soft-delete from the list. `completed` is kept
// (it gates the assignment checkpoint) and `dropped` rows are already hidden.
const DROPPABLE = new Set(["cancelled", "failed", "stale"]);

function assignmentLabel(assignment) {
  if (!assignment) return "Unassigned";
  return `${assignment.task_key} / ${assignment.agent_id} / ${assignment.canonical_model_label}`;
}

function checkpointLabel(item) {
  if (!item.checkpoint_required) return "not required";
  const value = item.checkpoint_status || "pending";
  return item.checkpoint_commit ? `${value} ${item.checkpoint_commit.slice(0, 8)}` : value;
}

function cancellationLabel(item) {
  if (item.cancellation_pending || (item.cancel_requested_at && !item.cancel_acknowledged_at)) {
    return "cancellation requested; awaiting child acknowledgement";
  }
  if (item.cancel_acknowledged_at && item.cancel_child_outcome) {
    return `child terminal: ${item.cancel_child_outcome}`;
  }
  return "";
}

export class OrchestrationQueuePanel {
  constructor({
    state, api, onRefresh, documentRef = globalThis.document,
    navigatorRef = globalThis.navigator, windowRef = globalThis.window,
  } = {}) {
    this.state = state;
    this.api = api;
    this.onRefresh = onRefresh;
    this.document = documentRef;
    this.navigator = navigatorRef;
    this.window = windowRef;
  }

  payload(data = this.state.latest) {
    return data?.orchestration || { queue: [], assignments: [] };
  }

  eligibleTodos() {
    const search = (this.state.queueSearch || "").trim().toLowerCase();
    const source = search ? this.state.queueCandidates : this.state.latest?.open_todos;
    return asList(source).filter((todo) => {
      if (!search) return true;
      return [todo.todo_key, todo.title, todo.detail, todo.app_scope]
        .some((value) => String(value || "").toLowerCase().includes(search));
    });
  }

  renderForm(data = this.state.latest) {
    const todos = this.eligibleTodos();
    const assignments = asList(this.payload(data).assignments).filter((item) => item.status === "active" && !item.is_stale);
    const todoSelect = $("queueTodoSelect", this.document);
    const assignmentSelect = $("queueAssignmentSelect", this.document);
    if (todoSelect) {
      todoSelect.innerHTML = todos.length
        ? todos.map((todo) => `<option value="${escapeHtml(todo.todo_key)}">${escapeHtml(todo.todo_key)} — ${escapeHtml(todo.title || "")}</option>`).join("")
        : '<option value="">No eligible open todos</option>';
      if (todos.some((todo) => todo.todo_key === this.state.queueTodoKey)) todoSelect.value = this.state.queueTodoKey;
      this.state.queueTodoKey = todoSelect.value;
    }
    if (assignmentSelect) {
      assignmentSelect.innerHTML = assignments.length
        ? assignments.map((item) => `<option value="${escapeHtml(item.assignment_key)}">${escapeHtml(assignmentLabel(item))}</option>`).join("")
        : '<option value="">Designate an orchestrator with MCP first</option>';
      if (assignments.some((item) => item.assignment_key === this.state.queueAssignmentKey)) assignmentSelect.value = this.state.queueAssignmentKey;
      this.state.queueAssignmentKey = assignmentSelect.value;
    }
    const error = $("queueError", this.document);
    if (error) { error.textContent = this.state.queueError || ""; error.hidden = !this.state.queueError; }
  }

  render(data = this.state.latest) {
    const payload = this.payload(data);
    const queue = asList(payload.queue);
    const assignments = asList(payload.assignments);
    const assignmentByKey = new Map(assignments.map((item) => [item.assignment_key, item]));
    const currentAssignment = assignments.find((item) => item.status === "stopping")
      || assignments.find((item) => item.status === "active" && !item.is_stale)
      || assignments.find((item) => item.status === "active")
      || null;
    const count = $("queueCount", this.document);
    if (count) count.textContent = `${queue.length} records`;
    const promptButton = $("queuePromptButton", this.document);
    if (promptButton) promptButton.disabled = Boolean(this.state.queuePromptBusy);
    const promptStatus = $("queuePromptStatus", this.document);
    if (promptStatus) promptStatus.textContent = this.state.queuePromptStatus || "";
    const wakeStatus = $("queueWakeStatus", this.document);
    if (wakeStatus) wakeStatus.textContent = this.state.queueWakeStatus || "";
    const assignmentSummary = $("orchestratorAssignment", this.document);
    if (assignmentSummary) {
      if (currentAssignment) {
        const recovery = currentAssignment.recovery_required ? "Recovery required" : "No recovery required";
        assignmentSummary.innerHTML = `
          <span class="badge ${escapeHtml(currentAssignment.status)}">${currentAssignment.status === "stopping" ? "Stopping" : "Assigned"}</span>
          <strong>${escapeHtml(currentAssignment.canonical_model_label || "")}</strong>
          <span>${escapeHtml(currentAssignment.client_name || "Unknown client")}</span>
          <span class="muted">${escapeHtml(currentAssignment.assignment_key)}</span>
          <span class="muted">Lease: ${escapeHtml(currentAssignment.lease_state || "unknown")}; heartbeat ${escapeHtml(fmtTime(currentAssignment.heartbeat_at))}; expires ${escapeHtml(fmtTime(currentAssignment.expires_at))}</span>
          <span class="muted">${escapeHtml(recovery)}</span>`;
      } else {
        assignmentSummary.innerHTML = '<span class="badge">Unassigned</span><span class="muted">Paste the bootstrap prompt into a new Codex or Claude Code session to designate an orchestrator.</span>';
      }
    }
    const stopButton = $("stopOrchestratorButton", this.document);
    if (stopButton) {
      stopButton.hidden = !currentAssignment;
      stopButton.disabled = Boolean(this.state.queueStopBusy) || currentAssignment?.status === "stopping";
      stopButton.dataset.assignmentKey = currentAssignment?.assignment_key || "";
      stopButton.textContent = currentAssignment?.status === "stopping" ? "Stopping…" : "Stop Orchestrator";
    }
    setEmpty("queueEmpty", queue.length === 0, this.document);
    const body = $("orchestrationQueue", this.document);
    if (body) body.innerHTML = queue.map((item) => {
      const assignment = assignmentByKey.get(item.assignment_key);
      const busy = this.state.queueBusyKey === item.queue_key;
      const cancellationPending = Boolean(item.cancellation_pending || (item.cancel_requested_at && !item.cancel_acknowledged_at));
      const canRetry = ["failed", "stale"].includes(item.status) || (item.status === "cancelled" && !cancellationPending);
      const canCancel = !TERMINAL.has(item.status) && !cancellationPending;
      const canDrop = DROPPABLE.has(item.status) && !cancellationPending;
      const error = item.error || item.checkpoint_error || "";
      const cancellation = cancellationLabel(item);
      return `<tr data-queue-key="${escapeHtml(item.queue_key)}">
        <td><span class="badge ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>${item.retry_count ? `<div class="muted">retry ${item.retry_count}</div>` : ""}${cancellation ? `<div class="muted">${escapeHtml(cancellation)}</div>` : ""}</td>
        <td><strong>${escapeHtml(item.todo_key)}</strong><div class="muted">#${escapeHtml(item.position)}</div></td>
        <td><div>${escapeHtml(assignmentLabel(assignment) || item.canonical_model_label)}</div>
          <details class="queue-prompt"><summary>Prompt snapshot</summary><pre>${escapeHtml(item.prompt_snapshot || "")}</pre></details>
          ${error ? `<div class="form-error">${escapeHtml(error)}</div>` : ""}</td>
        <td><span class="badge ${escapeHtml(item.checkpoint_status || "pending")}">${escapeHtml(checkpointLabel(item))}</span><div class="muted">${escapeHtml(item.checkpoint_failure_policy || "pause")}</div></td>
        <td>${escapeHtml(fmtTime(item.updated_at))}</td>
        <td><div class="row-actions queue-row-actions">
          ${canRetry ? `<button type="button" data-action="retry-queue" data-queue-key="${escapeHtml(item.queue_key)}"${busy ? " disabled" : ""}>Retry</button>` : ""}
          ${canCancel ? `<button type="button" class="danger" data-action="cancel-queue" data-queue-key="${escapeHtml(item.queue_key)}"${busy ? " disabled" : ""}>Cancel</button>` : ""}
          ${canDrop ? `<button type="button" data-action="drop-queue" data-queue-key="${escapeHtml(item.queue_key)}"${busy ? " disabled" : ""}>Drop</button>` : ""}
        </div></td>
      </tr>`;
    }).join("");
    this.renderForm(data);
  }

  selectTodo(todoKey) {
    this.state.queueTodoKey = todoKey;
    this.state.queueSearch = "";
    this.state.queueCandidates = [];
    const search = $("queueTodoSearch", this.document);
    if (search) search.value = "";
    this.renderForm();
    $("orchestrationQueueSection", this.document)?.scrollIntoView({ behavior: "smooth", block: "start" });
    $("queueTodoSelect", this.document)?.focus();
  }

  activeAssignments(data = this.state.latest) {
    return asList(this.payload(data).assignments)
      .filter((item) => item.status === "active" && !item.is_stale);
  }

  // The primary radial action confirms the complete enqueue -> prune handoff.
  // The secondary action stages the queue record without pruning the source
  // TODO. With exactly one active orchestrator there is no picker; with more
  // than one, a lightweight inline picker resolves the target; with none, the
  // manual add-form guidance is shown.
  queueFromRadial(todoKey, anchor = null, mode = "dispatch") {
    if (!todoKey) return;
    const assignments = this.activeAssignments();
    if (assignments.length === 0) {
      this.state.queueError = "Designate an orchestrator with MCP before queuing work.";
      this.selectTodo(todoKey);
      return;
    }
    if (assignments.length === 1) {
      this.enqueueTodo(todoKey, assignments[0].assignment_key, { dispatch: mode === "dispatch" });
      return;
    }
    this.showAssignmentPicker(todoKey, assignments, anchor, mode === "dispatch");
  }

  async wakeFromRadial(todoKey, anchor = null) {
    if (!todoKey || this.state.queueWakeBusyKey) return;
    const assignments = this.activeAssignments();
    if (assignments.length === 0) {
      this.state.queueWakeStatus = "Wake unavailable: designate an active orchestrator with MCP first.";
      this.selectTodo(todoKey);
      this.render();
      return;
    }
    if (assignments.length === 1) {
      return this.wakeOrchestrator(todoKey, assignments[0]);
    }
    this.showAssignmentPicker(todoKey, assignments, anchor, "wake");
  }

  async wakeOrchestrator(todoKey, assignment) {
    if (!todoKey || !assignment?.assignment_key || this.state.queueWakeBusyKey) return;
    this.state.queueWakeBusyKey = todoKey;
    this.state.queueWakeStatus = `Waking ${assignment.canonical_model_label || "the orchestrator"}…`;
    this.render();
    try {
      const result = await this.api.wakeOrchestration({
        assignment_key: assignment.assignment_key,
        task_key: assignment.task_key,
        agent_id: assignment.agent_id,
        wait_seconds: 0,
      });
      const queueItem = result?.queue_item;
      if (result?.action === "dispatch") {
        const target = queueItem?.todo_key || todoKey;
        this.state.queueWakeStatus = `Wake found dispatched work for ${target}; the orchestrator can claim it.`;
      } else if (result?.action === "stop") {
        this.state.queueWakeStatus = "Wake reached a stopping orchestrator; no work was delivered.";
      } else if (result?.action === "cancel") {
        const target = queueItem?.todo_key || todoKey;
        this.state.queueWakeStatus = `Wake found pending cancellation for ${target}; the orchestrator must acknowledge it.`;
      } else if (result?.action === "idle") {
        const current = asList(this.payload().queue).find((item) => item.todo_key === todoKey);
        const state = current?.status ? ` Current queue state: ${current.status}.` : "";
        this.state.queueWakeStatus = `Wake checked ${todoKey}; no dispatched work was available.${state}`;
      } else {
        this.state.queueWakeStatus = `Wake returned ${result?.action || "an unknown state"}.`;
      }
      await this.onRefresh();
    } catch (err) {
      this.state.queueWakeStatus = `Wake failed: ${err.message || String(err)}`;
    } finally {
      this.state.queueWakeBusyKey = "";
      this.render();
    }
  }

  showAssignmentPicker(todoKey, assignments, anchor = null, mode = "queue") {
    this.closeAssignmentPicker();
    const doc = this.document;
    if (!doc?.createElement) {
      // No live DOM (tests / SSR): fall back to prefilling the manual form.
      this.selectTodo(todoKey);
      return;
    }
    const menu = doc.createElement("div");
    const isWake = mode === "wake";
    const dispatch = mode === true || mode === "dispatch";
    let headLabel;
    if (isWake) headLabel = "Wake";
    else if (dispatch) headLabel = "Queue & dispatch";
    else headLabel = "Enqueue only";
    let ariaAction;
    if (isWake) ariaAction = "wake for";
    else if (dispatch) ariaAction = "queue and dispatch";
    else ariaAction = "enqueue";
    menu.className = `queue-assignment-picker${isWake ? " wake-assignment-picker" : ""}`;
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", `Choose an orchestrator to ${ariaAction} ${todoKey}`);
    menu.innerHTML = `<div class="queue-picker-head">${headLabel} orchestrator</div>${
      assignments.map((item) => `<button type="button" role="menuitem" data-assignment-key="${escapeHtml(item.assignment_key)}">${escapeHtml(assignmentLabel(item))}</button>`).join("")
    }`;
    menu.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-assignment-key]");
      if (!button) return;
      const assignmentKey = button.dataset.assignmentKey;
      this.closeAssignmentPicker();
      const assignment = assignments.find((item) => item.assignment_key === assignmentKey);
      if (isWake) this.wakeOrchestrator(todoKey, assignment);
      else this.enqueueTodo(todoKey, assignmentKey, { dispatch });
    });
    const rect = anchor?.getBoundingClientRect?.();
    if (rect) {
      menu.style.position = "fixed";
      menu.style.top = `${Math.round(rect.bottom + 4)}px`;
      menu.style.left = `${Math.round(rect.left)}px`;
    }
    (doc.body || doc.documentElement)?.appendChild(menu);
    this.assignmentPicker = menu;
    const dismiss = (event) => {
      if (menu.contains?.(event?.target)) return;
      this.closeAssignmentPicker();
    };
    this.assignmentPickerDismiss = dismiss;
    this.window?.addEventListener?.("click", dismiss, { capture: true });
    this.window?.addEventListener?.("keydown", this.assignmentPickerKeydown = (event) => {
      if (event.key === "Escape") this.closeAssignmentPicker();
    });
    menu.querySelector("button")?.focus?.();
  }

  closeAssignmentPicker() {
    if (this.assignmentPickerDismiss) {
      this.window?.removeEventListener?.("click", this.assignmentPickerDismiss, { capture: true });
      this.assignmentPickerDismiss = null;
    }
    if (this.assignmentPickerKeydown) {
      this.window?.removeEventListener?.("keydown", this.assignmentPickerKeydown);
      this.assignmentPickerKeydown = null;
    }
    this.assignmentPicker?.remove?.();
    this.assignmentPicker = null;
  }

  async refreshAfterMutation() {
    try {
      await this.onRefresh();
    } catch (err) {
      this.state.queueError = err.message || String(err);
    }
  }

  async enqueueTodo(todoKey, assignmentKey, { dispatch = false } = {}) {
    if (!todoKey || !assignmentKey) {
      this.state.queueError = "Select an open TODO and an active designated orchestrator.";
      this.renderForm();
      return;
    }
    if (this.state.queueBusyKey) return;
    const assignment = this.activeAssignments().find((item) => item.assignment_key === assignmentKey);
    if (dispatch) {
      const confirmed = this.window?.confirm?.(
        `Queue and dispatch ${todoKey} to ${assignmentLabel(assignment)}? The source TODO will be marked done as the handoff record after enqueue succeeds.`,
      );
      if (confirmed === false) return;
    }
    this.state.queueBusyKey = todoKey;
    this.state.queueError = "";
    try {
      await this.api.enqueueOrchestration({ todo_key: todoKey, assignment_key: assignmentKey });
      await this.refreshAfterMutation();
      if (dispatch) {
        await this.api.pruneTodo({
          todo_key: todoKey,
          status: "done",
          actor: "dashboard",
          detail: "Dispatched through the dashboard queue handoff.",
        });
        await this.refreshAfterMutation();
      }
    } catch (err) {
      this.state.queueError = dispatch
        ? `Queue or dispatch failed: ${err.message || String(err)}`
        : (err.message || String(err));
    } finally {
      this.state.queueBusyKey = "";
      this.render();
    }
  }

  async handleSearch(event) {
    this.state.queueSearch = event.target.value || "";
    const query = this.state.queueSearch.trim();
    const generation = ++this.state.queueSearchGeneration;
    if (query) {
      try {
        const result = await this.api.todos({ status: "open", search: query, limit: 100 });
        if (generation !== this.state.queueSearchGeneration) return;
        this.state.queueCandidates = asList(result.todos);
      } catch (err) {
        if (generation !== this.state.queueSearchGeneration) return;
        this.state.queueError = err.message || String(err);
      }
    } else {
      this.state.queueCandidates = [];
    }
    this.renderForm();
  }

  async enqueue(event) {
    event.preventDefault();
    const form = event.target;
    await this.enqueueTodo(form.elements.todo_key.value, form.elements.assignment_key.value);
  }

  async mutate(queueKey, action) {
    this.state.queueBusyKey = queueKey;
    this.state.queueError = "";
    this.render();
    try {
      if (action === "retry") {
        await this.api.retryOrchestration({ queue_key: queueKey, idempotency_key: `dashboard-retry-${Date.now()}` });
      } else if (action === "drop") {
        await this.api.dropOrchestration({ queue_key: queueKey, reason: "Dropped by dashboard owner." });
      } else {
        await this.api.cancelOrchestration({ queue_key: queueKey, reason: "Cancelled by dashboard owner." });
      }
      await this.onRefresh();
    } catch (err) {
      this.state.queueError = err.message || String(err);
    } finally {
      this.state.queueBusyKey = "";
      this.render();
    }
  }

  async copyPrompt(button = null) {
    this.state.queuePromptBusy = true;
    this.state.queuePromptStatus = "Preparing prompt…";
    this.render();
    try {
      const result = await this.api.orchestrationPrompt();
      if (!result?.prompt) throw new Error("The server returned an empty orchestrator prompt.");
      if (this.navigator?.clipboard?.writeText) await this.navigator.clipboard.writeText(result.prompt);
      else fallbackCopyText(result.prompt, this.document);
      this.state.queuePromptStatus = "Prompt copied.";
      showCopyButtonFeedback(button, this.window);
    } catch (err) {
      this.state.queuePromptStatus = `Copy failed: ${err.message || String(err)}`;
    } finally {
      this.state.queuePromptBusy = false;
      this.render();
    }
  }

  async stopOrchestrator(assignmentKey) {
    if (!assignmentKey || this.state.queueStopBusy) return;
    const confirmed = this.window?.confirm?.(
      "Stop this orchestrator and cancel all active queued work? In-flight child work remains visible until terminal evidence is acknowledged."
    );
    if (confirmed === false) return;
    this.state.queueStopBusy = true;
    this.state.queueError = "";
    this.render();
    try {
      await this.api.stopOrchestration({
        assignment_key: assignmentKey,
        idempotency_key: `dashboard-stop-${assignmentKey}`,
        reason: "Stopped by dashboard owner.",
      });
      await this.onRefresh();
    } catch (err) {
      this.state.queueError = err.message || String(err);
    } finally {
      this.state.queueStopBusy = false;
      this.render();
    }
  }

  handleClick(event) {
    const button = event.target.closest?.("[data-action]");
    if (!button) return;
    if (button.dataset.action === "copy-orchestrator-prompt") {
      this.copyPrompt(button);
      return;
    }
    if (button.dataset.action === "stop-orchestrator") {
      this.stopOrchestrator(button.dataset.assignmentKey);
      return;
    }
    if (!button.dataset.queueKey) return;
    if (button.dataset.action === "retry-queue") this.mutate(button.dataset.queueKey, "retry");
    if (button.dataset.action === "cancel-queue") this.mutate(button.dataset.queueKey, "cancel");
    if (button.dataset.action === "drop-queue") this.mutate(button.dataset.queueKey, "drop");
  }
}
