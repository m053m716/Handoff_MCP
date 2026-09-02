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

Scope configuration lives in the repo's project-local ``.mcp.json`` (written by
``handoff-mcp init``): the ``handoff`` server's ``env`` block pins
``HANDOFF_MCP_PROJECT_ROOT`` / ``HANDOFF_MCP_PROJECT_KEY``. The MCP client applies
that env to the ``serve`` process, but a separately launched ``handoff-mcp gui``
does not inherit it. So when those variables are absent from the process
environment, resolution falls back to reading them out of ``.mcp.json`` — this is
what makes the viewer scope to the same project the user configured, rather than
whatever git root the launch directory happens to sit in.
"""

from __future__ import annotations

import hashlib
import json
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

#: Project-local config file written by ``handoff-mcp init``. Its ``handoff``
#: server ``env`` block is the scope configuration the viewer reads back.
MCP_CONFIG_FILENAME = ".mcp.json"

#: Server name under ``mcpServers`` that this package registers itself as.
MCP_SERVER_NAME = "handoff"


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


def _config_search_roots(start: Path) -> list[Path]:
    """Directories to search for ``.mcp.json``, nearest first.

    The git top-level (where ``handoff-mcp init`` writes the file) is preferred,
    followed by ``start`` and each of its parents. Deduplicated, order preserved.
    """

    candidates: list[Path] = []
    top = _git_toplevel(start)
    if top is not None:
        candidates.append(top)
    candidates.append(start)
    candidates.extend(start.parents)

    seen: set[str] = set()
    ordered: list[Path] = []
    for candidate in candidates:
        marker = os.path.normcase(str(candidate))
        if marker not in seen:
            seen.add(marker)
            ordered.append(candidate)
    return ordered


def _handoff_env_from_config(start: Path) -> dict[str, str]:
    """Read the ``handoff`` server's ``env`` from the nearest ``.mcp.json``.

    Returns an empty dict when no config, no ``handoff`` entry, or no ``env`` is
    found, or when the file cannot be read/parsed. Only the two scope keys are
    returned; other environment entries are ignored.
    """

    for directory in _config_search_roots(start):
        config_path = directory / MCP_CONFIG_FILENAME
        try:
            raw = config_path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        entry = servers.get(MCP_SERVER_NAME)
        if not isinstance(entry, dict):
            continue
        env = entry.get("env")
        if not isinstance(env, dict):
            return {}
        scoped: dict[str, str] = {}
        for var in (PROJECT_ROOT_ENV_VAR, PROJECT_KEY_ENV_VAR):
            value = env.get(var)
            if isinstance(value, str) and value.strip():
                scoped[var] = value.strip()
        return scoped
    return {}


def resolve_project(start: str | Path = ".") -> Project:
    """Resolve the active :class:`Project` for ``start`` (default: cwd).

    The process environment wins. When ``HANDOFF_MCP_PROJECT_ROOT`` /
    ``HANDOFF_MCP_PROJECT_KEY`` are absent from it, their values are read back
    from the repo's ``.mcp.json`` ``handoff`` server ``env`` if present, so a
    hand-launched ``handoff-mcp gui`` scopes to the same project the client's
    ``serve`` process would. Only then does git detection of ``start`` apply.
    """

    start_path = Path(start)
    try:
        start_path = start_path.resolve()
    except OSError:
        start_path = Path(start).absolute()

    explicit_key = os.environ.get(PROJECT_KEY_ENV_VAR, "").strip()
    root_override = os.environ.get(PROJECT_ROOT_ENV_VAR, "").strip()

    # Fall back to the repo's committed .mcp.json scope for anything the ambient
    # environment did not already supply.
    if not explicit_key or not root_override:
        config_env = _handoff_env_from_config(start_path)
        if not root_override:
            root_override = config_env.get(PROJECT_ROOT_ENV_VAR, "")
        if not explicit_key:
            explicit_key = config_env.get(PROJECT_KEY_ENV_VAR, "")

    if root_override:
        root_start = Path(root_override)
        try:
            root_start = root_start.resolve()
        except OSError:
            root_start = Path(root_override).absolute()
    else:
        root_start = start_path

    root = _git_toplevel(root_start) or root_start
    label = root.name or str(root)
    key = explicit_key or _key_for(root)
    return Project(key=key, root=root, label=label)


__all__ = [
    "PROJECT_ROOT_ENV_VAR",
    "PROJECT_KEY_ENV_VAR",
    "MCP_CONFIG_FILENAME",
    "MCP_SERVER_NAME",
    "Project",
    "resolve_project",
]
