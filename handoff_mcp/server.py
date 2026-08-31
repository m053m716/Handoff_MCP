"""Line-delimited JSON-RPC MCP server over stdio.

No sockets, no HTTP: the server reads one JSON object per line from stdin and
writes one JSON object per line to stdout, which is the standard MCP stdio
transport. All records are scoped to the project resolved from the working
directory at startup (see :mod:`handoff_mcp.project`).
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .project import Project, resolve_project
from .storage import Store, default_db_path
from .tools import ToolError, call_tool, tool_definitions

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}

INSTRUCTIONS = (
    "Project-scoped TODO and session-handoff store for THIS repository. "
    "At the start of a session call `handoff_list` to reload outstanding "
    "breadcrumbs instead of re-deriving context. Record where you leave off "
    "with `handoff_add`, and track concrete next steps with `todo_add`. "
    "When your context window fills with stale search results or large logs, "
    "call `context_report` then `context_compact` for a reduction procedure. "
    "Every record is isolated to this project; there is no cross-project access."
)


class HandoffServer:
    """A minimal MCP application bound to one project's :class:`Store`."""

    def __init__(self, project: Project | None = None, store: Store | None = None) -> None:
        self.project = project or resolve_project()
        self.store = store or Store(self.project.key)

    # -- JSON-RPC ------------------------------------------------------------

    def handle(self, message: Any) -> Any:
        if isinstance(message, list):
            return [r for r in (self.handle(item) for item in message) if r is not None]
        if not isinstance(message, dict):
            return _error(None, -32600, "Invalid Request")

        request_id = message.get("id")
        has_id = "id" in message
        try:
            if message.get("jsonrpc") != "2.0":
                raise ProtocolError(-32600, "Invalid Request")
            method = message.get("method")
            if not isinstance(method, str):
                raise ProtocolError(-32600, "Invalid Request")
            params = message.get("params", {}) or {}
            if not isinstance(params, dict):
                raise ProtocolError(-32602, "Params must be an object.")
            result = self.dispatch(method, params)
            if not has_id:
                return None
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except ProtocolError as exc:
            return None if not has_id else _error(request_id, exc.code, exc.message)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"{__package__} server error: {exc}", file=sys.stderr, flush=True)
            return None if not has_id else _error(request_id, -32603, "Internal error")

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "handoff-mcp",
                    "title": "Handoff / TODO MCP",
                    "version": __version__,
                    "description": "Project-scoped TODO and handoff store over stdio.",
                },
                "instructions": INSTRUCTIONS,
            }
        if method in {"notifications/initialized", "ping", "logging/setLevel"}:
            return {}
        if method == "tools/list":
            return {"tools": tool_definitions()}
        if method == "tools/call":
            return self.handle_tool_call(params)
        if method == "prompts/list":
            return {"prompts": []}
        if method == "resources/list":
            return {"resources": []}
        raise ProtocolError(-32601, f"Method not found: {method}")

    def handle_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise ProtocolError(-32602, "`name` must be a string.")
        arguments = params.get("arguments", {}) or {}
        if not isinstance(arguments, dict):
            raise ProtocolError(-32602, "`arguments` must be an object.")
        try:
            structured = call_tool(self.store, name, arguments)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {
            "content": [{"type": "text", "text": json.dumps(structured, indent=2, ensure_ascii=False)}],
            "structuredContent": structured,
            "isError": False,
        }


class ProtocolError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _configure_stdio_utf8() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, TypeError, ValueError):
                pass


def run_stdio(server: HandoffServer | None = None) -> None:
    """Serve MCP over stdio until stdin closes."""

    _configure_stdio_utf8()
    server = server or HandoffServer()
    print(
        f"handoff-mcp stdio ready | project={server.project.label} "
        f"({server.project.key}) | db={default_db_path()}",
        file=sys.stderr,
        flush=True,
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = _error(None, -32700, "Parse error")
        else:
            response = server.handle(message)
        if response is not None and response != []:
            print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)


__all__ = ["HandoffServer", "ProtocolError", "run_stdio", "PROTOCOL_VERSION"]
