import { asList } from "../dom.js";

export const COMPLEXITY_SOURCES = [
  { key: "planned", label: "Planned", field: "planned_complexity" },
  { key: "actual", label: "Actual", field: "actual_complexity" },
  { key: "compare", label: "Planned vs Actual" },
];
export const COMPLEXITY_BUCKETS = [
  { key: "S", label: "S" },
  { key: "M", label: "M" },
  { key: "L", label: "L" },
  { key: "XL", label: "XL" },
];
export const COMPLEXITY_NUMERIC_BUCKETS = new Map([
  ["1", "S"], ["1.0", "S"], ["2", "M"], ["2.0", "M"],
  ["3", "L"], ["3.0", "L"], ["4", "XL"], ["4.0", "XL"],
]);

export const MODEL_PALETTE_SLOTS = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"];
export const MODEL_OTHER_COLOR = "#5b6672";
export const MODEL_UNKNOWN_LABEL = "Unknown";
export const MODEL_OTHER_LABEL = "Other";
// Extra deterministic hues beyond the 8 categorical slots. Every recognized
// model — not just the donut's top 8 — needs its own stable color for the
// history lanes, so labels that overflow the palette draw from this ring.
export const MODEL_EXTRA_HUES = [
  "#0f766e", "#7c3aed", "#be123c", "#a16207", "#0369a1",
  "#15803d", "#c2410c", "#9333ea", "#0891b2", "#b45309",
];
// Mirrors mcp/lifecycle.py: MODEL_REGISTRATION_FAMILIES / MODEL_REGISTRATION_EFFORTS
// and canonical_model_label(). Canonical labels are
//   "<Family> <version>[ <Variant>] <Effort>", except the gpt family which omits
//   the family token entirely: "5.6 Luna Extra High".
const MODEL_FAMILIES = ["GPT", "Opus", "Sonnet", "Haiku", "Fable", "Gemini", "Llama", "Mistral", "Grok"];
// Longest-first so "Extra High" wins over "High".
const MODEL_EFFORTS = ["Extra High", "Medium", "Ultra", "High", "Max", "None", "Low"];
const MODEL_VERSION_RE = /^\d+(?:\.\d+){0,3}(?:[-_][A-Za-z0-9]+)?$/;
// The gpt family drops its name in canonical labels, so a bare version leads the
// text. Re-attach the family name ("5.6 Luna" -> "GPT 5.6 Luna") so every family
// renders the same way: "<Family> <version>[ <Variant>]".
const GPT_IMPLIED_FAMILY = "GPT";

// Legacy free-text descriptors often name only a major version ("Opus 4", "gpt-5").
// Promote those to the release actually in use so they merge with the canonical
// bucket instead of forming a near-duplicate lane.
const MODEL_VERSION_PROMOTIONS = { Opus: { "4": "4.8" }, GPT: { "5": "5.5" } };
// Vendor-level catch-all when a descriptor names no version at all.
const MODEL_VENDOR_FALLBACKS = { anthropic: "Opus 4.8", openai: "GPT 5.5" };
// Descriptors that predate effort registration carry no tier; assume the tier
// these agents actually ran at rather than leaving the field blank.
export const MODEL_DEFAULT_EFFORT = "High";

function normalizeModelVersion(family, version) {
  return MODEL_VERSION_PROMOTIONS[family]?.[version] || version;
}

const titleCaseWord = (word) => (/^\d/.test(word) ? word : word.charAt(0).toUpperCase() + word.slice(1).toLowerCase());

function matchModelEffort(tokens) {
  for (const effort of MODEL_EFFORTS) {
    const parts = effort.split(" ");
    if (tokens.length <= parts.length) continue;
    const tail = tokens.slice(-parts.length).join(" ").toLowerCase();
    if (tail === effort.toLowerCase()) return { effort, rest: tokens.slice(0, -parts.length) };
  }
  return { effort: "", rest: tokens };
}

/**
 * Parse a canonical registration label into its parts. Returns null when the text
 * does not follow the canonical grammar, so callers can fall back to legacy
 * keyword matching.
 */
export function parseCanonicalModelLabel(descriptor) {
  const tokens = String(descriptor || "").trim().replace(/\s+/g, " ").split(" ").filter(Boolean);
  if (!tokens.length) return null;
  const { effort, rest } = matchModelEffort(tokens);
  if (!rest.length) return null;
  let family = "";
  let versionIndex = 0;
  const head = MODEL_FAMILIES.find((name) => name.toLowerCase() === rest[0].toLowerCase());
  if (head) { family = head; versionIndex = 1; }
  const version = rest[versionIndex] || "";
  if (!MODEL_VERSION_RE.test(version)) return null;
  if (!family) family = GPT_IMPLIED_FAMILY;
  const variant = rest.slice(versionIndex + 1).map(titleCaseWord).join(" ");
  return { family, version, variant, effort };
}

/** Identity without the effort tier: "GPT 5.6 Luna", "Opus 4.8". */
export function modelIdentityLabel(parsed) {
  if (!parsed) return "";
  // Promote bare major versions ("Opus 4" -> "Opus 4.8") here rather than only on
  // the legacy path: "Opus 4" parses as a valid canonical family+version, so it
  // would otherwise bypass normalization entirely.
  const version = normalizeModelVersion(parsed.family, parsed.version);
  const stem = `${parsed.family} ${version}`;
  return parsed.variant ? `${stem} ${parsed.variant}` : stem;
}

// Legacy fallback for unregistered/free-text descriptors only. No default
// versions: inventing one fuses genuinely distinct models into a single bucket.
const MODEL_FAMILY_PATTERNS = [
  ["Opus", /opus[\s._-]*(\d+(?:\.\d+)?)?/],
  ["Sonnet", /sonnet[\s._-]*(\d+(?:\.\d+)?)?/],
  ["Haiku", /(?:claude[\s._-]*)?haiku[\s._-]*(\d+(?:\.\d+)?)?/],
  ["Fable", /fable[\s._-]*(\d+(?:\.\d+)?)?/],
  ["GPT", /gpt[\s._-]*(\d+(?:\.\d+)?)?/],
  ["Gemini", /gemini[\s._-]*(\d+(?:\.\d+)?)?/],
  ["Llama", /llama[\s._-]*(\d+(?:\.\d+)?)?/],
  ["Mistral", /mistral[\s._-]*(\d+(?:\.\d+)?)?/],
  ["Grok", /grok[\s._-]*(\d+(?:\.\d+)?)?/],
];
const MODEL_FAMILY_DEFAULT_VERSIONS = { Fable: "5", GPT: "5.5", Haiku: "4.5" };

export const OSC_TIMESCALES = [
  { key: "10m", label: "10m", ms: 10 * 60e3 },
  { key: "30m", label: "30m", ms: 30 * 60e3 },
  { key: "1h", label: "1h", ms: 3600e3 },
  { key: "2h", label: "2h", ms: 2 * 3600e3 },
  { key: "6h", label: "6h", ms: 6 * 3600e3 },
  { key: "24h", label: "24h", ms: 24 * 3600e3 },
  { key: "7d", label: "7d", ms: 7 * 86400e3 },
  { key: "30d", label: "30d", ms: 30 * 86400e3 },
  { key: "all", label: "All", ms: Infinity },
];
export const OSC_DEFAULT_TIMESCALE = "6h";
export const OSC_BIN_COUNT = 32;
export const OSC_LOG_FLOOR = 1;
export const EFFICIENCY_MIN_SAMPLES = 5;
export const EFFICIENCY_MIN_PLOT_POINTS = 3;

export function complexitySourceConfig(source) {
  return COMPLEXITY_SOURCES.find((item) => item.key === source) || COMPLEXITY_SOURCES[0];
}

export function complexityBucketConfig(bucket) {
  return COMPLEXITY_BUCKETS.find((item) => item.key === bucket) || null;
}

export function hasComplexityValue(value) { return String(value ?? "").trim() !== ""; }

export function normalizeComplexityBucket(value) {
  const text = String(value ?? "").trim().toUpperCase();
  if (!text) return "unknown";
  if (COMPLEXITY_BUCKETS.some((bucket) => bucket.key === text)) return text;
  return COMPLEXITY_NUMERIC_BUCKETS.get(text) || "unknown";
}

export function isIncludedComplexityBucket(value) {
  return Boolean(complexityBucketConfig(normalizeComplexityBucket(value)));
}

function rowHasComparableComplexity(row) {
  return isIncludedComplexityBucket(row.planned_complexity)
    && isIncludedComplexityBucket(row.actual_complexity);
}

export function rowMatchesComplexityBucket(row, source, bucket) {
  if (!bucket) return true;
  if (!complexityBucketConfig(bucket)) return false;
  if (source === "compare") {
    if (!rowHasComparableComplexity(row)) return false;
    return normalizeComplexityBucket(row.planned_complexity) === bucket
      || normalizeComplexityBucket(row.actual_complexity) === bucket;
  }
  const field = complexitySourceConfig(source).field || "planned_complexity";
  return normalizeComplexityBucket(row[field]) === bucket;
}

export function filterRowsByComplexity(rows, state) {
  if (!state.complexityBucket) return asList(rows);
  return asList(rows).filter((row) => rowMatchesComplexityBucket(
    row, state.complexitySource, state.complexityBucket,
  ));
}

export function buildComplexityDistribution(tasks, state) {
  const rows = asList(tasks);
  const source = complexitySourceConfig(state.complexitySource).key;
  if (source === "compare") {
    const comparableRows = rows.filter(rowHasComparableComplexity);
    const plannedCounts = new Map(COMPLEXITY_BUCKETS.map((bucket) => [bucket.key, 0]));
    const actualCounts = new Map(COMPLEXITY_BUCKETS.map((bucket) => [bucket.key, 0]));
    comparableRows.forEach((row) => {
      const plannedBucket = normalizeComplexityBucket(row.planned_complexity);
      const actualBucket = normalizeComplexityBucket(row.actual_complexity);
      plannedCounts.set(plannedBucket, (plannedCounts.get(plannedBucket) || 0) + 1);
      actualCounts.set(actualBucket, (actualCounts.get(actualBucket) || 0) + 1);
    });
    return { source, rows, dataRows: comparableRows, hasData: comparableRows.length > 0, plannedCounts, actualCounts };
  }
  const field = complexitySourceConfig(source).field || "planned_complexity";
  const dataRows = rows.filter((row) => isIncludedComplexityBucket(row[field]));
  const hasData = dataRows.length > 0;
  const counts = new Map(COMPLEXITY_BUCKETS.map((bucket) => [bucket.key, 0]));
  if (hasData) dataRows.forEach((row) => {
    const bucket = normalizeComplexityBucket(row[field]);
    counts.set(bucket, (counts.get(bucket) || 0) + 1);
  });
  return { source, rows, dataRows, hasData, counts };
}

export function complexityEmptyMessage(source, rows) {
  if (source === "actual" && rows.some((row) => hasComplexityValue(row.planned_complexity))) return "No actual complexity submitted yet.";
  if (source === "compare" && rows.some((row) => hasComplexityValue(row.planned_complexity) || hasComplexityValue(row.actual_complexity))) return "No tasks have both planned and actual complexity yet.";
  return "No complexity data on recent tasks.";
}

/**
 * Collapse a descriptor to a stable model identity, dropping the effort tier so
 * panels group by model rather than by reasoning setting. Use
 * `classifyModelDescriptorDetailed` when the effort matters.
 */
export function classifyModelDescriptor(descriptor) {
  const text = String(descriptor || "").toLowerCase().trim();
  if (!text) return MODEL_UNKNOWN_LABEL;
  const parsed = parseCanonicalModelLabel(descriptor);
  if (parsed) return modelIdentityLabel(parsed);
  for (const [family, pattern] of MODEL_FAMILY_PATTERNS) {
    const match = text.match(pattern);
    if (match) {
      const version = normalizeModelVersion(family, match[1] || MODEL_FAMILY_DEFAULT_VERSIONS[family] || "");
      // Reuse the canonical builder so legacy text lands in the same bucket as a
      // registered label ("Codex GPT 5.5" -> "GPT-5.5", not "GPT 5.5").
      return version ? modelIdentityLabel({ family, version, variant: "" }) : family;
    }
  }
  // Bare vendor words carry no version. Fall back to the current default model
  // for that vendor so these rows join an existing bucket instead of splintering.
  if (/claude|anthropic/.test(text)) return MODEL_VENDOR_FALLBACKS.anthropic;
  if (/openai|chatgpt|codex/.test(text)) return MODEL_VENDOR_FALLBACKS.openai;
  return MODEL_UNKNOWN_LABEL;
}

/**
 * Full identity including the reasoning tier, e.g.
 * `{ label: "GPT-5.6 Luna", effort: "Extra High", display: "GPT-5.6 Luna Extra High" }`.
 * `effort` is "" when the descriptor did not carry one.
 */
export function classifyModelDescriptorDetailed(descriptor) {
  const label = classifyModelDescriptor(descriptor);
  if (label === MODEL_UNKNOWN_LABEL) return { label, effort: "", display: label };
  // Descriptors without a registered tier default to High rather than staying
  // blank, so effort-aware panels have one bucket per model instead of two.
  const effort = parseCanonicalModelLabel(descriptor)?.effort || MODEL_DEFAULT_EFFORT;
  return { label, effort, display: `${label} ${effort}` };
}

function firstDescriptor(row, legacyDescriptor) {
  return legacyDescriptor || row?.canonical_model_label || row?.model_descriptor
    || row?.agent_label || row?.agent_id || "";
}

export function modelIdentity(row, legacyDescriptor) {
  // The server resolves the effective grouping label through durable category
  // aliases (Token Usage renames). Prefer it so a renamed category cannot be
  // undone client-side by a raw canonical/descriptor read.
  const effective = row && String(row.effective_model_label || "").trim();
  if (effective) return effective;
  // Normalize the canonical label too: it embeds the effort tier, and passing it
  // through raw split one model across several buckets.
  const canonical = row && String(row.canonical_model_label || "").trim();
  if (canonical) {
    const parsed = parseCanonicalModelLabel(canonical);
    if (parsed) return modelIdentityLabel(parsed);
    return canonical;
  }
  return classifyModelDescriptor(firstDescriptor(row, legacyDescriptor));
}

/** Model identity plus reasoning tier for a row, preferring the canonical label. */
export function modelIdentityDetailed(row, legacyDescriptor) {
  const canonical = row && String(row.canonical_model_label || "").trim();
  return classifyModelDescriptorDetailed(canonical || firstDescriptor(row, legacyDescriptor));
}

export function isIncludedModelRow(row) {
  return modelIdentity(row, row?.model_descriptor) !== MODEL_UNKNOWN_LABEL;
}

export function filterRowsByModel(rows) {
  return asList(rows).filter(isIncludedModelRow);
}

export function computeModelUsageCounts(tasks) {
  const counts = new Map();
  filterRowsByModel(tasks).forEach((task) => {
    const label = modelIdentity(task, task.agent_label);
    counts.set(label, (counts.get(label) || 0) + 1);
  });
  return counts;
}

export function buildModelUsageSegments(counts, colorFor = null) {
  const namedEntries = Array.from(counts.entries()).filter(([label]) => label !== MODEL_UNKNOWN_LABEL).sort((a, b) => b[1] - a[1]);
  const total = namedEntries.reduce((sum, [, count]) => sum + count, 0);
  const shown = namedEntries.slice(0, MODEL_PALETTE_SLOTS.length);
  const overflowCount = namedEntries.slice(MODEL_PALETTE_SLOTS.length).reduce((sum, [, count]) => sum + count, 0);
  // The donut still caps at 8 categorical slots, but the color for each label is
  // resolved through the shared registry (when supplied) so the same model keeps
  // one color across the donut, bars, history, and efficiency plots.
  const pick = (label, index) => (colorFor ? colorFor(label) : MODEL_PALETTE_SLOTS[index]);
  const segments = shown.map(([label, count], index) => ({ label, count, color: pick(label, index) }));
  if (overflowCount > 0) segments.push({ label: MODEL_OTHER_LABEL, count: overflowCount, color: colorFor ? colorFor(MODEL_OTHER_LABEL) : MODEL_OTHER_COLOR });
  return { segments, total };
}

// ---------------------------------------------------------------------------
// Shared model color identity + ordering
//
// Every analytics panel resolves a model's color through one label-keyed
// registry so a model's color is identical for the donut/pie fill, Token Usage
// bar fill, history line/fill/marker, legend swatch, and efficiency point.
// Colors are allocated deterministically and never reassigned: well-known
// labels take fixed base colors, and any additional label takes the next free
// deterministic hue (preferring a hash-chosen slot) which is then persisted.
// ---------------------------------------------------------------------------

// Fixed base colors for labels that must stay visually stable regardless of
// sort order or token totals. Mirrors the historical donut slot assignments for
// the canonical lanes so existing dashboards keep their colors.
export const MODEL_BASE_COLORS = {
  "Opus 4.8": MODEL_PALETTE_SLOTS[0],
  "GPT 5.6 Luna": MODEL_PALETTE_SLOTS[1],
  "GPT 5.6 Sol": MODEL_PALETTE_SLOTS[2],
  [MODEL_OTHER_LABEL]: MODEL_OTHER_COLOR,
  [MODEL_UNKNOWN_LABEL]: MODEL_OTHER_COLOR,
};

// Deterministic hue pool for labels without a fixed base color. Reserved colors
// already spoken for by MODEL_BASE_COLORS are removed so overflow labels never
// collide with a canonical lane's color.
const RESERVED_BASE_COLORS = new Set(Object.values(MODEL_BASE_COLORS));
export const MODEL_ALLOCATABLE_HUES = [...MODEL_PALETTE_SLOTS, ...MODEL_EXTRA_HUES]
  .filter((color) => !RESERVED_BASE_COLORS.has(color));

function hashLabel(label) {
  let hash = 5381; const text = String(label || "");
  for (let i = 0; i < text.length; i += 1) hash = (((hash << 5) + hash) + text.charCodeAt(i)) >>> 0;
  return hash;
}

/**
 * Allocate a deterministic, distinct color for `label` given the colors already
 * assigned. Returns the existing assignment when present; otherwise picks the
 * hash-preferred free hue (falling back to the first free hue, then the shared
 * Other color) and records it. `registry` is a plain object mutated in place so
 * the caller can persist it.
 */
export function ensureModelColor(registry, label) {
  const key = String(label || "");
  if (!key) return MODEL_OTHER_COLOR;
  if (MODEL_BASE_COLORS[key]) return MODEL_BASE_COLORS[key];
  if (registry[key]) return registry[key];
  const used = new Set([...Object.values(registry), ...RESERVED_BASE_COLORS]);
  const free = MODEL_ALLOCATABLE_HUES.filter((color) => !used.has(color));
  let color;
  if (free.length) {
    const preferred = free[hashLabel(key) % free.length];
    color = preferred;
  } else {
    // Pool exhausted: fall back to a deterministic hue from the full set so the
    // label still gets a stable color (may repeat once every color is in use).
    color = MODEL_ALLOCATABLE_HUES[hashLabel(key) % MODEL_ALLOCATABLE_HUES.length] || MODEL_OTHER_COLOR;
  }
  registry[key] = color;
  return color;
}

/**
 * Deterministic display order shared by every panel: recognized labels sorted
 * by descending token/task weight, then alphabetically as a stable tiebreak,
 * with the synthetic Other/Unknown buckets pinned last. `weights` is a
 * Map<label, number>; labels absent from it sort after weighted ones.
 */
export function orderModelLabels(labels, weights = new Map()) {
  const seen = new Set();
  const unique = [];
  for (const raw of labels) {
    const label = String(raw || "");
    if (!label || seen.has(label)) continue;
    seen.add(label); unique.push(label);
  }
  const rank = (label) => (label === MODEL_OTHER_LABEL || label === MODEL_UNKNOWN_LABEL ? 1 : 0);
  return unique.sort((a, b) => {
    const ra = rank(a); const rb = rank(b);
    if (ra !== rb) return ra - rb;
    const wa = weights.get(a) || 0; const wb = weights.get(b) || 0;
    if (wa !== wb) return wb - wa;
    return a.localeCompare(b);
  });
}

export function fmtTokens(value) {
  const count = Number(value) || 0;
  if (count >= 1_000_000_000) return `${(count / 1_000_000_000).toFixed(1)}B`;
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M`;
  if (count >= 10_000) return `${Math.round(count / 1000)}k`;
  if (count >= 1000) return `${(count / 1000).toFixed(1)}k`;
  return String(count);
}

export function computeTokenUsageStats(rows) {
  const families = new Map();
  let totalTokens = 0;
  let taskCount = 0;
  filterRowsByModel(rows).forEach((row) => {
    const tokens = Number(row.total_tokens) || 0;
    const label = modelIdentity(row, row.model_descriptor);
    const entry = families.get(label) || { label, tasks: 0, tokens: 0 };
    entry.tasks += 1; entry.tokens += tokens; families.set(label, entry);
    totalTokens += tokens; taskCount += 1;
  });
  const stats = Array.from(families.values()).map((entry) => ({ ...entry, avg: entry.tasks ? entry.tokens / entry.tasks : 0 })).sort((a, b) => b.tokens - a.tokens);
  return { stats, totalTokens, taskCount, overallAvg: taskCount ? totalTokens / taskCount : 0 };
}

export function oscilloscopeModelLabel(row) {
  // History lanes now key on the same effective model identity as every other
  // panel (canonical label first, then the legacy descriptor fallback), so a
  // registered canonical variant gets its own exact lane instead of collapsing
  // into a fixed broad alias.
  return modelIdentity(row, row?.model_descriptor);
}

export function tokenEfficiencyBadge(avg, overallAvg) {
  if (!overallAvg) return "";
  const ratio = avg / overallAvg;
  const cls = ratio <= 0.85 ? "good" : ratio >= 1.15 ? "high" : "";
  return `<span class="token-eff ${cls}" title="Average tokens per reported task for this model vs the overall average (lower is leaner)">${ratio.toFixed(2)}× avg</span>`;
}

export const clamp = (value, lo, hi) => Math.min(Math.max(value, lo), hi);
export const oscTimescale = (key) => OSC_TIMESCALES.find((entry) => entry.key === key)
  || OSC_TIMESCALES.find((entry) => entry.key === OSC_DEFAULT_TIMESCALE) || OSC_TIMESCALES[0];

export function medianOf(values) {
  const sorted = values.slice().sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/**
 * Build one exact-model history lane per recognized token-usage label.
 *
 * `colorFor(label)` resolves the shared registry color. `enabledLabels`, when a
 * Set, filters lanes to the labels whose history is switched on (a null value
 * keeps every recognized lane, preserving the pre-toggle behavior). Lanes are
 * ordered by the shared model ordering (descending token weight, then
 * alphabetical) so their order matches the donut/legend rather than tracking the
 * order toggles were clicked. `Unknown` rows are always excluded.
 */
export function buildOscilloscopeSeries(rows, colorFor = (label) => MODEL_BASE_COLORS[label] || MODEL_OTHER_COLOR, enabledLabels = null) {
  const rowList = asList(rows);
  if (!rowList.length) return [];
  const series = new Map();
  const weights = new Map();
  rowList.forEach((row) => {
    const t = Date.parse(row.ts); const tokens = Number(row.total_tokens) || 0;
    if (!Number.isFinite(t) || tokens <= 0) return;
    const label = oscilloscopeModelLabel(row);
    if (label === MODEL_UNKNOWN_LABEL) return;
    weights.set(label, (weights.get(label) || 0) + tokens);
    const entry = series.get(label) || { label, color: colorFor(label), points: [] };
    entry.points.push({ t, tokens, task_key: row.task_key || "" }); series.set(label, entry);
  });
  const enabled = enabledLabels instanceof Set ? enabledLabels : null;
  const ordered = orderModelLabels([...series.keys()], weights)
    .filter((label) => !enabled || enabled.has(label))
    .map((label) => { const entry = series.get(label); entry.color = colorFor(label); entry.points.sort((a, b) => a.t - b.t); return entry; });
  return ordered;
}

export function resampleOscSeries(series, windowStart, windowEnd, windowMs) {
  const span = windowEnd - windowStart;
  if (!(span > 0)) return series;
  const binMs = span / OSC_BIN_COUNT; const zeroGapMs = (Number.isFinite(windowMs) ? windowMs : span) / 4;
  return series.map((entry) => {
    const bins = new Array(OSC_BIN_COUNT);
    entry.points.forEach((p) => { const idx = Math.floor((p.t - windowStart) / binMs); if (idx >= 0 && idx < OSC_BIN_COUNT) (bins[idx] || (bins[idx] = [])).push(p); });
    const samples = []; let lastDataT = null; let zeroHeld = false;
    for (let i = 0; i < OSC_BIN_COUNT; i += 1) {
      const center = windowStart + (i + 0.5) * binMs; const bucket = bins[i];
      if (bucket?.length) {
        const value = medianOf(bucket.map((p) => p.tokens)); let rep = bucket[0]; let best = Infinity;
        bucket.forEach((p) => { const d = Math.abs(p.t - center); if (d < best) { best = d; rep = p; } });
        samples.push({ t: center, tokens: value, task_key: rep.task_key || "" }); lastDataT = center; zeroHeld = false;
      } else if (lastDataT !== null) { if (!zeroHeld && center - lastDataT >= zeroGapMs) zeroHeld = true; if (zeroHeld) samples.push({ t: center, tokens: 0, task_key: "", synthetic: true }); }
    }
    return { ...entry, points: samples, rawCount: entry.points.length };
  });
}

export function oscTruncate(text, max) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  return value.length <= max ? value : `${value.slice(0, Math.max(0, max - 1)).trimEnd()}…`;
}

export function buildTaskMeta(tasks, todos) {
  const meta = new Map();
  const get = (key) => { let entry = meta.get(key); if (!entry) { entry = { summary: "", title: "" }; meta.set(key, entry); } return entry; };
  asList(tasks).forEach((task) => { const key = String(task.task_key || "").toLowerCase(); if (!key) return; const entry = get(key); if (task.summary) entry.summary = task.summary; if (task.task_title && task.task_title !== task.task_key) entry.title = task.task_title; });
  asList(todos).forEach((todo) => { if (!todo.title) return; [todo.source_task_key, todo.todo_key].forEach((raw) => { const key = String(raw || "").toLowerCase(); if (key) get(key).title = todo.title; }); });
  return meta;
}

export function oscSmoothPath(points) {
  if (!points.length) return "";
  if (points.length === 1) { const p = points[0]; return `M ${(p.x - 7).toFixed(2)} ${p.y.toFixed(2)} L ${(p.x + 7).toFixed(2)} ${p.y.toFixed(2)}`; }
  let d = `M ${points[0].x.toFixed(2)} ${points[0].y.toFixed(2)}`;
  for (let i = 0; i < points.length - 1; i += 1) {
    const p0 = points[i - 1] || points[i]; const p1 = points[i]; const p2 = points[i + 1]; const p3 = points[i + 2] || p2;
    const cp1x = Math.min(Math.max(p1.x + (p2.x - p0.x) / 6, p1.x), p2.x); const cp1y = p1.y + (p2.y - p0.y) / 6;
    const cp2x = Math.min(Math.max(p2.x - (p3.x - p1.x) / 6, cp1x), p2.x); const cp2y = p2.y - (p3.y - p1.y) / 6;
    d += ` C ${cp1x.toFixed(2)} ${cp1y.toFixed(2)} ${cp2x.toFixed(2)} ${cp2y.toFixed(2)} ${p2.x.toFixed(2)} ${p2.y.toFixed(2)}`;
  }
  return d;
}

export const EFFICIENCY_JITTER = 0.16;
export function efficiencyJitterOffset(key) {
  let hash = 5381; const text = String(key || "");
  for (let i = 0; i < text.length; i += 1) hash = (((hash << 5) + hash) + text.charCodeAt(i)) >>> 0;
  return (((hash % 1000) / 999) * 2 - 1) * EFFICIENCY_JITTER;
}

/**
 * Group efficiency samples by exact model identity, coloring each group through
 * the shared registry (`colorFor`) and ordering groups by the shared model
 * ordering. `Unknown` samples are excluded. Effort variants stay distinct.
 */
export function buildEfficiencyGroups(samples, colorFor = (label) => MODEL_BASE_COLORS[label] || MODEL_OTHER_COLOR) {
  const grouped = new Map();
  const weights = new Map();
  asList(samples).forEach((sample) => {
    const label = modelIdentity(sample, sample.model_descriptor);
    if (label === MODEL_UNKNOWN_LABEL) return;
    weights.set(label, (weights.get(label) || 0) + (Number(sample.total_tokens) || 0));
    const entry = grouped.get(label) || { label, color: colorFor(label), samples: [] };
    entry.samples.push(sample); grouped.set(label, entry);
  });
  return orderModelLabels([...grouped.keys()], weights)
    .map((label) => { const entry = grouped.get(label); entry.color = colorFor(label); return entry; });
}

export function computeEfficiencyKpis(groups) {
  return groups.map((group) => {
    const perUnit = group.samples.map((sample) => Number(sample.total_tokens) / sample.actual_complexity_num);
    const pairs = group.samples.filter((sample) => sample.planned_complexity_num != null);
    const bias = pairs.length ? pairs.reduce((sum, sample) => sum + (sample.actual_complexity_num - sample.planned_complexity_num), 0) / pairs.length : null;
    return { label: group.label, color: group.color, sampleCount: group.samples.length, medianPerUnit: perUnit.length ? medianOf(perUnit) : null, calibrationBias: bias, pairCount: pairs.length };
  }).filter((kpi) => kpi.sampleCount >= EFFICIENCY_MIN_SAMPLES && kpi.medianPerUnit != null);
}
