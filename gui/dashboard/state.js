import { ensureModelColor } from "./analytics/shared.js";
import { CACHE_MODES, DEFAULT_CACHE_MODE } from "./cache.js";

export const todoScopeFilterAll = "__all__";
export const todoScopeFilterUnspecified = "__unspecified__";
export const todoScopeFilterStorageKey = "mcpDashboard.todoScopeFilter";
export const todoStatusFilters = ["open", "suggested", "accepted", "in_progress", "blocked", "queued", "done", "dropped"];

// Read-cache mode and refresh interval persist under the same mcpDashboard
// localStorage convention as the scope filter. Both are browser-only client
// preferences and never leave the browser.
export const cacheModeStorageKey = "mcpDashboard.cacheMode";
export const refreshRateStorageKey = "mcpDashboard.refreshRate";
// Allowed polling intervals in milliseconds (0 = manual). Mirrors the
// #refreshRate control options in index.html; an unknown stored value falls
// back to the conservative default.
export const refreshRateChoices = [0, 5000, 15000, 30000, 60000];
export const defaultRefreshRateMs = 15000;

function readCacheMode(storage = globalThis.window?.localStorage) {
  try {
    const value = storage?.getItem(cacheModeStorageKey);
    return CACHE_MODES.includes(value) ? value : DEFAULT_CACHE_MODE;
  } catch {
    return DEFAULT_CACHE_MODE;
  }
}

function readRefreshRate(storage = globalThis.window?.localStorage) {
  try {
    const value = Number(storage?.getItem(refreshRateStorageKey));
    return refreshRateChoices.includes(value) ? value : defaultRefreshRateMs;
  } catch {
    return defaultRefreshRateMs;
  }
}

// Browser-only history-visibility and model-color preferences. Both are
// versioned so a schema change discards incompatible stored blobs instead of
// crashing on them. Neither is a backend concern: they never leave the browser.
export const hiddenHistoryModelsStorageKey = "mcpDashboard.hiddenHistoryModels.v1";
export const modelColorRegistryStorageKey = "mcpDashboard.modelColorRegistry.v1";
const HIDDEN_HISTORY_VERSION = 1;
const MODEL_COLOR_VERSION = 1;
const HEX_COLOR_RE = /^#[0-9a-fA-F]{3,8}$/;

function readTodoScopeFilter(storage = globalThis.window?.localStorage) {
  try {
    return storage?.getItem(todoScopeFilterStorageKey) || todoScopeFilterAll;
  } catch {
    return todoScopeFilterAll;
  }
}

export function saveTodoScopeFilter(value, storage = globalThis.window?.localStorage) {
  try {
    storage?.setItem(todoScopeFilterStorageKey, value);
  } catch {
    // Local storage can be unavailable in private or locked-down browser contexts.
  }
}

// Default-ON representation: only explicitly hidden labels are stored, so a
// newly discovered model is never in the set and therefore renders ON.
function readHiddenHistoryModels(storage = globalThis.window?.localStorage) {
  try {
    const raw = storage?.getItem(hiddenHistoryModelsStorageKey);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    if (!parsed || parsed.version !== HIDDEN_HISTORY_VERSION || !Array.isArray(parsed.hidden)) return new Set();
    return new Set(parsed.hidden.filter((label) => typeof label === "string" && label));
  } catch {
    return new Set();
  }
}

function readModelColorRegistry(storage = globalThis.window?.localStorage) {
  try {
    const raw = storage?.getItem(modelColorRegistryStorageKey);
    if (!raw) return Object.create(null);
    const parsed = JSON.parse(raw);
    if (!parsed || parsed.version !== MODEL_COLOR_VERSION || !parsed.colors || typeof parsed.colors !== "object") return Object.create(null);
    const registry = Object.create(null);
    for (const [label, color] of Object.entries(parsed.colors)) {
      if (typeof label === "string" && label && typeof color === "string" && HEX_COLOR_RE.test(color)) registry[label] = color;
    }
    return registry;
  } catch {
    return Object.create(null);
  }
}

export class DashboardState {
  constructor({ storage = globalThis.window?.localStorage } = {}) {
    this.timer = null;
    this.latest = null;
    this.storage = storage;
    // Explicit OFF choices for history lanes (default-ON: absence = shown).
    this.hiddenHistoryModels = readHiddenHistoryModels(storage);
    // Label -> hex color, allocated once and never reassigned.
    this.modelColorRegistry = readModelColorRegistry(storage);
    this.todoScopeFilter = readTodoScopeFilter(storage);
    // Persisted read-cache mode and polling interval (see cache.js).
    this.cacheMode = readCacheMode(storage);
    this.refreshRate = readRefreshRate(storage);
    this.todoStatusFilter = "open";
    this.todoPriorityFilter = "";
    this.todoNextCursor = null;
    this.pruneTodoKey = null;
    this.pruningTodoKey = null;
    this.pruneDrafts = Object.create(null);
    this.pruneError = "";
    this.priorityTodoKey = null;
    this.updatingPriorityKey = null;
    this.priorityError = "";
    this.scopeTodoKey = null;
    this.updatingScopeKey = null;
    this.scopeError = "";
    this.queueTodoKey = "";
    this.queueAssignmentKey = "";
    this.queueSearch = "";
    this.queueCandidates = [];
    this.queueSearchGeneration = 0;
    this.queueBusyKey = "";
    this.queueError = "";
    this.queuePromptBusy = false;
    this.queuePromptStatus = "";
    this.queueStopBusy = false;
    this.docResults = [];
    this.docDetail = null;
    this.docDetailLoadingKey = "";
    this.docDetailChunkId = "";
    this.docDetailError = "";
    this.docSearchError = "";
    this.docCopyStatus = "";
    this.docCopyTimer = null;
    this.docTodoTargetKey = "";
    this.docAppendingRefKey = "";
    this.modelSegments = [];
    this.complexitySource = "planned";
    this.complexityBucket = "";
    this.recsExpanded = false;
    this.osc = {
      timescale: "6h",
      selectedModel: null,
      hoverModel: null,
      resizeTimer: null,
      panMs: 0,
      pxPerMs: 0,
      panMin: 0,
      panMax: 0,
      pannable: false,
      dragging: false,
      dragged: false,
      suppressClick: false,
      dragStartX: 0,
      dragStartPan: 0,
      panRaf: 0,
    };
  }

  setLatest(payload) {
    this.latest = payload;
    this.todoNextCursor = payload?.open_todos_meta?.next_cursor || null;
    return payload;
  }

  persistTodoScopeFilter(value, storage = globalThis.window?.localStorage) {
    this.todoScopeFilter = value;
    saveTodoScopeFilter(value, storage);
  }

  persistCacheMode(value, storage = this.storage) {
    this.cacheMode = CACHE_MODES.includes(value) ? value : DEFAULT_CACHE_MODE;
    try { storage?.setItem(cacheModeStorageKey, this.cacheMode); } catch { /* storage optional */ }
    return this.cacheMode;
  }

  persistRefreshRate(value, storage = this.storage) {
    const numeric = Number(value);
    this.refreshRate = refreshRateChoices.includes(numeric) ? numeric : defaultRefreshRateMs;
    try { storage?.setItem(refreshRateStorageKey, String(this.refreshRate)); } catch { /* storage optional */ }
    return this.refreshRate;
  }

  // --- History-lane visibility (default ON) ---------------------------------

  isHistoryEnabled(label) {
    return !this.hiddenHistoryModels.has(String(label || ""));
  }

  /** Enabled set restricted to `knownLabels`, so stale hidden entries never
   *  suppress a lane and every known label defaults ON. */
  enabledHistoryLabels(knownLabels) {
    const known = new Set(Array.from(knownLabels, (label) => String(label || "")).filter(Boolean));
    const enabled = new Set();
    for (const label of known) if (!this.hiddenHistoryModels.has(label)) enabled.add(label);
    return enabled;
  }

  setHistoryEnabled(label, enabled) {
    const key = String(label || "");
    if (!key) return;
    if (enabled) this.hiddenHistoryModels.delete(key);
    else this.hiddenHistoryModels.add(key);
    this.persistHiddenHistoryModels();
  }

  toggleHistory(label) {
    this.setHistoryEnabled(label, !this.isHistoryEnabled(label));
  }

  /** Drop hidden entries no longer present among recognized labels so the
   *  persisted set does not accumulate dead models forever. */
  pruneHiddenHistoryModels(knownLabels) {
    const known = new Set(Array.from(knownLabels, (label) => String(label || "")).filter(Boolean));
    let changed = false;
    for (const label of Array.from(this.hiddenHistoryModels)) {
      if (!known.has(label)) { this.hiddenHistoryModels.delete(label); changed = true; }
    }
    if (changed) this.persistHiddenHistoryModels();
  }

  persistHiddenHistoryModels(storage = this.storage) {
    try {
      storage?.setItem(hiddenHistoryModelsStorageKey, JSON.stringify({
        version: HIDDEN_HISTORY_VERSION,
        hidden: Array.from(this.hiddenHistoryModels),
      }));
    } catch {
      // Storage unavailable: keep the in-memory selection for this session.
    }
  }

  // --- Shared model color registry ------------------------------------------

  /** Resolve and persist a stable color for `label`, shared by every panel. */
  colorFor(label) {
    const before = this.modelColorRegistry[String(label || "")];
    const color = ensureModelColor(this.modelColorRegistry, label);
    if (this.modelColorRegistry[String(label || "")] !== before) this.persistModelColorRegistry();
    return color;
  }

  persistModelColorRegistry(storage = this.storage) {
    try {
      storage?.setItem(modelColorRegistryStorageKey, JSON.stringify({
        version: MODEL_COLOR_VERSION,
        colors: { ...this.modelColorRegistry },
      }));
    } catch {
      // Storage unavailable: colors stay stable within the session regardless.
    }
  }

  /**
   * Migrate browser-only toggle and color preferences after a successful
   * category rename so the merged label keeps the old one's visibility and
   * color. Invoked by the category-rename flow.
   */
  remapModelLabel(oldLabel, newLabel) {
    const from = String(oldLabel || "");
    const to = String(newLabel || "");
    if (!from || !to || from === to) return;
    let changed = false;
    if (this.hiddenHistoryModels.has(from)) {
      this.hiddenHistoryModels.delete(from);
      this.hiddenHistoryModels.add(to);
      this.persistHiddenHistoryModels();
    }
    // Carry the old color forward only when the target has no color yet, so an
    // intentional merge into an existing category keeps that category's color.
    if (this.modelColorRegistry[from]) {
      if (!this.modelColorRegistry[to]) { this.modelColorRegistry[to] = this.modelColorRegistry[from]; changed = true; }
      delete this.modelColorRegistry[from]; changed = true;
    }
    if (changed) this.persistModelColorRegistry();
  }
}

export const state = new DashboardState();
