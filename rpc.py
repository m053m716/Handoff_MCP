"""Command-line compatibility wrapper for the reusable MCP tool client."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mcp.state_sync import (
    CF_ACCESS_CLIENT_ID_ENV_VARS,
    CF_ACCESS_CLIENT_ID_HEADER,
    CF_ACCESS_CLIENT_SECRET_ENV_VARS,
    CF_ACCESS_CLIENT_SECRET_HEADER,
    HttpError,
    SyncError,
    pretty_json,
)
from mcp.tool_client import (
    DEFAULT_TIMEOUT,
    FALLTHROUGH_HTTP_STATUSES,
    PROJECT,
    ToolCallError,
    cloudflare_headers_available,
    compactify,
    endpoint_candidates,
    headers_for,
    is_remote_url,
    remote_candidates,
    rpc_request as _rpc_request,
    rpc_request_with_fallback as _rpc_request_with_fallback,
    unwrap_tool_result,
)


def rpc_request(
    base_url: str,
    method: str,
    params: dict[str, Any] | None,
    timeout: float,
) -> Any:
    """Compatibility entry point retained for existing CLI integrations."""

    return _rpc_request(base_url, method, params, timeout)


def rpc_request_with_fallback(
    candidates: list[str],
    method: str,
    params: dict[str, Any] | None,
    timeout: float,
    errors_out: list[str] | None = None,
) -> tuple[str, Any]:
    """Use the compatibility request hook so existing monkeypatches keep working."""

    return _rpc_request_with_fallback(
        candidates,
        method,
        params,
        timeout,
        errors_out,
        request_fn=rpc_request,
    )


def list_tools(candidates: list[str], timeout: float) -> tuple[str, list[dict[str, Any]]]:
    base_url, response = rpc_request_with_fallback(candidates, "tools/list", {}, timeout)
    if isinstance(response, dict) and "error" in response:
        raise SyncError(f"tools/list failed: {compactify(response['error'])}")
    result = response.get("result") if isinstance(response, dict) else None
    tools = result.get("tools", []) if isinstance(result, dict) else []
    return base_url, [dict(tool) for tool in tools if isinstance(tool, dict)]


def note_endpoint(base_url: str) -> None:
    print(f"[mudra-rpc] endpoint: {base_url}", file=sys.stderr)


def load_call_arguments(args: argparse.Namespace) -> dict[str, Any]:
    if args.args is not None and args.args_file is not None:
        raise SyncError("Pass --args or --args-file, not both.")
    if args.args_file is not None:
        raw = sys.stdin.read() if args.args_file == "-" else Path(args.args_file).read_text(encoding="utf-8")
    else:
        raw = args.args if args.args is not None else "{}"
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SyncError(f"Tool arguments are not valid JSON: {exc}") from None
    if not isinstance(arguments, dict):
        raise SyncError("Tool arguments must be a JSON object.")
    return arguments


def cmd_status(args: argparse.Namespace) -> int:
    candidates = endpoint_candidates(args.url, args.local)
    report: dict[str, Any] = {
        "candidates": candidates,
        "cloudflare_credentials_present": cloudflare_headers_available(),
    }
    skipped: list[str] = []
    try:
        base_url, _ = rpc_request_with_fallback(candidates, "ping", {}, args.timeout, errors_out=skipped)
        report["endpoint"] = base_url
        report["remote"] = is_remote_url(base_url)
        report["reachable"] = True
    except SyncError as exc:
        report["reachable"] = False
        report["error"] = str(exc)
    if skipped:
        report["skipped_candidates"] = skipped
    print(pretty_json(report))
    return 0 if report["reachable"] else 2


def cmd_tools(args: argparse.Namespace) -> int:
    base_url, tools = list_tools(endpoint_candidates(args.url, args.local), args.timeout)
    note_endpoint(base_url)
    if args.names:
        for tool in tools:
            print(tool.get("name", ""))
    else:
        print(pretty_json([
            {"name": tool.get("name", ""), "description": tool.get("description", "")}
            for tool in tools
        ]))
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    base_url, tools = list_tools(endpoint_candidates(args.url, args.local), args.timeout)
    note_endpoint(base_url)
    by_name = {str(tool.get("name", "")): tool for tool in tools}
    missing = [name for name in args.tool if name not in by_name]
    if missing:
        raise SyncError(
            f"Unknown tool(s): {', '.join(missing)}. Available: {', '.join(sorted(by_name))}"
        )
    selected = [by_name[name] for name in args.tool]
    print(pretty_json(selected[0] if len(selected) == 1 else selected))
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    arguments = load_call_arguments(args)
    candidates = endpoint_candidates(args.url, args.local)
    base_url, response = rpc_request_with_fallback(
        candidates,
        "tools/call",
        {"name": args.tool, "arguments": arguments},
        args.timeout,
    )
    note_endpoint(base_url)
    if args.raw:
        print(pretty_json(response))
        return 0
    print(pretty_json(unwrap_tool_result(args.tool, response)))
    return 0


def add_global_options(parser: argparse.ArgumentParser, *, on_subcommand: bool) -> None:
    suppress = {"default": argparse.SUPPRESS} if on_subcommand else {}
    parser.add_argument("--url", help="Explicit MCP base URL (JSON-RPC is posted to <url>/mcp).", **suppress)
    parser.add_argument(
        "--local",
        action="store_true",
        help="Try config.yaml local candidates before the remote deployment.",
        **suppress,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help="Per-request timeout in seconds.",
        **(suppress if on_subcommand else {"default": DEFAULT_TIMEOUT}),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mcp.rpc",
        description=f"JSON-RPC client for the {PROJECT.server_name} server (remote or local).",
    )
    add_global_options(parser, on_subcommand=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Resolve and ping the MCP endpoint.")
    add_global_options(status, on_subcommand=True)
    status.set_defaults(handler=cmd_status)

    tools = subparsers.add_parser("tools", help="List available tools.")
    add_global_options(tools, on_subcommand=True)
    tools.add_argument("--names", action="store_true", help="Print tool names only, one per line.")
    tools.set_defaults(handler=cmd_tools)

    schema = subparsers.add_parser("schema", help="Print the input schema for one or more tools.")
    add_global_options(schema, on_subcommand=True)
    schema.add_argument("tool", nargs="+", help="Tool name(s), e.g. mudra_task_check_in.")
    schema.set_defaults(handler=cmd_schema)

    call = subparsers.add_parser(
        "call",
        help="Call a tool and print its result.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""PowerShell (first choice):
  [ordered]@{ query = 'dashboard tokens'; app_scope = 'mcp-server'; limit = 5 } | ConvertTo-Json -Depth 10 -Compress | python -m mcp.rpc call mudra_doc_search --args-file -

After one quoting or JSON-parser failure, stop retrying direct --args variants
and use --args-file - on stdin. Use --args only when the JSON string is already
shell-safe, for example in Bash:
  python -m mcp.rpc call mudra_doc_search --args '{"query":"dashboard tokens"}'""",
    )
    add_global_options(call, on_subcommand=True)
    call.add_argument("tool", help="Tool name, e.g. mudra_doc_search.")
    call.add_argument("--args-file", help="Read JSON arguments from a file, or '-' for stdin (preferred in PowerShell).")
    call.add_argument("--args", help="Tool arguments as a shell-safe JSON object string. Defaults to {}.")
    call.add_argument("--raw", action="store_true", help="Print the full JSON-RPC response envelope.")
    call.set_defaults(handler=cmd_call)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except ToolCallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
