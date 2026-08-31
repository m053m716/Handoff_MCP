"""Project identity and isolation.

The MCP server is meant to be reused across many local repositories from a
single installation and a single database. To keep repositories from seeing
each other's handoffs and todos, every record is tagged with a ``project_key``.

Resolution order for a working directory:

1. The Git top-level directory (``git rev-parse --show-toplevel``), if the path
   is inside a Git work tree. This is stable across branches and subdirectories.
2. Otherwise, the resolved absolute path of the working directory itself.

The resolved root path is hashed into a short, opaque, filesystem-safe key. A
human-readable ``project_label`` (the final path component) travels alongside it
for display only; it is never used for isolation.

Isolation is enforced in SQL: every query filters on ``project_key``. There is
deliberately no tool to list or cross into other projects.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Environment variable that pins the project root explicitly, overriding Git
#: detection. Useful when the server's working directory differs from the repo
#: (for example when launched by an editor from an arbitrary cwd).
PROJECT_ROOT_ENV_VAR = "HANDOFF_MCP_PROJECT_ROOT"

#: Environment variable that overrides the derived key outright. Set this to the
#: same value in two checkouts (e.g. a repo and its worktree) to share state.
PROJECT_KEY_ENV_VAR = "HANDOFF_MCP_PROJECT_KEY"


@dataclass(frozen=True)
class Project:
    """The isolation boundary for one repository / working tree."""

    key: str
    root: Path
    label: str

    def designation(self) -> dict[str, str]:
        return {"project_key": self.key, "project_root": str(self.root), "project_label": self.label}


def _git_toplevel(start: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    if not top:
        return None
    try:
        return Path(top).resolve()
    except OSError:
        return None


def _key_for(root: Path) -> str:
    # Case-fold on Windows so C:\Repo and c:\repo resolve to the same project.
    normalized = os.path.normcase(str(root))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest[:16]


def resolve_project(start: str | Path = ".") -> Project:
    """Resolve the active :class:`Project` for ``start`` (default: cwd)."""

    explicit_key = os.environ.get(PROJECT_KEY_ENV_VAR, "").strip()

    root_override = os.environ.get(PROJECT_ROOT_ENV_VAR, "").strip()
    if root_override:
        start_path = Path(root_override)
    else:
        start_path = Path(start)

    try:
        start_path = start_path.resolve()
    except OSError:
        start_path = Path(start).absolute()

    root = _git_toplevel(start_path) or start_path
    label = root.name or str(root)
    key = explicit_key or _key_for(root)
    return Project(key=key, root=root, label=label)


__all__ = [
    "PROJECT_ROOT_ENV_VAR",
    "PROJECT_KEY_ENV_VAR",
    "Project",
    "resolve_project",
]
