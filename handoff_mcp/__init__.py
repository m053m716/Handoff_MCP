"""Minimal, project-scoped TODO / Handoff MCP server.

A small stdio Model Context Protocol server backed by a single SQLite database.
Every record is scoped to a ``project_key`` derived from the Git repository root
(or the working-directory path when the tree is not a Git repo), so the same
server and database can be reused across many local repositories with strict
isolation between them.

No network or HTTP components are used for the MCP transport. A minimal
loopback-only viewer (``handoff-mcp gui``) is provided purely so a human can
inspect outstanding handoffs / todos and copy them into worker sessions.
"""

from __future__ import annotations

__version__ = "0.2.0"

SERVER_NAME = "handoff-mcp"

__all__ = ["__version__", "SERVER_NAME"]
