import { $, escapeHtml, setEmpty } from "../dom.js";
import {
  computeTokenUsageStats,
  fmtTokens,
  MODEL_OTHER_COLOR,
  MODEL_UNKNOWN_LABEL,
  filterRowsByModel,
  modelIdentity,
  tokenEfficiencyBadge,
} from "../analytics/shared.js";

const TOKEN_RECS_MAX_VISIBLE = 5;

export class TokenUsagePanel {
  constructor({ state, api, onRefresh, onToggleHistory, documentRef = globalThis.document } = {}) {
    this.state = state; this.api = api; this.onRefresh = onRefresh; this.onToggleHistory = onToggleHistory; this.document = documentRef;
  }

  render(rows) {
    const usage = filterRowsByModel(rows); const { stats, totalTokens, taskCount, overallAvg } = computeTokenUsageStats(usage);
    const count = $("tokenUsageCount", this.document); const empty = $("tokenUsageEmpty", this.document);
    if (count) count.textContent = taskCount ? `${fmtTokens(totalTokens)} tokens over ${taskCount} task${taskCount === 1 ? "" : "s"} · ~${fmtTokens(overallAvg)}/task` : "";
    if (empty) empty.textContent = this.state.complexityBucket ? "No token usage matches the complexity filter." : "No token usage submitted yet.";
    setEmpty("tokenUsageEmpty", taskCount === 0, this.document);
    const bars = $("tokenUsageBars", this.document); const recsElement = $("tokenRecommendations", this.document);
    if (!bars || !recsElement) return;
    if (taskCount === 0) { bars.innerHTML = ""; recsElement.innerHTML = ""; return; }
    const maxTokens = Math.max(...stats.map((entry) => entry.tokens), 1);
    // Drop stale hidden entries and detect the all-off state up front. Only
    // recognized (non-Unknown) rows carry a history toggle.
    const recognizedLabels = stats.map((entry) => entry.label).filter((label) => label !== MODEL_UNKNOWN_LABEL);
    this.state.pruneHiddenHistoryModels(recognizedLabels);
    bars.innerHTML = stats.map((entry) => {
      const color = entry.label === MODEL_UNKNOWN_LABEL ? MODEL_OTHER_COLOR : this.state.colorFor(entry.label);
      const widthPct = Math.max((entry.tokens / maxTokens) * 100, 2).toFixed(1);
      const statsText = `${fmtTokens(entry.tokens)} · ${entry.tasks} task${entry.tasks === 1 ? "" : "s"} · ~${fmtTokens(entry.avg)}/task`;
      // Named categories (never "Unknown") get accessible rename/delete controls
      // plus a history-visibility toggle.
      const controls = entry.label === MODEL_UNKNOWN_LABEL ? "" : `${this.historyToggle(entry.label)}${this.categoryControls(entry.label)}`;
      return `<div class="token-model-row"><div class="token-model-meta"><span class="token-model-label">${escapeHtml(entry.label)} <span class="muted">${escapeHtml(statsText)}</span></span><span class="token-model-actions">${tokenEfficiencyBadge(entry.avg, overallAvg)}${controls}</span></div><div class="token-bar-track"><div class="token-bar-fill" style="width:${widthPct}%;background:${color}"></div></div></div>`;
    }).join("");
    const recRow = (row) => {
      const status = row.recommendation_status || "open"; const isImplemented = status === "implemented";
      const modelLabel = modelIdentity(row, row.model_descriptor); const modelColor = this.state.colorFor(modelLabel);
      return `<div class="token-rec-item${isImplemented ? " implemented" : ""}" title="${escapeHtml(row.recommendation)}"><div class="token-rec-head"><code>${escapeHtml(row.task_key)}</code><span class="token-rec-model muted" title="Suggested by ${escapeHtml(modelLabel)}"><span class="token-rec-model-swatch" style="background:${modelColor}"></span>${escapeHtml(modelLabel)}</span></div><div class="token-rec-body"><span class="token-rec-text">${escapeHtml(row.recommendation)}</span><button class="token-rec-status-toggle" type="button" data-task-key="${escapeHtml(row.task_key)}" data-agent-id="${escapeHtml(row.agent_id)}" data-current-status="${escapeHtml(status)}" title="Toggle recommendation status">${isImplemented ? "✓" : "○"}</button></div></div>`;
    };
    const allRecommendations = usage.filter((row) => (row.recommendation || "").trim());
    const openRecs = allRecommendations.filter((row) => (row.recommendation_status || "open") === "open");
    const doneRecs = allRecommendations.filter((row) => (row.recommendation_status || "open") !== "open");
    let recsHtml = "";
    if (allRecommendations.length) {
      recsHtml = `<div class="token-recs-title muted">Reduction recommendations</div>`;
      if (allRecommendations.length <= TOKEN_RECS_MAX_VISIBLE) recsHtml += [...openRecs, ...doneRecs].map(recRow).join("");
      else {
        recsHtml += openRecs.slice(0, TOKEN_RECS_MAX_VISIBLE).map(recRow).join("");
        const recentDone = doneRecs.slice(0, TOKEN_RECS_MAX_VISIBLE);
        if (recentDone.length) { const expanded = Boolean(this.state.recsExpanded); recsHtml += `<button class="token-recs-completed-toggle" type="button" data-recs-toggle data-count="${recentDone.length}" aria-expanded="${expanded}">${expanded ? "Hide" : "Show"} ${recentDone.length} completed</button><div class="token-recs-completed${expanded ? " open" : ""}">${recentDone.map(recRow).join("")}</div>`; }
      }
    }
    recsElement.innerHTML = recsHtml;
  }

  // Accessible ON/OFF switch controlling this model's history lane. A native
  // switch role with aria-checked plus a visible ON/OFF text label keeps the
  // state screen-reader legible; the button is keyboard operable by default.
  historyToggle(label) {
    const safe = escapeHtml(label);
    const on = this.state.isHistoryEnabled(label);
    return `<button class="token-history-toggle${on ? " is-on" : ""}" type="button" role="switch" aria-checked="${on}" data-history-toggle="${safe}" aria-label="History lane for ${safe}" title="Show ${safe} in Token Usage Over Time"><span class="token-history-track" aria-hidden="true"></span><span class="token-history-state">${on ? "ON" : "OFF"}</span></button>`;
  }

  categoryControls(label) {
    const safe = escapeHtml(label);
    return `<button class="token-cat-action token-cat-rename" type="button" data-cat-rename="${safe}" aria-label="Rename category ${safe}" title="Rename category">✎</button>` +
      `<button class="token-cat-action token-cat-trash" type="button" data-cat-trash="${safe}" aria-label="Delete closed tasks for category ${safe}" title="Delete closed tasks">🗑</button>`;
  }

  showError(message) {
    const element = $("tokenCategoryError", this.document);
    if (!element) return;
    element.textContent = message || "";
    element.hidden = !message;
  }

  clearError() { this.showError(""); }

  dialogHost() { return $("tokenCategoryDialog", this.document); }

  closeDialog() {
    const host = this.dialogHost();
    if (host) { host.innerHTML = ""; host.hidden = true; }
  }

  openRenameDialog(label) {
    this.clearError();
    const host = this.dialogHost();
    if (!host) return;
    const safe = escapeHtml(label);
    host.hidden = false;
    host.innerHTML = `<div class="token-category-dialog" role="dialog" aria-modal="true" aria-label="Rename category">
      <form data-cat-rename-form data-cat-old="${safe}">
        <label class="token-category-field"><span>Rename “${safe}” to</span>
          <input type="text" name="new_label" value="${safe}" autocomplete="off" aria-label="New category name" required></label>
        <div class="token-category-dialog-actions">
          <button type="button" class="ghost" data-cat-cancel>Cancel</button>
          <button type="submit" class="primary">Rename</button>
        </div>
      </form></div>`;
    const input = host.querySelector("input[name='new_label']");
    if (input?.focus) { try { input.focus(); input.select?.(); } catch { /* headless */ } }
  }

  async openTrashDialog(label) {
    this.clearError();
    const host = this.dialogHost();
    let count = null;
    try {
      const preview = await this.api.previewAgentCategoryPurge({ label });
      count = Number(preview?.closed_task_count ?? 0);
    } catch (err) {
      this.showError(`Could not count closed tasks: ${err.message || err}`);
      return;
    }
    if (!host) return;
    const safe = escapeHtml(label);
    host.hidden = false;
    host.innerHTML = `<div class="token-category-dialog" role="alertdialog" aria-modal="true" aria-label="Delete closed tasks">
      <div class="token-category-confirm-text">Delete <strong>${count}</strong> closed task${count === 1 ? "" : "s"} for category “${safe}”? Active, paused, and blocked tasks are preserved. This cannot be undone.</div>
      <div class="token-category-dialog-actions">
        <button type="button" class="ghost" data-cat-cancel>Cancel</button>
        <button type="button" class="danger" data-cat-confirm-purge="${safe}"${count === 0 ? " disabled" : ""}>Delete ${count} closed</button>
      </div></div>`;
  }

  async submitRename(oldLabel, newLabelRaw) {
    const newLabel = String(newLabelRaw || "").trim();
    if (!newLabel) { this.showError("Enter a non-empty category name."); return; }
    try {
      await this.api.renameAgentCategory({ old_label: oldLabel, new_label: newLabel });
      // Migrate browser-only history-toggle and color prefs to the merged label
      // so the rename does not silently reset this browser's selection/color.
      this.state.remapModelLabel?.(oldLabel, newLabel);
      this.closeDialog();
      this.clearError();
      await this.onRefresh?.();
    } catch (err) {
      this.showError(err.message || String(err));
    }
  }

  async confirmPurge(label) {
    try {
      await this.api.purgeAgentCategoryClosed({ label, confirm: true });
      this.closeDialog();
      this.clearError();
      await this.onRefresh?.();
    } catch (err) {
      this.showError(err.message || String(err));
    }
  }

  async handleClick(event) {
    const slideToggle = event.target.closest?.("[data-recs-toggle]");
    if (slideToggle) {
      this.state.recsExpanded = !this.state.recsExpanded;
      const panel = $("tokenRecommendations", this.document)?.querySelector(".token-recs-completed");
      if (panel) panel.classList.toggle("open", this.state.recsExpanded);
      slideToggle.setAttribute("aria-expanded", String(this.state.recsExpanded));
      slideToggle.textContent = `${this.state.recsExpanded ? "Hide" : "Show"} ${slideToggle.dataset.count || ""} completed`;
      return;
    }
    const historyBtn = event.target.closest?.("[data-history-toggle]");
    if (historyBtn) {
      const label = historyBtn.getAttribute("data-history-toggle");
      this.state.toggleHistory(label);
      const on = this.state.isHistoryEnabled(label);
      historyBtn.setAttribute("aria-checked", String(on));
      historyBtn.classList.toggle("is-on", on);
      const stateLabel = historyBtn.querySelector?.(".token-history-state");
      if (stateLabel) stateLabel.textContent = on ? "ON" : "OFF";
      // Re-render the history chart in place: the full payload is already loaded,
      // so no refetch is needed. Toggling does not touch totals/bars/recs.
      this.onToggleHistory?.();
      return;
    }
    const renameBtn = event.target.closest?.("[data-cat-rename]");
    if (renameBtn) { this.openRenameDialog(renameBtn.getAttribute("data-cat-rename")); return; }
    const trashBtn = event.target.closest?.("[data-cat-trash]");
    if (trashBtn) { await this.openTrashDialog(trashBtn.getAttribute("data-cat-trash")); return; }
    if (event.target.closest?.("[data-cat-cancel]")) { this.closeDialog(); this.clearError(); return; }
    const confirmBtn = event.target.closest?.("[data-cat-confirm-purge]");
    if (confirmBtn) { await this.confirmPurge(confirmBtn.getAttribute("data-cat-confirm-purge")); return; }
    const button = event.target.closest?.(".token-rec-status-toggle");
    if (!button) return;
    const taskKey = button.dataset.taskKey; const agentId = button.dataset.agentId; if (!taskKey || !agentId) return;
    const newStatus = (button.dataset.currentStatus || "open") === "open" ? "implemented" : "open";
    button.disabled = true;
    try { await this.api.updateRecommendationStatus({ task_key: taskKey, agent_id: agentId, status: newStatus }); await this.onRefresh?.(); }
    catch (err) { console.error("Failed to update recommendation status:", err); button.disabled = false; }
  }

  async handleSubmit(event) {
    const form = event.target.closest?.("[data-cat-rename-form]");
    if (!form) return;
    event.preventDefault?.();
    const oldLabel = form.getAttribute("data-cat-old");
    const input = form.querySelector?.("input[name='new_label']");
    await this.submitRename(oldLabel, input?.value);
  }
}
