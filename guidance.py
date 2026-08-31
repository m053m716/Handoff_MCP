"""Evidence-led guidance recommendations; storage is authoritative, not Markdown."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from mcp.lifecycle import (ToolExecutionError, clamp_limit, compact_json,
    decode_page_cursor, encode_page_cursor, escape_like, optional_string,
    require_string, utc_now)

GUIDANCE_STATUSES = {"open", "incorporated", "dismissed", "superseded"}


def recommendation_key_for(text: str) -> str:
    """Return a deterministic, human-readable key; this deliberately does no fuzzy merge."""
    normalized = " ".join(text.lower().split())
    stem = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:72] or "guidance"
    return f"{stem}-{hashlib.sha256(normalized.encode()).hexdigest()[:10]}"


class GuidanceService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def _targets(self, args: dict[str, Any]) -> list[str]:
        values = args.get("target_documents", ["AGENTS.md"])
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ToolExecutionError("target_documents must be a non-empty list of document paths.")
        return list(dict.fromkeys(v.strip() for v in values))

    def _event(self, conn: Any, key: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO guidance_recommendation_events(recommendation_key, ts, action, detail) VALUES (?, ?, ?, ?)",
                     (key, utc_now(), action, compact_json(detail)))

    def add(self, args: dict[str, Any]) -> dict[str, Any]:
        proposed = require_string(args, "proposed_guidance", max_length=4000)
        explicit = optional_string(args, "recommendation_key", max_length=160)
        key = explicit or recommendation_key_for(proposed)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,159}", key):
            raise ToolExecutionError("recommendation_key must be stable lowercase letters, digits, and hyphens.")
        evidence = require_string(args, "evidence", max_length=8000)
        source_kind = optional_string(args, "source_kind", max_length=80) or "agent"
        source_ref = optional_string(args, "source_ref", max_length=320) or hashlib.sha256(evidence.encode()).hexdigest()
        task_key = optional_string(args, "task_key", max_length=160)
        agent_id = optional_string(args, "agent_id", max_length=160)
        targets = self._targets(args)
        with self.store.connection() as conn:
            row = conn.execute("SELECT * FROM guidance_recommendations WHERE recommendation_key=?", (key,)).fetchone()
            now = utc_now()
            if not row:
                conn.execute("INSERT INTO guidance_recommendations(recommendation_key, proposed_guidance, target_documents_json, status, resolution, created_at, updated_at) VALUES (?, ?, ?, 'open', '', ?, ?)", (key, proposed, compact_json(targets), now, now))
                self._event(conn, key, "created", {"targets": targets})
            elif row["proposed_guidance"] != proposed or row["target_documents_json"] != compact_json(targets):
                conn.execute("UPDATE guidance_recommendations SET proposed_guidance=?, target_documents_json=?, updated_at=? WHERE recommendation_key=?", (proposed, compact_json(targets), now, key))
                self._event(conn, key, "canonical_edit", {"targets": targets})
            before = conn.execute("SELECT id FROM guidance_recommendation_evidence WHERE recommendation_key=? AND source_kind=? AND source_ref=?", (key, source_kind, source_ref)).fetchone()
            conn.execute("INSERT INTO guidance_recommendation_evidence(recommendation_key, source_kind, source_ref, task_key, agent_id, evidence, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(recommendation_key,source_kind,source_ref) DO UPDATE SET task_key=excluded.task_key,agent_id=excluded.agent_id,evidence=excluded.evidence,observed_at=excluded.observed_at", (key, source_kind, source_ref, task_key or None, agent_id or None, evidence, now))
            current = conn.execute("SELECT status FROM guidance_recommendations WHERE recommendation_key=?", (key,)).fetchone()
            if before is None and current["status"] in {"incorporated", "dismissed"}:
                conn.execute("UPDATE guidance_recommendations SET status='open', resolution='', resolved_at=NULL, updated_at=? WHERE recommendation_key=?", (now, key))
                self._event(conn, key, "automatic_reopen", {"prior_status": current["status"]})
            conn.commit()
        return self.list({"recommendation_key": key, "limit": 1})

    # Public application handlers retain a descriptive namespace.
    def guidance_recommendation_add(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.add(args)

    def list(self, args: dict[str, Any]) -> dict[str, Any]:
        key = optional_string(args, "recommendation_key", max_length=160)
        status = optional_string(args, "status", max_length=20)
        if status and status not in GUIDANCE_STATUSES: raise ToolExecutionError("Invalid guidance status.")
        target = optional_string(args, "target_document", max_length=300)
        search = optional_string(args, "search", max_length=300)
        limit = clamp_limit(args.get("limit"), default=20, maximum=100); offset = decode_page_cursor(args.get("cursor"), kind="guidance")
        clauses=[]; params=[]
        if key: clauses.append("g.recommendation_key=?"); params.append(key)
        if status: clauses.append("g.status=?"); params.append(status)
        if target: clauses.append("g.target_documents_json LIKE ? ESCAPE '\\'"); params.append(f'%"{escape_like(target)}"%')
        if search: clauses.append("(g.recommendation_key LIKE ? ESCAPE '\\' OR g.proposed_guidance LIKE ? ESCAPE '\\' OR g.resolution LIKE ? ESCAPE '\\')"); params += [f"%{escape_like(search)}%"]*3
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.store.connection() as conn:
            total=conn.execute("SELECT COUNT(*) FROM guidance_recommendations g"+where, params).fetchone()[0]
            rows=conn.execute("SELECT g.*, COUNT(e.id) evidence_count FROM guidance_recommendations g LEFT JOIN guidance_recommendation_evidence e ON e.recommendation_key=g.recommendation_key"+where+" GROUP BY g.id ORDER BY g.updated_at DESC,g.id DESC LIMIT ? OFFSET ?", [*params,limit+1,offset]).fetchall()
            output=[]
            for row in rows[:limit]:
                d=dict(row); d["target_documents"]=json.loads(d.pop("target_documents_json")); d["events"]=[dict(e) for e in conn.execute("SELECT * FROM guidance_recommendation_events WHERE recommendation_key=? ORDER BY ts DESC,id DESC", (d["recommendation_key"],)).fetchall()]; d["evidence"]=[dict(e) for e in conn.execute("SELECT * FROM guidance_recommendation_evidence WHERE recommendation_key=? ORDER BY observed_at DESC,id DESC", (d["recommendation_key"],)).fetchall()]; output.append(d)
        return {"recommendations":output,"total":total,"limit":limit,"has_more":len(rows)>limit,"next_cursor":encode_page_cursor(offset+limit,kind="guidance") if len(rows)>limit else None}

    def guidance_recommendation_list(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.list(args)

    def reconcile(self, args: dict[str, Any]) -> dict[str, Any]:
        key=require_string(args,"recommendation_key",max_length=160); status=optional_string(args,"status",max_length=20)
        merge_from=optional_string(args,"merge_from_key",max_length=160); resolution=optional_string(args,"resolution",max_length=4000)
        if status and status not in {"incorporated","dismissed"}: raise ToolExecutionError("Reconcile status must be incorporated or dismissed.")
        if status and not resolution: raise ToolExecutionError("resolution is required when setting incorporated or dismissed.")
        with self.store.connection() as conn:
            if not conn.execute("SELECT 1 FROM guidance_recommendations WHERE recommendation_key=?",(key,)).fetchone(): raise ToolExecutionError("Unknown recommendation_key.")
            now=utc_now()
            if merge_from:
                if merge_from==key: raise ToolExecutionError("merge_from_key must differ from recommendation_key.")
                if not conn.execute("SELECT 1 FROM guidance_recommendations WHERE recommendation_key=?",(merge_from,)).fetchone(): raise ToolExecutionError("Unknown merge_from_key.")
                conn.execute("UPDATE guidance_recommendation_evidence SET recommendation_key=? WHERE recommendation_key=?",(key,merge_from)); conn.execute("UPDATE guidance_recommendations SET status='superseded',resolution=?,resolved_at=?,updated_at=? WHERE recommendation_key=?",(f"Merged into {key}",now,now,merge_from)); self._event(merge_from,"merged",{"retained_key":key}); self._event(key,"merge",{"source_key":merge_from})
            updates=[]; values=[]
            if optional_string(args,"proposed_guidance",max_length=4000): updates.append("proposed_guidance=?"); values.append(args["proposed_guidance"].strip())
            if "target_documents" in args: updates.append("target_documents_json=?"); values.append(compact_json(self._targets(args)))
            if status: updates += ["status=?","resolution=?","resolved_at=?"]; values += [status,resolution,now]
            if updates:
                values += [now, key]
                conn.execute("UPDATE guidance_recommendations SET "+",".join(updates)+",updated_at=? WHERE recommendation_key=?", values)
                self._event(conn, key, "reconciled", {"status": status, "resolution": resolution})
            conn.commit()
        return self.list({"recommendation_key":key,"limit":1})

    def guidance_recommendation_reconcile(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.reconcile(args)
