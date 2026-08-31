import test from "node:test";
import assert from "node:assert/strict";

import { DashboardApi } from "./api.js";
import { OrchestrationQueuePanel } from "./components/orchestration-queue.js";
import { DashboardApp } from "./app.js";

// A controllable document/window pair for polling and visibility tests.
function pollingHarness({ visibility = "visible", interval = 5000 } = {}) {
  let intervalId = 0;
  const timers = new Map();
  const listeners = new Map();
  const documentRef = {
    visibilityState: visibility,
    addEventListener: (event, handler) => listeners.set(event, handler),
    removeEventListener: (event) => listeners.delete(event),
    getElementById: () => null,
  };
  const windowRef = {
    setInterval: (fn) => { intervalId += 1; timers.set(intervalId, fn); return intervalId; },
    clearInterval: (id) => timers.delete(id),
  };
  const state = { timer: null, refreshRate: interval };
  return {
    documentRef,
    windowRef,
    state,
    fireInterval: () => { for (const fn of timers.values()) fn(); },
    fireVisibility: (next) => { documentRef.visibilityState = next; listeners.get("visibilitychange")?.(); },
    hasTimer: () => timers.size > 0,
  };
}

test("automatic polling does not overlap while a refresh is in flight", async () => {
  const h = pollingHarness();
  let active = 0;
  let maxActive = 0;
  let completed = 0;
  let release;
  const app = new DashboardApp({ state: h.state, api: {}, documentRef: h.documentRef, windowRef: h.windowRef });
  app.mount({
    refresh: async () => {
      active += 1; maxActive = Math.max(maxActive, active);
      await new Promise((resolve) => { release = resolve; });
      active -= 1; completed += 1;
    },
  });
  app.schedule();
  h.fireInterval(); // start a refresh
  h.fireInterval(); // should be skipped: one already in flight
  h.fireInterval();
  assert.equal(maxActive, 1, "only one refresh runs at a time");
  release();
  await Promise.resolve();
  assert.equal(completed, 1, "overlapping ticks were dropped, not queued");
});

test("polling suspends while hidden and resumes with one revalidation when visible", async () => {
  const h = pollingHarness({ visibility: "visible" });
  let refreshes = 0;
  const app = new DashboardApp({ state: h.state, api: {}, documentRef: h.documentRef, windowRef: h.windowRef });
  app.mount({ refresh: async () => { refreshes += 1; } });
  app.schedule();
  assert.ok(h.hasTimer(), "timer scheduled while visible");

  h.fireVisibility("hidden");
  assert.equal(h.hasTimer(), false, "timer cleared while hidden");
  h.fireInterval(); // no timers to fire; guard also blocks stray ticks
  const hiddenTick = new DashboardApp({ state: h.state, api: {}, documentRef: h.documentRef, windowRef: h.windowRef });
  hiddenTick.tick();
  assert.equal(refreshes, 0, "no refresh occurs while hidden");

  h.fireVisibility("visible");
  await Promise.resolve();
  assert.equal(refreshes, 1, "exactly one revalidation on becoming visible");
  assert.ok(h.hasTimer(), "polling resumes when visible");
});

test("manual bypass reads through the api while a poll may be running", async () => {
  const calls = [];
  const api = new DashboardApi({
    basePathOverride: "",
    fetchImpl: async (path) => { calls.push(path); return { ok: true, json: async () => ({ path }) }; },
  });
  await api.dashboard();       // populate cache
  await api.dashboard();       // served from cache
  assert.equal(calls.length, 1);
  await api.dashboard({ bypass: true });
  assert.equal(calls.length, 2, "manual bypass revalidated");
});

function element() {
  return {
    innerHTML: "", textContent: "", hidden: false, value: "", disabled: false,
    dataset: {}, focus() {}, classList: { add() {}, remove() {} },
  };
}

test("orchestration API exposes prompt, wake, and owner stop routes", async () => {
  const calls = [];
  const api = new DashboardApi({
    basePathOverride: "",
    fetchImpl: async (path, options) => {
      calls.push([path, options]);
      return { ok: true, json: async () => ({ ok: true }) };
    },
  });

  await api.orchestrationPrompt();
  await api.wakeOrchestration({ assignment_key: "oa-1", task_key: "orch", agent_id: "agent-1", wait_seconds: 0 });
  await api.stopOrchestration({ assignment_key: "oa-1" });

  assert.equal(calls[0][0], "/api/dashboard/orchestration/prompt");
  assert.equal(calls[1][0], "/api/dashboard/orchestration/wake");
  assert.equal(calls[2][0], "/api/dashboard/orchestration/stop");
  assert.equal(calls[0][1].method, "POST");
  assert.equal(calls[1][1].method, "POST");
  assert.deepEqual(JSON.parse(calls[1][1].body), {
    assignment_key: "oa-1", task_key: "orch", agent_id: "agent-1", wait_seconds: 0,
  });
});

test("queue panel renders assignment, prompt, states, and owner actions", () => {
  const elements = new Map([
    "queueCount", "queueEmpty", "orchestrationQueue", "queueTodoSelect",
    "queueAssignmentSelect", "queueError", "queuePromptButton", "queuePromptStatus",
    "orchestratorAssignment", "stopOrchestratorButton",
  ].map((id) => [id, element()]));
  const documentRef = { getElementById: (id) => elements.get(id) || null };
  const state = {
    queueSearch: "", queueTodoKey: "", queueAssignmentKey: "", queueBusyKey: "", queueError: "",
    latest: {
      open_todos: [{ todo_key: "todo-one", title: "Todo one", detail: "Detail", app_scope: "mcp-server" }],
      orchestration: {
        assignments: [{
          assignment_key: "oa-1", task_key: "orchestrator", agent_id: "agent-1",
          canonical_model_label: "5.6 Sol High", client_name: "Codex",
          status: "active", is_stale: false, lease_state: "healthy",
          heartbeat_at: "2026-08-12T00:00:00Z", expires_at: "2026-08-12T01:00:00Z",
          recovery_required: true,
        }],
        queue: [{
          queue_key: "oq-1", todo_key: "todo-one", status: "failed", position: 1,
          assignment_key: "oa-1", prompt_snapshot: "Immutable pickup prompt",
          checkpoint_required: true, checkpoint_status: "failed",
          checkpoint_failure_policy: "pause", checkpoint_error: "push failed",
          updated_at: "2026-08-12T00:00:00Z", retry_count: 1,
        }],
      },
    },
  };
  const panel = new OrchestrationQueuePanel({ state, api: {}, onRefresh() {}, documentRef });
  panel.render();
  assert.match(elements.get("orchestrationQueue").innerHTML, /Immutable pickup prompt/);
  assert.match(elements.get("orchestrationQueue").innerHTML, /data-action="retry-queue"/);
  assert.match(elements.get("orchestrationQueue").innerHTML, /failed/);
  assert.match(elements.get("queueAssignmentSelect").innerHTML, /5\.6 Sol High/);
  assert.match(elements.get("queueTodoSelect").innerHTML, /todo-one/);
  assert.match(elements.get("orchestratorAssignment").innerHTML, /Assigned/);
  assert.match(elements.get("orchestratorAssignment").innerHTML, /5\.6 Sol High/);
  assert.match(elements.get("orchestratorAssignment").innerHTML, /Codex/);
  assert.match(elements.get("orchestratorAssignment").innerHTML, /oa-1/);
  assert.match(elements.get("orchestratorAssignment").innerHTML, /Recovery required/);
  assert.equal(elements.get("stopOrchestratorButton").hidden, false);
});

test("queue panel distinguishes queued and dispatched rows with assignment context", () => {
  const elements = new Map([
    "queueCount", "queueEmpty", "orchestrationQueue", "queueTodoSelect",
    "queueAssignmentSelect", "queueError", "queuePromptButton", "queuePromptStatus",
    "orchestratorAssignment", "stopOrchestratorButton",
  ].map((id) => [id, element()]));
  const documentRef = { getElementById: (id) => elements.get(id) || null };
  const state = {
    queueSearch: "", queueTodoKey: "", queueAssignmentKey: "", queueBusyKey: "", queueError: "",
    latest: {
      open_todos: [],
      orchestration: {
        assignments: [{
          assignment_key: "oa-1", task_key: "orch", agent_id: "agent-1",
          canonical_model_label: "5.6 Luna High", status: "active", is_stale: false,
        }],
        queue: [
          { queue_key: "oq-queued", todo_key: "todo-stage", status: "queued", position: 1, assignment_key: "oa-1", checkpoint_required: false },
          { queue_key: "oq-dispatched", todo_key: "todo-ready", status: "dispatched", position: 2, assignment_key: "oa-1", checkpoint_required: false },
        ],
      },
    },
  };
  const panel = new OrchestrationQueuePanel({ state, api: {}, onRefresh() {}, documentRef });
  panel.render();
  const html = elements.get("orchestrationQueue").innerHTML;
  assert.match(html, /class="badge queued"/);
  assert.match(html, /class="badge dispatched"/);
  assert.match(html, /orch \/ agent-1 \/ 5\.6 Luna High/);
});

test("queue add flow submits the selected todo and assignment", async () => {
  const elements = new Map([
    "queueTodoSelect", "queueAssignmentSelect", "queueError",
  ].map((id) => [id, element()]));
  const documentRef = { getElementById: (id) => elements.get(id) || null };
  const calls = [];
  const state = {
    queueSearch: "", queueTodoKey: "", queueAssignmentKey: "", queueBusyKey: "", queueError: "",
    latest: { open_todos: [], orchestration: { assignments: [], queue: [] } },
  };
  const panel = new OrchestrationQueuePanel({
    state,
    api: { enqueueOrchestration: async (body) => calls.push(body) },
    onRefresh: async () => {},
    documentRef,
  });
  await panel.enqueue({
    preventDefault() {},
    target: { elements: { todo_key: { value: "todo-one" }, assignment_key: { value: "oa-1" } } },
  });
  assert.deepEqual(calls, [{ todo_key: "todo-one", assignment_key: "oa-1" }]);
  assert.equal(state.queueError, "");
});

test("queue and dispatch confirms the handoff and prunes only after enqueue", async () => {
  const calls = [];
  let refreshes = 0;
  const state = {
    queueBusyKey: "", queueError: "", queueSearch: "", queueCandidates: [],
    latest: { open_todos: [], orchestration: {
      assignments: [{ assignment_key: "oa-1", task_key: "orch", agent_id: "a1", canonical_model_label: "5.6 Luna High", status: "active", is_stale: false }],
      queue: [],
    } },
  };
  const panel = new OrchestrationQueuePanel({
    state,
    api: {
      enqueueOrchestration: async (body) => calls.push(["enqueue", body]),
      pruneTodo: async (body) => calls.push(["prune", body]),
    },
    onRefresh: async () => { refreshes += 1; },
    documentRef: { getElementById: () => null },
    windowRef: { confirm: (message) => {
      assert.match(message, /source TODO will be marked done/);
      assert.match(message, /orch \/ a1 \/ 5\.6 Luna High/);
      return true;
    } },
  });

  await panel.enqueueTodo("todo-dispatch", "oa-1", { dispatch: true });
  assert.deepEqual(calls, [
    ["enqueue", { todo_key: "todo-dispatch", assignment_key: "oa-1" }],
    ["prune", {
      todo_key: "todo-dispatch", status: "done", actor: "dashboard",
      detail: "Dispatched through the dashboard queue handoff.",
    }],
  ]);
  assert.equal(refreshes, 2, "queue and todo views refresh after each mutation");
  assert.equal(state.queueBusyKey, "");
});

test("enqueue-only stages work without pruning and confirmation cancellation is side-effect free", async () => {
  const calls = [];
  let refreshes = 0;
  const state = {
    queueBusyKey: "", queueError: "", queueSearch: "", queueCandidates: [],
    latest: { open_todos: [], orchestration: {
      assignments: [{ assignment_key: "oa-1", task_key: "orch", agent_id: "a1", canonical_model_label: "5.6 Luna High", status: "active", is_stale: false }],
      queue: [],
    } },
  };
  const panel = new OrchestrationQueuePanel({
    state,
    api: {
      enqueueOrchestration: async (body) => calls.push(["enqueue", body]),
      pruneTodo: async () => calls.push(["prune"]),
    },
    onRefresh: async () => { refreshes += 1; },
    documentRef: { getElementById: () => null },
    windowRef: { confirm: () => { throw new Error("enqueue-only must not confirm"); } },
  });

  await panel.enqueueTodo("todo-stage", "oa-1", { dispatch: false });
  assert.deepEqual(calls, [["enqueue", { todo_key: "todo-stage", assignment_key: "oa-1" }]]);
  assert.equal(refreshes, 1);

  const cancelled = new OrchestrationQueuePanel({
    state: { ...state, queueBusyKey: "" },
    api: { enqueueOrchestration: async () => calls.push(["unexpected"]) },
    onRefresh: async () => { refreshes += 1; },
    documentRef: { getElementById: () => null },
    windowRef: { confirm: () => false },
  });
  await cancelled.enqueueTodo("todo-cancel", "oa-1", { dispatch: true });
  assert.equal(calls.some(([kind]) => kind === "unexpected"), false);
  assert.equal(refreshes, 1);
});

test("queue dispatch guards missing assignments, duplicate clicks, and enqueue failures", async () => {
  const noAssignmentCalls = [];
  const noAssignmentState = {
    queueBusyKey: "", queueError: "", queueSearch: "", queueCandidates: [],
    latest: { open_todos: [], orchestration: { assignments: [], queue: [] } },
  };
  const noAssignment = new OrchestrationQueuePanel({
    state: noAssignmentState,
    api: { enqueueOrchestration: async () => noAssignmentCalls.push(true) },
    onRefresh() {},
    documentRef: { getElementById: () => null },
  });
  noAssignment.queueFromRadial("todo-no-assignment", null, "dispatch");
  assert.equal(noAssignmentCalls.length, 0);
  assert.match(noAssignmentState.queueError, /Designate an orchestrator/);

  let release;
  const calls = [];
  const state = {
    queueBusyKey: "", queueError: "", queueSearch: "", queueCandidates: [],
    latest: { open_todos: [], orchestration: {
      assignments: [{ assignment_key: "oa-1", task_key: "orch", agent_id: "a1", canonical_model_label: "5.6 Luna High", status: "active", is_stale: false }],
      queue: [],
    } },
  };
  const panel = new OrchestrationQueuePanel({
    state,
    api: { enqueueOrchestration: async (body) => {
      calls.push(body);
      await new Promise((resolve) => { release = resolve; });
    } },
    onRefresh() {},
    documentRef: { getElementById: () => null },
  });
  const first = panel.enqueueTodo("todo-duplicate", "oa-1", { dispatch: false });
  await Promise.resolve();
  const second = panel.enqueueTodo("todo-duplicate", "oa-1", { dispatch: false });
  release();
  await Promise.all([first, second]);
  assert.deepEqual(calls, [{ todo_key: "todo-duplicate", assignment_key: "oa-1" }]);

  const failedState = { ...state, queueBusyKey: "", queueError: "" };
  const failed = new OrchestrationQueuePanel({
    state: failedState,
    api: {
      enqueueOrchestration: async () => { throw new Error("enqueue failed"); },
      pruneTodo: async () => { throw new Error("prune must not run"); },
    },
    onRefresh() {},
    documentRef: { getElementById: () => null },
    windowRef: { confirm: () => true },
  });
  await failed.enqueueTodo("todo-failed", "oa-1", { dispatch: true });
  assert.match(failedState.queueError, /Queue or dispatch failed: enqueue failed/);
  assert.equal(failedState.queueBusyKey, "");
});

test("queue panel shows pending child cancellation and hides unsafe retry/cancel actions", () => {
  const elements = new Map([
    "queueCount", "queueEmpty", "orchestrationQueue", "queueTodoSelect",
    "queueAssignmentSelect", "queueError",
  ].map((id) => [id, element()]));
  const documentRef = { getElementById: (id) => elements.get(id) || null };
  const state = {
    queueSearch: "", queueTodoKey: "", queueAssignmentKey: "", queueBusyKey: "", queueError: "",
    latest: {
      open_todos: [],
      orchestration: {
        assignments: [],
        queue: [{
          queue_key: "oq-cancel", todo_key: "todo-cancel", status: "delegated", position: 1,
          assignment_key: "oa-1", prompt_snapshot: "Prompt", checkpoint_required: false,
          cancellation_pending: true, cancel_requested_at: "2026-08-12T00:00:00Z",
          cancel_acknowledged_at: null, updated_at: "2026-08-12T00:00:00Z",
        }],
      },
    },
  };
  const panel = new OrchestrationQueuePanel({ state, api: {}, onRefresh() {}, documentRef });
  panel.render();
  const html = elements.get("orchestrationQueue").innerHTML;
  assert.match(html, /cancellation requested; awaiting child acknowledgement/);
  assert.doesNotMatch(html, /data-action="retry-queue"/);
  assert.doesNotMatch(html, /data-action="cancel-queue"/);
});

test("queue panel offers Drop for cancelled/failed/stale rows and calls the drop route", async () => {
  const elements = new Map([
    "queueCount", "queueEmpty", "orchestrationQueue", "queueTodoSelect",
    "queueAssignmentSelect", "queueError",
  ].map((id) => [id, element()]));
  const documentRef = { getElementById: (id) => elements.get(id) || null };
  const calls = [];
  const state = {
    queueSearch: "", queueTodoKey: "", queueAssignmentKey: "", queueBusyKey: "", queueError: "",
    latest: {
      open_todos: [],
      orchestration: {
        assignments: [],
        queue: [
          { queue_key: "oq-cancelled", todo_key: "t-c", status: "cancelled", position: 1, assignment_key: "oa-1", prompt_snapshot: "P", checkpoint_required: false, updated_at: "2026-08-12T00:00:00Z" },
          { queue_key: "oq-completed", todo_key: "t-done", status: "completed", position: 2, assignment_key: "oa-1", prompt_snapshot: "P", checkpoint_required: false, updated_at: "2026-08-12T00:00:00Z" },
          { queue_key: "oq-pending", todo_key: "t-p", status: "delegated", position: 3, assignment_key: "oa-1", prompt_snapshot: "P", checkpoint_required: false, cancellation_pending: true, cancel_requested_at: "2026-08-12T00:00:00Z", cancel_acknowledged_at: null, updated_at: "2026-08-12T00:00:00Z" },
        ],
      },
    },
  };
  const panel = new OrchestrationQueuePanel({
    state,
    api: { dropOrchestration: async (body) => { calls.push(body); } },
    onRefresh: async () => {},
    documentRef,
  });
  panel.render();
  const html = elements.get("orchestrationQueue").innerHTML;
  // Drop appears for the cancelled row, but not for completed or cancellation-pending rows.
  assert.match(html, /data-action="drop-queue" data-queue-key="oq-cancelled"/);
  assert.doesNotMatch(html, /data-action="drop-queue" data-queue-key="oq-completed"/);
  assert.doesNotMatch(html, /data-action="drop-queue" data-queue-key="oq-pending"/);

  await panel.mutate("oq-cancelled", "drop");
  assert.deepEqual(calls, [{ queue_key: "oq-cancelled", reason: "Dropped by dashboard owner." }]);
});

test("radial enqueue-only action targets the sole active orchestrator", async () => {
  const documentRef = { getElementById: () => null };
  const calls = [];
  const state = {
    queueSearch: "", queueTodoKey: "", queueAssignmentKey: "", queueBusyKey: "", queueError: "",
    latest: {
      open_todos: [],
      orchestration: {
        assignments: [{ assignment_key: "oa-1", task_key: "orch", agent_id: "a1", canonical_model_label: "5.6 Sol High", status: "active", is_stale: false }],
        queue: [],
      },
    },
  };
  const panel = new OrchestrationQueuePanel({
    state,
    api: {
      enqueueOrchestration: async (body) => { calls.push(body); },
      pruneTodo: async () => { throw new Error("enqueue-only must not prune"); },
    },
    onRefresh: async () => {},
    documentRef,
  });
  panel.queueFromRadial("todo-solo", null, "enqueue");
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(calls, [{ todo_key: "todo-solo", assignment_key: "oa-1" }]);
});

test("radial queue opens a picker when more than one orchestrator is active", async () => {
  const created = [];
  let appended = null;
  const body = { appendChild(node) { appended = node; }, contains: () => false };
  const documentRef = {
    body,
    getElementById: () => null,
    createElement: () => {
      const node = {
        className: "", style: {}, innerHTML: "", children: [],
        listeners: {},
        setAttribute() {},
        addEventListener(type, handler) { this.listeners[type] = handler; },
        querySelector() { return { focus() {} }; },
        contains: () => false,
        remove() {},
      };
      created.push(node);
      return node;
    },
  };
  const calls = [];
  const windowRef = { addEventListener() {}, removeEventListener() {} };
  const state = {
    queueSearch: "", queueTodoKey: "", queueAssignmentKey: "", queueBusyKey: "", queueError: "",
    latest: {
      open_todos: [],
      orchestration: {
        assignments: [
          { assignment_key: "oa-1", task_key: "orch", agent_id: "a1", canonical_model_label: "5.6 Sol High", status: "active", is_stale: false },
          { assignment_key: "oa-2", task_key: "orch", agent_id: "a2", canonical_model_label: "Opus 4.8 High", status: "active", is_stale: false },
        ],
        queue: [],
      },
    },
  };
  const panel = new OrchestrationQueuePanel({
    state,
    api: { enqueueOrchestration: async (b) => { calls.push(b); } },
    onRefresh: async () => {},
    documentRef,
    windowRef,
  });
  panel.queueFromRadial("todo-multi", null, "enqueue");
  assert.equal(created.length, 1, "a picker element was created");
  const menu = appended;
  assert.match(menu.innerHTML, /oa-1/);
  assert.match(menu.innerHTML, /oa-2/);
  assert.equal(calls.length, 0, "no enqueue until an orchestrator is chosen");

  // Selecting an orchestrator in the picker enqueues against it.
  menu.contains = () => true;
  menu.listeners.click({ target: { closest: () => ({ dataset: { assignmentKey: "oa-2" } }) } });
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(calls, [{ todo_key: "todo-multi", assignment_key: "oa-2" }]);
});

test("radial queue with no orchestrator prefills the manual form with guidance", () => {
  const elements = new Map([
    "queueTodoSelect", "queueAssignmentSelect", "queueError", "queueTodoSearch",
    "orchestrationQueueSection",
  ].map((id) => [id, element()]));
  for (const el of elements.values()) el.scrollIntoView = () => {};
  const documentRef = { getElementById: (id) => elements.get(id) || null };
  const state = {
    queueSearch: "", queueTodoKey: "", queueAssignmentKey: "", queueBusyKey: "", queueError: "",
    queueCandidates: [],
    latest: { open_todos: [], orchestration: { assignments: [], queue: [] } },
  };
  let enqueued = 0;
  const panel = new OrchestrationQueuePanel({
    state, api: { enqueueOrchestration: async () => { enqueued += 1; } }, onRefresh() {}, documentRef,
  });
  panel.queueFromRadial("todo-none");
  assert.equal(enqueued, 0, "nothing is enqueued without an orchestrator");
  assert.match(state.queueError, /Designate an orchestrator/);
});

test("radial wake sends the exact active assignment identity and reports idle queue state", async () => {
  const elements = new Map(["queueWakeStatus"].map((id) => [id, element()]));
  const documentRef = { getElementById: (id) => elements.get(id) || null };
  const calls = [];
  let refreshes = 0;
  const assignment = {
    assignment_key: "oa-1", task_key: "orch", agent_id: "a1",
    canonical_model_label: "5.6 Luna High", status: "active", is_stale: false,
  };
  const state = {
    queueSearch: "", queueTodoKey: "", queueAssignmentKey: "", queueBusyKey: "", queueError: "",
    queueWakeBusyKey: "", queueWakeStatus: "",
    latest: { open_todos: [], orchestration: { assignments: [assignment], queue: [
      { todo_key: "todo-solo", status: "queued" },
    ] } },
  };
  const panel = new OrchestrationQueuePanel({
    state,
    api: {
      wakeOrchestration: async (body) => {
        calls.push(body);
        return { pending: false, action: "idle", queue_item: null };
      },
    },
    onRefresh: async () => { refreshes += 1; },
    documentRef,
  });

  await panel.wakeFromRadial("todo-solo");
  assert.deepEqual(calls, [{ assignment_key: "oa-1", task_key: "orch", agent_id: "a1", wait_seconds: 0 }]);
  assert.match(state.queueWakeStatus, /no dispatched work.*queued/i);
  assert.equal(refreshes, 1);
  assert.equal(state.queueWakeBusyKey, "");
});

test("radial wake reports server failures without leaving the wake busy", async () => {
  const elements = new Map(["queueWakeStatus"].map((id) => [id, element()]));
  const assignment = { assignment_key: "oa-1", task_key: "orch", agent_id: "a1", status: "active", is_stale: false };
  const state = {
    queueWakeBusyKey: "", queueWakeStatus: "", queueSearch: "", queueTodoKey: "", queueAssignmentKey: "",
    queueBusyKey: "", queueError: "", latest: { orchestration: { assignments: [assignment], queue: [] } },
  };
  const panel = new OrchestrationQueuePanel({
    state,
    api: { wakeOrchestration: async () => { throw new Error("identity mismatch"); } },
    onRefresh: async () => {},
    documentRef: { getElementById: (id) => elements.get(id) || null },
  });

  await panel.wakeFromRadial("todo-error");
  assert.match(state.queueWakeStatus, /Wake failed: identity mismatch/);
  assert.equal(state.queueWakeBusyKey, "");
});

test("radial wake requires an active assignment and supports explicit multi-assignment selection", async () => {
  const noAssignmentState = {
    queueSearch: "", queueTodoKey: "", queueAssignmentKey: "", queueBusyKey: "", queueError: "",
    queueWakeBusyKey: "", queueWakeStatus: "",
    latest: { open_todos: [], orchestration: { assignments: [], queue: [] } },
  };
  const noAssignmentElements = new Map(["queueTodoSelect", "queueAssignmentSelect", "queueError", "queueTodoSearch", "orchestrationQueueSection", "queueWakeStatus"]
    .map((id) => [id, element()]));
  noAssignmentElements.get("orchestrationQueueSection").scrollIntoView = () => {};
  const noAssignmentPanel = new OrchestrationQueuePanel({
    state: noAssignmentState,
    api: { wakeOrchestration: async () => { throw new Error("should not wake"); } },
    onRefresh: async () => {},
    documentRef: { getElementById: (id) => noAssignmentElements.get(id) || null },
  });
  await noAssignmentPanel.wakeFromRadial("todo-none");
  assert.match(noAssignmentState.queueWakeStatus, /designate an active orchestrator/i);

  let appended = null;
  const body = { appendChild(node) { appended = node; }, contains: () => false };
  const createdDocument = {
    body,
    getElementById: () => null,
    createElement: () => ({
      className: "", style: {}, innerHTML: "", listeners: {},
      setAttribute() {},
      addEventListener(type, handler) { this.listeners[type] = handler; },
      querySelector() { return { focus() {} }; },
      contains: () => false,
      remove() {},
    }),
  };
  const assignments = [
    { assignment_key: "oa-1", task_key: "orch-1", agent_id: "a1", canonical_model_label: "5.6 Luna High", status: "active", is_stale: false },
    { assignment_key: "oa-2", task_key: "orch-2", agent_id: "a2", canonical_model_label: "Opus 4.8 High", status: "active", is_stale: false },
  ];
  const wakeCalls = [];
  const multiState = {
    queueSearch: "", queueTodoKey: "", queueAssignmentKey: "", queueBusyKey: "", queueError: "",
    queueWakeBusyKey: "", queueWakeStatus: "",
    latest: { open_todos: [], orchestration: { assignments, queue: [] } },
  };
  const multiPanel = new OrchestrationQueuePanel({
    state: multiState,
    api: { wakeOrchestration: async (request) => { wakeCalls.push(request); return { action: "idle" }; } },
    onRefresh: async () => {},
    documentRef: createdDocument,
    windowRef: { addEventListener() {}, removeEventListener() {} },
  });
  multiPanel.wakeFromRadial("todo-multi");
  assert.match(appended.innerHTML, /Wake orchestrator/);
  appended.contains = () => true;
  appended.listeners.click({ target: { closest: () => ({ dataset: { assignmentKey: "oa-2" } }) } });
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(wakeCalls, [{ assignment_key: "oa-2", task_key: "orch-2", agent_id: "a2", wait_seconds: 0 }]);
});

test("orchestrator prompt copies with Clipboard API and reports failures", async () => {
  const elements = new Map([
    "queueCount", "queueEmpty", "orchestrationQueue", "queueTodoSelect",
    "queueAssignmentSelect", "queueError", "queuePromptButton", "queuePromptStatus",
    "orchestratorAssignment", "stopOrchestratorButton",
  ].map((id) => [id, element()]));
  const documentRef = { getElementById: (id) => elements.get(id) || null };
  const writes = [];
  const state = {
    queueSearch: "", queueTodoKey: "", queueAssignmentKey: "", queueBusyKey: "", queueError: "",
    queuePromptBusy: false, queuePromptStatus: "", queueStopBusy: false,
    latest: { open_todos: [], orchestration: { assignments: [], queue: [] } },
  };
  const panel = new OrchestrationQueuePanel({
    state,
    api: { orchestrationPrompt: async () => ({ prompt: "Bootstrap prompt" }) },
    onRefresh: async () => {},
    documentRef,
    navigatorRef: { clipboard: { writeText: async (text) => writes.push(text) } },
  });

  await panel.copyPrompt();
  assert.deepEqual(writes, ["Bootstrap prompt"]);
  assert.equal(state.queuePromptStatus, "Prompt copied.");

  panel.navigator = { clipboard: { writeText: async () => { throw new Error("permission denied"); } } };
  await panel.copyPrompt();
  assert.match(state.queuePromptStatus, /Copy failed: permission denied/);
});

test("orchestrator prompt uses the tested execCommand fallback", async () => {
  const elements = new Map([
    "queueCount", "queueEmpty", "orchestrationQueue", "queueTodoSelect",
    "queueAssignmentSelect", "queueError", "queuePromptButton", "queuePromptStatus",
    "orchestratorAssignment", "stopOrchestratorButton",
  ].map((id) => [id, element()]));
  let copiedValue = "";
  const body = { appendChild(node) { copiedValue = node.value; }, removeChild() {} };
  const documentRef = {
    body,
    getElementById: (id) => elements.get(id) || null,
    createElement: () => ({ value: "", style: {}, setAttribute() {}, select() {} }),
    execCommand: (command) => command === "copy",
  };
  const state = {
    queueSearch: "", queueTodoKey: "", queueAssignmentKey: "", queueBusyKey: "", queueError: "",
    queuePromptBusy: false, queuePromptStatus: "", queueStopBusy: false,
    latest: { open_todos: [], orchestration: { assignments: [], queue: [] } },
  };
  const panel = new OrchestrationQueuePanel({
    state,
    api: { orchestrationPrompt: async () => ({ prompt: "Fallback prompt" }) },
    onRefresh: async () => {},
    documentRef,
    navigatorRef: {},
  });

  await panel.copyPrompt();
  assert.equal(copiedValue, "Fallback prompt");
  assert.equal(state.queuePromptStatus, "Prompt copied.");
});

test("stop orchestrator confirms and sends a stable owner request", async () => {
  const elements = new Map([
    "queueCount", "queueEmpty", "orchestrationQueue", "queueTodoSelect",
    "queueAssignmentSelect", "queueError", "queuePromptButton", "queuePromptStatus",
    "orchestratorAssignment", "stopOrchestratorButton",
  ].map((id) => [id, element()]));
  const documentRef = { getElementById: (id) => elements.get(id) || null };
  const calls = [];
  let refreshes = 0;
  const state = {
    queueSearch: "", queueTodoKey: "", queueAssignmentKey: "", queueBusyKey: "", queueError: "",
    queuePromptBusy: false, queuePromptStatus: "", queueStopBusy: false,
    latest: { open_todos: [], orchestration: { assignments: [], queue: [] } },
  };
  const panel = new OrchestrationQueuePanel({
    state,
    api: { stopOrchestration: async (body) => calls.push(body) },
    onRefresh: async () => { refreshes += 1; },
    documentRef,
    windowRef: { confirm: () => true },
  });

  await panel.stopOrchestrator("oa-stop");
  assert.deepEqual(calls, [{
    assignment_key: "oa-stop",
    idempotency_key: "dashboard-stop-oa-stop",
    reason: "Stopped by dashboard owner.",
  }]);
  assert.equal(refreshes, 1);
  assert.equal(state.queueStopBusy, false);
});
