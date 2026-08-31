import { $, escapeHtml, fmtDuration, fmtTime, setEmpty, statusBadge } from "../dom.js";

const FLUSHABLE_TASK_STATUSES = ["in_progress", "paused", "blocked"];

function renderTaskFlushAction(task) {
  if (!FLUSHABLE_TASK_STATUSES.includes(task.status || "")) return "";
  const taskKey = task.task_key || "";
  const agentId = task.agent_id || "";
  return `
    <div class="row-actions">
      <button class="danger" type="button" data-action="flush-task" data-task-key="${escapeHtml(taskKey)}" data-agent-id="${escapeHtml(agentId)}" aria-label="Flush working state for ${escapeHtml(taskKey)} (${escapeHtml(agentId)})" title="Flush this row's working state">Flush</button>
    </div>
  `;
}

export class TaskPanel {
  constructor({
    state,
    api,
    app,
    onRefresh,
    documentRef = globalThis.document,
    windowRef = globalThis.window,
  } = {}) {
    this.state = state;
    this.api = api;
    this.app = app;
    this.onRefresh = onRefresh;
    this.document = documentRef;
    this.window = windowRef;
  }

  renderActive(tasks = []) {
    const rows = Array.isArray(tasks) ? tasks : [];
    const count = $("activeCount", this.document);
    const body = $("activeTasks", this.document);
    if (count) count.textContent = `${rows.length} current`;
    setEmpty("activeEmpty", rows.length === 0, this.document);
    if (!body) return;
    body.innerHTML = rows.map((task) => `
      <tr data-status="${escapeHtml(task.status || "")}" data-stale="${task.is_stale ? "true" : "false"}">
        <td>${statusBadge(task.status)}</td>
        <td><strong>${escapeHtml(task.task_title || task.task_key)}</strong><div class="muted">${escapeHtml(task.task_key)}</div></td>
        <td>${escapeHtml(task.canonical_model_label || task.agent_label || task.agent_id)}<div class="muted">${escapeHtml(task.agent_id)}</div></td>
        <td>${escapeHtml(task.summary)}${task.current_step ? `<div class="muted">${escapeHtml(task.current_step)}</div>` : ""}</td>
        <td>${escapeHtml(fmtTime(task.last_seen_at))}</td>
        <td>${escapeHtml(fmtDuration(task.duration_seconds))}<div class="muted">${escapeHtml(task.duration_state || "")}</div></td>
        <td>${escapeHtml(fmtTime(task.expires_at))}</td>
        <td>${renderTaskFlushAction(task)}</td>
      </tr>
    `).join("");
  }

  renderRecent(tasks = []) {
    const rows = Array.isArray(tasks) ? tasks : [];
    const count = $("recentCount", this.document);
    const body = $("recentTasks", this.document);
    if (count) count.textContent = `${rows.length} shown`;
    setEmpty("recentEmpty", rows.length === 0, this.document);
    if (!body) return;
    body.innerHTML = rows.map((task) => `
      <tr data-status="${escapeHtml(task.status || "")}" data-stale="${task.is_stale ? "true" : "false"}">
        <td>${statusBadge(task.status)}</td>
        <td><strong>${escapeHtml(task.task_key)}</strong></td>
        <td>${escapeHtml(task.canonical_model_label || task.agent_label || task.agent_id)}</td>
        <td>${escapeHtml(task.current_step || task.summary)}</td>
        <td>${escapeHtml(fmtDuration(task.duration_seconds))}<div class="muted">${escapeHtml(task.duration_state || "")}</div></td>
        <td>${escapeHtml(fmtTime(task.last_seen_at))}</td>
        <td>${renderTaskFlushAction(task)}</td>
      </tr>
    `).join("");
  }

  render(data = {}) {
    this.renderActive(data.active_tasks || []);
    this.renderRecent(data.recent_tasks || []);
  }

  async handleClick(event) {
    const button = event.target.closest?.('[data-action="flush-task"]');
    if (!button) return;
    const taskKey = button.dataset.taskKey || "";
    const agentId = button.dataset.agentId || "";
    if (!taskKey || !agentId) return;
    const reason = this.window.prompt(
      `Flush working state for "${taskKey}" (${agentId})? Optional reason:`,
      "",
    );
    if (reason === null) return;
    button.disabled = true;
    try {
      await this.api.flushTask({ task_key: taskKey, agent_id: agentId, reason: reason.trim() });
      await this.onRefresh();
    } finally {
      button.disabled = false;
    }
  }
}
