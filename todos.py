"""SQLite-backed MCP TODO and assignment domain service.

The service owns TODO CRUD, filtering, grouping, relationship scoring, assignment
cascades, and instruction rendering. It is transport-agnostic and depends on
an injected store plus lifecycle callbacks for shared persistence behavior.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from mcp.project import load_project_descriptor
from mcp.lifecycle import ToolExecutionError

PROJECT = load_project_descriptor()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# `queued` is an open status set only by the orchestration enqueue path (never by
# todo_add): it marks a todo that has been serialized into the orchestration queue
# but not yet pruned/dispatched. It reads as open/filterable so the dashboard can
# grey queued rows and the wake-ping action can gate on it. Agents author todos with
# TODO_ADDABLE_STATUSES only; enqueue/drop/cancel move a todo in and out of `queued`.
TODO_ADDABLE_STATUSES = {"suggested", "accepted", "in_progress", "blocked"}

TODO_ACTIVE_STATUSES = TODO_ADDABLE_STATUSES | {"queued"}

TODO_TERMINAL_STATUSES = {"done", "dropped"}

TODO_STATUSES = TODO_ACTIVE_STATUSES | TODO_TERMINAL_STATUSES

# Parenthesized SQL list of the open statuses, kept in sync with TODO_ACTIVE_STATUSES
# so the `status = 'open'` filter and open-list loaders never drift from the enum.
TODO_OPEN_STATUS_SQL = "(" + ", ".join(
    "'" + status + "'" for status in sorted(TODO_ACTIVE_STATUSES)
) + ")"

TODO_PRIORITIES = {"P0", "P1", "P2", "P3", "P4"}

COMPLEXITY_NUMERIC_SCALE = {"S": 1, "M": 2, "L": 3, "XL": 4}

TODO_JSON_LIST_COLUMNS = {
    "tags": "tags_json",
    "code_paths": "code_paths_json",
    "symbol_refs": "symbol_refs_json",
    "doc_keys": "doc_keys_json",
    "route_refs": "route_refs_json",
    "test_refs": "test_refs_json",
    "search_queries": "search_queries_json",
}

TODO_REFERENCE_LABELS = {
    "code_paths": "Code paths",
    "symbol_refs": "Symbols/tools",
    "doc_keys": "Docs",
    "route_refs": "Routes/APIs",
    "test_refs": "Validation",
    "search_queries": "Fallback searches",
}

TODO_FEATURE_GUIDANCE: dict[str, dict[str, Any]] = {
    "mcp-model-identity-canonical-labels": {
        "heading": "Feature guidance - structured model registration and canonical identity.",
        "sections": [
            (
                "Registration lifecycle",
                [
                    "Call `mudra_agent_model_register` before `mudra_task_check_in`; pass the returned `model_registration_key` to check-in and `mudra_task_token_usage`.",
                    "The key is stable for `(agent_id, session_hint)` and updates the current registration without changing `(task_key, agent_id)` uniqueness. New lifecycle rows snapshot both the key and `canonical_model_label`; historical text rows are not rewritten.",
                    "Registered identity wins over legacy `agent_label` and `model_descriptor`. Reject stale keys, cross-agent keys, contradictory model families/providers, and unknown future families instead of guessing.",
                ],
            ),
            (
                "Canonical label examples",
                [
                    "Structured `{provider: openai, model_family: gpt, model_version: 5.6, model_variant: Sol, reasoning_effort: high}` produces `5.6 Sol High`.",
                    "The corresponding Luna `extra_high` registration produces `5.6 Luna Extra High`; Anthropic Opus 4.8 high produces `Opus 4.8 High`; Anthropic Fable 5 medium produces `Fable 5 Medium`.",
                    "Use `mudra_doc_search` in `mcp-server` before `mudra_doc_get`; lifecycle and dashboard contracts are in `docs/mcp-server/agent-lifecycle.md` and `docs/mcp-server/dashboard.md`.",
                ],
            ),
        ],
    },
    "mcp-dashboard-token-usage-oscilloscope": {
        "heading": (
            "Feature guidance - Token Usage Over Time oscilloscope (line plot of "
            "per-model token usage over time, integrated with the Agent Model Usage donut)."
        ),
        "sections": [
            (
                "Time-series data retrieval & API",
                [
                    "The dashboard is fed by `dashboard_snapshot()` (server) exposed over `GET /api/dashboard`; analytics rows come from the complete `agent_task_state` and `agent_task_token_usage` histories, while operational widgets retain their own display limits.",
                    "Each row already carries a `ts` (report time) plus input/output/total tokens and a model descriptor - enough to plot a per-model time series without a new table. Prefer widening/deriving from the existing snapshot query over adding a route; only add a dedicated endpoint if the plot needs a longer/independent window than the panel.",
                    "Classify each row to a model with the SAME helper the donut uses (`classifyModelDescriptor`) so lines and donut segments map to identical model buckets and colors.",
                    "Analytics use all task-state and token-usage history, ordered newest-first; drive the oscilloscope from the token-usage rows and reconcile buckets against the donut rather than assuming operational-widget windows align.",
                ],
            ),
            (
                "Frontend state management",
                [
                    "Add oscilloscope state to the dashboard `state` object (as `renderModelUsage`/`computeModelUsageCounts` do): selected timescale (1h/6h/24h/7d/30d/All), isolated/highlighted model, and horizontal pan/scroll position. Keep it in the single `state` object and re-render through the existing `loadDashboard`/render pipeline.",
                    "Timescale buttons filter the plotted window; wider ranges (7d/30d/All) should widen the plotted area so it pans horizontally while the left model-label column stays fixed.",
                    "Persist only what should survive a refresh consistently with sibling panels; transient hover state stays ephemeral.",
                ],
            ),
            (
                "Rendering approach & performance",
                [
                    "Match the existing donut/token panels: hand-built inline SVG in `dashboard.js` (see `buildModelUsageSegments`/`renderModelUsage`) rather than pulling in a charting library. Provide a `buildOscilloscopeSeries` builder that maps rows -> per-model polyline points, and `renderOscilloscope`/`renderOscilloscopeControls` for the DOM.",
                    "Pre-aggregate/smooth points per model before drawing; cap the number of rendered points for wide ranges so the SVG stays light on the periodic `loadDashboard` refresh.",
                    "Reuse the donut color assignment so a model's line, donut segment, and legend swatch are the same color.",
                ],
            ),
            (
                "Interactive hover/click integration with donut & legend",
                [
                    "Wire hover to a shared highlight path (`applyModelHighlight`): hovering a donut segment, legend entry, or a plot lane should bold the matching line/segment and dim the others across all three components.",
                    "Click isolates a single model (taller/emphasized lane, filtered donut/legend) with a clear-isolation affordance (chip clear button or re-click) that restores all models. Keep donut, legend, and oscilloscope selection in lockstep via the shared `state`.",
                ],
            ),
            (
                "Logarithmic y-axis",
                [
                    "Token counts span orders of magnitude, so support a log y-scale: guard `log(0)`/negatives (clamp to a small floor or plot a gap), and label gridlines at powers of ten. Keep the mapping in the series builder so both drawing and hover tooltips share one scale function.",
                ],
            ),
            (
                "Data freshness & synchronization",
                [
                    "Render from the same `GET /api/dashboard` payload and the same periodic refresh as the donut/token panels - do not add a second polling loop. On each `loadDashboard`, rebuild the series while preserving user-selected timescale/isolation/pan in `state`.",
                    "Keep counts consistent with the Token Usage panel (`computeTokenUsageStats`, `fmtTokens`) so totals shown on the plot match the numeric panel.",
                ],
            ),
            (
                "Testing strategy",
                [
                    "Follow the sibling panels' headless approach: `node --check mcp/gui/dashboard.js`, plus stub-DOM render tests and direct-method smoke tests for `buildOscilloscopeSeries`/scale/bucketing (see the existing dashboard tests, e.g. `mudra/tests/test_mcp_server_dashboard.py`). Cover empty data, a single model, many models, log-scale zero/negative guards, and timescale filtering.",
                    "Real-browser interaction (hover/isolate/pan) is validated separately - see the follow-up todo `mcp-dashboard-oscilloscope-manual-verify`; the server is launched at the owner's discretion (AGENTS.md).",
                ],
            ),
            (
                "Responsive design constraints",
                [
                    "Fit the existing dashboard grid/panel row alongside the donut and Token Usage panels; keep the left label column fixed and let the plot area scroll horizontally on narrow widths. Verify legibility down to the narrow single-column breakpoint used elsewhere (~<=620px) via `mcp/gui/styles.css`.",
                ],
            ),
        ],
        "code_paths": [
            "mcp/gui/dashboard.js",
            "mcp/gui/index.html",
            "mcp/gui/styles.css",
            "mcp/server.py",
        ],
        "docs": ["mcp-server:docs/mcp-server/dashboard.md"],
    },
}

TODO_GROUP_TYPES = {"manual", "auto"}

TODO_GROUP_RELATION_KINDS = {"manual", "auto", "explicit", "inferred", "seed"}

TODO_GROUP_DEFAULT_MIN_SCORE = 10

TODO_GROUP_DEFAULT_LIMIT = 8

TODO_GROUP_MAX_KEYS = 40

TODO_GROUP_GENERIC_TAGS = {
    "agent",
    "android",
    "api",
    "dashboard",
    "docs",
    "gateway",
    "ios",
    "local",
    "mcp",
    "scripts",
    "server",
    "todo",
    "todos",
}

TODO_GROUP_TEXT_STOPWORDS = {
    "about",
    "accept",
    "accepted",
    "add",
    "agent",
    "agents",
    "also",
    "and",
    "api",
    "app",
    "are",
    "before",
    "behavior",
    "can",
    "check",
    "code",
    "context",
    "current",
    "detail",
    "details",
    "doc",
    "docs",
    "for",
    "from",
    "has",
    "have",
    "http",
    "implement",
    "include",
    "local",
    "mcp",
    "mudra",
    "next",
    "one",
    "open",
    "or",
    "repo",
    "scope",
    "server",
    "should",
    "source",
    "state",
    "support",
    "task",
    "that",
    "the",
    "this",
    "todo",
    "todos",
    "tool",
    "tools",
    "update",
    "use",
    "when",
    "with",
    "work",
}

DOC_SCOPES: dict[str, dict[str, Any]] = {
    key: dict(value) for key, value in PROJECT.doc_scopes.items()
}

TODO_LIST_DEFAULT_LIMIT = 20

TODO_LIST_MAX_LIMIT = 100

def clamp_limit(value: Any, default: int = 10, maximum: int = 100) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(limit, maximum))

def encode_page_cursor(offset: int, *, kind: str) -> str:
    """Encode a continuation offset as an opaque, transport-safe cursor."""
    payload = json.dumps({"v": 1, "kind": kind, "offset": max(0, int(offset))}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

def decode_page_cursor(value: Any, *, kind: str) -> int:
    if value in (None, ""):
        return 0
    if not isinstance(value, str) or len(value) > 256:
        raise ToolExecutionError("`cursor` must be an opaque continuation token.")
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if payload.get("v") != 1 or payload.get("kind") != kind:
            raise ValueError
        offset = payload.get("offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except (AttributeError, ValueError, TypeError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        raise ToolExecutionError("`cursor` is invalid or belongs to another list.") from None

def require_string(args: dict[str, Any], key: str, *, max_length: int | None = None) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError(f"`{key}` must be a non-empty string.")
    value = value.strip()
    if max_length is not None and len(value) > max_length:
        raise ToolExecutionError(f"`{key}` must be {max_length} characters or fewer.")
    return value

def optional_string(
    args: dict[str, Any],
    key: str,
    *,
    default: str = "",
    max_length: int | None = None,
) -> str:
    value = args.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ToolExecutionError(f"`{key}` must be a string when provided.")
    value = value.strip()
    if max_length is not None and len(value) > max_length:
        raise ToolExecutionError(f"`{key}` must be {max_length} characters or fewer.")
    return value

def bool_arg(args: dict[str, Any], key: str, *, default: bool = False) -> bool:
    value = args.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    raise ToolExecutionError(f"`{key}` must be true or false when provided.")

def json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)

def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def safe_todo_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:/-]+", "-", value.strip()).strip("-").lower()

def safe_group_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:/-]+", "-", value.strip()).strip("-").lower()

def normalize_todo_status(value: Any, *, default: str) -> str:
    if value is None:
        status = default
    elif isinstance(value, str):
        status = value.strip()
    else:
        raise ToolExecutionError("`status` must be a string when provided.")
    if status not in TODO_STATUSES:
        raise ToolExecutionError(
            "`status` must be one of: " + ", ".join(sorted(TODO_STATUSES)) + "."
        )
    return status

def normalize_todo_priority(value: Any, *, default: str = "P2") -> str:
    if value is None:
        priority = default
    elif isinstance(value, str):
        priority = value.strip().upper()
    else:
        raise ToolExecutionError("`priority` must be a string when provided.")
    if priority not in TODO_PRIORITIES:
        raise ToolExecutionError(
            "`priority` must be one of: " + ", ".join(sorted(TODO_PRIORITIES)) + "."
        )
    return priority

def parse_string_list(
    value: Any,
    field_name: str,
    *,
    max_item_length: int = 120,
    max_items: int = 100,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ToolExecutionError(f"`{field_name}` must be an array of strings when provided.")
    return [item.strip()[:max_item_length] for item in value[:max_items] if item.strip()]

def normalize_relation_kind(value: Any, *, default: str = "manual") -> str:
    if value is None:
        kind = default
    elif isinstance(value, str):
        kind = value.strip()
    else:
        raise ToolExecutionError("`relation_kind` must be a string when provided.")
    if kind not in TODO_GROUP_RELATION_KINDS:
        raise ToolExecutionError(
            "`relation_kind` must be one of: "
            + ", ".join(sorted(TODO_GROUP_RELATION_KINDS))
            + "."
        )
    return kind

def normalize_group_type(value: Any, *, default: str = "manual") -> str:
    if value is None:
        group_type = default
    elif isinstance(value, str):
        group_type = value.strip()
    else:
        raise ToolExecutionError("`group_type` must be a string when provided.")
    if group_type not in TODO_GROUP_TYPES:
        raise ToolExecutionError(
            "`group_type` must be one of: " + ", ".join(sorted(TODO_GROUP_TYPES)) + "."
        )
    return group_type

def normalize_reference_value(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())

def todo_text_tokens(todo: dict[str, Any]) -> set[str]:
    text = f"{todo.get('title', '')} {todo.get('detail', '')}"
    return {
        term
        for term in tokenize_query(text)
        if len(term) >= 3 and term not in TODO_GROUP_TEXT_STOPWORDS
    }

def shared_normalized_values(a_values: list[str], b_values: list[str]) -> list[str]:
    a_map = {normalize_reference_value(value): value for value in a_values if value.strip()}
    b_keys = {normalize_reference_value(value) for value in b_values if value.strip()}
    return sorted(a_map[key] for key in a_map.keys() & b_keys)

def related_path_pairs(a_values: list[str], b_values: list[str]) -> list[str]:
    pairs: list[str] = []
    a_paths = [value.strip().replace("\\", "/") for value in a_values if value.strip()]
    b_paths = [value.strip().replace("\\", "/") for value in b_values if value.strip()]
    for a_path in a_paths:
        a_norm = a_path.strip("/")
        for b_path in b_paths:
            b_norm = b_path.strip("/")
            if not a_norm or not b_norm:
                continue
            if a_norm.casefold() == b_norm.casefold():
                label = a_path
                if label not in pairs:
                    pairs.append(label)
    return pairs

def default_todo_group_key(todo_keys: list[str]) -> str:
    key_part = "-".join(todo_keys[:2])[:100].strip("-") or "todos"
    digest = hash_text("|".join(sorted(todo_keys)))[:10]
    return safe_group_key(f"related-{key_part}-{digest}")[:160]

def score_related_todos(primary: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    score = 0
    reasons: list[str] = []

    weighted_fields = [
        ("route_refs", "routes", 4, 10),
        ("symbol_refs", "symbols", 2, 6),
    ]
    shared_tags = [
        value
        for value in shared_normalized_values(
            list(primary.get("tags") or []),
            list(candidate.get("tags") or []),
        )
        if normalize_reference_value(value) not in TODO_GROUP_GENERIC_TAGS
    ]
    if shared_tags:
        score += min(6, len(shared_tags) * 2)
        reasons.append(f"shared tags: {', '.join(shared_tags[:3])}")

    shared_docs = [
        value
        for value in shared_normalized_values(
            list(primary.get("doc_keys") or []),
            list(candidate.get("doc_keys") or []),
        )
        if not value.casefold().endswith(":source-manifest")
        and not value.casefold().endswith(":readme.md")
        and not value.casefold().endswith("/readme.md")
    ]
    if shared_docs:
        score += min(10, len(shared_docs) * 4)
        reasons.append(f"shared docs: {', '.join(shared_docs[:3])}")

    for field_name, label, weight, cap in weighted_fields:
        shared = shared_normalized_values(
            list(primary.get(field_name) or []),
            list(candidate.get(field_name) or []),
        )
        if not shared:
            continue
        score += min(cap, len(shared) * weight)
        reasons.append(f"shared {label}: {', '.join(shared[:3])}")

    path_pairs = related_path_pairs(
        list(primary.get("code_paths") or []),
        list(candidate.get("code_paths") or []),
    )
    if path_pairs:
        score += min(9, len(path_pairs) * 3)
        reasons.append(f"overlapping code paths: {', '.join(path_pairs[:3])}")

    primary_source = normalize_reference_value(primary.get("source_task_key") or "")
    candidate_source = normalize_reference_value(candidate.get("source_task_key") or "")
    if primary_source and primary_source == candidate_source:
        score += 5
        reasons.append(f"shared source_task_key: {primary.get('source_task_key')}")

    shared_terms = sorted(todo_text_tokens(primary) & todo_text_tokens(candidate))
    if len(shared_terms) >= 3:
        score += min(5, len(shared_terms))
        reasons.append(f"title/detail overlap: {', '.join(shared_terms[:5])}")

    return {
        "score": score,
        "reasons": reasons,
        "reason": "; ".join(reasons[:4]) if reasons else "",
    }

def todo_reference_advisories(todo: dict[str, Any]) -> list[str]:
    app_scope = todo.get("app_scope") or ""
    has_scope = app_scope in DOC_SCOPES
    has_behavior_refs = any(
        todo.get(field_name)
        for field_name in ("code_paths", "symbol_refs", "route_refs", "test_refs")
    )
    if not has_scope and not has_behavior_refs:
        return []

    advisories: list[str] = []
    if not todo.get("doc_keys"):
        if has_scope:
            advisories.append(
                f"No `doc_keys` yet; search `{app_scope}` docs with `mudra_doc_search` "
                "before implementation and add relevant doc refs to the todo or handoff."
            )
        else:
            advisories.append(
                "No `doc_keys` yet; choose the nearest docs scope, search it with "
                "`mudra_doc_search`, and add relevant doc refs to the todo or handoff."
            )
    if has_scope and not todo.get("code_paths"):
        advisories.append(
            "No `code_paths` yet; add likely files, directories, or globs so the "
            "next agent can start from concrete source references."
        )
    return advisories

def row_to_todo(row: sqlite3.Row) -> dict[str, Any]:
    todo = dict(row)
    for field_name, column_name in TODO_JSON_LIST_COLUMNS.items():
        todo[field_name] = json.loads(todo.pop(column_name, "[]") or "[]")
    todo["is_open"] = todo["status"] in TODO_ACTIVE_STATUSES
    todo["reference_advisories"] = todo_reference_advisories(todo)
    return todo

def tokenize_query(query: str) -> list[str]:
    return [term.lower() for term in re.findall(r"[A-Za-z0-9_./:-]{2,}", query)]

class TodoService:
    """MCP TODO domain service with explicit storage/lifecycle seams."""

    def __init__(
        self,
        store: Any,
        lifecycle: Any | None = None,
        orchestration: Any | None = None,
        *,
        project: Any | None = None,
        row_to_task: Callable[[sqlite3.Row], dict[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.lifecycle = lifecycle
        self.orchestration = orchestration
        self.project = project or PROJECT
        self._row_to_task = row_to_task or (lambda row: dict(row))

    @property
    def connection(self) -> Callable[[], Any]:
        return self.store.connection

    def validate_app_scope(self, value: str | None) -> str | None:
        if not value:
            return None
        if value not in DOC_SCOPES:
            raise ToolExecutionError(
                "`app_scope` must be one of: " + ", ".join(sorted(DOC_SCOPES)) + "."
            )
        return value

    def row_to_task(self, row: sqlite3.Row) -> dict[str, Any]:
        return self._row_to_task(row)

    def insert_task_event(self, conn: sqlite3.Connection, **kwargs: Any) -> None:
        if self.lifecycle is None or not hasattr(self.lifecycle, "insert_task_event"):
            raise RuntimeError("TodoService requires a lifecycle service for task events.")
        self.lifecycle.insert_task_event(conn, **kwargs)

    def active_task_keys(self) -> set[str]:
        if self.lifecycle is not None and hasattr(self.lifecycle, "active_task_keys"):
            return set(self.lifecycle.active_task_keys())
        return set()

    def insert_todo_event(
        self,
        conn: sqlite3.Connection,
        *,
        todo_id: int | None,
        todo_key: str,
        action: str,
        actor: str,
        detail: str = "",
    ) -> None:
        conn.execute(
            """
            INSERT INTO mcp_todo_events(todo_id, todo_key, ts, action, actor, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (todo_id, todo_key, utc_now(), action, actor, detail),
        )

    def todo_keys_arg(
        self,
        args: dict[str, Any],
        key: str = "todo_keys",
        *,
        min_items: int = 0,
    ) -> list[str]:
        values = parse_string_list(
            args.get(key, []),
            key,
            max_item_length=160,
            max_items=TODO_GROUP_MAX_KEYS,
        )
        todo_keys: list[str] = []
        seen: set[str] = set()
        for value in values:
            todo_key = safe_todo_key(value)
            if not todo_key or todo_key in seen:
                continue
            todo_keys.append(todo_key)
            seen.add(todo_key)
        if len(todo_keys) < min_items:
            raise ToolExecutionError(f"`{key}` must include at least {min_items} todo keys.")
        return todo_keys

    def fetch_todo_rows_by_keys(
        self,
        conn: sqlite3.Connection,
        todo_keys: list[str],
    ) -> dict[str, sqlite3.Row]:
        if not todo_keys:
            return {}
        placeholders = ", ".join("?" for _ in todo_keys)
        rows = conn.execute(
            f"SELECT * FROM mcp_todos WHERE todo_key IN ({placeholders})",
            todo_keys,
        ).fetchall()
        return {row["todo_key"]: row for row in rows}

    def load_open_todos(
        self,
        conn: sqlite3.Connection,
        *,
        app_scope: str = "",
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        where = [f"status IN {TODO_OPEN_STATUS_SQL}"]
        params: list[Any] = []
        if app_scope:
            where.append("app_scope = ?")
            params.append(app_scope)
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT * FROM mcp_todos
            WHERE {' AND '.join(where)}
            ORDER BY
                CASE priority
                    WHEN 'P0' THEN 0
                    WHEN 'P1' THEN 1
                    WHEN 'P2' THEN 2
                    WHEN 'P3' THEN 3
                    ELSE 4
                END,
                updated_at DESC,
                id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [row_to_todo(row) for row in rows]

    def attach_primary_group_metadata(
        self,
        conn: sqlite3.Connection,
        todos: list[dict[str, Any]],
    ) -> None:
        """Attach the lexicographically primary persisted group to each todo."""
        todo_keys = [todo.get("todo_key") for todo in todos if todo.get("todo_key")]
        if not todo_keys:
            return
        placeholders = ", ".join("?" for _ in todo_keys)
        rows = conn.execute(
            f"""
            SELECT
                m.todo_key,
                g.group_key,
                g.title AS group_title,
                m.position AS group_position,
                (
                    SELECT COUNT(*)
                    FROM mcp_todo_group_members all_members
                    WHERE all_members.group_id = m.group_id
                ) AS group_size
            FROM mcp_todo_group_members m
            JOIN mcp_todo_groups g ON g.id = m.group_id
            WHERE m.todo_key IN ({placeholders})
            ORDER BY m.todo_key ASC, g.group_key ASC
            """,
            todo_keys,
        ).fetchall()
        primary_by_todo: dict[str, sqlite3.Row] = {}
        for row in rows:
            primary_by_todo.setdefault(row["todo_key"], row)
        for todo in todos:
            group = primary_by_todo.get(todo.get("todo_key"))
            if not group:
                continue
            todo["group_key"] = group["group_key"]
            todo["group_title"] = group["group_title"]
            todo["group_position"] = group["group_position"]
            todo["group_size"] = group["group_size"]
            todo["primary_group_key"] = group["group_key"]
            todo["primary_group_title"] = group["group_title"]

    def upsert_todo_group(
        self,
        conn: sqlite3.Connection,
        *,
        group_key: str,
        title: str,
        detail: str,
        group_type: str,
        source: str,
        criteria: dict[str, Any],
        members: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if len(members) < 2:
            raise ToolExecutionError("A todo group must include at least two members.")
        member_keys = [str(member.get("todo_key") or "") for member in members]
        if len(member_keys) != len(set(member_keys)):
            raise ToolExecutionError("A todo group cannot contain duplicate members.")
        now = utc_now()
        normalized_group_type = normalize_group_type(group_type, default="manual")
        conn.execute(
            """
            INSERT INTO mcp_todo_groups(
                group_key, title, detail, group_type, source, criteria_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_key) DO UPDATE SET
                title = excluded.title,
                detail = excluded.detail,
                group_type = excluded.group_type,
                source = excluded.source,
                criteria_json = excluded.criteria_json,
                updated_at = excluded.updated_at
            """,
            (
                group_key,
                title,
                detail,
                normalized_group_type,
                source,
                json_text(criteria),
                now,
                now,
            ),
        )
        group_row = conn.execute(
            "SELECT * FROM mcp_todo_groups WHERE group_key = ?",
            (group_key,),
        ).fetchone()
        todo_rows = self.fetch_todo_rows_by_keys(
            conn,
            [member["todo_key"] for member in members],
        )
        placeholders = ", ".join("?" for _ in member_keys)
        conn.execute(
            f"""
            DELETE FROM mcp_todo_group_members
            WHERE group_id = ?
              AND todo_key NOT IN ({placeholders})
            """,
            [group_row["id"], *member_keys],
        )
        for position, member in enumerate(members, start=1):
            todo_key = member["todo_key"]
            todo_row = todo_rows.get(todo_key)
            if not todo_row:
                raise ToolExecutionError(f"No todo found for `{todo_key}`.")
            relation_kind = normalize_relation_kind(
                member.get("relation_kind"),
                default="manual" if normalized_group_type == "manual" else "auto",
            )
            confidence = float(member.get("confidence", 1.0))
            confidence = max(0.0, min(confidence, 1.0))
            reason = str(member.get("reason") or "")[:2000]
            conn.execute(
                """
                INSERT INTO mcp_todo_group_members(
                    group_id, todo_id, todo_key, position, relation_kind, confidence,
                    reason, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, todo_key) DO UPDATE SET
                    todo_id = excluded.todo_id,
                    position = excluded.position,
                    relation_kind = excluded.relation_kind,
                    confidence = excluded.confidence,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (
                    group_row["id"],
                    todo_row["id"],
                    todo_key,
                    position,
                    relation_kind,
                    confidence,
                    reason,
                    now,
                    now,
                ),
            )
            self.insert_todo_event(
                conn,
                todo_id=todo_row["id"],
                todo_key=todo_key,
                action="group",
                actor=source,
                detail=f"{group_key}: {reason or title}",
            )
        return self.todo_group_get_by_key(conn, group_key)

    def todo_group_get_by_key(
        self,
        conn: sqlite3.Connection,
        group_key: str,
    ) -> dict[str, Any]:
        group_key = safe_group_key(group_key)
        group_row = conn.execute(
            "SELECT * FROM mcp_todo_groups WHERE group_key = ?",
            (group_key,),
        ).fetchone()
        if not group_row:
            return {"found": False, "group_key": group_key}
        member_rows = conn.execute(
            """
            SELECT
                m.relation_kind,
                m.confidence,
                m.reason,
                m.position AS group_position,
                COUNT(*) OVER (PARTITION BY m.group_id) AS group_size,
                m.created_at AS member_created_at,
                m.updated_at AS member_updated_at,
                t.*
            FROM mcp_todo_group_members m
            JOIN mcp_todos t ON t.todo_key = m.todo_key
            WHERE m.group_id = ?
            ORDER BY m.position ASC, m.id ASC
            """,
            (group_row["id"],),
        ).fetchall()
        members: list[dict[str, Any]] = []
        for member_row in member_rows:
            todo = row_to_todo(member_row)
            todo["relation_kind"] = member_row["relation_kind"]
            todo["confidence"] = member_row["confidence"]
            todo["relation_reason"] = member_row["reason"]
            todo["group_key"] = group_row["group_key"]
            todo["group_title"] = group_row["title"]
            todo["group_position"] = member_row["group_position"]
            todo["group_size"] = member_row["group_size"]
            todo["relation_source"] = group_row["group_type"]
            members.append(todo)
        group = dict(group_row)
        group["criteria"] = json.loads(group.pop("criteria_json") or "{}")
        group["members"] = members
        group["group_size"] = len(members)
        return {"found": True, "group": group}

    def todo_group_get(self, group_key: str) -> dict[str, Any]:
        with self.connection() as conn:
            return self.todo_group_get_by_key(conn, group_key)

    def todo_group_reorder(self, args: dict[str, Any]) -> dict[str, Any]:
        group_key = safe_group_key(require_string(args, "group_key", max_length=160))
        raw_keys = parse_string_list(
            args.get("todo_keys", []),
            "todo_keys",
            max_item_length=160,
            max_items=TODO_GROUP_MAX_KEYS,
        )
        ordered_keys = [safe_todo_key(value) for value in raw_keys]
        if any(not key for key in ordered_keys):
            raise ToolExecutionError("`todo_keys` must contain non-empty todo keys.")
        if len(ordered_keys) < 2:
            raise ToolExecutionError("`todo_keys` must include at least two todo keys.")
        if len(ordered_keys) != len(set(ordered_keys)):
            raise ToolExecutionError("`todo_keys` cannot contain duplicate members.")
        actor = optional_string(args, "actor", default="dashboard", max_length=120) or "dashboard"
        now = utc_now()
        with self.connection() as conn:
            group_row = conn.execute(
                "SELECT id FROM mcp_todo_groups WHERE group_key = ?",
                (group_key,),
            ).fetchone()
            if not group_row:
                raise ToolExecutionError(f"No todo group found for `{group_key}`.")
            actual_keys = [
                row["todo_key"]
                for row in conn.execute(
                    """
                    SELECT todo_key
                    FROM mcp_todo_group_members
                    WHERE group_id = ?
                    ORDER BY position ASC, id ASC
                    """,
                    (group_row["id"],),
                ).fetchall()
            ]
            if len(ordered_keys) != len(actual_keys) or set(ordered_keys) != set(actual_keys):
                raise ToolExecutionError(
                    "`todo_keys` must exactly match the group's members with no additions or omissions."
                )
            for position, todo_key in enumerate(ordered_keys, start=1):
                conn.execute(
                    """
                    UPDATE mcp_todo_group_members
                    SET position = ?, updated_at = ?
                    WHERE group_id = ? AND todo_key = ?
                    """,
                    (position, now, group_row["id"], todo_key),
                )
            conn.execute(
                "UPDATE mcp_todo_groups SET updated_at = ? WHERE id = ?",
                (now, group_row["id"]),
            )
            conn.commit()
            result = self.todo_group_get_by_key(conn, group_key)
        result["actor"] = actor
        return result

    def todo_group_related(self, args: dict[str, Any]) -> dict[str, Any]:
        todo_keys = self.todo_keys_arg(args, min_items=2)
        group_key = safe_group_key(
            optional_string(args, "group_key", default="", max_length=160)
            or default_todo_group_key(todo_keys)
        )
        title = optional_string(args, "title", default="", max_length=240) or (
            "Related TODOs: " + ", ".join(todo_keys[:3])
        )
        detail = optional_string(args, "detail", default="", max_length=20_000)
        actor = optional_string(args, "actor", default="agent", max_length=120) or "agent"
        relation_kind = normalize_relation_kind(args.get("relation_kind"), default="manual")
        members = [
            {
                "todo_key": todo_key,
                "relation_kind": relation_kind,
                "confidence": 1.0,
                "reason": detail or "Manual related TODO association.",
            }
            for todo_key in todo_keys
        ]
        with self.connection() as conn:
            rows = self.fetch_todo_rows_by_keys(conn, todo_keys)
            missing = [todo_key for todo_key in todo_keys if todo_key not in rows]
            if missing:
                raise ToolExecutionError("No todo found for: " + ", ".join(missing) + ".")
            group = self.upsert_todo_group(
                conn,
                group_key=group_key,
                title=title,
                detail=detail,
                group_type="manual",
                source=actor,
                criteria={"manual_todo_keys": todo_keys},
                members=members,
            )
            conn.commit()
        guidance = self.todo_related_for_assignment(
            {"todo_key": todo_keys[0], "include_inferred": False, "limit": TODO_GROUP_DEFAULT_LIMIT}
        )
        return {"group": group["group"], "related_todo_guidance": guidance}

    def candidate_groups_from_scores(
        self,
        todos: list[dict[str, Any]],
        *,
        seed_keys: list[str],
        min_score: int,
        limit: int,
        include_seed_inferred: bool = False,
    ) -> list[dict[str, Any]]:
        todo_by_key = {todo["todo_key"]: todo for todo in todos}
        groups: list[dict[str, Any]] = []
        if seed_keys:
            missing = [todo_key for todo_key in seed_keys if todo_key not in todo_by_key]
            if missing:
                raise ToolExecutionError("No open todo found for: " + ", ".join(missing) + ".")
            primary = todo_by_key[seed_keys[0]]
            members: list[dict[str, Any]] = [
                {
                    "todo": todo_by_key[todo_key],
                    "score": 999,
                    "reason": "explicit seed todo",
                    "relation_kind": "seed" if index == 0 else "explicit",
                }
                for index, todo_key in enumerate(seed_keys)
            ]
            existing_keys = set(seed_keys)
            if include_seed_inferred or len(seed_keys) == 1:
                for candidate in todos:
                    if candidate["todo_key"] in existing_keys:
                        continue
                    scored = score_related_todos(primary, candidate)
                    if scored["score"] < min_score:
                        continue
                    members.append(
                        {
                            "todo": candidate,
                            "score": scored["score"],
                            "reason": scored["reason"],
                            "relation_kind": "inferred",
                        }
                    )
            members = sorted(
                members,
                key=lambda item: (
                    item["relation_kind"] not in {"seed", "explicit"},
                    -item["score"],
                    item["todo"]["todo_key"],
                ),
            )[: max(2, limit)]
            if len(members) >= 2:
                scores = [member["score"] for member in members if member["score"] < 999]
                groups.append(
                    {
                        "todo_keys": [member["todo"]["todo_key"] for member in members],
                        "members": members,
                        "score": max(scores) if scores else 999,
                        "reason": "; ".join(
                            member["reason"]
                            for member in members
                            if member["reason"] and member["relation_kind"] != "seed"
                        )[:2000],
                    }
                )
            return groups

        edges: list[tuple[str, str, dict[str, Any]]] = []
        for index, primary in enumerate(todos):
            for candidate in todos[index + 1 :]:
                scored = score_related_todos(primary, candidate)
                if scored["score"] >= min_score:
                    edges.append((primary["todo_key"], candidate["todo_key"], scored))
        edges.sort(key=lambda edge: (-edge[2]["score"], edge[0], edge[1]))
        for a_key, b_key, scored in edges[:limit]:
            members = [
                {
                    "todo": todo_by_key[a_key],
                    "score": scored["score"],
                    "reason": scored["reason"],
                    "relation_kind": "inferred",
                },
                {
                    "todo": todo_by_key[b_key],
                    "score": scored["score"],
                    "reason": scored["reason"],
                    "relation_kind": "inferred",
                },
            ]
            groups.append(
                {
                    "todo_keys": [a_key, b_key],
                    "members": members,
                    "score": scored["score"],
                    "reason": scored["reason"],
                }
            )
        return groups

    def todo_auto_group_related(self, args: dict[str, Any]) -> dict[str, Any]:
        seed_keys = self.todo_keys_arg(args)
        app_scope = self.validate_app_scope(optional_string(args, "app_scope", default="")) or ""
        min_score = clamp_limit(
            args.get("min_score"),
            default=TODO_GROUP_DEFAULT_MIN_SCORE,
            maximum=50,
        )
        limit = clamp_limit(args.get("limit"), default=TODO_GROUP_DEFAULT_LIMIT, maximum=20)
        apply = bool_arg(args, "apply", default=False)
        include_seed_inferred = bool_arg(args, "include_inferred_candidates", default=False)
        actor = optional_string(args, "actor", default="agent", max_length=120) or "agent"
        group_key_arg = optional_string(args, "group_key", default="", max_length=160)
        cursor = optional_string(args, "cursor", default="", max_length=256)
        group_offset = decode_page_cursor(cursor, kind="todo-groups")
        with self.connection() as conn:
            candidate_args = dict(args)
            candidate_args.update(
                {"status": args.get("status", "open"), "app_scope": app_scope, "limit": 100, "cursor": ""}
            )
            candidate_result = self.query_todos(candidate_args, maximum=TODO_LIST_MAX_LIMIT)
            todos = candidate_result["todos"]
            if seed_keys:
                rows = self.fetch_todo_rows_by_keys(conn, seed_keys)
                missing = [todo_key for todo_key in seed_keys if todo_key not in rows]
                if missing:
                    raise ToolExecutionError("No todo found for: " + ", ".join(missing) + ".")
                existing = {todo["todo_key"] for todo in todos}
                for row in rows.values():
                    if row["todo_key"] not in existing:
                        todos.append(row_to_todo(row))
            all_candidate_groups = self.candidate_groups_from_scores(
                todos,
                seed_keys=seed_keys,
                min_score=min_score,
                limit=20,
                include_seed_inferred=include_seed_inferred,
            )
            candidate_groups = all_candidate_groups[group_offset : group_offset + limit]
            persisted: list[dict[str, Any]] = []
            if apply:
                for index, candidate_group in enumerate(candidate_groups):
                    todo_keys = candidate_group["todo_keys"]
                    group_key = (
                        safe_group_key(group_key_arg)
                        if group_key_arg and index == 0
                        else default_todo_group_key(todo_keys)
                    )
                    title = "Related TODOs: " + ", ".join(todo_keys[:3])
                    members = [
                        {
                            "todo_key": member["todo"]["todo_key"],
                            "relation_kind": "auto"
                            if member["relation_kind"] in {"seed", "explicit"}
                            else "inferred",
                            "confidence": 1.0
                            if member["score"] >= 999
                            else min(1.0, member["score"] / max(float(min_score * 2), 1.0)),
                            "reason": member["reason"],
                        }
                        for member in candidate_group["members"]
                    ]
                    members.sort(
                        key=lambda member: (
                            -next(
                                candidate["score"]
                                for candidate in candidate_group["members"]
                                if candidate["todo"]["todo_key"] == member["todo_key"]
                            ),
                            member["todo_key"],
                        )
                    )
                    group = self.upsert_todo_group(
                        conn,
                        group_key=group_key,
                        title=title,
                        detail=candidate_group["reason"],
                        group_type="auto",
                        source=actor,
                        criteria={
                            "seed_todo_keys": seed_keys,
                            "min_score": min_score,
                            "app_scope": app_scope,
                            "apply": apply,
                            "include_inferred_candidates": include_seed_inferred,
                        },
                        members=members,
                    )
                    persisted.append(group["group"])
                conn.commit()
        suggestions = [
            {
                "todo_keys": group["todo_keys"],
                "score": group["score"],
                "reason": group["reason"],
                "members": [
                    {
                        "todo_key": member["todo"]["todo_key"],
                        "title": member["todo"]["title"],
                        "app_scope": member["todo"].get("app_scope") or "",
                        "priority": member["todo"]["priority"],
                        "status": member["todo"]["status"],
                        "score": member["score"],
                        "relation_kind": member["relation_kind"],
                        "reason": member["reason"],
                    }
                    for member in group["members"]
                ],
            }
            for group in candidate_groups
        ]
        return {
            "applied": apply,
            "min_score": min_score,
            "include_inferred_candidates": include_seed_inferred,
            "suggested_groups": suggestions,
            "groups": persisted,
            "candidate_total": candidate_result["total"],
            "candidate_has_more": candidate_result["has_more"],
            "candidate_next_cursor": candidate_result["next_cursor"],
            "cursor": cursor or None,
            "total": len(all_candidate_groups),
            "has_more": group_offset + len(candidate_groups) < len(all_candidate_groups),
            "next_cursor": (
                encode_page_cursor(group_offset + len(candidate_groups), kind="todo-groups")
                if group_offset + len(candidate_groups) < len(all_candidate_groups)
                else None
            ),
        }

    def resolve_todo_for_task_key(
        self,
        conn: sqlite3.Connection,
        task_key: str,
    ) -> sqlite3.Row | None:
        task_key = safe_todo_key(task_key)
        if not task_key:
            return None
        return conn.execute(
            """
            SELECT * FROM mcp_todos
            WHERE todo_key = ? OR source_task_key = ?
            ORDER BY
                CASE WHEN todo_key = ? THEN 0 ELSE 1 END,
                updated_at DESC,
                id DESC
            LIMIT 1
            """,
            (task_key, task_key, task_key),
        ).fetchone()

    def explicit_related_todos(
        self,
        conn: sqlite3.Connection,
        todo_key: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        group_rows = conn.execute(
            """
            SELECT DISTINCT g.*
            FROM mcp_todo_groups g
            JOIN mcp_todo_group_members m ON m.group_id = g.id
            WHERE m.todo_key = ?
            ORDER BY g.group_key ASC
            """,
            (todo_key,),
        ).fetchall()
        related: list[dict[str, Any]] = []
        groups: list[dict[str, Any]] = []
        seen: set[str] = set()
        for group_row in group_rows:
            group = self.todo_group_get_by_key(conn, group_row["group_key"])
            if not group.get("found"):
                continue
            group_item = group["group"]
            groups.append(
                {
                    key: group_item[key]
                    for key in (
                        "group_key",
                        "title",
                        "detail",
                        "group_type",
                        "source",
                        "criteria",
                        "created_at",
                        "updated_at",
                    )
                }
            )
            groups[-1]["group_size"] = len(group_item["members"])
            for member in group_item["members"]:
                if member["todo_key"] == todo_key or member["todo_key"] in seen:
                    continue
                seen.add(member["todo_key"])
                related.append(member)
        return related, groups

    def inferred_related_todos(
        self,
        conn: sqlite3.Connection,
        primary: dict[str, Any],
        *,
        min_score: int,
        limit: int,
        exclude_keys: set[str],
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        candidates = self.load_open_todos(conn, limit=300)
        inferred: list[dict[str, Any]] = []
        for candidate in candidates:
            candidate_key = candidate["todo_key"]
            if candidate_key == primary["todo_key"] or candidate_key in exclude_keys:
                continue
            if filters and not self.todo_matches_filters(candidate, filters):
                continue
            scored = score_related_todos(primary, candidate)
            if scored["score"] < min_score:
                continue
            candidate["relation_kind"] = "inferred"
            candidate["confidence"] = min(1.0, scored["score"] / max(float(min_score * 2), 1.0))
            candidate["relation_reason"] = scored["reason"]
            candidate["relation_source"] = "inferred"
            candidate["score"] = scored["score"]
            inferred.append(candidate)
        inferred.sort(key=lambda item: (-item["score"], item["todo_key"]))
        return inferred[:limit]

    def render_related_todo_cascade(self, guidance: dict[str, Any] | None) -> str:
        if not guidance or not guidance.get("related_todos"):
            return ""
        primary = guidance.get("todo") or {}
        lines = [
            f"Related TODO cascade for `{primary.get('todo_key', guidance.get('todo_key', ''))}`:",
        ]
        for todo in guidance["related_todos"][:TODO_GROUP_DEFAULT_LIMIT]:
            reason = todo.get("relation_reason") or todo.get("reason") or todo.get("group_title") or ""
            if len(reason) > 240:
                reason = reason[:237].rstrip() + "..."
            suffix = f" - {reason}" if reason else ""
            lines.append(
                f"- `{todo['todo_key']}` [{todo['priority']}] "
                f"{todo.get('app_scope') or 'unspecified'}: {todo['title']}{suffix}"
            )
        lines.extend(
            [
                "",
                "Before working deep into this context, decide for each related TODO whether to complete it now, pull it forward as the next task, or explicitly defer it in the todo/group detail.",
                "Use `mudra_todo_related_for_assignment` or `GET /api/dashboard/todos/<todo_key>/related` to refresh this cascade if the todo state changes.",
            ]
        )
        return "\n".join(lines)

    def todo_related_for_assignment(self, args: dict[str, Any]) -> dict[str, Any]:
        todo_key = optional_string(args, "todo_key", default="", max_length=160)
        source_task_key = optional_string(args, "source_task_key", default="", max_length=160)
        include_inferred = bool_arg(args, "include_inferred", default=True)
        min_score = clamp_limit(
            args.get("min_score"),
            default=TODO_GROUP_DEFAULT_MIN_SCORE,
            maximum=50,
        )
        limit = clamp_limit(args.get("limit"), default=TODO_GROUP_DEFAULT_LIMIT, maximum=20)
        cursor = optional_string(args, "cursor", default="", max_length=256)
        offset = decode_page_cursor(cursor, kind="related-todos")
        filter_args = dict(args)
        # With no candidate filters, retain the historical behavior of showing
        # explicit group members regardless of terminal status. Once a filter
        # is supplied, all candidate predicates are ANDed.
        has_candidate_filter = any(
            args.get(key)
            for key in ("app_scope", "status", "priority", "tags", "source", "search", "reference_search")
        )
        filters = self.todo_filter_options(filter_args) if has_candidate_filter else None
        with self.connection() as conn:
            row: sqlite3.Row | None = None
            resolved_by = ""
            if todo_key:
                todo_key = safe_todo_key(todo_key)
                row = conn.execute(
                    "SELECT * FROM mcp_todos WHERE todo_key = ?",
                    (todo_key,),
                ).fetchone()
                resolved_by = "todo_key"
            if row is None and source_task_key:
                row = self.resolve_todo_for_task_key(conn, source_task_key)
                resolved_by = "source_task_key" if row is not None else ""
            if row is None:
                lookup = todo_key or source_task_key
                return {
                    "found": False,
                    "todo_key": lookup,
                    "related_todos": [],
                    "groups": [],
                    "cascade_prompt": "",
                    "total": 0,
                    "has_more": False,
                    "next_cursor": None,
                }
            primary = row_to_todo(row)
            explicit_related, groups = self.explicit_related_todos(conn, primary["todo_key"])
            related = [
                todo
                for todo in explicit_related
                if not filters or self.todo_matches_filters(todo, filters)
            ]
            seen = {todo["todo_key"] for todo in related}
            if include_inferred:
                related.extend(
                    self.inferred_related_todos(
                        conn,
                        primary,
                        min_score=min_score,
                        limit=300,
                        exclude_keys=seen,
                        filters=filters,
                    )
                )
        total = len(related)
        page = related[offset : offset + limit]
        next_offset = offset + len(page)
        next_cursor = encode_page_cursor(next_offset, kind="related-todos") if next_offset < total else None
        guidance = {
            "found": True,
            "todo_key": primary["todo_key"],
            "resolved_by": resolved_by,
            "todo": primary,
            "groups": groups,
            "related_todos": page,
            "include_inferred": include_inferred,
            "min_score": min_score,
            "cursor": cursor or None,
            "limit": limit,
            "total": total,
            "has_more": next_cursor is not None,
            "next_cursor": next_cursor,
        }
        guidance["cascade_prompt"] = self.render_related_todo_cascade(guidance)
        return guidance

    def todo_add(self, args: dict[str, Any]) -> dict[str, Any]:
        todo_key = safe_todo_key(require_string(args, "todo_key", max_length=160))
        title = require_string(args, "title", max_length=240)
        detail = optional_string(args, "detail", default="", max_length=20_000)
        app_scope = self.validate_app_scope(optional_string(args, "app_scope", default="")) or ""
        priority = normalize_todo_priority(args.get("priority"), default="P2")
        status = normalize_todo_status(args.get("status"), default="suggested")
        if status not in TODO_ADDABLE_STATUSES:
            raise ToolExecutionError("`status` for todo_add must be an open todo status.")
        source = optional_string(args, "source", default="agent", max_length=120) or "agent"
        source_task_key = optional_string(args, "source_task_key", default="", max_length=160)
        planned_complexity = optional_string(args, "planned_complexity", default="", max_length=40)
        actual_complexity = optional_string(args, "actual_complexity", default="", max_length=40)
        now = utc_now()
        with self.connection() as conn:
            existing = conn.execute(
                "SELECT * FROM mcp_todos WHERE todo_key = ?",
                (todo_key,),
            ).fetchone()
            existing_todo = row_to_todo(existing) if existing else {}
            tags = parse_string_list(args.get("tags", existing_todo.get("tags", [])), "tags")
            reference_values: dict[str, list[str]] = {}
            for field_name in TODO_JSON_LIST_COLUMNS:
                if field_name == "tags":
                    continue
                reference_values[field_name] = parse_string_list(
                    args.get(field_name, existing_todo.get(field_name, [])),
                    field_name,
                    max_item_length=500,
                    max_items=80,
                )
            conn.execute(
                """
                INSERT INTO mcp_todos(
                    todo_key, app_scope, title, detail, status, priority, source,
                    source_task_key, tags_json, code_paths_json, symbol_refs_json,
                    doc_keys_json, route_refs_json, test_refs_json, search_queries_json,
                    planned_complexity, actual_complexity, created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(todo_key) DO UPDATE SET
                    app_scope = excluded.app_scope,
                    title = excluded.title,
                    detail = excluded.detail,
                    status = excluded.status,
                    priority = excluded.priority,
                    source = excluded.source,
                    source_task_key = excluded.source_task_key,
                    tags_json = excluded.tags_json,
                    code_paths_json = excluded.code_paths_json,
                    symbol_refs_json = excluded.symbol_refs_json,
                    doc_keys_json = excluded.doc_keys_json,
                    route_refs_json = excluded.route_refs_json,
                    test_refs_json = excluded.test_refs_json,
                    search_queries_json = excluded.search_queries_json,
                    planned_complexity = excluded.planned_complexity,
                    actual_complexity = excluded.actual_complexity,
                    updated_at = excluded.updated_at,
                    completed_at = NULL
                """,
                (
                    todo_key,
                    app_scope,
                    title,
                    detail,
                    status,
                    priority,
                    source,
                    source_task_key,
                    json_text(tags),
                    json_text(reference_values["code_paths"]),
                    json_text(reference_values["symbol_refs"]),
                    json_text(reference_values["doc_keys"]),
                    json_text(reference_values["route_refs"]),
                    json_text(reference_values["test_refs"]),
                    json_text(reference_values["search_queries"]),
                    planned_complexity or None,
                    actual_complexity or None,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM mcp_todos WHERE todo_key = ?",
                (todo_key,),
            ).fetchone()
            self.insert_todo_event(
                conn,
                todo_id=row["id"],
                todo_key=todo_key,
                action="update" if existing else "add",
                actor=source,
                detail=detail,
            )
            conn.commit()
        todo = row_to_todo(row)
        related_guidance = None
        if status in {"accepted", "in_progress"}:
            related_guidance = self.todo_related_for_assignment(
                {
                    "todo_key": todo_key,
                    "include_inferred": True,
                    "limit": TODO_GROUP_DEFAULT_LIMIT,
                }
            )
        return {
            "todo": todo,
            "advisories": todo["reference_advisories"],
            "related_todo_guidance": related_guidance,
        }

    def todo_list(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.query_todos(args)

    def todo_get(self, todo_key: str, *, include_events: bool = True) -> dict[str, Any]:
        todo_key = safe_todo_key(todo_key)
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM mcp_todos WHERE todo_key = ?",
                (todo_key,),
            ).fetchone()
            if not row:
                return {"found": False, "todo_key": todo_key}
            events: list[dict[str, Any]] = []
            if include_events:
                events = [
                    dict(event)
                    for event in conn.execute(
                        """
                        SELECT * FROM mcp_todo_events
                        WHERE todo_key = ?
                        ORDER BY ts DESC, id DESC
                        LIMIT 30
                        """,
                        (todo_key,),
                    ).fetchall()
                ]
        return {"found": True, "todo": row_to_todo(row), "events": events}

    def mark_todo_queued(
        self, conn: sqlite3.Connection, todo_key: str, *, actor: str = "orchestrator"
    ) -> None:
        """Set an open todo's status to `queued` inside the caller's transaction.

        Only an open, non-queued todo is transitioned; terminal or already-queued
        rows are left untouched so enqueue idempotency does not churn the status.
        The caller commits.
        """
        row = conn.execute(
            "SELECT id, status FROM mcp_todos WHERE todo_key = ?", (todo_key,)
        ).fetchone()
        if not row or row["status"] == "queued" or row["status"] in TODO_TERMINAL_STATUSES:
            return
        now = utc_now()
        conn.execute(
            "UPDATE mcp_todos SET status = 'queued', updated_at = ? WHERE todo_key = ?",
            (now, todo_key),
        )
        self.insert_todo_event(
            conn,
            todo_id=row["id"],
            todo_key=todo_key,
            action="update",
            actor=actor,
            detail="queued for orchestration",
        )

    def clear_todo_queued(
        self,
        conn: sqlite3.Connection,
        todo_key: str,
        *,
        status: str = "accepted",
        actor: str = "orchestrator",
        detail: str = "requeued to open",
    ) -> None:
        """Move a `queued` todo back to an open status inside the caller's transaction.

        Used when a not-yet-dispatched queue item is dropped or cancelled so the
        todo does not stay greyed forever. Only a currently-`queued` row is
        transitioned; the caller commits.
        """
        if status not in TODO_ACTIVE_STATUSES or status == "queued":
            status = "accepted"
        row = conn.execute(
            "SELECT id, status FROM mcp_todos WHERE todo_key = ?", (todo_key,)
        ).fetchone()
        if not row or row["status"] != "queued":
            return
        now = utc_now()
        conn.execute(
            "UPDATE mcp_todos SET status = ?, updated_at = ? WHERE todo_key = ?",
            (status, now, todo_key),
        )
        self.insert_todo_event(
            conn,
            todo_id=row["id"],
            todo_key=todo_key,
            action="update",
            actor=actor,
            detail=detail,
        )

    def todo_prune(self, args: dict[str, Any]) -> dict[str, Any]:
        todo_key = safe_todo_key(require_string(args, "todo_key", max_length=160))
        status = normalize_todo_status(args.get("status"), default="done")
        if status not in TODO_TERMINAL_STATUSES:
            raise ToolExecutionError("`status` for todo_prune must be done or dropped.")
        actor = optional_string(args, "actor", default="agent", max_length=120) or "agent"
        detail = optional_string(args, "detail", default="", max_length=20_000)
        # Optional posthoc/actual complexity estimate, made by the agent that just
        # finished the work, for planned-vs-actual comparison (see
        # docs/mcp-server/model-efficiency.md). Only overwrites actual_complexity
        # when provided, so pruning without an estimate leaves any prior value
        # (e.g. set earlier via mudra_todo_add) untouched.
        actual_complexity = optional_string(
            args, "actual_complexity", default="", max_length=40
        )
        now = utc_now()
        closed_task_rows: list[sqlite3.Row] = []
        orchestration_dispatch: dict[str, Any] | None = None
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM mcp_todos WHERE todo_key = ?",
                (todo_key,),
            ).fetchone()
            if not row:
                raise ToolExecutionError(f"No todo found for `{todo_key}`.")
            if actual_complexity:
                conn.execute(
                    """
                    UPDATE mcp_todos
                    SET status = ?,
                        updated_at = ?,
                        completed_at = ?,
                        actual_complexity = ?
                    WHERE todo_key = ?
                    """,
                    (status, now, now, actual_complexity, todo_key),
                )
            else:
                conn.execute(
                    """
                    UPDATE mcp_todos
                    SET status = ?,
                        updated_at = ?,
                        completed_at = ?
                    WHERE todo_key = ?
                    """,
                    (status, now, now, todo_key),
                )
            updated = conn.execute(
                "SELECT * FROM mcp_todos WHERE todo_key = ?",
                (todo_key,),
            ).fetchone()
            self.insert_todo_event(
                conn,
                todo_id=updated["id"],
                todo_key=todo_key,
                action="prune",
                actor=actor,
                detail=detail or status,
            )
            task_status = "done" if status == "done" else "abandoned"
            task_detail = (
                detail
                or f"Linked todo `{todo_key}` was pruned as {status}; closing task state."
            )
            task_rows = conn.execute(
                """
                SELECT * FROM agent_task_state
                WHERE task_key = ?
                  AND status IN ('in_progress', 'paused', 'blocked')
                """,
                (todo_key,),
            ).fetchall()
            for task_row in task_rows:
                conn.execute(
                    """
                    UPDATE agent_task_state
                    SET status = ?,
                        last_seen_at = ?,
                        expires_at = ?,
                        completed_at = ?,
                        flushed_at = NULL,
                        flush_id = NULL
                    WHERE id = ?
                    """,
                    (task_status, now, now, now, task_row["id"]),
                )
                closed_task = conn.execute(
                    "SELECT * FROM agent_task_state WHERE id = ?",
                    (task_row["id"],),
                ).fetchone()
                self.insert_task_event(
                    conn,
                    task_state_id=closed_task["id"],
                    task_key=closed_task["task_key"],
                    agent_id=closed_task["agent_id"],
                    action="todo_prune",
                    status=task_status,
                    summary=closed_task["summary"],
                    detail=task_detail,
                )
                closed_task_rows.append(closed_task)
            if self.orchestration is not None:
                orchestration_dispatch = self.orchestration.dispatch_for_pruned_todo(
                    conn, todo_key=todo_key
                )
            conn.commit()
        return {
            "todo": row_to_todo(updated),
            "closed_tasks": [self.row_to_task(task_row) for task_row in closed_task_rows],
            "orchestration_dispatch": orchestration_dispatch,
        }

    def todo_update_priority(self, args: dict[str, Any]) -> dict[str, Any]:
        todo_key = safe_todo_key(require_string(args, "todo_key", max_length=160))
        priority = normalize_todo_priority(args.get("priority"), default="P2")
        actor = optional_string(args, "actor", default="dashboard", max_length=120) or "dashboard"
        detail = optional_string(args, "detail", default="", max_length=20_000)
        now = utc_now()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM mcp_todos WHERE todo_key = ?",
                (todo_key,),
            ).fetchone()
            if not row:
                raise ToolExecutionError(f"No todo found for `{todo_key}`.")
            old_priority = row["priority"]
            conn.execute(
                """
                UPDATE mcp_todos
                SET priority = ?,
                    updated_at = ?
                WHERE todo_key = ?
                """,
                (priority, now, todo_key),
            )
            updated = conn.execute(
                "SELECT * FROM mcp_todos WHERE todo_key = ?",
                (todo_key,),
            ).fetchone()
            self.insert_todo_event(
                conn,
                todo_id=updated["id"],
                todo_key=todo_key,
                action="priority",
                actor=actor,
                detail=detail or f"{old_priority} -> {priority}",
            )
            conn.commit()
        return {"todo": row_to_todo(updated)}

    def todo_update_scope(self, args: dict[str, Any]) -> dict[str, Any]:
        todo_key = safe_todo_key(require_string(args, "todo_key", max_length=160))
        app_scope = self.validate_app_scope(optional_string(args, "app_scope", default="")) or ""
        actor = optional_string(args, "actor", default="dashboard", max_length=120) or "dashboard"
        detail = optional_string(args, "detail", default="", max_length=20_000)
        now = utc_now()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM mcp_todos WHERE todo_key = ?",
                (todo_key,),
            ).fetchone()
            if not row:
                raise ToolExecutionError(f"No todo found for `{todo_key}`.")
            old_scope = row["app_scope"] or ""
            conn.execute(
                """
                UPDATE mcp_todos
                SET app_scope = ?,
                    updated_at = ?
                WHERE todo_key = ?
                """,
                (app_scope, now, todo_key),
            )
            updated = conn.execute(
                "SELECT * FROM mcp_todos WHERE todo_key = ?",
                (todo_key,),
            ).fetchone()
            self.insert_todo_event(
                conn,
                todo_id=updated["id"],
                todo_key=todo_key,
                action="scope",
                actor=actor,
                detail=detail or f"{old_scope or 'unspecified'} -> {app_scope or 'unspecified'}",
            )
            conn.commit()
        return {"todo": row_to_todo(updated)}

    def todo_append_references(self, args: dict[str, Any]) -> dict[str, Any]:
        todo_key = safe_todo_key(require_string(args, "todo_key", max_length=160))
        doc_keys = parse_string_list(
            args.get("doc_keys"),
            "doc_keys",
            max_item_length=500,
            max_items=80,
        )
        if not doc_keys:
            raise ToolExecutionError(
                "`doc_keys` must include at least one doc_key or chunk_id reference."
            )
        actor = optional_string(args, "actor", default="dashboard", max_length=120) or "dashboard"
        detail = optional_string(args, "detail", default="", max_length=20_000)
        now = utc_now()
        appended: list[str] = []
        already_present: list[str] = []
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM mcp_todos WHERE todo_key = ?",
                (todo_key,),
            ).fetchone()
            if not row:
                raise ToolExecutionError(f"No todo found for `{todo_key}`.")
            if row["status"] not in TODO_ACTIVE_STATUSES:
                raise ToolExecutionError(f"Todo `{todo_key}` is not open.")
            todo = row_to_todo(row)
            merged = list(todo.get("doc_keys") or [])
            seen = set(merged)
            for doc_key in doc_keys:
                if doc_key in seen:
                    if doc_key not in already_present:
                        already_present.append(doc_key)
                    continue
                merged.append(doc_key)
                seen.add(doc_key)
                appended.append(doc_key)
            if len(merged) > 80:
                raise ToolExecutionError("`doc_keys` cannot exceed 80 items after append.")
            if appended:
                conn.execute(
                    """
                    UPDATE mcp_todos
                    SET doc_keys_json = ?,
                        updated_at = ?
                    WHERE todo_key = ?
                    """,
                    (json_text(merged), now, todo_key),
                )
            updated = conn.execute(
                "SELECT * FROM mcp_todos WHERE todo_key = ?",
                (todo_key,),
            ).fetchone()
            event_detail = detail
            if not event_detail:
                if appended:
                    event_detail = "Appended doc refs: " + ", ".join(appended[:8])
                else:
                    event_detail = "Doc refs already present: " + ", ".join(already_present[:8])
            self.insert_todo_event(
                conn,
                todo_id=updated["id"],
                todo_key=todo_key,
                action="references",
                actor=actor,
                detail=event_detail,
            )
            conn.commit()
        return {
            "todo": row_to_todo(updated),
            "appended": appended,
            "already_present": already_present,
        }

    def todo_filter_options(
        self,
        args: dict[str, Any],
        *,
        default_status: str = "open",
    ) -> dict[str, Any]:
        status = optional_string(args, "status", default=default_status, max_length=40) or default_status
        if status != "open" and status not in TODO_STATUSES:
            raise ToolExecutionError(
                "`status` must be `open` or one of: " + ", ".join(sorted(TODO_STATUSES)) + "."
            )
        priority_value = optional_string(args, "priority", default="", max_length=8)
        priority = normalize_todo_priority(priority_value) if priority_value else ""
        tags = parse_string_list(args.get("tags"), "tags", max_item_length=120, max_items=40)
        search = optional_string(args, "search", default="", max_length=240)
        reference_search = optional_string(args, "reference_search", default="", max_length=240)
        if search and reference_search:
            search = f"{search} {reference_search}"
        else:
            search = search or reference_search
        return {
            "status": status,
            "app_scope": self.validate_app_scope(optional_string(args, "app_scope", default="")) or "",
            "priority": priority,
            "tags": tags,
            "source": optional_string(args, "source", default="", max_length=120),
            "search": search,
            "todo_key": safe_todo_key(optional_string(args, "todo_key", default="", max_length=160)),
            "cursor": optional_string(args, "cursor", default="", max_length=256),
        }

    @staticmethod
    def todo_matches_filters(todo: dict[str, Any], filters: dict[str, Any]) -> bool:
        status = filters["status"]
        if status == "open":
            if todo.get("status") not in TODO_ACTIVE_STATUSES:
                return False
        elif todo.get("status") != status:
            return False
        if filters["app_scope"] and todo.get("app_scope") != filters["app_scope"]:
            return False
        if filters["priority"] and todo.get("priority") != filters["priority"]:
            return False
        if filters["source"] and todo.get("source") != filters["source"]:
            return False
        todo_tags = {str(tag).casefold() for tag in (todo.get("tags") or [])}
        if any(tag.casefold() not in todo_tags for tag in filters["tags"]):
            return False
        search = filters["search"].casefold()
        if search:
            searchable = " ".join(
                str(todo.get(field) or "")
                for field in (
                    "todo_key",
                    "title",
                    "detail",
                    "source",
                    "source_task_key",
                    "tags",
                    "code_paths",
                    "symbol_refs",
                    "doc_keys",
                    "route_refs",
                    "test_refs",
                    "search_queries",
                )
            ).casefold()
            if search not in searchable:
                return False
        return True

    def query_todos(
        self,
        args: dict[str, Any],
        *,
        default_status: str = "open",
        default_limit: int = TODO_LIST_DEFAULT_LIMIT,
        maximum: int = TODO_LIST_MAX_LIMIT,
        cursor_kind: str = "todos",
    ) -> dict[str, Any]:
        filters = self.todo_filter_options(args, default_status=default_status)
        limit = clamp_limit(args.get("limit"), default=default_limit, maximum=maximum)
        offset = decode_page_cursor(filters["cursor"], kind=cursor_kind)

        # An explicit key is authoritative: it is a direct lookup, even when
        # the caller also supplied status/scope/search filters.
        with self.connection() as conn:
            if filters["todo_key"]:
                rows = conn.execute(
                    "SELECT * FROM mcp_todos WHERE todo_key = ?",
                    (filters["todo_key"],),
                ).fetchall()
            else:
                where = []
                params: list[Any] = []
                if filters["status"] == "open":
                    where.append(f"status IN {TODO_OPEN_STATUS_SQL}")
                else:
                    where.append("status = ?")
                    params.append(filters["status"])
                if filters["app_scope"]:
                    where.append("app_scope = ?")
                    params.append(filters["app_scope"])
                if filters["priority"]:
                    where.append("priority = ?")
                    params.append(filters["priority"])
                if filters["source"]:
                    where.append("source = ?")
                    params.append(filters["source"])
                rows = conn.execute(
                    f"""
                    SELECT * FROM mcp_todos
                    WHERE {' AND '.join(where)}
                    ORDER BY
                        CASE priority
                            WHEN 'P0' THEN 0
                            WHEN 'P1' THEN 1
                            WHEN 'P2' THEN 2
                            WHEN 'P3' THEN 3
                            ELSE 4
                        END,
                        CASE status
                            WHEN 'suggested' THEN 0
                            WHEN 'accepted' THEN 1
                            WHEN 'blocked' THEN 2
                            WHEN 'in_progress' THEN 3
                            WHEN 'queued' THEN 4
                            WHEN 'done' THEN 5
                            ELSE 6
                        END,
                        updated_at DESC,
                        todo_key ASC,
                        id DESC
                    """,
                    params,
                ).fetchall()
            all_todos = [row_to_todo(row) for row in rows]
            self.attach_primary_group_metadata(conn, all_todos)
        if not filters["todo_key"]:
            all_todos = [todo for todo in all_todos if self.todo_matches_filters(todo, filters)]
        total = len(all_todos)
        page = all_todos[offset : offset + limit]
        next_offset = offset + len(page)
        next_cursor = encode_page_cursor(next_offset, kind=cursor_kind) if next_offset < total else None
        return {
            **filters,
            "app_scope": filters["app_scope"] or None,
            "priority": filters["priority"] or None,
            "source": filters["source"] or None,
            "search": filters["search"] or None,
            "todo_key": filters["todo_key"] or None,
            "cursor": filters["cursor"] or None,
            "limit": limit,
            "todos": page,
            "total": total,
            "has_more": next_cursor is not None,
            "next_cursor": next_cursor,
        }

    def todo_is_in_progress(self, todo: dict[str, Any], active_keys: set[str]) -> bool:
        if todo.get("status") == "in_progress":
            return True
        todo_key = (todo.get("todo_key") or "").casefold()
        if todo_key and todo_key in active_keys:
            return True
        source_key = (todo.get("source_task_key") or "").casefold()
        return bool(source_key) and source_key in active_keys

    def todo_next_instruction(self, args: dict[str, Any]) -> dict[str, Any]:
        app_scope = self.validate_app_scope(optional_string(args, "app_scope", default="")) or ""
        limit = clamp_limit(args.get("limit"), default=3, maximum=10)
        todo_key = optional_string(args, "todo_key", default="", max_length=160)
        variant = optional_string(args, "variant", default="implement", max_length=40) or "implement"
        if variant not in {"implement", "validation_failed"}:
            raise ToolExecutionError("`variant` must be `implement` or `validation_failed` when provided.")
        if todo_key:
            todo_key = safe_todo_key(todo_key)
            result = self.todo_get(todo_key, include_events=False)
            if not result["found"]:
                raise ToolExecutionError(f"No todo found for `{todo_key}`.")
            primary = result["todo"]
            if primary["status"] in TODO_TERMINAL_STATUSES:
                raise ToolExecutionError(f"Todo `{todo_key}` is already {primary['status']}.")
            active_keys = self.active_task_keys()
            context_scope = primary.get("app_scope") or app_scope
            context_args = dict(args)
            context_args.update(
                {
                    "status": "open",
                    "app_scope": context_scope,
                    "todo_key": "",
                    "limit": clamp_limit(limit + 5, maximum=25),
                }
            )
            context_result = self.todo_list(context_args)
            context_todos = context_result["todos"]
            current_guidance = self.todo_related_for_assignment(
                {
                    "todo_key": primary["todo_key"],
                    "include_inferred": True,
                    "limit": TODO_GROUP_DEFAULT_LIMIT,
                }
            )
            if self.todo_is_in_progress(primary, active_keys):
                todos = [
                    todo
                    for todo in context_todos
                    if todo["todo_key"] != primary["todo_key"]
                    and not self.todo_is_in_progress(todo, active_keys)
                ][:limit]
                instruction = self.render_todo_instruction(
                    todos,
                    app_scope=context_scope,
                    current=primary,
                    related_guidance=current_guidance,
                    variant=variant,
                )
                advisories = todos[0].get("reference_advisories", []) if todos else []
                return {
                    "instruction": instruction,
                    "todos": todos,
                    "current": primary,
                    "advisories": advisories,
                    "related_todo_guidance": current_guidance,
                    "total": context_result["total"],
                    "has_more": context_result["has_more"],
                    "next_cursor": context_result["next_cursor"],
                }
            todos = [primary]
            for todo in context_todos:
                if todo["todo_key"] == primary["todo_key"]:
                    continue
                if self.todo_is_in_progress(todo, active_keys):
                    continue
                todos.append(todo)
                if len(todos) >= limit:
                    break
            instruction = self.render_todo_instruction(
                todos,
                app_scope=context_scope,
                related_guidance=current_guidance,
                variant=variant,
            )
            return {
                "instruction": instruction,
                "todos": todos,
                "current": None,
                "advisories": primary.get("reference_advisories", []),
                "related_todo_guidance": current_guidance,
                "total": context_result["total"],
                "has_more": context_result["has_more"],
                "next_cursor": context_result["next_cursor"],
            }
        # Fetch a few extra so that, when a todo is already in progress, we can
        # skip past it (and any other in-progress work) and still surface `limit`
        # upcoming todos.
        list_args = dict(args)
        list_args.update(
            {
                "status": args.get("status", "open"),
                "app_scope": app_scope,
                "todo_key": "",
                "limit": clamp_limit(limit + 5, maximum=25),
            }
        )
        fetched_result = self.todo_list(list_args)
        fetched = fetched_result["todos"]
        # A todo counts as the current task if its own status is `in_progress`
        # OR an agent is actively checked in against it (see `active_task_keys`).
        # Treat that as the current task and surface the next ones instead, so a
        # repeat call doesn't just hand back work that's already underway.
        # `todo_list` orders by priority then recency, so the highest-priority
        # in-progress todo is the current one and the remaining open todos are
        # returned in their normal order.
        active_keys = self.active_task_keys()
        current = next(
            (todo for todo in fetched if self.todo_is_in_progress(todo, active_keys)),
            None,
        )
        if current is not None:
            todos = [
                todo
                for todo in fetched
                if not self.todo_is_in_progress(todo, active_keys)
            ][:limit]
            related_guidance = self.todo_related_for_assignment(
                {
                    "todo_key": current["todo_key"],
                    "include_inferred": True,
                    "limit": TODO_GROUP_DEFAULT_LIMIT,
                }
            )
        else:
            todos = fetched[:limit]
            related_guidance = (
                self.todo_related_for_assignment(
                    {
                        "todo_key": todos[0]["todo_key"],
                        "include_inferred": True,
                        "limit": TODO_GROUP_DEFAULT_LIMIT,
                    }
                )
                if todos
                else None
            )
        instruction = self.render_todo_instruction(
            todos,
            app_scope=app_scope,
            current=current,
            related_guidance=related_guidance,
            variant=variant,
        )
        advisories = todos[0].get("reference_advisories", []) if todos else []
        return {
            "instruction": instruction,
            "todos": todos,
            "current": current,
            "advisories": advisories,
            "related_todo_guidance": related_guidance,
            "total": fetched_result["total"],
            "has_more": fetched_result["has_more"],
            "next_cursor": fetched_result["next_cursor"],
        }

    def todo_reference_lines(
        self, todo: dict[str, Any], *, exclude: tuple[str, ...] = ()
    ) -> list[str]:
        lines: list[str] = []
        app_scope = todo.get("app_scope") or ""
        if app_scope in DOC_SCOPES:
            roots = DOC_SCOPES[app_scope]["roots"]
            if roots:
                lines.append(f"- Scope roots: {self.inline_values(roots)}")
        for field_name, label in TODO_REFERENCE_LABELS.items():
            if field_name in exclude:
                continue
            values = todo.get(field_name) or []
            if values:
                lines.append(f"- {label}: {self.inline_values(values)}")
        return lines

    @staticmethod
    def todo_definition_of_done_lines(todo: dict[str, Any]) -> list[str]:
        test_refs = [ref for ref in (todo.get("test_refs") or []) if ref]
        if not test_refs:
            return []
        return [
            "Definition of done - run each validation below and paste the key output into the prune detail; treat unmet items as incomplete work:",
            *[f"- {ref}" for ref in test_refs[:8]],
        ]

    @staticmethod
    def todo_feature_guidance_lines(todo: dict[str, Any]) -> list[str]:
        """Feature-specific checkout guidance for large feature todos.

        Keyed off `todo_key` in TODO_FEATURE_GUIDANCE. Returns a titled block of
        prose/bullets so an agent checking out (or in progress on) the todo gets
        full context on data flow, component dependencies, and dashboard
        implementation patterns without having to rediscover them.
        """
        entry = TODO_FEATURE_GUIDANCE.get(todo.get("todo_key") or "")
        if not entry:
            return []
        lines: list[str] = ["Feature implementation guidance:"]
        heading = entry.get("heading")
        if heading:
            lines.append(heading)
        for title, bullets in entry.get("sections", []):
            lines.append(f"- {title}:")
            for bullet in bullets:
                lines.append(f"  - {bullet}")
        return lines

    @staticmethod
    def todo_advisory_lines(todo: dict[str, Any]) -> list[str]:
        return [
            f"- {advisory}"
            for advisory in (todo.get("reference_advisories") or [])
            if advisory
        ]

    @staticmethod
    def todo_docs_scope_hint(todo: dict[str, Any], *, fallback_scope: str = "") -> str:
        app_scope = todo.get("app_scope") or fallback_scope
        if app_scope in DOC_SCOPES:
            return f"`{app_scope}`"
        return "the nearest relevant documentation scope"

    @staticmethod
    def inline_values(values: list[str], *, limit: int = 8) -> str:
        shown = values[:limit]
        rendered = ", ".join(f"`{value}`" for value in shown)
        if len(values) > limit:
            rendered += f", and {len(values) - limit} more"
        return rendered

    def todo_reference_summary(self, todo: dict[str, Any]) -> str:
        for field_name in ("code_paths", "symbol_refs", "route_refs", "doc_keys"):
            values = todo.get(field_name) or []
            if values:
                return ", ".join(values[:2])
        return ""

    def render_todo_instruction(
        self,
        todos: list[dict[str, Any]],
        *,
        app_scope: str = "",
        current: dict[str, Any] | None = None,
        related_guidance: dict[str, Any] | None = None,
        variant: str = "implement",
    ) -> str:
        if not todos:
            scope_text = f" for `{app_scope}`" if app_scope else ""
            if current is not None:
                lines = [
                    f"Todo `{current['todo_key']}` is already in progress and there are no further open todos{scope_text}.",
                    "",
                    f"Title: {current['title']}",
                    "",
                ]
                feature_lines = self.todo_feature_guidance_lines(current)
                if feature_lines:
                    lines.extend([*feature_lines, ""])
                cascade_prompt = self.render_related_todo_cascade(related_guidance)
                if cascade_prompt:
                    lines.extend([cascade_prompt, ""])
                definition_lines = self.todo_definition_of_done_lines(current)
                if definition_lines:
                    lines.extend([*definition_lines, ""])
                lines.extend(
                    [
                        "Finish the in-progress work, then call `mudra_task_check_out` with `done` and `mudra_todo_prune` to close it.",
                        "Before checkout, update affected docs/specs/runbooks for behavior changes and reindex the affected docs scope.",
                        "When pruning, estimate this task's actual (posthoc) complexity and pass it as `actual_complexity` to `mudra_todo_prune` for planned-vs-actual comparison.",
                        "After checkout, report approximate token usage with `mudra_task_token_usage` (best-effort totals are fine); add `mudra_task_token_recommendation` only for a specific, non-generic efficiency observation.",
                        "Add concrete follow-up todos with `mudra_todo_add` before requesting the next instruction.",
                    ]
                )
                return "\n".join(lines)
            return "\n".join(
                [
                    f"No open MCP todos are currently recorded{scope_text}.",
                    "",
                    f"Please inspect the {PROJECT.server_name} documentation index and propose concrete next steps.",
                    "Use `mudra_doc_scopes`, then `mudra_doc_search` for the relevant area.",
                    "Add 3-5 actionable follow-up todos with `mudra_todo_add`, each with a stable `todo_key`, `app_scope`, priority, concrete `code_paths`/`symbol_refs` when known, relevant `doc_keys` when behavior or docs may change, a best-effort `planned_complexity` (e.g. S/M/L/XL), and enough detail for another agent to start.",
                    "Then choose the highest-impact todo, register your model identity with "
                    "`mudra_agent_model_register` (your own `model_family`/`model_version`, "
                    "`model_variant` only when your model has a variant name such as `Sol` or "
                    "`Luna`, client product name in `client_name` only), check in with "
                    "`mudra_task_check_in` passing the returned `model_registration_key`, and begin work.",
                ]
            )

        primary = todos[0]
        lines = [
            "",
        ]
        if current is not None:
            lines.extend(
                [
                    f"Todo `{current['todo_key']}` is already in progress; this is the next todo after it.",
                    "",
                ]
            )
        lines.extend([
            f"Please pick up MCP todo `{primary['todo_key']}`.",
            "",
            f"Title: {primary['title']}",
            f"Scope: {primary['app_scope'] or 'unspecified'}",
            f"Priority: {primary['priority']}",
            "",
        ])
        if variant == "validation_failed":
            lines.extend(
                [
                    "A manual validation or on-device test FAILED; do not treat this todo as complete.",
                    "Operator failure report (fill in WHAT failed and the observed issue): <<< describe what failed and the observed issue >>>",
                    f"Re-scope todo `{primary['todo_key']}`, update its detail/references as needed, and write a NEW plan of action.",
                    "Then choose one resolution path:",
                    f"- Option A (follow-up): submit the re-scoped plan as a follow-up todo via `mudra_todo_add`, linking it to `{primary['todo_key']}`, then check out this task.",
                    "- Option B (in-session): only if the re-scoped fix is Small or Medium (S/M) AND the user agrees, implement it now and check out done.",
                    "",
                ]
            )
        if primary["detail"]:
            lines.extend(["Details:", primary["detail"], ""])
        reference_lines = self.todo_reference_lines(primary, exclude=("test_refs",))
        if reference_lines:
            lines.extend(["Likely references:", *reference_lines, ""])
            if primary.get("search_queries"):
                lines.append("Use the fallback searches only if the explicit references are stale.")
                lines.append("")
        else:
            lines.extend(
                [
                    "No explicit code references are attached yet. Use the scope roots and MCP docs before broad repository search.",
                    "",
                ]
            )
        feature_lines = self.todo_feature_guidance_lines(primary)
        if feature_lines:
            lines.extend([*feature_lines, ""])
        cascade_prompt = self.render_related_todo_cascade(related_guidance)
        if cascade_prompt:
            lines.extend([cascade_prompt, ""])
        if len(todos) > 1:
            lines.append("Nearby open todos for context:")
            for todo in todos[1:]:
                ref_hint = self.todo_reference_summary(todo)
                suffix = f" - refs: {ref_hint}" if ref_hint else ""
                lines.append(
                    f"- `{todo['todo_key']}` [{todo['priority']}] {todo['title']}{suffix}"
                )
            lines.append("")
        advisory_lines = self.todo_advisory_lines(primary)
        if advisory_lines:
            lines.extend(["Advisory checklist:", *advisory_lines, ""])
        definition_lines = self.todo_definition_of_done_lines(primary)
        if definition_lines:
            lines.extend([*definition_lines, ""])
        docs_scope_hint = self.todo_docs_scope_hint(primary, fallback_scope=app_scope)
        register_line = (
            "First, register your model identity with `mudra_agent_model_register`, then "
            "pass the returned `model_registration_key` to check-in and token usage. "
            "Report the model you are actually running as: set `model_family` and "
            "`model_version` to your own model (not the client you run inside), set "
            "`model_variant` to your variant name when your model has one (for example "
            "`Sol` or `Luna`) and omit it entirely when it does not, and set "
            "`reasoning_effort` to your current tier. Put the client product name in "
            "`client_name` only (for example `Codex CLI`, `Claude Code`) - never in "
            "`model_variant`, and never put the effort tier in `model_variant`. "
            "Use the returned `canonical_model_label` verbatim; do not compose your own."
        )
        check_in_line = (
            "Then check `mudra_task_active` for overlapping work and "
            "call `mudra_task_check_in` with this todo key as the task key or source context"
        )
        if cascade_prompt:
            check_in_line += (
                ", passing `suppress_cascade: true` since this prompt already includes "
                "the related-todo cascade."
            )
        else:
            check_in_line += "."
        lines.extend(
            [
                register_line,
                check_in_line,
                f"Before implementation, search {docs_scope_hint} with `mudra_doc_search`; use `mudra_doc_get` only for returned `doc_key` or `chunk_id` targets when snippets are not enough.",
                "Before checkout, update affected docs/specs/runbooks for behavior changes, reindex the affected docs scope, and carry relevant `doc_keys` into follow-up todos or handoffs.",
                "When complete, call `mudra_task_check_out` with `done`, prune this todo with `mudra_todo_prune`, and add any concrete follow-up todos with `mudra_todo_add` (include a best-effort `planned_complexity` for each), including likely code paths, symbols, routes, docs, and validation commands.",
                "When pruning, estimate this task's actual (posthoc) complexity and pass it as `actual_complexity` to `mudra_todo_prune` for planned-vs-actual comparison.",
                "After checkout, report approximate token usage with `mudra_task_token_usage` (best-effort totals are fine); add `mudra_task_token_recommendation` only for a specific, non-generic efficiency observation.",
            ]
        )
        return "\n".join(lines)
