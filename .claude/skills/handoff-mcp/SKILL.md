---
name: handoff-mcp
description: Use the Handoff MCP server for durable, project-scoped TODOs and session handoffs across agent sessions; apply when work needs breadcrumbs, next steps, or context-reduction guidance.
metadata:
  short-description: Persist project handoffs and next-step TODOs
---

# Handoff MCP

Use the `handoff` MCP server to keep durable session memory scoped to the current
repository. The server stores TODOs and handoffs in its project-local store and
does not read or modify the conversation itself.

## Session workflow

1. At the start of a session, call `handoff_list` and `todo_list` before
   re-deriving context from the repository.
2. Use `todo_add` for concrete work that remains open. Keep each title short;
   add useful detail, priority, and tags when needed.
3. Use `handoff_add` at the end of a work chunk with the current state, next
   steps, key decisions, and relevant file paths.
4. Resolve completed breadcrumbs with `handoff_resolve` and mark finished or
   dropped TODOs with `todo_update`.
5. When the active context is bloated, call `context_report`, then
   `context_compact`; pass `summary` to save the compacted state as a handoff.

Keep records factual and repository-scoped. Do not use this skill as a generic
note-taking system for unrelated projects, and do not treat `context_compact`
as a tool that can delete the client conversation.
