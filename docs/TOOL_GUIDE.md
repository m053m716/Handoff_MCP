# Tool guide

How to use the `handoff-mcp` tools well, aimed at the agent calling them. Every tool operates only on the current project; there is no way to reach another project's data.

## Recommended session workflow

1. **Start of session — reload, don't re-derive.** Call `handoff_list` first. Open handoffs are the durable memory of where prior work stopped and what to do next. This is cheaper and more reliable than re-reading the whole transcript or re-searching the codebase.
2. **During work — capture next steps as you find them.** When you discover something that must be done but is not the current focus, call `todo_add` instead of holding it in the conversation. A TODO out of context is a TODO that survives compaction.
3. **When context gets heavy — compact deliberately.** If searches and logs are piling up, call `context_report`, then `context_compact`. See [Reducing session context](#reducing-session-context).
4. **End of a work chunk — leave a breadcrumb.** Call `handoff_add` with a summary, next steps, and the few key facts (file paths, decisions, gotchas) the next worker needs. Then a fresh session can resume from `handoff_list` alone.
5. **Close the loop.** Mark todos done with `todo_update` and handoffs resolved with `handoff_resolve` so the open list stays a true worklist.

## Record tools

### `todo_add`
Add one concrete, actionable next step.

| arg | type | notes |
| --- | --- | --- |
| `title` | string (required) | One line. The imperative action. |
| `detail` | string | Optional longer description. |
| `priority` | integer 1–5 | 1 = highest, default 3. Lists sort by priority. |
| `tags` | array of strings | Optional labels, e.g. `["bug", "docs"]`. |

Returns the created todo, including its id (`T-4`).

### `todo_list`
| arg | type | notes |
| --- | --- | --- |
| `status` | `open` \| `done` \| `dropped` \| `all` | Default `open`. |
| `limit` | integer 1–200 | Default 50. |

Ordered by priority ascending, then id. Prefer `status: "open"` (the default) to keep the payload small.

### `todo_update`
| arg | type | notes |
| --- | --- | --- |
| `id` | string (required) | `T-3`, `3`, or `"3"` all work. |
| `status` | `open` \| `done` \| `dropped` | |
| `priority` | integer 1–5 | |
| `title` / `detail` / `tags` | | Edit in place. |

### `handoff_add`
Record where work was left off and what should happen next. This is the higher-value record — invest a sentence or two.

| arg | type | notes |
| --- | --- | --- |
| `summary` | string (required) | What was accomplished / current state. |
| `next_steps` | string | What the next worker should do. |
| `context` | string | Key decisions, gotchas, file paths. |
| `references` | string | Relevant files, links, ids. |
| `author` | string | Optional session/agent label. |

### `handoff_list`
| arg | type | notes |
| --- | --- | --- |
| `status` | `open` \| `resolved` \| `all` | Default `open`. |
| `limit` | integer 1–200 | Default 50. Newest first. |

### `handoff_resolve`
Mark a handoff resolved once its work is complete. Arg: `id` (e.g. `H-2`).

### `project_status`
No arguments. Returns `{ "counts": { "open_todos": N, "open_handoffs": M } }`. A cheap way to check whether there is outstanding work before diving in.

## Reducing session context

Long agent sessions accumulate tokens that no longer serve the current task: the output of a grep you already used, a build log from ten steps ago, a directory listing, a finished sub-task. That dead weight crowds out room for real work and can degrade attention on what matters.

**Important limitation, stated plainly:** an MCP server cannot delete tokens from your context window. Only the client can. What these tools give you is (a) a way to *notice* the problem and (b) a *correct procedure* to reclaim the space yourself, with the durable parts saved so nothing is lost.

### `context_report`
No arguments. Returns a checklist of signs your context is bloated:

- A search/read pulled in far more text than you ended up using.
- Large tool outputs sit far above the current task.
- You are re-deriving facts already established earlier.
- The conversation spans several now-finished sub-tasks.

If two or more hold, compact.

### `context_compact`
Returns the reduction procedure. Optionally persists a handoff in the same call.

| arg | type | notes |
| --- | --- | --- |
| `summary` | string | If given, also stored as a handoff breadcrumb. |
| `next_steps` / `context` / `author` | string | Recorded with the handoff. |

The procedure it returns:

1. **Summarise** the finished work into 3–6 durable bullet points (decisions, file paths, results).
2. **Persist** that summary as a handoff (`handoff_add`, or pass `summary` to `context_compact`) so it survives even a full restart.
3. **Record open next steps** as todos (`todo_add`) so nothing is dropped.
4. **Stop re-reading** the large tool outputs — refer to the summary instead of re-running the search.
5. **Restart if supported.** If your client can start a fresh session, do so and open with `handoff_list` to reload only the breadcrumb — a clean window carrying just the summary, not the full history.

### Avoiding bloat in the first place

The cheapest compaction is the search you never over-fetch:

- **Scope searches tightly.** Specific paths, narrow patterns, and a `head_limit` beat a broad grep that returns hundreds of lines. If you only need to know *whether* something exists, request counts or file names, not full content.
- **Read the lines you need.** Use offset/limit on large files rather than reading the whole thing.
- **Prefer a stored handoff over a pasted transcript.** When moving work between sessions, a `handoff_add` breadcrumb carries the signal; a pasted transcript carries the signal *and* all the noise.
- **Convert findings to records early.** A fact written to a todo or handoff is a fact you can safely forget in the live conversation.

## Ids

- Todos: `T-<n>`; handoffs: `H-<n>`. Numbering is per project and monotonic, so ids are stable and safe to quote in prose or paste into another session.
- Anywhere an `id` is accepted, `T-3`, `H-3`, `3`, and `"3"` are all understood.
