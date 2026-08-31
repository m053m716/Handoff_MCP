import { appPath, basePath } from "./dom.js";
import { DashboardCache, invalidationsForPath } from "./cache.js";

const defaultFetch = globalThis.fetch?.bind(globalThis);

export async function responseErrorText(response) {
  const text = await response.text();
  if (!text) return `Request failed: ${response.status}`;
  try {
    const data = JSON.parse(text);
    return data.error?.message || data.error || data.message || text;
  } catch {
    return text;
  }
}

export class DashboardApi {
  constructor({ fetchImpl = defaultFetch, basePathOverride = basePath, cache } = {}) {
    this.fetch = fetchImpl;
    this.basePath = basePathOverride;
    // Read cache for GET requests. Callers may inject a preconfigured cache
    // (e.g. with a chosen mode); otherwise a default in-memory cache is used.
    this.cache = cache || new DashboardCache();
  }

  path(pathname) { return appPath(pathname, this.basePath); }

  setCacheMode(mode) { return this.cache.setMode(mode); }

  async request(pathname, options = {}) {
    const response = await this.fetch(this.path(pathname), options);
    if (!response.ok) throw new Error(await responseErrorText(response));
    return response;
  }

  // Perform an uncached JSON fetch (used both directly for non-GET calls and as
  // the loader behind the read cache).
  async fetchJson(pathname, options = {}) {
    return (await this.request(pathname, options)).json();
  }

  // Read-only JSON GET routed through the read cache. `bypass: true` (manual
  // Refresh) skips any fresh entry but still revalidates and writes back.
  async json(pathname, options = {}) {
    const { bypass = false, ...fetchOptions } = options;
    return this.cache.read(
      pathname,
      () => this.fetchJson(pathname, fetchOptions),
      { bypass },
    );
  }

  dashboard({ bypass = false } = {}) { return this.json("/api/dashboard", { cache: "no-store", bypass }); }

  todos({ status = "open", priority = "", appScope = "", search = "", cursor = "", limit = 20, bypass = false } = {}) {
    const params = new URLSearchParams({ status, limit: String(limit) });
    if (priority) params.set("priority", priority);
    if (appScope) params.set("app_scope", appScope);
    if (search) params.set("search", search);
    if (cursor) params.set("cursor", cursor);
    return this.json(`/api/dashboard/todos?${params.toString()}`, { cache: "no-store", bypass });
  }

  // Mutations are never cached. After a successful write, invalidate the cached
  // reads the route affects so the next render revalidates before displaying.
  async postJson(pathname, body) {
    const result = await this.fetchJson(pathname, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    this.cache.invalidatePrefixes(invalidationsForPath(pathname));
    return result;
  }

  updateTodoPriority(todo_key, priority) { return this.postJson("/api/dashboard/todos/priority", { todo_key, priority, actor: "dashboard" }); }
  updateTodoScope(todo_key, app_scope) { return this.postJson("/api/dashboard/todos/scope", { todo_key, app_scope, actor: "dashboard" }); }
  pruneTodo(body) { return this.postJson("/api/dashboard/todos/prune", body); }
  flushTask(body) { return this.postJson("/api/dashboard/owner/flush-agent-state", body); }
  updateRecommendationStatus(body) { return this.postJson("/api/dashboard/recommendations/status", body); }
  guidanceRecommendations(params = {}) { return this.getJson(`/api/dashboard/guidance-recommendations?${new URLSearchParams(params)}`); }
  addGuidanceRecommendation(body) { return this.postJson("/api/dashboard/guidance-recommendations/add", body); }
  reconcileGuidanceRecommendation(body) { return this.postJson("/api/dashboard/guidance-recommendations/reconcile", body); }
  renameAgentCategory(body) { return this.postJson("/api/dashboard/token-usage/agent-category/rename", body); }
  previewAgentCategoryPurge(body) { return this.postJson("/api/dashboard/token-usage/agent-category/purge-preview", body); }
  purgeAgentCategoryClosed(body) { return this.postJson("/api/dashboard/token-usage/agent-category/purge-closed", body); }
  searchDocs(body) { return this.postJson("/docs/search", body); }
  docDetail(docKey) { return this.json(`/docs/doc?key=${encodeURIComponent(docKey)}`, { cache: "no-store" }); }
  docScope(scope) { return this.json(`/docs/scope/${encodeURIComponent(scope)}`, { cache: "no-store" }); }
  appendDocReferences(body) { return this.postJson("/api/dashboard/todos/references", body); }
  todoGroup(groupKey) { return this.json(`/api/dashboard/todos/groups/${encodeURIComponent(groupKey)}`, { cache: "no-store" }); }
  reorderTodoGroup(body) { return this.postJson("/api/dashboard/todos/groups/reorder", body); }
  nextInstruction(todo_key, limit = 3, variant = "") {
    const body = { todo_key, limit };
    if (variant) body.variant = variant;
    return this.postJson("/api/dashboard/todos/next-instruction", body);
  }
  orchestrationPrompt() { return this.postJson("/api/dashboard/orchestration/prompt", {}); }
  enqueueOrchestration(body) { return this.postJson("/api/dashboard/orchestration/enqueue", body); }
  wakeOrchestration(body) { return this.postJson("/api/dashboard/orchestration/wake", body); }
  retryOrchestration(body) { return this.postJson("/api/dashboard/orchestration/retry", body); }
  dropOrchestration(body) { return this.postJson("/api/dashboard/orchestration/drop", body); }
  stopOrchestration(body) { return this.postJson("/api/dashboard/orchestration/stop", body); }
  cancelOrchestration(body) { return this.postJson("/api/dashboard/orchestration/cancel", body); }
}
