import {
  state,
  todoScopeFilterAll,
  todoScopeFilterUnspecified,
} from "./dashboard/state.js";
import { DashboardApi } from "./dashboard/api.js";
import { DashboardCache, CACHE_MODES } from "./dashboard/cache.js";
import {
  basePath, $, fmtTime, jumpToHud,
} from "./dashboard/dom.js";
import { DashboardApp } from "./dashboard/app.js";
import { MetricsPanel } from "./dashboard/components/metrics.js";
import { TodoPanel } from "./dashboard/components/todos.js";
import { TaskPanel } from "./dashboard/components/tasks.js";
import { EventsPanel } from "./dashboard/components/events.js";
import { DocumentationIndexPanel } from "./dashboard/components/documentation-index.js";
import { DocumentationWorkbench } from "./dashboard/components/documentation-workbench.js";
import { ModelUsagePanel } from "./dashboard/components/model-usage.js";
import { TokenUsagePanel } from "./dashboard/components/token-usage.js";
import { OscilloscopePanel } from "./dashboard/components/oscilloscope.js";
import { EfficiencyPanel } from "./dashboard/components/efficiency.js";
import { OrchestrationQueuePanel } from "./dashboard/components/orchestration-queue.js";
import { mountDashboardPanels } from "./dashboard/shell.js";

mountDashboardPanels();

// Read cache seeded from the persisted client cache mode. Reads go through it;
// mutations invalidate it; a manual Refresh clears it to force revalidation.
const dashboardCache = new DashboardCache({ mode: state.cacheMode });
const dashboardApi = new DashboardApi({ basePathOverride: basePath, cache: dashboardCache });
const dashboardApp = new DashboardApp({ state, api: dashboardApi });

// Manual Refresh: drop every cached read first so the whole page revalidates
// past any TTL, then reload.
function manualRefresh() {
  dashboardCache.clear();
  return loadDashboard({ bypass: true });
}

function handleCacheModeChange(event) {
  const mode = CACHE_MODES.includes(event.target.value) ? event.target.value : state.cacheMode;
  state.persistCacheMode(mode);
  dashboardApi.setCacheMode(mode);
}

function handleRefreshRateChange() {
  const control = $("refreshRate");
  state.persistRefreshRate(control ? control.value : state.refreshRate);
  dashboardApp.schedule();
}
let todoPanel;
let documentationWorkbench;
let queuePanel;

const documentationIndex = new DocumentationIndexPanel({
  onUseScope: (scope) => documentationWorkbench?.useScope(scope),
  onOpenScope: (scope) => documentationWorkbench?.openScope(scope),
});

const metricsPanel = new MetricsPanel();
const eventsPanel = new EventsPanel();
const modelUsagePanel = new ModelUsagePanel({ state });
const tokenUsagePanel = new TokenUsagePanel({
  state,
  api: dashboardApi,
  // Toggling history visibility re-renders only the history chart from the
  // already-loaded payload; totals, bars, pie, recs, and efficiency are untouched.
  onToggleHistory: () => oscilloscopePanel.rerender(),
});
const oscilloscopePanel = new OscilloscopePanel({ state });
const efficiencyPanel = new EfficiencyPanel({ state });
oscilloscopePanel.setFilterRows((rows) => modelUsagePanel.filterRows(rows));

function renderAnalytics(data = state.latest) {
  if (!data) return;
  const tasks = data.recent_tasks || [];
  modelUsagePanel.renderAll(tasks);
  const filteredTasks = modelUsagePanel.filterRows(tasks);
  const filteredUsage = modelUsagePanel.filterRows(data.token_usage || []);
  tokenUsagePanel.render(filteredUsage);
  const registry = data.guidance_recommendations || { recommendations: [], total: 0 };
  const count = $("guidanceRegistryCount"); const rows = $("guidanceRegistryRows");
  if (count) count.textContent = `${registry.total || 0} item${registry.total === 1 ? "" : "s"}`;
  if (rows) {
    rows.replaceChildren();
    for (const item of registry.recommendations || []) {
      const article = document.createElement("article"); article.className = "token-rec-item";
      article.textContent = `${item.recommendation_key} · ${item.status} · ${item.evidence_count} evidence — ${item.proposed_guidance} (${(item.target_documents || []).join(", ")})`;
      rows.append(article);
    }
    if (!(registry.recommendations || []).length) rows.textContent = "No guidance evidence recorded yet.";
  }
  oscilloscopePanel.render(filteredUsage, filteredTasks);
  efficiencyPanel.render(modelUsagePanel.filterRows(data.efficiency_samples || []), filteredTasks);
  oscilloscopePanel.applyModelHighlight(state.osc.hoverModel);
}

function rerenderAnalytics() {
  const data = state.latest;
  if (!data) return;
  renderAnalytics(data);
}

oscilloscopePanel.onSelectionChange = () => {
  oscilloscopePanel.rerender();
  efficiencyPanel.rerender((rows) => modelUsagePanel.filterRows(rows));
};
modelUsagePanel.onFilterChange = rerenderAnalytics;
modelUsagePanel.onHighlight = (label) => oscilloscopePanel.applyModelHighlight(label);
tokenUsagePanel.onRefresh = () => loadDashboard();

function handleModelUsageClick(event) {
  const sourceButton = event.target.closest?.("[data-complexity-source]");
  if (sourceButton) { modelUsagePanel.setComplexitySource(sourceButton.getAttribute("data-complexity-source")); return; }
  const bucketButton = event.target.closest?.("[data-complexity-bucket]");
  if (bucketButton) { modelUsagePanel.selectComplexityBucket(bucketButton.getAttribute("data-complexity-bucket")); return; }
  const scaleButton = event.target.closest?.("[data-osc-timescale]");
  if (scaleButton) { oscilloscopePanel.setTimescale(scaleButton.getAttribute("data-osc-timescale")); return; }
  if (event.target.closest?.("[data-osc-clear]")) { oscilloscopePanel.clearSelection(); return; }
  if (state.osc.suppressClick) { state.osc.suppressClick = false; return; }
  const modelElement = event.target.closest?.("[data-model-label]");
  if (modelElement) oscilloscopePanel.toggleModel(modelElement.getAttribute("data-model-label"));
}

function handleModelUsageOver(event) {
  const modelElement = event.target.closest?.("[data-model-label]");
  oscilloscopePanel.applyModelHighlight(modelElement ? modelElement.getAttribute("data-model-label") : null);
}

function handleModelUsageKeydown(event) {
  if (event.key !== "Enter" && event.key !== " ") return;
  const modelElement = event.target.closest?.("[data-model-label]");
  if (modelElement) { event.preventDefault(); oscilloscopePanel.toggleModel(modelElement.getAttribute("data-model-label")); }
}

async function loadDashboard({ bypass = false } = {}) {
  const generation = dashboardApp.beginRequest("dashboard");
  const data = await dashboardApi.dashboard({ bypass });
  if (!dashboardApp.isCurrentRequest("dashboard", generation)) return;
  state.setLatest(data);
  if (state.todoStatusFilter !== "open" || state.todoPriorityFilter ||
      (state.todoScopeFilter !== todoScopeFilterAll && state.todoScopeFilter !== todoScopeFilterUnspecified)) {
    await todoPanel.load();
  } else {
    await todoPanel.loadValidationLanes();
  }
  const title = `${data.project?.server_name || "MCP"} Dashboard`;
  document.title = title;
  $("dashboardTitle").textContent = title;
  documentationWorkbench.renderTodoTargetOptions(data.open_todos || []);
  $("repoRoot").textContent = data.status.repo_root;
  $("updatedAt").textContent = `Updated ${fmtTime(data.generated_at)}`;
  metricsPanel.render(data);
  todoPanel.renderCurrent();
  queuePanel.render(data);
  taskPanel.render(data);
  eventsPanel.render(data.task_events || []);
  renderAnalytics(data);
  documentationIndex.render(data.doc_scopes || [], data.docs_drift || null);
  documentationWorkbench.renderTodoHints(data.open_todos || []);
  documentationWorkbench.render();
}

documentationWorkbench = new DocumentationWorkbench({
  state,
  api: dashboardApi,
  app: dashboardApp,
  findOpenTodo: (todoKey) => todoPanel.findOpenTodo(todoKey),
  loadDashboard,
});
const taskPanel = new TaskPanel({ state, api: dashboardApi, app: dashboardApp, onRefresh: loadDashboard });
queuePanel = new OrchestrationQueuePanel({ state, api: dashboardApi, onRefresh: loadDashboard });
todoPanel = new TodoPanel({
  state,
  api: dashboardApi,
  app: dashboardApp,
  onRefresh: loadDashboard,
  onSearchDocs: (todoKey) => documentationWorkbench.searchTodoDocs(todoKey),
  onCopyPrompt: (todoKey, button, variant) => documentationWorkbench.copyTodoNextInstruction(todoKey, button, variant),
  onQueue: (todoKey, anchor) => queuePanel.queueFromRadial(todoKey, anchor),
  onWake: (todoKey, anchor) => queuePanel.wakeFromRadial(todoKey, anchor),
  onQueue: (todoKey, anchor, mode) => queuePanel.queueFromRadial(todoKey, anchor, mode),
});

// Reflect persisted client preferences into the header controls before the
// first schedule so polling and the cache mode start from the stored choices.
function syncClientPreferenceControls() {
  const rate = $("refreshRate");
  if (rate) rate.value = String(state.refreshRate);
  const cacheMode = $("cacheMode");
  if (cacheMode) cacheMode.value = state.cacheMode;
}

syncClientPreferenceControls();

dashboardApp.start({
  refresh: loadDashboard,
  bindings: [
    { selector: "#refreshBtn", event: "click", handler: manualRefresh },
    { selector: "#refreshRate", event: "change", handler: handleRefreshRateChange },
    { selector: "#cacheMode", event: "change", handler: handleCacheModeChange },
    { selector: "#todoScopeFilter", event: "change", handler: (event) => todoPanel.handleScopeFilterChange(event) },
    { selector: "#todoStatusFilter", event: "change", handler: (event) => todoPanel.handleStatusFilterChange(event) },
    { selector: "#todoPriorityFilter", event: "change", handler: (event) => todoPanel.handlePriorityFilterChange(event) },
    { selector: "#todoLoadMore", event: "click", handler: () => todoPanel.load({ append: true }) },
    { selector: "#queueAddForm", event: "submit", handler: (event) => queuePanel.enqueue(event) },
    { selector: "#queueTodoSearch", event: "input", handler: (event) => queuePanel.handleSearch(event) },
    { selector: "#orchestrationQueueSection", event: "click", handler: (event) => queuePanel.handleClick(event) },
    { selector: "#activeTasks", event: "click", handler: (event) => taskPanel.handleClick(event) },
    { selector: "#recentTasks", event: "click", handler: (event) => taskPanel.handleClick(event) },
    { selector: "#docSearchForm", event: "submit", handler: (event) => documentationWorkbench.handleSearchSubmit(event) },
    { selector: "#copyDocSearchPrompt", event: "click", handler: (event) => documentationWorkbench.copyCurrentSearchPrompt(event) },
    { selector: "#docTodoTarget", event: "change", handler: (event) => documentationWorkbench.handleTargetChange(event) },
    { selector: "#metrics", event: "click", handler: (event) => metricsPanel.handleClick(event) },
    { selector: "[data-jump-hud]", event: "click", handler: jumpToHud, all: true },
    { selector: "#openTodos", event: "click", handler: (event) => todoPanel.handleClick(event) },
    { selector: "#openTodos", event: "contextmenu", handler: (event) => todoPanel.handleContextMenu(event) },
    { selector: "#openTodos", event: "submit", handler: (event) => todoPanel.prune(event) },
    { selector: "#openTodos", event: "input", handler: (event) => todoPanel.handleDraftChange(event) },
    { selector: "#openTodos", event: "change", handler: (event) => todoPanel.handleChange(event) },
    { selector: "#tokenUsagePanel", event: "click", handler: (event) => tokenUsagePanel.handleClick(event) },
    { selector: "#tokenUsagePanel", event: "submit", handler: (event) => tokenUsagePanel.handleSubmit(event) },
    { selector: "#docScopes", event: "click", handler: (event) => documentationIndex.handleClick(event) },
    { selector: "#docSearchSection", event: "click", handler: (event) => documentationWorkbench.handleClick(event) },
    { selector: "#modelUsageSection", event: "click", handler: handleModelUsageClick },
    { selector: "#modelUsageSection", event: "keydown", handler: handleModelUsageKeydown },
    { selector: "#modelUsageSection", event: "mouseover", handler: handleModelUsageOver },
    { selector: "#modelUsageSection", event: "mouseleave", handler: () => oscilloscopePanel.applyModelHighlight(null) },
    { selector: "#oscilloscopeScroll", event: "mousedown", handler: (event) => oscilloscopePanel.handlePanStart(event) },
    { target: window, event: "resize", handler: () => { window.clearTimeout(state.osc.resizeTimer); state.osc.resizeTimer = window.setTimeout(() => { oscilloscopePanel.rerender(); efficiencyPanel.rerender((rows) => modelUsagePanel.filterRows(rows)); }, 150); } },
  ],
}).catch((err) => { $("updatedAt").textContent = err.message; });
