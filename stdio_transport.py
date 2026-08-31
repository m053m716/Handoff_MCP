"""Line-delimited JSON MCP stdio transport."""

from __future__ import annotations

import json
import sys
from typing import Any

from mcp.auth import RemoteAuthPolicy, SERVICE_TOKEN_IDS_ENV_VAR
from mcp.project import load_project_descriptor


def configure_stdio_utf8() -> None:
    # MCP stdio is UTF-8; Windows redirected streams otherwise use the ANSI code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, TypeError, ValueError):
            continue


def run_stdio(server: Any) -> None:
    """Run the compatibility facade over the line-delimited stdio protocol."""
    configure_stdio_utf8()
    server.ensure_schema()
    project = server.project
    print(
        f"{project.server_name} stdio server ready; using {server.db_path}",
        file=sys.stderr,
        flush=True,
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = server.error_response(None, -32700, "Parse error")
        else:
            response = server.handle_jsonrpc(message)
        if response is not None and response != []:
            print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)


def announce_remote_auth(remote_auth: RemoteAuthPolicy | None, *, project: Any | None = None) -> None:
    if remote_auth is None or not remote_auth.enabled:
        return
    if project is None:
        project = load_project_descriptor()
    if remote_auth.service_token_ids:
        print(
            f"{project.server_name} remote auth mode '{remote_auth.mode}' active with "
            f"{len(remote_auth.service_token_ids)} allowlisted service token(s).",
            file=sys.stderr,
            flush=True,
        )
        return
    print(
        f"{project.server_name} remote auth mode '{remote_auth.mode}' is enabled with an "
        f"empty service-token allowlist; every request will be denied until "
        f"{SERVICE_TOKEN_IDS_ENV_VAR} or --service-token-id is set.",
        file=sys.stderr,
        flush=True,
    )
