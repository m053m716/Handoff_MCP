"""Administrative rename and closed-task purge for Token Usage agent categories.

The Token Usage dashboard panel groups rows by an *effective label* -- the same
identity ``mcp/gui/dashboard/analytics/shared.js`` computes with ``modelIdentity``:
the stored ``canonical_model_label`` normalized to its family/version/variant
(effort tier stripped), or the free-text descriptor classifier as a fallback.
``Unknown`` rows are excluded from grouping and from these tools.

This module centralizes:

* effective-label resolution and validation (with the durable alias overlay),
* the one-transaction rename that rewrites every label-bearing occurrence and
  records a durable alias so later writes cannot recreate the old category, and
* the confirmation-guarded purge that deletes only terminal task pairs for a
  category and their dependent event/usage rows.

Structured model identity fields (provider / family / version / variant / effort)
are never mutated; a rename only changes display grouping and alias mapping.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.lifecycle import ToolExecutionError, TERMINAL_TASK_STATUSES, utc_now
from mcp.model_identity import (
    MODEL_UNKNOWN_LABEL,
    canonical_effective_label,
    model_identity_label,
    parse_canonical_model_label,
    row_effective_label,
)


def new_label_effective(new_label: str) -> str:
    """Effective identity a typed replacement label groups under.

    A label matching the canonical registration grammar (``Opus 4.8``,
    ``5.6 Luna``) normalizes to its model identity so the rename can merge into a
    real model bucket. Any other free text keeps its own identity verbatim -- it
    is *not* pushed through the lossy vendor classifier, which would wrongly fuse
    a name like ``Claude Opus`` into ``Opus 4.8``.
    """
    parsed = parse_canonical_model_label(new_label)
    return model_identity_label(parsed) if parsed else new_label


# Tables/columns that carry a canonical_model_label occurrence, rewritten wholesale
# on rename. Order is irrelevant; all run inside one transaction.
CANONICAL_LABEL_TABLES = (
    "agent_model_registrations",
    "agent_task_state",
    "agent_task_token_usage",
    "mcp_orchestrator_assignments",
    "mcp_orchestration_queue",
)
# Legacy free-text columns that supply the effective category when no canonical
# label is present on the row.
LEGACY_LABEL_COLUMNS = (
    ("agent_task_state", "agent_label"),
    ("agent_task_token_usage", "model_descriptor"),
)
MAX_CATEGORY_LABEL_LENGTH = 200


def _normalize_new_label(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError("`new_label` must be a non-empty string.")
    label = value.strip()
    if len(label) > MAX_CATEGORY_LABEL_LENGTH:
        raise ToolExecutionError(
            f"`new_label` must be {MAX_CATEGORY_LABEL_LENGTH} characters or fewer."
        )
    if label.casefold() == MODEL_UNKNOWN_LABEL.casefold():
        raise ToolExecutionError(
            f"`{MODEL_UNKNOWN_LABEL}` is reserved and cannot be used as a category name."
        )
    return label


def _normalize_effective(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError(f"`{field}` must be a non-empty string.")
    label = value.strip()
    if len(label) > MAX_CATEGORY_LABEL_LENGTH:
        raise ToolExecutionError(
            f"`{field}` must be {MAX_CATEGORY_LABEL_LENGTH} characters or fewer."
        )
    if label.casefold() == MODEL_UNKNOWN_LABEL.casefold():
        raise ToolExecutionError(
            f"`{MODEL_UNKNOWN_LABEL}` rows are excluded from Token Usage and cannot be "
            "renamed or purged."
        )
    return label


class AgentCategoryService:
    """Own effective-label aliasing and the rename / closed-task purge operations."""

    def __init__(self, store: Any, *, lifecycle: Any) -> None:
        self.store = store
        self.lifecycle = lifecycle

    # ---- alias overlay ---------------------------------------------------

    def load_aliases(self, conn: Any) -> dict[str, str]:
        return {
            row["alias_label"]: row["canonical_label"]
            for row in conn.execute(
                "SELECT alias_label, canonical_label FROM agent_category_aliases"
            ).fetchall()
        }

    @staticmethod
    def apply_alias(label: str, aliases: dict[str, str]) -> str:
        """Resolve an effective label through the alias chain (cycle-safe)."""
        seen: set[str] = set()
        current = label
        while current in aliases and current not in seen:
            seen.add(current)
            current = aliases[current]
        return current

    def effective_label_for_row(self, row: Any, aliases: dict[str, str]) -> str:
        """Effective (alias-resolved) grouping label for a token-usage/task row."""
        base = row_effective_label(
            canonical_model_label=row["canonical_model_label"]
            if "canonical_model_label" in row.keys()
            else "",
            model_descriptor=row["model_descriptor"]
            if "model_descriptor" in row.keys()
            else "",
            agent_label=row["agent_label"] if "agent_label" in row.keys() else "",
            agent_id=row["agent_id"] if "agent_id" in row.keys() else "",
        )
        return self.apply_alias(base, aliases)

    # ---- rename ----------------------------------------------------------

    def rename_category(self, args: dict[str, Any]) -> dict[str, Any]:
        old_effective = _normalize_effective(args.get("old_label"), field="old_label")
        new_label = _normalize_new_label(args.get("new_label"))
        new_effective = new_label_effective(new_label)
        now = utc_now()
        affected: dict[str, int] = {}
        with self.store.connection() as conn:
            aliases = self.load_aliases(conn)
            if old_effective == new_effective:
                raise ToolExecutionError(
                    "`new_label` resolves to the same category as `old_label`; nothing to rename."
                )
            self._reject_alias_cycle(aliases, old_effective, new_effective)

            # 1. canonical_model_label occurrences whose effective identity matches.
            for table in CANONICAL_LABEL_TABLES:
                affected[f"{table}.canonical_model_label"] = self._rewrite_canonical(
                    conn, table, old_effective, aliases, new_label
                )
            # 2. legacy free-text columns that *supply* the category (no canonical
            #    override on the row).
            for table, column in LEGACY_LABEL_COLUMNS:
                affected[f"{table}.{column}"] = self._rewrite_legacy(
                    conn, table, column, old_effective, aliases, new_label
                )
            # 3. durable alias so later writes normalize old -> new.
            alias_rows = self._record_aliases(
                conn, old_effective, new_effective, new_label, now
            )
            affected["agent_category_aliases"] = alias_rows

            total = sum(affected.values())
            self._audit(
                conn,
                event_type="token_usage_category_rename",
                detail={
                    "old_label": old_effective,
                    "new_label": new_label,
                    "new_effective_label": new_effective,
                    "affected": affected,
                    "total_rows": total,
                },
                now=now,
            )
            conn.commit()
        return {
            "old_label": old_effective,
            "new_label": new_label,
            "new_effective_label": new_effective,
            "affected": affected,
            "total_affected": sum(affected.values()),
        }

    def _reject_alias_cycle(
        self, aliases: dict[str, str], old_effective: str, new_effective: str
    ) -> None:
        # Adding old -> new must not create a cycle: following new through the
        # existing alias chain must never arrive back at old.
        seen: set[str] = set()
        current = new_effective
        while current in aliases and current not in seen:
            if current == old_effective:
                raise ToolExecutionError(
                    "This rename would create an alias cycle; choose a different `new_label`."
                )
            seen.add(current)
            current = aliases[current]
        if current == old_effective:
            raise ToolExecutionError(
                "This rename would create an alias cycle; choose a different `new_label`."
            )

    def _rewrite_canonical(
        self,
        conn: Any,
        table: str,
        old_effective: str,
        aliases: dict[str, str],
        new_label: str,
    ) -> int:
        rows = conn.execute(
            f"SELECT rowid AS _rid, canonical_model_label FROM {table} "
            f"WHERE canonical_model_label <> ''"
        ).fetchall()
        matched = [
            row["_rid"]
            for row in rows
            if self.apply_alias(
                canonical_effective_label(row["canonical_model_label"]), aliases
            )
            == old_effective
            and row["canonical_model_label"] != new_label
        ]
        for rid in matched:
            conn.execute(
                f"UPDATE {table} SET canonical_model_label = ? WHERE rowid = ?",
                (new_label, rid),
            )
        return len(matched)

    def _rewrite_legacy(
        self,
        conn: Any,
        table: str,
        column: str,
        old_effective: str,
        aliases: dict[str, str],
        new_label: str,
    ) -> int:
        rows = conn.execute(
            f"SELECT rowid AS _rid, {column} AS value, canonical_model_label "
            f"FROM {table} WHERE {column} <> '' AND canonical_model_label = ''"
        ).fetchall()
        matched = [
            row["_rid"]
            for row in rows
            if self.apply_alias(canonical_effective_label(row["value"]), aliases)
            == old_effective
            and row["value"] != new_label
        ]
        for rid in matched:
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE rowid = ?", (new_label, rid)
            )
        return len(matched)

    def _record_aliases(
        self, conn: Any, old_effective: str, new_effective: str, new_label: str, now: str
    ) -> int:
        """Point old_effective (and any alias currently landing on it) at new_effective."""
        count = 0
        # Re-target existing aliases that resolved to old_effective so a chain does
        # not dangle after the rename.
        for row in conn.execute(
            "SELECT alias_label FROM agent_category_aliases WHERE canonical_label = ?",
            (old_effective,),
        ).fetchall():
            if row["alias_label"] == new_effective:
                continue
            conn.execute(
                "UPDATE agent_category_aliases SET canonical_label = ?, updated_at = ? "
                "WHERE alias_label = ?",
                (new_effective, now, row["alias_label"]),
            )
            count += 1
        if old_effective != new_effective:
            conn.execute(
                "INSERT INTO agent_category_aliases(alias_label, canonical_label, created_at, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(alias_label) DO UPDATE SET canonical_label = excluded.canonical_label, "
                "updated_at = excluded.updated_at",
                (old_effective, new_effective, now, now),
            )
            count += 1
        return count

    # ---- purge -----------------------------------------------------------

    def purge_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        effective = _normalize_effective(args.get("label"), field="label")
        with self.store.connection() as conn:
            aliases = self.load_aliases(conn)
            resolved = self.apply_alias(effective, aliases)
            ids = self._terminal_state_ids(conn, resolved, aliases)
        return {
            "label": effective,
            "effective_label": resolved,
            "closed_task_count": len(ids),
        }

    def purge_closed(self, args: dict[str, Any]) -> dict[str, Any]:
        effective = _normalize_effective(args.get("label"), field="label")
        confirm = args.get("confirm")
        if confirm is not True:
            raise ToolExecutionError(
                "`confirm` must be true to delete closed tasks for this category."
            )
        now = utc_now()
        deleted = {"agent_task_events": 0, "agent_task_token_usage": 0, "agent_task_state": 0}
        with self.store.connection() as conn:
            aliases = self.load_aliases(conn)
            resolved = self.apply_alias(effective, aliases)
            # Re-evaluate matching terminal tasks inside the transaction.
            state_ids = self._terminal_state_ids(conn, resolved, aliases)
            if state_ids:
                pairs = conn.execute(
                    "SELECT id, task_key, agent_id FROM agent_task_state "
                    f"WHERE id IN ({','.join('?' for _ in state_ids)})",
                    tuple(state_ids),
                ).fetchall()
                key_pairs = [(row["task_key"], row["agent_id"]) for row in pairs]
                # Dependent rows first: events by task_state_id, usage by (task_key, agent_id).
                placeholders = ",".join("?" for _ in state_ids)
                deleted["agent_task_events"] = conn.execute(
                    f"DELETE FROM agent_task_events WHERE task_state_id IN ({placeholders})",
                    tuple(state_ids),
                ).rowcount
                for task_key, agent_id in key_pairs:
                    deleted["agent_task_token_usage"] += conn.execute(
                        "DELETE FROM agent_task_token_usage WHERE task_key = ? AND agent_id = ?",
                        (task_key, agent_id),
                    ).rowcount
                deleted["agent_task_state"] = conn.execute(
                    f"DELETE FROM agent_task_state WHERE id IN ({placeholders})",
                    tuple(state_ids),
                ).rowcount
            self._audit(
                conn,
                event_type="token_usage_category_purge",
                detail={
                    "label": effective,
                    "effective_label": resolved,
                    "deleted": deleted,
                },
                now=now,
            )
            conn.commit()
        return {
            "label": effective,
            "effective_label": resolved,
            "deleted": deleted,
            "total_deleted": sum(deleted.values()),
        }

    def _terminal_state_ids(
        self, conn: Any, resolved_label: str, aliases: dict[str, str]
    ) -> list[int]:
        placeholders = ",".join("?" for _ in TERMINAL_TASK_STATUSES)
        rows = conn.execute(
            "SELECT id, canonical_model_label, agent_label, agent_id "
            "FROM agent_task_state "
            f"WHERE status IN ({placeholders})",
            tuple(sorted(TERMINAL_TASK_STATUSES)),
        ).fetchall()
        return [
            row["id"]
            for row in rows
            if self.effective_label_for_row(row, aliases) == resolved_label
        ]

    # ---- audit -----------------------------------------------------------

    def _audit(self, conn: Any, *, event_type: str, detail: dict[str, Any], now: str) -> None:
        conn.execute(
            "INSERT INTO events(ts, actor, event_type, detail) VALUES (?, ?, ?, ?)",
            (now, "dashboard", event_type, json.dumps(detail, ensure_ascii=False, separators=(",", ":"))),
        )
