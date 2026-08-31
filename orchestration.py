"""Prune-triggered TODO orchestration and credential-safe Git checkpoints."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from mcp.lifecycle import ToolExecutionError


QUEUE_ACTIVE_STATUSES = {"queued", "dispatched", "claimed", "launching", "delegated", "recovery_required"}
QUEUE_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "stale", "dropped"}
QUEUE_STATUSES = QUEUE_ACTIVE_STATUSES | QUEUE_TERMINAL_STATUSES
# `dropped` is a soft-delete: it is terminal and auditable, but rows in this
# state are filtered out of the dashboard queue list so the owner can clear a
# cancelled/failed/stale row without forcing a retry while history is kept.
QUEUE_DROPPABLE_STATUSES = {"cancelled", "failed", "stale"}
CHECKPOINT_FAILURE_POLICIES = {"pause", "continue"}
CHECKPOINT_TERMINAL_STATUSES = {"success", "no_changes", "failed"}
# `client_local` is the default: Git runs in the supervisor's own checkout and
# the server only records bounded results. `server_local` is the legacy
# capability that lets the built-in `mudra_git_*` tools touch a checkout on this
# device, and it must be requested explicitly at designation time.
WORKSPACE_CAPABILITIES = {"client_local", "server_local"}
CHECKPOINT_RESULTS = {"success", "no_changes", "failed"}
CHECKPOINT_STAGES = {"commit", "push"}
ASSIGNMENT_STATUSES = {"active", "stopping", "cancelled", "stale", "stopped"}
MAX_WAIT_SECONDS = 30
MAX_GIT_OUTPUT_CHARS = 100_000
MAX_CHECKPOINT_EVIDENCE_CHARS = 2_000
DEFAULT_CLAIM_TTL_SECONDS = 900


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def stable_key(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def require_string(args: dict[str, Any], key: str, *, max_length: int = 500) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError(f"`{key}` must be a non-empty string.")
    value = value.strip()
    if len(value) > max_length:
        raise ToolExecutionError(f"`{key}` must be at most {max_length} characters.")
    return value


def optional_string(
    args: dict[str, Any], key: str, *, default: str = "", max_length: int = 20_000
) -> str:
    value = args.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ToolExecutionError(f"`{key}` must be a string when provided.")
    value = value.strip()
    if len(value) > max_length:
        raise ToolExecutionError(f"`{key}` must be at most {max_length} characters.")
    return value


def int_arg(
    args: dict[str, Any], key: str, *, default: int, minimum: int, maximum: int
) -> int:
    value = args.get(key, default)
    if isinstance(value, bool):
        raise ToolExecutionError(f"`{key}` must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ToolExecutionError(f"`{key}` must be an integer.") from None
    if parsed < minimum or parsed > maximum:
        raise ToolExecutionError(f"`{key}` must be between {minimum} and {maximum}.")
    return parsed


def bool_arg(args: dict[str, Any], key: str, *, default: bool) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise ToolExecutionError(f"`{key}` must be a boolean.")
    return value


def parse_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def lease_token_for(queue_key: str, attempt: int, claim_key: str) -> str:
    """Return an opaque token that can be recovered for an idempotent claim."""
    return stable_key("lease", queue_key, str(attempt), claim_key)


def lease_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def row_to_assignment(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["allowed_paths"] = parse_json(result.pop("allowed_paths_json", "[]"), [])
    # Existing databases may still contain the retired legacy notify column.
    # Keep that historical storage opaque to the current contract.
    result.pop("notify_waiting_since", None)
    result["checkpoint_required"] = bool(result.get("checkpoint_required"))
    result["workspace_capability"] = result.get("workspace_capability") or "client_local"
    result["is_stale"] = result.get("status") == "active" and str(result.get("expires_at") or "") <= utc_now()
    if result.get("status") == "active":
        result["lease_state"] = "stale" if result["is_stale"] else "healthy"
    else:
        result["lease_state"] = result.get("status") or "unknown"
    return result


def row_to_queue(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result.pop("claim_lease_token_hash", None)
    result["todo_snapshot"] = parse_json(result.pop("todo_snapshot_json", "{}"), {})
    result["checkpoint_required"] = bool(result.get("checkpoint_required"))
    result["cancellation_pending"] = bool(
        result.get("cancel_requested_at") and not result.get("cancel_acknowledged_at")
    )
    return result


def orchestration_tool_definitions() -> list[dict[str, Any]]:
    identity = {
        "assignment_key": {"type": "string"},
        "task_key": {"type": "string"},
        "agent_id": {"type": "string"},
    }
    queue_identity = {
        "queue_key": {"type": "string"},
        "task_key": {"type": "string"},
        "agent_id": {"type": "string"},
    }
    return [
        {
            "name": "mudra_orchestration_prompt",
            "title": "Render Mudra Orchestrator Bootstrap Prompt",
            "description": "Render the server-owned bootstrap prompt for a Codex or Claude Code orchestration supervisor session.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_designate",
            "title": "Designate Mudra Orchestrator",
            "description": "Bind an orchestrator task/agent to a registered canonical model, an opaque client-local workspace, and an explicit Git authorization allowlist. The server never resolves or accesses the client's local path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_key": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "model_registration_key": {"type": "string"},
                    "workspace_id": {
                        "type": "string",
                        "description": "Opaque repo-local workspace identity. The exclusive writer lease is keyed by (workspace_id, branch), so different workspaces may designate the same logical repository and branch concurrently.",
                    },
                    "repo_root": {
                        "type": "string",
                        "description": "Optional bounded client-local root, recorded as untrusted display metadata only. Legacy designations without workspace_id derive one from this value.",
                    },
                    "workspace_capability": {
                        "type": "string",
                        "enum": sorted(WORKSPACE_CAPABILITIES),
                        "default": "client_local",
                        "description": "`client_local` (default) executes Git in the supervisor's own checkout and records results through the checkpoint protocol. `server_local` additionally enables the legacy built-in mudra_git_* tools and requires a repo_root that resolves on this server.",
                    },
                    "branch": {"type": "string"},
                    "allowed_paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "checkpoint_required": {"type": "boolean", "default": True},
                    "checkpoint_failure_policy": {"type": "string", "enum": sorted(CHECKPOINT_FAILURE_POLICIES), "default": "pause"},
                    "ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 86400, "default": 3600},
                },
                "required": ["task_key", "agent_id", "model_registration_key", "branch", "allowed_paths"],
                "anyOf": [{"required": ["workspace_id"]}, {"required": ["repo_root"]}],
                "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_list",
            "title": "Inspect Mudra Orchestration Queue",
            "description": "Inspect orchestrator assignments, queue state, prompt snapshots, checkpoint outcomes, and failures.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": sorted(QUEUE_STATUSES)},
                    "assignment_key": {"type": "string"},
                    "todo_key": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_enqueue",
            "title": "Enqueue Mudra Todo",
            "description": "Serialize an open TODO and its server-rendered prompt before prune, bound to one designated orchestrator.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "todo_key": {"type": "string"},
                    "assignment_key": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                    "retry": {"type": "boolean", "default": False},
                },
                "required": ["todo_key", "assignment_key"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_heartbeat",
            "title": "Heartbeat Mudra Orchestrator",
            "description": "Refresh an exact orchestrator assignment without changing its model identity.",
            "inputSchema": {
                "type": "object", "properties": {
                    **identity,
                    "ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 86400, "default": 3600},
                }, "required": ["assignment_key", "task_key", "agent_id"], "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_wake",
            "title": "Wait For Mudra Orchestration Work",
            "description": "Poll or long-poll for one dispatched item assigned to the exact orchestrator identity.",
            "inputSchema": {
                "type": "object", "properties": {
                    **identity,
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_SECONDS, "default": 0},
                }, "required": ["assignment_key", "task_key", "agent_id"], "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_claim",
            "title": "Claim Mudra Orchestration Work",
            "description": "Atomically claim one dispatched prompt; an idempotent retry cannot create a second child handoff.",
            "inputSchema": {
                "type": "object", "properties": {
                    **queue_identity,
                    "idempotency_key": {"type": "string"},
                    "attempt": {"type": "integer", "minimum": 1},
                    "claim_ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 86400, "default": DEFAULT_CLAIM_TTL_SECONDS},
                }, "required": ["queue_key", "task_key", "agent_id", "idempotency_key"], "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_launch_prepare",
            "title": "Prepare Mudra Orchestration Launch",
            "description": "Persist an attempt-scoped launch intent before a provider call.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **queue_identity,
                    "attempt": {"type": "integer", "minimum": 1},
                    "lease_token": {"type": "string"},
                    "launch_key": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["queue_key", "task_key", "agent_id", "attempt", "lease_token"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_renew",
            "title": "Renew Mudra Orchestration Lease",
            "description": "Renew the claim lease for one exact attempt while it is being launched or monitored.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **queue_identity,
                    "attempt": {"type": "integer", "minimum": 1},
                    "lease_token": {"type": "string"},
                    "claim_ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 86400, "default": DEFAULT_CLAIM_TTL_SECONDS},
                },
                "required": ["queue_key", "task_key", "agent_id", "attempt", "lease_token"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_reconcile",
            "title": "Reconcile Mudra Orchestration Attempt",
            "description": "Return one exact restart action and optionally record safe child evidence for recovery-required work.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **identity,
                    "queue_key": {"type": "string"},
                    "resolution": {"type": "string", "enum": ["claimed", "delegated", "failed", "cancelled"]},
                    "child_session_id": {"type": "string"},
                    "child_outcome": {"type": "string"},
                    "terminal_evidence": {"type": "string"},
                },
                "required": ["assignment_key", "task_key", "agent_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_release",
            "title": "Release Mudra Orchestrator",
            "description": "Stop an assignment after all in-flight work has reached a safe terminal state.",
            "inputSchema": {
                "type": "object",
                "properties": {**identity, "force": {"type": "boolean", "default": False}},
                "required": ["assignment_key", "task_key", "agent_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_delegate",
            "title": "Record Mudra Delegation",
            "description": "Record the same-model child session and correlation outcome for a claimed prompt.",
            "inputSchema": {
                "type": "object", "properties": {
                    **queue_identity,
                    "child_session_id": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                    "attempt": {"type": "integer", "minimum": 1},
                    "lease_token": {"type": "string"},
                    "launch_key": {"type": "string"},
                    "accepted_model_registration_key": {"type": "string"},
                    "accepted_provider": {"type": "string"},
                    "accepted_model_family": {"type": "string"},
                    "accepted_model_version": {"type": "string"},
                    "accepted_model_variant": {"type": "string"},
                    "accepted_reasoning_effort": {"type": "string"},
                    "accepted_client_name": {"type": "string"},
                    "accepted_model": {
                        "type": "object",
                        "properties": {
                            "model_registration_key": {"type": "string"},
                            "provider": {"type": "string"},
                            "model_family": {"type": "string"},
                            "model_version": {"type": "string"},
                            "model_variant": {"type": "string"},
                            "reasoning_effort": {"type": "string"},
                            "client_name": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                }, "required": ["queue_key", "task_key", "agent_id", "child_session_id", "idempotency_key"], "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_complete",
            "title": "Complete Mudra Orchestration Work",
            "description": "Record a delegated child outcome and leave the queue checkpoint-gated before later dispatch.",
            "inputSchema": {
                "type": "object", "properties": {
                    **queue_identity,
                    "outcome": {"type": "string", "enum": ["completed", "failed"]},
                    "detail": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                }, "required": ["queue_key", "task_key", "agent_id", "outcome", "idempotency_key"], "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_checkpoint_prepare",
            "title": "Prepare Client Orchestration Checkpoint",
            "description": "Return a bounded checkpoint intent for execution in the designated client-local workspace. The server validates queue, attempt, and lease state but never inspects the client's Git state.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **queue_identity,
                    "workspace_id": {"type": "string"},
                    "attempt": {"type": "integer", "minimum": 1},
                    "lease_token": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["queue_key", "task_key", "agent_id", "workspace_id", "idempotency_key"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_checkpoint_record",
            "title": "Record Client Orchestration Checkpoint",
            "description": "Idempotently record the bounded outcome of a client-executed checkpoint. Record the `commit` stage before the `push` stage so a push failure resumes without creating a second commit. The server records the client's claim and never independently verifies client-local Git state.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **queue_identity,
                    "workspace_id": {"type": "string"},
                    "checkpoint_key": {"type": "string"},
                    "stage": {
                        "type": "string",
                        "enum": sorted(CHECKPOINT_STAGES),
                        "default": "push",
                        "description": "`commit` records a durable local commit that survives push retries; `push` records the terminal checkpoint outcome.",
                    },
                    "result": {"type": "string", "enum": sorted(CHECKPOINT_RESULTS)},
                    "commit_hash": {
                        "type": "string",
                        "description": "Client-reported commit hash, 7-64 hexadecimal characters.",
                    },
                    "push_target": {"type": "string", "maxLength": 500},
                    "evidence_summary": {
                        "type": "string",
                        "maxLength": MAX_CHECKPOINT_EVIDENCE_CHARS,
                        "description": "Bounded, redacted human-readable summary. Never send diffs, credentials, lease tokens, local paths, or shell commands.",
                    },
                    "idempotency_key": {"type": "string"},
                },
                "required": ["queue_key", "task_key", "agent_id", "workspace_id", "checkpoint_key", "result", "idempotency_key"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_retry",
            "title": "Retry Mudra Orchestration Work",
            "description": "Explicitly retry a failed, cancelled, or stale queue record with a new dispatch identity.",
            "inputSchema": {"type": "object", "properties": {"queue_key": {"type": "string"}, "idempotency_key": {"type": "string"}}, "required": ["queue_key", "idempotency_key"], "additionalProperties": False},
        },
        {
            "name": "mudra_orchestration_drop",
            "title": "Drop Mudra Orchestration Work",
            "description": "Soft-delete a cancelled, failed, or stale queue record so it is hidden from the queue list while its history is preserved. A completed row or a row with an unresolved cancellation cannot be dropped.",
            "inputSchema": {"type": "object", "properties": {"queue_key": {"type": "string"}, "reason": {"type": "string"}}, "required": ["queue_key"], "additionalProperties": False},
        },
        {
            "name": "mudra_orchestration_stop",
            "title": "Stop Mudra Orchestrator",
            "description": "Request an atomic cooperative stop, cancel queued work, and retain any possibly launched child until terminal evidence is acknowledged.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "assignment_key": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["assignment_key"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mudra_orchestration_cancel",
            "title": "Cancel Mudra Orchestration Work",
            "description": "Request cancellation; delegated children remain in flight until an explicit terminal acknowledgement.",
            "inputSchema": {"type": "object", "properties": {"queue_key": {"type": "string"}, "reason": {"type": "string"}}, "required": ["queue_key"], "additionalProperties": False},
        },
        {
            "name": "mudra_orchestration_cancel_ack",
            "title": "Acknowledge Mudra Child Cancellation",
            "description": "Record terminal child evidence for a cancellation request and release the next queue item only after acknowledgement.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **queue_identity,
                    "child_session_id": {"type": "string"},
                    "child_outcome": {"type": "string"},
                    "outcome": {"type": "string"},
                    "detail": {"type": "string"},
                    "terminal_evidence": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["queue_key", "task_key", "agent_id", "idempotency_key"],
                "additionalProperties": False,
            },
        },
        *[
            {
                "name": f"mudra_git_{operation}",
                "title": f"Mudra Git {operation.title()}",
                "description": description,
                "inputSchema": schema,
            }
            for operation, description, schema in [
                ("status", "Inspect the authorized working tree and reject unrelated dirty paths.", {"type": "object", "properties": queue_identity, "required": ["queue_key", "task_key", "agent_id"], "additionalProperties": False}),
                ("diff", "Read an authorized working-tree or staged diff without exposing credentials.", {"type": "object", "properties": {**queue_identity, "cached": {"type": "boolean", "default": False}}, "required": ["queue_key", "task_key", "agent_id"], "additionalProperties": False}),
                ("add", "Stage only explicitly authorized paths after confirming no unrelated dirty work.", {"type": "object", "properties": {**queue_identity, "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1}}, "required": ["queue_key", "task_key", "agent_id", "paths"], "additionalProperties": False}),
                ("commit", "Commit only authorized staged paths with an idempotent checkpoint key.", {"type": "object", "properties": {**queue_identity, "message": {"type": "string"}, "idempotency_key": {"type": "string"}}, "required": ["queue_key", "task_key", "agent_id", "message", "idempotency_key"], "additionalProperties": False}),
                ("checkpoint_noop", "Recheck the authorized branch and clean working tree, record no_changes, and release the next item.", {"type": "object", "properties": {**queue_identity, "idempotency_key": {"type": "string"}}, "required": ["queue_key", "task_key", "agent_id", "idempotency_key"], "additionalProperties": False}),
                ("push", "Push the recorded checkpoint through the existing Git credential helper and release the next item according to policy.", {"type": "object", "properties": {**queue_identity, "idempotency_key": {"type": "string"}, "remote": {"type": "string", "default": "origin"}}, "required": ["queue_key", "task_key", "agent_id", "idempotency_key"], "additionalProperties": False}),
            ]
        ],
    ]


class OrchestrationService:
    """Persist queue snapshots and coordinate explicit orchestrator handoffs."""

    def __init__(
        self,
        store: Any,
        *,
        lifecycle: Any,
        todos: Any,
        command_runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.store = store
        self.lifecycle = lifecycle
        self.todos = todos
        self.command_runner = command_runner

    @property
    def connection(self) -> Callable[[], Any]:
        return self.store.connection

    def _event(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row | dict[str, Any],
        event_type: str,
        idempotency_key: str,
        detail: str = "",
    ) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO mcp_orchestration_events(
                   queue_key, todo_key, ts, event_type, correlation_id, idempotency_key, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                row["queue_key"], row["todo_key"], utc_now(), event_type,
                row["correlation_id"], idempotency_key, detail,
            ),
        )

    def _assignment(self, conn: sqlite3.Connection, assignment_key: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM mcp_orchestrator_assignments WHERE assignment_key = ?",
            (assignment_key,),
        ).fetchone()
        if not row:
            raise ToolExecutionError(f"No orchestrator assignment found for `{assignment_key}`.")
        return row

    def _queue(self, conn: sqlite3.Connection, queue_key: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM mcp_orchestration_queue WHERE queue_key = ?", (queue_key,)
        ).fetchone()
        if not row:
            raise ToolExecutionError(f"No orchestration queue item found for `{queue_key}`.")
        return row

    def _verify_orchestrator(self, row: sqlite3.Row, args: dict[str, Any]) -> None:
        task_key = require_string(args, "task_key", max_length=160)
        agent_id = require_string(args, "agent_id", max_length=160)
        if row["orchestrator_task_key"] != task_key or row["orchestrator_agent_id"] != agent_id:
            raise ToolExecutionError("The queue item is assigned to a different orchestrator task/agent identity.")

    @staticmethod
    def _attempt(row: sqlite3.Row, args: dict[str, Any]) -> int:
        value = args.get("attempt", row["attempt"] if "attempt" in row.keys() else row["retry_count"] + 1)
        if isinstance(value, bool):
            raise ToolExecutionError("`attempt` must be a positive integer.")
        try:
            attempt = int(value)
        except (TypeError, ValueError):
            raise ToolExecutionError("`attempt` must be a positive integer.") from None
        if attempt < 1 or attempt != int(row["attempt"] or 1):
            raise ToolExecutionError("The attempt does not match the persisted queue attempt.")
        return attempt

    def _verify_lease(self, row: sqlite3.Row, args: dict[str, Any], *, required: bool = True) -> str:
        if "lease_token" not in args:
            if required:
                raise ToolExecutionError("An attempt-scoped `lease_token` is required.")
            return ""
        token = require_string(args, "lease_token", max_length=240)
        expected = str(row["claim_lease_token_hash"] or "")
        if not expected or lease_hash(token) != expected:
            raise ToolExecutionError("The claim lease token does not match this queue attempt.")
        if row["claim_expires_at"] and row["claim_expires_at"] <= utc_now():
            raise ToolExecutionError("The claim lease has expired; reconcile the attempt before continuing.")
        return token

    @staticmethod
    def _accepted_model(args: dict[str, Any]) -> dict[str, str]:
        nested = args.get("accepted_model")
        if nested is not None and not isinstance(nested, dict):
            raise ToolExecutionError("`accepted_model` must be an object when provided.")
        nested = nested or {}
        aliases = {
            "model_registration_key": ("accepted_model_registration_key", "model_registration_key"),
            "provider": ("accepted_provider",),
            "model_family": ("accepted_model_family",),
            "model_version": ("accepted_model_version",),
            "model_variant": ("accepted_model_variant",),
            "reasoning_effort": ("accepted_reasoning_effort",),
            "client_name": ("accepted_client_name",),
        }
        result: dict[str, str] = {}
        for key, candidates in aliases.items():
            value = nested.get(key)
            if value is None:
                for candidate in candidates:
                    if candidate in args:
                        value = args[candidate]
                        break
            if not isinstance(value, str) or not value.strip():
                raise ToolExecutionError(f"Accepted model field `{key}` is required.")
            result[key] = value.strip()
        return result

    @staticmethod
    def _model_snapshot(row: sqlite3.Row) -> dict[str, str]:
        return {
            "model_registration_key": row["model_registration_key"],
            "provider": row["provider"],
            "model_family": row["model_family"],
            "model_version": row["model_version"],
            "model_variant": row["model_variant"],
            "reasoning_effort": row["reasoning_effort"],
            "client_name": row["client_name"],
        }

    def _verify_accepted_model(self, row: sqlite3.Row, accepted: dict[str, str]) -> None:
        expected = self._model_snapshot(row)
        mismatches = [key for key, value in accepted.items() if expected.get(key, "") != value]
        if mismatches:
            raise ToolExecutionError(
                "Accepted model does not match the immutable queue snapshot: " + ", ".join(mismatches)
            )

    def _verify_assignment_identity(self, row: sqlite3.Row, args: dict[str, Any]) -> None:
        if row["assignment_key"] != require_string(args, "assignment_key", max_length=80):
            raise ToolExecutionError("The assignment key does not match.")
        if row["task_key"] != require_string(args, "task_key", max_length=160) or row[
            "agent_id"
        ] != require_string(args, "agent_id", max_length=160):
            raise ToolExecutionError("The assignment belongs to a different orchestrator task/agent identity.")

    @staticmethod
    def _normalize_paths(values: Any) -> list[str]:
        if not isinstance(values, list) or not values:
            raise ToolExecutionError("`allowed_paths` must be a non-empty array of relative repository paths.")
        normalized: list[str] = []
        for raw in values:
            if not isinstance(raw, str) or not raw.strip():
                raise ToolExecutionError("Authorized paths must be non-empty strings.")
            path = PurePosixPath(raw.strip().replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
                raise ToolExecutionError("Authorized paths must be specific relative paths without `..`.")
            value = str(path)
            if value not in normalized:
                normalized.append(value)
        return normalized

    @staticmethod
    def _path_allowed(path: str, allowed_paths: Sequence[str]) -> bool:
        normalized = str(PurePosixPath(path.replace("\\", "/")))
        return any(normalized == root or normalized.startswith(root.rstrip("/") + "/") for root in allowed_paths)

    def orchestration_prompt(self, _args: dict[str, Any] | None = None) -> dict[str, Any]:
        prompt = """Act as the long-running Mudra orchestration supervisor for this repository.

Use the model currently selected for this Codex or Claude Code VSCode extension session. Do not invent, infer, or accept a free-form display label. First determine your actual provider, model family, numeric version, optional variant, reasoning effort, and client name from the session context. Create one stable agent_id for this supervisor session and use task_key `mudra-orchestration-supervisor`.

Use the canonical MCP JSON-RPC endpoint `https://mcp.nml.wtf/mcp`. Configure the MCP client transport to map environment variables `CF_CLIENT_ID` and `CF_CLIENT_SECRET` to headers `CF-Access-Client-Id` and `CF-Access-Client-Secret`; never print, paste, persist, or commit those values. Prefer the exposed `mudra_*` MCP tools. If they are unavailable, use the checked-in `python -m mcp.rpc call <tool> --args-file -` CLI, which resolves the remote endpoint and headers. Do not hand-roll JSON-RPC or edit mcp.db.

Startup contract:
1. Call `mudra_agent_model_register` with your stable agent_id and the actual provider, model_family, model_version, model_variant (required for the gpt family, otherwise when present), reasoning_effort, and client_name. Retain the returned model_registration_key and canonical_model_label; never derive the label yourself.
2. Resolve the repo-local workspace identity from `<git-dir>/mudra-orchestrator-workspace-id` (create a UUID there atomically when absent), the exact checked-out branch, and the client-local root display value. Call `mudra_orchestration_designate` with task_key, agent_id, the returned model_registration_key, workspace_id, repo_root, branch, explicit allowed_paths `["mcp","mudra","mudra-gateway","android","ios","firmware","hardware","matlab","specs","docs","scripts","config.yaml","AGENTS.md"]`, checkpoint_required true, checkpoint_failure_policy `pause`, and ttl_seconds 3600. Retain assignment_key. The MCP server does not resolve or access the client path. The exclusive writer lease is keyed by (workspace_id, branch), so a supervisor in a different checkout may hold its own lease on the same repository and branch; a rejection naming your own workspace_id means another live assignment already owns this checkout.
3. Call `mudra_task_check_in` for the supervisor task using the same registration key. Call `mudra_orchestration_reconcile` before accepting new work.

Supervisor loop:
- Maintain `mudra_orchestration_heartbeat` independently of child monitoring. Poll `mudra_orchestration_wake` with assignment_key, task_key, and agent_id.
- For action `dispatch`, call `mudra_orchestration_claim` exactly once with a stable attempt-scoped idempotency key; persist/retain the returned lease token; call `mudra_orchestration_launch_prepare` before any provider call; create or reattach exactly one child through a supported session adapter using prompt_snapshot unchanged and the queue's exact registered provider/model/version/variant/reasoning/client snapshot; then call `mudra_orchestration_delegate`. Renew the claim lease through `mudra_orchestration_renew` while launching or monitoring. Complete with stable idempotency through `mudra_orchestration_complete`, call `mudra_orchestration_checkpoint_prepare`, execute the returned intent through a client-local checkpoint adapter, and record only its bounded result through `mudra_orchestration_checkpoint_record`.
- On restart or ambiguous provider state, call `mudra_orchestration_reconcile`; never launch a second child while recovery_required or while terminal child evidence is unknown.
- For action `cancel`, use the supported provider/session adapter to cancel or inspect the correlated child or launch key. Call `mudra_orchestration_cancel_ack` only after terminal evidence proves the child can no longer write. A VSCode extension without a force-terminate API supports cooperative shutdown only; do not claim it was killed without adapter evidence.
- For action `stop`, stop dispatching and heartbeats. If cancellation remains pending, settle it as above. When no unresolved queue row may still write, call `mudra_orchestration_release`, check out the supervisor task, and exit. Do not retry or dispatch work after a stop request.

Preserve all assignment, queue, event, idempotency, and checkpoint history. Never expose assignment tokens, claim lease tokens, Cloudflare credentials, or provider credentials in logs, prompts, dashboard fields, or child handoffs."""
        return {
            "prompt": prompt,
            "endpoint": "https://mcp.nml.wtf/mcp",
            "supported_clients": ["Codex", "Claude Code"],
            "requires_model_registration": True,
        }

    def orchestration_designate(self, args: dict[str, Any]) -> dict[str, Any]:
        task_key = require_string(args, "task_key", max_length=160)
        agent_id = require_string(args, "agent_id", max_length=160)
        registration_key = require_string(args, "model_registration_key", max_length=100)
        repo_root = optional_string(args, "repo_root", max_length=1000)
        workspace_id = optional_string(args, "workspace_id", max_length=100)
        if not workspace_id:
            if not repo_root:
                raise ToolExecutionError("`workspace_id` is required for client-local designation.")
            workspace_id = stable_key("legacy_workspace", repo_root)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,99}", workspace_id):
            raise ToolExecutionError("`workspace_id` must be an opaque 8-100 character identifier.")
        capability = optional_string(
            args, "workspace_capability", default="client_local", max_length=20
        ) or "client_local"
        if capability not in WORKSPACE_CAPABILITIES:
            raise ToolExecutionError(
                "`workspace_capability` must be `client_local` or `server_local`."
            )
        if capability == "server_local" and not repo_root:
            raise ToolExecutionError(
                "`server_local` designation requires a `repo_root` that resolves on this server."
            )
        # This is bounded client-supplied display metadata. The central server
        # never resolves or accesses a client's local filesystem path.
        branch = require_string(args, "branch", max_length=240)
        allowed_paths = self._normalize_paths(args.get("allowed_paths"))
        checkpoint_required = bool_arg(args, "checkpoint_required", default=True)
        failure_policy = optional_string(
            args, "checkpoint_failure_policy", default="pause", max_length=20
        ) or "pause"
        if failure_policy not in CHECKPOINT_FAILURE_POLICIES:
            raise ToolExecutionError("`checkpoint_failure_policy` must be `pause` or `continue`.")
        ttl_seconds = int_arg(args, "ttl_seconds", default=3600, minimum=60, maximum=86400)
        assignment_key = stable_key("oa", task_key, agent_id)
        now = utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            registration = self.lifecycle.resolve_model_registration(conn, registration_key, agent_id)
            if registration is None:
                raise ToolExecutionError("A registered canonical model identity is required.")
            conflict = conn.execute(
                """SELECT assignment_key, task_key, agent_id
                   FROM mcp_orchestrator_assignments
                   WHERE workspace_id=? AND branch=? AND assignment_key<>?
                     AND (status='stopping' OR (status='active' AND expires_at>?)) LIMIT 1""",
                (workspace_id, branch, assignment_key, now),
            ).fetchone()
            if conflict:
                # Same-workspace conflict only. A different workspace_id may hold
                # its own writer lease on the same logical repository and branch.
                raise ToolExecutionError(
                    "Workspace/branch writer lease is already held by "
                    f"assignment `{conflict['assignment_key']}` "
                    f"({conflict['task_key']}/{conflict['agent_id']}).",
                    {
                        "reason": "workspace_branch_writer_conflict",
                        "workspace_id": workspace_id,
                        "branch": branch,
                        "held_by": {
                            "assignment_key": conflict["assignment_key"],
                            "task_key": conflict["task_key"],
                            "agent_id": conflict["agent_id"],
                        },
                        "resolution": (
                            "Release or expire the existing assignment, or designate "
                            "from a different workspace_id."
                        ),
                    },
                )
            conn.execute(
                """INSERT INTO mcp_orchestrator_assignments(
                       assignment_key, task_key, agent_id, model_registration_key,
                       provider, model_family, model_version, model_variant,
                       reasoning_effort, canonical_model_label, client_name, status,
                       heartbeat_at, expires_at, repo_root, workspace_id, workspace_capability,
                       branch, allowed_paths_json,
                       checkpoint_required, checkpoint_failure_policy, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task_key, agent_id) DO UPDATE SET
                       model_registration_key=excluded.model_registration_key,
                       provider=excluded.provider, model_family=excluded.model_family,
                       model_version=excluded.model_version, model_variant=excluded.model_variant,
                       reasoning_effort=excluded.reasoning_effort,
                       canonical_model_label=excluded.canonical_model_label,
                       client_name=excluded.client_name, status='active',
                       heartbeat_at=excluded.heartbeat_at, expires_at=excluded.expires_at,
                       stop_requested_at=NULL, stop_acknowledged_at=NULL,
                       stop_reason='', stop_idempotency_key='', stop_terminal_evidence='',
                       repo_root=excluded.repo_root, workspace_id=excluded.workspace_id,
                       workspace_capability=excluded.workspace_capability,
                       branch=excluded.branch,
                       allowed_paths_json=excluded.allowed_paths_json,
                       checkpoint_required=excluded.checkpoint_required,
                       checkpoint_failure_policy=excluded.checkpoint_failure_policy,
                       updated_at=excluded.updated_at""",
                (
                    assignment_key, task_key, agent_id, registration_key,
                    registration["provider"], registration["model_family"],
                    registration["model_version"], registration["model_variant"],
                    registration["reasoning_effort"], registration["canonical_model_label"],
                    registration["client_name"], now, utc_after(ttl_seconds), repo_root,
                    workspace_id, capability, branch,
                    json.dumps(allowed_paths, separators=(",", ":")),
                    int(checkpoint_required), failure_policy, now, now,
                ),
            )
            row = self._assignment(conn, assignment_key)
            conn.commit()
        return {"assignment": row_to_assignment(row)}

    def orchestration_heartbeat(self, args: dict[str, Any]) -> dict[str, Any]:
        ttl_seconds = int_arg(args, "ttl_seconds", default=3600, minimum=60, maximum=86400)
        now = utc_now()
        with self.connection() as conn:
            row = self._assignment(conn, require_string(args, "assignment_key", max_length=80))
            self._verify_assignment_identity(row, args)
            if row["status"] != "active":
                raise ToolExecutionError(
                    f"The orchestrator assignment is {row['status']}; heartbeat cannot reactivate it."
                )
            conn.execute(
                "UPDATE mcp_orchestrator_assignments SET status='active', heartbeat_at=?, expires_at=?, updated_at=? WHERE assignment_key=?",
                (now, utc_after(ttl_seconds), now, row["assignment_key"]),
            )
            updated = self._assignment(conn, row["assignment_key"])
            conn.commit()
        return {"assignment": row_to_assignment(updated)}

    def _snapshot_todo(self, todo: dict[str, Any]) -> dict[str, Any]:
        fields = [
            "todo_key", "title", "detail", "app_scope", "priority", "status",
            "source", "source_task_key", "planned_complexity", "actual_complexity",
            "tags", "code_paths", "symbol_refs", "doc_keys", "route_refs",
            "test_refs", "search_queries", "created_at", "updated_at",
        ]
        return {key: todo.get(key) for key in fields}

    def orchestration_enqueue(self, args: dict[str, Any]) -> dict[str, Any]:
        todo_key = require_string(args, "todo_key", max_length=160)
        assignment_key = require_string(args, "assignment_key", max_length=80)
        retry = bool_arg(args, "retry", default=False)
        requested_idempotency = optional_string(args, "idempotency_key", max_length=160)
        with self.connection() as conn:
            assignment = self._assignment(conn, assignment_key)
            if assignment["status"] != "active" or assignment["expires_at"] <= utc_now():
                raise ToolExecutionError("The designated orchestrator is not active; heartbeat or designate it again.")
            todo_result = self.todos.todo_get(todo_key, include_events=False)
            if not todo_result["found"]:
                raise ToolExecutionError(f"No todo found for `{todo_key}`.")
            todo = todo_result["todo"]
            if todo.get("status") in {"done", "dropped"}:
                raise ToolExecutionError("Only an open TODO can be queued before prune.")
            existing_active = conn.execute(
                "SELECT * FROM mcp_orchestration_queue WHERE todo_key=? AND status IN ('queued','dispatched','claimed','launching','delegated','recovery_required') ORDER BY enqueued_at DESC LIMIT 1",
                (todo_key,),
            ).fetchone()
            if existing_active and not retry:
                return {"created": False, "duplicate": True, "queue_item": row_to_queue(existing_active)}
            if existing_active:
                raise ToolExecutionError("The TODO already has active orchestration work; retry is available only after a terminal queue state.")
            terminal = conn.execute(
                "SELECT * FROM mcp_orchestration_queue WHERE todo_key=? ORDER BY enqueued_at DESC LIMIT 1",
                (todo_key,),
            ).fetchone()
            if terminal and not retry:
                raise ToolExecutionError("This TODO has prior orchestration history; pass `retry: true` to enqueue it explicitly.")
            idempotency_key = requested_idempotency or stable_key(
                "enqueue", todo_key, assignment_key, str((terminal["retry_count"] + 1) if terminal else 0)
            )
            prior = conn.execute(
                "SELECT * FROM mcp_orchestration_queue WHERE enqueue_idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if prior:
                return {"created": False, "duplicate": True, "queue_item": row_to_queue(prior)}
            related = self.todos.todo_related_for_assignment(
                {"todo_key": todo_key, "include_inferred": True, "limit": 12}
            )
            prompt = self.todos.render_todo_instruction(
                [todo], app_scope=todo.get("app_scope") or "", related_guidance=related
            )
            now = utc_now()
            position = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM mcp_orchestration_queue"
            ).fetchone()[0]
            retry_count = (terminal["retry_count"] + 1) if terminal else 0
            queue_key = stable_key("oq", todo_key, assignment_key, idempotency_key)
            correlation_id = stable_key("corr", queue_key, str(retry_count))
            dispatch_idempotency = stable_key("dispatch", queue_key, str(retry_count))
            snapshot = self._snapshot_todo(todo)
            conn.execute(
                """INSERT INTO mcp_orchestration_queue(
                       queue_key, todo_key, status, position, todo_snapshot_json,
                       prompt_snapshot, assignment_key, orchestrator_task_key,
                       orchestrator_agent_id, model_registration_key, provider,
                       model_family, model_version, model_variant, reasoning_effort,
                       canonical_model_label, client_name, writer_assignment_key,
                       writer_repo_root, writer_workspace_id, writer_branch, enqueue_idempotency_key,
                       dispatch_idempotency_key, correlation_id, retry_count,
                       attempt, checkpoint_required, checkpoint_failure_policy, checkpoint_status,
                       enqueued_at, updated_at)
                   VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    queue_key, todo_key, position,
                    json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), prompt,
                    assignment_key, assignment["task_key"], assignment["agent_id"],
                    assignment["model_registration_key"], assignment["provider"],
                    assignment["model_family"], assignment["model_version"],
                    assignment["model_variant"], assignment["reasoning_effort"],
                    assignment["canonical_model_label"], assignment["client_name"],
                    assignment["assignment_key"], assignment["repo_root"],
                    assignment["workspace_id"], assignment["branch"],
                    idempotency_key, dispatch_idempotency, correlation_id, retry_count,
                    retry_count + 1,
                    assignment["checkpoint_required"], assignment["checkpoint_failure_policy"],
                    "pending" if assignment["checkpoint_required"] else "not_required", now, now,
                ),
            )
            row = self._queue(conn, queue_key)
            self._event(conn, row, "enqueued", idempotency_key, "TODO and prompt snapshot persisted before prune.")
            # Mark the source todo `queued` so the dashboard greys it and the
            # wake-ping action can gate on queue membership while it waits.
            self.todos.mark_todo_queued(conn, todo_key)
            conn.commit()
        return {"created": True, "duplicate": False, "queue_item": row_to_queue(row)}

    def _checkpoint_blocks_dispatch(self, conn: sqlite3.Connection, assignment_key: str) -> bool:
        row = conn.execute(
            """SELECT checkpoint_required, checkpoint_failure_policy, checkpoint_status
               FROM mcp_orchestration_queue
               WHERE assignment_key=? AND status='completed'
               ORDER BY completed_at DESC, position DESC LIMIT 1""",
            (assignment_key,),
        ).fetchone()
        if not row or not row["checkpoint_required"]:
            return False
        if row["checkpoint_status"] in CHECKPOINT_TERMINAL_STATUSES:
            return row["checkpoint_status"] == "failed" and row["checkpoint_failure_policy"] == "pause"
        return True

    def _dispatch_next(self, conn: sqlite3.Connection, assignment_key: str) -> dict[str, Any] | None:
        in_flight = conn.execute(
            "SELECT 1 FROM mcp_orchestration_queue WHERE assignment_key=? AND status IN ('dispatched','claimed','launching','delegated','recovery_required') LIMIT 1",
            (assignment_key,),
        ).fetchone()
        if in_flight:
            return None
        if self._checkpoint_blocks_dispatch(conn, assignment_key):
            return None
        now = utc_now()
        assignment = self._assignment(conn, assignment_key)
        if assignment["status"] != "active" or assignment["expires_at"] <= now:
            return None
        row = conn.execute(
            """SELECT q.* FROM mcp_orchestration_queue q
               JOIN mcp_todos t ON t.todo_key=q.todo_key
               WHERE q.assignment_key=? AND q.status='queued'
                 AND t.status IN ('done','dropped')
               ORDER BY q.position, q.enqueued_at LIMIT 1""",
            (assignment_key,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE mcp_orchestration_queue SET status='dispatched', dispatched_at=COALESCE(dispatched_at, ?), updated_at=?, error='' WHERE queue_key=? AND status='queued'",
            (now, now, row["queue_key"]),
        )
        updated = self._queue(conn, row["queue_key"])
        self._event(
            conn, updated, "dispatched", updated["dispatch_idempotency_key"],
            "Pending work is available through mudra_orchestration_wake; no browser process was launched.",
        )
        return row_to_queue(updated)

    def dispatch_for_pruned_todo(
        self, conn: sqlite3.Connection, *, todo_key: str
    ) -> dict[str, Any] | None:
        rows = conn.execute(
            "SELECT * FROM mcp_orchestration_queue WHERE todo_key=? AND status='queued' ORDER BY position",
            (todo_key,),
        ).fetchall()
        if not rows:
            return None
        first = rows[0]
        assignment = self._assignment(conn, first["assignment_key"])
        if assignment["status"] != "active" or assignment["expires_at"] <= utc_now():
            now = utc_now()
            conn.execute(
                "UPDATE mcp_orchestration_queue SET status='stale', error='Orchestrator assignment expired before dispatch.', updated_at=? WHERE queue_key=? AND status='queued'",
                (now, first["queue_key"]),
            )
            updated = self._queue(conn, first["queue_key"])
            self._event(conn, updated, "stale", updated["dispatch_idempotency_key"], updated["error"])
            return row_to_queue(updated)
        dispatched = self._dispatch_next(conn, first["assignment_key"])
        if dispatched:
            return dispatched
        return row_to_queue(self._queue(conn, first["queue_key"]))

    def _sweep_stale(self, conn: sqlite3.Connection) -> None:
        now = utc_now()
        expired = conn.execute(
            """SELECT q.* FROM mcp_orchestration_queue q
               JOIN mcp_orchestrator_assignments a ON a.assignment_key=q.assignment_key
               WHERE q.status IN ('dispatched','claimed','launching','delegated','recovery_required')
                 AND NOT (q.cancel_requested_at IS NOT NULL AND q.cancel_acknowledged_at IS NULL)
                 AND (a.status!='active' OR a.expires_at<=? OR
                      (q.claim_expires_at IS NOT NULL AND q.claim_expires_at<=?))""",
            (now, now),
        ).fetchall()
        for row in expired:
            next_status = "stale" if row["status"] == "dispatched" else "recovery_required"
            reason = (
                "Orchestrator assignment expired before a child lease was issued."
                if next_status == "stale"
                else "Lease or assignment expired while a child may exist; reconciliation is required."
            )
            conn.execute(
                "UPDATE mcp_orchestration_queue SET status=?, recovery_reason=?, error=?, updated_at=? WHERE queue_key=?",
                (next_status, reason, reason, now, row["queue_key"]),
            )
            updated = self._queue(conn, row["queue_key"])
            self._event(conn, updated, next_status, stable_key(next_status, row["queue_key"], row["assignment_key"]), updated["error"])

    def orchestration_list(self, args: dict[str, Any]) -> dict[str, Any]:
        status = optional_string(args, "status", max_length=30)
        if status and status not in QUEUE_STATUSES:
            raise ToolExecutionError("Unknown orchestration queue status.")
        assignment_key = optional_string(args, "assignment_key", max_length=80)
        todo_key = optional_string(args, "todo_key", max_length=160)
        limit = int_arg(args, "limit", default=50, minimum=1, maximum=200)
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("status=?")
            values.append(status)
        else:
            # Soft-deleted rows are auditable only through an explicit
            # status='dropped' filter; the default list hides them.
            clauses.append("status!='dropped'")
        if assignment_key:
            clauses.append("assignment_key=?")
            values.append(assignment_key)
        if todo_key:
            clauses.append("todo_key=?")
            values.append(todo_key)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as conn:
            self._sweep_stale(conn)
            rows = conn.execute(
                f"SELECT * FROM mcp_orchestration_queue{where} ORDER BY position, enqueued_at LIMIT ?",
                (*values, limit),
            ).fetchall()
            assignments = conn.execute(
                "SELECT * FROM mcp_orchestrator_assignments ORDER BY updated_at DESC"
            ).fetchall()
            conn.commit()
        queue_payload = [row_to_queue(row) for row in rows]
        assignment_payload = [row_to_assignment(row) for row in assignments]
        for assignment in assignment_payload:
            assigned_rows = [
                item for item in queue_payload
                if item.get("assignment_key") == assignment["assignment_key"]
            ]
            assignment["recovery_required"] = any(
                item.get("status") == "recovery_required" for item in assigned_rows
            )
            assignment["cancellation_pending_count"] = sum(
                1 for item in assigned_rows if item.get("cancellation_pending")
            )
        return {
            "queue": queue_payload,
            "assignments": assignment_payload,
            "states": sorted(QUEUE_STATUSES),
        }

    def orchestration_wake(self, args: dict[str, Any]) -> dict[str, Any]:
        wait_seconds = int_arg(args, "wait_seconds", default=0, minimum=0, maximum=MAX_WAIT_SECONDS)
        deadline = time.monotonic() + wait_seconds
        while True:
            with self.connection() as conn:
                assignment = self._assignment(conn, require_string(args, "assignment_key", max_length=80))
                self._verify_assignment_identity(assignment, args)
                self._sweep_stale(conn)
                row = conn.execute(
                    """SELECT * FROM mcp_orchestration_queue
                       WHERE assignment_key=?
                         AND (status='dispatched'
                              OR (status IN ('claimed','launching','delegated','recovery_required')
                                  AND cancel_requested_at IS NOT NULL
                                  AND cancel_acknowledged_at IS NULL))
                       ORDER BY CASE WHEN cancel_requested_at IS NOT NULL
                                      AND cancel_acknowledged_at IS NULL THEN 0 ELSE 1 END,
                                position LIMIT 1""",
                    (assignment["assignment_key"],),
                ).fetchone()
                conn.commit()
            if row:
                action = (
                    "cancel"
                    if row["cancel_requested_at"] and not row["cancel_acknowledged_at"]
                    else "dispatch"
                )
                return {
                    "pending": True,
                    "action": action,
                    "attempt": int(row["attempt"] or 1),
                    "launch_key": row["launch_key"] or "",
                    "queue_item": row_to_queue(row),
                }
            if assignment["status"] in {"stopping", "stopped"}:
                return {
                    "pending": False,
                    "action": "stop",
                    "attempt": None,
                    "queue_item": None,
                    "assignment": row_to_assignment(assignment),
                }
            if time.monotonic() >= deadline:
                return {"pending": False, "action": "idle", "attempt": None, "queue_item": None}
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def orchestration_claim(self, args: dict[str, Any]) -> dict[str, Any]:
        queue_key = require_string(args, "queue_key", max_length=80)
        idempotency = require_string(args, "idempotency_key", max_length=160)
        claim_ttl = int_arg(
            args, "claim_ttl_seconds", default=DEFAULT_CLAIM_TTL_SECONDS, minimum=60, maximum=86400
        )
        with self.connection() as conn:
            row = self._queue(conn, queue_key)
            self._verify_orchestrator(row, args)
            attempt = self._attempt(row, args)
            token = lease_token_for(queue_key, attempt, idempotency)
            if row["claim_idempotency_key"] == idempotency and row["status"] in {"claimed", "launching", "delegated", "completed", "failed"}:
                return {
                    "claimed": True,
                    "duplicate": True,
                    "attempt": attempt,
                    "lease_token": token,
                    "claim_expires_at": row["claim_expires_at"],
                    "queue_item": row_to_queue(row),
                }
            if row["status"] != "dispatched":
                raise ToolExecutionError(f"Queue item `{queue_key}` is {row['status']}, not dispatched.")
            now = utc_now()
            conn.execute(
                """UPDATE mcp_orchestration_queue
                   SET status='claimed', claim_idempotency_key=?, attempt=?,
                       claim_lease_token_hash=?, claim_expires_at=?, claimed_at=?, updated_at=?
                   WHERE queue_key=? AND status='dispatched'""",
                (idempotency, attempt, lease_hash(token), utc_after(claim_ttl), now, now, queue_key),
            )
            updated = self._queue(conn, queue_key)
            self._event(conn, updated, "claimed", idempotency)
            conn.commit()
        return {
            "claimed": True,
            "duplicate": False,
            "attempt": attempt,
            "lease_token": token,
            "claim_expires_at": updated["claim_expires_at"],
            "queue_item": row_to_queue(updated),
        }

    def orchestration_launch_prepare(self, args: dict[str, Any]) -> dict[str, Any]:
        queue_key = require_string(args, "queue_key", max_length=80)
        with self.connection() as conn:
            row = self._queue(conn, queue_key)
            self._verify_orchestrator(row, args)
            attempt = self._attempt(row, args)
            token = self._verify_lease(row, args)
            launch_key = optional_string(args, "launch_key", max_length=240) or stable_key(
                "launch", queue_key, str(attempt)
            )
            if row["launch_key"] == launch_key and row["status"] in {"launching", "delegated", "completed", "failed"}:
                return {
                    "prepared": True,
                    "duplicate": True,
                    "attempt": attempt,
                    "launch_key": launch_key,
                    "lease_token": token,
                    "queue_item": row_to_queue(row),
                }
            if row["status"] != "claimed":
                raise ToolExecutionError("Only a claimed queue item can prepare a provider launch.")
            now = utc_now()
            conn.execute(
                """UPDATE mcp_orchestration_queue
                   SET status='launching', launch_key=?, launch_prepared_at=?,
                       accepted_model_registration_key=model_registration_key,
                       accepted_provider=provider, accepted_model_family=model_family,
                       accepted_model_version=model_version, accepted_model_variant=model_variant,
                       accepted_reasoning_effort=reasoning_effort, accepted_client_name=client_name,
                       updated_at=? WHERE queue_key=? AND status='claimed'""",
                (launch_key, now, now, queue_key),
            )
            updated = self._queue(conn, queue_key)
            self._event(conn, updated, "launch_prepared", launch_key, "Launch intent persisted before provider call.")
            conn.commit()
        return {
            "prepared": True,
            "duplicate": False,
            "attempt": attempt,
            "launch_key": launch_key,
            "lease_token": token,
            "queue_item": row_to_queue(updated),
        }

    def orchestration_renew(self, args: dict[str, Any]) -> dict[str, Any]:
        queue_key = require_string(args, "queue_key", max_length=80)
        claim_ttl = int_arg(
            args, "claim_ttl_seconds", default=DEFAULT_CLAIM_TTL_SECONDS, minimum=60, maximum=86400
        )
        with self.connection() as conn:
            row = self._queue(conn, queue_key)
            self._verify_orchestrator(row, args)
            attempt = self._attempt(row, args)
            token = self._verify_lease(row, args)
            if row["status"] not in {"claimed", "launching", "delegated"}:
                raise ToolExecutionError("Only claimed, launching, or delegated work can renew a claim lease.")
            expires = utc_after(claim_ttl)
            now = utc_now()
            conn.execute(
                "UPDATE mcp_orchestration_queue SET claim_expires_at=?, updated_at=? WHERE queue_key=?",
                (expires, now, queue_key),
            )
            updated = self._queue(conn, queue_key)
            self._event(conn, updated, "lease_renewed", stable_key("renew", queue_key, str(attempt), expires), "Claim lease renewed.")
            conn.commit()
        return {"renewed": True, "attempt": attempt, "lease_token": token, "claim_expires_at": expires, "queue_item": row_to_queue(updated)}

    def orchestration_reconcile(self, args: dict[str, Any]) -> dict[str, Any]:
        assignment_key = require_string(args, "assignment_key", max_length=80)
        resolution = optional_string(args, "resolution", max_length=30)
        with self.connection() as conn:
            assignment = self._assignment(conn, assignment_key)
            self._verify_assignment_identity(assignment, args)
            self._sweep_stale(conn)
            queue_key = optional_string(args, "queue_key", max_length=80)
            if queue_key:
                row = self._queue(conn, queue_key)
                if row["assignment_key"] != assignment_key:
                    raise ToolExecutionError("The queue item does not belong to this assignment.")
            else:
                row = conn.execute(
                    """SELECT * FROM mcp_orchestration_queue
                       WHERE assignment_key=?
                         AND (status IN ('dispatched','claimed','launching','delegated','recovery_required')
                              OR (status='completed' AND checkpoint_required=1
                                  AND checkpoint_status IN ('pending','failed')))
                       ORDER BY CASE status WHEN 'completed' THEN 0 WHEN 'recovery_required' THEN 1
                                            WHEN 'launching' THEN 2 WHEN 'delegated' THEN 3 ELSE 4 END,
                                position
                       LIMIT 1""",
                    (assignment_key,),
                ).fetchone()
            if row and resolution:
                if row["status"] != "recovery_required":
                    raise ToolExecutionError("A resolution is only accepted for recovery-required work.")
                now = utc_now()
                child_id = optional_string(args, "child_session_id", max_length=240)
                outcome = optional_string(args, "child_outcome", max_length=80)
                evidence = optional_string(args, "terminal_evidence", max_length=20_000)
                if resolution == "claimed":
                    conn.execute(
                        "UPDATE mcp_orchestration_queue SET status='claimed', claim_expires_at=?, recovery_reason='', error='', updated_at=? WHERE queue_key=?",
                        (utc_after(DEFAULT_CLAIM_TTL_SECONDS), now, row["queue_key"]),
                    )
                    event_detail = "Recovery reconciled with no child evidence; attempt returned to claimed."
                elif resolution == "delegated":
                    if not child_id:
                        raise ToolExecutionError("`child_session_id` is required to reconcile delegated work.")
                    conn.execute(
                        """UPDATE mcp_orchestration_queue SET status='delegated', child_session_id=?,
                           child_outcome=?, claim_expires_at=?, recovery_reason='', error='', delegated_at=COALESCE(delegated_at, ?), updated_at=?
                           WHERE queue_key=?""",
                        (child_id, outcome, utc_after(DEFAULT_CLAIM_TTL_SECONDS), now, now, row["queue_key"]),
                    )
                    event_detail = evidence or "Recovery reattached the persisted child session."
                elif resolution in {"failed", "cancelled"}:
                    conn.execute(
                        """UPDATE mcp_orchestration_queue SET status=?, child_outcome=?, recovery_reason='',
                           error=?, completed_at=COALESCE(completed_at, ?), cancelled_at=CASE WHEN ?='cancelled' THEN COALESCE(cancelled_at, ?) ELSE cancelled_at END,
                           updated_at=? WHERE queue_key=?""",
                        (resolution, outcome or resolution, evidence or resolution, now, resolution, now, now, row["queue_key"]),
                    )
                    event_detail = evidence or f"Recovery recorded terminal {resolution} evidence."
                else:
                    raise ToolExecutionError("Unsupported recovery resolution.")
                row = self._queue(conn, row["queue_key"])
                self._event(conn, row, "reconciled", stable_key("reconcile", row["queue_key"], resolution), event_detail)
            action = "wake"
            if row:
                if row["status"] == "recovery_required":
                    action = "reconcile"
                elif row["cancel_requested_at"] and not row["cancel_acknowledged_at"]:
                    action = "cancel"
                elif row["status"] == "claimed":
                    action = "launch_prepare"
                elif row["status"] == "launching":
                    action = "delegate" if row["child_session_id"] else "launch"
                elif row["status"] == "delegated":
                    action = "cancel" if row["cancel_requested_at"] and not row["cancel_acknowledged_at"] else "monitor"
                elif row["status"] == "dispatched":
                    action = "claim"
                elif row["status"] == "completed":
                    action = "checkpoint"
            conn.commit()
        return {
            "reconciled": bool(row),
            "action": action,
            "pending": bool(row),
            "attempt": int(row["attempt"]) if row else None,
            "queue_item": row_to_queue(row) if row else None,
            "assignment": row_to_assignment(assignment),
        }

    def orchestration_release(self, args: dict[str, Any]) -> dict[str, Any]:
        assignment_key = require_string(args, "assignment_key", max_length=80)
        force = bool_arg(args, "force", default=False)
        with self.connection() as conn:
            assignment = self._assignment(conn, assignment_key)
            self._verify_assignment_identity(assignment, args)
            if assignment["status"] == "stopped":
                return {
                    "released": True,
                    "duplicate": True,
                    "assignment": row_to_assignment(assignment),
                }
            in_flight = conn.execute(
                """SELECT queue_key, status FROM mcp_orchestration_queue
                   WHERE assignment_key=? AND status IN ('dispatched','claimed','launching','delegated','recovery_required')
                   ORDER BY position LIMIT 1""",
                (assignment_key,),
            ).fetchone()
            if in_flight and not force:
                raise ToolExecutionError(
                    f"Cannot release assignment while queue item `{in_flight['queue_key']}` is {in_flight['status']}."
                )
            if in_flight and force:
                raise ToolExecutionError("`force` cannot release an assignment with unresolved in-flight work.")
            now = utc_now()
            conn.execute(
                """UPDATE mcp_orchestrator_assignments
                   SET status='stopped', stop_acknowledged_at=COALESCE(stop_acknowledged_at, ?),
                       stop_terminal_evidence=CASE
                           WHEN stop_requested_at IS NOT NULL AND stop_terminal_evidence=''
                           THEN 'All orchestration queue work reached terminal state.'
                           ELSE stop_terminal_evidence END,
                       updated_at=? WHERE assignment_key=?""",
                (now, now, assignment_key),
            )
            updated = self._assignment(conn, assignment_key)
            conn.commit()
        return {"released": True, "duplicate": False, "assignment": row_to_assignment(updated)}

    def orchestration_delegate(self, args: dict[str, Any]) -> dict[str, Any]:
        queue_key = require_string(args, "queue_key", max_length=80)
        child_session_id = require_string(args, "child_session_id", max_length=240)
        idempotency = require_string(args, "idempotency_key", max_length=160)
        with self.connection() as conn:
            row = self._queue(conn, queue_key)
            self._verify_orchestrator(row, args)
            modern = any(key in args for key in ("attempt", "lease_token", "launch_key", "accepted_provider", "accepted_model"))
            if modern or row["status"] == "launching":
                attempt = self._attempt(row, args)
                token = self._verify_lease(row, args)
                launch_key = require_string(args, "launch_key", max_length=240)
                if row["launch_key"] != launch_key:
                    raise ToolExecutionError("The launch key does not match the persisted launch intent.")
                accepted = self._accepted_model(args)
                self._verify_accepted_model(row, accepted)
            else:
                # Compatibility for callers written before launch_prepare. New
                # runtimes always use the strict attempt/lease contract above.
                if row["status"] != "claimed":
                    raise ToolExecutionError("Only a claimed queue item can record a child delegation.")
                attempt = int(row["attempt"] or 1)
                token = ""
                launch_key = stable_key("launch", queue_key, str(attempt))
                now = utc_now()
                conn.execute(
                    """UPDATE mcp_orchestration_queue
                       SET status='launching', launch_key=?, launch_prepared_at=?,
                           accepted_model_registration_key=model_registration_key,
                           accepted_provider=provider, accepted_model_family=model_family,
                           accepted_model_version=model_version, accepted_model_variant=model_variant,
                           accepted_reasoning_effort=reasoning_effort, accepted_client_name=client_name,
                           updated_at=? WHERE queue_key=? AND status='claimed'""",
                    (launch_key, now, now, queue_key),
                )
                row = self._queue(conn, queue_key)
                accepted = self._model_snapshot(row)
            if row["delegate_idempotency_key"] == idempotency and row["child_session_id"] == child_session_id:
                return {"delegated": True, "duplicate": True, "attempt": attempt, "launch_key": launch_key, "queue_item": row_to_queue(row)}
            if row["status"] not in {"claimed", "launching"}:
                raise ToolExecutionError("Only a claimed or launching queue item can record a child delegation.")
            if row["child_session_id"] and row["child_session_id"] != child_session_id:
                raise ToolExecutionError("A different child session is already correlated with this queue item.")
            now = utc_now()
            conn.execute(
                """UPDATE mcp_orchestration_queue
                   SET status='delegated', child_session_id=?, delegate_idempotency_key=?,
                       delegated_at=?, accepted_model_registration_key=?, accepted_provider=?,
                       accepted_model_family=?, accepted_model_version=?, accepted_model_variant=?,
                       accepted_reasoning_effort=?, accepted_client_name=?, updated_at=? WHERE queue_key=?""",
                (child_session_id, idempotency, now, accepted["model_registration_key"],
                 accepted["provider"], accepted["model_family"], accepted["model_version"],
                 accepted["model_variant"], accepted["reasoning_effort"], accepted["client_name"], now, queue_key),
            )
            updated = self._queue(conn, queue_key)
            self._event(conn, updated, "delegated", idempotency, child_session_id)
            conn.commit()
        return {"delegated": True, "duplicate": False, "attempt": attempt, "launch_key": launch_key, "queue_item": row_to_queue(updated)}

    def orchestration_complete(self, args: dict[str, Any]) -> dict[str, Any]:
        queue_key = require_string(args, "queue_key", max_length=80)
        outcome = require_string(args, "outcome", max_length=20)
        if outcome not in {"completed", "failed"}:
            raise ToolExecutionError("`outcome` must be `completed` or `failed`.")
        idempotency = require_string(args, "idempotency_key", max_length=160)
        detail = optional_string(args, "detail", max_length=20_000)
        with self.connection() as conn:
            row = self._queue(conn, queue_key)
            self._verify_orchestrator(row, args)
            attempt = self._attempt(row, args)
            lease_token = ""
            if any(key in args for key in ("attempt", "lease_token")):
                lease_token = self._verify_lease(row, args)
            if row["completion_idempotency_key"] == idempotency and row["status"] == outcome:
                return {"completed": outcome == "completed", "duplicate": True, "attempt": attempt, "queue_item": row_to_queue(row)}
            if row["cancel_requested_at"] and not row["cancel_acknowledged_at"]:
                raise ToolExecutionError(
                    "Cancellation is awaiting child terminal acknowledgement; use mudra_orchestration_cancel_ack."
                )
            if row["status"] != "delegated":
                raise ToolExecutionError("Only a delegated queue item can be completed.")
            now = utc_now()
            checkpoint_status = row["checkpoint_status"]
            if outcome == "completed" and not row["checkpoint_required"]:
                checkpoint_status = "not_required"
            conn.execute(
                """UPDATE mcp_orchestration_queue SET status=?, child_outcome=?,
                       completion_idempotency_key=?, completed_at=?, updated_at=?,
                       checkpoint_status=?, error=? WHERE queue_key=?""",
                (outcome, detail or outcome, idempotency, now, now, checkpoint_status,
                 detail if outcome == "failed" else "", queue_key),
            )
            updated = self._queue(conn, queue_key)
            self._event(conn, updated, outcome, idempotency, detail)
            if outcome == "completed" and not updated["checkpoint_required"]:
                self._dispatch_next(conn, updated["assignment_key"])
            conn.commit()
        return {"completed": outcome == "completed", "duplicate": False, "attempt": attempt, "queue_item": row_to_queue(updated)}

    @staticmethod
    def _checkpoint_commit_hash(args: dict[str, Any]) -> str:
        """Return a bounded, syntactically valid client-reported commit hash."""
        value = optional_string(args, "commit_hash", max_length=64)
        if not value:
            return ""
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", value):
            raise ToolExecutionError(
                "`commit_hash` must be 7-64 hexadecimal characters."
            )
        return value.lower()

    @staticmethod
    def _checkpoint_evidence(args: dict[str, Any]) -> str:
        """Return bounded evidence with obvious secret-bearing shapes rejected.

        Evidence is an untrusted client claim. The server keeps it short,
        strips credentials it can recognize, and never treats it as proof of
        client-local Git state.
        """
        value = args.get("evidence_summary")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ToolExecutionError("`evidence_summary` must be a string when provided.")
        value = value.strip()
        if len(value) > MAX_CHECKPOINT_EVIDENCE_CHARS:
            raise ToolExecutionError(
                "`evidence_summary` must be at most "
                f"{MAX_CHECKPOINT_EVIDENCE_CHARS} characters; send a short redacted "
                "summary rather than diffs or command output."
            )
        return OrchestrationService._redact_git_output(value)

    def _checkpoint_row(
        self, conn: sqlite3.Connection, args: dict[str, Any]
    ) -> tuple[sqlite3.Row, str]:
        """Resolve and authorize a queue row for a client checkpoint call."""
        queue_key = require_string(args, "queue_key", max_length=80)
        workspace_id = require_string(args, "workspace_id", max_length=100)
        row = self._queue(conn, queue_key)
        self._verify_orchestrator(row, args)
        if row["writer_workspace_id"] != workspace_id:
            raise ToolExecutionError(
                "The checkpoint workspace does not match the writer assignment.",
                {
                    "reason": "workspace_mismatch",
                    "queue_key": queue_key,
                    "expected_workspace_id": row["writer_workspace_id"],
                },
            )
        # An explicit attempt or lease token is validated when supplied; a stale
        # or superseded lease must never be able to record a checkpoint.
        if "attempt" in args:
            self._attempt(row, args)
        if "lease_token" in args:
            self._verify_lease(row, args)
        return row, workspace_id

    def orchestration_checkpoint_prepare(self, args: dict[str, Any]) -> dict[str, Any]:
        idempotency = require_string(args, "idempotency_key", max_length=160)
        with self.connection() as conn:
            row, workspace_id = self._checkpoint_row(conn, args)
            queue_key = row["queue_key"]
            if row["status"] != "completed":
                raise ToolExecutionError("Only completed work can prepare a checkpoint.")
            if not row["checkpoint_required"]:
                raise ToolExecutionError("This queue item does not require a checkpoint.")
            if row["checkpoint_status"] in {"success", "no_changes"}:
                raise ToolExecutionError("The checkpoint is already terminal.")
            checkpoint_key = row["checkpoint_key"] or stable_key(
                "checkpoint", queue_key, str(int(row["attempt"] or 1))
            )
            duplicate = bool(row["checkpoint_key"])
            if not duplicate:
                now = utc_now()
                conn.execute(
                    """UPDATE mcp_orchestration_queue
                       SET checkpoint_key=?, checkpoint_prepared_at=?, updated_at=?
                       WHERE queue_key=?""",
                    (checkpoint_key, now, now, queue_key),
                )
                row = self._queue(conn, queue_key)
                self._event(conn, row, "checkpoint_prepared", idempotency)
            assignment = self._assignment(conn, row["assignment_key"])
            conn.commit()
        # A recorded commit awaiting a successful push resumes at the push stage
        # so a retry never creates a second commit.
        recorded_commit = row["checkpoint_commit"] or ""
        return {
            "prepared": True,
            "duplicate": duplicate,
            "checkpoint": {
                "checkpoint_key": checkpoint_key,
                "queue_key": queue_key,
                "workspace_id": workspace_id,
                "expected_branch": row["writer_branch"],
                "allowed_paths": parse_json(assignment["allowed_paths_json"], []),
                "failure_policy": row["checkpoint_failure_policy"],
                "checkpoint_required": bool(row["checkpoint_required"]),
                "resume_stage": "push" if recorded_commit else "commit",
                "recorded_commit": recorded_commit,
            },
            "queue_item": row_to_queue(row),
        }

    def orchestration_checkpoint_record(self, args: dict[str, Any]) -> dict[str, Any]:
        checkpoint_key = require_string(args, "checkpoint_key", max_length=100)
        stage = optional_string(args, "stage", default="push", max_length=20) or "push"
        if stage not in CHECKPOINT_STAGES:
            raise ToolExecutionError("`stage` must be `commit` or `push`.")
        result = require_string(args, "result", max_length=20)
        if result not in CHECKPOINT_RESULTS:
            raise ToolExecutionError("`result` must be `success`, `no_changes`, or `failed`.")
        idempotency = require_string(args, "idempotency_key", max_length=160)
        commit_hash = self._checkpoint_commit_hash(args)
        push_target = optional_string(args, "push_target", max_length=500)
        evidence = self._checkpoint_evidence(args)
        if stage == "commit":
            if result == "success" and not commit_hash:
                raise ToolExecutionError("A successful `commit` stage must report `commit_hash`.")
            if push_target:
                raise ToolExecutionError("`push_target` belongs to the `push` stage.")
        if result == "success" and stage == "push" and not commit_hash:
            raise ToolExecutionError("`commit_hash` is required for a successful checkpoint.")
        if result == "no_changes" and commit_hash:
            raise ToolExecutionError("A no_changes checkpoint cannot record a commit hash.")
        with self.connection() as conn:
            row, _workspace_id = self._checkpoint_row(conn, args)
            queue_key = row["queue_key"]
            if not row["checkpoint_key"] or row["checkpoint_key"] != checkpoint_key:
                raise ToolExecutionError("The checkpoint key does not match a prepared checkpoint.")
            recorded_commit = row["checkpoint_commit"] or ""
            if row["checkpoint_idempotency_key"] == idempotency:
                expected = "committed" if stage == "commit" and result == "success" else result
                if row["checkpoint_status"] != expected:
                    raise ToolExecutionError("The checkpoint replay conflicts with the recorded result.")
                return {
                    "recorded": True,
                    "duplicate": True,
                    "stage": stage,
                    "queue_item": row_to_queue(row),
                    "next_dispatched": None,
                }
            # A failed push over a durable commit stays resumable: the commit
            # survives, and a later push retry may still reach `success`.
            resumable_failure = (
                row["checkpoint_status"] == "failed"
                and bool(recorded_commit)
                and stage == "push"
            )
            if row["checkpoint_status"] in CHECKPOINT_TERMINAL_STATUSES and not resumable_failure:
                raise ToolExecutionError("The checkpoint already has a different terminal record.")
            # A commit recorded by an earlier attempt is durable: a resumed push
            # must reference the same commit rather than silently replacing it.
            if recorded_commit and commit_hash and commit_hash != recorded_commit:
                raise ToolExecutionError(
                    "The reported commit does not match the recorded checkpoint commit; "
                    "resume the push for the existing commit instead of creating another.",
                    {
                        "reason": "commit_conflict",
                        "recorded_commit": recorded_commit,
                    },
                )
            if stage == "push" and result == "no_changes" and recorded_commit:
                raise ToolExecutionError(
                    "A commit is already recorded for this checkpoint; it cannot resolve as no_changes."
                )
            now = utc_now()
            if stage == "commit":
                if result == "no_changes":
                    # A clean authorized tree is terminal without a commit.
                    status = "no_changes"
                else:
                    status = "committed" if result == "success" else "failed"
                commit_value = commit_hash or recorded_commit
                conn.execute(
                    """UPDATE mcp_orchestration_queue
                       SET checkpoint_status=?, checkpoint_idempotency_key=?,
                           checkpoint_commit=?, checkpoint_commit_recorded_at=?,
                           checkpoint_evidence_summary=?, checkpoint_error=?, updated_at=?
                       WHERE queue_key=?""",
                    (
                        status,
                        idempotency,
                        commit_value,
                        now if commit_value else row["checkpoint_commit_recorded_at"],
                        evidence,
                        evidence if result == "failed" else "",
                        now,
                        queue_key,
                    ),
                )
            else:
                commit_value = commit_hash or recorded_commit
                conn.execute(
                    """UPDATE mcp_orchestration_queue
                       SET checkpoint_status=?, checkpoint_idempotency_key=?,
                           checkpoint_commit=?, checkpoint_push_target=?,
                           checkpoint_evidence_summary=?, checkpoint_error=?, updated_at=?
                       WHERE queue_key=?""",
                    (
                        result,
                        idempotency,
                        commit_value,
                        push_target,
                        evidence,
                        evidence if result == "failed" else "",
                        now,
                        queue_key,
                    ),
                )
            updated = self._queue(conn, queue_key)
            event = (
                f"checkpoint_stage_{stage}_{result}"
                if stage == "commit"
                else f"checkpoint_{result}"
            )
            self._event(conn, updated, event, idempotency, evidence)
            dispatched = None
            terminal = updated["checkpoint_status"] in CHECKPOINT_TERMINAL_STATUSES
            if terminal and (
                updated["checkpoint_status"] != "failed"
                or updated["checkpoint_failure_policy"] == "continue"
            ):
                dispatched = self._dispatch_next(conn, updated["assignment_key"])
            conn.commit()
        return {
            "recorded": True,
            "duplicate": False,
            "stage": stage,
            "queue_item": row_to_queue(updated),
            "next_dispatched": dispatched,
        }

    def orchestration_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        assignment_key = require_string(args, "assignment_key", max_length=80)
        reason = optional_string(
            args, "reason", default="Stopped by dashboard owner.", max_length=20_000
        ) or "Stopped by dashboard owner."
        requested_idempotency = optional_string(args, "idempotency_key", max_length=160)
        now = utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            assignment = self._assignment(conn, assignment_key)
            stop_key = (
                assignment["stop_idempotency_key"]
                or requested_idempotency
                or stable_key("stop", assignment_key, assignment["created_at"])
            )
            duplicate = bool(assignment["stop_requested_at"])
            if not duplicate:
                conn.execute(
                    """UPDATE mcp_orchestrator_assignments
                       SET status='stopping', stop_requested_at=?, stop_reason=?,
                           stop_idempotency_key=?, stop_acknowledged_at=NULL,
                           stop_terminal_evidence='', updated_at=?
                       WHERE assignment_key=?""",
                    (now, reason, stop_key, now, assignment_key),
                )

            rows = conn.execute(
                """SELECT * FROM mcp_orchestration_queue
                   WHERE assignment_key=?
                     AND status IN ('queued','dispatched','claimed','launching','delegated','recovery_required')
                   ORDER BY position""",
                (assignment_key,),
            ).fetchall()
            cleared_count = 0
            requested_count = 0
            for row in rows:
                if row["status"] in {"queued", "dispatched"}:
                    ack_key = stable_key("stop_cancel_ack", stop_key, row["queue_key"])
                    conn.execute(
                        """UPDATE mcp_orchestration_queue
                           SET status='cancelled', cancelled_at=COALESCE(cancelled_at, ?),
                               cancel_requested_at=COALESCE(cancel_requested_at, ?),
                               cancel_acknowledged_at=COALESCE(cancel_acknowledged_at, ?),
                               cancel_reason=CASE WHEN cancel_reason='' THEN ? ELSE cancel_reason END,
                               cancel_ack_idempotency_key=CASE
                                   WHEN cancel_ack_idempotency_key='' THEN ? ELSE cancel_ack_idempotency_key END,
                               cancel_child_outcome=CASE
                                   WHEN cancel_child_outcome='' THEN 'not_started' ELSE cancel_child_outcome END,
                               cancel_child_evidence=CASE
                                   WHEN cancel_child_evidence='' THEN 'No child session was created.'
                                   ELSE cancel_child_evidence END,
                               updated_at=?, error=? WHERE queue_key=?""",
                        (now, now, now, reason, ack_key, now, reason, row["queue_key"]),
                    )
                    updated = self._queue(conn, row["queue_key"])
                    self._event(conn, updated, "stop_cancelled", stop_key, reason)
                    self._event(
                        conn, updated, "cancel_acknowledged", ack_key,
                        "No child session was created.",
                    )
                    cleared_count += 1
                else:
                    if not row["cancel_requested_at"]:
                        conn.execute(
                            """UPDATE mcp_orchestration_queue
                               SET cancel_requested_at=?, cancel_reason=?, error=?, updated_at=?
                               WHERE queue_key=?""",
                            (now, reason, reason, now, row["queue_key"]),
                        )
                        updated = self._queue(conn, row["queue_key"])
                        self._event(conn, updated, "stop_cancel_requested", stop_key, reason)
                    requested_count += 1

            pending_count = conn.execute(
                """SELECT COUNT(*) FROM mcp_orchestration_queue
                   WHERE assignment_key=?
                     AND status IN ('claimed','launching','delegated','recovery_required')
                     AND cancel_requested_at IS NOT NULL
                     AND cancel_acknowledged_at IS NULL""",
                (assignment_key,),
            ).fetchone()[0]
            if pending_count == 0:
                conn.execute(
                    """UPDATE mcp_orchestrator_assignments
                       SET status='stopped', stop_acknowledged_at=COALESCE(stop_acknowledged_at, ?),
                           stop_terminal_evidence=CASE WHEN stop_terminal_evidence=''
                               THEN 'No unresolved child session remained.'
                               ELSE stop_terminal_evidence END,
                           updated_at=? WHERE assignment_key=?""",
                    (now, now, assignment_key),
                )
            updated_assignment = self._assignment(conn, assignment_key)
            conn.commit()
        return {
            "stop_requested": True,
            "stopped": updated_assignment["status"] == "stopped",
            "duplicate": duplicate,
            "idempotency_key": stop_key,
            "cleared_count": cleared_count,
            "cancellation_requested_count": requested_count,
            "pending_count": pending_count,
            "assignment": row_to_assignment(updated_assignment),
        }

    def orchestration_cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        queue_key = require_string(args, "queue_key", max_length=80)
        reason = optional_string(args, "reason", default="Cancelled by owner.", max_length=20_000)
        with self.connection() as conn:
            row = self._queue(conn, queue_key)
            if row["status"] == "completed":
                raise ToolExecutionError("A completed queue item cannot be cancelled.")
            if row["status"] == "cancelled":
                return {"cancelled": True, "duplicate": True, "queue_item": row_to_queue(row)}
            now = utc_now()
            has_live_child = row["status"] == "delegated" and bool(row["child_session_id"])
            if row["cancel_requested_at"] and not row["cancel_acknowledged_at"]:
                return {"cancelled": False, "requested": True, "duplicate": True, "queue_item": row_to_queue(row)}
            if has_live_child:
                conn.execute(
                    """UPDATE mcp_orchestration_queue
                       SET cancel_requested_at=?, cancel_reason=?, error=?, updated_at=?
                       WHERE queue_key=?""",
                    (now, reason, reason, now, queue_key),
                )
                updated = self._queue(conn, queue_key)
                self._event(conn, updated, "cancel_requested", stable_key("cancel", queue_key), reason)
                conn.commit()
                return {"cancelled": False, "requested": True, "duplicate": False, "queue_item": row_to_queue(updated)}
            conn.execute(
                """UPDATE mcp_orchestration_queue
                   SET status='cancelled', cancelled_at=?, cancel_requested_at=?,
                       cancel_acknowledged_at=?, cancel_reason=?,
                       cancel_ack_idempotency_key=?, cancel_child_outcome='not_started',
                       cancel_child_evidence='No child session was created.',
                       updated_at=?, error=? WHERE queue_key=?""",
                (now, now, now, reason, stable_key("cancel_ack", queue_key, "prelaunch"), now, reason, queue_key),
            )
            updated = self._queue(conn, queue_key)
            self._event(conn, updated, "cancel_requested", stable_key("cancel", queue_key), reason)
            self._event(conn, updated, "cancel_acknowledged", updated["cancel_ack_idempotency_key"], "No child session was created.")
            # A prelaunch cancel leaves the source todo `queued`; return it to open.
            self.todos.clear_todo_queued(
                conn, updated["todo_key"], detail="queue record cancelled"
            )
            self._dispatch_next(conn, updated["assignment_key"])
            conn.commit()
        return {"cancelled": True, "acknowledged": True, "duplicate": False, "queue_item": row_to_queue(updated)}

    def orchestration_cancel_ack(self, args: dict[str, Any]) -> dict[str, Any]:
        queue_key = require_string(args, "queue_key", max_length=80)
        idempotency = require_string(args, "idempotency_key", max_length=160)
        child_session_id = optional_string(args, "child_session_id", max_length=240)
        child_outcome = optional_string(
            args, "child_outcome", default=optional_string(args, "outcome", max_length=80), max_length=80
        )
        evidence = optional_string(
            args,
            "terminal_evidence",
            default=optional_string(args, "detail", max_length=20_000),
            max_length=20_000,
        )
        if not child_outcome:
            raise ToolExecutionError("`child_outcome` must provide terminal child evidence.")
        if not evidence:
            evidence = child_outcome
        with self.connection() as conn:
            row = self._queue(conn, queue_key)
            self._verify_orchestrator(row, args)
            if row["cancel_acknowledged_at"]:
                if row["cancel_ack_idempotency_key"] == idempotency:
                    return {"cancelled": True, "acknowledged": True, "duplicate": True, "queue_item": row_to_queue(row)}
                raise ToolExecutionError("Cancellation has already been acknowledged.")
            if not row["cancel_requested_at"]:
                raise ToolExecutionError("No cancellation request is pending for this queue item.")
            if row["status"] not in {"claimed", "launching", "delegated", "recovery_required"}:
                raise ToolExecutionError(
                    "Only cancellation for an attempt that may have launched a child can be acknowledged."
                )
            if row["child_session_id"] and child_session_id and child_session_id != row["child_session_id"]:
                raise ToolExecutionError("The acknowledgement child session does not match the delegated child.")
            now = utc_now()
            conn.execute(
                """UPDATE mcp_orchestration_queue
                   SET status='cancelled', cancelled_at=?, cancel_acknowledged_at=?,
                       cancel_ack_idempotency_key=?, cancel_child_outcome=?,
                       cancel_child_evidence=?, child_outcome=?, updated_at=?
                   WHERE queue_key=?""",
                (now, now, idempotency, child_outcome, evidence, child_outcome, now, queue_key),
            )
            updated = self._queue(conn, queue_key)
            self._event(conn, updated, "cancel_acknowledged", idempotency, evidence)
            self._dispatch_next(conn, updated["assignment_key"])
            assignment = self._assignment(conn, updated["assignment_key"])
            conn.commit()
        return {
            "cancelled": True,
            "acknowledged": True,
            "duplicate": False,
            "release_ready": assignment["status"] == "stopping",
            "queue_item": row_to_queue(updated),
        }

    def orchestration_retry(self, args: dict[str, Any]) -> dict[str, Any]:
        queue_key = require_string(args, "queue_key", max_length=80)
        idempotency = require_string(args, "idempotency_key", max_length=160)
        with self.connection() as conn:
            row = self._queue(conn, queue_key)
            self._sweep_stale(conn)
            row = self._queue(conn, queue_key)
            assignment = self._assignment(conn, row["assignment_key"])
            if assignment["status"] != "active":
                raise ToolExecutionError(
                    f"The orchestrator assignment is {assignment['status']}; stopped work cannot be retried."
                )
            dispatch_idempotency = stable_key("dispatch", queue_key, idempotency)
            if row["dispatch_idempotency_key"] == dispatch_idempotency and row["status"] in {"queued", "dispatched"}:
                return {"retried": True, "duplicate": True, "queue_item": row_to_queue(row)}
            if row["cancel_requested_at"] and not row["cancel_acknowledged_at"]:
                raise ToolExecutionError("Cancellation is awaiting child terminal acknowledgement.")
            if row["status"] == "recovery_required":
                raise ToolExecutionError("Recovery-required work must be reconciled before it can be retried.")
            if row["status"] not in {"failed", "cancelled", "stale"}:
                raise ToolExecutionError("Only failed, cancelled, or stale queue work can be retried.")
            now = utc_now()
            conn.execute(
                """UPDATE mcp_orchestration_queue SET status='queued', retry_count=retry_count+1,
                       attempt=attempt+1,
                       dispatch_idempotency_key=?, correlation_id=?, claim_idempotency_key='',
                       delegate_idempotency_key='', completion_idempotency_key='',
                       child_session_id='', child_outcome='', error='', recovery_reason='',
                       claim_lease_token_hash='', claim_expires_at=NULL, launch_key='', launch_prepared_at=NULL,
                       accepted_model_registration_key='', accepted_provider='', accepted_model_family='',
                       accepted_model_version='', accepted_model_variant='', accepted_reasoning_effort='',
                       accepted_client_name='', dispatched_at=NULL,
                       claimed_at=NULL, delegated_at=NULL, completed_at=NULL, cancelled_at=NULL,
                       cancel_requested_at=NULL, cancel_acknowledged_at=NULL,
                       cancel_reason='', cancel_ack_idempotency_key='',
                       cancel_child_outcome='', cancel_child_evidence='',
                       updated_at=? WHERE queue_key=?""",
                (dispatch_idempotency, stable_key("corr", queue_key, idempotency), now, queue_key),
            )
            updated = self._queue(conn, queue_key)
            self._event(conn, updated, "retried", idempotency)
            self._dispatch_next(conn, updated["assignment_key"])
            updated = self._queue(conn, queue_key)
            conn.commit()
        return {"retried": True, "duplicate": False, "queue_item": row_to_queue(updated)}

    def orchestration_drop(self, args: dict[str, Any]) -> dict[str, Any]:
        """Soft-delete a terminal queue row so it stops cluttering the list.

        Only a cancelled, failed, or stale row can be dropped. A completed row
        is retained (it gates the assignment checkpoint), and a row with an
        unresolved cancellation request is rejected so a possibly-live child is
        never abandoned. The row keeps its history and is only hidden from the
        default queue list.
        """
        queue_key = require_string(args, "queue_key", max_length=80)
        reason = optional_string(args, "reason", default="Dropped by owner.", max_length=20_000)
        with self.connection() as conn:
            row = self._queue(conn, queue_key)
            if row["status"] == "dropped":
                return {"dropped": True, "duplicate": True, "queue_item": row_to_queue(row)}
            if row["cancel_requested_at"] and not row["cancel_acknowledged_at"]:
                raise ToolExecutionError(
                    "Cancellation is awaiting child terminal acknowledgement; drop is not permitted until it is acknowledged."
                )
            if row["status"] not in QUEUE_DROPPABLE_STATUSES:
                raise ToolExecutionError(
                    "Only a cancelled, failed, or stale queue record can be dropped."
                )
            now = utc_now()
            conn.execute(
                "UPDATE mcp_orchestration_queue SET status='dropped', dropped_at=?, updated_at=? WHERE queue_key=?",
                (now, now, queue_key),
            )
            updated = self._queue(conn, queue_key)
            self._event(conn, updated, "dropped", stable_key("dropped", queue_key), reason)
            # If the source todo is still `queued` (never dispatched/pruned), return
            # it to an open status so it does not stay greyed after the drop.
            self.todos.clear_todo_queued(
                conn, updated["todo_key"], detail="queue record dropped"
            )
            conn.commit()
        return {"dropped": True, "duplicate": False, "queue_item": row_to_queue(updated)}

    @staticmethod
    def _redact_git_output(value: str) -> str:
        redacted = re.sub(r"(https?://)[^/@\s]+@", r"\1<redacted>@", value)
        redacted = re.sub(r"(?i)(token|password|secret)=([^\s]+)", r"\1=<redacted>", redacted)
        return redacted[:MAX_GIT_OUTPUT_CHARS]

    def _run_git(self, repo_root: Path, args: Sequence[str]) -> dict[str, Any]:
        completed = self.command_runner(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        stdout = self._redact_git_output(completed.stdout or "")
        stderr = self._redact_git_output(completed.stderr or "")
        return {"returncode": int(completed.returncode), "stdout": stdout, "stderr": stderr}

    def _git_context(
        self, conn: sqlite3.Connection, args: dict[str, Any]
    ) -> tuple[sqlite3.Row, sqlite3.Row, Path, list[str]]:
        row = self._queue(conn, require_string(args, "queue_key", max_length=80))
        self._verify_orchestrator(row, args)
        assignment = self._assignment(conn, row["assignment_key"])
        # The legacy server-local Git tools require an explicit capability. A
        # client_local assignment is rejected before any path is resolved, so
        # the server never touches a filesystem on behalf of a workspace that
        # did not opt in.
        capability = str(assignment["workspace_capability"] or "client_local")
        if capability != "server_local":
            raise ToolExecutionError(
                "This designation is client_local; the server-local `mudra_git_*` tools "
                "are disabled. Use `mudra_orchestration_checkpoint_prepare` and execute "
                "the checkpoint in the designated workspace, then record the bounded "
                "result with `mudra_orchestration_checkpoint_record`.",
                {
                    "reason": "server_local_capability_required",
                    "workspace_id": assignment["workspace_id"],
                    "workspace_capability": capability,
                    "branch": assignment["branch"],
                },
            )
        # `repo_root` is untrusted client-local display metadata. The legacy
        # server-local Git tools are the only place it is ever resolved, and
        # only when the designation really is a checkout on this device. A
        # client-local designation on another device must use the client
        # checkpoint protocol instead of making the server touch a path.
        recorded_root = str(assignment["repo_root"] or "").strip()
        if not recorded_root:
            raise ToolExecutionError(
                "This designation has no server-resolvable repository path; use "
                "`mudra_orchestration_checkpoint_prepare` and execute the checkpoint "
                "in the designated client-local workspace.",
                {
                    "reason": "workspace_not_server_local",
                    "workspace_id": assignment["workspace_id"],
                    "branch": assignment["branch"],
                },
            )
        repo_root = Path(recorded_root).resolve()
        if not repo_root.is_dir() or not (repo_root / ".git").exists():
            raise ToolExecutionError(
                "The authorized Git repository is not present on this server; use "
                "`mudra_orchestration_checkpoint_prepare` and execute the checkpoint "
                "in the designated client-local workspace.",
                {
                    "reason": "workspace_not_server_local",
                    "workspace_id": assignment["workspace_id"],
                    "branch": assignment["branch"],
                },
            )
        allowed_paths = parse_json(assignment["allowed_paths_json"], [])
        branch_result = self._run_git(repo_root, ["branch", "--show-current"])
        if branch_result["returncode"] != 0 or branch_result["stdout"].strip() != assignment["branch"]:
            raise ToolExecutionError(
                f"Authorized branch `{assignment['branch']}` is not currently checked out."
            )
        return row, assignment, repo_root, allowed_paths

    def _working_tree(self, repo_root: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
        result = self._run_git(repo_root, ["status", "--porcelain=v1", "--untracked-files=all"])
        if result["returncode"] != 0:
            raise ToolExecutionError(result["stderr"] or "git status failed.")
        entries: list[dict[str, str]] = []
        for line in result["stdout"].splitlines():
            if len(line) < 4:
                continue
            raw_path = line[3:]
            if " -> " in raw_path:
                raw_path = raw_path.split(" -> ", 1)[1]
            entries.append({"status": line[:2], "path": raw_path.strip('"')})
        return entries, result

    def git_status(self, args: dict[str, Any]) -> dict[str, Any]:
        with self.connection() as conn:
            row, assignment, repo_root, allowed = self._git_context(conn, args)
            entries, command = self._working_tree(repo_root)
        unrelated = [entry for entry in entries if not self._path_allowed(entry["path"], allowed)]
        return {
            "queue_key": row["queue_key"], "repo_root": str(repo_root),
            "branch": assignment["branch"], "allowed_paths": allowed,
            "entries": entries, "unrelated_entries": unrelated,
            "authorized": not unrelated, "command": command,
        }

    def git_diff(self, args: dict[str, Any]) -> dict[str, Any]:
        cached = bool_arg(args, "cached", default=False)
        with self.connection() as conn:
            row, _assignment, repo_root, allowed = self._git_context(conn, args)
        command = self._run_git(repo_root, ["diff", *( ["--cached"] if cached else []), "--", *allowed])
        if command["returncode"] != 0:
            raise ToolExecutionError(command["stderr"] or "git diff failed.")
        return {"queue_key": row["queue_key"], "cached": cached, "diff": command["stdout"]}

    def git_add(self, args: dict[str, Any]) -> dict[str, Any]:
        paths = self._normalize_paths(args.get("paths"))
        with self.connection() as conn:
            row, _assignment, repo_root, allowed = self._git_context(conn, args)
        unauthorized = [path for path in paths if not self._path_allowed(path, allowed)]
        if unauthorized:
            raise ToolExecutionError("Requested paths are outside the Git authorization allowlist: " + ", ".join(unauthorized))
        entries, _status = self._working_tree(repo_root)
        unrelated = [entry["path"] for entry in entries if not self._path_allowed(entry["path"], allowed)]
        if unrelated:
            raise ToolExecutionError("Unrelated working-tree changes block the checkpoint: " + ", ".join(unrelated))
        command = self._run_git(repo_root, ["add", "--", *paths])
        if command["returncode"] != 0:
            raise ToolExecutionError(command["stderr"] or "git add failed.")
        return {"queue_key": row["queue_key"], "staged_paths": paths}

    def _staged_paths(self, repo_root: Path) -> list[str]:
        result = self._run_git(repo_root, ["diff", "--cached", "--name-only"])
        if result["returncode"] != 0:
            raise ToolExecutionError(result["stderr"] or "Unable to inspect staged paths.")
        return [line.strip() for line in result["stdout"].splitlines() if line.strip()]

    def git_commit(self, args: dict[str, Any]) -> dict[str, Any]:
        message = require_string(args, "message", max_length=500)
        idempotency = require_string(args, "idempotency_key", max_length=160)
        with self.connection() as conn:
            row, _assignment, repo_root, allowed = self._git_context(conn, args)
            if row["status"] != "completed":
                raise ToolExecutionError("Git checkpoint commit is allowed only after successful child completion.")
            if row["checkpoint_commit"]:
                return {"committed": True, "duplicate": True, "commit": row["checkpoint_commit"]}
            staged = self._staged_paths(repo_root)
            if not staged:
                raise ToolExecutionError("No staged changes are available for the checkpoint.")
            unauthorized = [path for path in staged if not self._path_allowed(path, allowed)]
            if unauthorized:
                raise ToolExecutionError("Unauthorized staged paths block the checkpoint: " + ", ".join(unauthorized))
            command = self._run_git(repo_root, ["commit", "-m", message])
            now = utc_now()
            if command["returncode"] != 0:
                error = command["stderr"] or command["stdout"] or "git commit failed."
                conn.execute(
                    "UPDATE mcp_orchestration_queue SET checkpoint_status='failed', checkpoint_error=?, updated_at=? WHERE queue_key=?",
                    (error, now, row["queue_key"]),
                )
                updated = self._queue(conn, row["queue_key"])
                self._event(conn, updated, "checkpoint_commit_failed", idempotency, error)
                if updated["checkpoint_failure_policy"] == "continue":
                    self._dispatch_next(conn, updated["assignment_key"])
                conn.commit()
                raise ToolExecutionError(error)
            head = self._run_git(repo_root, ["rev-parse", "HEAD"])
            commit = head["stdout"].strip()
            conn.execute(
                "UPDATE mcp_orchestration_queue SET checkpoint_status='committed', checkpoint_idempotency_key=?, checkpoint_commit=?, checkpoint_error='', updated_at=? WHERE queue_key=?",
                (idempotency, commit, now, row["queue_key"]),
            )
            updated = self._queue(conn, row["queue_key"])
            self._event(conn, updated, "checkpoint_committed", idempotency, commit)
            conn.commit()
        return {"committed": True, "duplicate": False, "commit": commit, "queue_item": row_to_queue(updated)}

    def git_checkpoint_noop(self, args: dict[str, Any]) -> dict[str, Any]:
        idempotency = require_string(args, "idempotency_key", max_length=160)
        with self.connection() as conn:
            row, assignment, repo_root, allowed = self._git_context(conn, args)
            if row["status"] != "completed":
                raise ToolExecutionError("Git checkpoint no-op is allowed only after successful child completion.")
            entries, _status = self._working_tree(repo_root)
            unrelated = [entry["path"] for entry in entries if not self._path_allowed(entry["path"], allowed)]
            if unrelated:
                raise ToolExecutionError("Unrelated working-tree changes block the checkpoint: " + ", ".join(unrelated))
            if entries:
                raise ToolExecutionError("Authorized working-tree changes remain; commit the checkpoint instead.")
            if row["checkpoint_status"] == "no_changes":
                return {
                    "no_changes": True,
                    "duplicate": True,
                    "queue_item": row_to_queue(row),
                    "next_dispatched": None,
                }
            now = utc_now()
            conn.execute(
                "UPDATE mcp_orchestration_queue SET checkpoint_status='no_changes', checkpoint_idempotency_key=?, checkpoint_commit='', checkpoint_error='', updated_at=? WHERE queue_key=?",
                (idempotency, now, row["queue_key"]),
            )
            updated = self._queue(conn, row["queue_key"])
            self._event(conn, updated, "checkpoint_no_changes", idempotency, "Authorized working tree is clean.")
            dispatched = self._dispatch_next(conn, updated["assignment_key"])
            conn.commit()
        return {
            "no_changes": True,
            "duplicate": False,
            "queue_item": row_to_queue(updated),
            "next_dispatched": dispatched,
        }

    def git_push(self, args: dict[str, Any]) -> dict[str, Any]:
        idempotency = require_string(args, "idempotency_key", max_length=160)
        remote = optional_string(args, "remote", default="origin", max_length=160) or "origin"
        if remote.startswith("-"):
            raise ToolExecutionError("`remote` must be a configured Git remote name.")
        with self.connection() as conn:
            row, assignment, repo_root, _allowed = self._git_context(conn, args)
            if row["checkpoint_status"] == "success":
                return {"pushed": True, "duplicate": True, "commit": row["checkpoint_commit"]}
            if not row["checkpoint_commit"]:
                raise ToolExecutionError("Commit the authorized checkpoint before pushing it.")
            head = self._run_git(repo_root, ["rev-parse", "HEAD"])
            if head["returncode"] != 0 or head["stdout"].strip() != row["checkpoint_commit"]:
                raise ToolExecutionError("HEAD no longer matches the recorded checkpoint commit.")
            command = self._run_git(repo_root, ["push", remote, f"HEAD:{assignment['branch']}"])
            now = utc_now()
            if command["returncode"] != 0:
                error = command["stderr"] or command["stdout"] or "git push failed."
                conn.execute(
                    "UPDATE mcp_orchestration_queue SET checkpoint_status='failed', checkpoint_idempotency_key=?, checkpoint_error=?, updated_at=? WHERE queue_key=?",
                    (idempotency, error, now, row["queue_key"]),
                )
                updated = self._queue(conn, row["queue_key"])
                self._event(conn, updated, "checkpoint_push_failed", idempotency, error)
                if updated["checkpoint_failure_policy"] == "continue":
                    self._dispatch_next(conn, updated["assignment_key"])
                conn.commit()
                raise ToolExecutionError(error)
            conn.execute(
                "UPDATE mcp_orchestration_queue SET checkpoint_status='success', checkpoint_idempotency_key=?, checkpoint_error='', updated_at=? WHERE queue_key=?",
                (idempotency, now, row["queue_key"]),
            )
            updated = self._queue(conn, row["queue_key"])
            self._event(conn, updated, "checkpoint_pushed", idempotency, updated["checkpoint_commit"])
            dispatched = self._dispatch_next(conn, updated["assignment_key"])
            conn.commit()
        return {
            "pushed": True, "duplicate": False, "commit": updated["checkpoint_commit"],
            "queue_item": row_to_queue(updated), "next_dispatched": dispatched,
        }

    def dashboard_snapshot(self) -> dict[str, Any]:
        return self.orchestration_list({"limit": 100})
