import test from "node:test";
import assert from "node:assert/strict";
import { OscilloscopePanel } from "./components/oscilloscope.js";
import { TokenUsagePanel } from "./components/token-usage.js";
import {
  DashboardState,
  hiddenHistoryModelsStorageKey,
  modelColorRegistryStorageKey,
} from "./state.js";
import {
  buildEfficiencyGroups,
  buildComplexityDistribution,
  buildModelUsageSegments,
  buildOscilloscopeSeries,
  classifyModelDescriptor,
  classifyModelDescriptorDetailed,
  parseCanonicalModelLabel,
  modelIdentity,
  computeTokenUsageStats,
  computeModelUsageCounts,
  efficiencyJitterOffset,
  ensureModelColor,
  orderModelLabels,
  filterRowsByComplexity,
  MODEL_BASE_COLORS,
} from "./analytics/shared.js";

// In-memory localStorage stub so DashboardState persistence is testable and
// isolated per test. Optionally seeded from an initial map.
function memoryStorage(initial = {}) {
  const store = new Map(Object.entries(initial));
  return {
    store,
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => { store.set(key, String(value)); },
    removeItem: (key) => { store.delete(key); },
  };
}

// A DashboardState-backed fixture, so panels get the real colorFor/history APIs.
function stateFixture(storage = memoryStorage()) {
  const state = new DashboardState({ storage });
  state.complexitySource = "planned";
  state.complexityBucket = "";
  state.modelSegments = [];
  state.recsExpanded = false;
  return state;
}

const colorFor = (state) => (label) => state.colorFor(label);

test("analytics helpers keep empty and mixed-model states stable", () => {
  assert.deepEqual(buildModelUsageSegments(new Map()), { segments: [], total: 0 });
  const rows = [
    { agent_label: "Codex GPT 5.5", planned_complexity: "S" },
    { agent_label: "Claude Haiku", planned_complexity: "M" },
    { agent_label: "unregistered-agent", planned_complexity: "S" },
  ];
  assert.equal(classifyModelDescriptor(rows[0].agent_label), "GPT 5.5");
  assert.equal(classifyModelDescriptor(rows[1].agent_label), "Haiku 4.5");
  const segments = buildModelUsageSegments(computeModelUsageCounts(rows));
  assert.deepEqual(segments.segments.map((segment) => segment.label), ["GPT 5.5", "Haiku 4.5"]);
  assert.equal(segments.total, 2);
});

test("canonical registration labels parse into family, variant, and effort", () => {
  assert.deepEqual(parseCanonicalModelLabel("5.6 Luna Extra High"),
    { family: "GPT", version: "5.6", variant: "Luna", effort: "Extra High" });
  assert.deepEqual(parseCanonicalModelLabel("Opus 4.8 High"),
    { family: "Opus", version: "4.8", variant: "", effort: "High" });
  // Multi-word variants and hyphenated versions must survive the parse.
  assert.deepEqual(parseCanonicalModelLabel("Opus 4.8 Deep Think High"),
    { family: "Opus", version: "4.8", variant: "Deep Think", effort: "High" });
  assert.equal(parseCanonicalModelLabel("unregistered-agent"), null);

  // "Extra High" must win over the "High" suffix.
  assert.equal(classifyModelDescriptorDetailed("5.6 Luna Extra High").effort, "Extra High");
  assert.equal(classifyModelDescriptorDetailed("5.6 Luna High").effort, "High");
  // Missing effort defaults to High for both families.
  assert.equal(classifyModelDescriptorDetailed("Opus 4.8").effort, "High");
  assert.equal(classifyModelDescriptorDetailed("Codex CLI (GPT 5.6)").effort, "High");
  assert.equal(classifyModelDescriptorDetailed("unregistered-agent").effort, "");

  // Every effort tier of one model collapses to a single identity bucket.
  const luna = ["5.6 Luna Extra High", "5.6 Luna High", "5.6 Luna Medium", "5.6 Luna Low"];
  assert.deepEqual([...new Set(luna.map((label) => modelIdentity({ canonical_model_label: label })))],
    ["GPT 5.6 Luna"]);
  // Variants stay distinct from one another.
  assert.equal(modelIdentity({ canonical_model_label: "5.6 Sol High" }), "GPT 5.6 Sol");
  // Legacy free text unifies with the canonical spelling rather than forking a bucket.
  assert.equal(classifyModelDescriptor("Codex GPT 5.5"), classifyModelDescriptor("GPT 5.5"));
});

test("versionless legacy descriptors fall back to current default models", () => {
  // Major-version-only text promotes to the release actually in use.
  assert.equal(classifyModelDescriptor("Opus 4"), "Opus 4.8");
  assert.equal(classifyModelDescriptor("Claude Code (Opus 4)"), "Opus 4.8");
  assert.equal(classifyModelDescriptor("gpt-5"), "GPT 5.5");
  assert.equal(classifyModelDescriptor("Codex GPT-5"), "GPT 5.5");
  // Bare vendor words with no version at all.
  assert.equal(classifyModelDescriptor("Claude"), "Opus 4.8");
  assert.equal(classifyModelDescriptor("anthropic"), "Opus 4.8");
  assert.equal(classifyModelDescriptor("Codex"), "GPT 5.5");
  assert.equal(classifyModelDescriptor("openai"), "GPT 5.5");
  // Explicit versions are never overridden by the catch-alls.
  assert.equal(classifyModelDescriptor("Opus 4.8"), "Opus 4.8");
  assert.equal(classifyModelDescriptor("Claude Code (Opus 5)"), "Opus 5");
  assert.equal(classifyModelDescriptor("Codex CLI (GPT 5.6)"), "GPT 5.6");
});

test("real stored descriptors from mcp.db land in stable buckets", () => {
  // Canonical labels the server actually wrote, including the family-less GPT form.
  assert.equal(modelIdentity({ canonical_model_label: "5.6 Sol High" }), "GPT 5.6 Sol");
  // Bare major versions promote to the release in use, so these join the GPT 5.5 bucket.
  assert.equal(modelIdentity({ canonical_model_label: "5 Codex High" }), "GPT 5.5 Codex");
  assert.equal(modelIdentity({ canonical_model_label: "GPT 5 High" }), "GPT 5.5");
  // "5 High High" is a misregistration (effort in the variant slot); it must still
  // parse rather than fall through to the lossy legacy path.
  assert.equal(modelIdentity({ canonical_model_label: "5 High High" }), "GPT 5.5 High");
  // Free-text agent_labels that predate registration.
  assert.equal(modelIdentity({ agent_label: "Claude Code (Opus 4.8)" }), "Opus 4.8");
  assert.equal(modelIdentity({ agent_label: "Claude Code (Fable 5)" }), "Fable 5");
  assert.equal(modelIdentity({ agent_label: "Codex CLI (GPT 5.6)" }), "GPT 5.6");
  assert.equal(modelIdentity({ agent_label: "Claude Code (Opus 4.8 1M)" }), "Opus 4.8");
});

test("complexity filtering and model-isolation share the same state", () => {
  const state = stateFixture();
  state.complexityBucket = "S";
  const rows = [{ task_key: "small", planned_complexity: "S" }, { task_key: "large", planned_complexity: "L" }];
  assert.deepEqual(filterRowsByComplexity(rows, state).map((row) => row.task_key), ["small"]);
  let changes = 0;
  const panel = new OscilloscopePanel({ state, onSelectionChange: () => { changes += 1; } });
  panel.toggleModel("GPT 5.5");
  assert.equal(state.osc.selectedModel, "GPT 5.5");
  panel.toggleModel("GPT 5.5");
  assert.equal(state.osc.selectedModel, null);
  assert.equal(changes, 2);
});

test("analytics policies exclude unknowns from complexity, tokens, and history lanes", () => {
  const state = stateFixture();
  const distribution = buildComplexityDistribution([
    { planned_complexity: "S" },
    { planned_complexity: "unknown" },
    { planned_complexity: "unrecognized" },
  ], state);
  assert.deepEqual([...distribution.counts.entries()], [["S", 1], ["M", 0], ["L", 0], ["XL", 0]]);
  assert.equal(distribution.dataRows.length, 1);

  const usage = computeTokenUsageStats([
    { model_descriptor: "Opus 4.8", total_tokens: 100 },
    { model_descriptor: "opaque-unregistered-agent", total_tokens: 9999 },
  ]);
  assert.equal(usage.taskCount, 1);
  assert.equal(usage.totalTokens, 100);

  // Unknown rows are dropped from history lanes; every recognized model gets its
  // own exact lane (no fixed three-lane allowlist).
  const series = buildOscilloscopeSeries([
    { model_descriptor: "5.6 Luna Extra High", ts: "2026-08-11T00:00:00Z", total_tokens: 20 },
    { model_descriptor: "5.6 Sol High", ts: "2026-08-11T00:01:00Z", total_tokens: 30 },
    { model_descriptor: "Haiku 4.5", ts: "2026-08-11T00:02:00Z", total_tokens: 40 },
    { model_descriptor: "opaque-unregistered-agent", ts: "2026-08-11T00:03:00Z", total_tokens: 9999 },
  ], colorFor(state));
  assert.deepEqual(series.map((entry) => entry.label).sort(),
    ["GPT 5.6 Luna", "GPT 5.6 Sol", "Haiku 4.5"]);
  assert.equal(series.find((entry) => entry.label === "GPT 5.6 Luna").points.length, 1);
});

test("history lanes preserve exact canonical-variant separation", () => {
  const state = stateFixture();
  const series = buildOscilloscopeSeries([
    { model_descriptor: "5.6 Luna High", ts: "2026-08-11T00:00:00Z", total_tokens: 10 },
    { model_descriptor: "5.6 Sol High", ts: "2026-08-11T00:01:00Z", total_tokens: 20 },
  ], colorFor(state));
  const labels = series.map((entry) => entry.label);
  assert.ok(labels.includes("GPT 5.6 Luna"));
  assert.ok(labels.includes("GPT 5.6 Sol"));
  assert.notEqual(labels[0], labels[1]);
});

test("history lanes follow shared model ordering, not toggle-click order", () => {
  const state = stateFixture();
  const rows = [
    { model_descriptor: "Haiku 4.5", ts: "2026-08-11T00:00:00Z", total_tokens: 10 },
    { model_descriptor: "Opus 4.8", ts: "2026-08-11T00:01:00Z", total_tokens: 500 },
    { model_descriptor: "GPT 5.6 Sol", ts: "2026-08-11T00:02:00Z", total_tokens: 100 },
  ];
  // Descending token weight regardless of the order rows appear in.
  const order = buildOscilloscopeSeries(rows, colorFor(state)).map((entry) => entry.label);
  assert.deepEqual(order, ["Opus 4.8", "GPT 5.6 Sol", "Haiku 4.5"]);
  // Toggling off then on the middle lane leaves the order untouched.
  state.setHistoryEnabled("GPT 5.6 Sol", false);
  state.setHistoryEnabled("GPT 5.6 Sol", true);
  const reordered = buildOscilloscopeSeries(rows, colorFor(state), state.enabledHistoryLabels(["Opus 4.8", "GPT 5.6 Sol", "Haiku 4.5"])).map((entry) => entry.label);
  assert.deepEqual(reordered, ["Opus 4.8", "GPT 5.6 Sol", "Haiku 4.5"]);
});

test("enabled-history set filters lanes without touching totals", () => {
  const state = stateFixture();
  const rows = [
    { model_descriptor: "Opus 4.8", ts: "2026-08-11T00:00:00Z", total_tokens: 100 },
    { model_descriptor: "Haiku 4.5", ts: "2026-08-11T00:01:00Z", total_tokens: 200 },
  ];
  const known = ["Opus 4.8", "Haiku 4.5"];
  // Default: both ON.
  assert.equal(buildOscilloscopeSeries(rows, colorFor(state), state.enabledHistoryLabels(known)).length, 2);
  // Hide one lane; only that lane drops.
  state.setHistoryEnabled("Haiku 4.5", false);
  const enabled = state.enabledHistoryLabels(known);
  const shown = buildOscilloscopeSeries(rows, colorFor(state), enabled).map((entry) => entry.label);
  assert.deepEqual(shown, ["Opus 4.8"]);
  // Totals/stats are unaffected by the toggle.
  const usage = computeTokenUsageStats(rows);
  assert.equal(usage.totalTokens, 300);
  assert.equal(usage.taskCount, 2);
});

test("history visibility defaults ON and persists explicit OFF across reloads", () => {
  const storage = memoryStorage();
  const state = stateFixture(storage);
  // Default ON with no saved preference.
  assert.equal(state.isHistoryEnabled("Opus 4.8"), true);
  state.setHistoryEnabled("Opus 4.8", false);
  assert.equal(state.isHistoryEnabled("Opus 4.8"), false);
  assert.match(storage.getItem(hiddenHistoryModelsStorageKey), /Opus 4\.8/);
  // A fresh state reading the same storage retains the OFF choice; a
  // newly-discovered model still defaults ON.
  const reloaded = stateFixture(storage);
  assert.equal(reloaded.isHistoryEnabled("Opus 4.8"), false);
  assert.equal(reloaded.isHistoryEnabled("GPT 5.6 Sol"), true);
});

test("stale hidden entries are pruned and never suppress a live lane", () => {
  const storage = memoryStorage({
    [hiddenHistoryModelsStorageKey]: JSON.stringify({ version: 1, hidden: ["Retired 1.0", "Opus 4.8"] }),
  });
  const state = stateFixture(storage);
  state.pruneHiddenHistoryModels(["Opus 4.8", "Haiku 4.5"]);
  // The still-present hidden model stays hidden; the retired one is dropped.
  assert.equal(state.hiddenHistoryModels.has("Opus 4.8"), true);
  assert.equal(state.hiddenHistoryModels.has("Retired 1.0"), false);
  const enabled = state.enabledHistoryLabels(["Opus 4.8", "Haiku 4.5"]);
  assert.deepEqual([...enabled].sort(), ["Haiku 4.5"]);
});

test("corrupt or wrong-version stored preferences fall back to defaults", () => {
  const badHidden = memoryStorage({ [hiddenHistoryModelsStorageKey]: "{not json" });
  assert.equal(stateFixture(badHidden).hiddenHistoryModels.size, 0);
  const oldVersion = memoryStorage({ [hiddenHistoryModelsStorageKey]: JSON.stringify({ version: 0, hidden: ["Opus 4.8"] }) });
  assert.equal(stateFixture(oldVersion).isHistoryEnabled("Opus 4.8"), true);
  const badColors = memoryStorage({ [modelColorRegistryStorageKey]: JSON.stringify({ version: 1, colors: { "X": "not-a-color", "Y": "#123456" } }) });
  const state = stateFixture(badColors);
  // Invalid hex is dropped; the valid one survives.
  assert.equal(state.modelColorRegistry.X, undefined);
  assert.equal(state.modelColorRegistry.Y, "#123456");
});

test("storage-write failure leaves the in-memory selection intact", () => {
  const throwingStorage = {
    getItem: () => null,
    setItem: () => { throw new Error("quota exceeded"); },
    removeItem: () => {},
  };
  const state = stateFixture(throwingStorage);
  // Must not throw even though persistence fails.
  state.setHistoryEnabled("Opus 4.8", false);
  assert.equal(state.isHistoryEnabled("Opus 4.8"), false);
  assert.doesNotThrow(() => state.colorFor("Some New Model"));
});

test("shared color registry gives one stable color per label across panels", () => {
  const state = stateFixture();
  // Base-color labels are fixed and shared everywhere.
  assert.equal(state.colorFor("Opus 4.8"), MODEL_BASE_COLORS["Opus 4.8"]);
  const segments = buildModelUsageSegments(
    computeModelUsageCounts([{ agent_label: "Opus 4.8" }, { agent_label: "Haiku 4.5" }]),
    colorFor(state),
  ).segments;
  const barColor = state.colorFor("Haiku 4.5");
  const segColor = segments.find((seg) => seg.label === "Haiku 4.5").color;
  const series = buildOscilloscopeSeries(
    [{ model_descriptor: "Haiku 4.5", ts: "2026-08-11T00:00:00Z", total_tokens: 5 }],
    colorFor(state),
  );
  const historyColor = series[0].color;
  const groups = buildEfficiencyGroups(
    [{ model_descriptor: "Haiku 4.5", total_tokens: 100, actual_complexity_num: 1 }],
    colorFor(state),
  );
  const effColor = groups[0].color;
  // Strict per-label color equality across pie/bar/history/efficiency.
  assert.equal(segColor, barColor);
  assert.equal(historyColor, barColor);
  assert.equal(effColor, barColor);
});

test("colors are deterministic, distinct, and never reassigned by sort/token churn", () => {
  const a = stateFixture();
  const b = stateFixture();
  // Same label -> same color across independent registries.
  assert.equal(a.colorFor("Team Falcon"), b.colorFor("Team Falcon"));
  // Distinct labels get distinct colors (within the pool).
  assert.notEqual(a.colorFor("Team Falcon"), a.colorFor("Team Eagle"));
  // A later allocation never rewrites an earlier one.
  const first = a.colorFor("Team Falcon");
  a.colorFor("Team Eagle"); a.colorFor("Team Hawk");
  assert.equal(a.colorFor("Team Falcon"), first);
  // ensureModelColor is idempotent on the same registry object.
  const reg = Object.create(null);
  const c1 = ensureModelColor(reg, "Team Falcon");
  const c2 = ensureModelColor(reg, "Team Falcon");
  assert.equal(c1, c2);
});

test("persisted color registry keeps colors stable across a reload", () => {
  const storage = memoryStorage();
  const state = stateFixture(storage);
  const color = state.colorFor("Team Falcon");
  assert.match(storage.getItem(modelColorRegistryStorageKey), /Team Falcon/);
  const reloaded = stateFixture(storage);
  assert.equal(reloaded.colorFor("Team Falcon"), color);
});

test("remapModelLabel migrates history toggle and color to the merged label", () => {
  const storage = memoryStorage();
  const state = stateFixture(storage);
  const color = state.colorFor("Old Name");
  state.setHistoryEnabled("Old Name", false);
  state.remapModelLabel("Old Name", "New Name");
  // Visibility and color follow the rename.
  assert.equal(state.isHistoryEnabled("New Name"), false);
  assert.equal(state.isHistoryEnabled("Old Name"), true);
  assert.equal(state.colorFor("New Name"), color);
  assert.equal(state.modelColorRegistry["Old Name"], undefined);
  // Merge target that already has a color keeps its own color.
  const other = stateFixture();
  const targetColor = other.colorFor("Existing");
  other.colorFor("Source");
  other.remapModelLabel("Source", "Existing");
  assert.equal(other.colorFor("Existing"), targetColor);
});

test("orderModelLabels pins Other/Unknown last and sorts by weight then name", () => {
  const weights = new Map([["Opus 4.8", 100], ["Haiku 4.5", 100], ["GPT 5.6 Sol", 500]]);
  assert.deepEqual(
    orderModelLabels(["Other", "Haiku 4.5", "Opus 4.8", "GPT 5.6 Sol"], weights),
    ["GPT 5.6 Sol", "Haiku 4.5", "Opus 4.8", "Other"],
  );
});

test("oscilloscope and efficiency identity use deterministic palette and jitter", () => {
  const state = stateFixture();
  const groups = buildEfficiencyGroups([
    { model_descriptor: "Codex GPT 5.5", total_tokens: 100, actual_complexity_num: 1 },
    { model_descriptor: "Claude Haiku", total_tokens: 200, actual_complexity_num: 2 },
  ], colorFor(state));
  // Ordered by descending token weight.
  assert.deepEqual(groups.map((group) => group.label), ["Haiku 4.5", "GPT 5.5"]);
  assert.equal(efficiencyJitterOffset("same-task:agent"), efficiencyJitterOffset("same-task:agent"));
});

test("recommendation status action calls the API and refreshes", async () => {
  const button = { disabled: false, dataset: { taskKey: "todo-1", agentId: "agent-1", currentStatus: "open" } };
  const apiCalls = [];
  let refreshed = 0;
  const panel = new TokenUsagePanel({
    state: stateFixture(),
    api: { updateRecommendationStatus: async (body) => apiCalls.push(body) },
    onRefresh: async () => { refreshed += 1; },
    documentRef: { getElementById: () => null },
  });
  await panel.handleClick({ target: { closest: (selector) => selector === ".token-rec-status-toggle" ? button : null } });
  assert.deepEqual(apiCalls, [{ task_key: "todo-1", agent_id: "agent-1", status: "implemented" }]);
  assert.equal(refreshed, 1);
});

test("effective_model_label wins over canonical label for grouping", () => {
  // The server resolves durable renames into effective_model_label; the panel
  // must group by it so a rename cannot be undone client-side.
  assert.equal(modelIdentity({ canonical_model_label: "Opus 4.8 High", effective_model_label: "Team Opus" }), "Team Opus");
  assert.equal(modelIdentity({ canonical_model_label: "Opus 4.8 High" }), "Opus 4.8");
});

// Minimal DOM host: a dialog element plus the error banner, addressable by id.
function tokenDialogDocument() {
  const elements = {
    tokenCategoryDialog: { hidden: true, innerHTML: "", _input: null,
      querySelector(sel) { return sel.includes("input") ? this._input : null; } },
    tokenCategoryError: { hidden: true, textContent: "" },
  };
  return { getElementById: (id) => elements[id] || null, _elements: elements };
}

test("rename dialog submits trimmed label to the API and refreshes", async () => {
  const doc = tokenDialogDocument();
  const calls = [];
  let refreshed = 0;
  const panel = new TokenUsagePanel({
    state: stateFixture(),
    api: { renameAgentCategory: async (body) => { calls.push(body); } },
    onRefresh: async () => { refreshed += 1; },
    documentRef: doc,
  });
  await panel.submitRename("Opus 4.8", "  Team Opus  ");
  assert.deepEqual(calls, [{ old_label: "Opus 4.8", new_label: "Team Opus" }]);
  assert.equal(refreshed, 1);
  assert.equal(doc._elements.tokenCategoryDialog.hidden, true);
});

test("rename migrates local toggle/color state on success", async () => {
  const state = stateFixture();
  state.setHistoryEnabled("Opus 4.8", false);
  const panel = new TokenUsagePanel({
    state,
    api: { renameAgentCategory: async () => {} },
    onRefresh: async () => {},
    documentRef: tokenDialogDocument(),
  });
  await panel.submitRename("Opus 4.8", "Team Opus");
  assert.equal(state.isHistoryEnabled("Team Opus"), false);
});

test("rename surfaces backend errors without refreshing", async () => {
  const doc = tokenDialogDocument();
  let refreshed = 0;
  const panel = new TokenUsagePanel({
    state: stateFixture(),
    api: { renameAgentCategory: async () => { throw new Error("alias cycle"); } },
    onRefresh: async () => { refreshed += 1; },
    documentRef: doc,
  });
  await panel.submitRename("Opus 4.8", "Bad");
  assert.equal(refreshed, 0);
  assert.equal(doc._elements.tokenCategoryError.hidden, false);
  assert.match(doc._elements.tokenCategoryError.textContent, /alias cycle/);
});

test("rename rejects an empty replacement before calling the API", async () => {
  const doc = tokenDialogDocument();
  let called = false;
  const panel = new TokenUsagePanel({
    state: stateFixture(),
    api: { renameAgentCategory: async () => { called = true; } },
    onRefresh: async () => {},
    documentRef: doc,
  });
  await panel.submitRename("Opus 4.8", "   ");
  assert.equal(called, false);
  assert.equal(doc._elements.tokenCategoryError.hidden, false);
});

test("trash flow previews the count then confirms the purge", async () => {
  const doc = tokenDialogDocument();
  const purges = [];
  let previewed = null;
  let refreshed = 0;
  const panel = new TokenUsagePanel({
    state: stateFixture(),
    api: {
      previewAgentCategoryPurge: async (body) => { previewed = body; return { closed_task_count: 3 }; },
      purgeAgentCategoryClosed: async (body) => { purges.push(body); },
    },
    onRefresh: async () => { refreshed += 1; },
    documentRef: doc,
  });
  await panel.openTrashDialog("Opus 4.8");
  assert.deepEqual(previewed, { label: "Opus 4.8" });
  assert.match(doc._elements.tokenCategoryDialog.innerHTML, /3/);
  assert.equal(doc._elements.tokenCategoryDialog.hidden, false);

  await panel.confirmPurge("Opus 4.8");
  assert.deepEqual(purges, [{ label: "Opus 4.8", confirm: true }]);
  assert.equal(refreshed, 1);
  assert.equal(doc._elements.tokenCategoryDialog.hidden, true);
});

test("cancel closes the dialog and mutates nothing", async () => {
  const doc = tokenDialogDocument();
  let mutated = false;
  const panel = new TokenUsagePanel({
    state: stateFixture(),
    api: {
      renameAgentCategory: async () => { mutated = true; },
      purgeAgentCategoryClosed: async () => { mutated = true; },
    },
    onRefresh: async () => {},
    documentRef: doc,
  });
  panel.openRenameDialog("Opus 4.8");
  assert.equal(doc._elements.tokenCategoryDialog.hidden, false);
  await panel.handleClick({ target: { closest: (sel) => sel === "[data-cat-cancel]" ? {} : null } });
  assert.equal(doc._elements.tokenCategoryDialog.hidden, true);
  assert.equal(mutated, false);
});

// A DOM host that records innerHTML per id so render output is inspectable.
function renderDocument() {
  const rows = [];
  const elements = {};
  return {
    rows,
    elements,
    getElementById: (id) => {
      if (!elements[id]) { elements[id] = { id, textContent: "", innerHTML: "", hidden: true }; rows.push([id, elements[id]]); }
      return elements[id];
    },
  };
}

test("rendered category rows escape labels and add controls for named models only", () => {
  const doc = renderDocument();
  const state = stateFixture();
  const panel = new TokenUsagePanel({ state, api: {}, documentRef: doc });
  // Named category + an Unknown category; only the named one gets controls.
  panel.render([
    { canonical_model_label: "Opus 4.8 High", total_tokens: 100, task_key: "a", agent_id: "x" },
    { model_descriptor: "<script>", total_tokens: 50, task_key: "b", agent_id: "y" },
  ]);
  const bars = doc.elements.tokenUsageBars;
  assert.ok(bars);
  assert.match(bars.innerHTML, /data-cat-rename="Opus 4\.8"/);
  assert.match(bars.innerHTML, /data-cat-trash="Opus 4\.8"/);
  // Unknown row is excluded entirely (filtered by filterRowsByModel).
  assert.doesNotMatch(bars.innerHTML, /&lt;script&gt;/);
});

test("token rows render an accessible ON/OFF history switch reflecting state", () => {
  const doc = renderDocument();
  const state = stateFixture();
  state.setHistoryEnabled("Opus 4.8", false);
  const panel = new TokenUsagePanel({ state, api: {}, documentRef: doc });
  panel.render([{ canonical_model_label: "Opus 4.8 High", total_tokens: 100, task_key: "a", agent_id: "x" }]);
  const html = doc.elements.tokenUsageBars.innerHTML;
  // Native switch semantics + a visible OFF label, keyed to the exact model.
  assert.match(html, /role="switch"/);
  assert.match(html, /aria-checked="false"/);
  assert.match(html, /data-history-toggle="Opus 4\.8"/);
  assert.match(html, />OFF</);
});

test("clicking the history switch toggles state and rerenders without refetch", async () => {
  const state = stateFixture();
  let rerenders = 0;
  let refetches = 0;
  const btn = {
    getAttribute: (name) => (name === "data-history-toggle" ? "Opus 4.8" : null),
    setAttribute: () => {},
    classList: { toggle: () => {} },
    querySelector: () => ({ textContent: "" }),
  };
  const panel = new TokenUsagePanel({
    state,
    api: {},
    onRefresh: async () => { refetches += 1; },
    onToggleHistory: () => { rerenders += 1; },
    documentRef: { getElementById: () => null },
  });
  await panel.handleClick({ target: { closest: (sel) => sel === "[data-history-toggle]" ? btn : null } });
  assert.equal(state.isHistoryEnabled("Opus 4.8"), false);
  assert.equal(rerenders, 1);
  assert.equal(refetches, 0);
});

test("history toggle is independent from the transient isolation selection", () => {
  const state = stateFixture();
  // Isolating a model does not hide any lane's history.
  state.osc.selectedModel = "Opus 4.8";
  assert.equal(state.isHistoryEnabled("Opus 4.8"), true);
  // Disabling a lane whose model is the current isolation focus clears the focus
  // on the next render (it must not silently re-enable the disabled lane).
  state.setHistoryEnabled("Opus 4.8", false);
  const panel = new OscilloscopePanel({
    state,
    documentRef: renderDocument(),
    windowRef: { requestAnimationFrame: () => 0 },
  });
  panel.render(
    [{ model_descriptor: "Opus 4.8", ts: "2026-08-11T00:00:00Z", total_tokens: 100 }],
    [],
  );
  assert.equal(state.osc.selectedModel, null);
});

test("all-off history renders a specific instruction, not a no-usage empty state", () => {
  const doc = renderDocument();
  const state = stateFixture();
  state.modelSegments = [{ label: "Opus 4.8", color: "#2a78d6" }];
  state.setHistoryEnabled("Opus 4.8", false);
  const panel = new OscilloscopePanel({
    state,
    documentRef: doc,
    windowRef: { requestAnimationFrame: () => 0 },
  });
  panel.render(
    [{ model_descriptor: "Opus 4.8", ts: "2026-08-11T00:00:00Z", total_tokens: 100 }],
    [],
  );
  const empty = doc.elements.oscilloscopeEmpty;
  assert.match(empty.textContent, /history lanes are hidden/i);
  assert.equal(empty.hidden, false);
});
