"""Client-local Git checkpoint adapter for the orchestration runtime.

The adapter runs only after successful child completion and a server
``mudra_orchestration_checkpoint_prepare``. It re-verifies the workspace, the
exact authorized branch, HEAD, the complete dirty status, and the authorized
relative paths before it stages anything, then executes ``status``/``diff``/
``add``/``commit``/``push`` with argument arrays and bounded redacted output.

Non-goals, enforced here rather than left to a caller: the adapter never
broadens ``allowed_paths``, stashes, resets or reverts user changes,
force-pushes, stores or reads credentials, or constructs a shell command
string. Push uses the user's existing Git credential helper through the plain
``git push`` invocation.
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from mcp.orchestrator_runtime import CheckpointIntent, CheckpointResult
from mcp.workspace_identity import WorkspaceIdentity


COMMAND_TIMEOUT_SECONDS = 120
MAX_EVIDENCE_CHARS = 2_000
MAX_GIT_OUTPUT_CHARS = 4_000
COMMIT_HASH_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")

CommandRunner = Callable[..., "subprocess.CompletedProcess[str]"]


class CheckpointCancelled(RuntimeError):
    """Cooperative stop was requested before the checkpoint reached a stage boundary."""


class CheckpointAbort(RuntimeError):
    """The checkpoint cannot proceed safely; it is recorded as a bounded failure."""


def redact_git_output(value: str) -> str:
    """Strip credential-shaped text with the same patterns the server applies."""

    redacted = re.sub(r"(https?://)[^/@\s]+@", r"\1<redacted>@", value)
    redacted = re.sub(r"(?i)(token|password|secret)=([^\s]+)", r"\1=<redacted>", redacted)
    return redacted[:MAX_GIT_OUTPUT_CHARS]


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def failure_text(self) -> str:
        return (self.stderr or self.stdout or "Git command failed.").strip()


@dataclass(frozen=True)
class StatusEntry:
    status: str
    path: str


def _normalize_allowed(paths: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in paths:
        candidate = str(raw).strip().replace("\\", "/").strip("/")
        if candidate and candidate not in normalized:
            normalized.append(candidate)
    return tuple(normalized)


def path_allowed(path: str, allowed_paths: Sequence[str]) -> bool:
    normalized = path.strip().replace("\\", "/").strip("/")
    if not normalized:
        return False
    return any(
        normalized == root or normalized.startswith(root + "/") for root in allowed_paths
    )


@dataclass
class LocalGitCheckpointAdapter:
    """Execute one bounded Git checkpoint inside the runtime's own worktree."""

    remote: str = "origin"
    message_prefix: str = "Task complete"
    runner: CommandRunner = subprocess.run
    stop_event: threading.Event = field(default_factory=threading.Event)
    _results: dict[str, CheckpointResult] = field(default_factory=dict, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    execution_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.remote or self.remote.startswith("-"):
            raise ValueError("remote must be a configured Git remote name")

    # ------------------------------------------------------------------ git

    def _git(self, repo_root: Path, args: Sequence[str]) -> GitResult:
        completed = self.runner(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
        return GitResult(
            returncode=int(completed.returncode),
            stdout=redact_git_output(completed.stdout or ""),
            stderr=redact_git_output(completed.stderr or ""),
        )

    def _require(self, repo_root: Path, args: Sequence[str], failure: str) -> GitResult:
        result = self._git(repo_root, args)
        if not result.ok:
            raise CheckpointAbort(f"{failure}: {result.failure_text}")
        return result

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise CheckpointCancelled("Cooperative stop was requested.")

    # ------------------------------------------------------------ inspection

    def _status_entries(self, repo_root: Path) -> list[StatusEntry]:
        result = self._require(
            repo_root,
            ["status", "--porcelain=v1", "--untracked-files=all"],
            "Unable to read the working tree status",
        )
        entries: list[StatusEntry] = []
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            raw_path = line[3:]
            if " -> " in raw_path:
                raw_path = raw_path.split(" -> ", 1)[1]
            entries.append(StatusEntry(status=line[:2], path=raw_path.strip('"')))
        return entries

    def _head(self, repo_root: Path) -> str:
        result = self._git(repo_root, ["rev-parse", "HEAD"])
        # An unborn branch has no HEAD yet; that is a valid starting state.
        return result.stdout.strip() if result.ok else ""

    def _verify_workspace(
        self, intent: CheckpointIntent, workspace: WorkspaceIdentity
    ) -> None:
        if intent.workspace_id != workspace.workspace_id:
            raise CheckpointAbort("The checkpoint intent targets a different workspace.")
        repo_root = workspace.repo_root
        toplevel = self._require(
            repo_root, ["rev-parse", "--show-toplevel"], "Workspace is not a Git worktree"
        ).stdout.strip()
        if Path(toplevel).resolve() != Path(repo_root).resolve():
            raise CheckpointAbort("The workspace path no longer resolves to its recorded repository root.")
        branch = self._require(
            repo_root, ["branch", "--show-current"], "Unable to read the current branch"
        ).stdout.strip()
        current = branch or "HEAD"
        if current != intent.expected_branch:
            raise CheckpointAbort(
                f"Authorized branch `{intent.expected_branch}` is not checked out (found `{current}`)."
            )

    # -------------------------------------------------------------- staging

    def _authorized_changes(
        self, repo_root: Path, allowed_paths: Sequence[str]
    ) -> list[str]:
        """Return authorized dirty paths, refusing when unrelated changes exist."""

        entries = self._status_entries(repo_root)
        unrelated = sorted(
            {entry.path for entry in entries if not path_allowed(entry.path, allowed_paths)}
        )
        if unrelated:
            raise CheckpointAbort(
                "Unrelated working-tree changes block the checkpoint: " + ", ".join(unrelated[:20])
            )
        return sorted({entry.path for entry in entries})

    def _stage(
        self, repo_root: Path, allowed_paths: Sequence[str], dirty: Sequence[str]
    ) -> list[str]:
        # Stage the exact authorized dirty paths rather than the allowlist
        # roots: an allowed root that does not exist in this checkout would make
        # `git add` fail with a pathspec error and turn every checkpoint into a
        # spurious failure.
        self._require(
            repo_root,
            ["add", "--", *dirty],
            "Unable to stage the authorized checkpoint paths",
        )
        staged = self._require(
            repo_root, ["diff", "--cached", "--name-only"], "Unable to inspect staged paths"
        )
        paths = [line.strip() for line in staged.stdout.splitlines() if line.strip()]
        unauthorized = sorted({path for path in paths if not path_allowed(path, allowed_paths)})
        if unauthorized:
            raise CheckpointAbort(
                "Unauthorized staged paths block the checkpoint: " + ", ".join(unauthorized[:20])
            )
        return paths

    # --------------------------------------------------------------- stages

    def _commit(self, intent: CheckpointIntent, repo_root: Path) -> CheckpointResult:
        allowed = _normalize_allowed(intent.allowed_paths)
        if not allowed:
            raise CheckpointAbort("The checkpoint intent carries no authorized paths.")
        dirty = self._authorized_changes(repo_root, allowed)
        head_before = self._head(repo_root)
        if not dirty:
            return CheckpointResult(
                "no_changes",
                evidence_summary="The authorized working tree is clean; no commit was created.",
            )
        self._check_stop()
        staged = self._stage(repo_root, allowed, dirty)
        if not staged:
            return CheckpointResult(
                "no_changes",
                evidence_summary="Authorized paths produced no staged changes.",
            )
        self._check_stop()
        message = f"{self.message_prefix}: {intent.queue_key}"
        commit = self._git(repo_root, ["commit", "-m", message])
        if not commit.ok:
            return CheckpointResult(
                "failed",
                evidence_summary=self._evidence("Checkpoint commit failed", commit.failure_text),
            )
        head_after = self._head(repo_root)
        if not COMMIT_HASH_PATTERN.match(head_after) or head_after == head_before:
            return CheckpointResult(
                "failed",
                evidence_summary="The commit did not advance HEAD to a readable commit.",
            )
        return CheckpointResult(
            "success",
            commit_hash=head_after,
            evidence_summary=self._evidence(
                f"Committed {len(staged)} authorized path(s)", ", ".join(staged[:20])
            ),
        )

    def _push(
        self, intent: CheckpointIntent, repo_root: Path, commit_hash: str
    ) -> CheckpointResult:
        head = self._head(repo_root)
        if head != commit_hash:
            return CheckpointResult(
                "failed",
                commit_hash=commit_hash,
                evidence_summary="HEAD no longer matches the recorded checkpoint commit; resolve the workspace before retrying the push.",
            )
        push_target = f"{self.remote}/{intent.expected_branch}"
        # No force flag, no refspec built from untrusted text, no credential
        # handling: the user's configured helper answers for this remote.
        pushed = self._git(
            repo_root, ["push", self.remote, f"HEAD:{intent.expected_branch}"]
        )
        if not pushed.ok:
            return CheckpointResult(
                "failed",
                commit_hash=commit_hash,
                push_target=push_target,
                evidence_summary=self._evidence("Checkpoint push failed", pushed.failure_text),
            )
        return CheckpointResult(
            "success",
            commit_hash=commit_hash,
            push_target=push_target,
            evidence_summary=f"Pushed {commit_hash[:12]} to {push_target}.",
        )

    @staticmethod
    def _evidence(headline: str, detail: str) -> str:
        text = f"{headline}: {detail}" if detail else headline
        return redact_git_output(text)[:MAX_EVIDENCE_CHARS]

    # ---------------------------------------------------------------- entry

    def execute(
        self, intent: CheckpointIntent, workspace: WorkspaceIdentity
    ) -> CheckpointResult:
        with self._lock:
            cached = self._results.get(intent.checkpoint_key)
            if cached is not None:
                return cached
            self.execution_count += 1
            try:
                result = self._execute(intent, workspace)
            except CheckpointCancelled:
                # Cooperative cancellation is not a recorded terminal failure;
                # the runtime keeps the checkpoint resumable.
                raise
            except CheckpointAbort as exc:
                result = CheckpointResult(
                    "failed", evidence_summary=self._evidence("Checkpoint aborted", str(exc))
                )
            except subprocess.TimeoutExpired:
                result = CheckpointResult(
                    "failed",
                    evidence_summary="A Git command exceeded the checkpoint time limit.",
                )
            # Only terminal push-stage results are memoized. A resumable failed
            # push keeps its commit, and the runtime replays through prepare.
            if result.result != "failed":
                self._results[intent.checkpoint_key] = result
            return result

    def _execute(
        self, intent: CheckpointIntent, workspace: WorkspaceIdentity
    ) -> CheckpointResult:
        self._check_stop()
        self._verify_workspace(intent, workspace)
        repo_root = Path(workspace.repo_root)
        if intent.resume_stage == "push":
            if not intent.recorded_commit:
                raise CheckpointAbort("A push resume requires the recorded checkpoint commit.")
            return self._push(intent, repo_root, intent.recorded_commit)
        committed = self._commit(intent, repo_root)
        if committed.result != "success":
            return committed
        self._check_stop()
        return self._push(intent, repo_root, committed.commit_hash)


__all__ = [
    "CheckpointAbort",
    "CheckpointCancelled",
    "GitResult",
    "LocalGitCheckpointAdapter",
    "StatusEntry",
    "path_allowed",
    "redact_git_output",
]
