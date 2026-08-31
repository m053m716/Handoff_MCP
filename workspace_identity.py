"""Repo-local opaque identity for one Git checkout or linked worktree."""

from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


IDENTITY_FILENAME = "mudra-orchestrator-workspace-id"


class WorkspaceIdentityError(RuntimeError):
    """The requested path is not a usable Git worktree."""


@dataclass(frozen=True)
class WorkspaceIdentity:
    workspace_id: str
    repo_root: Path
    git_dir: Path
    branch: str
    detached: bool = False

    def designation(self) -> dict[str, str]:
        return {
            "workspace_id": self.workspace_id,
            "repo_root": str(self.repo_root),
            "branch": self.branch,
        }


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _git(
    runner: CommandRunner,
    cwd: Path,
    args: Sequence[str],
) -> str:
    result = runner(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Git command failed").strip()[:500]
        raise WorkspaceIdentityError(detail)
    return result.stdout.strip()


def _read_uuid(path: Path) -> str | None:
    try:
        return str(uuid.UUID(path.read_text(encoding="ascii").strip()))
    except (OSError, UnicodeError, ValueError):
        return None


def _create_or_replace_uuid(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(4):
        candidate = str(uuid.uuid4())
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            current = _read_uuid(path)
            if current:
                return current
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(candidate + "\n", encoding="ascii")
            os.replace(temporary, path)
            return candidate
        else:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(candidate + "\n")
            return candidate
    raise WorkspaceIdentityError("Could not create the workspace identity file.")


def resolve_workspace_identity(
    start: str | Path = ".",
    *,
    runner: CommandRunner = subprocess.run,
) -> WorkspaceIdentity:
    """Resolve one exact worktree and persist its opaque identity in Git metadata."""

    start_path = Path(start).resolve()
    repo_root = Path(_git(runner, start_path, ["rev-parse", "--show-toplevel"])).resolve()
    raw_git_dir = Path(_git(runner, repo_root, ["rev-parse", "--git-dir"]))
    git_dir = (repo_root / raw_git_dir).resolve() if not raw_git_dir.is_absolute() else raw_git_dir.resolve()
    branch = _git(runner, repo_root, ["branch", "--show-current"])
    detached = not bool(branch)
    if detached:
        branch = "HEAD"
    identity_path = git_dir / IDENTITY_FILENAME
    workspace_id = _read_uuid(identity_path) or _create_or_replace_uuid(identity_path)
    return WorkspaceIdentity(
        workspace_id=workspace_id,
        repo_root=repo_root,
        git_dir=git_dir,
        branch=branch,
        detached=detached,
    )


__all__ = [
    "IDENTITY_FILENAME",
    "WorkspaceIdentity",
    "WorkspaceIdentityError",
    "resolve_workspace_identity",
]
