"""Application-layer MCP orchestration and protocol dispatch.

This module owns the MCP-facing registry and cross-domain composition.  Domain
operations remain injected through ``backend`` while the storage/lifecycle/
TODO/documentation seams are introduced incrementally.  Keeping the layer
transport-free lets both stdio and HTTP use the same protocol behavior.
"""

from __future__ import annotations

import sys
from typing import Any, Callable
from urllib.parse import unquote

from mcp.project import load_project_descriptor
from mcp.orchestration import orchestration_tool_definitions
from mcp.myohid_tests import myohid_test_tool_definitions
from mcp.markdown_sanitize import markdown_sanitize as markdown_sanitize_handler

from mcp.documents import DOC_DRIFT_AUDIT_SCOPE, DOC_FETCH_DEFAULT_CHARS, DOC_FETCH_MAX_CHARS, DOC_SEARCH_MAX_LIMIT, DOC_SCOPES, DOC_SOURCE_TYPES
from mcp.lifecycle import (
    ACTIVE_TASK_STATUSES,
    DEFAULT_TASK_TTL_SECONDS,
    MAX_TASK_TTL_SECONDS,
    MODEL_REGISTRATION_EFFORTS,
    MODEL_REGISTRATION_FAMILIES,
    describe_model_catalog,
    MODEL_REGISTRATION_PROVIDERS,
    TASK_ACTIVE_DEFAULT_LIMIT,
    row_to_task,
    utc_now,
)
from mcp.todos import (
    TODO_ACTIVE_STATUSES,
    TODO_ADDABLE_STATUSES,
    TODO_GROUP_DEFAULT_LIMIT,
    TODO_GROUP_DEFAULT_MIN_SCORE,
    TODO_GROUP_RELATION_KINDS,
    TODO_LIST_DEFAULT_LIMIT,
    TODO_LIST_MAX_LIMIT,
    TODO_PRIORITIES,
    TODO_STATUSES,
)


def dashboard_todo_display_status(todo_status: str, linked_task_status: str = '') -> str:
    status = linked_task_status or todo_status
    return 'active' if status == 'in_progress' else status


def complexity_numeric(value: Any) -> int | None:
    return {'S': 1, 'M': 2, 'L': 3, 'XL': 4}.get(str(value or '').strip().upper())


class ApplicationProtocolError(Exception):
    """Protocol error used when the application is used without a facade."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class McpApplication:
    """Compose MCP domain services and expose the MCP application contract.

    ``backend`` is the compatibility facade used during the incremental
    server refactor.  New callers may also provide ``store``, ``lifecycle``,
    ``todos``, and ``documents`` explicitly; handlers prefer those services
    when they expose the corresponding operation.
    """

    protocol_version = "2025-11-25"
    supported_protocol_versions = frozenset(
        {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
    )

    _HANDLER_NAMES = {
        "mudra_mcp_status": "status",
        "mudra_note_upsert": "note_upsert",
        "mudra_note_read": "note_read",
        "mudra_note_search": "note_search",
        "mudra_note_delete": "note_delete",
        "mudra_event_log": "event_log",
        "mudra_event_recent": "event_recent",
        "mudra_agent_model_register": "agent_model_register",
        "mudra_task_check_in": "task_check_in",
        "mudra_task_check_out": "task_check_out",
        "mudra_task_status": "task_status",
        "mudra_task_active": "task_active",
        "mudra_task_token_usage": "task_token_usage",
        "mudra_task_token_recommendation": "task_token_recommendation",
        "mudra_guidance_recommendation_add": "guidance_recommendation_add",
        "mudra_guidance_recommendation_list": "guidance_recommendation_list",
        "mudra_guidance_recommendation_reconcile": "guidance_recommendation_reconcile",
        "mudra_todo_add": "todo_add",
        "mudra_todo_list": "todo_list",
        "mudra_todo_prune": "todo_prune",
        "mudra_todo_next_instruction": "todo_next_instruction",
        "mudra_todo_group_related": "todo_group_related",
        "mudra_todo_group_reorder": "todo_group_reorder",
        "mudra_todo_auto_group_related": "todo_auto_group_related",
        "mudra_todo_related_for_assignment": "todo_related_for_assignment",
        "mudra_orchestration_prompt": "orchestration_prompt",
        "mudra_orchestration_designate": "orchestration_designate",
        "mudra_orchestration_list": "orchestration_list",
        "mudra_orchestration_enqueue": "orchestration_enqueue",
        "mudra_orchestration_heartbeat": "orchestration_heartbeat",
        "mudra_orchestration_wake": "orchestration_wake",
        "mudra_orchestration_claim": "orchestration_claim",
        "mudra_orchestration_launch_prepare": "orchestration_launch_prepare",
        "mudra_orchestration_renew": "orchestration_renew",
        "mudra_orchestration_reconcile": "orchestration_reconcile",
        "mudra_orchestration_release": "orchestration_release",
        "mudra_orchestration_delegate": "orchestration_delegate",
        "mudra_orchestration_complete": "orchestration_complete",
        "mudra_orchestration_checkpoint_prepare": "orchestration_checkpoint_prepare",
        "mudra_orchestration_checkpoint_record": "orchestration_checkpoint_record",
        "mudra_orchestration_retry": "orchestration_retry",
        "mudra_orchestration_drop": "orchestration_drop",
        "mudra_orchestration_stop": "orchestration_stop",
        "mudra_orchestration_cancel": "orchestration_cancel",
        "mudra_orchestration_cancel_ack": "orchestration_cancel_ack",
        "mudra_myohid_test_catalog": "myohid_test_catalog",
        "mudra_myohid_test_suite": "myohid_test_suite",
        "mudra_git_status": "git_status",
        "mudra_git_diff": "git_diff",
        "mudra_git_add": "git_add",
        "mudra_git_commit": "git_commit",
        "mudra_git_checkpoint_noop": "git_checkpoint_noop",
        "mudra_git_push": "git_push",
        "mudra_doc_scopes": "doc_scopes",
        "mudra_doc_index_repo": "doc_index_repo",
        "mudra_doc_search": "doc_search",
        "mudra_doc_get": "doc_get",
        "mudra_doc_upsert": "doc_upsert",
        "markdown_sanitize": "markdown_sanitize",
    }
    _NO_ARGUMENT_TOOLS = {"mudra_mcp_status", "mudra_doc_scopes"}

    def __init__(
        self,
        backend: Any | None = None,
        lifecycle: Any | None = None,
        todos: Any | None = None,
        documents: Any | None = None,
        orchestration: Any | None = None,
        myohid_tests: Any | None = None,
        agent_categories: Any | None = None,
        guidance: Any | None = None,
        *,
        store: Any | None = None,
        project: Any | None = None,
        protocol_error_type: type[Exception] = ApplicationProtocolError,
        tool_execution_error_type: type[Exception] = Exception,
    ) -> None:
        self.project = project or load_project_descriptor()
        self.store = store
        self.lifecycle = lifecycle
        self.todos = todos
        self.documents = documents
        self.orchestration = orchestration
        self.myohid_tests = myohid_tests
        self.agent_categories = agent_categories
        self.guidance = guidance
        self.backend = backend
        self._protocol_error_type = protocol_error_type
        self._tool_execution_error_type = tool_execution_error_type
        self._tool_handlers = self._build_tool_registry()

    def _build_tool_registry(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        registry: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        for tool_name, method_name in self._HANDLER_NAMES.items():
            owner = self._service_for(method_name)
            if owner is not None and hasattr(owner, method_name):
                handler = getattr(owner, method_name)
                if tool_name in self._NO_ARGUMENT_TOOLS:
                    registry[tool_name] = lambda _args, handler=handler: handler()
                else:
                    registry[tool_name] = handler
        return registry

    def _service_for(self, method_name: str) -> Any | None:
        if method_name == "status" and self.store is not None:
            return self
        if method_name == "markdown_sanitize":
            return self
        if method_name in {
            "note_upsert",
            "note_read",
            "note_search",
            "note_delete",
            "task_check_in",
            "task_check_out",
            "task_status",
            "task_active",
            "task_token_usage",
            "task_token_recommendation",
            "agent_model_register",
            "event_log",
            "event_recent",
        } and self.lifecycle is not None:
            return self.lifecycle
        if method_name.startswith("todo_") and self.todos is not None:
            return self.todos
        if method_name.startswith("guidance_recommendation_") and self.guidance is not None:
            return self.guidance
        if (method_name.startswith("orchestration_") or method_name.startswith("git_")) and self.orchestration is not None:
            return self.orchestration
        if method_name.startswith("myohid_test_") and self.myohid_tests is not None:
            return self.myohid_tests
        if method_name.startswith("doc_") and self.documents is not None:
            return self.documents
        return self.backend

    def status(self) -> dict[str, Any]:
        with self.store.connection() as conn:
            note_count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            task_state_count = conn.execute("SELECT COUNT(*) FROM agent_task_state").fetchone()[0]
            active_task_count = conn.execute(
                """
                SELECT COUNT(*) FROM agent_task_state
                WHERE status IN ('in_progress', 'paused', 'blocked')
                  AND expires_at > ?
                """,
                (utc_now(),),
            ).fetchone()[0]
            token_usage_count = conn.execute(
                "SELECT COUNT(*) FROM agent_task_token_usage"
            ).fetchone()[0]
            doc_count = conn.execute("SELECT COUNT(*) FROM doc_entries").fetchone()[0]
            doc_chunk_count = conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
            todo_count = conn.execute("SELECT COUNT(*) FROM mcp_todos").fetchone()[0]
            open_todo_count = conn.execute(
                """
                SELECT COUNT(*) FROM mcp_todos
                WHERE status IN ('suggested', 'accepted', 'in_progress', 'blocked', 'queued')
                """
            ).fetchone()[0]
            todo_group_count = conn.execute(
                "SELECT COUNT(*) FROM mcp_todo_groups"
            ).fetchone()[0]
            latest_note = conn.execute(
                "SELECT key, updated_at FROM notes ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            latest_event = conn.execute(
                "SELECT event_type, ts FROM events ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return {
            "server": "mudra-local-mcp",
            "protocol_version": self.protocol_version,
            "repo_root": str(self.project.repo_root),
            "db_path": str(self.store.db_path),
            "db_exists": self.store.db_path.exists(),
            "notes": note_count,
            "events": event_count,
            "task_states": task_state_count,
            "active_tasks": active_task_count,
            "token_usage_rows": token_usage_count,
            "docs": doc_count,
            "doc_chunks": doc_chunk_count,
            "todos": todo_count,
            "open_todos": open_todo_count,
            "todo_groups": todo_group_count,
            "latest_note": dict(latest_note) if latest_note else None,
            "latest_event": dict(latest_event) if latest_event else None,
        }

    def tool_definitions(self) -> list[dict[str, Any]]:
        return self._tool_definitions()

    def resource_definitions(self) -> list[dict[str, Any]]:
        return self._resource_definitions()

    def resource_templates(self) -> list[dict[str, Any]]:
        return self._resource_templates()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = self._tool_handlers.get(name)
        if handler is None:
            raise self._protocol_error_type(-32602, f"Unknown tool: {name}")
        return handler(arguments)

    def markdown_sanitize(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return markdown_sanitize_handler(arguments)

    def dashboard_snapshot(self) -> dict[str, Any]:
        return self._dashboard_snapshot()

    def read_resource(self, uri: str) -> str:
        prefix = f"{self.project.resource_scheme}://"
        routes = (
            ("summary", "summary_markdown", None),
            ("notes", "notes_markdown", {"limit": 100}),
            ("events/recent", "events_markdown", {"limit": 100}),
            ("tasks/active", "tasks_markdown", {"limit": 100}),
            ("docs/scopes", "doc_scopes_markdown", None),
            ("todos/open", "todos_markdown", {"limit": 50}),
        )
        if uri.startswith(prefix):
            path = uri.removeprefix(prefix)
            for route, method_name, kwargs in routes:
                if path == route:
                    return self._markdown(method_name, kwargs)
            parameterized = (
                ("note/", "note_markdown"),
                ("task/", "task_markdown"),
                ("docs/scope/", "docs_scope_markdown"),
                ("docs/search/", "docs_search_markdown"),
                ("doc/", "doc_markdown"),
                ("todo/", "todo_markdown"),
            )
            for route, method_name in parameterized:
                if path.startswith(route):
                    return self._markdown(method_name, {"value": unquote(path[len(route):])})
        raise self._protocol_error_type(-32602, f"Unknown resource URI: {uri}")

    def _markdown(self, method_name: str, kwargs: dict[str, Any] | None) -> str:
        method = getattr(self, method_name)
        if kwargs is None:
            return method()
        if "value" in kwargs:
            return method(kwargs["value"])
        return method(**kwargs)

    def summary_markdown(self) -> str:
        return self._summary_markdown()

    def handle_jsonrpc(self, message: Any) -> Any:
        if isinstance(message, list):
            responses = [self.handle_jsonrpc(item) for item in message]
            return [response for response in responses if response is not None]
        if not isinstance(message, dict):
            return self.error_response(None, -32600, "Invalid Request")
        request_id = message.get("id")
        has_id = "id" in message
        try:
            if message.get("jsonrpc") != "2.0":
                raise self._protocol_error_type(-32600, "Invalid Request")
            method = message.get("method")
            if not isinstance(method, str):
                raise self._protocol_error_type(-32600, "Invalid Request")
            params = message.get("params", {})
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise self._protocol_error_type(-32602, "Params must be an object.")
            result = self.dispatch(method, params)
            if not has_id:
                return None
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except self._protocol_error_type as exc:
            if not has_id:
                return None
            return self.error_response(request_id, exc.code, exc.message)
        except Exception as exc:
            print(f"MCP server error: {exc}", file=sys.stderr)
            if not has_id:
                return None
            return self.error_response(request_id, -32603, "Internal error")

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested = params.get("protocolVersion")
            protocol_version = (
                requested
                if requested in self.supported_protocol_versions
                else self.protocol_version
            )
            return {
                "protocolVersion": protocol_version,
                "capabilities": {
                    "resources": {"listChanged": False},
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "mudra-local-mcp",
                    "title": "Mudra Local MCP",
                    "version": "0.1.0",
                    "description": "Repo-local SQLite-backed context server for Mudra.",
                },
                "instructions": (
                    "Use this server for durable Mudra repository notes, lightweight "
                    "handoff breadcrumbs, local task check-ins, and searchable repo "
                    "documentation. Use mudra_doc_search before mudra_doc_get to avoid "
                    "loading broad docs into context. Use mudra_todo_* tools to maintain "
                    "local next-step suggestions. Agents may use the mudra_task_*, "
                    "mudra_todo_*, and mudra_doc_* tools, but must not call "
                    "/api/dashboard/owner/flush-agent-state; that HTTP route is "
                    "reserved for the repository owner. Register structured model "
                    "identity with mudra_agent_model_register before check-in; pass "
                    "the returned model_registration_key through check-in and token "
                    "usage. Never synthesize a display label from client metadata."
                ),
            }
        if method in {"notifications/initialized", "ping", "logging/setLevel"}:
            return {}
        if method == "tools/list":
            return {"tools": self.tool_definitions()}
        if method == "tools/call":
            return self.handle_tool_call(params)
        if method == "resources/list":
            return {"resources": self.resource_definitions()}
        if method == "resources/templates/list":
            return {"resourceTemplates": self.resource_templates()}
        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str):
                raise self._protocol_error_type(-32602, "`uri` must be a string.")
            return {
                "contents": [
                    {"uri": uri, "mimeType": "text/markdown", "text": self.read_resource(uri)}
                ]
            }
        if method == "prompts/list":
            return {"prompts": []}
        if method == "completion/complete":
            return {"completion": {"values": [], "total": 0, "hasMore": False}}
        raise self._protocol_error_type(-32601, f"Method not found: {method}")

    def handle_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise self._protocol_error_type(-32602, "`name` must be a string.")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise self._protocol_error_type(-32602, "`arguments` must be an object.")
        try:
            structured = self.call_tool(name, arguments)
        except self._tool_execution_error_type as exc:
            error: dict[str, Any] = {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }
            details = getattr(exc, "details", None)
            if isinstance(details, dict) and details:
                error["structuredContent"] = {"error": {"message": str(exc), **details}}
            return error
        return {
            "content": [{"type": "text", "text": _pretty_json(structured)}],
            "structuredContent": structured,
            "isError": False,
        }

    @staticmethod
    def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


    def _resource_uri(self, path: str) -> str:
        return f"{self.project.resource_scheme}://" + path.lstrip("/")

    def _tool_definitions(self) -> list[dict[str, Any]]:
        object_schema: dict[str, Any] = {"type": "object", "additionalProperties": False}
        return [
            {
                "name": "mudra_mcp_status",
                "title": f"{self.project.server_name} Status",
                "description": "Show repo root, database path, and mcp.db record counts.",
                "inputSchema": object_schema,
            },
            {
                "name": "markdown_sanitize",
                "title": "Sanitize Markdown",
                "description": (
                    "Join hard-wrapped prose paragraphs in safe mode while preserving "
                    "code, JSON, tables, figure/link lines, and blank-line structure."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Markdown text to normalize without content rewriting.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["safe"],
                            "default": "safe",
                            "description": "Normalization mode; only safe prose-only mode is supported.",
                        },
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_note_upsert",
                "title": "Upsert Mudra Note",
                "description": "Create or update a durable repository note in mcp.db.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Stable note key, for example android.recording.parity.",
                        },
                        "value": {
                            "type": "string",
                            "description": "Short durable context, decision, or handoff detail.",
                        },
                        "source": {
                            "type": "string",
                            "description": "Who or what wrote the note.",
                            "default": "agent",
                        },
                    },
                    "required": ["key", "value"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_note_read",
                "title": "Read Mudra Note",
                "description": "Read one repository note by exact key.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_note_search",
                "title": "Search Mudra Notes",
                "description": "Search durable repository notes, or list recently updated notes.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Case-insensitive search text. Omit for recent notes.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 10,
                        },
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_note_delete",
                "title": "Delete Mudra Note",
                "description": "Delete one repository note by exact key.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_event_log",
                "title": "Log Mudra Event",
                "description": "Append a lightweight repository event or handoff breadcrumb.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "event_type": {
                            "type": "string",
                            "description": "Short category such as decision, handoff, or migration.",
                        },
                        "detail": {
                            "type": "string",
                            "description": "Concise event detail. Do not include secrets.",
                        },
                        "actor": {
                            "type": "string",
                            "description": "Who or what logged the event.",
                            "default": "agent",
                        },
                    },
                    "required": ["event_type", "detail"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_event_recent",
                "title": "Recent Mudra Events",
                "description": "Read recent repository event breadcrumbs from mcp.db.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "event_type": {
                            "type": "string",
                            "description": "Optional event type filter.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 10,
                        },
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_agent_model_register",
                "title": "Register Agent Model",
                "description": (
                    "Register a structured agent model identity and return its stable key "
                    "and canonical display label. New clients must call this before "
                    "check-in and must use the returned label without composing, "
                    "reordering, or adding client-name tokens."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "Stable agent/session identity; not a model label.",
                        },
                        "provider": {
                            "type": "string",
                            "enum": sorted(MODEL_REGISTRATION_PROVIDERS),
                            "description": "Canonical provider enum; casing is normalized.",
                        },
                        "model_family": {
                            "type": "string",
                            "enum": sorted(MODEL_REGISTRATION_FAMILIES),
                            "description": (
                                "Family of the model you are actually running as, without its "
                                "version. Not the client product you run inside."
                            ),
                        },
                        "model_version": {
                            "type": "string",
                            "description": (
                                "Numeric version of your own model, for example 5.6 or 4.8. "
                                "Must be a known version for the family; registration is "
                                "rejected otherwise. Known models: "
                                f"{describe_model_catalog()}."
                            ),
                        },
                        "model_variant": {
                            "type": "string",
                            "description": (
                                "Your model's named variant when it has one, for example Sol, "
                                "Luna, or Terra. Required for the gpt family, which registers "
                                "only versions that have named variants; omit it entirely when "
                                "your model has no variant name. Never put the reasoning tier "
                                "here (that is reasoning_effort), and never put the client "
                                "product name here (that is client_name). Only variants listed "
                                "for your family/version are accepted. Dashboard panels group by "
                                "this value, so two sessions of the same model must send the same "
                                "variant text."
                            ),
                        },
                        "reasoning_effort": {
                            "type": "string",
                            "enum": sorted(MODEL_REGISTRATION_EFFORTS),
                            "description": (
                                "Your current reasoning tier; xhigh is accepted as extra_high. "
                                "This belongs here, never in model_variant."
                            ),
                        },
                        "client_name": {
                            "type": "string",
                            "description": (
                                "Optional client metadata, for example Codex CLI or Claude Code; "
                                "never a model-family, variant, or display-label token. The "
                                "client is not the model: running inside Codex CLI does not make "
                                "your variant Codex."
                            ),
                        },
                        "session_hint": {
                            "type": "string",
                            "description": "Optional stable hint distinguishing registrations for one agent.",
                        },
                    },
                    "required": [
                        "agent_id", "provider", "model_family", "model_version", "reasoning_effort"
                    ],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_task_check_in",
                "title": "Check In Mudra Task",
                "description": (
                    "Create or refresh an agent task state row so local agents can "
                    "see that work is currently in progress. New clients must first "
                    "call mudra_agent_model_register and pass its key here; check-in "
                    "snapshots the exact registered canonical label."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_key": {
                            "type": "string",
                            "description": "Stable task slug shared across agents.",
                        },
                        "agent_id": {
                            "type": "string",
                            "description": "Stable identifier for this local agent/session.",
                        },
                        "task_title": {
                            "type": "string",
                            "description": "Human-readable task title.",
                        },
                        "agent_label": {
                            "type": "string",
                            "description": (
                                "Compatibility-only label for unregistered legacy clients; "
                                "never overrides a valid registered identity."
                            ),
                        },
                        "model_registration_key": {
                            "type": "string",
                            "description": (
                                "Registered model key. It takes precedence over legacy "
                                "agent_label and snapshots the exact canonical label returned "
                                "by mudra_agent_model_register."
                            ),
                        },
                        "status": {
                            "type": "string",
                            "enum": ["in_progress", "paused", "blocked"],
                            "default": "in_progress",
                        },
                        "summary": {
                            "type": "string",
                            "description": "Short current task summary.",
                        },
                        "current_step": {
                            "type": "string",
                            "description": "What the agent is doing right now.",
                        },
                        "workspace_root": {
                            "type": "string",
                            "description": "Workspace path for this task.",
                        },
                        "ttl_seconds": {
                            "type": "integer",
                            "minimum": 60,
                            "maximum": MAX_TASK_TTL_SECONDS,
                            "default": DEFAULT_TASK_TTL_SECONDS,
                        },
                        "suppress_cascade": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Skip the related-todo cascade lookup in the response. "
                                "Pass true when the pickup instruction already included "
                                "the cascade (e.g. from mudra_todo_next_instruction) or "
                                "on heartbeat refreshes."
                            ),
                        },
                    },
                    "required": ["task_key", "agent_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_task_check_out",
                "title": "Check Out Mudra Task",
                "description": (
                    "Mark an agent task state as done, abandoned, paused, or blocked "
                    "when the agent stops active work. Check-out closes the existing "
                    "task_key + agent_id row and preserves its registered key and exact "
                    "canonical-label snapshot; it accepts no model-name components."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_key": {"type": "string"},
                        "agent_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["done", "abandoned", "paused", "blocked"],
                            "default": "done",
                        },
                        "summary": {"type": "string"},
                        "current_step": {"type": "string"},
                        "detail": {"type": "string"},
                        "ttl_seconds": {
                            "type": "integer",
                            "minimum": 60,
                            "maximum": MAX_TASK_TTL_SECONDS,
                            "default": DEFAULT_TASK_TTL_SECONDS,
                        },
                    },
                    "required": ["task_key", "agent_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_task_status",
                "title": "Mudra Task Status",
                "description": "Read current state and recent history for one task key.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_key": {"type": "string"},
                        "include_events": {"type": "boolean", "default": True},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 20,
                        },
                    },
                    "required": ["task_key"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_task_active",
                "title": "Active Mudra Tasks",
                "description": "List non-expired in-progress, paused, or blocked task states.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_key": {
                            "type": "string",
                            "description": "Optional task slug filter.",
                        },
                        "status": {
                            "type": "string",
                            "enum": sorted(ACTIVE_TASK_STATUSES),
                            "description": "Optional active-task status filter.",
                        },
                        "search": {
                            "type": "string",
                            "description": "Optional text search across task key, title, agent, summary, and current step.",
                        },
                        "cursor": {
                            "type": "string",
                            "description": "Opaque continuation cursor returned by a previous call.",
                        },
                        "include_stale": {
                            "type": "boolean",
                            "description": "Include expired active statuses.",
                            "default": False,
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 25,
                        },
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_task_token_usage",
                "title": "Report Mudra Task Token Usage",
                "description": (
                    "Record approximate token usage for a task (task_key + agent_id), "
                    "normally right after mudra_task_check_out. Provide total_tokens, "
                    "or input_tokens/output_tokens so the total can be derived. "
                    "Use the same registration key as check-in. Resubmitting replaces "
                    "the previous usage for the same task and agent."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_key": {
                            "type": "string",
                            "description": "Task slug used at check-in.",
                        },
                        "agent_id": {
                            "type": "string",
                            "description": (
                                "Stable agent/session identity used at registration and check-in; "
                                "not a model label."
                            ),
                        },
                        "total_tokens": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Approximate total tokens for the task. Derived from input+output when omitted.",
                        },
                        "input_tokens": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Approximate input/prompt tokens.",
                        },
                        "output_tokens": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Approximate output/completion tokens.",
                        },
                        "model_descriptor": {
                            "type": "string",
                            "description": (
                                "Compatibility-only model label for unregistered legacy clients; "
                                "never overrides a valid registered identity."
                            ),
                        },
                        "model_registration_key": {
                            "type": "string",
                            "description": (
                                "The same registered model key used at check-in. It must belong "
                                "to agent_id and takes precedence over model_descriptor."
                            ),
                        },
                        "notes": {
                            "type": "string",
                            "description": "Optional short context, e.g. how the estimate was produced.",
                        },
                    },
                    "required": ["task_key", "agent_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_task_token_recommendation",
                "title": "Save Mudra Token-Reduction Recommendation",
                "description": (
                    "Save one concrete recommendation for how token usage in the "
                    "just-completed task could have been reduced. Call only after "
                    "mudra_task_token_usage, and only for specific, non-generic "
                    "efficiency observations."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_key": {
                            "type": "string",
                            "description": "Task slug used at check-in.",
                        },
                        "agent_id": {
                            "type": "string",
                            "description": "Agent id used at check-in.",
                        },
                        "recommendation": {
                            "type": "string",
                            "description": (
                                "Concrete, task-specific reduction idea, for example "
                                "'narrower doc searches' or 'skip re-reading edited files'."
                            ),
                        },
                        "recommendation_key": {
                            "type": "string",
                            "description": "Optional stable key for the evidence registry; otherwise derived deterministically from recommendation text.",
                        },
                    },
                    "required": ["task_key", "agent_id", "recommendation"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_todo_add",
                "title": "Add Mudra Todo",
                "description": (
                    "Create or update a local suggested next-step todo in mcp.db. "
                    "For feature or behavior work, include app_scope plus concrete "
                    "code_paths and doc_keys when known; missing doc/code references "
                    "are advisory and never block creation."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "todo_key": {
                            "type": "string",
                            "description": "Stable slug, for example gateway-task-events-get.",
                        },
                        "title": {"type": "string"},
                        "detail": {"type": "string"},
                        "app_scope": {
                            "type": "string",
                            "enum": ["", *sorted(DOC_SCOPES)],
                            "description": "Optional application scope.",
                        },
                        "priority": {
                            "type": "string",
                            "enum": sorted(TODO_PRIORITIES),
                            "default": "P2",
                        },
                        "status": {
                            "type": "string",
                            "enum": sorted(TODO_ADDABLE_STATUSES),
                            "default": "suggested",
                        },
                        "source": {
                            "type": "string",
                            "description": "Who or what created the todo.",
                            "default": "agent",
                        },
                        "source_task_key": {
                            "type": "string",
                            "description": "Task key that produced this follow-up, if any.",
                        },
                        "planned_complexity": {
                            "type": "string",
                            "maxLength": 40,
                            "description": (
                                "Estimated/predicted complexity for this todo before work "
                                "starts (e.g. 'S', 'M', 'L', 'XL', or a numeric score). "
                                "Shown on Open todo rows and used for planned-vs-actual "
                                "comparison; see docs/mcp-server/model-efficiency.md."
                            ),
                        },
                        "actual_complexity": {
                            "type": "string",
                            "maxLength": 40,
                            "description": (
                                "Posthoc/actual complexity, normally set via "
                                "mudra_todo_prune's actual_complexity when the todo is "
                                "completed rather than here; settable via todo_add only "
                                "to correct or backfill a value."
                            ),
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                        },
                        "code_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                            "description": (
                                "Repo-relative files, directories, or globs likely involved. "
                                "Recommended for feature or behavior todos."
                            ),
                        },
                        "symbol_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                            "description": "Classes, functions, tools, tables, constants, or modules likely involved.",
                        },
                        "doc_keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                            "description": (
                                "MCP documentation doc_key or chunk_id references to inspect. "
                                "Search the relevant docs scope first and attach useful refs "
                                "when behavior, APIs, UI flows, schemas, or workflows may change."
                            ),
                        },
                        "route_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                            "description": "HTTP routes, MCP tools, CLI options, or API endpoints likely involved.",
                        },
                        "test_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                            "description": "Focused tests, checks, or commands likely to validate the work.",
                        },
                        "search_queries": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                            "description": "Optional fallback searches if the explicit references are stale.",
                        },
                    },
                    "required": ["todo_key", "title"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_todo_list",
                "title": "List Mudra Todos",
                "description": "List local MCP todos, open by default.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["open", *sorted(TODO_STATUSES)],
                            "default": "open",
                        },
                        "app_scope": {
                            "type": "string",
                            "enum": ["", *sorted(DOC_SCOPES)],
                        },
                        "priority": {
                            "type": "string",
                            "enum": sorted(TODO_PRIORITIES),
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Require every listed tag (AND semantics).",
                        },
                        "source": {"type": "string"},
                        "search": {
                            "type": "string",
                            "description": "Search todo key, title/detail, source, and reference fields.",
                        },
                        "reference_search": {
                            "type": "string",
                            "description": "Search reference fields; combined with search when both are provided.",
                        },
                        "todo_key": {
                            "type": "string",
                            "description": "Exact todo lookup; takes precedence over other filters.",
                        },
                        "cursor": {
                            "type": "string",
                            "description": "Opaque continuation cursor returned by a previous call.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": TODO_LIST_MAX_LIMIT,
                            "default": TODO_LIST_DEFAULT_LIMIT,
                        },
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_todo_prune",
                "title": "Prune Mudra Todo",
                "description": "Mark a todo done or dropped so it disappears from the open list.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "todo_key": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["done", "dropped"],
                            "default": "done",
                        },
                        "actor": {
                            "type": "string",
                            "default": "agent",
                        },
                        "detail": {
                            "type": "string",
                            "description": "Completion note or reason for dropping.",
                        },
                        "actual_complexity": {
                            "type": "string",
                            "maxLength": 40,
                            "description": (
                                "Optional posthoc/actual complexity estimate for the "
                                "just-finished work (e.g. 'S', 'M', 'L', 'XL', or a "
                                "numeric score), for planned-vs-actual comparison. "
                                "Overwrites any prior actual_complexity when provided; "
                                "omit to leave it unchanged."
                            ),
                        },
                    },
                    "required": ["todo_key"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_todo_next_instruction",
                "title": "Mudra Todo Next Instruction",
                "description": "Render copy/paste text for instructing a local agent from open todos.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "app_scope": {
                            "type": "string",
                            "enum": ["", *sorted(DOC_SCOPES)],
                        },
                        "status": {
                            "type": "string",
                            "enum": ["open", *sorted(TODO_STATUSES)],
                            "default": "open",
                        },
                        "priority": {"type": "string", "enum": sorted(TODO_PRIORITIES)},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "source": {"type": "string"},
                        "search": {"type": "string"},
                        "reference_search": {"type": "string"},
                        "cursor": {
                            "type": "string",
                            "description": "Opaque continuation cursor returned by a previous call.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "default": 3,
                        },
                        "todo_key": {
                            "type": "string",
                            "description": "Optional todo key to render as the primary instruction target.",
                        },
                        "variant": {
                            "type": "string",
                            "enum": ["implement", "validation_failed"],
                            "default": "implement",
                            "description": "Optional prompt variant; validation_failed renders a manual/device validation failure re-scope prompt.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_todo_group_related",
                "title": "Group Related Mudra Todos",
                "description": (
                    "Manually associate two or more existing todos into a related "
                    "group. Groups can span app_scope values."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "todo_keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Existing todo keys to place in one related group.",
                        },
                        "group_key": {
                            "type": "string",
                            "description": "Optional stable group slug. A deterministic key is generated when omitted.",
                        },
                        "title": {"type": "string"},
                        "detail": {"type": "string"},
                        "actor": {"type": "string", "default": "agent"},
                        "relation_kind": {
                            "type": "string",
                            "enum": sorted(TODO_GROUP_RELATION_KINDS),
                            "default": "manual",
                        },
                    },
                    "required": ["todo_keys"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_todo_group_reorder",
                "title": "Reorder Related Mudra Todos",
                "description": (
                    "Rewrite the complete dense order of an existing todo group. "
                    "The ordered member list must exactly match the group's current members."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "group_key": {"type": "string"},
                        "todo_keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                            "description": "Complete ordered list of the group's todo keys.",
                        },
                        "actor": {"type": "string", "default": "agent"},
                    },
                    "required": ["group_key", "todo_keys"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_todo_auto_group_related",
                "title": "Auto Group Related Mudra Todos",
                "description": (
                    "Score likely-related todos using shared tags, routes, docs, code paths, "
                    "source_task_key, and title/detail similarity. Pass apply=true to persist "
                    "the suggested groups."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "todo_keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                            "description": (
                                "Optional seed or explicit todo keys. With two or more keys, "
                                "those keys are kept together and scored against other open todos."
                            ),
                        },
                        "app_scope": {
                            "type": "string",
                            "enum": ["", *sorted(DOC_SCOPES)],
                            "description": "Optional scope filter for candidate todos.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["open", *sorted(TODO_STATUSES)],
                            "default": "open",
                        },
                        "priority": {"type": "string", "enum": sorted(TODO_PRIORITIES)},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "source": {"type": "string"},
                        "search": {"type": "string"},
                        "reference_search": {"type": "string"},
                        "cursor": {"type": "string"},
                        "min_score": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                            "default": TODO_GROUP_DEFAULT_MIN_SCORE,
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                            "default": TODO_GROUP_DEFAULT_LIMIT,
                        },
                        "apply": {
                            "type": "boolean",
                            "default": False,
                            "description": "Persist suggested groups when true; otherwise return a dry-run preview.",
                        },
                        "include_inferred_candidates": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "When seed todo_keys include two or more todos, also pull in "
                                "other scored candidates. One-key seeds always infer candidates."
                            ),
                        },
                        "group_key": {
                            "type": "string",
                            "description": "Optional group key when seeding a single group.",
                        },
                        "actor": {"type": "string", "default": "agent"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_todo_related_for_assignment",
                "title": "Related Todos For Assignment",
                "description": (
                    "Return related todo groups and cascade guidance for accepting, "
                    "starting, or checking in against one todo."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "todo_key": {"type": "string"},
                        "source_task_key": {
                            "type": "string",
                            "description": "Optional task key used to resolve a linked todo.",
                        },
                        "app_scope": {"type": "string", "enum": ["", *sorted(DOC_SCOPES)]},
                        "status": {
                            "type": "string",
                            "enum": ["open", *sorted(TODO_STATUSES)],
                            "default": "open",
                        },
                        "priority": {"type": "string", "enum": sorted(TODO_PRIORITIES)},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "source": {"type": "string"},
                        "search": {"type": "string"},
                        "reference_search": {"type": "string"},
                        "cursor": {"type": "string"},
                        "include_inferred": {
                            "type": "boolean",
                            "default": True,
                            "description": "Include non-persisted high-confidence related candidates.",
                        },
                        "min_score": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                            "default": TODO_GROUP_DEFAULT_MIN_SCORE,
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                            "default": TODO_GROUP_DEFAULT_LIMIT,
                        },
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_doc_scopes",
                "title": "Mudra Documentation Scopes",
                "description": "List searchable documentation scopes for repo applications.",
                "inputSchema": object_schema,
            },
            {
                "name": "mudra_doc_index_repo",
                "title": "Index Mudra Repo Documentation",
                "description": (
                    "Index existing repo docs and inferred source manifests into mcp.db "
                    "for searchable documentation retrieval. Returns a compact per-scope "
                    "summary by default; verify a reindex with a focused mudra_doc_search "
                    "call rather than requesting the full per-document listing."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "app_scope": {
                            "type": "string",
                            "enum": sorted(DOC_SCOPES),
                            "description": "Optional scope to reindex; omit for all scopes.",
                        },
                        "include_manifests": {
                            "type": "boolean",
                            "default": True,
                            "description": "Include inferred source manifests for each scope.",
                        },
                        "verbose": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Include the full per-document `indexed` listing in the "
                                "response. Leave false to keep reindex output compact; "
                                "prefer mudra_doc_search to verify results instead."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_doc_search",
                "title": "Search Mudra Documentation",
                "description": (
                    "Search indexed docs and return focused snippets. Use this before "
                    "fetching whole docs to keep context small."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "app_scope": {
                            "type": "string",
                            "enum": sorted(DOC_SCOPES),
                            "description": "Optional app scope filter.",
                        },
                        "source_type": {
                            "type": "string",
                            "enum": sorted(DOC_SOURCE_TYPES),
                            "description": "Optional file/manual/inferred filter.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": DOC_SEARCH_MAX_LIMIT,
                            "default": 10,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mudra_doc_get",
                "title": "Get Mudra Documentation",
                "description": (
                    "Fetch one indexed doc or one chunk by key/id after search has "
                    "identified the relevant target."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "doc_key": {
                            "type": "string",
                            "description": "Indexed document key returned by search.",
                        },
                        "chunk_id": {
                            "type": "integer",
                            "description": "Specific chunk id returned by search.",
                        },
                        "max_chars": {
                            "type": "integer",
                            "minimum": 500,
                            "maximum": DOC_FETCH_MAX_CHARS,
                            "default": DOC_FETCH_DEFAULT_CHARS,
                        },
                    },
                    "additionalProperties": False,
                },
            },
            *orchestration_tool_definitions(),
            *myohid_test_tool_definitions(),
            {"name":"mudra_guidance_recommendation_add", "title":"Add guidance evidence", "description":"Record evidence for a proposed guidance item. Entries are evidence, not proof; this never edits instruction files.", "inputSchema":{"type":"object","properties":{"recommendation_key":{"type":"string"},"proposed_guidance":{"type":"string"},"target_documents":{"type":"array","items":{"type":"string"},"default":["AGENTS.md"]},"evidence":{"type":"string"},"source_kind":{"type":"string"},"source_ref":{"type":"string"},"task_key":{"type":"string"},"agent_id":{"type":"string"}},"required":["proposed_guidance","evidence"],"additionalProperties":False}},
            {"name":"mudra_guidance_recommendation_list", "title":"List guidance evidence", "description":"List database-backed guidance evidence and its auditable history.", "inputSchema":{"type":"object","properties":{"recommendation_key":{"type":"string"},"status":{"type":"string","enum":["open","incorporated","dismissed","superseded"]},"target_document":{"type":"string"},"search":{"type":"string"},"limit":{"type":"integer"},"cursor":{"type":"string"}},"additionalProperties":False}},
            {"name":"mudra_guidance_recommendation_reconcile", "title":"Reconcile guidance evidence", "description":"Explicitly reconcile evidence. It records a decision but never edits or commits AGENTS.md or CLAUDE.md.", "inputSchema":{"type":"object","properties":{"recommendation_key":{"type":"string"},"proposed_guidance":{"type":"string"},"target_documents":{"type":"array","items":{"type":"string"}},"status":{"type":"string","enum":["incorporated","dismissed"]},"resolution":{"type":"string"},"merge_from_key":{"type":"string"}},"required":["recommendation_key"],"additionalProperties":False}},
            {
                "name": "mudra_doc_upsert",
                "title": "Upsert Mudra Documentation",
                "description": (
                    "Add or update a manual or inferred documentation entry in mcp.db."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "doc_key": {"type": "string"},
                        "app_scope": {"type": "string", "enum": sorted(DOC_SCOPES)},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "summary": {"type": "string"},
                        "source_type": {
                            "type": "string",
                            "enum": ["manual", "inferred"],
                            "default": "manual",
                        },
                        "source_path": {"type": "string"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                        },
                    },
                    "required": ["doc_key", "app_scope", "title", "content"],
                    "additionalProperties": False,
                },
            },
        ]

    def _resource_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "uri": self._resource_uri("summary"),
                "name": "mudra-mcp-summary",
                "title": f"{self.project.display_name} MCP Summary",
                "description": "Database status plus recent notes and events.",
                "mimeType": "text/markdown",
            },
            {
                "uri": self._resource_uri("notes"),
                "name": "mudra-mcp-notes",
                "title": f"{self.project.display_name} MCP Notes",
                "description": "Recently updated durable repository notes.",
                "mimeType": "text/markdown",
            },
            {
                "uri": self._resource_uri("events/recent"),
                "name": "mudra-mcp-events-recent",
                "title": f"Recent {self.project.display_name} MCP Events",
                "description": "Recent repository event breadcrumbs.",
                "mimeType": "text/markdown",
            },
            {
                "uri": self._resource_uri("tasks/active"),
                "name": "mudra-mcp-tasks-active",
                "title": f"Active {self.project.display_name} MCP Tasks",
                "description": "Current non-expired local agent task states.",
                "mimeType": "text/markdown",
            },
            {
                "uri": self._resource_uri("docs/scopes"),
                "name": "mudra-mcp-doc-scopes",
                "title": f"{self.project.display_name} Documentation Scopes",
                "description": "Searchable documentation scopes for repo applications.",
                "mimeType": "text/markdown",
            },
            {
                "uri": self._resource_uri("todos/open"),
                "name": "mudra-mcp-todos-open",
                "title": f"Open {self.project.display_name} MCP Todos",
                "description": "Open local todo suggestions for future agent tasks.",
                "mimeType": "text/markdown",
            },
        ]

    def _resource_templates(self) -> list[dict[str, Any]]:
        return [
            {
                "uriTemplate": self._resource_uri("note/{key}"),
                "name": "mudra-mcp-note-by-key",
                "title": f"{self.project.display_name} MCP Note By Key",
                "description": "Read a durable repository note by key.",
                "mimeType": "text/markdown",
            },
            {
                "uriTemplate": self._resource_uri("task/{task_key}"),
                "name": "mudra-mcp-task-by-key",
                "title": f"{self.project.display_name} MCP Task By Key",
                "description": "Read current state and history for a local agent task.",
                "mimeType": "text/markdown",
            },
            {
                "uriTemplate": self._resource_uri("docs/scope/{app_scope}"),
                "name": "mudra-mcp-docs-by-scope",
                "title": f"{self.project.display_name} MCP Docs By Scope",
                "description": "List indexed documentation entries for one app scope.",
                "mimeType": "text/markdown",
            },
            {
                "uriTemplate": self._resource_uri("docs/search/{query}"),
                "name": "mudra-mcp-doc-search",
                "title": f"{self.project.display_name} MCP Doc Search",
                "description": "Search indexed documentation and return focused snippets.",
                "mimeType": "text/markdown",
            },
            {
                "uriTemplate": self._resource_uri("doc/{doc_key}"),
                "name": "mudra-mcp-doc-by-key",
                "title": f"{self.project.display_name} MCP Doc By Key",
                "description": "Read one indexed documentation entry by doc_key.",
                "mimeType": "text/markdown",
            },
            {
                "uriTemplate": self._resource_uri("todo/{todo_key}"),
                "name": "mudra-mcp-todo-by-key",
                "title": f"{self.project.display_name} MCP Todo By Key",
                "description": "Read one local todo by todo_key.",
                "mimeType": "text/markdown",
            }
        ]

    def _dashboard_snapshot(self) -> dict[str, Any]:
        status = self.status()
        with self.store.connection() as conn:
            task_counts = {
                row["status"]: row["count"]
                for row in conn.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM agent_task_state
                    GROUP BY status
                    ORDER BY status
                    """
                ).fetchall()
            }
            task_events = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM agent_task_events
                    ORDER BY ts DESC, id DESC
                    LIMIT 30
                    """
                ).fetchall()
            ]
            flushes = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM owner_flushes
                    ORDER BY ts DESC, id DESC
                    LIMIT 10
                    """
                ).fetchall()
            ]
            token_usage = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM agent_task_token_usage
                    ORDER BY ts DESC, id DESC
                    """
                ).fetchall()
            ]
            recent_tasks = [
                row_to_task(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM agent_task_state
                    ORDER BY last_seen_at DESC, id DESC
                    """
                ).fetchall()
            ]
            todo_counts = {
                row["status"]: row["count"]
                for row in conn.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM mcp_todos
                    GROUP BY status
                    ORDER BY status
                    """
                ).fetchall()
            }
        open_todo_result = self.todos.todo_list({"status": "open", "limit": TODO_LIST_DEFAULT_LIMIT})
        open_todos = open_todo_result["todos"]
        guidance_recommendations = self.guidance.list({"limit": 100}) if self.guidance else {"recommendations": [], "total": 0}
        active_task_result = self.lifecycle.task_active({"limit": TASK_ACTIVE_DEFAULT_LIMIT})
        active_tasks = active_task_result["tasks"]
        complexity_keys = {
            str(row.get("task_key") or "")
            for row in [*recent_tasks, *token_usage]
            if row.get("task_key")
        }
        todo_complexity_by_key: dict[str, dict[str, Any]] = {}
        if complexity_keys:
            placeholders = ",".join("?" for _ in complexity_keys)
            with self.store.connection() as conn:
                todo_complexity_by_key = {
                    row["todo_key"]: dict(row)
                    for row in conn.execute(
                        f"""
                        SELECT todo_key, title, status, planned_complexity, actual_complexity
                        FROM mcp_todos
                        WHERE todo_key IN ({placeholders})
                        """,
                        tuple(sorted(complexity_keys)),
                    ).fetchall()
                }

        def attach_todo_complexity(row: dict[str, Any]) -> None:
            todo = todo_complexity_by_key.get(str(row.get("task_key") or ""))
            row["todo_title"] = todo.get("title") if todo else None
            row["todo_status"] = todo.get("status") if todo else None
            row["planned_complexity"] = todo.get("planned_complexity") if todo else None
            row["actual_complexity"] = todo.get("actual_complexity") if todo else None

        for task in recent_tasks:
            attach_todo_complexity(task)
        for row in token_usage:
            attach_todo_complexity(row)

        # Effective Token-Usage grouping label, resolved through durable category
        # aliases so a rename cannot be undone by later registration/token writes.
        # The dashboard's modelIdentity() prefers this field when present.
        if self.agent_categories is not None:
            with self.store.connection() as conn:
                category_aliases = self.agent_categories.load_aliases(conn)
            for row in (*recent_tasks, *token_usage):
                row["effective_model_label"] = (
                    self.agent_categories.effective_label_for_row(row, category_aliases)
                )

        # Efficiency samples: one row per token-usage row, carrying the joined
        # todo complexities (already attached above via todo_key = task_key)
        # plus a model descriptor with the agent_task_state.agent_label
        # fallback, mirroring task_token_usage's own write-time fallback so
        # legacy rows with an empty model_descriptor still classify. See
        # docs/mcp-server/model-efficiency.md.
        agent_label_by_task_agent: dict[tuple[str, str], str] = {}
        usage_task_keys = {row["task_key"] for row in token_usage if row.get("task_key")}
        if usage_task_keys:
            placeholders = ",".join("?" for _ in usage_task_keys)
            with self.store.connection() as conn:
                agent_label_by_task_agent = {
                    (row["task_key"], row["agent_id"]): (
                        row["canonical_model_label"] or row["agent_label"]
                    )
                    for row in conn.execute(
                        f"""
                        SELECT task_key, agent_id, agent_label, canonical_model_label
                        FROM agent_task_state
                        WHERE task_key IN ({placeholders})
                        """,
                        tuple(sorted(usage_task_keys)),
                    ).fetchall()
                }
        efficiency_samples = []
        for row in token_usage:
            task_key = str(row.get("task_key") or "")
            agent_id = str(row.get("agent_id") or "")
            todo = todo_complexity_by_key.get(task_key)
            descriptor = (
                row.get("canonical_model_label")
                or
                row.get("model_descriptor")
                or agent_label_by_task_agent.get((task_key, agent_id))
                or agent_id
            )
            planned = row.get("planned_complexity")
            actual = row.get("actual_complexity")
            efficiency_samples.append(
                {
                    "task_key": task_key,
                    "agent_id": agent_id,
                    "todo_key": todo["todo_key"] if todo else None,
                    "model_descriptor": descriptor,
                    "planned_complexity": planned,
                    "actual_complexity": actual,
                    "planned_complexity_num": complexity_numeric(planned),
                    "actual_complexity_num": complexity_numeric(actual),
                    "input_tokens": row.get("input_tokens"),
                    "output_tokens": row.get("output_tokens"),
                    "total_tokens": row.get("total_tokens"),
                    "ts": row.get("ts"),
                }
            )
        active_task_status_by_key: dict[str, str] = {}
        for task in active_tasks:
            if task.get("task_key"):
                # task_active orders by last_seen_at DESC; keep the freshest
                # check-in when multiple agents share a task key.
                active_task_status_by_key.setdefault(task["task_key"].casefold(), task["status"])
        stale_task_keys = {
            task["task_key"].casefold()
            for task in recent_tasks
            if task.get("task_key") and task.get("is_stale")
        }
        for todo in open_todos:
            linked_keys = [
                str(value).casefold()
                for value in (todo.get("todo_key"), todo.get("source_task_key"))
                if value
            ]
            todo["is_stale"] = any(key in stale_task_keys for key in linked_keys)
            todo["linked_task_status"] = next(
                (
                    active_task_status_by_key[key]
                    for key in linked_keys
                    if key in active_task_status_by_key
                ),
                "",
            )
            todo["display_status"] = dashboard_todo_display_status(
                todo.get("status", ""),
                todo.get("linked_task_status", ""),
            )
            related_guidance = self.todos.todo_related_for_assignment(
                {
                    "todo_key": todo.get("todo_key", ""),
                    "include_inferred": False,
                    "limit": 6,
                }
            )
            todo["related_todos"] = [
                {
                    "todo_key": related.get("todo_key", ""),
                    "title": related.get("title", ""),
                    "app_scope": related.get("app_scope", ""),
                    "priority": related.get("priority", ""),
                    "status": related.get("status", ""),
                    "group_key": related.get("group_key", ""),
                    "group_title": related.get("group_title", ""),
                    "relation_kind": related.get("relation_kind", ""),
                    "relation_source": related.get("relation_source", ""),
                    "relation_reason": related.get("relation_reason", ""),
                }
                for related in related_guidance.get("related_todos", [])
            ]
            todo["related_group_keys"] = [
                group.get("group_key", "")
                for group in related_guidance.get("groups", [])
                if group.get("group_key")
            ]

        return {
            "generated_at": utc_now(),
            "project": {
                "key": self.project.key,
                "display_name": self.project.display_name,
                "server_name": self.project.server_name,
                "resource_scheme": self.project.resource_scheme,
                "remote_base_url": self.project.remote_base_url,
            },
            "status": status,
            "task_counts": task_counts,
            "todo_counts": todo_counts,
            "open_todos": open_todos,
            "open_todos_meta": {
                "total": open_todo_result["total"],
                "has_more": open_todo_result["has_more"],
                "next_cursor": open_todo_result["next_cursor"],
                "limit": open_todo_result["limit"],
            },
            "orchestration": self.orchestration.dashboard_snapshot() if self.orchestration else {"queue": [], "assignments": [], "states": []},
            "active_tasks": active_tasks,
            "active_tasks_meta": {
                "total": active_task_result["total"],
                "has_more": active_task_result["has_more"],
                "next_cursor": active_task_result["next_cursor"],
                "limit": active_task_result["limit"],
            },
            "recent_tasks": recent_tasks,
            "task_events": task_events,
            "flushes": flushes,
            "token_usage": token_usage,
            "guidance_recommendations": guidance_recommendations,
            "efficiency_samples": efficiency_samples,
            "doc_scopes": self.documents.doc_scopes()["scopes"],
            "docs_drift": self.documents.docs_drift_audit(DOC_DRIFT_AUDIT_SCOPE),
        }

    def _summary_markdown(self) -> str:
        status = self.status()
        return "\n".join(
            [
                f"# {self.project.display_name} MCP Summary",
                "",
                f"- Repo root: `{status['repo_root']}`",
                f"- Database: `{status['db_path']}`",
                f"- Notes: {status['notes']}",
                f"- Events: {status['events']}",
                f"- Task states: {status['task_states']}",
                f"- Active tasks: {status['active_tasks']}",
                f"- Indexed docs: {status['docs']}",
                f"- Doc chunks: {status['doc_chunks']}",
                f"- Open todos: {status['open_todos']}",
                f"- Total todos: {status['todos']}",
                "",
                "## Documentation",
                self.doc_scopes_markdown(),
                "",
                "## Open Todos",
                self.todos_markdown(limit=10, include_title=False),
                "",
                "## Active Tasks",
                self.tasks_markdown(limit=10, include_title=False),
                "",
                "## Recent Notes",
                self.notes_markdown(limit=10, include_title=False),
                "",
                "## Recent Events",
                self.events_markdown(limit=10, include_title=False),
            ]
        )

    def notes_markdown(self, *, limit: int, include_title: bool = True) -> str:
        rows = self.lifecycle.note_search({"limit": limit})["notes"]
        lines: list[str] = [f"# {self.project.server_name} Notes", ""] if include_title else []
        if not rows:
            lines.append("_No notes stored yet._")
            return "\n".join(lines)
        for row in rows:
            lines.extend(
                [
                    f"## {row['key']}",
                    "",
                    f"- Source: `{row['source']}`",
                    f"- Updated: `{row['updated_at']}`",
                    "",
                    row["value"],
                    "",
                ]
            )
        return "\n".join(lines).rstrip()

    def note_markdown(self, key: str) -> str:
        result = self.lifecycle.note_read({"key": key})
        if not result["found"]:
            return f"# {self.project.server_name} Note\n\nNo note found for `{key}`."
        row = result["note"]
        return "\n".join(
            [
                f"# {row['key']}",
                "",
                f"- Source: `{row['source']}`",
                f"- Created: `{row['created_at']}`",
                f"- Updated: `{row['updated_at']}`",
                "",
                row["value"],
            ]
        )

    def events_markdown(self, *, limit: int, include_title: bool = True) -> str:
        rows = self.lifecycle.event_recent({"limit": limit})["events"]
        lines: list[str] = [f"# {self.project.server_name} Events", ""] if include_title else []
        if not rows:
            lines.append("_No events logged yet._")
            return "\n".join(lines)
        for row in rows:
            detail = row["detail"].replace("\n", " ")
            lines.append(
                f"- `{row['ts']}` `{row['event_type']}` by `{row['actor']}`: {detail}"
            )
        return "\n".join(lines)

    def tasks_markdown(self, *, limit: int, include_title: bool = True) -> str:
        tasks = self.lifecycle.task_active({"limit": limit})["tasks"]
        lines: list[str] = [f"# {self.project.server_name} Active Tasks", ""] if include_title else []
        if not tasks:
            lines.append("_No active local agent tasks._")
            return "\n".join(lines)
        for task in tasks:
            lines.extend(
                [
                    f"## {task['task_key']}",
                    "",
                    f"- Title: {task['task_title']}",
                    f"- Agent: `{task['agent_id']}` {task['agent_label']}".rstrip(),
                    f"- Status: `{task['status']}`",
                    f"- Last seen: `{task['last_seen_at']}`",
                    f"- Expires: `{task['expires_at']}`",
                    f"- Workspace: `{task['workspace_root']}`",
                ]
            )
            if task["summary"]:
                lines.extend(["", task["summary"]])
            if task["current_step"]:
                lines.extend(["", f"Current step: {task['current_step']}"])
            lines.append("")
        return "\n".join(lines).rstrip()

    def task_markdown(self, task_key: str) -> str:
        result = self.lifecycle.task_status({"task_key": task_key, "include_events": True, "limit": 50})
        lines = [f"# {self.project.server_name} Task: {task_key}", ""]
        if not result["states"]:
            lines.append("_No task states stored for this key._")
            return "\n".join(lines)
        lines.extend(["## Current State", ""])
        for task in result["states"]:
            lines.extend(
                [
                    f"### {task['agent_id']}",
                    "",
                    f"- Title: {task['task_title']}",
                    f"- Agent label: {task['agent_label'] or '_none_'}",
                    f"- Status: `{task['status']}`",
                    f"- Active: `{task['is_active']}`",
                    f"- Stale: `{task['is_stale']}`",
                    f"- Started: `{task['started_at']}`",
                    f"- Last seen: `{task['last_seen_at']}`",
                    f"- Expires: `{task['expires_at']}`",
                ]
            )
            if task["completed_at"]:
                lines.append(f"- Completed: `{task['completed_at']}`")
            if task["flushed_at"]:
                lines.append(f"- Flushed: `{task['flushed_at']}`")
            if task["summary"]:
                lines.extend(["", task["summary"]])
            if task["current_step"]:
                lines.extend(["", f"Current step: {task['current_step']}"])
            lines.append("")
        if result["events"]:
            lines.extend(["## Recent Events", ""])
            for event in result["events"]:
                lines.append(
                    f"- `{event['ts']}` `{event['action']}` `{event['status']}` "
                    f"by `{event['agent_id']}`: {event['summary']}"
                )
        return "\n".join(lines).rstrip()

    def todos_markdown(self, *, limit: int, include_title: bool = True) -> str:
        todos = self.todos.todo_list({"status": "open", "limit": limit})["todos"]
        lines: list[str] = [f"# {self.project.server_name} Open Todos", ""] if include_title else []
        if not todos:
            lines.append("_No open MCP todos recorded._")
            return "\n".join(lines)
        for todo in todos:
            lines.extend(
                [
                    f"## {todo['todo_key']}",
                    "",
                    f"- Title: {todo['title']}",
                    f"- Scope: `{todo['app_scope'] or 'unspecified'}`",
                    f"- Priority: `{todo['priority']}`",
                    f"- Status: `{todo['status']}`",
                    f"- Updated: `{todo['updated_at']}`",
                ]
            )
            advisories = todo.get("reference_advisories") or []
            if advisories:
                lines.append("- Advisories: " + " ".join(advisories))
            if todo["detail"]:
                lines.extend(["", todo["detail"]])
            lines.append("")
        return "\n".join(lines).rstrip()

    def todo_markdown(self, todo_key: str) -> str:
        result = self.todos.todo_get(todo_key)
        if not result["found"]:
            return f"# {self.project.server_name} Todo\n\nNo todo found for `{todo_key}`."
        todo = result["todo"]
        lines = [
            f"# {todo['todo_key']}",
            "",
            f"- Title: {todo['title']}",
            f"- Scope: `{todo['app_scope'] or 'unspecified'}`",
            f"- Priority: `{todo['priority']}`",
            f"- Status: `{todo['status']}`",
            f"- Created: `{todo['created_at']}`",
            f"- Updated: `{todo['updated_at']}`",
        ]
        if todo["completed_at"]:
            lines.append(f"- Completed: `{todo['completed_at']}`")
        if todo["tags"]:
            lines.append("- Tags: " + ", ".join(f"`{tag}`" for tag in todo["tags"]))
        if todo["detail"]:
            lines.extend(["", todo["detail"]])
        reference_lines = self.todos.todo_reference_lines(todo)
        if reference_lines:
            lines.extend(["", "## References", "", *reference_lines])
        advisory_lines = self.todos.todo_advisory_lines(todo)
        if advisory_lines:
            lines.extend(["", "## Advisory Checklist", "", *advisory_lines])
        if result["events"]:
            lines.extend(["", "## Events", ""])
            for event in result["events"]:
                lines.append(
                    f"- `{event['ts']}` `{event['action']}` by `{event['actor']}`: {event['detail']}"
                )
        return "\n".join(lines).rstrip()

    def doc_scopes_markdown(self) -> str:
        return self.documents.doc_scopes_markdown()

    def docs_scope_markdown(self, app_scope: str) -> str:
        return self.documents.docs_scope_markdown(app_scope)

    def docs_search_markdown(self, query: str) -> str:
        return self.documents.docs_search_markdown(query)

    def doc_markdown(self, doc_key: str) -> str:
        return self.documents.doc_markdown(doc_key)

    def status(self) -> dict[str, Any]:
        with self.store.connection() as conn:
            note_count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            task_state_count = conn.execute("SELECT COUNT(*) FROM agent_task_state").fetchone()[0]
            active_task_count = conn.execute(
                """
                SELECT COUNT(*) FROM agent_task_state
                WHERE status IN ('in_progress', 'paused', 'blocked')
                  AND expires_at > ?
                """,
                (utc_now(),),
            ).fetchone()[0]
            token_usage_count = conn.execute(
                "SELECT COUNT(*) FROM agent_task_token_usage"
            ).fetchone()[0]
            doc_count = conn.execute("SELECT COUNT(*) FROM doc_entries").fetchone()[0]
            doc_chunk_count = conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
            todo_count = conn.execute("SELECT COUNT(*) FROM mcp_todos").fetchone()[0]
            open_todo_count = conn.execute(
                """
                SELECT COUNT(*) FROM mcp_todos
                WHERE status IN ('suggested', 'accepted', 'in_progress', 'blocked', 'queued')
                """
            ).fetchone()[0]
            todo_group_count = conn.execute(
                "SELECT COUNT(*) FROM mcp_todo_groups"
            ).fetchone()[0]
            latest_note = conn.execute(
                "SELECT key, updated_at FROM notes ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            latest_event = conn.execute(
                "SELECT event_type, ts FROM events ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return {
            "server": "mudra-local-mcp",
            "protocol_version": self.protocol_version,
            "repo_root": str(self.project.repo_root),
            "db_path": str(self.store.db_path),
            "db_exists": self.store.db_path.exists(),
            "notes": note_count,
            "events": event_count,
            "task_states": task_state_count,
            "active_tasks": active_task_count,
            "token_usage_rows": token_usage_count,
            "docs": doc_count,
            "doc_chunks": doc_chunk_count,
            "todos": todo_count,
            "open_todos": open_todo_count,
            "todo_groups": todo_group_count,
            "latest_note": dict(latest_note) if latest_note else None,
            "latest_event": dict(latest_event) if latest_event else None,
        }



def _pretty_json(value: Any) -> str:
    import json

    return json.dumps(value, indent=2, sort_keys=True, default=str)
