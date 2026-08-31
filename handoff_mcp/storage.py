"""SQLite persistence, scoped to a single project.

One database file holds records for every project that uses this server; each
row carries a ``project_key`` and every statement issued by :class:`Store`
filters on the key bound at construction time. A :class:`Store` instance can
therefore only ever see one project's data.

The schema is intentionally small:

``todos``
    Local next-step items. ``status`` is one of ``open`` / ``done`` /
    ``dropped``. ``priority`` is a small integer (lower sorts first).

``handoffs``
    Session-to-session breadcrumbs — the richer "here is where I left off /
    here is what the next worker should do" note. ``status`` is ``open`` /
    ``resolved``.

Both tables share a monotonic ``seq`` per project so items get short,
human-quotable ids (``T-3``, ``H-7``) that are stable and easy to paste.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

TODO_STATUSES = ("open", "done", "dropped")
HANDOFF_STATUSES = ("open", "resolved")

#: Environment override for the database location (mainly for tests).
DB_PATH_ENV_VAR = "HANDOFF_MCP_DB"


def default_db_path() -> Path:
    """Return the shared database path under the user's home directory."""

    override = os.environ.get(DB_PATH_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".handoff-mcp" / "handoff.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    """Project-scoped accessor over the shared SQLite database."""

    def __init__(self, project_key: str, db_path: Path | None = None) -> None:
        if not project_key:
            raise ValueError("project_key is required for isolation.")
        self.project_key = project_key
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    # -- connection / schema -------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS project_counters (
                    project_key TEXT NOT NULL,
                    kind        TEXT NOT NULL,
                    next_seq    INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (project_key, kind)
                );

                CREATE TABLE IF NOT EXISTS todos (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_key  TEXT NOT NULL,
                    seq          INTEGER NOT NULL,
                    title        TEXT NOT NULL,
                    detail       TEXT NOT NULL DEFAULT '',
                    status       TEXT NOT NULL DEFAULT 'open',
                    priority     INTEGER NOT NULL DEFAULT 3,
                    tags         TEXT NOT NULL DEFAULT '',
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL,
                    UNIQUE (project_key, seq)
                );

                CREATE TABLE IF NOT EXISTS handoffs (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_key   TEXT NOT NULL,
                    seq           INTEGER NOT NULL,
                    summary       TEXT NOT NULL,
                    next_steps    TEXT NOT NULL DEFAULT '',
                    context       TEXT NOT NULL DEFAULT '',
                    references_   TEXT NOT NULL DEFAULT '',
                    status        TEXT NOT NULL DEFAULT 'open',
                    author        TEXT NOT NULL DEFAULT '',
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL,
                    UNIQUE (project_key, seq)
                );

                CREATE INDEX IF NOT EXISTS idx_todos_project_status
                    ON todos (project_key, status);
                CREATE INDEX IF NOT EXISTS idx_handoffs_project_status
                    ON handoffs (project_key, status);
                """
            )
            conn.commit()

    def _next_seq(self, conn: sqlite3.Connection, kind: str) -> int:
        conn.execute(
            "INSERT INTO project_counters(project_key, kind, next_seq) VALUES (?, ?, 1) "
            "ON CONFLICT(project_key, kind) DO NOTHING",
            (self.project_key, kind),
        )
        row = conn.execute(
            "SELECT next_seq FROM project_counters WHERE project_key=? AND kind=?",
            (self.project_key, kind),
        ).fetchone()
        seq = int(row["next_seq"])
        conn.execute(
            "UPDATE project_counters SET next_seq=? WHERE project_key=? AND kind=?",
            (seq + 1, self.project_key, kind),
        )
        return seq

    # -- todos ---------------------------------------------------------------

    def add_todo(
        self,
        *,
        title: str,
        detail: str = "",
        priority: int = 3,
        tags: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            seq = self._next_seq(conn, "todo")
            conn.execute(
                "INSERT INTO todos(project_key, seq, title, detail, status, priority, tags, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)",
                (self.project_key, seq, title, detail, priority, tags, now, now),
            )
            conn.commit()
        return self.get_todo(seq)

    def get_todo(self, seq: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM todos WHERE project_key=? AND seq=?",
                (self.project_key, seq),
            ).fetchone()
        if row is None:
            raise KeyError(f"No todo T-{seq} in this project.")
        return _todo_row(row)

    def list_todos(
        self,
        *,
        status: str | None = "open",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = ["project_key=?"]
        params: list[Any] = [self.project_key]
        if status:
            clauses.append("status=?")
            params.append(status)
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM todos WHERE "
                + " AND ".join(clauses)
                + " ORDER BY priority ASC, seq ASC LIMIT ?",
                params,
            ).fetchall()
        return [_todo_row(row) for row in rows]

    def update_todo(
        self,
        seq: int,
        *,
        status: str | None = None,
        priority: int | None = None,
        title: str | None = None,
        detail: str | None = None,
        tags: str | None = None,
    ) -> dict[str, Any]:
        updates: list[str] = []
        params: list[Any] = []
        if status is not None:
            if status not in TODO_STATUSES:
                raise ValueError(f"status must be one of {TODO_STATUSES}.")
            updates.append("status=?")
            params.append(status)
        if priority is not None:
            updates.append("priority=?")
            params.append(int(priority))
        if title is not None:
            updates.append("title=?")
            params.append(title)
        if detail is not None:
            updates.append("detail=?")
            params.append(detail)
        if tags is not None:
            updates.append("tags=?")
            params.append(tags)
        if not updates:
            return self.get_todo(seq)
        updates.append("updated_at=?")
        params.append(utc_now())
        params.extend([self.project_key, seq])
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE todos SET " + ", ".join(updates) + " WHERE project_key=? AND seq=?",
                params,
            )
            conn.commit()
            if cur.rowcount == 0:
                raise KeyError(f"No todo T-{seq} in this project.")
        return self.get_todo(seq)

    # -- handoffs ------------------------------------------------------------

    def add_handoff(
        self,
        *,
        summary: str,
        next_steps: str = "",
        context: str = "",
        references: str = "",
        author: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            seq = self._next_seq(conn, "handoff")
            conn.execute(
                "INSERT INTO handoffs(project_key, seq, summary, next_steps, context, "
                "references_, status, author, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
                (self.project_key, seq, summary, next_steps, context, references, author, now, now),
            )
            conn.commit()
        return self.get_handoff(seq)

    def get_handoff(self, seq: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM handoffs WHERE project_key=? AND seq=?",
                (self.project_key, seq),
            ).fetchone()
        if row is None:
            raise KeyError(f"No handoff H-{seq} in this project.")
        return _handoff_row(row)

    def list_handoffs(
        self,
        *,
        status: str | None = "open",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = ["project_key=?"]
        params: list[Any] = [self.project_key]
        if status:
            clauses.append("status=?")
            params.append(status)
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM handoffs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY seq DESC LIMIT ?",
                params,
            ).fetchall()
        return [_handoff_row(row) for row in rows]

    def update_handoff(
        self,
        seq: int,
        *,
        status: str | None = None,
        summary: str | None = None,
        next_steps: str | None = None,
        context: str | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        updates: list[str] = []
        params: list[Any] = []
        if status is not None:
            if status not in HANDOFF_STATUSES:
                raise ValueError(f"status must be one of {HANDOFF_STATUSES}.")
            updates.append("status=?")
            params.append(status)
        if summary is not None:
            updates.append("summary=?")
            params.append(summary)
        if next_steps is not None:
            updates.append("next_steps=?")
            params.append(next_steps)
        if context is not None:
            updates.append("context=?")
            params.append(context)
        if references is not None:
            updates.append("references_=?")
            params.append(references)
        if not updates:
            return self.get_handoff(seq)
        updates.append("updated_at=?")
        params.append(utc_now())
        params.extend([self.project_key, seq])
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE handoffs SET " + ", ".join(updates) + " WHERE project_key=? AND seq=?",
                params,
            )
            conn.commit()
            if cur.rowcount == 0:
                raise KeyError(f"No handoff H-{seq} in this project.")
        return self.get_handoff(seq)

    # -- introspection (used by the GUI) ------------------------------------

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            open_todos = conn.execute(
                "SELECT COUNT(*) FROM todos WHERE project_key=? AND status='open'",
                (self.project_key,),
            ).fetchone()[0]
            open_handoffs = conn.execute(
                "SELECT COUNT(*) FROM handoffs WHERE project_key=? AND status='open'",
                (self.project_key,),
            ).fetchone()[0]
        return {"open_todos": int(open_todos), "open_handoffs": int(open_handoffs)}


def _split_tags(raw: str) -> list[str]:
    return [t for t in (part.strip() for part in raw.split(",")) if t]


def _todo_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": f"T-{row['seq']}",
        "seq": row["seq"],
        "title": row["title"],
        "detail": row["detail"],
        "status": row["status"],
        "priority": row["priority"],
        "tags": _split_tags(row["tags"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _handoff_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": f"H-{row['seq']}",
        "seq": row["seq"],
        "summary": row["summary"],
        "next_steps": row["next_steps"],
        "context": row["context"],
        "references": row["references_"],
        "status": row["status"],
        "author": row["author"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


__all__ = [
    "TODO_STATUSES",
    "HANDOFF_STATUSES",
    "DB_PATH_ENV_VAR",
    "default_db_path",
    "utc_now",
    "Store",
]
