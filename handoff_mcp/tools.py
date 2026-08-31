"""Tool definitions and handlers for the Handoff / TODO MCP server.

Two families of tools:

* **Records** — create and manage project-scoped todos and handoffs. These
  read and write the SQLite store and are the reason the server exists.

* **Context hygiene** — ``context_report`` and ``context_compact`` return
  *guidance text* for the calling agent. An MCP server cannot reach into the
  client's conversation and delete tokens; what it can do is give the agent a
  concrete, correct procedure for shrinking its own context (summarise-then-
  handoff, drop stale tool output, restart with a handoff breadcrumb). These
  tools package that procedure so it is one call away and always consistent.

Every handler receives the already-resolved, project-scoped :class:`Store`, so
isolation is total: a handler literally has no way to address another project.
"""

from __future__ import annotations

from typing import Any, Callable

from .storage import HANDOFF_STATUSES, TODO_STATUSES, Store


class ToolError(Exception):
    """Raised for user-facing, non-protocol tool failures (bad arguments etc.)."""


Handler = Callable[[Store, dict[str, Any]], dict[str, Any]]


# --------------------------------------------------------------------------- #
# Argument helpers
# --------------------------------------------------------------------------- #

def _require_str(args: dict[str, Any], key: str, *, max_length: int = 4000) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"`{key}` is required and must be a non-empty string.")
    text = value.strip()
    if len(text) > max_length:
        raise ToolError(f"`{key}` must be at most {max_length} characters.")
    return text


def _opt_str(args: dict[str, Any], key: str, *, max_length: int = 8000) -> str:
    value = args.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ToolError(f"`{key}` must be a string.")
    text = value.strip()
    if len(text) > max_length:
        raise ToolError(f"`{key}` must be at most {max_length} characters.")
    return text


def _opt_int(args: dict[str, Any], key: str, *, default: int, lo: int, hi: int) -> int:
    value = args.get(key, default)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"`{key}` must be an integer between {lo} and {hi}.")
    if not lo <= number <= hi:
        raise ToolError(f"`{key}` must be between {lo} and {hi}.")
    return number


def _seq(args: dict[str, Any], key: str) -> int:
    """Accept ``3``, ``\"3\"``, ``\"T-3\"`` or ``\"H-3\"`` and return the integer 3."""
    value = args.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        text = value.strip().upper()
        if text[:2] in ("T-", "H-"):
            text = text[2:]
        if text.isdigit():
            return int(text)
    raise ToolError(f"`{key}` must be an id like 3, \"T-3\", or \"H-3\".")


# --------------------------------------------------------------------------- #
# Record tools
# --------------------------------------------------------------------------- #

def todo_add(store: Store, args: dict[str, Any]) -> dict[str, Any]:
    todo = store.add_todo(
        title=_require_str(args, "title", max_length=300),
        detail=_opt_str(args, "detail"),
        priority=_opt_int(args, "priority", default=3, lo=1, hi=5),
        tags=",".join(_tag_list(args)),
    )
    return {"todo": todo}


def todo_list(store: Store, args: dict[str, Any]) -> dict[str, Any]:
    status = _status_filter(args, TODO_STATUSES, default="open")
    limit = _opt_int(args, "limit", default=50, lo=1, hi=200)
    todos = store.list_todos(status=status, limit=limit)
    return {"todos": todos, "count": len(todos), "status_filter": status or "all"}


def todo_update(store: Store, args: dict[str, Any]) -> dict[str, Any]:
    seq = _seq(args, "id")
    try:
        todo = store.update_todo(
            seq,
            status=_opt_enum(args, "status", TODO_STATUSES),
            priority=(None if "priority" not in args else _opt_int(args, "priority", default=3, lo=1, hi=5)),
            title=(None if "title" not in args else _require_str(args, "title", max_length=300)),
            detail=(None if "detail" not in args else _opt_str(args, "detail")),
            tags=(None if "tags" not in args else ",".join(_tag_list(args))),
        )
    except KeyError as exc:
        raise ToolError(str(exc)) from exc
    return {"todo": todo}


def handoff_add(store: Store, args: dict[str, Any]) -> dict[str, Any]:
    handoff = store.add_handoff(
        summary=_require_str(args, "summary", max_length=600),
        next_steps=_opt_str(args, "next_steps"),
        context=_opt_str(args, "context"),
        references=_opt_str(args, "references", max_length=4000),
        author=_opt_str(args, "author", max_length=200),
    )
    return {"handoff": handoff}


def handoff_list(store: Store, args: dict[str, Any]) -> dict[str, Any]:
    status = _status_filter(args, HANDOFF_STATUSES, default="open")
    limit = _opt_int(args, "limit", default=50, lo=1, hi=200)
    handoffs = store.list_handoffs(status=status, limit=limit)
    return {"handoffs": handoffs, "count": len(handoffs), "status_filter": status or "all"}


def handoff_resolve(store: Store, args: dict[str, Any]) -> dict[str, Any]:
    seq = _seq(args, "id")
    try:
        handoff = store.update_handoff(seq, status="resolved")
    except KeyError as exc:
        raise ToolError(str(exc)) from exc
    return {"handoff": handoff}


def project_status(store: Store, args: dict[str, Any]) -> dict[str, Any]:
    return {"counts": store.counts()}


# --------------------------------------------------------------------------- #
# Context-hygiene tools (guidance, not mutation of the client's context)
# --------------------------------------------------------------------------- #

def context_report(store: Store, args: dict[str, Any]) -> dict[str, Any]:
    """Return a checklist for noticing when a session's context is bloated."""

    return {
        "purpose": (
            "This tool does not read your conversation. It returns a checklist so "
            "you can self-assess whether your context window is carrying dead weight."
        ),
        "signs_of_bloated_context": [
            "A grep / search / file read pulled in hundreds of lines you have already used.",
            "Large tool outputs (build logs, test dumps, directory listings) sit far above the current task.",
            "You are repeating or re-deriving facts already established earlier.",
            "The conversation spans several distinct sub-tasks that are now finished.",
        ],
        "what_to_do": (
            "When two or more signs hold, call `context_compact` for the step-by-step "
            "reduction procedure, then act on it."
        ),
    }


def context_compact(store: Store, args: dict[str, Any]) -> dict[str, Any]:
    """Return the summarise-then-handoff procedure for shrinking context.

    Optionally persists a handoff breadcrumb so the compacted state survives a
    fresh session. Pass ``summary`` (and optionally ``next_steps``) to record it.
    """

    saved_handoff = None
    summary = _opt_str(args, "summary", max_length=600)
    if summary:
        saved_handoff = store.add_handoff(
            summary=summary,
            next_steps=_opt_str(args, "next_steps"),
            context=_opt_str(args, "context"),
            author=_opt_str(args, "author", max_length=200) or "context_compact",
        )

    return {
        "note": (
            "An MCP tool cannot delete tokens from your context window; only the "
            "client can. This procedure lets you reclaim that space yourself."
        ),
        "procedure": [
            "1. Summarise the finished work into 3-6 durable bullet points (decisions, file paths, results).",
            "2. Record the summary as a handoff with `handoff_add` (or pass `summary` to this tool) so it is durable.",
            "3. Note any still-open next steps as todos with `todo_add` so nothing is lost.",
            "4. Stop re-reading large tool outputs; refer to the summary/handoff instead of re-running the search.",
            "5. If your client supports it, start a fresh session and open with `handoff_list` to reload only the breadcrumb.",
        ],
        "avoid_future_bloat": [
            "Scope searches tightly (specific paths, `head_limit`, narrow patterns) instead of broad greps.",
            "Read only the lines you need with offset/limit rather than whole large files.",
            "Prefer a stored handoff over pasting long transcripts between sessions.",
        ],
        "saved_handoff": saved_handoff,
    }


# --------------------------------------------------------------------------- #
# small shared helpers
# --------------------------------------------------------------------------- #

def _tag_list(args: dict[str, Any]) -> list[str]:
    value = args.get("tags")
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, list):
        parts = value
    else:
        raise ToolError("`tags` must be a comma-separated string or a list of strings.")
    return [str(p).strip() for p in parts if str(p).strip()]


def _opt_enum(args: dict[str, Any], key: str, allowed: tuple[str, ...]) -> str | None:
    if key not in args or args[key] is None:
        return None
    value = args[key]
    if value not in allowed:
        raise ToolError(f"`{key}` must be one of {allowed}.")
    return value


def _status_filter(args: dict[str, Any], allowed: tuple[str, ...], *, default: str) -> str | None:
    value = args.get("status", default)
    if value in (None, "", "all"):
        return None
    if value not in allowed:
        raise ToolError(f"`status` must be one of {allowed} or \"all\".")
    return value


# --------------------------------------------------------------------------- #
# Registry: tool name -> (handler, JSON schema definition)
# --------------------------------------------------------------------------- #

HANDLERS: dict[str, Handler] = {
    "todo_add": todo_add,
    "todo_list": todo_list,
    "todo_update": todo_update,
    "handoff_add": handoff_add,
    "handoff_list": handoff_list,
    "handoff_resolve": handoff_resolve,
    "project_status": project_status,
    "context_report": context_report,
    "context_compact": context_compact,
}


def tool_definitions() -> list[dict[str, Any]]:
    """Return the ``tools/list`` payload."""

    return [
        {
            "name": "todo_add",
            "description": (
                "Add a short next-step TODO for THIS project. Use for concrete, actionable "
                "items a worker can pick up. Keep the title one line; put detail in `detail`."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "One-line actionable item."},
                    "detail": {"type": "string", "description": "Optional longer description."},
                    "priority": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "1 (highest) to 5 (lowest). Default 3.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional labels, e.g. [\"bug\", \"docs\"].",
                    },
                },
                "required": ["title"],
            },
        },
        {
            "name": "todo_list",
            "description": "List TODOs for THIS project, ordered by priority. Defaults to open items.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["open", "done", "dropped", "all"],
                        "description": "Filter by status. Default \"open\".",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
        },
        {
            "name": "todo_update",
            "description": (
                "Update a TODO by id (e.g. \"T-3\"). Mark done/dropped, re-prioritise, or edit text."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "TODO id, e.g. \"T-3\" or 3."},
                    "status": {"type": "string", "enum": list(TODO_STATUSES)},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id"],
            },
        },
        {
            "name": "handoff_add",
            "description": (
                "Record a session handoff for THIS project: where work was left off and what the "
                "next worker should do. This is the breadcrumb another agent session reloads to "
                "resume without re-reading the whole transcript."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "What was accomplished / current state."},
                    "next_steps": {"type": "string", "description": "What the next worker should do."},
                    "context": {"type": "string", "description": "Key decisions, gotchas, file paths."},
                    "references": {"type": "string", "description": "Relevant files, links, ids."},
                    "author": {"type": "string", "description": "Optional author/session label."},
                },
                "required": ["summary"],
            },
        },
        {
            "name": "handoff_list",
            "description": (
                "List handoffs for THIS project, newest first. Call this at the START of a session "
                "to reload outstanding breadcrumbs instead of re-deriving context."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["open", "resolved", "all"],
                        "description": "Filter by status. Default \"open\".",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
        },
        {
            "name": "handoff_resolve",
            "description": "Mark a handoff (e.g. \"H-2\") resolved once its work is done.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Handoff id, e.g. \"H-2\" or 2."}},
                "required": ["id"],
            },
        },
        {
            "name": "project_status",
            "description": "Return counts of open todos and handoffs for THIS project.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "context_report",
            "description": (
                "Return a checklist for judging whether your context window is carrying dead weight "
                "(stale greps, large logs, finished sub-tasks). Read the guide before relying on it."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "context_compact",
            "description": (
                "Return the step-by-step procedure to reduce unneeded session context (summarise, "
                "store a handoff, drop stale tool output, optionally restart from the breadcrumb). "
                "Pass `summary` to also persist a handoff in one call."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "If given, also stored as a handoff breadcrumb."},
                    "next_steps": {"type": "string"},
                    "context": {"type": "string"},
                    "author": {"type": "string"},
                },
            },
        },
    ]


def call_tool(store: Store, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"Unknown tool: {name}")
    return handler(store, arguments)


__all__ = ["ToolError", "tool_definitions", "call_tool", "HANDLERS"]
