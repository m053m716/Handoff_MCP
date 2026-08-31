const openTodosPanel = () => `
  <section id="openTodosSection">
    <div class="section-head">
      <div class="section-title">
        <h2>Open Todos</h2>
        <span class="muted" id="todoCount"></span>
      </div>
      <div class="section-actions">
        <label class="filter-control" for="todoScopeFilter">
          <span>Scope</span>
          <select id="todoScopeFilter" aria-label="Filter open todos by scope">
            <option value="__all__">All scopes</option>
          </select>
        </label>
        <label class="filter-control" for="todoStatusFilter">
          <span>Status</span>
          <select id="todoStatusFilter" aria-label="Filter todos by status">
            <option value="open">Open</option>
            <option value="suggested">Suggested</option>
            <option value="accepted">Accepted</option>
            <option value="in_progress">In progress</option>
            <option value="blocked">Blocked</option>
            <option value="done">Done</option>
            <option value="dropped">Dropped</option>
          </select>
        </label>
        <label class="filter-control" for="todoPriorityFilter">
          <span>Priority</span>
          <select id="todoPriorityFilter" aria-label="Filter todos by priority">
            <option value="">All priorities</option>
            <option value="P0">P0</option>
            <option value="P1">P1</option>
            <option value="P2">P2</option>
            <option value="P3">P3</option>
            <option value="P4">P4</option>
          </select>
        </label>
        <button class="hud-jump" type="button" data-jump-hud aria-label="Return to dashboard HUD" title="Return to HUD">HUD</button>
      </div>
    </div>
    <div id="openTodos" class="todo-lanes" aria-live="polite"></div>
    <div id="todoGroupAnnouncement" class="sr-only" aria-live="polite"></div>
    <button id="todoLoadMore" type="button" hidden>Load more todos</button>
    <div class="empty" id="todoEmpty" hidden>No open todos recorded.</div>
  </section>`;

const orchestrationQueuePanel = () => `
  <section id="orchestrationQueueSection">
    <div class="section-head">
      <div class="section-title"><h2>Orchestration Queue</h2><span class="muted" id="queueCount"></span></div>
      <div class="section-actions orchestration-head-actions">
        <span id="queuePromptStatus" class="muted" role="status" aria-live="polite"></span>
        <span id="queueWakeStatus" class="muted" role="status" aria-live="polite"></span>
        <button id="queuePromptButton" type="button" data-action="copy-orchestrator-prompt">Prompt</button>
        <button id="stopOrchestratorButton" type="button" class="danger" data-action="stop-orchestrator" hidden>Stop Orchestrator</button>
        <button class="hud-jump" type="button" data-jump-hud aria-label="Return to dashboard HUD" title="Return to HUD">HUD</button>
      </div>
    </div>
    <div id="orchestratorAssignment" class="orchestrator-assignment" aria-live="polite"></div>
    <form id="queueAddForm" class="queue-add-form">
      <input id="queueTodoSearch" name="search" type="search" placeholder="Search open todos" autocomplete="off" aria-label="Search open todos to queue">
      <select id="queueTodoSelect" name="todo_key" aria-label="Open todo to queue"></select>
      <select id="queueAssignmentSelect" name="assignment_key" aria-label="Designated orchestrator"></select>
      <button type="submit" class="primary" aria-label="Add selected todo to orchestration queue">+ Queue</button>
    </form>
    <div id="queueError" class="form-error" hidden></div>
    <div class="table-wrap bounded-table-wrap queue-table-wrap" tabindex="0" role="region" aria-label="Scrollable orchestration queue table">
      <table class="queue-table"><thead><tr>
        <th class="col-status">State</th><th class="col-task">Todo</th><th>Assignment and prompt</th>
        <th class="col-status">Checkpoint</th><th class="col-time">Updated</th><th class="col-action">Actions</th>
      </tr></thead><tbody id="orchestrationQueue"></tbody></table>
    </div>
    <div class="empty" id="queueEmpty" hidden>No orchestration work queued.</div>
  </section>`;

const taskPanels = () => `
  <section id="activeTasksSection">
    <div class="section-head">
      <div class="section-title"><h2>Active Agent Tasks</h2><span class="muted" id="activeCount"></span></div>
      <button class="hud-jump" type="button" data-jump-hud aria-label="Return to dashboard HUD" title="Return to HUD">HUD</button>
    </div>
    <div class="table-wrap bounded-table-wrap compact-table-wrap" tabindex="0" role="region" aria-label="Scrollable active agent tasks table">
      <table class="active-tasks-table"><thead><tr>
        <th class="col-status">Status</th><th class="col-task">Task</th><th class="col-agent">Agent</th>
        <th class="active-summary-col">Summary</th><th class="col-time">Last Seen</th><th class="col-duration">Duration</th>
        <th class="col-time">Expires</th><th class="col-action">Actions</th>
      </tr></thead><tbody id="activeTasks"></tbody></table>
    </div>
    <div class="empty" id="activeEmpty" hidden>No active task check-ins.</div>
  </section>
  <section id="recentTasksSection">
    <div class="section-head">
      <div class="section-title"><h2>Recent Task State</h2><span class="muted" id="recentCount"></span></div>
      <button class="hud-jump" type="button" data-jump-hud aria-label="Return to dashboard HUD" title="Return to HUD">HUD</button>
    </div>
    <div class="table-wrap bounded-table-wrap compact-table-wrap" tabindex="0" role="region" aria-label="Scrollable recent task state table">
      <table><thead><tr>
        <th class="col-status">Status</th><th class="col-task">Task</th><th class="col-agent-sm">Agent</th>
        <th>Current Step</th><th class="col-duration">Duration</th><th class="col-time">Updated</th><th class="col-action">Actions</th>
      </tr></thead><tbody id="recentTasks"></tbody></table>
    </div>
    <div class="empty" id="recentEmpty" hidden>No task state has been recorded.</div>
  </section>
  <section id="eventsSection">
    <div class="section-head">
      <div class="section-title"><h2>Recent Check-In Events</h2><span class="muted" id="eventCount"></span></div>
      <button class="hud-jump" type="button" data-jump-hud aria-label="Return to dashboard HUD" title="Return to HUD">HUD</button>
    </div>
    <div class="table-wrap bounded-table-wrap" tabindex="0" role="region" aria-label="Scrollable recent check-in events table">
      <table><thead><tr>
        <th class="col-time">Time</th><th class="col-action">Action</th><th class="col-status">Status</th>
        <th class="col-task">Task</th><th class="col-agent-sm">Agent</th><th>Summary</th>
      </tr></thead><tbody id="taskEvents"></tbody></table>
    </div>
    <div class="empty" id="eventsEmpty" hidden>No task events recorded.</div>
  </section>`;

const analyticsPanel = () => `
  <section id="modelUsageSection">
    <div class="section-head">
      <div class="section-title"><h2>Agent Model Usage</h2><span class="muted" id="modelUsageCount"></span></div>
      <button class="hud-jump" type="button" data-jump-hud aria-label="Return to dashboard HUD" title="Return to HUD">HUD</button>
    </div>
    <div class="model-usage-body">
      <div class="model-usage-summary">
        <div class="model-usage-chart" id="modelUsageChart"></div>
        <div class="model-usage-legend" id="modelUsageLegend"></div>
        <div class="complexity-distribution" id="complexityDistributionPanel">
          <div class="complexity-head"><h3>Task Complexity</h3><span class="muted" id="complexityDistributionCount"></span></div>
          <div class="complexity-source-controls" id="complexitySourceControls" role="group" aria-label="Task complexity source"></div>
          <div class="complexity-bars" id="complexityDistributionBars"></div>
          <div class="empty" id="complexityDistributionEmpty" hidden>No complexity data on recent tasks.</div>
        </div>
      </div>
      <div class="model-usage-tokens" id="tokenUsagePanel">
        <div class="token-usage-head"><h3>Token Usage</h3><span class="muted" id="tokenUsageCount"></span></div>
        <div class="token-usage-bars" id="tokenUsageBars"></div>
        <div class="token-recommendations" id="tokenRecommendations"></div>
        <div class="empty" id="tokenUsageEmpty" hidden>No token usage submitted yet.</div>
        <div class="token-category-dialog-host" id="tokenCategoryDialog" hidden></div>
        <div class="token-category-error form-error" id="tokenCategoryError" role="alert" hidden></div>
      </div>
      <div class="model-usage-tokens" id="guidanceRegistryPanel">
        <div class="token-usage-head"><h3>Guidance Evidence Registry</h3><span class="muted" id="guidanceRegistryCount"></span></div>
        <p class="muted">Evidence is not proof. Reconciliation records a decision; it never edits instruction files.</p>
        <div id="guidanceRegistryRows"></div>
      </div>
    </div>
    <div class="oscilloscope" id="oscilloscope">
      <div class="osc-head"><h3>Token Usage Over Time</h3><div class="osc-controls" id="oscilloscopeControls" hidden></div></div>
      <div class="osc-body">
        <div class="osc-labels" id="oscilloscopeLabels"></div>
        <div class="osc-scroll" id="oscilloscopeScroll" tabindex="0" role="region" aria-label="Scrollable token-usage time-series plot">
          <div class="osc-chart" id="oscilloscopeChart"></div>
        </div>
      </div>
      <div class="empty" id="oscilloscopeEmpty" hidden>No token usage submitted yet.</div>
    </div>
    <div class="efficiency" id="efficiencyPanel">
      <div class="efficiency-head"><h3>Model Efficiency</h3><span class="muted" id="efficiencyCount"></span></div>
      <div class="efficiency-kpis" id="efficiencyKpis"></div>
      <div class="efficiency-legend" id="efficiencyLegend"></div>
      <div class="efficiency-chart" id="efficiencyChart"></div>
      <div class="empty" id="efficiencyEmpty" hidden>Efficiency data accumulates as todos are pruned with an actual complexity and their tasks report token usage.</div>
    </div>
    <div class="empty" id="modelUsageEmpty" hidden>No agent task state recorded yet.</div>
  </section>`;

const documentationPanels = () => `
  <div class="docs-layout">
    <section id="docsSection">
      <div class="section-head">
        <div class="section-title"><h2>Documentation Index</h2><span class="muted" id="docsCount"></span></div>
        <button class="hud-jump" type="button" data-jump-hud aria-label="Return to dashboard HUD" title="Return to HUD">HUD</button>
      </div>
      <div class="table-wrap bounded-table-wrap compact-table-wrap" tabindex="0" role="region" aria-label="Scrollable documentation index table">
        <table><thead><tr><th class="col-agent">Scope</th><th>Title</th><th class="col-count">Docs</th><th class="col-count">Chunks</th><th class="col-action">Actions</th></tr></thead>
          <tbody id="docScopes"></tbody></table>
      </div>
      <div id="docsDriftWarnings" class="docs-drift" hidden></div>
    </section>
    <section id="docSearchSection" class="doc-workbench-section">
      <div class="section-head">
        <div class="section-title"><h2>Documentation Workbench</h2><span class="muted" id="docSearchStatus"></span></div>
        <button class="hud-jump" type="button" data-jump-hud aria-label="Return to dashboard HUD" title="Return to HUD">HUD</button>
      </div>
      <form class="doc-search-form" id="docSearchForm">
        <select id="docScope" name="scope" aria-label="Documentation scope"><option value="">All scopes</option></select>
        <select id="docTodoTarget" name="todo_key" aria-label="Todo target for doc refs"><option value="">Todo target</option></select>
        <input id="docQuery" name="query" placeholder="Search docs" autocomplete="off">
        <select id="docLimit" name="limit" aria-label="Result limit"><option value="6">6</option><option value="8" selected>8</option><option value="12">12</option><option value="16">16</option></select>
        <button type="submit">Search</button><button id="copyDocSearchPrompt" type="button">Copy Search Prompt</button>
      </form>
      <div id="docTodoHints" class="doc-todo-hints"></div>
      <div class="doc-workbench-grid">
        <div class="doc-results-panel"><ul class="docs-results" id="docResults"></ul><div class="empty" id="docSearchEmpty">No search results.</div></div>
        <aside class="doc-detail" id="docDetail">Select a result.</aside>
      </div>
    </section>
  </div>`;

export function mountDashboardPanels(documentRef = globalThis.document) {
  const mount = documentRef?.getElementById("dashboardPanels");
  if (!mount || mount.dataset.mounted === "true") return mount;
  mount.innerHTML = [openTodosPanel(), orchestrationQueuePanel(), taskPanels(), analyticsPanel(), documentationPanels()].join("\n");
  mount.dataset.mounted = "true";
  return mount;
}
