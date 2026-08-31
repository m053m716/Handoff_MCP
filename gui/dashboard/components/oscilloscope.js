import { $, asList, escapeHtml, fmtTime, setEmpty } from "../dom.js";
import {
  OSC_LOG_FLOOR, OSC_TIMESCALES, MODEL_UNKNOWN_LABEL,
  buildOscilloscopeSeries, buildTaskMeta, modelIdentity,
  clamp, fmtTokens, oscSmoothPath, oscTimescale, oscTruncate, resampleOscSeries,
} from "../analytics/shared.js";

const ALL_HISTORY_OFF_MESSAGE = "All history lanes are hidden. Turn on a model's history toggle in Token Usage to plot it here.";

function containerWidth(documentRef) {
  const element = $("oscilloscopeScroll", documentRef); const width = element ? element.clientWidth : 0;
  return width > 40 ? width : 640;
}
function fmtTick(ms, spanMs) {
  const date = new Date(ms); if (Number.isNaN(date.getTime())) return "";
  return spanMs <= 36 * 3600e3 ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : date.toLocaleDateString([], { month: "numeric", day: "numeric" });
}

export class OscilloscopePanel {
  constructor({ state, documentRef = globalThis.document, windowRef = globalThis.window, onSelectionChange } = {}) {
    this.state = state; this.document = documentRef; this.window = windowRef; this.onSelectionChange = onSelectionChange;
  }

  renderControls(hasData) {
    const controls = $("oscilloscopeControls", this.document); if (!controls) return;
    const buttons = OSC_TIMESCALES.map((entry) => { const active = entry.key === this.state.osc.timescale; return `<button type="button" class="osc-scale-btn${active ? " is-active" : ""}" data-osc-timescale="${entry.key}" aria-pressed="${active}">${escapeHtml(entry.label)}</button>`; }).join("");
    const selected = this.state.osc.selectedModel;
    const chip = selected ? `<span class="osc-filter-chip" data-model-label="${escapeHtml(selected)}">Isolating <strong>${escapeHtml(selected)}</strong><button type="button" class="osc-filter-clear" data-osc-clear aria-label="Show all models">×</button></span>` : "";
    controls.innerHTML = `<div class="osc-scale-group" role="group" aria-label="Oscilloscope time window">${buttons}</div>${chip}`;
    controls.hidden = !hasData;
  }

  // Recognized (non-Unknown) model labels present in the current usage payload,
  // regardless of the enabled set — this is the universe the toggles act over.
  recognizedLabels(rows) {
    const labels = new Set();
    asList(rows).forEach((row) => {
      if ((Number(row.total_tokens) || 0) <= 0) return;
      const label = modelIdentity(row, row.model_descriptor);
      if (label && label !== MODEL_UNKNOWN_LABEL) labels.add(label);
    });
    return labels;
  }

  render(rows, tasks) {
    const chart = $("oscilloscopeChart", this.document); if (!chart) return;
    const empty = $("oscilloscopeEmpty", this.document);
    const recognized = this.recognizedLabels(rows);
    const enabled = this.state.enabledHistoryLabels(recognized);
    // Specific all-off instruction takes precedence over the generic empty state.
    const allOff = recognized.size > 0 && enabled.size === 0;
    if (empty) empty.textContent = allOff ? ALL_HISTORY_OFF_MESSAGE
      : this.state.complexityBucket ? "No token usage matches the complexity filter." : "No token usage submitted yet.";
    // Drop a transient isolation focus whose lane is no longer enabled so it
    // cannot silently re-enable a disabled model.
    if (this.state.osc.selectedModel && !enabled.has(this.state.osc.selectedModel)) this.state.osc.selectedModel = null;
    let series = buildOscilloscopeSeries(rows, (label) => this.state.colorFor(label), enabled);
    const taskMeta = buildTaskMeta(tasks, this.state.latest?.open_todos || []);
    const scale = oscTimescale(this.state.osc.timescale); const now = Date.now();
    const allTimes = series.flatMap((entry) => entry.points.map((point) => point.t));
    const dataMin = allTimes.length ? Math.min(...allTimes) : now - scale.ms; const dataMax = allTimes.length ? Math.max(...allTimes) : now;
    let windowStart; let windowEnd; let pannable = false; let panMin = 0; let panMax = 0;
    if (scale.ms === Infinity) { windowStart = dataMin; windowEnd = Math.max(now, dataMax); this.state.osc.panMs = 0; }
    else {
      const defaultStart = now - scale.ms;
      if (allTimes.length && dataMin > defaultStart) { windowStart = dataMin; windowEnd = dataMin + scale.ms; this.state.osc.panMs = 0; }
      else { const maxStart = defaultStart; const minStart = allTimes.length ? Math.min(dataMin, maxStart) : maxStart; panMin = minStart - defaultStart; panMax = 0; this.state.osc.panMs = clamp(this.state.osc.panMs || 0, panMin, panMax); windowStart = defaultStart + this.state.osc.panMs; windowEnd = windowStart + scale.ms; pannable = minStart < maxStart; }
    }
    if (windowEnd <= windowStart) windowEnd = windowStart + 3600e3;
    // Every dynamic lane is built from rows with tokens, so a lane with no
    // resampled points inside the window still draws a flat baseline (rather
    // than vanishing) so an enabled-but-idle model stays visible.
    series = resampleOscSeries(series, windowStart, windowEnd, scale.ms).map((entry) => entry.points.length ? entry : { ...entry, points: [{ t: windowStart, tokens: 0, task_key: "", synthetic: true }, { t: windowEnd, tokens: 0, task_key: "", synthetic: true }] });
    const selected = this.state.osc.selectedModel;
    if (selected) { const only = series.filter((entry) => entry.label === selected); if (only.length) series = only; }
    const hasData = series.length > 0; this.renderControls((this.state.modelSegments || []).length > 0); setEmpty("oscilloscopeEmpty", !hasData, this.document);
    if (!hasData) { chart.innerHTML = ""; return; }
    const positiveTokens = series.flatMap((entry) => entry.points.filter((point) => point.tokens > 0).map((point) => Math.max(point.tokens, OSC_LOG_FLOOR)));
    const logMin = positiveTokens.length ? Math.log(Math.min(...positiveTokens)) : 0; const logMax = positiveTokens.length ? Math.log(Math.max(...positiveTokens)) : 0; const logSpan = logMax - logMin;
    const single = Boolean(selected) && series.length === 1; const laneH = single ? 132 : 52; const lanePadTop = 12; const lanePadBottom = 12; const axisH = 20; const padX = 10; const spanMs = windowEnd - windowStart;
    const contentW = containerWidth(this.document); const plotW = Math.max(contentW - padX * 2, 1); const height = series.length * laneH + axisH;
    this.state.osc.pxPerMs = plotW / spanMs; this.state.osc.pannable = pannable; this.state.osc.panMin = panMin; this.state.osc.panMax = panMax;
    const xOf = (t) => padX + ((t - windowStart) / spanMs) * plotW;
    const normOf = (tokens) => { if (!(tokens > 0)) return 0; if (logSpan <= 0) return 0.5; return clamp((Math.log(Math.max(tokens, OSC_LOG_FLOOR)) - logMin) / logSpan, 0, 1); };
    const lanes = series.map((entry, index) => {
      const laneTop = index * laneH; const innerTop = laneTop + lanePadTop; const innerH = laneH - lanePadTop - lanePadBottom; const baseY = laneTop + laneH - lanePadBottom;
      const points = entry.points.map((point) => ({ x: xOf(point.t), y: innerTop + (1 - normOf(point.tokens)) * innerH, point })); const path = oscSmoothPath(points);
      const markers = points.filter((point) => !point.point.synthetic).map((point) => { const info = taskMeta.get((point.point.task_key || "").toLowerCase()); const lines = [`${entry.label} · ${fmtTokens(point.point.tokens)} tokens`, fmtTime(new Date(point.point.t).toISOString())]; if (point.point.task_key) lines.push(point.point.task_key); if (info?.title) lines.push(oscTruncate(info.title, 90)); if (info?.summary) lines.push(oscTruncate(info.summary, 220)); return `<circle class="osc-point" cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="3" fill="${entry.color}"><title>${escapeHtml(lines.filter(Boolean).join("\n"))}</title></circle>`; }).join("");
      const realPoints = entry.points.filter((point) => !point.synthetic); const latest = realPoints[realPoints.length - 1] || entry.points[entry.points.length - 1]; const taskCount = entry.rawCount != null ? entry.rawCount : entry.points.length; const alt = index % 2 === 1 ? " osc-lane-bg-alt" : "";
      return `<g class="osc-lane" data-model-label="${escapeHtml(entry.label)}"><rect class="osc-lane-bg${alt}" x="0" y="${laneTop}" width="${contentW}" height="${laneH}"></rect><line class="osc-lane-base" x1="${padX}" y1="${baseY.toFixed(2)}" x2="${(contentW - padX).toFixed(2)}" y2="${baseY.toFixed(2)}"></line><path class="osc-line" d="${path}" fill="none" stroke="${entry.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></path>${markers}<title>${escapeHtml(entry.label)} · ${taskCount} task${taskCount === 1 ? "" : "s"} · latest ${fmtTokens(latest.tokens)} tokens</title></g>`;
    }).join("");
    const ticks = Array.from({ length: 5 }, (_, i) => { const t = windowStart + (spanMs * i) / 4; const x = xOf(t); const anchor = i === 0 ? "start" : i === 4 ? "end" : "middle"; return `<line class="osc-grid" x1="${x.toFixed(2)}" y1="0" x2="${x.toFixed(2)}" y2="${series.length * laneH}"></line><text class="osc-tick" x="${x.toFixed(2)}" y="${(height - 6).toFixed(2)}" text-anchor="${anchor}">${escapeHtml(fmtTick(t, spanMs))}</text>`; }).join("");
    chart.innerHTML = `<svg class="osc-svg" width="${contentW}" height="${height}" viewBox="0 0 ${contentW} ${height}" role="img" aria-label="Token usage over time by model">${ticks}${lanes}</svg>`;
    const scroll = $("oscilloscopeScroll", this.document); if (scroll) scroll.classList.toggle("osc-pannable", pannable);
    const labels = $("oscilloscopeLabels", this.document);
    if (labels) labels.innerHTML = series.map((entry) => { const realPoints = entry.points.filter((point) => !point.synthetic); const latest = realPoints[realPoints.length - 1] || entry.points[entry.points.length - 1]; const count = entry.rawCount != null ? entry.rawCount : entry.points.length; return `<div class="osc-label-row" data-model-label="${escapeHtml(entry.label)}" role="button" tabindex="0" style="height:${laneH}px"><span class="osc-label-swatch" style="background:${entry.color}"></span><span class="osc-label-text"><span class="osc-label-name">${escapeHtml(entry.label)}</span><span class="osc-label-meta muted">${fmtTokens(latest.tokens)} · ${count}×</span></span></div>`; }).join("") + `<div class="osc-label-axis" style="height:${axisH}px"></div>`;
    this.applyModelHighlight(this.state.osc.hoverModel);
  }

  rerender() { const data = this.state.latest; if (data) this.render(this.filterRows(data.token_usage || []), this.filterRows(data.recent_tasks || [])); }
  filterRows(rows) { return this.onFilterRows ? this.onFilterRows(rows) : asList(rows); }
  setFilterRows(fn) { this.onFilterRows = fn; }
  applyModelHighlight(label) {
    this.state.osc.hoverModel = label || null; const section = $("modelUsageSection", this.document); if (!section) return;
    section.querySelectorAll("[data-model-label]").forEach((element) => { const matches = element.getAttribute("data-model-label") === this.state.osc.hoverModel; element.classList.toggle("is-active", Boolean(this.state.osc.hoverModel) && matches); element.classList.toggle("is-dim", Boolean(this.state.osc.hoverModel) && !matches); });
  }
  toggleModel(label) { if (!label) return; this.state.osc.selectedModel = this.state.osc.selectedModel === label ? null : label; this.state.osc.panMs = 0; this.onSelectionChange?.(); }
  clearSelection() { this.state.osc.selectedModel = null; this.state.osc.panMs = 0; this.onSelectionChange?.(); }
  setTimescale(key) { if (!OSC_TIMESCALES.some((entry) => entry.key === key)) return; this.state.osc.timescale = key; this.state.osc.panMs = 0; this.rerender(); }
  schedulePanRender() { if (this.state.osc.panRaf) return; this.state.osc.panRaf = this.window.requestAnimationFrame(() => { this.state.osc.panRaf = 0; this.rerender(); }); }
  handlePanMove(event) { if (!this.state.osc.dragging) return; const deltaPx = event.clientX - this.state.osc.dragStartX; if (Math.abs(deltaPx) > 3) this.state.osc.dragged = true; const deltaMs = this.state.osc.pxPerMs ? -deltaPx / this.state.osc.pxPerMs : 0; this.state.osc.panMs = clamp(this.state.osc.dragStartPan + deltaMs, this.state.osc.panMin, this.state.osc.panMax); this.schedulePanRender(); }
  endPan() { if (!this.state.osc.dragging) return; this.state.osc.dragging = false; this.state.osc.suppressClick = this.state.osc.dragged; $("oscilloscopeScroll", this.document)?.classList.remove("is-panning"); this.document.removeEventListener("mousemove", this._moveHandler); this.document.removeEventListener("mouseup", this._upHandler); }
  handlePanStart(event) { if (event.button !== 0 || !this.state.osc.pannable) return; if (event.target.closest?.("[data-osc-timescale], [data-osc-clear]")) return; this.state.osc.dragging = true; this.state.osc.dragged = false; this.state.osc.dragStartX = event.clientX; this.state.osc.dragStartPan = this.state.osc.panMs || 0; $("oscilloscopeScroll", this.document)?.classList.add("is-panning"); this._moveHandler = (move) => this.handlePanMove(move); this._upHandler = () => this.endPan(); this.document.addEventListener("mousemove", this._moveHandler); this.document.addEventListener("mouseup", this._upHandler); event.preventDefault(); }
}
