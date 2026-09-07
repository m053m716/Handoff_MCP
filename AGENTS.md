<!-- handoff-mcp:begin (managed by `handoff-mcp init --vscode`) -->
## Handoff MCP — session memory for agents

This repo has the `handoff` MCP server configured (see `.mcp.json` /
`.codex/config.toml`). It is a durable, project-scoped place to leave
breadcrumbs between sessions. Use it instead of re-deriving context.

**At the start of a session**, call `handoff_list` to reload where prior work
stopped and what to do next, and `todo_list` for outstanding next steps. This is
cheaper and more reliable than re-reading the whole transcript.

**While working**, when you find something that must be done but is not the
current focus, call `todo_add` rather than holding it in the conversation.

**When context gets heavy** (stale greps, large logs, finished sub-tasks pile
up), call `context_report`, then `context_compact` — it returns a
summarise-then-handoff procedure and can persist the summary as a handoff in one
call.

**At the end of a work chunk**, call `handoff_add` with a summary, next steps,
and the few key facts (file paths, decisions, gotchas) the next worker needs. A
fresh session can then resume from `handoff_list` alone.

**Close the loop** with `todo_update` (done/dropped) and `handoff_resolve` so
the open lists stay a true worklist.

| Tool | Use it to |
| --- | --- |
| `handoff_list` | Reload breadcrumbs at session start. |
| `handoff_add` | Record where you stopped and what is next. |
| `handoff_resolve` | Mark a handoff done. |
| `todo_add` / `todo_list` / `todo_update` | Track next-step TODOs. |
| `project_status` | Counts of open todos and handoffs. |
| `context_report` / `context_compact` | Notice and shrink a bloated context window. |

Every tool is scoped to this project only; there is no way to reach another
project's data. Full reference: `docs/TOOL_GUIDE.md`.
<!-- handoff-mcp:end -->
