export class DashboardApp {
  constructor({ state, api, documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
    this.state = state;
    this.api = api;
    this.document = documentRef;
    this.window = windowRef;
    this.refreshHandler = null;
    this.bindings = [];
    this.requestGenerations = new Map();
    // Guards a single automatic refresh cycle so overlapping polls cannot pile
    // up when a fetch runs longer than the interval.
    this.refreshInFlight = false;
    this.visibilityHandler = null;
  }

  mount({ refresh, bindings = [] } = {}) {
    this.refreshHandler = refresh;
    this.bindings = bindings;
    for (const { selector, event, handler, target, all = false } of bindings) {
      const root = target || (all
        ? this.document?.querySelectorAll(selector)
        : this.document?.querySelector(selector));
      if (root?.addEventListener) root.addEventListener(event, handler);
      else root?.forEach((element) => element.addEventListener(event, handler));
    }
    this.bindVisibility();
    return this;
  }

  // Suspend automatic polling while the tab is hidden and resume with one
  // immediate revalidation when it becomes visible again, so a background tab
  // stops generating traffic without going stale on return.
  bindVisibility() {
    if (this.visibilityHandler || !this.document?.addEventListener) return;
    this.visibilityHandler = () => {
      if (this.isHidden()) {
        this.stopTimer();
      } else {
        this.schedule();
        this.refresh();
      }
    };
    this.document.addEventListener("visibilitychange", this.visibilityHandler);
  }

  isHidden() {
    return this.document?.visibilityState === "hidden";
  }

  // Resolve the polling interval (ms) from persisted state, falling back to the
  // #refreshRate control value for compatibility.
  intervalMs() {
    if (this.state && typeof this.state.refreshRate === "number") return this.state.refreshRate;
    return Number(this.document?.getElementById("refreshRate")?.value) || 0;
  }

  stopTimer() {
    if (this.state.timer) this.window.clearInterval(this.state.timer);
    this.state.timer = null;
  }

  schedule() {
    this.stopTimer();
    const interval = this.intervalMs();
    // Never poll while hidden; the visibility handler reschedules on return.
    if (interval > 0 && this.refreshHandler && !this.isHidden()) {
      this.state.timer = this.window.setInterval(() => this.tick(), interval);
    }
  }

  // Automatic tick: skip if a refresh is already running or the tab is hidden,
  // so cycles never overlap and hidden tabs stay quiet.
  tick() {
    if (this.refreshInFlight || this.isHidden()) return;
    this.refresh();
  }

  async refresh() {
    if (!this.refreshHandler) return;
    if (this.refreshInFlight) return;
    this.refreshInFlight = true;
    try {
      return await this.refreshHandler();
    } finally {
      this.refreshInFlight = false;
    }
  }

  beginRequest(key) {
    const generation = (this.requestGenerations.get(key) || 0) + 1;
    this.requestGenerations.set(key, generation);
    return generation;
  }

  isCurrentRequest(key, generation) {
    return this.requestGenerations.get(key) === generation;
  }

  start({ refresh, bindings } = {}) {
    this.mount({ refresh, bindings });
    this.schedule();
    return this.refresh();
  }

  stop() {
    this.stopTimer();
    if (this.visibilityHandler && this.document?.removeEventListener) {
      this.document.removeEventListener("visibilitychange", this.visibilityHandler);
      this.visibilityHandler = null;
    }
  }
}
