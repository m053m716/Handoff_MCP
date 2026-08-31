import test from "node:test";
import assert from "node:assert/strict";
import { TodoPanel } from "./components/todos.js";

class FakeMenuElement {
  constructor(documentRef) {
    this.document = documentRef;
    this.dataset = {};
    this.style = {};
    this.listeners = {};
    this.children = [];
    this.className = "";
    this.removed = false;
    this.buttons = [];
  }

  setAttribute(name, value) { this[name] = value; }

  addEventListener(name, handler) { this.listeners[name] = handler; }

  appendChild(child) { this.children.push(child); child.parentNode = this; }

  remove() { this.removed = true; }

  contains(target) { return target === this || this.children.includes(target); }

  getBoundingClientRect() { return { width: 0, height: 0 }; }

  querySelector(selector) {
    return selector === "button:not([disabled])" ? this.buttons[0] : null;
  }

  querySelectorAll(selector) {
    return selector === "button:not([disabled])" ? this.buttons : [];
  }

  set innerHTML(value) {
    this.html = value;
    this.buttons = [...value.matchAll(/<button[^>]*data-action="([^"]+)"[^>]*data-todo-key="([^"]+)"[^>]*>([^<]+)<\/button>/g)]
      .map((match) => {
        const button = {
          dataset: { action: match[1], todoKey: match[2] },
          textContent: match[3],
          closest: (selector) => selector === "[data-action]" ? button : selector === ".todo-context-menu" ? this : null,
          focus: () => { this.document.activeElement = button; button.focused = true; },
        };
        return button;
      });
  }
}

const todoFixture = () => ({
  todo_key: "dashboard-compact-todo-cards",
  title: "Make Open Todo cards compact",
  detail: "verbose detail stays available after expansion",
  status: "open",
  priority: "P1",
  app_scope: "mcp-server",
  planned_complexity: "M",
  code_paths: ["mcp/gui/dashboard/components/todos.js"],
  reference_advisories: ["Add doc_keys"],
  related_todos: [{ todo_key: "related", title: "Related todo", priority: "P2" }],
  updated_at: "2026-08-12T12:00:00Z",
});

const validationTodoFixture = () => ({
  ...todoFixture(),
  todo_key: "on-device-validation-check",
  title: "Run the human validation session",
  app_scope: "on-device-validation",
  detail: "validation detail stays in the validation lane",
});

const fixturePanel = ({ interactive = false, callbacks = {} } = {}) => {
  const elements = new Map([
    ["todoCount", { textContent: "" }],
    ["todoEmpty", { hidden: true, textContent: "" }],
    ["openTodos", { innerHTML: "" }],
  ]);
  const state = {
    latest: { open_todos: [todoFixture(), validationTodoFixture()], doc_scopes: [] },
    todoScopeFilter: "__all__",
    todoStatusFilter: "open",
    todoPriorityFilter: "",
    todoNextCursor: null,
    pruneTodoKey: null,
    pruneDrafts: {},
  };
  const documentRef = {
    getElementById: (id) => elements.get(id),
    querySelectorAll: () => [],
  };
  if (interactive) {
    documentRef.listeners = {};
    documentRef.body = { appendChild: (child) => { documentRef.menu = child; }, contains: () => false };
    documentRef.documentElement = { clientWidth: 1024, clientHeight: 768 };
    documentRef.activeElement = documentRef.body;
    documentRef.addEventListener = (name, handler) => { documentRef.listeners[name] = handler; };
    documentRef.createElement = () => new FakeMenuElement(documentRef);
  }
  const scheduled = [];
  const windowRef = interactive
    ? { innerWidth: 1024, innerHeight: 768, addEventListener: () => {}, setTimeout: (callback) => scheduled.push(callback) }
    : { setTimeout: (callback) => scheduled.push(callback) };
  const panel = new TodoPanel({
    state,
    documentRef,
    windowRef,
    onSearchDocs: callbacks.onSearchDocs || (() => {}),
    onCopyPrompt: callbacks.onCopyPrompt || (() => {}),
    onQueue: callbacks.onQueue || (() => {}),
    onWake: callbacks.onWake || (() => {}),
  });
  return { elements, panel, scheduled, documentRef, windowRef };
};

test("open todo cards keep summaries compact and details/actions disclosed", () => {
  const { elements, panel } = fixturePanel();
  panel.renderCurrent();
  const html = elements.get("openTodos").innerHTML;
  const bodyStart = html.indexOf('<div class="todo-disclosure-body">');

  assert.match(html, /<details class="todo-disclosure"/);
  assert.match(html, /data-todo-lane="agent"/);
  assert.match(html, /data-todo-lane="validation"/);
  assert.match(html, /Agent TODOs/);
  assert.match(html, /Human \/ device validation TODOs/);
  assert.match(html, /data-todo-lane-count="agent">1</);
  assert.match(html, /data-todo-lane-count="validation">1</);
  assert.match(html, /on-device-validation-check/);
  assert.match(html, /title="Updated [^"]+"/);
  assert.doesNotMatch(html, /<th class="col-time">Updated<\/th>/);
  assert.match(html, /<summary class="todo-summary" data-action="toggle-todo"/);
  assert.ok(bodyStart > 0);
  assert.ok(html.indexOf("verbose detail stays available") > bodyStart);
  assert.ok(html.indexOf("Queue") > bodyStart);
  assert.ok(html.indexOf("priority-chip") < bodyStart);
  assert.ok(html.indexOf("scope-chip") < bodyStart);
  assert.ok(html.indexOf("open-prune") < bodyStart);
  assert.doesNotMatch(html, /<details class="todo-disclosure"[^>]* open>/);

  panel.expandedTodoKeys.add("dashboard-compact-todo-cards");
  panel.renderCurrent();
  assert.match(elements.get("openTodos").innerHTML, /<details class="todo-disclosure"[^>]* open>/);
});

test("queued todos render greyed via is-queued while non-queued rows do not", () => {
  const { elements, panel } = fixturePanel();
  const queued = { ...todoFixture(), todo_key: "queued-todo", status: "queued" };
  panel.state.latest.open_todos = [queued, todoFixture()];
  panel.state.todoValidationTodos = [];
  panel.renderCurrent();

  const html = elements.get("openTodos").innerHTML;
  assert.match(
    html,
    /<tr class="todo-row[^"]*\bis-queued\b[^"]*"[^>]*data-todo-key="queued-todo"/,
  );
  const openRow = html.match(
    /<tr class="(todo-row[^"]*)"[^>]*data-todo-key="dashboard-compact-todo-cards"/,
  );
  assert.ok(openRow);
  assert.doesNotMatch(openRow[1], /\bis-queued\b/);
});

test("todo disclosure interaction preserves expanded state across rerenders", () => {
  const { panel, scheduled } = fixturePanel();
  const details = { open: true };
  const summary = {
    dataset: { action: "toggle-todo", todoKey: "dashboard-compact-todo-cards" },
    closest: (selector) => selector === "details" ? details : null,
    setAttribute: () => {},
  };
  panel.handleClick({
    target: { closest: (selector) => selector === "[data-action]" ? summary : null },
  });
  scheduled.shift()();
  assert.ok(panel.expandedTodoKeys.has("dashboard-compact-todo-cards"));

  details.open = false;
  panel.handleClick({
    target: { closest: (selector) => selector === "[data-action]" ? summary : null },
  });
  scheduled.shift()();
  assert.ok(!panel.expandedTodoKeys.has("dashboard-compact-todo-cards"));
});

test("default view merges bounded validation supplements without duplicating rows", () => {
  const { elements, panel } = fixturePanel();
  panel.state.latest.open_todos = [todoFixture()];
  panel.state.todoValidationTodos = [validationTodoFixture(), todoFixture()];
  panel.renderCurrent();

  const html = elements.get("openTodos").innerHTML;
  assert.match(html, /data-todo-lane-count="agent">1</);
  assert.match(html, /data-todo-lane-count="validation">1</);
  assert.equal((html.match(/<tr class="todo-row\b/g) || []).length, 2);
});

test("todo prune confirmation keeps its compact inline form contract", () => {
  const { elements, panel } = fixturePanel();
  panel.state.pruneTodoKey = "dashboard-compact-todo-cards";
  panel.state.pruneDrafts[panel.state.pruneTodoKey] = { detail: "closeout", status: "dropped" };
  panel.renderCurrent();

  const html = elements.get("openTodos").innerHTML;
  assert.match(html, /<tr class="todo-prune-row" data-todo-key="dashboard-compact-todo-cards">/);
  assert.match(html, /<form class="todo-prune-form" data-todo-key="dashboard-compact-todo-cards">/);
  assert.match(html, /class="todo-prune-summary"/);
  assert.match(html, /name="detail"[^>]*value="closeout"/);
  assert.match(html, /<option value="done">Done<\/option>/);
  assert.match(html, /<option value="dropped" selected>Dropped<\/option>/);
  assert.match(html, /class="todo-prune-actions"/);
  assert.match(html, /data-action="cancel-prune"/);
});

test("todo context menus preserve native menus outside open rows and clamp to the viewport", () => {
  const { panel, documentRef, windowRef } = fixturePanel({ interactive: true });
  const summary = { focus: () => { summary.focused = true; } };
  const row = {
    dataset: { todoKey: "dashboard-compact-todo-cards", todoStatus: "open" },
    querySelector: () => summary,
  };
  let prevented = 0;
  panel.handleContextMenu({
    target: { closest: () => row },
    clientX: 1,
    clientY: 1,
    preventDefault: () => { prevented += 1; },
  });

  assert.equal(prevented, 1);
  assert.equal(documentRef.menu.style.left, "8px");
  assert.equal(documentRef.menu.style.top, "8px");
  assert.match(documentRef.menu.className, /has-4-actions/);
  assert.equal(documentRef.menu.style["--todo-context-menu-size"], "240px");
  assert.doesNotMatch(documentRef.menu.html, /menu-angle|menu-counter-angle/);
  assert.match(documentRef.menu.html, /Queue &amp; dispatch|Queue & dispatch/);
  assert.match(documentRef.menu.html, /Copy Prompt/);

  let keyPrevented = 0;
  documentRef.menu.listeners.keydown({
    key: "ArrowRight",
    target: documentRef.menu.buttons[0],
    preventDefault: () => { keyPrevented += 1; },
  });
  assert.equal(keyPrevented, 1);
  assert.equal(documentRef.menu.buttons[1].focused, true);

  documentRef.listeners.click({ target: {} });
  assert.equal(panel.contextMenu, null);
  assert.equal(summary.focused, true);

  windowRef.innerWidth = 220;
  windowRef.innerHeight = 180;
  panel.openContextMenu(row.dataset.todoKey, 219, 179, row);
  assert.match(documentRef.menu.className, /is-linear/);

  panel.handleContextMenu({
    target: { closest: () => null },
    preventDefault: () => { prevented += 1; },
  });
  assert.equal(prevented, 1);

  documentRef.listeners.keydown({ key: "Escape", preventDefault: () => {} });
  assert.equal(panel.contextMenu, null);
  assert.equal(summary.focused, true);
});

test("todo context menu actions share row availability and callbacks", () => {
  const calls = [];
  const { panel, documentRef } = fixturePanel({
    interactive: true,
    callbacks: {
      onSearchDocs: (todoKey) => calls.push(["search", todoKey]),
      onQueue: (todoKey, button, mode) => calls.push(["queue", todoKey, mode]),
      onCopyPrompt: (todoKey) => calls.push(["copy", todoKey]),
    },
  });
  const row = {
    dataset: { todoKey: "dashboard-compact-todo-cards", todoStatus: "open" },
    querySelector: () => null,
  };
  panel.openContextMenu(row.dataset.todoKey, 500, 400, row);
  for (const action of ["queue-dispatch", "queue-stage", "search-todo-docs", "copy-todo-next-instruction"]) {
    const button = documentRef.menu.buttons.find((candidate) => candidate.dataset.action === action);
    panel.handleClick({ target: button });
    if (action !== "copy-todo-next-instruction") panel.openContextMenu(row.dataset.todoKey, 500, 400, row);
  }
  assert.deepEqual(calls, [
    ["queue", "dashboard-compact-todo-cards", "dispatch"],
    ["queue", "dashboard-compact-todo-cards", "enqueue"],
    ["search", "dashboard-compact-todo-cards"],
    ["copy", "dashboard-compact-todo-cards"],
  ]);

  const noRefs = { ...todoFixture(), code_paths: [], reference_advisories: [], doc_keys: [] };
  assert.deepEqual(panel.todoRowActions(noRefs).map((item) => item.action), ["queue-dispatch", "queue-stage"]);
});

test("queued todo context menus expose the orchestrator wake action", () => {
  const calls = [];
  const { panel, documentRef } = fixturePanel({
    interactive: true,
    callbacks: { onWake: (todoKey) => calls.push(todoKey) },
  });
  const queued = { ...todoFixture(), status: "queued" };
  panel.state.latest.open_todos = [queued];

  assert.ok(panel.todoRowActions(queued).some((item) => item.action === "wake-orchestrator"));
  panel.openContextMenu(queued.todo_key, 500, 400, { querySelector: () => null });
  const button = documentRef.menu.buttons.find((item) => item.dataset.action === "wake-orchestrator");
  assert.equal(button.textContent, "Wake");
  panel.handleClick({ target: button });
  assert.deepEqual(calls, [queued.todo_key]);
});

test("validation-failed action is limited to validation todos and uses the five-action radial menu", () => {
  const calls = [];
  const { panel, documentRef } = fixturePanel({
    interactive: true,
    callbacks: {
      onCopyPrompt: (todoKey, button, variant) => calls.push([todoKey, variant]),
    },
  });
  const validation = validationTodoFixture();
  const ordinary = todoFixture();

  assert.ok(panel.todoRowActions(validation).some((item) => item.action === "copy-todo-validation-failed"));
  assert.ok(!panel.todoRowActions(ordinary).some((item) => item.action === "copy-todo-validation-failed"));

  panel.openContextMenu(validation.todo_key, 500, 400, { querySelector: () => null });
  assert.match(documentRef.menu.className, /has-5-actions/);
  const button = documentRef.menu.buttons.find((item) => item.dataset.action === "copy-todo-validation-failed");
  panel.handleClick({ target: button });
  assert.deepEqual(calls, [[validation.todo_key, "validation_failed"]]);
});

test("persisted todo groups render as contiguous ordered table-safe clusters", () => {
  const { elements, panel } = fixturePanel();
  const second = {
    ...todoFixture(),
    todo_key: "ordered-second",
    group_key: "ordered-group",
    group_title: "Ordered group",
    group_position: 2,
    group_size: 2,
  };
  const first = {
    ...todoFixture(),
    todo_key: "ordered-first",
    group_key: "ordered-group",
    group_title: "Ordered group",
    group_position: 1,
    group_size: 2,
  };
  panel.state.latest.open_todos = [second, todoFixture(), first];
  panel.renderCurrent();
  let html = elements.get("openTodos").innerHTML;
  assert.match(html, /<tbody class="todo-group-body" data-group-key="ordered-group">/);
  assert.match(html, /<tr class="todo-group-heading"><th colspan="4"><span>Ordered group/);
  assert.ok(html.indexOf("ordered-first") < html.indexOf("ordered-second"));
  assert.match(html, /group-position-badge[^>]*>1\/2</);
  assert.match(html, /group-position-badge[^>]*>2\/2</);
  assert.equal((html.match(/<tr class="todo-row[^>]*data-todo-key="ordered-(?:first|second)"/g) || []).length, 2);

  const badge = {
    dataset: { action: "open-group-order", groupKey: "ordered-group", todoKey: "ordered-first" },
    closest: (selector) => selector === "[data-action]" ? badge : null,
  };
  panel.handleClick({ target: badge });
  html = elements.get("openTodos").innerHTML;
  assert.match(html, /data-action="move-group-first"/);
  assert.match(html, /data-action="move-group-up"/);
  assert.match(html, /data-action="move-group-down"/);
  assert.match(html, /data-action="move-group-last"/);
  assert.equal((html.match(/class="todo-group-order-editor"/g) || []).length, 1);
  assert.match(html, /data-todo-key="ordered-first"[^>]*aria-label="Todo 1 of 2 in group ordered-group" aria-expanded="true"/);
});

test("group position actions persist the complete order and rerender it", async () => {
  const { elements, panel } = fixturePanel();
  const first = {
    ...todoFixture(),
    todo_key: "ordered-first",
    group_key: "ordered-group",
    group_title: "Ordered group",
    group_position: 1,
    group_size: 2,
  };
  const second = {
    ...todoFixture(),
    todo_key: "ordered-second",
    group_key: "ordered-group",
    group_title: "Ordered group",
    group_position: 2,
    group_size: 2,
  };
  const requests = [];
  panel.state.latest.open_todos = [first, second];
  panel.api = {
    todoGroup: async () => ({ group: { members: [first, second] } }),
    reorderTodoGroup: async (body) => {
      requests.push(body);
      return {
        group: {
          members: [
            { ...second, group_position: 1 },
            { ...first, group_position: 2 },
          ],
        },
      };
    },
  };
  panel.onRefresh = async () => {};

  await panel.moveGroupMember("ordered-group", "ordered-second", "first");

  assert.deepEqual(requests, [{
    group_key: "ordered-group",
    todo_keys: ["ordered-second", "ordered-first"],
    actor: "dashboard",
  }]);
  const html = elements.get("openTodos").innerHTML;
  assert.ok(html.indexOf("ordered-second") < html.indexOf("ordered-first"));
});
