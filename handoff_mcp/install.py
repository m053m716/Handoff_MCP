"""``handoff-mcp-install`` — register the server in the global user configs.

Installed as a console script by the ``[dev]`` extra. It upserts the ``handoff``
MCP server entry into:

* ``~/.claude.json``            (Claude Code global MCP servers)
* ``~/.codex/config.toml``      (Codex CLI global MCP servers)

The global entries define the server everywhere without pinning a repo root, so
scope falls back to Git detection from the launch directory (or to whatever a
project-local ``.mcp.json`` / ``.codex/config.toml`` pins). Both writes
merge/upsert: an existing ``handoff`` entry is replaced in place and any other
servers or tables are preserved.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import write_codex_config, write_json_config


def _claude_config_path(home: Path) -> Path:
    return home / ".claude.json"


def _codex_config_path(home: Path) -> Path:
    return home / ".codex" / "config.toml"


def install_global(home: Path, *, project_root: str | None = None) -> list[Path]:
    """Upsert the handoff server into the global Claude and Codex configs.

    ``project_root`` pins ``HANDOFF_MCP_PROJECT_ROOT`` in both entries; the
    default (``None``) leaves scope to Git detection. Returns the paths written.
    Raises the underlying error (``OSError``/``ValueError``) if a file cannot be
    read or parsed, so an existing config is never clobbered blindly.
    """

    claude_path = _claude_config_path(home)
    codex_path = _codex_config_path(home)

    # Global entries are not tied to a single trusted project.
    write_json_config(claude_path, project_root)
    write_codex_config(codex_path, project_root, trust=False)
    return [claude_path, codex_path]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handoff-mcp-install",
        description=(
            "Register the handoff MCP server in the global user configs "
            "(~/.claude.json and ~/.codex/config.toml)."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"handoff-mcp {__version__}"
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help=(
            "Pin HANDOFF_MCP_PROJECT_ROOT to this path in both global entries. "
            "By default the global entries omit it and scope falls back to Git "
            "detection (or a project-local config)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = Path.home()
    try:
        written = install_global(home, project_root=args.project_root)
    except (OSError, ValueError) as exc:
        print(f"handoff-mcp-install: {exc}", file=sys.stderr)
        return 1

    print("Registered the 'handoff' MCP server globally.")
    for path in written:
        print(f"  Wrote {path}")
    if args.project_root:
        print(f"\n  Pinned HANDOFF_MCP_PROJECT_ROOT = {args.project_root}")
    else:
        print(
            "\n  No project root pinned; scope resolves from Git detection or a "
            "project-local config."
        )
    print(
        "\nCodex loads global servers directly; Claude Code reads ~/.claude.json. "
        "Per-repo scope still comes from 'handoff-mcp init'."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
