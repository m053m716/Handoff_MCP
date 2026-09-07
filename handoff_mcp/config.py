"""Writing the ``handoff`` server entry into client config files.

Two clients are supported, each with a JSON and a TOML config surface:

* **Claude Code** reads JSON — a project-local ``.mcp.json`` (written by
  ``handoff-mcp init``) or the global ``~/.claude.json``.
* **Codex CLI** reads TOML — a project-local ``.codex/config.toml`` or the
  global ``~/.codex/config.toml``.

This module centralises building the server entry and merging it into an
existing file without disturbing unrelated content. The project has no runtime
dependencies, so a small, purpose-built TOML reader/writer handles the narrow
shape we need (an ``[mcp_servers.handoff]`` table plus a nested ``env`` table);
it is not a general TOML implementation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .project import MCP_SERVER_NAME, PROJECT_ROOT_ENV_VAR

#: Command the client spawns for the stdio server.
SERVER_COMMAND = "handoff-mcp"
SERVER_ARGS = ["serve"]

#: TOML table prefix for the Codex server entry.
_CODEX_TABLE = f"mcp_servers.{MCP_SERVER_NAME}"


def json_server_entry(project_root: str | None) -> dict:
    """The ``mcpServers.handoff`` object for a JSON (Claude Code) config.

    ``project_root`` pins ``HANDOFF_MCP_PROJECT_ROOT`` in the server's ``env``;
    pass ``None`` for a global entry that falls back to Git detection.
    """

    entry: dict = {
        "type": "stdio",
        "command": SERVER_COMMAND,
        "args": list(SERVER_ARGS),
    }
    if project_root is not None:
        entry["env"] = {PROJECT_ROOT_ENV_VAR: project_root}
    return entry


def merge_json_config(existing: dict, project_root: str | None) -> dict:
    """Return ``existing`` with the ``handoff`` server merged in.

    Mutates and returns the same dict. The caller is responsible for reading and
    writing the file; this only touches ``mcpServers.handoff``.
    """

    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("'mcpServers' is not an object")
    servers[MCP_SERVER_NAME] = json_server_entry(project_root)
    return existing


def render_json_config(existing: dict, project_root: str | None) -> str:
    """Serialise a merged JSON config to text (two-space indent, trailing NL)."""

    merged = merge_json_config(existing, project_root)
    return json.dumps(merged, indent=2) + "\n"


# --- Minimal TOML support (Codex config) ---------------------------------

def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def codex_server_block(project_root: str | None, *, trust: bool) -> str:
    """The ``[mcp_servers.handoff]`` TOML block for a Codex config.

    ``trust`` adds ``trust_level = "trusted"`` (used for a per-repo config so
    Codex loads it without a separate prompt). ``project_root`` pins
    ``HANDOFF_MCP_PROJECT_ROOT`` in an ``env`` sub-table; ``None`` omits it.
    """

    lines = [
        f"[{_CODEX_TABLE}]",
        f'type = "stdio"',
        f'command = "{_toml_escape(SERVER_COMMAND)}"',
        f"args = [{', '.join(chr(34) + _toml_escape(a) + chr(34) for a in SERVER_ARGS)}]",
    ]
    if trust:
        lines.append('trust_level = "trusted"')
    if project_root is not None:
        lines.append("")
        lines.append(f"[{_CODEX_TABLE}.env]")
        lines.append(f'{PROJECT_ROOT_ENV_VAR} = "{_toml_escape(project_root)}"')
    return "\n".join(lines) + "\n"


#: Matches our own ``[mcp_servers.handoff]`` table and its ``.env`` sub-table,
#: consuming through to the next top-level ``[...]`` header or end of file. Used
#: to replace a prior handoff block in place while leaving other tables intact.
_HANDOFF_BLOCK_RE = re.compile(
    r"(?ms)^[ \t]*\[" + re.escape(_CODEX_TABLE) + r"(?:\.[^\]]*)?\]\s*$.*?"
    r"(?=^\s*\[(?!" + re.escape(_CODEX_TABLE) + r"[.\]])|\Z)"
)


def merge_codex_config(existing_text: str, project_root: str | None, *, trust: bool) -> str:
    """Return ``existing_text`` with the handoff block added or replaced.

    Any existing ``[mcp_servers.handoff]`` table (and its ``.env`` sub-table) is
    removed and the freshly rendered block appended. Content outside the handoff
    tables is preserved verbatim. An empty/whitespace input yields just the block.
    """

    block = codex_server_block(project_root, trust=trust)
    stripped = _HANDOFF_BLOCK_RE.sub("", existing_text).strip()
    if not stripped:
        return block
    return stripped + "\n\n" + block


# --- Agent usage guidance (AGENTS.md / README.md) ------------------------

#: Markers that delimit the block this tool owns, so re-running upserts it in
#: place instead of appending a second copy. Anything between them is replaced;
#: anything outside is left untouched.
GUIDANCE_BEGIN = "<!-- handoff-mcp:begin (managed by `handoff-mcp init --vscode`) -->"
GUIDANCE_END = "<!-- handoff-mcp:end -->"

#: Concise usage guidance for an agent working in the repo. Kept short on
#: purpose: the exhaustive reference lives in docs/TOOL_GUIDE.md.
GUIDANCE_BODY = """\
## Handoff MCP — session memory for agents

This repo has the `handoff` MCP server configured (see `.mcp.json` /
`.codex/config.toml`). It is a durable, project-scoped place to leave
breadcrumbs between sessions. Use it instead of re-deriving context.

**At the start of a session**, call `handoff_list` to reload where prior work
stopped and what to do next, and `todo_list` for outstanding next steps. This is
cheaper and more reliable than re-reading the whole transcript.

**While working**, when you find something that must be done but is not the
current focus, call `todo_add` rather than holding it in the conversation.

**When context gets heavy** (stale greps, large logs, finished sub-tasks pile
up), call `context_report`, then `context_compact` — it returns a
summarise-then-handoff procedure and can persist the summary as a handoff in one
call.

**At the end of a work chunk**, call `handoff_add` with a summary, next steps,
and the few key facts (file paths, decisions, gotchas) the next worker needs. A
fresh session can then resume from `handoff_list` alone.

**Close the loop** with `todo_update` (done/dropped) and `handoff_resolve` so
the open lists stay a true worklist.

| Tool | Use it to |
| --- | --- |
| `handoff_list` | Reload breadcrumbs at session start. |
| `handoff_add` | Record where you stopped and what is next. |
| `handoff_resolve` | Mark a handoff done. |
| `todo_add` / `todo_list` / `todo_update` | Track next-step TODOs. |
| `project_status` | Counts of open todos and handoffs. |
| `context_report` / `context_compact` | Notice and shrink a bloated context window. |

Every tool is scoped to this project only; there is no way to reach another
project's data. Full reference: `docs/TOOL_GUIDE.md`.\
"""


def guidance_block() -> str:
    """The full marker-delimited guidance block (no surrounding blank lines)."""

    return f"{GUIDANCE_BEGIN}\n{GUIDANCE_BODY}\n{GUIDANCE_END}"


def _upsert_guidance(existing_text: str) -> str:
    """Return ``existing_text`` with the guidance block added or replaced.

    If a prior block (delimited by the markers) is present it is replaced in
    place; otherwise the block is appended. Content outside the markers is
    preserved verbatim.
    """

    block = guidance_block()
    begin = existing_text.find(GUIDANCE_BEGIN)
    if begin != -1:
        end = existing_text.find(GUIDANCE_END, begin)
        if end != -1:
            end += len(GUIDANCE_END)
            return existing_text[:begin] + block + existing_text[end:]

    base = existing_text.rstrip()
    if not base:
        return block + "\n"
    return base + "\n\n" + block + "\n"


def write_agent_guidance(path: Path, *, create_if_missing: bool) -> bool:
    """Add/replace the handoff guidance block in the Markdown file at ``path``.

    Returns ``True`` if the file was written, ``False`` if it was skipped
    (``create_if_missing`` is False and the file does not exist). The block is
    upserted between its markers, so unrelated content is preserved and repeated
    runs do not duplicate it.
    """

    if path.exists():
        existing = path.read_text(encoding="utf-8")
    elif create_if_missing:
        existing = ""
    else:
        return False

    updated = _upsert_guidance(existing)
    if updated == existing:
        return True  # already current; nothing to change but treated as present
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return True


# --- File-writing helpers ------------------------------------------------

def write_json_config(path: Path, project_root: str | None) -> None:
    """Merge the handoff server into the JSON config at ``path`` and write it.

    Creates parent directories as needed. Raises ``ValueError`` on unreadable or
    malformed existing content so the caller can report it without clobbering.
    """

    existing: dict = {}
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        if raw.strip():
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"{path} is not a JSON object")
            existing = data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_json_config(existing, project_root), encoding="utf-8")


def write_codex_config(path: Path, project_root: str | None, *, trust: bool) -> None:
    """Merge the handoff server into the Codex TOML config at ``path``.

    Creates parent directories as needed. Preserves unrelated tables.
    """

    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(merge_codex_config(existing_text, project_root, trust=trust), encoding="utf-8")


__all__ = [
    "SERVER_COMMAND",
    "SERVER_ARGS",
    "json_server_entry",
    "merge_json_config",
    "render_json_config",
    "codex_server_block",
    "merge_codex_config",
    "write_json_config",
    "write_codex_config",
    "GUIDANCE_BEGIN",
    "GUIDANCE_END",
    "guidance_block",
    "write_agent_guidance",
]
