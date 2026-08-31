import { $, asList, escapeHtml, setEmpty } from "../dom.js";
import {
  buildComplexityDistribution,
  buildModelUsageSegments,
  COMPLEXITY_BUCKETS,
  COMPLEXITY_SOURCES,
  complexityBucketConfig,
  complexitySourceConfig,
  complexityEmptyMessage,
  computeModelUsageCounts,
  filterRowsByComplexity,
  rowMatchesComplexityBucket,
} from "../analytics/shared.js";

export class ModelUsagePanel {
  constructor({ state, documentRef = globalThis.document, onFilterChange, onHighlight } = {}) {
    this.state = state;
    this.document = documentRef;
    this.onFilterChange = onFilterChange;
    this.onHighlight = onHighlight;
  }

  filterRows(rows) { return filterRowsByComplexity(rows, this.state); }

  selectedComplexityLabel() {
    const bucket = complexityBucketConfig(this.state.complexityBucket);
    if (!bucket) return "";
    const source = complexitySourceConfig(this.state.complexitySource);
    return source.key === "compare" ? `${bucket.label} planned or actual complexity` : `${bucket.label} ${source.label.toLowerCase()} complexity`;
  }

  renderComplexityDistribution(tasks) {
    const distribution = buildComplexityDistribution(tasks, this.state);
    this.state.complexitySource = distribution.source;
    const countEl = $("complexityDistributionCount", this.document);
    const controls = $("complexitySourceControls", this.document);
    const bars = $("complexityDistributionBars", this.document);
    const empty = $("complexityDistributionEmpty", this.document);
    if (!countEl || !controls || !bars || !empty) return;
    controls.innerHTML = COMPLEXITY_SOURCES.map((source) => {
      const active = source.key === distribution.source;
      return `<button type="button" class="complexity-source-btn${active ? " is-active" : ""}" data-complexity-source="${escapeHtml(source.key)}" aria-pressed="${active}">${escapeHtml(source.label)}</button>`;
    }).join("");
    if (!distribution.hasData) {
      this.state.complexityBucket = "";
      countEl.textContent = ""; bars.innerHTML = "";
      empty.textContent = complexityEmptyMessage(distribution.source, distribution.rows);
      setEmpty("complexityDistributionEmpty", true, this.document);
      return;
    }
    const selectedLabel = this.selectedComplexityLabel();
    const filteredCount = this.state.complexityBucket
      ? distribution.dataRows.filter((row) => rowMatchesComplexityBucket(row, distribution.source, this.state.complexityBucket)).length
      : distribution.dataRows.length;
    countEl.textContent = this.state.complexityBucket
      ? `${filteredCount} filtered by ${selectedLabel}`
      : distribution.source === "compare" ? `${distribution.dataRows.length} paired tasks` : `${distribution.dataRows.length} recent tasks`;
    setEmpty("complexityDistributionEmpty", false, this.document);
    if (distribution.source === "compare") {
      const maxCount = Math.max(...COMPLEXITY_BUCKETS.flatMap((bucket) => [distribution.plannedCounts.get(bucket.key) || 0, distribution.actualCounts.get(bucket.key) || 0]), 1);
      bars.innerHTML = COMPLEXITY_BUCKETS.map((bucket) => {
        const planned = distribution.plannedCounts.get(bucket.key) || 0; const actual = distribution.actualCounts.get(bucket.key) || 0;
        const plannedPct = distribution.dataRows.length ? (planned / distribution.dataRows.length) * 100 : 0;
        const actualPct = distribution.dataRows.length ? (actual / distribution.dataRows.length) * 100 : 0;
        const plannedWidth = Math.max((planned / maxCount) * 100, planned ? 3 : 0).toFixed(1); const actualWidth = Math.max((actual / maxCount) * 100, actual ? 3 : 0).toFixed(1);
        const selected = this.state.complexityBucket === bucket.key;
        return `<button type="button" class="complexity-bar-row compare${selected ? " is-selected" : ""}" data-complexity-bucket="${escapeHtml(bucket.key)}" aria-pressed="${selected}" title="Filter to tasks where planned or actual complexity is ${escapeHtml(bucket.label)}"><span class="complexity-bar-label">${escapeHtml(bucket.label)}</span><span class="complexity-pair-bars"><span class="complexity-pair-track" title="Planned ${planned} (${plannedPct.toFixed(0)}%)"><span class="complexity-bar-fill predicted" style="width:${plannedWidth}%"></span></span><span class="complexity-pair-track" title="Actual ${actual} (${actualPct.toFixed(0)}%)"><span class="complexity-bar-fill actual" style="width:${actualWidth}%"></span></span></span><span class="complexity-bar-count"><span class="predicted-key">P</span> ${planned} / <span class="actual-key">A</span> ${actual}</span></button>`;
      }).join("");
      return;
    }
    const maxCount = Math.max(...COMPLEXITY_BUCKETS.map((bucket) => distribution.counts.get(bucket.key) || 0), 1);
    bars.innerHTML = COMPLEXITY_BUCKETS.map((bucket) => {
      const count = distribution.counts.get(bucket.key) || 0; const pct = distribution.dataRows.length ? (count / distribution.dataRows.length) * 100 : 0;
      const width = Math.max((count / maxCount) * 100, count ? 3 : 0).toFixed(1); const selected = this.state.complexityBucket === bucket.key;
      return `<button type="button" class="complexity-bar-row${selected ? " is-selected" : ""}" data-complexity-bucket="${escapeHtml(bucket.key)}" aria-pressed="${selected}" title="Filter to ${escapeHtml(bucket.label)} ${escapeHtml(complexitySourceConfig(distribution.source).label.toLowerCase())} complexity"><span class="complexity-bar-label">${escapeHtml(bucket.label)}</span><span class="complexity-bar-track"><span class="complexity-bar-fill ${distribution.source === "actual" ? "actual" : "predicted"}" style="width:${width}%"></span></span><span class="complexity-bar-count">${count} <span class="muted">${pct.toFixed(0)}%</span></span></button>`;
    }).join("");
  }

  render(tasks) {
    const taskRows = asList(tasks);
    const { segments, total } = buildModelUsageSegments(computeModelUsageCounts(taskRows), (label) => this.state.colorFor(label));
    this.state.modelSegments = segments;
    const count = $("modelUsageCount", this.document); const empty = $("modelUsageEmpty", this.document);
    if (count) count.textContent = total ? `${total} recorded` : "";
    if (empty) empty.textContent = this.state.complexityBucket ? "No recognized model usage matches the complexity filter." : taskRows.length ? "No recognized model descriptors in recent task state." : "No agent task state recorded yet.";
    setEmpty("modelUsageEmpty", total === 0, this.document);
    const chart = $("modelUsageChart", this.document); const legend = $("modelUsageLegend", this.document);
    if (!chart || !legend) return;
    if (total === 0) { chart.innerHTML = ""; legend.innerHTML = ""; return; }
    const gap = segments.length > 1 ? 0.6 : 0; let cumulative = 0; const ringRadius = 15.91549430918954;
    const rings = segments.map((segment) => { const pct = (segment.count / total) * 100; const visualPct = Math.max(pct - gap, 0); const dashOffset = 25 - cumulative; cumulative += pct; return `<circle class="donut-segment" data-model-label="${escapeHtml(segment.label)}" cx="21" cy="21" r="${ringRadius}" fill="transparent" stroke="${segment.color}" stroke-width="6" stroke-linecap="round" stroke-dasharray="${visualPct} ${100 - visualPct}" stroke-dashoffset="${dashOffset}"><title>${escapeHtml(segment.label)}: ${segment.count} (${pct.toFixed(1)}%)</title></circle>`; }).join("");
    chart.innerHTML = `<svg viewBox="0 0 42 42" class="donut" role="img" aria-label="Agent model usage donut chart"><circle class="donut-ring" cx="21" cy="21" r="${ringRadius}" fill="transparent" stroke-width="6"></circle>${rings}<text x="21" y="19.5" class="donut-total-value" text-anchor="middle">${total}</text><text x="21" y="26" class="donut-total-label" text-anchor="middle">tasks</text></svg>`;
    legend.innerHTML = segments.map((segment) => `<div class="donut-legend-item" data-model-label="${escapeHtml(segment.label)}" role="button" tabindex="0"><span class="donut-swatch" style="background:${segment.color}"></span><span class="donut-legend-label">${escapeHtml(segment.label)}</span><span class="donut-legend-count muted">${segment.count} (${((segment.count / total) * 100).toFixed(1)}%)</span></div>`).join("");
    this.onHighlight?.(this.state.osc.hoverModel);
  }

  renderAll(tasks) { this.renderComplexityDistribution(tasks); this.render(this.filterRows(tasks)); }

  setComplexitySource(source) {
    if (!COMPLEXITY_SOURCES.some((item) => item.key === source)) return;
    if (this.state.complexitySource === source && !this.state.complexityBucket) return;
    this.state.complexitySource = source; this.state.complexityBucket = ""; this.state.osc.panMs = 0; this.onFilterChange?.();
  }

  selectComplexityBucket(bucket) {
    if (!complexityBucketConfig(bucket)) return;
    this.state.complexityBucket = this.state.complexityBucket === bucket ? "" : bucket; this.state.osc.panMs = 0; this.onFilterChange?.();
  }
}
