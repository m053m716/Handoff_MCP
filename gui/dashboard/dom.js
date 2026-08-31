export const routeMarkers = ["/dashboard", "/mcp-gui/", "/scripts/mcp-gui/", "/scripts/mcp_gui/"];

export function inferBasePath(pathname = globalThis.window?.location?.pathname || "/") {
  const path = pathname || "/";
  for (const marker of routeMarkers) {
    const index = path.indexOf(marker);
    if (index >= 0) return path.slice(0, index);
  }
  return "";
}

export function appPath(path, basePath = inferBasePath()) {
  const suffix = String(path || "");
  if (!basePath) return suffix || "/";
  return `${basePath}${suffix.startsWith("/") ? suffix : `/${suffix}`}`;
}

export const basePath = inferBasePath();
export const $ = (id, documentRef = globalThis.document) => documentRef?.getElementById(id);
export const asList = (value) => Array.isArray(value) ? value.filter(Boolean) : [];

export const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
}[ch]));

export const fmtTime = (value) => {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
};

export const fmtDuration = (seconds) => {
  if (seconds === null || seconds === undefined) return "";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
};

export const statusBadge = (status) => `<span class="badge ${escapeHtml(status)}">${escapeHtml(status)}</span>`;
export const todoDisplayStatus = (todo) => {
  const status = todo.display_status || todo.linked_task_status || todo.status || "";
  return status === "in_progress" ? "active" : status;
};
export const setEmpty = (id, show, documentRef = globalThis.document) => {
  const element = $(id, documentRef);
  if (element) element.hidden = !show;
};

export function firstElement(selectors, documentRef = globalThis.document) {
  for (const selector of selectors) {
    const element = documentRef?.querySelector(selector);
    if (element) return element;
  }
  return null;
}

export function jumpToElement(element, options = {}) {
  if (!element) return;
  element.scrollIntoView?.({ behavior: "smooth", block: "start" });
  element.classList.remove("jump-highlight");
  void element.offsetWidth;
  element.classList.add("jump-highlight");
  if (options.focus && typeof element.focus === "function") {
    try { element.focus({ preventScroll: true }); } catch { element.focus(); }
  }
}

export function jumpToHud(documentRef = globalThis.document) {
  jumpToElement(firstElement(["#dashboardHud", "#metrics", ".topbar"], documentRef), { focus: true });
}

export function jumpFromMetric(target, documentRef = globalThis.document) {
  const targets = {
    active: ["#activeTasksSection"],
    "open-todos": ["#openTodosSection"],
    stale: ['#openTodos tr[data-todo-status="stale"]', '#openTodos tr[data-todo-stale="true"]', '#recentTasks tr[data-stale="true"]', "#docsDriftWarnings:not([hidden])", "#recentTasksSection"],
    done: ['#recentTasks tr[data-status="done"]', "#recentTasksSection"],
    blocked: ['#openTodos tr[data-todo-status="blocked"]', '#activeTasks tr[data-status="blocked"]', '#recentTasks tr[data-status="blocked"]', "#openTodosSection"],
    docs: ["#docsSection"],
  };
  jumpToElement(firstElement(targets[target] || [], documentRef));
}

export function fallbackCopyText(text, documentRef = globalThis.document) {
  const textarea = documentRef.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  documentRef.body.appendChild(textarea);
  textarea.select();
  const copied = documentRef.execCommand("copy");
  documentRef.body.removeChild(textarea);
  if (!copied) throw new Error("Clipboard copy failed");
}

const copyButtonTimers = new WeakMap();

export function showCopyButtonFeedback(button, windowRef = globalThis.window, timers = copyButtonTimers) {
  if (!button) return;
  const previous = timers.get(button);
  if (previous) previous.forEach((timer) => windowRef.clearTimeout(timer));
  if (!button.dataset.copyOriginalText) {
    button.dataset.copyOriginalText = button.textContent;
    const width = button.getBoundingClientRect().width;
    if (width) button.style.minWidth = `${Math.ceil(width)}px`;
  }
  button.classList.add("copy-feedback-fading");
  const toCopied = windowRef.setTimeout(() => {
    button.textContent = "Copied!";
    button.classList.add("copy-confirmed");
    button.classList.remove("copy-feedback-fading");
  }, 90);
  const fadeCopied = windowRef.setTimeout(() => button.classList.add("copy-feedback-fading"), 950);
  const restoreOriginal = windowRef.setTimeout(() => {
    button.textContent = button.dataset.copyOriginalText || button.textContent;
    button.classList.remove("copy-confirmed");
  }, 1180);
  const fadeOriginalIn = windowRef.setTimeout(() => button.classList.remove("copy-feedback-fading"), 1210);
  const cleanup = windowRef.setTimeout(() => {
    button.style.minWidth = "";
    delete button.dataset.copyOriginalText;
    timers.delete(button);
  }, 1500);
  timers.set(button, [toCopied, fadeCopied, restoreOriginal, fadeOriginalIn, cleanup]);
}
