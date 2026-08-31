"""Lifecycle, notes, events, and model-identity domain service.

This module owns the SQLite-backed agent lifecycle contract.  It is deliberately
transport- and TODO-independent; assignment cascade lookup is supplied as an
optional callback by the composition root.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Generator

from mcp.project import load_project_descriptor


class ToolExecutionError(Exception):
    """Expected domain validation failure exposed by MCP tool calls.

    `details` carries an optional bounded, machine-readable payload for callers
    that must branch on the failure. It never carries assignment tokens, claim
    lease tokens, or credential values.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


ACTIVE_TASK_STATUSES = {"in_progress", "paused", "blocked"}
TERMINAL_TASK_STATUSES = {"done", "abandoned", "flushed"}
TASK_STATUSES = ACTIVE_TASK_STATUSES | TERMINAL_TASK_STATUSES
DEFAULT_TASK_TTL_SECONDS = 3600
MAX_TASK_TTL_SECONDS = 24 * 3600
MAX_TOKEN_COUNT = 10_000_000_000
TASK_ACTIVE_DEFAULT_LIMIT = 25
TASK_ACTIVE_MAX_LIMIT = 100
MODEL_REGISTRATION_KEY_PREFIX = "mr_"
MODEL_REGISTRATION_PROVIDERS = {
    "openai", "anthropic", "google", "meta", "mistral", "xai", "other",
}
MODEL_REGISTRATION_FAMILIES = {
    "gpt": "GPT", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku",
    "fable": "Fable", "gemini": "Gemini", "llama": "Llama",
    "mistral": "Mistral", "grok": "Grok",
}
MODEL_REGISTRATION_FAMILY_PROVIDERS = {
    "gpt": {"openai"}, "opus": {"anthropic"}, "sonnet": {"anthropic"},
    "haiku": {"anthropic"}, "fable": {"anthropic"}, "gemini": {"google"},
    "llama": {"meta"}, "mistral": {"mistral"}, "grok": {"xai"},
}
MODEL_REGISTRATION_EFFORTS = {
    "low": "Low", "medium": "Medium", "high": "High",
    "extra_high": "Extra High", "max": "Max", "ultra": "Ultra", "none": "None",
}
# Known models per family: version -> allowed variant names (lowercase keys, with
# the canonical display spelling as the value). An empty variant map means the
# model has no named variants and `model_variant` must be omitted.
#
# This registry is the guard against self-reported drift: agents were sending the
# client product ("Codex") or the effort tier ("High") as `model_variant`, which
# split one model across several dashboard buckets. Unknown combinations are
# rejected rather than silently recorded.
#
# Add new models here as they ship. Keep in sync with the classifier defaults in
# `mcp/model_identity.py` and `mcp/gui/dashboard/analytics/shared.js`.
MODEL_REGISTRATION_CATALOG: dict[str, dict[str, dict[str, str]]] = {
    "gpt": {
        # 5 and 5.5 are deliberately absent; see GPT_MINIMUM_VERSION.
        "5.6": {"sol": "Sol", "luna": "Luna", "terra": "Terra"},
    },
    "opus": {"4.8": {}, "5": {}},
    "sonnet": {"4.5": {}, "5": {}},
    "haiku": {"4.5": {}},
    "fable": {"5": {}},
    "gemini": {"3": {}},
    "llama": {"4": {}},
    "mistral": {"3": {}},
    "grok": {"4": {}},
}

# GPT sessions in this repository are all 5.6 or newer, and every one of them runs
# as a named variant. Both rules exist because under-reporting was silently valid:
# a Luna agent that sent `5` produced the label `5 High`, which passed every check
# because bare GPT-5 was itself a catalog entry, and no variant is required when the
# claimed version has none. Rejecting the older versions removes the shape that made
# the mistake invisible; requiring the variant means a GPT registration cannot omit
# the field that distinguishes one 5.6 session from another.
#
# Raise the floor as older releases fall out of use. Keep the legacy classifier in
# `mcp/model_identity.py` and `mcp/gui/dashboard/analytics/shared.js` permissive:
# historical rows still carry `GPT 5`/`GPT 5.5` labels and must keep classifying.
GPT_MINIMUM_VERSION = (5, 6)
MODEL_FAMILIES_REQUIRING_VARIANT = frozenset({"gpt"})


def model_version_sort_key(version: str) -> tuple[int, ...]:
    """Numeric ordering key for a catalog version, ignoring any build suffix."""
    numeric = re.split(r"[-_]", version, maxsplit=1)[0]
    return tuple(int(part) for part in numeric.split("."))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clamp_limit(value: Any, default: int = 10, maximum: int = 100) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(limit, maximum))


def encode_page_cursor(offset: int, *, kind: str) -> str:
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
        offset = payload.get("offset")
        if payload.get("v") != 1 or payload.get("kind") != kind:
            raise ValueError
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except (AttributeError, ValueError, TypeError, binascii.Error, json.JSONDecodeError,
            UnicodeDecodeError):
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
    args: dict[str, Any], key: str, *, default: str = "", max_length: int | None = None
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


def optional_token_count(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ToolExecutionError(f"`{key}` must be a non-negative integer when provided.")
    if isinstance(value, float):
        value = int(value)
    if not isinstance(value, int) or value < 0 or value > MAX_TOKEN_COUNT:
        raise ToolExecutionError(f"`{key}` must be a non-negative integer when provided.")
    return value


def task_ttl_seconds(args: dict[str, Any]) -> int:
    try:
        ttl = int(args.get("ttl_seconds", DEFAULT_TASK_TTL_SECONDS))
    except (TypeError, ValueError):
        raise ToolExecutionError("`ttl_seconds` must be an integer.") from None
    if ttl < 60:
        raise ToolExecutionError("`ttl_seconds` must be at least 60.")
    return min(ttl, MAX_TASK_TTL_SECONDS)


def normalize_task_status(value: Any, *, default: str) -> str:
    if value is None:
        status = default
    elif isinstance(value, str):
        status = value.strip()
    else:
        raise ToolExecutionError("`status` must be a string when provided.")
    if status not in TASK_STATUSES:
        raise ToolExecutionError("`status` must be one of: " + ", ".join(sorted(TASK_STATUSES)) + ".")
    return status


def row_to_task(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    task = dict(row)
    task["is_stale"] = (
        task["status"] in ACTIVE_TASK_STATUSES
        and bool(task["expires_at"])
        and task["expires_at"] <= utc_now()
    )
    task["is_active"] = task["status"] in ACTIVE_TASK_STATUSES and not task["is_stale"]
    start_ts = parse_utc_timestamp(task.get("started_at"))
    if task["status"] in {"done", "abandoned"} and task.get("completed_at"):
        end_ts = parse_utc_timestamp(task["completed_at"])
        duration_state = "completed" if task["status"] == "done" else "abandoned"
    elif task["status"] == "flushed" and task.get("flushed_at"):
        end_ts = parse_utc_timestamp(task["flushed_at"])
        duration_state = "flushed"
    else:
        end_ts = parse_utc_timestamp(task.get("last_seen_at"))
        duration_state = "stale" if task["is_stale"] else "active"
    task["duration_seconds"] = int((end_ts - start_ts).total_seconds()) if start_ts and end_ts else None
    task["duration_state"] = duration_state
    return task


def normalize_model_registration_enum(
    args: dict[str, Any], key: str, allowed: set[str], *, aliases: dict[str, str] | None = None
) -> str:
    value = require_string(args, key, max_length=80).casefold()
    value = re.sub(r"[\s-]+", "_", value)
    value = aliases.get(value, value) if aliases else value
    if value not in allowed:
        raise ToolExecutionError(f"`{key}` must be one of: {', '.join(sorted(allowed))}.")
    return value


def normalize_model_registration_version(args: dict[str, Any]) -> str:
    value = require_string(args, "model_version", max_length=40)
    if not re.fullmatch(r"\d+(?:\.\d+){0,3}(?:[-_][A-Za-z0-9]+)?", value):
        raise ToolExecutionError(
            "`model_version` must be a numeric version such as `5.6` or `4.8`; "
            "do not embed the model family in this field."
        )
    return value


def normalize_model_registration_variant(args: dict[str, Any]) -> str:
    value = optional_string(args, "model_variant", max_length=40)
    if value and not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:[ _-][A-Za-z0-9]+)*", value):
        raise ToolExecutionError(
            "`model_variant` must be a name made of letters, numbers, spaces, underscores, or hyphens."
        )
    return " ".join(re.split(r"[ _-]+", value.title())) if value else ""


def describe_model_catalog() -> str:
    """Render the known-model catalog for tool schema descriptions."""
    parts = []
    for family, versions in MODEL_REGISTRATION_CATALOG.items():
        for version, variants in versions.items():
            entry = f"{family} {version}"
            if variants:
                entry += f" ({'/'.join(sorted(variants.values()))})"
            parts.append(entry)
    return "; ".join(parts)


def validate_registered_model(family: str, version: str, variant: str) -> tuple[str, str]:
    """Check version/variant against the known-model catalog for `family`.

    Returns the canonical `(version, variant)` spelling. Raises when the model is
    not in the catalog, so a mis-set field fails loudly at registration instead of
    fragmenting the dashboard's model buckets later.
    """
    known_versions = MODEL_REGISTRATION_CATALOG.get(family)
    if known_versions is None:
        return version, variant

    if version not in known_versions:
        if family == "gpt" and model_version_sort_key(version) < GPT_MINIMUM_VERSION:
            floor = ".".join(str(part) for part in GPT_MINIMUM_VERSION)
            raise ToolExecutionError(
                f"`model_version` `{version}` is retired for `model_family` `gpt`; "
                f"registration requires {floor} or newer. A bare major version such "
                "as `5` is the most common form of under-reporting, because it looks "
                "valid while hiding which release is actually running. State your "
                f"real version (one of: {', '.join(sorted(known_versions))}) and its "
                "variant."
            )
        raise ToolExecutionError(
            f"`model_version` `{version}` is not a known version for `model_family` "
            f"`{family}`; expected one of: {', '.join(sorted(known_versions))}. "
            "Report the model you are running as, not the client you run inside. "
            "If this model is genuinely new, add it to MODEL_REGISTRATION_CATALOG "
            "in mcp/lifecycle.py first."
        )

    known_variants = known_versions[version]
    if not variant:
        if family in MODEL_FAMILIES_REQUIRING_VARIANT and known_variants:
            raise ToolExecutionError(
                f"`model_variant` is required for `{family} {version}`; expected one "
                f"of: {', '.join(sorted(known_variants.values()))}. Report the variant "
                "you are actually running as -- omitting it makes distinct models share "
                "one label. The reasoning tier belongs in `reasoning_effort` and the "
                "client product name in `client_name`."
            )
        return version, ""

    canonical_variant = known_variants.get(variant.casefold())
    if canonical_variant is None:
        if not known_variants:
            raise ToolExecutionError(
                f"`{family} {version}` has no named variants, so `model_variant` must "
                f"be omitted; received `{variant}`. The reasoning tier belongs in "
                "`reasoning_effort` and the client product name in `client_name`."
            )
        raise ToolExecutionError(
            f"`model_variant` `{variant}` is not a known variant for `{family} "
            f"{version}`; expected one of: {', '.join(sorted(known_variants.values()))}, "
            "or omit it. The reasoning tier belongs in `reasoning_effort` and the "
            "client product name in `client_name`."
        )
    return version, canonical_variant


def model_registration_key(agent_id: str, session_hint: str) -> str:
    identity = f"{agent_id}\0{session_hint}".encode("utf-8")
    return MODEL_REGISTRATION_KEY_PREFIX + hashlib.sha256(identity).hexdigest()[:24]


def canonical_model_label(family: str, version: str, variant: str, reasoning_effort: str) -> str:
    if family == "gpt":
        identity = version
        if variant:
            identity += f" {variant}"
    else:
        identity = f"{MODEL_REGISTRATION_FAMILIES[family]} {version}"
        if variant:
            identity += f" {variant}"
    return f"{identity} {MODEL_REGISTRATION_EFFORTS[reasoning_effort]}"


class LifecycleService:
    """Own notes, events, model registrations, task state, and token metrics."""

    def __init__(
        self,
        store: Any,
        *,
        project: Any | None = None,
        guidance: Any | None = None,
        related_todo_guidance: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.guidance = guidance
        self.project = project or load_project_descriptor()
        self.related_todo_guidance = related_todo_guidance

    @property
    def repo_root(self) -> Any:
        return self.project.repo_root

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        with self.store.connection() as conn:
            yield conn

    def note_upsert(self, args: dict[str, Any]) -> dict[str, Any]:
        key = require_string(args, "key", max_length=160)
        value = require_string(args, "value", max_length=20_000)
        source = args.get("source", "agent")
        if not isinstance(source, str) or not source.strip():
            raise ToolExecutionError("`source` must be a non-empty string when provided.")
        source = source.strip()[:80]
        now = utc_now()
        with self.connection() as conn:
            conn.execute("""INSERT INTO notes(key, value, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, source=excluded.source,
                updated_at=excluded.updated_at""", (key, value, source, now, now))
            conn.commit()
            row = conn.execute("SELECT * FROM notes WHERE key = ?", (key,)).fetchone()
        return {"note": dict(row)}

    def note_read(self, args: dict[str, Any]) -> dict[str, Any]:
        key = require_string(args, "key", max_length=160)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM notes WHERE key = ?", (key,)).fetchone()
        return {"found": row is not None, "note": dict(row) if row else None}

    def note_search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query", "") or ""
        if not isinstance(query, str):
            raise ToolExecutionError("`query` must be a string when provided.")
        query = query.strip()
        limit = clamp_limit(args.get("limit"), default=10)
        with self.connection() as conn:
            if query:
                like = f"%{escape_like(query)}%"
                rows = conn.execute("""SELECT * FROM notes
                    WHERE key LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\' OR source LIKE ? ESCAPE '\\'
                    ORDER BY updated_at DESC LIMIT ?""", (like, like, like, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM notes ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return {"query": query, "notes": [dict(row) for row in rows]}

    def note_delete(self, args: dict[str, Any]) -> dict[str, Any]:
        key = require_string(args, "key", max_length=160)
        with self.connection() as conn:
            cursor = conn.execute("DELETE FROM notes WHERE key = ?", (key,))
            conn.commit()
        return {"deleted": cursor.rowcount > 0, "key": key}

    def event_log(self, args: dict[str, Any]) -> dict[str, Any]:
        event_type = require_string(args, "event_type", max_length=80)
        detail = require_string(args, "detail", max_length=20_000)
        actor = args.get("actor", "agent")
        if not isinstance(actor, str) or not actor.strip():
            raise ToolExecutionError("`actor` must be a non-empty string when provided.")
        now = utc_now()
        with self.connection() as conn:
            cursor = conn.execute("INSERT INTO events(ts, actor, event_type, detail) VALUES (?, ?, ?, ?)",
                                  (now, actor.strip()[:80], event_type, detail))
            conn.commit()
            row = conn.execute("SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return {"event": dict(row)}

    def event_recent(self, args: dict[str, Any]) -> dict[str, Any]:
        event_type = args.get("event_type")
        if event_type is not None and (not isinstance(event_type, str) or not event_type.strip()):
            raise ToolExecutionError("`event_type` must be a non-empty string when provided.")
        event_type = event_type.strip() if isinstance(event_type, str) else None
        limit = clamp_limit(args.get("limit"), default=10)
        with self.connection() as conn:
            if event_type:
                rows = conn.execute("SELECT * FROM events WHERE event_type = ? ORDER BY ts DESC LIMIT ?",
                                    (event_type, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return {"event_type": event_type, "events": [dict(row) for row in rows]}

    def insert_task_event(self, conn: sqlite3.Connection, *, task_state_id: int,
                          task_key: str, agent_id: str, action: str, status: str,
                          summary: str = "", detail: str = "") -> None:
        conn.execute("""INSERT INTO agent_task_events(
            task_state_id, task_key, agent_id, ts, action, status, summary, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                     (task_state_id, task_key, agent_id, utc_now(), action, status, summary, detail))

    @staticmethod
    def registration_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {"model_registration_key": row["registration_key"], "agent_id": row["agent_id"],
                "session_hint": row["session_hint"], "provider": row["provider"],
                "model_family": row["model_family"], "model_version": row["model_version"],
                "model_variant": row["model_variant"], "reasoning_effort": row["reasoning_effort"],
                "client_name": row["client_name"], "canonical_model_label": row["canonical_model_label"],
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    @staticmethod
    def normalize_registration_args(args: dict[str, Any]) -> dict[str, str]:
        provider = normalize_model_registration_enum(args, "provider", MODEL_REGISTRATION_PROVIDERS,
            aliases={"open_ai": "openai", "google_ai": "google", "claude": "anthropic"})
        family = normalize_model_registration_enum(args, "model_family", set(MODEL_REGISTRATION_FAMILIES),
            aliases={"gpt5": "gpt", "gpt_5": "gpt"})
        allowed = MODEL_REGISTRATION_FAMILY_PROVIDERS.get(family)
        if allowed and provider not in allowed:
            raise ToolExecutionError(f"`provider` `{provider}` contradicts `model_family` `{family}`; expected: {', '.join(sorted(allowed))}.")
        version = normalize_model_registration_version(args)
        variant = normalize_model_registration_variant(args)
        version, variant = validate_registered_model(family, version, variant)
        return {"provider": provider, "model_family": family,
                "model_version": version,
                "model_variant": variant,
                "reasoning_effort": normalize_model_registration_enum(args, "reasoning_effort",
                    set(MODEL_REGISTRATION_EFFORTS), aliases={"xhigh": "extra_high"}),
                "client_name": optional_string(args, "client_name", max_length=120),
                "session_hint": optional_string(args, "session_hint", max_length=160)}

    def resolve_model_registration(self, conn: sqlite3.Connection, registration_key: str,
                                   agent_id: str) -> sqlite3.Row | None:
        if not registration_key:
            return None
        row = conn.execute("SELECT * FROM agent_model_registrations WHERE registration_key = ?",
                           (registration_key,)).fetchone()
        if not row:
            raise ToolExecutionError(
                "`model_registration_key` does not refer to a current registration. "
                "Register the model again before using task lifecycle tools."
            )
        if row["agent_id"] != agent_id:
            raise ToolExecutionError("`model_registration_key` belongs to a different `agent_id`; model registrations cannot be shared across agent identities.")
        return row

    def resolve_task_model_identity(
        self,
        conn: sqlite3.Connection,
        *,
        agent_id: str,
        state: sqlite3.Row | None = None,
        requested_key: str = "",
    ) -> tuple[str, str]:
        """Validate a registration binding and return its task-local label snapshot."""
        bound_key = state["model_registration_key"] if state is not None else ""
        if requested_key and bound_key and requested_key != bound_key:
            raise ToolExecutionError(
                "`model_registration_key` does not match the registration bound to this "
                "task and agent. Start a new task key for a model change."
            )
        registration_key = requested_key or bound_key
        registration = self.resolve_model_registration(conn, registration_key, agent_id)
        if registration is None:
            return "", ""
        if bound_key:
            canonical_snapshot = state["canonical_model_label"]
            if not canonical_snapshot:
                raise ToolExecutionError(
                    "The registered task identity is missing its canonical label snapshot."
                )
            return registration_key, canonical_snapshot
        return registration_key, registration["canonical_model_label"]

    def agent_model_register(self, args: dict[str, Any]) -> dict[str, Any]:
        agent_id = require_string(args, "agent_id", max_length=160)
        normalized = self.normalize_registration_args(args)
        key = model_registration_key(agent_id, normalized["session_hint"])
        label = canonical_model_label(normalized["model_family"], normalized["model_version"],
                                      normalized["model_variant"], normalized["reasoning_effort"])
        now = utc_now()
        with self.connection() as conn:
            conn.execute("""INSERT INTO agent_model_registrations(
                registration_key, agent_id, session_hint, provider, model_family, model_version,
                model_variant, reasoning_effort, client_name, canonical_model_label, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, session_hint) DO UPDATE SET registration_key=excluded.registration_key,
                provider=excluded.provider, model_family=excluded.model_family, model_version=excluded.model_version,
                model_variant=excluded.model_variant, reasoning_effort=excluded.reasoning_effort,
                client_name=excluded.client_name, canonical_model_label=excluded.canonical_model_label,
                updated_at=excluded.updated_at""",
                         (key, agent_id, normalized["session_hint"], normalized["provider"], normalized["model_family"],
                          normalized["model_version"], normalized["model_variant"], normalized["reasoning_effort"],
                          normalized["client_name"], label, now, now))
            row = conn.execute("SELECT * FROM agent_model_registrations WHERE registration_key = ?", (key,)).fetchone()
            conn.commit()
        registration = self.registration_dict(row)
        return {"model_registration_key": key, "canonical_model_label": label, "registration": registration,
                **{name: registration[name] for name in ("provider", "model_family", "model_version",
                    "model_variant", "reasoning_effort", "client_name", "session_hint")}}

    def task_check_in(self, args: dict[str, Any]) -> dict[str, Any]:
        task_key = require_string(args, "task_key", max_length=160)
        agent_id = require_string(args, "agent_id", max_length=160)
        ttl = task_ttl_seconds(args)
        now = utc_now()
        with self.connection() as conn:
            existing = conn.execute("SELECT * FROM agent_task_state WHERE task_key = ? AND agent_id = ?",
                                    (task_key, agent_id)).fetchone()
            status = normalize_task_status(args.get("status"),
                default=existing["status"] if existing and existing["status"] in ACTIVE_TASK_STATUSES else "in_progress")
            if status not in ACTIVE_TASK_STATUSES:
                raise ToolExecutionError("`status` for check-in must be in_progress, paused, or blocked.")
            requested = optional_string(args, "model_registration_key", max_length=100)
            registration_key, canonical = self.resolve_task_model_identity(
                conn, agent_id=agent_id, state=existing, requested_key=requested
            )
            title = optional_string(args, "task_title", default=existing["task_title"] if existing else task_key, max_length=200) or task_key
            legacy_label = optional_string(args, "agent_label", default=existing["agent_label"] if existing else "", max_length=120)
            summary = optional_string(args, "summary", default=existing["summary"] if existing else "", max_length=4000)
            step = optional_string(args, "current_step", default=existing["current_step"] if existing else "", max_length=4000)
            workspace = optional_string(args, "workspace_root", default=existing["workspace_root"] if existing else str(self.repo_root), max_length=1000) or str(self.repo_root)
            conn.execute("""INSERT INTO agent_task_state(
                task_key, task_title, agent_id, agent_label, model_registration_key, canonical_model_label,
                status, summary, current_step, workspace_root, started_at, last_seen_at, expires_at,
                completed_at, flushed_at, flush_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                ON CONFLICT(task_key, agent_id) DO UPDATE SET task_title=excluded.task_title,
                agent_label=excluded.agent_label, model_registration_key=excluded.model_registration_key,
                canonical_model_label=excluded.canonical_model_label, status=excluded.status,
                summary=excluded.summary, current_step=excluded.current_step, workspace_root=excluded.workspace_root,
                last_seen_at=excluded.last_seen_at, expires_at=excluded.expires_at, completed_at=NULL,
                flushed_at=NULL, flush_id=NULL""",
                         (task_key, title, agent_id, canonical or legacy_label, registration_key, canonical, status,
                          summary, step, workspace, now, now, utc_after(ttl)))
            row = conn.execute("SELECT * FROM agent_task_state WHERE task_key = ? AND agent_id = ?",
                               (task_key, agent_id)).fetchone()
            self.insert_task_event(conn, task_state_id=row["id"], task_key=task_key, agent_id=agent_id,
                                   action="heartbeat" if existing else "check_in", status=status, summary=summary,
                                   detail=compact_json({"current_step": step, "ttl_seconds": ttl,
                                                        "model_registration_key": registration_key,
                                                        "canonical_model_label": canonical}))
            conn.commit()
        if bool_arg(args, "suppress_cascade", default=False) or self.related_todo_guidance is None:
            guidance = None
        else:
            guidance = self.related_todo_guidance({"source_task_key": task_key, "include_inferred": True, "limit": 8})
        return {"task": row_to_task(row), "related_todo_guidance": guidance}

    def task_check_out(self, args: dict[str, Any]) -> dict[str, Any]:
        task_key = require_string(args, "task_key", max_length=160)
        agent_id = require_string(args, "agent_id", max_length=160)
        status = normalize_task_status(args.get("status"), default="done")
        if status not in {"done", "abandoned", "paused", "blocked"}:
            raise ToolExecutionError("`status` for check-out must be done, abandoned, paused, or blocked.")
        now = utc_now()
        expires = now if status in {"done", "abandoned"} else utc_after(task_ttl_seconds(args))
        completed = now if status in {"done", "abandoned"} else None
        with self.connection() as conn:
            existing = conn.execute("SELECT * FROM agent_task_state WHERE task_key = ? AND agent_id = ?",
                                    (task_key, agent_id)).fetchone()
            if not existing:
                raise ToolExecutionError("No task state exists for this task_key and agent_id. Check in first.")
            self.resolve_task_model_identity(conn, agent_id=agent_id, state=existing)
            summary = optional_string(args, "summary", default=existing["summary"], max_length=4000)
            step = optional_string(args, "current_step", default=existing["current_step"], max_length=4000)
            detail = optional_string(args, "detail", default="", max_length=20_000)
            conn.execute("""UPDATE agent_task_state SET status=?, summary=?, current_step=?, last_seen_at=?,
                expires_at=?, completed_at=?, flushed_at=NULL, flush_id=NULL WHERE id=?""",
                         (status, summary, step, now, expires, completed, existing["id"]))
            row = conn.execute("SELECT * FROM agent_task_state WHERE id = ?", (existing["id"],)).fetchone()
            self.insert_task_event(conn, task_state_id=row["id"], task_key=task_key, agent_id=agent_id,
                                   action="check_out", status=status, summary=summary, detail=detail)
            conn.commit()
        return {"task": row_to_task(row)}

    def task_token_usage(self, args: dict[str, Any]) -> dict[str, Any]:
        task_key = require_string(args, "task_key", max_length=160)
        agent_id = require_string(args, "agent_id", max_length=160)
        input_tokens = optional_token_count(args, "input_tokens")
        output_tokens = optional_token_count(args, "output_tokens")
        total = optional_token_count(args, "total_tokens")
        if total is None:
            if input_tokens is None and output_tokens is None:
                raise ToolExecutionError("Provide `total_tokens`, or `input_tokens`/`output_tokens` so a total can be derived.")
            total = (input_tokens or 0) + (output_tokens or 0)
        descriptor = optional_string(args, "model_descriptor", default="", max_length=200)
        requested = optional_string(args, "model_registration_key", max_length=100)
        notes = optional_string(args, "notes", default="", max_length=4000)
        with self.connection() as conn:
            state = conn.execute("SELECT * FROM agent_task_state WHERE task_key = ? AND agent_id = ?",
                                 (task_key, agent_id)).fetchone()
            registration_key, canonical = self.resolve_task_model_identity(
                conn, agent_id=agent_id, state=state, requested_key=requested
            )
            model_descriptor = canonical or descriptor or (state["agent_label"] if state else "") or agent_id
            if descriptor and not registration_key and state and state["agent_label"] != descriptor:
                conn.execute("UPDATE agent_task_state SET agent_label=?, canonical_model_label='' WHERE id=?",
                             (descriptor, state["id"]))
            conn.execute("""INSERT INTO agent_task_token_usage(
                task_key, agent_id, ts, model_descriptor, model_registration_key, canonical_model_label,
                input_tokens, output_tokens, total_tokens, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_key, agent_id) DO UPDATE SET ts=excluded.ts, model_descriptor=excluded.model_descriptor,
                model_registration_key=excluded.model_registration_key, canonical_model_label=excluded.canonical_model_label,
                input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
                total_tokens=excluded.total_tokens, notes=excluded.notes""",
                         (task_key, agent_id, utc_now(), model_descriptor, registration_key, canonical,
                          input_tokens, output_tokens, total, notes))
            row = conn.execute("SELECT * FROM agent_task_token_usage WHERE task_key=? AND agent_id=?",
                               (task_key, agent_id)).fetchone()
            if state:
                self.insert_task_event(conn, task_state_id=state["id"], task_key=task_key, agent_id=agent_id,
                    action="token_usage", status=state["status"], summary=f"~{total} tokens reported",
                    detail=compact_json({"input_tokens": input_tokens, "output_tokens": output_tokens,
                                         "total_tokens": total, "model_descriptor": model_descriptor,
                                         "model_registration_key": registration_key,
                                         "canonical_model_label": canonical}))
            conn.commit()
        return {"token_usage": dict(row), "linked_task_state": state is not None}

    def task_token_recommendation(self, args: dict[str, Any]) -> dict[str, Any]:
        task_key = require_string(args, "task_key", max_length=160)
        agent_id = require_string(args, "agent_id", max_length=160)
        recommendation = require_string(args, "recommendation", max_length=4000)
        with self.connection() as conn:
            existing = conn.execute("SELECT * FROM agent_task_token_usage WHERE task_key=? AND agent_id=?",
                                    (task_key, agent_id)).fetchone()
            if not existing:
                raise ToolExecutionError("No token usage exists for this task_key and agent_id. Submit `mudra_task_token_usage` first.")
            now = utc_now()
            conn.execute("UPDATE agent_task_token_usage SET recommendation=?, recommendation_ts=? WHERE id=?",
                         (recommendation, now, existing["id"]))
            row = conn.execute("SELECT * FROM agent_task_token_usage WHERE id=?", (existing["id"],)).fetchone()
            state = conn.execute("SELECT * FROM agent_task_state WHERE task_key=? AND agent_id=?",
                                 (task_key, agent_id)).fetchone()
            if state:
                self.insert_task_event(conn, task_state_id=state["id"], task_key=task_key, agent_id=agent_id,
                    action="token_recommendation", status=state["status"], summary=recommendation[:200],
                    detail=compact_json({"recommendation": recommendation}))
            conn.commit()
        guidance = None
        if self.guidance is not None:
            guidance = self.guidance.add({
                "recommendation_key": args.get("recommendation_key"),
                "proposed_guidance": recommendation,
                "evidence": recommendation,
                "source_kind": "task_token",
                "source_ref": f"{task_key}:{agent_id}",
                "task_key": task_key,
                "agent_id": agent_id,
            })
        return {"token_usage": dict(row), "guidance": guidance}

    def update_recommendation_status(self, args: dict[str, Any]) -> dict[str, Any]:
        task_key = require_string(args, "task_key", max_length=160)
        agent_id = require_string(args, "agent_id", max_length=160)
        status = require_string(args, "status", max_length=20)
        if status not in {"open", "implemented"}:
            raise ToolExecutionError("status must be 'open' or 'implemented'")
        with self.connection() as conn:
            existing = conn.execute("SELECT * FROM agent_task_token_usage WHERE task_key=? AND agent_id=?",
                                    (task_key, agent_id)).fetchone()
            if not existing:
                raise ToolExecutionError("No token usage exists for this task_key and agent_id. Submit `mudra_task_token_usage` first.")
            if not (existing["recommendation"] or "").strip():
                raise ToolExecutionError("No recommendation to track for this task_key and agent_id.")
            now = utc_now()
            conn.execute("UPDATE agent_task_token_usage SET recommendation_status=?, recommendation_status_updated_at=? WHERE id=?",
                         (status, now, existing["id"]))
            row = conn.execute("SELECT * FROM agent_task_token_usage WHERE id=?", (existing["id"],)).fetchone()
            state = conn.execute("SELECT * FROM agent_task_state WHERE task_key=? AND agent_id=? ORDER BY started_at DESC",
                                 (task_key, agent_id)).fetchone()
            if state:
                self.insert_task_event(conn, task_state_id=state["id"], task_key=task_key, agent_id=agent_id,
                    action="recommendation_status", status=state["status"], summary=f"Recommendation marked as {status}",
                    detail=compact_json({"status": status}))
            conn.commit()
        return {"token_usage": dict(row)}

    def task_status(self, args: dict[str, Any]) -> dict[str, Any]:
        task_key = require_string(args, "task_key", max_length=160)
        include_events = bool_arg(args, "include_events", default=True)
        limit = clamp_limit(args.get("limit"), default=20)
        with self.connection() as conn:
            states = conn.execute("SELECT * FROM agent_task_state WHERE task_key=? ORDER BY last_seen_at DESC",
                                  (task_key,)).fetchall()
            events = conn.execute("SELECT * FROM agent_task_events WHERE task_key=? ORDER BY ts DESC, id DESC LIMIT ?",
                                  (task_key, limit)).fetchall() if include_events else []
        tasks = [row_to_task(row) for row in states]
        return {"task_key": task_key, "states": tasks,
                "active_count": sum(1 for task in tasks if task["is_active"]),
                "stale_count": sum(1 for task in tasks if task["is_stale"]),
                "events": [dict(row) for row in events]}

    def task_active(self, args: dict[str, Any]) -> dict[str, Any]:
        task_key = optional_string(args, "task_key", default="", max_length=160)
        include_stale = bool_arg(args, "include_stale", default=False)
        status = optional_string(args, "status", default="", max_length=40)
        if status and status not in ACTIVE_TASK_STATUSES:
            raise ToolExecutionError("`status` must be one of: " + ", ".join(sorted(ACTIVE_TASK_STATUSES)) + ".")
        search = optional_string(args, "search", default="", max_length=240).casefold()
        cursor = optional_string(args, "cursor", default="", max_length=256)
        limit = clamp_limit(args.get("limit"), default=TASK_ACTIVE_DEFAULT_LIMIT, maximum=TASK_ACTIVE_MAX_LIMIT)
        offset = decode_page_cursor(cursor, kind="active-tasks")
        sql = ["SELECT * FROM agent_task_state WHERE status IN ('in_progress', 'paused', 'blocked')"]
        params: list[Any] = []
        if not include_stale:
            sql.append("AND expires_at > ?")
            params.append(utc_now())
        if task_key:
            sql.append("AND task_key = ?")
            params.append(task_key)
        if status and not task_key:
            sql.append("AND status = ?")
            params.append(status)
        sql.append("ORDER BY last_seen_at DESC, id DESC")
        with self.connection() as conn:
            rows = conn.execute("\n".join(sql), params).fetchall()
        tasks = [row_to_task(row) for row in rows]
        if search and not task_key:
            tasks = [task for task in tasks if search in " ".join(str(task.get(field) or "")
                for field in ("task_key", "task_title", "agent_id", "agent_label", "summary", "current_step")).casefold()]
        total = len(tasks)
        page = tasks[offset:offset + limit]
        next_offset = offset + len(page)
        next_cursor = encode_page_cursor(next_offset, kind="active-tasks") if next_offset < total else None
        return {"task_key": task_key or None, "include_stale": include_stale, "status": status or None,
                "search": search or None, "cursor": cursor or None, "limit": limit, "tasks": page,
                "total": total, "has_more": next_cursor is not None, "next_cursor": next_cursor,
                "active_count": sum(1 for task in page if task["is_active"]),
                "stale_count": sum(1 for task in page if task["is_stale"])}

    def task_recent(self, *, limit: int = 50) -> dict[str, Any]:
        limit = clamp_limit(limit, default=50)
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM agent_task_state ORDER BY last_seen_at DESC, id DESC LIMIT ?",
                                (limit,)).fetchall()
        tasks = [row_to_task(row) for row in rows]
        return {"tasks": tasks, "active_count": sum(1 for task in tasks if task["is_active"]),
                "stale_count": sum(1 for task in tasks if task["is_stale"])}

    def active_task_keys(self) -> set[str]:
        with self.connection() as conn:
            rows = conn.execute("""SELECT DISTINCT task_key FROM agent_task_state
                WHERE status IN ('in_progress', 'paused', 'blocked') AND expires_at > ?""", (utc_now(),)).fetchall()
        return {row["task_key"].casefold() for row in rows if row["task_key"]}
