"""MCP-first orchestration supervisor runtime.

The runtime owns no server database state. Every durable transition goes through
``McpToolClient``; child and checkpoint side effects are isolated behind small
reattachable protocols.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import yaml

from mcp.child_session_adapters import (
    ChildLaunch,
    ChildSession,
    ChildSessionAdapter,
    FakeChildSessionAdapter,
    model_from_queue,
)
from mcp.tool_client import McpToolClient
from mcp.workspace_identity import WorkspaceIdentity, resolve_workspace_identity


DEFAULT_ALLOWED_PATHS = (
    "mcp",
    "mudra",
    "mudra-gateway",
    "android",
    "ios",
    "firmware",
    "hardware",
    "matlab",
    "specs",
    "docs",
    "scripts",
    "config.yaml",
    "AGENTS.md",
)
REQUIRED_TOOLS = (
    "mudra_agent_model_register",
    "mudra_task_check_in",
    "mudra_task_check_out",
    "mudra_orchestration_designate",
    "mudra_orchestration_heartbeat",
    "mudra_orchestration_wake",
    "mudra_orchestration_claim",
    "mudra_orchestration_launch_prepare",
    "mudra_orchestration_renew",
    "mudra_orchestration_reconcile",
    "mudra_orchestration_delegate",
    "mudra_orchestration_complete",
    "mudra_orchestration_cancel_ack",
    "mudra_orchestration_checkpoint_prepare",
    "mudra_orchestration_checkpoint_record",
    "mudra_orchestration_release",
)
SECRET_PATTERN = re.compile(
    r"(?i)(authorization|token|secret|password|lease_token|assignment_token)\s*[:=]\s*[^\s,;}]+"
)


@dataclass(frozen=True)
class RuntimeConfig:
    task_key: str
    agent_id: str
    provider: str
    model_family: str
    model_version: str
    reasoning_effort: str
    model_variant: str = ""
    client_name: str = "Codex"
    mcp_url: str | None = None
    repo_root: Path = Path(".")
    allowed_paths: tuple[str, ...] = DEFAULT_ALLOWED_PATHS
    checkpoint_required: bool = True
    checkpoint_failure_policy: str = "pause"
    assignment_ttl_seconds: int = 3600
    claim_ttl_seconds: int = 900
    heartbeat_interval_seconds: float = 30.0
    lease_renew_interval_seconds: float = 30.0
    wake_seconds: int = 0
    idle_poll_interval_seconds: float = 2.0
    child_poll_interval_seconds: float = 0.05
    child_timeout_seconds: float = 300.0
    log_max_chars: int = 2_000
    adapter: str = "fake"
    checkpoint_adapter: str = "fake"
    checkpoint_remote: str = "origin"
    enabled: bool = False

    def __post_init__(self) -> None:
        for key in ("task_key", "agent_id", "provider", "model_family", "model_version", "reasoning_effort"):
            if not str(getattr(self, key)).strip():
                raise ValueError(f"{key} must be a non-empty string")
        if self.model_family == "gpt" and not self.model_variant:
            raise ValueError("model_variant is required for the gpt model family")
        if self.checkpoint_failure_policy not in {"pause", "continue"}:
            raise ValueError("checkpoint_failure_policy must be pause or continue")
        if self.checkpoint_adapter not in {"fake", "local_git"}:
            raise ValueError("checkpoint_adapter must be fake or local_git")
        if not self.allowed_paths:
            raise ValueError("allowed_paths must not be empty")
        if self.assignment_ttl_seconds < 60 or self.claim_ttl_seconds < 60:
            raise ValueError("assignment and claim TTLs must be at least 60 seconds")
        if self.heartbeat_interval_seconds <= 0 or self.lease_renew_interval_seconds <= 0:
            raise ValueError("maintenance intervals must be positive")
        if not 0 <= self.wake_seconds <= 30:
            raise ValueError("wake_seconds must be between 0 and 30")
        if self.idle_poll_interval_seconds <= 0:
            raise ValueError("idle_poll_interval_seconds must be positive")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "RuntimeConfig":
        allowed = values.get("allowed_paths", DEFAULT_ALLOWED_PATHS)
        if not isinstance(allowed, (list, tuple)) or not all(isinstance(item, str) for item in allowed):
            raise ValueError("allowed_paths must be a list of strings")
        kwargs = dict(values)
        kwargs["allowed_paths"] = tuple(allowed)
        kwargs["repo_root"] = Path(values.get("repo_root", "."))
        return cls(**kwargs)

    @classmethod
    def load(cls, path: str | Path) -> "RuntimeConfig":
        source = Path(path)
        payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        section = payload.get("mcp", {}).get("orchestrator")
        if not isinstance(section, dict):
            raise ValueError("config must contain an mcp.orchestrator mapping")
        return cls.from_mapping(section)


@dataclass(frozen=True)
class CheckpointIntent:
    checkpoint_key: str
    queue_key: str
    workspace_id: str
    expected_branch: str
    allowed_paths: tuple[str, ...]
    failure_policy: str
    # `push` means the server already holds a durable commit for this
    # checkpoint; the adapter must resume the push rather than commit again.
    resume_stage: str = "commit"
    recorded_commit: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CheckpointIntent":
        return cls(
            checkpoint_key=str(payload["checkpoint_key"]),
            queue_key=str(payload["queue_key"]),
            workspace_id=str(payload["workspace_id"]),
            expected_branch=str(payload["expected_branch"]),
            allowed_paths=tuple(str(item) for item in payload.get("allowed_paths", [])),
            failure_policy=str(payload["failure_policy"]),
            resume_stage=str(payload.get("resume_stage", "commit")),
            recorded_commit=str(payload.get("recorded_commit", "")),
        )


@dataclass(frozen=True)
class CheckpointResult:
    result: str
    commit_hash: str = ""
    push_target: str = ""
    evidence_summary: str = ""

    def __post_init__(self) -> None:
        if self.result not in {"success", "no_changes", "failed"}:
            raise ValueError("Unsupported checkpoint result")
        if self.result == "success" and not self.commit_hash:
            raise ValueError("Successful checkpoints require a commit hash")


class CheckpointAdapter(Protocol):
    def execute(self, intent: CheckpointIntent, workspace: WorkspaceIdentity) -> CheckpointResult: ...


@dataclass
class FakeCheckpointAdapter:
    result: CheckpointResult = field(default_factory=lambda: CheckpointResult("no_changes"))
    execution_count: int = 0
    _results: dict[str, CheckpointResult] = field(default_factory=dict, init=False)

    def execute(self, intent: CheckpointIntent, workspace: WorkspaceIdentity) -> CheckpointResult:
        if intent.workspace_id != workspace.workspace_id:
            raise ValueError("Checkpoint intent targets a different workspace")
        existing = self._results.get(intent.checkpoint_key)
        if existing:
            return existing
        self.execution_count += 1
        self._results[intent.checkpoint_key] = self.result
        return self.result


class StructuredRuntimeLogger:
    def __init__(self, logger: logging.Logger | None = None, *, max_chars: int = 2_000) -> None:
        self.logger = logger or logging.getLogger("mudra.orchestrator")
        self.max_chars = max(256, max_chars)

    def emit(self, event: str, **fields: Any) -> None:
        safe: dict[str, Any] = {"event": event}
        for key, value in fields.items():
            if any(secret in key.lower() for secret in ("token", "secret", "password", "credential")):
                continue
            text = SECRET_PATTERN.sub(r"\1=[redacted]", str(value))
            safe[key] = text[: self.max_chars]
        self.logger.info(json.dumps(safe, ensure_ascii=True, sort_keys=True))


class OrchestratorRuntime:
    def __init__(
        self,
        config: RuntimeConfig,
        child_adapter: ChildSessionAdapter,
        checkpoint_adapter: CheckpointAdapter,
        *,
        client: McpToolClient | None = None,
        identity_resolver: Callable[[str | Path], WorkspaceIdentity] = resolve_workspace_identity,
        logger: StructuredRuntimeLogger | None = None,
    ) -> None:
        self.config = config
        self.child_adapter = child_adapter
        self.checkpoint_adapter = checkpoint_adapter
        self.client = client or McpToolClient(
            base_url=config.mcp_url,
            required_tools=REQUIRED_TOOLS,
        )
        self.identity_resolver = identity_resolver
        self.log = logger or StructuredRuntimeLogger(max_chars=config.log_max_chars)
        self.workspace: WorkspaceIdentity | None = None
        self.registration: dict[str, Any] | None = None
        self.assignment: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None
        self._lease_lock = threading.RLock()
        self._lease: dict[str, Any] | None = None

    @property
    def identity(self) -> dict[str, str]:
        if not self.assignment:
            raise RuntimeError("Runtime is not bootstrapped")
        return {
            "assignment_key": str(self.assignment["assignment_key"]),
            "task_key": self.config.task_key,
            "agent_id": self.config.agent_id,
        }

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.client.call(name, arguments)
        if not isinstance(result, dict):
            raise RuntimeError(f"{name} returned a non-object result")
        return result

    def bootstrap(self) -> dict[str, Any]:
        if self.assignment:
            return self.assignment
        workspace = self.identity_resolver(self.config.repo_root)
        registration_args = {
            "agent_id": self.config.agent_id,
            "provider": self.config.provider,
            "model_family": self.config.model_family,
            "model_version": self.config.model_version,
            "reasoning_effort": self.config.reasoning_effort,
            "client_name": self.config.client_name,
        }
        if self.config.model_variant:
            registration_args["model_variant"] = self.config.model_variant
        registration = self._call("mudra_agent_model_register", registration_args)
        designated = self._call(
            "mudra_orchestration_designate",
            {
                "task_key": self.config.task_key,
                "agent_id": self.config.agent_id,
                "model_registration_key": registration["model_registration_key"],
                **workspace.designation(),
                "allowed_paths": list(self.config.allowed_paths),
                "checkpoint_required": self.config.checkpoint_required,
                "checkpoint_failure_policy": self.config.checkpoint_failure_policy,
                "ttl_seconds": self.config.assignment_ttl_seconds,
            },
        )
        self._call(
            "mudra_task_check_in",
            {
                "task_key": self.config.task_key,
                "agent_id": self.config.agent_id,
                "model_registration_key": registration["model_registration_key"],
                "task_title": "Mudra orchestration supervisor",
                "summary": "Executing serialized MCP TODO work",
                "current_step": "Reconciling durable orchestration state",
                "workspace_root": str(workspace.repo_root),
                "ttl_seconds": self.config.assignment_ttl_seconds,
                "suppress_cascade": True,
            },
        )
        self.workspace = workspace
        self.registration = registration
        self.assignment = designated["assignment"]
        self._start_maintenance()
        self.log.emit(
            "bootstrap",
            assignment_key=self.assignment["assignment_key"],
            workspace_id=workspace.workspace_id,
            branch=workspace.branch,
            model=registration.get("canonical_model_label", ""),
        )
        return self.assignment

    def _start_maintenance(self) -> None:
        if self._maintenance_thread and self._maintenance_thread.is_alive():
            return
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name="mudra-orchestrator-maintenance",
            daemon=True,
        )
        self._maintenance_thread.start()

    def _maintenance_loop(self) -> None:
        next_heartbeat = time.monotonic()
        next_renew = time.monotonic()
        while not self._stop.wait(0.05):
            now = time.monotonic()
            try:
                if self.assignment and now >= next_heartbeat:
                    self._call(
                        "mudra_orchestration_heartbeat",
                        {**self.identity, "ttl_seconds": self.config.assignment_ttl_seconds},
                    )
                    next_heartbeat = now + self.config.heartbeat_interval_seconds
                with self._lease_lock:
                    lease = dict(self._lease) if self._lease else None
                if lease and now >= next_renew:
                    self._call(
                        "mudra_orchestration_renew",
                        {
                            "queue_key": lease["queue_key"],
                            "task_key": self.config.task_key,
                            "agent_id": self.config.agent_id,
                            "attempt": lease["attempt"],
                            "lease_token": lease["lease_token"],
                            "claim_ttl_seconds": self.config.claim_ttl_seconds,
                        },
                    )
                    next_renew = now + self.config.lease_renew_interval_seconds
            except Exception as exc:  # maintenance failure is surfaced without secrets
                self.log.emit("maintenance_error", error=exc)
                next_heartbeat = now + min(5.0, self.config.heartbeat_interval_seconds)
                next_renew = now + min(5.0, self.config.lease_renew_interval_seconds)

    def _claim(self, queue_item: Mapping[str, Any]) -> dict[str, Any]:
        attempt = int(queue_item.get("attempt") or 1)
        queue_key = str(queue_item["queue_key"])
        result = self._call(
            "mudra_orchestration_claim",
            {
                "queue_key": queue_key,
                "task_key": self.config.task_key,
                "agent_id": self.config.agent_id,
                "attempt": attempt,
                "idempotency_key": f"claim:{queue_key}:{attempt}",
                "claim_ttl_seconds": self.config.claim_ttl_seconds,
            },
        )
        with self._lease_lock:
            self._lease = {
                "queue_key": queue_key,
                "attempt": attempt,
                "lease_token": result["lease_token"],
            }
        return result

    def _launch_and_monitor(self, queue_item: Mapping[str, Any], claim: Mapping[str, Any]) -> dict[str, Any]:
        queue_key = str(queue_item["queue_key"])
        attempt = int(queue_item.get("attempt") or 1)
        lease_token = str(claim["lease_token"])
        prepared = self._call(
            "mudra_orchestration_launch_prepare",
            {
                "queue_key": queue_key,
                "task_key": self.config.task_key,
                "agent_id": self.config.agent_id,
                "attempt": attempt,
                "lease_token": lease_token,
                "idempotency_key": f"launch-prepare:{queue_key}:{attempt}",
            },
        )
        persisted = prepared["queue_item"]
        launch_key = str(prepared["launch_key"])
        assert self.workspace is not None
        child = self.child_adapter.create_or_get(
            ChildLaunch(
                launch_key=launch_key,
                prompt=str(persisted["prompt_snapshot"]),
                model=model_from_queue(persisted),
                workspace_id=self.workspace.workspace_id,
                repo_root=str(self.workspace.repo_root),
            )
        )
        delegated = self._call(
            "mudra_orchestration_delegate",
            {
                "queue_key": queue_key,
                "task_key": self.config.task_key,
                "agent_id": self.config.agent_id,
                "child_session_id": child.child_session_id,
                "idempotency_key": f"delegate:{queue_key}:{attempt}",
                "attempt": attempt,
                "lease_token": lease_token,
                "launch_key": launch_key,
                "accepted_model": child.accepted_model,
            },
        )
        return self._monitor(delegated["queue_item"], child, lease_token)

    def _monitor(
        self,
        queue_item: Mapping[str, Any],
        child: ChildSession | None = None,
        lease_token: str = "",
    ) -> dict[str, Any]:
        queue_key = str(queue_item["queue_key"])
        attempt = int(queue_item.get("attempt") or 1)
        if not lease_token:
            lease_token = str(self._claim(queue_item)["lease_token"])
        child_id = str(queue_item.get("child_session_id") or (child.child_session_id if child else ""))
        deadline = time.monotonic() + self.config.child_timeout_seconds
        while True:
            state = child if child is not None else self.child_adapter.inspect(child_id)
            child = None
            if state.terminal:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Child session {child_id} did not become terminal")
            self._stop.wait(self.config.child_poll_interval_seconds)
        outcome = "completed" if state.status == "completed" else "failed"
        completed = self._call(
            "mudra_orchestration_complete",
            {
                "queue_key": queue_key,
                "task_key": self.config.task_key,
                "agent_id": self.config.agent_id,
                "attempt": attempt,
                "lease_token": lease_token,
                "outcome": outcome,
                "detail": state.summary[:2_000],
                "idempotency_key": f"complete:{queue_key}:{attempt}",
            },
        )
        with self._lease_lock:
            self._lease = None
        if outcome == "completed" and completed["queue_item"].get("checkpoint_required"):
            return self.run_checkpoint(completed["queue_item"])
        return completed

    def run_checkpoint(self, queue_item: Mapping[str, Any]) -> dict[str, Any]:
        if not self.workspace:
            raise RuntimeError("Runtime is not bootstrapped")
        queue_key = str(queue_item["queue_key"])
        attempt = int(queue_item.get("attempt") or 1)
        base = {
            "queue_key": queue_key,
            "task_key": self.config.task_key,
            "agent_id": self.config.agent_id,
            "workspace_id": self.workspace.workspace_id,
        }
        prepared = self._call(
            "mudra_orchestration_checkpoint_prepare",
            {**base, "idempotency_key": f"checkpoint-prepare:{queue_key}:{attempt}"},
        )
        intent = CheckpointIntent.from_payload(prepared["checkpoint"])
        result = self.checkpoint_adapter.execute(intent, self.workspace)
        # Record a freshly created commit before the terminal push result, so a
        # push failure resumes at the push stage instead of committing twice.
        if (
            result.commit_hash
            and intent.resume_stage == "commit"
            and result.result != "no_changes"
        ):
            self._call(
                "mudra_orchestration_checkpoint_record",
                {
                    **base,
                    "checkpoint_key": intent.checkpoint_key,
                    "stage": "commit",
                    "result": "success",
                    "commit_hash": result.commit_hash,
                    "evidence_summary": result.evidence_summary[:2_000],
                    "idempotency_key": f"checkpoint-commit:{intent.checkpoint_key}",
                },
            )
        return self._call(
            "mudra_orchestration_checkpoint_record",
            {
                **base,
                "checkpoint_key": intent.checkpoint_key,
                "stage": "push",
                "result": result.result,
                "commit_hash": result.commit_hash,
                "push_target": result.push_target,
                "evidence_summary": result.evidence_summary[:2_000],
                "idempotency_key": f"checkpoint-record:{intent.checkpoint_key}:{attempt}",
            },
        )

    def _cancel(self, queue_item: Mapping[str, Any]) -> dict[str, Any]:
        queue_key = str(queue_item["queue_key"])
        attempt = int(queue_item.get("attempt") or 1)
        child_id = str(queue_item.get("child_session_id") or "")
        if child_id:
            child = self.child_adapter.cancel(child_id)
            if not child.terminal:
                raise RuntimeError("Adapter did not provide terminal cancellation evidence")
            outcome = child.status
            evidence = child.summary
        else:
            outcome = "cancelled"
            evidence = "No child session was recorded."
        return self._call(
            "mudra_orchestration_cancel_ack",
            {
                "queue_key": queue_key,
                "task_key": self.config.task_key,
                "agent_id": self.config.agent_id,
                "child_session_id": child_id,
                "child_outcome": outcome,
                "terminal_evidence": evidence[:2_000],
                "idempotency_key": f"cancel-ack:{queue_key}:{attempt}",
            },
        )

    def run_once(self) -> dict[str, Any]:
        self.bootstrap()
        reconciled = self._call("mudra_orchestration_reconcile", self.identity)
        action = reconciled.get("action", "wake")
        queue_item = reconciled.get("queue_item")
        if action == "wake":
            wake = self._call(
                "mudra_orchestration_wake",
                {**self.identity, "wait_seconds": self.config.wake_seconds},
            )
            action = wake.get("action", "idle")
            queue_item = wake.get("queue_item")
        if action == "idle":
            return {"action": "idle"}
        if action == "stop":
            return self.drain()
        if not isinstance(queue_item, dict):
            raise RuntimeError(f"Runtime action {action} did not include a queue item")
        self.log.emit("action", action=action, queue_key=queue_item.get("queue_key"))
        if action == "cancel":
            return self._cancel(queue_item)
        if action in {"claim", "dispatch", "launch_prepare"}:
            claim = self._claim(queue_item)
            return self._launch_and_monitor(claim["queue_item"], claim)
        if action == "launch":
            claim = self._claim(queue_item)
            return self._launch_and_monitor(queue_item, claim)
        if action == "monitor":
            return self._monitor(queue_item)
        if action == "checkpoint":
            return self.run_checkpoint(queue_item)
        if action == "reconcile":
            launch_key = str(queue_item.get("launch_key") or "")
            child_id = str(queue_item.get("child_session_id") or "")
            if child_id:
                state = self.child_adapter.inspect(child_id)
            elif launch_key:
                assert self.workspace is not None
                state = self.child_adapter.create_or_get(
                    ChildLaunch(
                        launch_key=launch_key,
                        prompt=str(queue_item["prompt_snapshot"]),
                        model=model_from_queue(queue_item),
                        workspace_id=self.workspace.workspace_id,
                        repo_root=str(self.workspace.repo_root),
                    )
                )
            else:
                resolution = self._call(
                    "mudra_orchestration_reconcile",
                    {**self.identity, "queue_key": queue_item["queue_key"], "resolution": "claimed"},
                )
                return self.run_once() if resolution.get("pending") else resolution
            resolution = self._call(
                "mudra_orchestration_reconcile",
                {
                    **self.identity,
                    "queue_key": queue_item["queue_key"],
                    "resolution": "delegated",
                    "child_session_id": state.child_session_id,
                    "child_outcome": state.status,
                    "terminal_evidence": state.summary[:2_000],
                },
            )
            return self._monitor(resolution["queue_item"], state)
        raise RuntimeError(f"Unsupported reconcile action: {action}")

    def run(self) -> None:
        self.bootstrap()
        while not self._stop.is_set():
            result = self.run_once()
            if result.get("action") == "idle":
                self._stop.wait(self.config.idle_poll_interval_seconds)

    def drain(self) -> dict[str, Any]:
        if not self.assignment:
            return {"released": False, "duplicate": False}
        reconciled = self._call("mudra_orchestration_reconcile", self.identity)
        if reconciled.get("pending"):
            raise RuntimeError("Cannot drain while an orchestration attempt remains in flight")
        released = self._call("mudra_orchestration_release", self.identity)
        try:
            self._call(
                "mudra_task_check_out",
                {
                    "task_key": self.config.task_key,
                    "agent_id": self.config.agent_id,
                    "status": "done",
                },
            )
        finally:
            self.close()
        return released

    def close(self) -> None:
        self._stop.set()
        if self._maintenance_thread and self._maintenance_thread is not threading.current_thread():
            self._maintenance_thread.join(timeout=2.0)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Mudra MCP orchestration supervisor")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    config = RuntimeConfig.load(args.config)
    if not config.enabled:
        parser.error("mcp.orchestrator.enabled must be true before the supervisor can run")
    if config.adapter != "fake":
        parser.error("Only adapter: fake is available; real provider adapters require explicit approval")
    if config.checkpoint_adapter == "local_git":
        # Imported here because the adapter depends on this module's contracts.
        from mcp.git_checkpoint_adapter import LocalGitCheckpointAdapter

        checkpoint: CheckpointAdapter = LocalGitCheckpointAdapter(remote=config.checkpoint_remote)
    else:
        checkpoint = FakeCheckpointAdapter()
    runtime = OrchestratorRuntime(config, FakeChildSessionAdapter(), checkpoint)
    try:
        if args.once:
            runtime.run_once()
        else:
            runtime.run()
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())


__all__ = [
    "CheckpointAdapter",
    "CheckpointIntent",
    "CheckpointResult",
    "FakeCheckpointAdapter",
    "OrchestratorRuntime",
    "RuntimeConfig",
    "StructuredRuntimeLogger",
]
