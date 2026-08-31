import { $, escapeHtml, jumpFromMetric } from "../dom.js";

function metric(label, value, cls, jumpTarget) {
  return `
    <button class="metric ${cls} metric-button" type="button" data-metric-jump="${escapeHtml(jumpTarget)}" aria-label="Jump to ${escapeHtml(label)}">
      <div class="label">${escapeHtml(label)}</div>
      <div class="value">${escapeHtml(value)}</div>
    </button>
  `;
}

export class MetricsPanel {
  constructor({ documentRef = globalThis.document, onJump = jumpFromMetric } = {}) {
    this.document = documentRef;
    this.onJump = onJump;
  }

  render(data = {}) {
    const counts = data.task_counts || {};
    const todoCounts = data.todo_counts || {};
    const todos = data.open_todos || [];
    const staleTodos = todos.filter((todo) => todo.is_stale).length;
    const staleTasks = (data.recent_tasks || []).filter((task) => task.is_stale).length;
    const docsDrift = Number(data.docs_drift?.advisory_count || 0);
    const element = $("metrics", this.document);
    if (!element) return;
    element.innerHTML = [
      metric("Active", data.status?.active_tasks || 0, "active", "active"),
      metric("Open Todos", data.status?.open_todos || 0, "active", "open-todos"),
      metric("Stale", staleTodos + staleTasks + docsDrift, "stale", "stale"),
      metric("Done", counts.done || 0, "done", "done"),
      metric("Blocked", todoCounts.blocked || 0, "stale", "blocked"),
      metric("Docs", `${data.status?.docs || 0}/${data.status?.doc_chunks || 0}`, "done", "docs"),
    ].join("");
  }

  handleClick(event) {
    const button = event.target.closest?.("[data-metric-jump]");
    if (button) this.onJump(button.dataset.metricJump, this.document);
  }
}
