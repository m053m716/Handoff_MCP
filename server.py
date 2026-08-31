"""Local MCP server for repository-scoped Mudra context.

The server stores durable notes and event breadcrumbs in ./mcp.db. It has no
third-party dependencies; SQLite and the HTTP server both come from the Python
standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from mcp.project import (
    DEFAULT_PROJECT_MODULE,
    PROJECT_MODULE_ENV_VAR,
    load_project_descriptor,
    project_module_from_argv,
)
from mcp.application import McpApplication
from mcp.agent_categories import AgentCategoryService
from mcp.guidance import GuidanceService
from mcp.lifecycle import (
    ACTIVE_TASK_STATUSES,
    DEFAULT_TASK_TTL_SECONDS,
    MAX_TASK_TTL_SECONDS,
    MAX_TOKEN_COUNT,
    MODEL_REGISTRATION_EFFORTS,
    MODEL_REGISTRATION_FAMILIES,
    MODEL_REGISTRATION_FAMILY_PROVIDERS,
    MODEL_REGISTRATION_KEY_PREFIX,
    MODEL_REGISTRATION_PROVIDERS,
    TASK_ACTIVE_DEFAULT_LIMIT,
    TASK_ACTIVE_MAX_LIMIT,
    TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    LifecycleService,
    bool_arg,
    canonical_model_label,
    clamp_limit,
    decode_page_cursor,
    encode_page_cursor,
    escape_like,
    model_registration_key,
    normalize_model_registration_enum,
    normalize_model_registration_variant,
    normalize_model_registration_version,
    normalize_task_status,
    optional_token_count,
    optional_string,
    parse_utc_timestamp,
    require_string,
    row_to_task,
    task_ttl_seconds,
    utc_after,
    utc_now,
)
from mcp.storage import McpStore, SQLITE_BUSY_TIMEOUT_MS
from mcp.orchestration import OrchestrationService
from mcp.myohid_tests import MyoHidTestService
from mcp.todos import (
    TODO_ACTIVE_STATUSES,
    TODO_GROUP_DEFAULT_LIMIT,
    TODO_GROUP_DEFAULT_MIN_SCORE,
    TODO_GROUP_GENERIC_TAGS,
    TODO_GROUP_MAX_KEYS,
    TODO_GROUP_RELATION_KINDS,
    TODO_GROUP_TEXT_STOPWORDS,
    TODO_GROUP_TYPES,
    TODO_FEATURE_GUIDANCE,
    TODO_JSON_LIST_COLUMNS,
    TODO_LIST_DEFAULT_LIMIT,
    TODO_LIST_MAX_LIMIT,
    TODO_PRIORITIES,
    TODO_REFERENCE_LABELS,
    TODO_STATUSES,
    TODO_TERMINAL_STATUSES,
    TodoService,
    default_todo_group_key,
    normalize_group_type,
    normalize_reference_value,
    normalize_relation_kind,
    normalize_todo_priority,
    normalize_todo_status,
    json_text,
    parse_string_list,
    related_path_pairs,
    row_to_todo,
    safe_group_key,
    safe_todo_key,
    score_related_todos,
    shared_normalized_values,
    todo_reference_advisories,
    todo_text_tokens,
)
from mcp.lifecycle import ToolExecutionError

# Pin the active project descriptor before importing mcp.config: both modules
# bind descriptor-derived constants at import time, so a --project-module flag
# must land in the environment first to keep them consistent.
_PROJECT_MODULE_OVERRIDE = project_module_from_argv(sys.argv[1:])
if _PROJECT_MODULE_OVERRIDE:
    os.environ[PROJECT_MODULE_ENV_VAR] = _PROJECT_MODULE_OVERRIDE

from mcp.config import (
    DEFAULT_MCP_SERVER_IP,
    DEFAULT_MCP_SERVER_PORT,
    MCP_HOST_ENV_VAR,
    MCP_PORT_ENV_VAR,
    load_mcp_server_config,
    mcp_bind_host_candidates,
    mcp_origin_hosts,
)
from mcp.documents import (
    DOC_CHUNK_MAX_CHARS,
    DOC_CHUNK_TARGET_CHARS,
    DOC_DRIFT_AUDIT_MAX_ITEMS,
    DOC_DRIFT_AUDIT_SCOPE,
    DOC_DRIFT_TIMESTAMP_SKEW_SECONDS,
    DOC_FETCH_DEFAULT_CHARS,
    DOC_FETCH_MAX_CHARS,
    DOC_SEARCH_MAX_LIMIT,
    DOC_SCOPES,
    DOC_SOURCE_TYPES,
    DocumentationService,
    hash_text,
    tokenize_query,
    utc_timestamp_from_mtime,
)
from mcp.auth import (
    CF_ACCESS_CLIENT_ID_HEADER,
    CF_ACCESS_JWT_HEADER,
    REMOTE_AUTH_ENV_VAR,
    REMOTE_AUTH_MODES,
    RemoteAuthPolicy as _TransportRemoteAuthPolicy,
    REMOTE_AUTH_DISABLED as _TRANSPORT_REMOTE_AUTH_DISABLED,
    SERVICE_TOKEN_IDS_ENV_VAR,
    jwt_unverified_claims,
)
from mcp.http_transport import normalize_url_prefix
from mcp.http_transport import (
    McpHttpHandler as _TransportMcpHttpHandler,
    MudraHttpServer as _TransportMudraHttpServer,
    _port_in_use as _transport_port_in_use,
    run_http as _transport_run_http,
    start_http_thread as _transport_start_http_thread,
)
from mcp.stdio_transport import (
    announce_remote_auth as _transport_announce_remote_auth,
    configure_stdio_utf8 as _transport_configure_stdio_utf8,
    run_stdio as _transport_run_stdio,
)


PROJECT = load_project_descriptor()
REPO_ROOT = PROJECT.repo_root
MCP_GUI_DIR = PROJECT.gui_dir
DB_PATH = PROJECT.db_path
MCP_RESOURCE_PREFIX = f"{PROJECT.resource_scheme}://"
PROTOCOL_VERSION = McpApplication.protocol_version
SUPPORTED_PROTOCOL_VERSIONS = set(McpApplication.supported_protocol_versions)
MAX_BODY_BYTES = 1_000_000
SQLITE_BUSY_TIMEOUT_MS = 30_000

DOC_SCOPES = {key: dict(value) for key, value in PROJECT.doc_scopes.items()}


class ProtocolError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


COMPLEXITY_NUMERIC_SCALE = {"S": 1, "M": 2, "L": 3, "XL": 4}


def dashboard_todo_display_status(todo_status: str, linked_task_status: str = "") -> str:
    status = linked_task_status or todo_status
    return "active" if status == "in_progress" else status


def complexity_numeric(value: Any) -> int | None:
    return COMPLEXITY_NUMERIC_SCALE.get(str(value or "").strip().upper())


def resource_uri(path: str) -> str:
    return MCP_RESOURCE_PREFIX + path.lstrip("/")


# Preserve the established mcp.server transport imports for callers that use the
# facade as their public entrypoint.
McpHttpHandler = _TransportMcpHttpHandler
MudraHttpServer = _TransportMudraHttpServer
RemoteAuthPolicy = _TransportRemoteAuthPolicy
REMOTE_AUTH_DISABLED = _TRANSPORT_REMOTE_AUTH_DISABLED
_port_in_use = _transport_port_in_use
run_http = _transport_run_http
start_http_thread = _transport_start_http_thread
run_stdio = _transport_run_stdio
announce_remote_auth = _transport_announce_remote_auth
configure_stdio_utf8 = _transport_configure_stdio_utf8
class MudraMcpServer:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.project = PROJECT
        self.db_path = db_path
        self.store = McpStore(
            db_path,
            doc_scopes=DOC_SCOPES,
            todo_json_columns=TODO_JSON_LIST_COLUMNS.values(),
        )
        self.guidance = GuidanceService(self.store)
        self.lifecycle = LifecycleService(self.store, project=self.project, guidance=self.guidance)
        self.agent_categories = AgentCategoryService(self.store, lifecycle=self.lifecycle)
        self.todos = TodoService(
            self.store,
            lifecycle=self.lifecycle,
            project=self.project,
            row_to_task=row_to_task,
        )
        self.lifecycle.related_todo_guidance = self.todos.todo_related_for_assignment
        self.orchestration = OrchestrationService(
            self.store,
            lifecycle=self.lifecycle,
            todos=self.todos,
        )
        self.myohid_tests = MyoHidTestService(PROJECT.repo_root)
        self.todos.orchestration = self.orchestration
        self.documents = DocumentationService(self.store, project=self.project)
        self.application = McpApplication(
            self,
            lifecycle=self.lifecycle,
            todos=self.todos,
            documents=self.documents,
            orchestration=self.orchestration,
            myohid_tests=self.myohid_tests,
            agent_categories=self.agent_categories,
            guidance=self.guidance,
            store=self.store,
            project=PROJECT,
            protocol_error_type=ProtocolError,
            tool_execution_error_type=ToolExecutionError,
        )

    # Public compatibility facade.  Application-level MCP behavior lives in
    # mcp.application; these methods preserve the established server surface
    # for HTTP handlers, scripts, and existing integrations.
    def tool_definitions(self) -> list[dict[str, Any]]:
        return self.application.tool_definitions()

    def resource_definitions(self) -> list[dict[str, Any]]:
        return self.application.resource_definitions()

    def resource_templates(self) -> list[dict[str, Any]]:
        return self.application.resource_templates()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.application.call_tool(name, arguments)

    def dashboard_snapshot(self) -> dict[str, Any]:
        return self.application.dashboard_snapshot()

    def status(self) -> dict[str, Any]:
        return self.application.status()

    def read_resource(self, uri: str) -> str:
        return self.application.read_resource(uri)

    def summary_markdown(self) -> str:
        return self.application.summary_markdown()

    def notes_markdown(self, *, limit: int, include_title: bool = True) -> str:
        return self.application.notes_markdown(limit=limit, include_title=include_title)

    def note_markdown(self, key: str) -> str:
        return self.application.note_markdown(key)

    def events_markdown(self, *, limit: int, include_title: bool = True) -> str:
        return self.application.events_markdown(limit=limit, include_title=include_title)

    def tasks_markdown(self, *, limit: int, include_title: bool = True) -> str:
        return self.application.tasks_markdown(limit=limit, include_title=include_title)

    def task_markdown(self, task_key: str) -> str:
        return self.application.task_markdown(task_key)

    def todos_markdown(self, *, limit: int, include_title: bool = True) -> str:
        return self.application.todos_markdown(limit=limit, include_title=include_title)

    def todo_markdown(self, todo_key: str) -> str:
        return self.application.todo_markdown(todo_key)

    def doc_scopes_markdown(self) -> str:
        return self.documents.doc_scopes_markdown()

    def docs_scope_markdown(self, app_scope: str) -> str:
        return self.documents.docs_scope_markdown(app_scope)

    def docs_search_markdown(self, query: str) -> str:
        return self.documents.docs_search_markdown(query)

    def doc_markdown(self, doc_key: str) -> str:
        return self.documents.doc_markdown(doc_key)

    def handle_jsonrpc(self, message: Any) -> Any:
        return self.application.handle_jsonrpc(message)

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.application.dispatch(method, params)

    def handle_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.application.handle_tool_call(params)

    @staticmethod
    def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return McpApplication.error_response(request_id, code, message)

    # TODO domain compatibility delegates.  Persistence, filtering, grouping,
    # assignment cascades, and instruction rendering live in TodoService.
    def todo_add(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.todos.todo_add(args)

    def todo_list(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.todos.todo_list(args)

    def todo_get(self, todo_key: str, *, include_events: bool = True) -> dict[str, Any]:
        return self.todos.todo_get(todo_key, include_events=include_events)

    def todo_prune(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.todos.todo_prune(args)

    def todo_update_priority(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.todos.todo_update_priority(args)

    def todo_update_scope(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.todos.todo_update_scope(args)

    def todo_append_references(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.todos.todo_append_references(args)

    def todo_group_get(self, group_key: str) -> dict[str, Any]:
        return self.todos.todo_group_get(group_key)

    def todo_group_related(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.todos.todo_group_related(args)

    def todo_group_reorder(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.todos.todo_group_reorder(args)

    def todo_auto_group_related(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.todos.todo_auto_group_related(args)

    def todo_related_for_assignment(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.todos.todo_related_for_assignment(args)

    def todo_next_instruction(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.todos.todo_next_instruction(args)

    # Orchestration and Git checkpoint compatibility delegates.
    def orchestration_prompt(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_prompt(args)

    def orchestration_designate(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_designate(args)

    def orchestration_list(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_list(args)

    def orchestration_enqueue(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_enqueue(args)

    def orchestration_heartbeat(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_heartbeat(args)

    def orchestration_wake(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_wake(args)

    def orchestration_claim(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_claim(args)

    def orchestration_launch_prepare(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_launch_prepare(args)

    def orchestration_renew(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_renew(args)

    def orchestration_reconcile(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_reconcile(args)

    def orchestration_release(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_release(args)

    def orchestration_delegate(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_delegate(args)

    def orchestration_complete(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_complete(args)

    def orchestration_checkpoint_prepare(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_checkpoint_prepare(args)

    def orchestration_checkpoint_record(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_checkpoint_record(args)

    def orchestration_retry(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_retry(args)

    def orchestration_drop(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_drop(args)

    def orchestration_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_stop(args)

    def orchestration_cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_cancel(args)

    def orchestration_cancel_ack(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.orchestration_cancel_ack(args)

    def git_status(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.git_status(args)

    def git_diff(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.git_diff(args)

    def git_add(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.git_add(args)

    def git_commit(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.git_commit(args)

    def git_checkpoint_noop(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.git_checkpoint_noop(args)

    def git_push(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.orchestration.git_push(args)

    def render_todo_instruction(
        self,
        todos: list[dict[str, Any]],
        *,
        app_scope: str = "",
        current: dict[str, Any] | None = None,
        related_guidance: dict[str, Any] | None = None,
        variant: str = "implement",
    ) -> str:
        return self.todos.render_todo_instruction(
            todos,
            app_scope=app_scope,
            current=current,
            related_guidance=related_guidance,
            variant=variant,
        )

    @staticmethod
    def todo_feature_guidance_lines(todo: dict[str, Any]) -> list[str]:
        return TodoService.todo_feature_guidance_lines(todo)

    @staticmethod
    def todo_definition_of_done_lines(todo: dict[str, Any]) -> list[str]:
        return TodoService.todo_definition_of_done_lines(todo)

    @staticmethod
    def todo_advisory_lines(todo: dict[str, Any]) -> list[str]:
        return TodoService.todo_advisory_lines(todo)

    @staticmethod
    def todo_docs_scope_hint(todo: dict[str, Any], *, fallback_scope: str = "") -> str:
        return TodoService.todo_docs_scope_hint(todo, fallback_scope=fallback_scope)

    @staticmethod
    def inline_values(values: list[str], *, limit: int = 8) -> str:
        return TodoService.inline_values(values, limit=limit)

    def render_related_todo_cascade(self, guidance: dict[str, Any] | None) -> str:
        return self.todos.render_related_todo_cascade(guidance)

    def connect(self) -> sqlite3.Connection:
        return self.store.connect()

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        with self.store.connection() as conn:
            yield conn

    def ensure_schema(self, conn: sqlite3.Connection | None = None) -> None:
        self.store.ensure_schema(conn)







    # Lifecycle compatibility delegates.  Notes, events, model identity,
    # task state, token usage, and recommendation tracking are owned by
    # LifecycleService; keep this facade source-compatible for direct callers.
    def note_upsert(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.note_upsert(args)

    def note_read(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.note_read(args)

    def note_search(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.note_search(args)

    def note_delete(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.note_delete(args)

    def event_log(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.event_log(args)

    def event_recent(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.event_recent(args)

    def agent_model_register(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.agent_model_register(args)

    def task_check_in(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.task_check_in(args)

    def task_check_out(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.task_check_out(args)

    def task_status(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.task_status(args)

    def task_active(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.task_active(args)

    def task_recent(self, *, limit: int = 50) -> dict[str, Any]:
        return self.lifecycle.task_recent(limit=limit)

    def task_token_usage(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.task_token_usage(args)

    def task_token_recommendation(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.task_token_recommendation(args)

    def guidance_recommendation_add(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.guidance.add(args)

    def guidance_recommendation_list(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.guidance.list(args)

    def guidance_recommendation_reconcile(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.guidance.reconcile(args)

    def update_recommendation_status(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.update_recommendation_status(args)

    def rename_agent_category(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.agent_categories.rename_category(args)

    def agent_category_purge_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.agent_categories.purge_preview(args)

    def agent_category_purge_closed(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.agent_categories.purge_closed(args)

    def insert_task_event(self, conn: sqlite3.Connection, **kwargs: Any) -> None:
        self.lifecycle.insert_task_event(conn, **kwargs)

    def active_task_keys(self) -> set[str]:
        return self.lifecycle.active_task_keys()

    def docs_drift_audit(self, app_scope: str = DOC_DRIFT_AUDIT_SCOPE) -> dict[str, Any]:
        return self.documents.docs_drift_audit(app_scope)

    def validate_app_scope(self, value: str | None) -> str | None:
        return self.documents.validate_app_scope(value)

    def doc_scopes(self) -> dict[str, Any]:
        return self.documents.doc_scopes()

    def doc_scope_entries(self, app_scope: str) -> dict[str, Any]:
        return self.documents.doc_scope_entries(app_scope)

    def store_doc(
        self,
        conn: sqlite3.Connection,
        *,
        doc_key: str,
        app_scope: str,
        title: str,
        content: str,
        source_type: str,
        source_path: str = "",
        summary: str = "",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.documents.store_doc(
            conn,
            doc_key=doc_key,
            app_scope=app_scope,
            title=title,
            content=content,
            source_type=source_type,
            source_path=source_path,
            summary=summary,
            tags=tags,
        )

    def doc_index_repo(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.documents.doc_index_repo(args)

    def ensure_docs_indexed(self) -> None:
        self.documents.ensure_docs_indexed()

    def doc_search(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.documents.doc_search(args)

    def doc_get(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.documents.doc_get(args)

    def doc_upsert(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.documents.doc_upsert(args)













    @staticmethod





    def inline_values(values: list[str], *, limit: int = 8) -> str:
        shown = values[:limit]
        rendered = ", ".join(f"`{value}`" for value in shown)
        if len(values) > limit:
            rendered += f", and {len(values) - limit} more"
        return rendered



    def owner_flush_agent_state(self, args: dict[str, Any]) -> dict[str, Any]:
        reason = optional_string(args, "reason", default="", max_length=2000)
        task_key = optional_string(args, "task_key", default="", max_length=160)
        agent_id = optional_string(args, "agent_id", default="", max_length=160)
        include_stale = bool_arg(args, "include_stale", default=True)
        now = utc_now()
        where = [
            "status IN ('in_progress', 'paused', 'blocked')",
        ]
        params: list[Any] = []
        if task_key:
            where.append("task_key = ?")
            params.append(task_key)
        if agent_id:
            where.append("agent_id = ?")
            params.append(agent_id)
        if not include_stale:
            where.append("expires_at > ?")
            params.append(now)

        with self.connection() as conn:
            targets = conn.execute(
                f"SELECT * FROM agent_task_state WHERE {' AND '.join(where)}",
                params,
            ).fetchall()
            cursor = conn.execute(
                """
                INSERT INTO owner_flushes(ts, reason, flushed_count)
                VALUES (?, ?, ?)
                """,
                (now, reason, len(targets)),
            )
            flush_id = cursor.lastrowid
            for row in targets:
                conn.execute(
                    """
                    UPDATE agent_task_state
                    SET status = 'flushed',
                        last_seen_at = ?,
                        expires_at = ?,
                        flushed_at = ?,
                        flush_id = ?
                    WHERE id = ?
                    """,
                    (now, now, now, flush_id, row["id"]),
                )
                self.insert_task_event(
                    conn,
                    task_state_id=row["id"],
                    task_key=row["task_key"],
                    agent_id=row["agent_id"],
                    action="flush",
                    status="flushed",
                    summary=reason,
                    detail=compact_json(
                        {
                            "owner_route": "/api/dashboard/owner/flush-agent-state",
                            "previous_status": row["status"],
                        }
                    ),
                )
            conn.commit()
            flush = conn.execute(
                "SELECT * FROM owner_flushes WHERE id = ?",
                (flush_id,),
            ).fetchone()
            flushed_rows = conn.execute(
                """
                SELECT * FROM agent_task_state
                WHERE flush_id = ?
                ORDER BY task_key, agent_id
                """,
                (flush_id,),
            ).fetchall()
        return {
            "flush": dict(flush),
            "flushed_tasks": [row_to_task(row) for row in flushed_rows],
        }

    @staticmethod
    def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def parse_args(argv: list[str]) -> argparse.Namespace:
    mcp_config = load_mcp_server_config()
    parser = argparse.ArgumentParser(description=f"Run the repo-local {PROJECT.server_name} server.")
    parser.add_argument(
        "--project-module",
        default=None,
        help=(
            "Project descriptor spec 'package.module:ATTR' supplying scopes, "
            f"branding, and paths (default {DEFAULT_PROJECT_MODULE}). Applied "
            f"before startup; equivalent to setting {PROJECT_MODULE_ENV_VAR}."
        ),
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help=(
            "Run the configured Streamable HTTP-compatible endpoint at /mcp "
            "(default). May be combined with --stdio to also serve the "
            "JavaScript dashboard while speaking stdio."
        ),
    )
    parser.add_argument(
        "--stdio",
        action="store_true",
        help=(
            "Run the line-delimited stdio MCP transport. Combine with --http "
            "to additionally serve the HTTP dashboard from the same process."
        ),
    )
    parser.add_argument(
        "--host",
        default=mcp_config.server_ip,
        help=(
            f"HTTP bind host. Defaults to {MCP_HOST_ENV_VAR}, then "
            "config.yaml mcp.server_ip, then "
            f"{DEFAULT_MCP_SERVER_IP}; non-local binds fall back to 127.0.0.1."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=mcp_config.server_port,
        help=(
            f"HTTP bind port. Defaults to {MCP_PORT_ENV_VAR}, then "
            "config.yaml mcp.server_port, then "
            f"{DEFAULT_MCP_SERVER_PORT}."
        ),
    )
    parser.add_argument(
        "--url-prefix",
        default=mcp_config.url_prefix,
        help=(
            "Optional external URL path prefix to strip before routing, for "
            "example /mudra when served at https://mcp.nml.wtf/mudra."
        ),
    )
    parser.add_argument(
        "--remote-auth",
        choices=sorted(REMOTE_AUTH_MODES),
        default=None,
        help=(
            "Origin-side auth policy for remote deployments. 'cloudflare' "
            "requires a Cf-Access-Jwt-Assertion header plus an allowlisted "
            "CF-Access-Client-Id on every request and denies the owner route. "
            f"Defaults to {REMOTE_AUTH_ENV_VAR}, then off."
        ),
    )
    parser.add_argument(
        "--service-token-id",
        action="append",
        default=[],
        help=(
            "Cloudflare Access service-token Client ID accepted by "
            "--remote-auth cloudflare. Repeatable; extends "
            f"{SERVICE_TOKEN_IDS_ENV_VAR}."
        ),
    )
    parser.add_argument("--init-db", action="store_true", help="Create or migrate mcp.db, then exit.")
    parser.add_argument("--index-docs", action="store_true", help="Index repo docs into mcp.db, then exit.")
    parser.add_argument(
        "--doc-scope",
        choices=sorted(DOC_SCOPES),
        help="Limit --index-docs to one documentation scope.",
    )
    parser.add_argument(
        "--verbose-index",
        action="store_true",
        help="With --index-docs, print the full per-document listing instead of a compact summary.",
    )
    parser.add_argument(
        "--todo-next-instruction",
        action="store_true",
        help="Print copy/paste text for the next local-agent todo, then exit.",
    )
    parser.add_argument("--list-todos", action="store_true", help="List MCP todos, then exit.")
    parser.add_argument("--add-todo", metavar="TODO_KEY", help="Add or update one MCP todo, then exit.")
    parser.add_argument("--prune-todo", metavar="TODO_KEY", help="Mark one MCP todo done or dropped, then exit.")
    parser.add_argument("--todo-title", help="Title for --add-todo.")
    parser.add_argument("--todo-detail", default="", help="Detail for --add-todo or --prune-todo.")
    parser.add_argument("--todo-scope", choices=sorted(DOC_SCOPES), help="Limit todo command to one scope.")
    parser.add_argument("--todo-code-path", action="append", default=[], help="Repo-relative file, directory, or glob for --add-todo. Repeatable.")
    parser.add_argument("--todo-symbol", action="append", default=[], help="Code symbol, table, tool, or module for --add-todo. Repeatable.")
    parser.add_argument("--todo-doc", action="append", default=[], help="MCP documentation doc_key or chunk id for --add-todo. Repeatable.")
    parser.add_argument("--todo-route", action="append", default=[], help="HTTP route, MCP tool, CLI option, or API endpoint for --add-todo. Repeatable.")
    parser.add_argument("--todo-test", action="append", default=[], help="Focused validation command for --add-todo. Repeatable.")
    parser.add_argument("--todo-search", action="append", default=[], help="Fallback focused search query for --add-todo. Repeatable.")
    parser.add_argument("--todo-key", help="Specific todo key for --todo-next-instruction.")
    parser.add_argument(
        "--todo-priority",
        choices=sorted(TODO_PRIORITIES),
        default="P2",
        help="Priority for --add-todo.",
    )
    parser.add_argument(
        "--todo-status",
        choices=sorted(TODO_STATUSES | {"open"}),
        default="open",
        help="Status filter for --list-todos, or done/dropped for --prune-todo.",
    )
    parser.add_argument("--todo-limit", type=int, default=20, help="Todo list or instruction limit.")
    parser.add_argument("--status", action="store_true", help="Print database status JSON, then exit.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.project_module:
        # The flag only takes effect when it is visible in sys.argv at import
        # time (module constants already bound). Reject a mismatched value
        # instead of silently serving the wrong project.
        active_spec = os.environ.get(PROJECT_MODULE_ENV_VAR, "").strip()
        if args.project_module.strip() != active_spec:
            print(
                f"--project-module {args.project_module!r} was not applied at "
                f"startup (active spec: {(active_spec or DEFAULT_PROJECT_MODULE)!r}). "
                f"Set {PROJECT_MODULE_ENV_VAR} before launching instead.",
                file=sys.stderr,
            )
            return 2
    server = MudraMcpServer()
    if args.init_db:
        server.ensure_schema()
        print(f"Initialized {server.db_path}")
        return 0
    if args.index_docs:
        result = server.doc_index_repo(
            {
                "app_scope": args.doc_scope or "",
                "include_manifests": True,
                "verbose": args.verbose_index,
            }
        )
        print(pretty_json(result))
        return 0
    if args.todo_next_instruction:
        result = server.todo_next_instruction(
            {
                "app_scope": args.todo_scope or "",
                "limit": args.todo_limit,
                "todo_key": args.todo_key or "",
            }
        )
        print(result["instruction"])
        return 0
    if args.list_todos:
        result = server.todo_list(
            {
                "status": args.todo_status,
                "app_scope": args.todo_scope or "",
                "limit": args.todo_limit,
            }
        )
        print(pretty_json(result))
        return 0
    if args.add_todo:
        if not args.todo_title:
            print("--add-todo requires --todo-title.", file=sys.stderr)
            return 2
        result = server.todo_add(
            {
                "todo_key": args.add_todo,
                "title": args.todo_title,
                "detail": args.todo_detail,
                "app_scope": args.todo_scope or "",
                "priority": args.todo_priority,
                "source": "cli",
                "tags": [],
                "code_paths": args.todo_code_path,
                "symbol_refs": args.todo_symbol,
                "doc_keys": args.todo_doc,
                "route_refs": args.todo_route,
                "test_refs": args.todo_test,
                "search_queries": args.todo_search,
            }
        )
        print(pretty_json(result))
        return 0
    if args.prune_todo:
        prune_status = args.todo_status if args.todo_status in TODO_TERMINAL_STATUSES else "done"
        result = server.todo_prune(
            {
                "todo_key": args.prune_todo,
                "status": prune_status,
                "actor": "cli",
                "detail": args.todo_detail,
            }
        )
        print(pretty_json(result))
        return 0
    if args.status:
        print(pretty_json(server.status()))
        return 0
    remote_auth = RemoteAuthPolicy.from_env(
        mode=args.remote_auth, extra_service_token_ids=args.service_token_id
    )
    if args.stdio:
        configure_stdio_utf8()
        if args.http:
            # Combined transport: HTTP/dashboard on a daemon thread, stdio in
            # the foreground for the MCP client that spawned this process.
            start_http_thread(
                server, args.host, args.port, url_prefix=args.url_prefix,
                remote_auth=remote_auth,
            )
        run_stdio(server)
    else:
        run_http(
            server, args.host, args.port, url_prefix=args.url_prefix,
            remote_auth=remote_auth,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
