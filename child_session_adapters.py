"""Child-session adapter contracts and a deterministic reattachable fake."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol


TERMINAL_CHILD_STATUSES = {"completed", "failed", "cancelled"}


@dataclass(frozen=True)
class AdapterCapabilities:
    reattach_by_launch_key: bool = True
    cancel: bool = True


@dataclass(frozen=True)
class ChildLaunch:
    launch_key: str
    prompt: str
    model: dict[str, str]
    workspace_id: str
    repo_root: str


@dataclass(frozen=True)
class ChildSession:
    child_session_id: str
    launch_key: str
    status: str
    accepted_model: dict[str, str]
    summary: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_CHILD_STATUSES


class ChildSessionAdapter(Protocol):
    @property
    def capabilities(self) -> AdapterCapabilities: ...

    def create_or_get(self, launch: ChildLaunch) -> ChildSession: ...

    def inspect(self, child_session_id: str) -> ChildSession: ...

    def cancel(self, child_session_id: str) -> ChildSession: ...


@dataclass
class FakeChildSessionAdapter:
    """In-memory fake with provider-style create-or-get and reattach semantics."""

    terminal_status: str = "completed"
    terminal_summary: str = "Fake child completed."
    _sessions: dict[str, ChildSession] = field(default_factory=dict, init=False)
    _launches: dict[str, str] = field(default_factory=dict, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    create_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.terminal_status not in TERMINAL_CHILD_STATUSES | {"running"}:
            raise ValueError("terminal_status must be running, completed, failed, or cancelled")

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities()

    @property
    def sessions(self) -> dict[str, ChildSession]:
        with self._lock:
            return dict(self._sessions)

    def create_or_get(self, launch: ChildLaunch) -> ChildSession:
        if not launch.launch_key or not launch.prompt:
            raise ValueError("launch_key and prompt are required")
        with self._lock:
            existing_id = self._launches.get(launch.launch_key)
            if existing_id:
                return self._sessions[existing_id]
            digest = hashlib.sha256(launch.launch_key.encode("utf-8")).hexdigest()[:20]
            child_id = f"fake-child-{digest}"
            session = ChildSession(
                child_session_id=child_id,
                launch_key=launch.launch_key,
                status=self.terminal_status,
                accepted_model={key: str(value or "") for key, value in launch.model.items()},
                summary=self.terminal_summary,
            )
            self._launches[launch.launch_key] = child_id
            self._sessions[child_id] = session
            self.create_count += 1
            return session

    def inspect(self, child_session_id: str) -> ChildSession:
        with self._lock:
            try:
                return self._sessions[child_session_id]
            except KeyError as exc:
                raise LookupError(f"Unknown child session: {child_session_id}") from exc

    def cancel(self, child_session_id: str) -> ChildSession:
        with self._lock:
            current = self.inspect(child_session_id)
            if current.terminal:
                return current
            cancelled = ChildSession(
                child_session_id=current.child_session_id,
                launch_key=current.launch_key,
                status="cancelled",
                accepted_model=current.accepted_model,
                summary="Fake child cancellation acknowledged.",
            )
            self._sessions[child_session_id] = cancelled
            return cancelled

    def set_status(self, child_session_id: str, status: str, summary: str = "") -> ChildSession:
        if status not in TERMINAL_CHILD_STATUSES | {"running"}:
            raise ValueError("Unsupported child status")
        with self._lock:
            current = self.inspect(child_session_id)
            updated = ChildSession(
                child_session_id=current.child_session_id,
                launch_key=current.launch_key,
                status=status,
                accepted_model=current.accepted_model,
                summary=summary,
            )
            self._sessions[child_session_id] = updated
            return updated


def model_from_queue(queue_item: dict[str, Any]) -> dict[str, str]:
    return {
        "model_registration_key": str(queue_item.get("model_registration_key") or ""),
        "provider": str(queue_item.get("provider") or ""),
        "model_family": str(queue_item.get("model_family") or ""),
        "model_version": str(queue_item.get("model_version") or ""),
        "model_variant": str(queue_item.get("model_variant") or ""),
        "reasoning_effort": str(queue_item.get("reasoning_effort") or ""),
        "client_name": str(queue_item.get("client_name") or ""),
    }


__all__ = [
    "AdapterCapabilities",
    "ChildLaunch",
    "ChildSession",
    "ChildSessionAdapter",
    "FakeChildSessionAdapter",
    "TERMINAL_CHILD_STATUSES",
    "model_from_queue",
]
