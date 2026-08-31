"""SQLite storage and schema ownership for the local MCP server."""

from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Iterable, Mapping


SQLITE_BUSY_TIMEOUT_MS = 30_000
SCHEMA_VERSION = "14"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class McpStore:
    """Own the MCP SQLite database, schema, migrations, and connections.

    ``doc_scopes`` and ``todo_json_columns`` are injected because their
    definitions belong to the project/application layer.  The store owns the
    persistence representation and never imports the compatibility server.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        doc_scopes: Mapping[str, Mapping[str, Any]],
        todo_json_columns: Iterable[str],
        busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS,
    ) -> None:
        self.db_path = db_path
        self.doc_scopes = doc_scopes
        self.todo_json_columns = tuple(todo_json_columns)
        self.busy_timeout_ms = busy_timeout_ms
        self._schema_lock = threading.RLock()
        self._schema_ready = False

    def _open_connection(self, *, initialize_journal: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1000,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        if initialize_journal:
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def connect(self) -> sqlite3.Connection:
        self.ensure_schema()
        return self._open_connection()

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def ensure_schema(self, conn: sqlite3.Connection | None = None) -> None:
        should_close = conn is None
        with self._schema_lock:
            if self._schema_ready and conn is None:
                return
            if conn is None:
                conn = self._open_connection(initialize_journal=True)
            else:
                conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA journal_mode = WAL")
            try:
                conn.executescript(
                    """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'agent',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT 'agent',
                    event_type TEXT NOT NULL,
                    detail TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_task_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_key TEXT NOT NULL,
                    task_title TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    agent_label TEXT NOT NULL DEFAULT '',
                    model_registration_key TEXT NOT NULL DEFAULT '',
                    canonical_model_label TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    current_step TEXT NOT NULL DEFAULT '',
                    workspace_root TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    completed_at TEXT,
                    flushed_at TEXT,
                    flush_id INTEGER,
                    UNIQUE(task_key, agent_id),
                    FOREIGN KEY(flush_id) REFERENCES owner_flushes(id)
                );

                CREATE TABLE IF NOT EXISTS agent_task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_state_id INTEGER,
                    task_key TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(task_state_id) REFERENCES agent_task_state(id)
                );

                CREATE TABLE IF NOT EXISTS agent_task_token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_key TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    model_descriptor TEXT NOT NULL DEFAULT '',
                    model_registration_key TEXT NOT NULL DEFAULT '',
                    canonical_model_label TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    recommendation TEXT NOT NULL DEFAULT '',
                    recommendation_ts TEXT,
                    UNIQUE(task_key, agent_id)
                );

                CREATE TABLE IF NOT EXISTS guidance_recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recommendation_key TEXT NOT NULL UNIQUE,
                    proposed_guidance TEXT NOT NULL,
                    target_documents_json TEXT NOT NULL DEFAULT '["AGENTS.md"]',
                    status TEXT NOT NULL DEFAULT 'open',
                    resolution TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS guidance_recommendation_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recommendation_key TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    task_key TEXT,
                    agent_id TEXT,
                    evidence TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    UNIQUE(recommendation_key, source_kind, source_ref),
                    FOREIGN KEY(recommendation_key) REFERENCES guidance_recommendations(recommendation_key)
                );
                CREATE TABLE IF NOT EXISTS guidance_recommendation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recommendation_key TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(recommendation_key) REFERENCES guidance_recommendations(recommendation_key)
                );

                CREATE TABLE IF NOT EXISTS agent_model_registrations (
                    registration_key TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    session_hint TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL,
                    model_family TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    model_variant TEXT NOT NULL DEFAULT '',
                    reasoning_effort TEXT NOT NULL,
                    client_name TEXT NOT NULL DEFAULT '',
                    canonical_model_label TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(agent_id, session_hint)
                );

                CREATE TABLE IF NOT EXISTS agent_category_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alias_label TEXT NOT NULL UNIQUE,
                    canonical_label TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS owner_flushes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    flushed_count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS doc_app_scopes (
                    scope TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    root_paths_json TEXT NOT NULL,
                    display_order INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS doc_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_key TEXT NOT NULL UNIQUE,
                    app_scope TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_path TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    content_hash TEXT NOT NULL DEFAULT '',
                    indexed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(app_scope) REFERENCES doc_app_scopes(scope)
                );

                CREATE TABLE IF NOT EXISTS doc_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    heading TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(doc_id, chunk_index),
                    FOREIGN KEY(doc_id) REFERENCES doc_entries(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS mcp_todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    todo_key TEXT NOT NULL UNIQUE,
                    app_scope TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'suggested',
                    priority TEXT NOT NULL DEFAULT 'P2',
                    source TEXT NOT NULL DEFAULT 'agent',
                    source_task_key TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    code_paths_json TEXT NOT NULL DEFAULT '[]',
                    symbol_refs_json TEXT NOT NULL DEFAULT '[]',
                    doc_keys_json TEXT NOT NULL DEFAULT '[]',
                    route_refs_json TEXT NOT NULL DEFAULT '[]',
                    test_refs_json TEXT NOT NULL DEFAULT '[]',
                    search_queries_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS mcp_todo_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    todo_id INTEGER,
                    todo_key TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT 'agent',
                    detail TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(todo_id) REFERENCES mcp_todos(id)
                );

                CREATE TABLE IF NOT EXISTS mcp_todo_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_key TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    group_type TEXT NOT NULL DEFAULT 'manual',
                    source TEXT NOT NULL DEFAULT 'agent',
                    criteria_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mcp_todo_group_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    todo_id INTEGER,
                    todo_key TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    relation_kind TEXT NOT NULL DEFAULT 'manual',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(group_id, todo_key),
                    FOREIGN KEY(group_id) REFERENCES mcp_todo_groups(id) ON DELETE CASCADE,
                    FOREIGN KEY(todo_id) REFERENCES mcp_todos(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS mcp_orchestrator_assignments (
                    assignment_key TEXT PRIMARY KEY,
                    task_key TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    model_registration_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model_family TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    model_variant TEXT NOT NULL DEFAULT '',
                    reasoning_effort TEXT NOT NULL,
                    canonical_model_label TEXT NOT NULL,
                    client_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    stop_requested_at TEXT,
                    stop_acknowledged_at TEXT,
                    stop_reason TEXT NOT NULL DEFAULT '',
                    stop_idempotency_key TEXT NOT NULL DEFAULT '',
                    stop_terminal_evidence TEXT NOT NULL DEFAULT '',
                    repo_root TEXT NOT NULL,
                    workspace_id TEXT NOT NULL DEFAULT '',
                    workspace_capability TEXT NOT NULL DEFAULT 'client_local',
                    branch TEXT NOT NULL,
                    allowed_paths_json TEXT NOT NULL DEFAULT '[]',
                    checkpoint_required INTEGER NOT NULL DEFAULT 1,
                    checkpoint_failure_policy TEXT NOT NULL DEFAULT 'pause',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(task_key, agent_id)
                );

                CREATE TABLE IF NOT EXISTS mcp_orchestration_queue (
                    queue_key TEXT PRIMARY KEY,
                    todo_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    position INTEGER NOT NULL,
                    todo_snapshot_json TEXT NOT NULL,
                    prompt_snapshot TEXT NOT NULL,
                    assignment_key TEXT NOT NULL,
                    orchestrator_task_key TEXT NOT NULL,
                    orchestrator_agent_id TEXT NOT NULL,
                    model_registration_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model_family TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    model_variant TEXT NOT NULL DEFAULT '',
                    reasoning_effort TEXT NOT NULL,
                    canonical_model_label TEXT NOT NULL,
                    client_name TEXT NOT NULL DEFAULT '',
                    writer_assignment_key TEXT NOT NULL DEFAULT '',
                    writer_repo_root TEXT NOT NULL DEFAULT '',
                    writer_workspace_id TEXT NOT NULL DEFAULT '',
                    writer_branch TEXT NOT NULL DEFAULT '',
                    enqueue_idempotency_key TEXT NOT NULL UNIQUE,
                    dispatch_idempotency_key TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    claim_lease_token_hash TEXT NOT NULL DEFAULT '',
                    claim_expires_at TEXT,
                    launch_key TEXT NOT NULL DEFAULT '',
                    launch_prepared_at TEXT,
                    accepted_model_registration_key TEXT NOT NULL DEFAULT '',
                    accepted_provider TEXT NOT NULL DEFAULT '',
                    accepted_model_family TEXT NOT NULL DEFAULT '',
                    accepted_model_version TEXT NOT NULL DEFAULT '',
                    accepted_model_variant TEXT NOT NULL DEFAULT '',
                    accepted_reasoning_effort TEXT NOT NULL DEFAULT '',
                    accepted_client_name TEXT NOT NULL DEFAULT '',
                    recovery_reason TEXT NOT NULL DEFAULT '',
                    claim_idempotency_key TEXT NOT NULL DEFAULT '',
                    delegate_idempotency_key TEXT NOT NULL DEFAULT '',
                    completion_idempotency_key TEXT NOT NULL DEFAULT '',
                    child_session_id TEXT NOT NULL DEFAULT '',
                    child_outcome TEXT NOT NULL DEFAULT '',
                    cancel_requested_at TEXT,
                    cancel_acknowledged_at TEXT,
                    cancel_reason TEXT NOT NULL DEFAULT '',
                    cancel_ack_idempotency_key TEXT NOT NULL DEFAULT '',
                    cancel_child_outcome TEXT NOT NULL DEFAULT '',
                    cancel_child_evidence TEXT NOT NULL DEFAULT '',
                    checkpoint_required INTEGER NOT NULL DEFAULT 1,
                    checkpoint_failure_policy TEXT NOT NULL DEFAULT 'pause',
                    checkpoint_status TEXT NOT NULL DEFAULT 'pending',
                    checkpoint_idempotency_key TEXT NOT NULL DEFAULT '',
                    checkpoint_key TEXT NOT NULL DEFAULT '',
                    checkpoint_prepared_at TEXT,
                    checkpoint_commit TEXT NOT NULL DEFAULT '',
                    checkpoint_commit_recorded_at TEXT,
                    checkpoint_push_target TEXT NOT NULL DEFAULT '',
                    checkpoint_evidence_summary TEXT NOT NULL DEFAULT '',
                    checkpoint_error TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    enqueued_at TEXT NOT NULL,
                    dispatched_at TEXT,
                    claimed_at TEXT,
                    delegated_at TEXT,
                    completed_at TEXT,
                    cancelled_at TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(assignment_key) REFERENCES mcp_orchestrator_assignments(assignment_key)
                );

                CREATE TABLE IF NOT EXISTS mcp_orchestration_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_key TEXT NOT NULL,
                    todo_key TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    UNIQUE(queue_key, event_type, idempotency_key),
                    FOREIGN KEY(queue_key) REFERENCES mcp_orchestration_queue(queue_key) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_notes_updated_at
                    ON notes(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_ts
                    ON events(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_events_type_ts
                    ON events(event_type, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_guidance_recommendations_status_updated
                    ON guidance_recommendations(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_guidance_evidence_task
                    ON guidance_recommendation_evidence(task_key, agent_id, observed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_guidance_events_key_ts
                    ON guidance_recommendation_events(recommendation_key, ts DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_task_state_task_key
                    ON agent_task_state(task_key);
                CREATE INDEX IF NOT EXISTS idx_agent_task_state_active
                    ON agent_task_state(status, expires_at, last_seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_task_events_task_ts
                    ON agent_task_events(task_key, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_task_events_state_ts
                    ON agent_task_events(task_state_id, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_task_token_usage_ts
                    ON agent_task_token_usage(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_model_registrations_agent
                    ON agent_model_registrations(agent_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_category_aliases_canonical
                    ON agent_category_aliases(canonical_label);
                CREATE INDEX IF NOT EXISTS idx_doc_entries_scope
                    ON doc_entries(app_scope, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_doc_entries_source
                    ON doc_entries(source_type, source_path);
                CREATE INDEX IF NOT EXISTS idx_doc_chunks_doc
                    ON doc_chunks(doc_id, chunk_index);
                CREATE INDEX IF NOT EXISTS idx_mcp_todos_status_priority
                    ON mcp_todos(status, priority, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_mcp_todos_scope_status
                    ON mcp_todos(app_scope, status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_mcp_todo_events_key_ts
                    ON mcp_todo_events(todo_key, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_mcp_todo_groups_key
                    ON mcp_todo_groups(group_key);
                CREATE INDEX IF NOT EXISTS idx_mcp_todo_group_members_todo
                    ON mcp_todo_group_members(todo_key, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_mcp_todo_group_members_group
                    ON mcp_todo_group_members(group_id, todo_key);
                CREATE INDEX IF NOT EXISTS idx_orchestrator_assignments_status
                    ON mcp_orchestrator_assignments(status, expires_at, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_orchestration_queue_status_position
                    ON mcp_orchestration_queue(status, position, enqueued_at);
                CREATE INDEX IF NOT EXISTS idx_orchestration_queue_todo
                    ON mcp_orchestration_queue(todo_key, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_orchestration_queue_assignment
                    ON mcp_orchestration_queue(assignment_key, status, position);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_orchestration_active_todo
                    ON mcp_orchestration_queue(todo_key)
                    WHERE status IN ('queued', 'dispatched', 'claimed', 'delegated');
                CREATE INDEX IF NOT EXISTS idx_orchestration_events_queue_ts
                    ON mcp_orchestration_events(queue_key, ts DESC, id DESC);
                """
                )
                self.ensure_todo_reference_columns(conn)
                self.ensure_complexity_columns(conn)
                self.ensure_token_usage_columns(conn)
                self.ensure_guidance_backfill(conn)
                self.ensure_model_identity_columns(conn)
                # Must follow ensure_model_identity_columns: the backfill reads
                # canonical_model_label as its highest-priority source.
                self.ensure_model_breakdown_columns(conn)
                self.ensure_todo_group_position_columns(conn)
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_mcp_todo_group_members_position
                    ON mcp_todo_group_members(group_id, position)
                    """
                )
                self.ensure_orchestration_cancellation_columns(conn)
                self.ensure_orchestration_lease_columns(conn)
                self.ensure_orchestration_workspace_checkpoint_columns(conn)
                self.ensure_orchestrator_stop_columns(conn)
                self.ensure_orchestration_drop_columns(conn)
                conn.execute("DROP INDEX IF EXISTS idx_orchestration_active_todo")
                conn.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS idx_orchestration_active_todo
                       ON mcp_orchestration_queue(todo_key)
                       WHERE status IN ('queued', 'dispatched', 'claimed', 'launching', 'delegated', 'recovery_required')"""
                )
                conn.execute("DROP INDEX IF EXISTS idx_orchestrator_writer")
                conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_orchestrator_writer
                       ON mcp_orchestrator_assignments(workspace_id, branch, status, expires_at)"""
                )
                conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_orchestration_claim_expiry
                       ON mcp_orchestration_queue(status, claim_expires_at)"""
                )
                now = utc_now()
                conn.execute(
                    """
                INSERT INTO meta(key, value, updated_at)
                VALUES ('schema_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                    (SCHEMA_VERSION, now),
                )
                for scope, config in self.doc_scopes.items():
                    conn.execute(
                        """
                    INSERT INTO doc_app_scopes(
                        scope, title, description, root_paths_json, display_order, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope) DO UPDATE SET
                        title = excluded.title,
                        description = excluded.description,
                        root_paths_json = excluded.root_paths_json,
                        display_order = excluded.display_order,
                        updated_at = excluded.updated_at
                    """,
                        (
                            scope,
                            config["title"],
                            config["description"],
                            compact_json(config["roots"]),
                            config["display_order"],
                            now,
                        ),
                    )
                conn.commit()
                self._schema_ready = True
            finally:
                if should_close:
                    conn.close()

    def _table_columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        return {
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def ensure_todo_reference_columns(self, conn: sqlite3.Connection) -> None:
        existing = self._table_columns(conn, "mcp_todos")
        for column_name in self.todo_json_columns:
            if column_name not in existing:
                conn.execute(
                    f"ALTER TABLE mcp_todos ADD COLUMN {column_name} TEXT NOT NULL DEFAULT '[]'"
                )

    def ensure_complexity_columns(self, conn: sqlite3.Connection) -> None:
        existing = self._table_columns(conn, "mcp_todos")
        if "planned_complexity" not in existing:
            conn.execute("ALTER TABLE mcp_todos ADD COLUMN planned_complexity TEXT")
        if "actual_complexity" not in existing:
            conn.execute("ALTER TABLE mcp_todos ADD COLUMN actual_complexity TEXT")

    def ensure_token_usage_columns(self, conn: sqlite3.Connection) -> None:
        existing = self._table_columns(conn, "agent_task_token_usage")
        if "recommendation_status" not in existing:
            conn.execute(
                "ALTER TABLE agent_task_token_usage ADD COLUMN recommendation_status TEXT NOT NULL DEFAULT 'open'"
            )
        if "recommendation_status_updated_at" not in existing:
            conn.execute(
                "ALTER TABLE agent_task_token_usage ADD COLUMN recommendation_status_updated_at TEXT"
            )

    def ensure_guidance_backfill(self, conn: sqlite3.Connection) -> None:
        """Import each current legacy recommendation exactly once, preserving its limits."""
        from mcp.guidance import recommendation_key_for
        required = {"task_key", "agent_id", "recommendation", "recommendation_ts"}
        if not required.issubset(self._table_columns(conn, "agent_task_token_usage")):
            return
        rows = conn.execute("SELECT task_key, agent_id, recommendation, recommendation_ts FROM agent_task_token_usage WHERE trim(recommendation) <> ''").fetchall()
        for row in rows:
            text = row["recommendation"].strip(); key = recommendation_key_for(text); now = row["recommendation_ts"] or utc_now()
            conn.execute("INSERT OR IGNORE INTO guidance_recommendations(recommendation_key,proposed_guidance,target_documents_json,status,resolution,created_at,updated_at) VALUES (?,?,'[\"AGENTS.md\"]','open','',?,?)", (key,text,now,now))
            conn.execute("INSERT OR IGNORE INTO guidance_recommendation_evidence(recommendation_key,source_kind,source_ref,task_key,agent_id,evidence,observed_at) VALUES (?,?,?, ?,?,?,?)", (key,"task_token",f"{row['task_key']}:{row['agent_id']}",row["task_key"],row["agent_id"],text,now))

    def ensure_model_identity_columns(self, conn: sqlite3.Connection) -> None:
        for table in ("agent_task_state", "agent_task_token_usage"):
            existing = self._table_columns(conn, table)
            for column_name in ("model_registration_key", "canonical_model_label"):
                if column_name not in existing:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column_name} TEXT NOT NULL DEFAULT ''"
                    )

    def ensure_model_breakdown_columns(self, conn: sqlite3.Connection) -> None:
        """Add resolved model family/version/variant/effort columns and backfill.

        Historic rows recorded only free text (`agent_label`, `model_descriptor`),
        so the dashboard had to re-guess the model on every render. These columns
        capture one resolved identity per row. The backfill runs once: rows that
        already have a non-empty `resolved_model_label` are skipped, so restarts
        and later migrations do not re-derive or overwrite them.
        """
        columns = {
            "resolved_model_label": "TEXT NOT NULL DEFAULT ''",
            "resolved_model_family": "TEXT NOT NULL DEFAULT ''",
            "resolved_model_version": "TEXT NOT NULL DEFAULT ''",
            "resolved_model_variant": "TEXT NOT NULL DEFAULT ''",
            "resolved_reasoning_effort": "TEXT NOT NULL DEFAULT ''",
        }
        # `agent_label` on task state, `model_descriptor` on token usage.
        sources = {
            "agent_task_state": "agent_label",
            "agent_task_token_usage": "model_descriptor",
        }
        for table, free_text_column in sources.items():
            existing = self._table_columns(conn, table)
            for name, decl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            self._backfill_model_breakdown(
                conn, table, free_text_column, existing | set(columns)
            )

    @staticmethod
    def _backfill_model_breakdown(
        conn: sqlite3.Connection,
        table: str,
        free_text_column: str,
        available: set[str],
    ) -> None:
        from mcp.model_identity import resolve_model_identity

        # Older databases predate some source columns; select only what exists so
        # the migration works from any prior schema version.
        sources = [
            column
            for column in ("canonical_model_label", free_text_column, "agent_id")
            if column in available
        ]
        if not sources:
            return
        rows = conn.execute(
            f"""
            SELECT id, {', '.join(sources)}
            FROM {table}
            WHERE resolved_model_label = ''
            """
        ).fetchall()
        for row in rows:
            # Prefer the registered canonical label, then the free-text descriptor,
            # then the agent id as a last resort.
            identity = resolve_model_identity(*(row[column] or "" for column in sources))
            if not identity:
                continue
            conn.execute(
                f"""
                UPDATE {table}
                SET resolved_model_label = ?, resolved_model_family = ?,
                    resolved_model_version = ?, resolved_model_variant = ?,
                    resolved_reasoning_effort = ?
                WHERE id = ?
                """,
                (
                    identity.label, identity.family, identity.version,
                    identity.variant, identity.effort, row["id"],
                ),
            )

    def ensure_todo_group_position_columns(self, conn: sqlite3.Connection) -> None:
        """Add and backfill stable dense member positions for todo groups."""
        existing = self._table_columns(conn, "mcp_todo_group_members")
        if "position" not in existing:
            conn.execute(
                "ALTER TABLE mcp_todo_group_members ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
            )
        groups = conn.execute(
            "SELECT id FROM mcp_todo_groups ORDER BY id ASC"
        ).fetchall()
        for group in groups:
            members = conn.execute(
                """
                SELECT m.id
                FROM mcp_todo_group_members m
                JOIN mcp_todos t ON t.todo_key = m.todo_key
                WHERE m.group_id = ?
                ORDER BY
                    CASE t.priority
                        WHEN 'P0' THEN 0
                        WHEN 'P1' THEN 1
                        WHEN 'P2' THEN 2
                        WHEN 'P3' THEN 3
                        ELSE 4
                    END,
                    t.updated_at DESC,
                    t.id DESC
                """,
                (group["id"],),
            ).fetchall()
            if not members:
                continue
            has_missing_position = conn.execute(
                """
                SELECT 1
                FROM mcp_todo_group_members
                WHERE group_id = ? AND position < 1
                LIMIT 1
                """,
                (group["id"],),
            ).fetchone()
            if not has_missing_position:
                continue
            for position, member in enumerate(members, start=1):
                conn.execute(
                    "UPDATE mcp_todo_group_members SET position = ? WHERE id = ?",
                    (position, member["id"]),
                )

    def ensure_orchestration_cancellation_columns(self, conn: sqlite3.Connection) -> None:
        existing = self._table_columns(conn, "mcp_orchestration_queue")
        columns = {
            "cancel_requested_at": "TEXT",
            "cancel_acknowledged_at": "TEXT",
            "cancel_reason": "TEXT NOT NULL DEFAULT ''",
            "cancel_ack_idempotency_key": "TEXT NOT NULL DEFAULT ''",
            "cancel_child_outcome": "TEXT NOT NULL DEFAULT ''",
            "cancel_child_evidence": "TEXT NOT NULL DEFAULT ''",
        }
        for column_name, definition in columns.items():
            if column_name not in existing:
                conn.execute(
                    f"ALTER TABLE mcp_orchestration_queue ADD COLUMN {column_name} {definition}"
                )

    def ensure_orchestration_lease_columns(self, conn: sqlite3.Connection) -> None:
        """Add attempt, claim lease, launch, model, writer, and recovery fields."""
        existing = self._table_columns(conn, "mcp_orchestration_queue")
        columns = {
            "client_name": "TEXT NOT NULL DEFAULT ''",
            "writer_assignment_key": "TEXT NOT NULL DEFAULT ''",
            "writer_repo_root": "TEXT NOT NULL DEFAULT ''",
            "writer_branch": "TEXT NOT NULL DEFAULT ''",
            "attempt": "INTEGER NOT NULL DEFAULT 1",
            "claim_lease_token_hash": "TEXT NOT NULL DEFAULT ''",
            "claim_expires_at": "TEXT",
            "launch_key": "TEXT NOT NULL DEFAULT ''",
            "launch_prepared_at": "TEXT",
            "accepted_model_registration_key": "TEXT NOT NULL DEFAULT ''",
            "accepted_provider": "TEXT NOT NULL DEFAULT ''",
            "accepted_model_family": "TEXT NOT NULL DEFAULT ''",
            "accepted_model_version": "TEXT NOT NULL DEFAULT ''",
            "accepted_model_variant": "TEXT NOT NULL DEFAULT ''",
            "accepted_reasoning_effort": "TEXT NOT NULL DEFAULT ''",
            "accepted_client_name": "TEXT NOT NULL DEFAULT ''",
            "recovery_reason": "TEXT NOT NULL DEFAULT ''",
        }
        for column_name, definition in columns.items():
            if column_name not in existing:
                conn.execute(
                    f"ALTER TABLE mcp_orchestration_queue ADD COLUMN {column_name} {definition}"
                )
        conn.execute(
            "UPDATE mcp_orchestration_queue SET attempt=CASE WHEN retry_count < 0 THEN 1 ELSE retry_count + 1 END"
        )
        conn.execute(
            """UPDATE mcp_orchestration_queue
               SET client_name=COALESCE(NULLIF(client_name, ''), ''),
                   writer_assignment_key=COALESCE(NULLIF(writer_assignment_key, ''), assignment_key),
                   launch_key=COALESCE(NULLIF(launch_key, ''), '')"""
        )

    def ensure_orchestration_drop_columns(self, conn: sqlite3.Connection) -> None:
        """Add the soft-delete timestamp for owner-dropped terminal queue rows.

        A dropped row keeps its full history (events, prompt snapshot, model
        identity) but is filtered out of the dashboard queue list, so evidence
        is preserved without forcing a Retry on cancelled/failed/stale work.
        """
        existing = self._table_columns(conn, "mcp_orchestration_queue")
        if "dropped_at" not in existing:
            conn.execute(
                "ALTER TABLE mcp_orchestration_queue ADD COLUMN dropped_at TEXT"
            )

    def ensure_orchestrator_stop_columns(self, conn: sqlite3.Connection) -> None:
        """Add durable cooperative-stop state to orchestrator assignments."""
        existing = self._table_columns(conn, "mcp_orchestrator_assignments")
        columns = {
            "stop_requested_at": "TEXT",
            "stop_acknowledged_at": "TEXT",
            "stop_reason": "TEXT NOT NULL DEFAULT ''",
            "stop_idempotency_key": "TEXT NOT NULL DEFAULT ''",
            "stop_terminal_evidence": "TEXT NOT NULL DEFAULT ''",
        }
        for column_name, definition in columns.items():
            if column_name not in existing:
                conn.execute(
                    f"ALTER TABLE mcp_orchestrator_assignments ADD COLUMN {column_name} {definition}"
                )

    def ensure_orchestration_workspace_checkpoint_columns(
        self, conn: sqlite3.Connection
    ) -> None:
        """Add opaque workspace ownership and client checkpoint result fields."""
        assignment_columns = self._table_columns(conn, "mcp_orchestrator_assignments")
        if "workspace_id" not in assignment_columns:
            conn.execute(
                "ALTER TABLE mcp_orchestrator_assignments ADD COLUMN workspace_id TEXT NOT NULL DEFAULT ''"
            )
        if "workspace_capability" not in assignment_columns:
            # Existing designations keep the legacy server-local Git capability so
            # in-flight checkpoints stay operable; new designations must opt in.
            conn.execute(
                "ALTER TABLE mcp_orchestrator_assignments "
                "ADD COLUMN workspace_capability TEXT NOT NULL DEFAULT 'server_local'"
            )
        queue_columns = self._table_columns(conn, "mcp_orchestration_queue")
        columns = {
            "writer_workspace_id": "TEXT NOT NULL DEFAULT ''",
            "checkpoint_key": "TEXT NOT NULL DEFAULT ''",
            "checkpoint_prepared_at": "TEXT",
            "checkpoint_commit_recorded_at": "TEXT",
            "checkpoint_push_target": "TEXT NOT NULL DEFAULT ''",
            "checkpoint_evidence_summary": "TEXT NOT NULL DEFAULT ''",
        }
        for column_name, definition in columns.items():
            if column_name not in queue_columns:
                conn.execute(
                    f"ALTER TABLE mcp_orchestration_queue ADD COLUMN {column_name} {definition}"
                )
        # Historical rows remain operable through a bounded opaque legacy key.
        conn.execute(
            """UPDATE mcp_orchestrator_assignments
               SET workspace_id='legacy_' || substr(lower(hex(randomblob(16))), 1, 32)
               WHERE workspace_id=''"""
        )
        conn.execute(
            """UPDATE mcp_orchestration_queue
               SET writer_workspace_id=COALESCE(
                   NULLIF(writer_workspace_id, ''),
                   (SELECT workspace_id FROM mcp_orchestrator_assignments a
                    WHERE a.assignment_key=mcp_orchestration_queue.assignment_key),
                   ''
               )
               WHERE writer_workspace_id=''"""
        )
