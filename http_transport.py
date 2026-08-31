"""Threaded HTTP transport and dashboard routes for the local MCP server."""

from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from mcp.auth import (
    CF_ACCESS_CLIENT_ID_HEADER,
    CF_ACCESS_JWT_HEADER,
    REMOTE_AUTH_DISABLED,
    RemoteAuthPolicy,
    jwt_unverified_claims,
)
from mcp.config import mcp_bind_host_candidates, mcp_origin_hosts
from mcp.lifecycle import ToolExecutionError
from mcp.project import load_project_descriptor
from mcp.stdio_transport import announce_remote_auth


PROJECT = load_project_descriptor()
MCP_GUI_DIR = PROJECT.gui_dir
PROTOCOL_VERSION = "2025-11-25"
MAX_BODY_BYTES = 1_000_000
TODO_LIST_DEFAULT_LIMIT = 20
TODO_GROUP_DEFAULT_MIN_SCORE = 10
TODO_GROUP_DEFAULT_LIMIT = 8
HTTP_REQUEST_QUEUE_SIZE = 64
HTTP_CLIENT_SOCKET_TIMEOUT_SECONDS = 30


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def normalize_url_prefix(value: str | None) -> str:
    text = str(value or "").strip()
    if not text or text == "/":
        return ""
    return "/" + text.strip("/")


class McpHttpHandler(BaseHTTPRequestHandler):
    server_version = f"{PROJECT.key}-mcp/0.1"
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(HTTP_CLIENT_SOCKET_TIMEOUT_SECONDS)

    def do_OPTIONS(self) -> None:
        if not self.remote_request_allowed():
            return
        if not self.origin_allowed():
            self.send_json_error(403, "Forbidden origin")
            return
        self.send_response(204)
        self.send_common_headers(content_length=0)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, MCP-Protocol-Version")
        self.end_headers()

    def do_GET(self) -> None:
        if not self.remote_request_allowed():
            return
        parsed, path = self.route_request()
        if path in {"/", "/dashboard"}:
            self.send_static_file(MCP_GUI_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self.send_no_content()
            return
        if path in {"/styles.css", "/dashboard.js"}:
            self.send_gui_asset(path.removeprefix("/"))
            return
        for prefix in ("/mcp-gui/", "/scripts/mcp-gui/", "/scripts/mcp_gui/"):
            if path.startswith(prefix):
                self.send_gui_asset(unquote(path.removeprefix(prefix)))
                return
        if path == "/api/dashboard":
            self.send_json(200, self.mcp_server.dashboard_snapshot())
            return
        if path == "/api/dashboard/orchestration":
            self.send_json(200, self.mcp_server.orchestration_list({"limit": 100}))
            return
        if path == "/api/dashboard/guidance-recommendations":
            query = parse_qs(parsed.query)
            try:
                self.send_json(200, self.mcp_server.guidance_recommendation_list({key: query.get(key, [""])[0] for key in ("recommendation_key", "status", "target_document", "search", "cursor", "limit")}))
            except ToolExecutionError as exc:
                self.send_json_error(400, str(exc), getattr(exc, "details", None))
            return
        if path == "/api/dashboard/todos":
            query = parse_qs(parsed.query)
            args = self._todo_query_args(query, include_todo_key=True)
            try:
                self.send_json(200, self.mcp_server.todo_list(args))
            except ToolExecutionError as exc:
                self.send_json_error(400, str(exc), getattr(exc, "details", None))
            return
        if path == "/api/dashboard/todos/next-instruction":
            query = parse_qs(parsed.query)
            args = self._todo_query_args(query, include_todo_key=True)
            args["limit"] = query.get("limit", [3])[0]
            try:
                self.send_json(200, self.mcp_server.todo_next_instruction(args))
            except ToolExecutionError as exc:
                self.send_json_error(400, str(exc), getattr(exc, "details", None))
            return
        if path.startswith("/api/dashboard/todos/groups/"):
            group_key = unquote(path.removeprefix("/api/dashboard/todos/groups/"))
            self.send_json(200, self.mcp_server.todo_group_get(group_key))
            return
        if path.startswith("/api/dashboard/todos/") and path.endswith("/related"):
            todo_key = unquote(path.removeprefix("/api/dashboard/todos/").removesuffix("/related"))
            query = parse_qs(parsed.query)
            args = self._todo_query_args(query, include_todo_key=False)
            args.update({
                "todo_key": todo_key,
                "include_inferred": query.get("include_inferred", ["true"])[0].lower()
                not in {"0", "false", "no"},
                "min_score": query.get("min_score", [TODO_GROUP_DEFAULT_MIN_SCORE])[0],
                "limit": query.get("limit", [TODO_GROUP_DEFAULT_LIMIT])[0],
            })
            try:
                self.send_json(200, self.mcp_server.todo_related_for_assignment(args))
            except ToolExecutionError as exc:
                self.send_json_error(400, str(exc), getattr(exc, "details", None))
            return
        if path.startswith("/api/dashboard/todos/"):
            todo_key = unquote(path.removeprefix("/api/dashboard/todos/"))
            self.send_json(200, self.mcp_server.todo_get(todo_key))
            return
        if path == "/docs/scopes":
            self.send_json(200, self.mcp_server.doc_scopes())
            return
        if path.startswith("/docs/scope/"):
            app_scope = unquote(path.removeprefix("/docs/scope/"))
            try:
                self.send_json(200, self.mcp_server.doc_scope_entries(app_scope))
            except ToolExecutionError as exc:
                self.send_json_error(400, str(exc), getattr(exc, "details", None))
            return
        if path == "/docs/doc":
            key = parse_qs(parsed.query).get("key", [""])[0]
            if not key:
                self.send_json_error(400, "Missing docs key query parameter.")
                return
            self.send_json(200, self.mcp_server.doc_get({"doc_key": key}))
            return
        self.send_json_error(405, "This server accepts MCP JSON-RPC POST requests at /mcp.")

    def do_DELETE(self) -> None:
        if not self.remote_request_allowed():
            return
        self.send_json_error(405, "This stateless MCP server does not manage HTTP sessions.")

    def do_POST(self) -> None:
        if not self.remote_request_allowed():
            return
        _parsed, path = self.route_request()
        handlers = {
            "/docs/search": self.handle_docs_search,
            "/docs/index": self.handle_docs_index,
            "/api/dashboard/todos/add": self.handle_todo_add,
            "/api/dashboard/todos/prune": self.handle_todo_prune,
            "/api/dashboard/todos/priority": self.handle_todo_priority,
            "/api/dashboard/todos/scope": self.handle_todo_scope,
            "/api/dashboard/todos/references": self.handle_todo_references,
            "/api/dashboard/todos/next-instruction": self.handle_todo_next_instruction,
            "/api/dashboard/todos/groups": self.handle_todo_group_related,
            "/api/dashboard/todos/groups/reorder": self.handle_todo_group_reorder,
            "/api/dashboard/todos/groups:auto-associate": self.handle_todo_auto_group_related,
            "/api/dashboard/recommendations/status": self.handle_recommendations_status,
            "/api/dashboard/guidance-recommendations/add": self.handle_guidance_recommendation_add,
            "/api/dashboard/guidance-recommendations/reconcile": self.handle_guidance_recommendation_reconcile,
            "/api/dashboard/token-usage/agent-category/rename": self.handle_agent_category_rename,
            "/api/dashboard/token-usage/agent-category/purge-preview": self.handle_agent_category_purge_preview,
            "/api/dashboard/token-usage/agent-category/purge-closed": self.handle_agent_category_purge_closed,
            "/api/dashboard/orchestration/prompt": self.handle_orchestration_prompt,
        }
        owner_handlers = {
            "/api/dashboard/orchestration/designate": self.handle_orchestration_designate,
            "/api/dashboard/orchestration/enqueue": self.handle_orchestration_enqueue,
            "/api/dashboard/orchestration/heartbeat": self.handle_orchestration_heartbeat,
            "/api/dashboard/orchestration/wake": self.handle_orchestration_wake,
            "/api/dashboard/orchestration/claim": self.handle_orchestration_claim,
            "/api/dashboard/orchestration/launch_prepare": self.handle_orchestration_launch_prepare,
            "/api/dashboard/orchestration/renew": self.handle_orchestration_renew,
            "/api/dashboard/orchestration/reconcile": self.handle_orchestration_reconcile,
            "/api/dashboard/orchestration/release": self.handle_orchestration_release,
            "/api/dashboard/orchestration/delegate": self.handle_orchestration_delegate,
            "/api/dashboard/orchestration/complete": self.handle_orchestration_complete,
            "/api/dashboard/orchestration/retry": self.handle_orchestration_retry,
            "/api/dashboard/orchestration/drop": self.handle_orchestration_drop,
            "/api/dashboard/orchestration/stop": self.handle_orchestration_stop,
            "/api/dashboard/orchestration/cancel": self.handle_orchestration_cancel,
            "/api/dashboard/orchestration/cancel_ack": self.handle_orchestration_cancel_ack,
            "/api/dashboard/orchestration/checkpoint/prepare": self.handle_orchestration_checkpoint_prepare,
            "/api/dashboard/orchestration/checkpoint/record": self.handle_orchestration_checkpoint_record,
            "/api/dashboard/orchestration/git/status": self.handle_git_status,
            "/api/dashboard/orchestration/git/diff": self.handle_git_diff,
            "/api/dashboard/orchestration/git/add": self.handle_git_add,
            "/api/dashboard/orchestration/git/commit": self.handle_git_commit,
            "/api/dashboard/orchestration/git/checkpoint_noop": self.handle_git_checkpoint_noop,
            "/api/dashboard/orchestration/git/push": self.handle_git_push,
        }
        if path == "/api/dashboard/owner/flush-agent-state":
            if self.remote_auth.enabled:
                self.deny_remote_request(
                    403,
                    "Owner route /api/dashboard/owner/flush-agent-state is denied "
                    "on remote deployments until an owner-only policy is implemented.",
                    self.remote_client_id(),
                )
                return
            self.handle_owner_flush()
            return
        if path in handlers:
            handlers[path]()
            return
        if path in owner_handlers:
            if self.remote_auth.enabled:
                self.deny_remote_request(
                    403,
                    "Dashboard orchestration mutations are local-owner-only; use the authenticated MCP tools for agent orchestration.",
                    self.remote_client_id(),
                )
                return
            owner_handlers[path]()
            return
        if path != "/mcp":
            self.send_json_error(404, "Not found")
            return
        if not self.origin_allowed():
            self.send_json_error(403, "Forbidden origin")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json_error(411, "Invalid Content-Length")
            return
        if length <= 0:
            self.send_json_error(400, "Empty request body")
            return
        if length > MAX_BODY_BYTES:
            self.send_json_error(413, "Request body too large")
            return
        raw = self.rfile.read(length)
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, self.mcp_server.error_response(None, -32700, "Parse error"))
            return
        response = self.mcp_server.handle_jsonrpc(message)
        if response is None or response == []:
            self.send_response(202)
            self.send_common_headers(content_length=0)
            self.end_headers()
            return
        self.send_json(200, response)

    def _todo_query_args(self, query: dict[str, list[str]], *, include_todo_key: bool) -> dict[str, Any]:
        args: dict[str, Any] = {
            "status": query.get("status", ["open"])[0],
            "app_scope": query.get("app_scope", [""])[0],
            "priority": query.get("priority", [""])[0],
            "source": query.get("source", [""])[0],
            "search": query.get("search", [""])[0],
            "reference_search": query.get("reference_search", [""])[0],
            "tags": query.get("tags", []),
            "cursor": query.get("cursor", [""])[0],
            "limit": query.get("limit", [TODO_LIST_DEFAULT_LIMIT])[0],
        }
        if include_todo_key:
            args["todo_key"] = query.get("todo_key", [""])[0]
        return args

    def read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ToolExecutionError("Invalid Content-Length") from None
        if length > MAX_BODY_BYTES:
            raise ToolExecutionError("Request body too large")
        if not length:
            return {}
        try:
            args = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ToolExecutionError("Request body must be a JSON object.") from None
        if not isinstance(args, dict):
            raise ToolExecutionError("Request body must be a JSON object.")
        return args

    def _post_json(self, method_name: str) -> None:
        if not self.origin_allowed():
            self.send_json_error(403, "Forbidden origin")
            return
        try:
            result = getattr(self.mcp_server, method_name)(self.read_json_body())
        except ToolExecutionError as exc:
            self.send_json_error(400, str(exc), getattr(exc, "details", None))
            return
        except Exception as exc:
            if method_name != "update_recommendation_status":
                raise
            self.send_json_error(500, f"Internal error: {exc}")
            return
        self.send_json(200, result)

    def handle_docs_search(self) -> None: self._post_json("doc_search")
    def handle_docs_index(self) -> None: self._post_json("doc_index_repo")
    def handle_todo_add(self) -> None: self._post_json("todo_add")
    def handle_todo_prune(self) -> None: self._post_json("todo_prune")
    def handle_todo_priority(self) -> None: self._post_json("todo_update_priority")
    def handle_todo_scope(self) -> None: self._post_json("todo_update_scope")
    def handle_todo_references(self) -> None: self._post_json("todo_append_references")
    def handle_todo_next_instruction(self) -> None: self._post_json("todo_next_instruction")
    def handle_todo_group_related(self) -> None: self._post_json("todo_group_related")
    def handle_todo_group_reorder(self) -> None: self._post_json("todo_group_reorder")
    def handle_todo_auto_group_related(self) -> None: self._post_json("todo_auto_group_related")
    def handle_recommendations_status(self) -> None: self._post_json("update_recommendation_status")
    def handle_guidance_recommendation_add(self) -> None: self._post_json("guidance_recommendation_add")
    def handle_guidance_recommendation_reconcile(self) -> None: self._post_json("guidance_recommendation_reconcile")
    def handle_agent_category_rename(self) -> None: self._post_json("rename_agent_category")
    def handle_agent_category_purge_preview(self) -> None: self._post_json("agent_category_purge_preview")
    def handle_agent_category_purge_closed(self) -> None: self._post_json("agent_category_purge_closed")
    def handle_orchestration_prompt(self) -> None: self._post_json("orchestration_prompt")
    def handle_orchestration_designate(self) -> None: self._post_json("orchestration_designate")
    def handle_orchestration_enqueue(self) -> None: self._post_json("orchestration_enqueue")
    def handle_orchestration_heartbeat(self) -> None: self._post_json("orchestration_heartbeat")
    def handle_orchestration_wake(self) -> None: self._post_json("orchestration_wake")
    def handle_orchestration_claim(self) -> None: self._post_json("orchestration_claim")
    def handle_orchestration_launch_prepare(self) -> None: self._post_json("orchestration_launch_prepare")
    def handle_orchestration_renew(self) -> None: self._post_json("orchestration_renew")
    def handle_orchestration_reconcile(self) -> None: self._post_json("orchestration_reconcile")
    def handle_orchestration_release(self) -> None: self._post_json("orchestration_release")
    def handle_orchestration_delegate(self) -> None: self._post_json("orchestration_delegate")
    def handle_orchestration_complete(self) -> None: self._post_json("orchestration_complete")
    def handle_orchestration_retry(self) -> None: self._post_json("orchestration_retry")
    def handle_orchestration_drop(self) -> None: self._post_json("orchestration_drop")
    def handle_orchestration_stop(self) -> None: self._post_json("orchestration_stop")
    def handle_orchestration_cancel(self) -> None: self._post_json("orchestration_cancel")
    def handle_orchestration_cancel_ack(self) -> None: self._post_json("orchestration_cancel_ack")
    def handle_orchestration_checkpoint_prepare(self) -> None: self._post_json("orchestration_checkpoint_prepare")
    def handle_orchestration_checkpoint_record(self) -> None: self._post_json("orchestration_checkpoint_record")
    def handle_git_status(self) -> None: self._post_json("git_status")
    def handle_git_diff(self) -> None: self._post_json("git_diff")
    def handle_git_add(self) -> None: self._post_json("git_add")
    def handle_git_commit(self) -> None: self._post_json("git_commit")
    def handle_git_checkpoint_noop(self) -> None: self._post_json("git_checkpoint_noop")
    def handle_git_push(self) -> None: self._post_json("git_push")
    def handle_owner_flush(self) -> None: self._post_json("owner_flush_agent_state")

    @property
    def mcp_server(self) -> Any:
        return self.server.mcp_server  # type: ignore[attr-defined]

    def route_request(self) -> tuple[Any, str]:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        prefix = getattr(self.server, "url_prefix", "")
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            path = path.removeprefix(prefix).rstrip("/") or "/"
        return parsed, path

    def origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).hostname in mcp_origin_hosts()

    @property
    def remote_auth(self) -> RemoteAuthPolicy:
        return getattr(self.server, "remote_auth", None) or REMOTE_AUTH_DISABLED

    def remote_client_id(self) -> str:
        header_id = (self.headers.get(CF_ACCESS_CLIENT_ID_HEADER) or "").strip()
        if header_id:
            return header_id
        claims = jwt_unverified_claims(self.headers.get(CF_ACCESS_JWT_HEADER) or "")
        common_name = claims.get("common_name")
        return common_name.strip() if isinstance(common_name, str) else ""

    def remote_request_allowed(self) -> bool:
        policy = self.remote_auth
        if not policy.enabled:
            return True
        client_id = self.remote_client_id()
        if not self.headers.get(CF_ACCESS_JWT_HEADER):
            self.deny_remote_request(401, "Missing Cloudflare Access service-token credentials.", client_id)
            return False
        if not client_id or client_id not in policy.service_token_ids:
            self.deny_remote_request(403, "Service token is not allowed for this project.", client_id)
            return False
        prefix = getattr(self.server, "url_prefix", "")
        if prefix:
            raw_path = urlparse(self.path).path.rstrip("/") or "/"
            if raw_path != prefix and not raw_path.startswith(prefix + "/"):
                self.deny_remote_request(404, "Unknown project path.", client_id)
                return False
        return True

    def deny_remote_request(self, status: int, message: str, client_id: str) -> None:
        print(
            f"[{PROJECT.key}] remote auth denied: {self.command} {urlparse(self.path).path} "
            f"client_id={client_id or '-'} status={status} {message}",
            file=sys.stderr,
            flush=True,
        )
        self.send_json_error(status, message)

    def send_gui_asset(self, rel_path: str) -> None:
        if not rel_path or rel_path.startswith(("/", "\\")):
            self.send_json_error(404, "Not found")
            return
        candidate = (MCP_GUI_DIR / rel_path).resolve()
        try:
            candidate.relative_to(MCP_GUI_DIR.resolve())
        except ValueError:
            self.send_json_error(404, "Not found")
            return
        if rel_path in {"favicon.ico", "favicon.png"} and not candidate.is_file():
            self.send_no_content()
            return
        content_type = {
            ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
            ".html": "text/html; charset=utf-8", ".ico": "image/x-icon",
            ".png": "image/png", ".svg": "image/svg+xml",
        }.get(candidate.suffix.lower(), "application/octet-stream")
        self.send_static_file(candidate, content_type)

    def send_no_content(self) -> None:
        self.send_response(204)
        self.send_common_headers(content_length=0)
        self.end_headers()

    def send_static_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_json_error(404, "Not found")
            return
        try:
            body = path.read_bytes()
        except OSError:
            self.send_json_error(500, "Unable to read static file")
            return
        self.send_response(200)
        self.send_common_headers(content_length=len(body))
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: Any, *, close: bool = False) -> None:
        body = compact_json(payload).encode("utf-8")
        self.send_response(status)
        self.send_common_headers(content_length=len(body))
        self.send_header("Content-Type", "application/json")
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def send_json_error(
        self, status: int, message: str, details: dict[str, Any] | None = None
    ) -> None:
        payload = self.mcp_server.error_response(None, status, message)
        if details:
            error = payload.get("error")
            if isinstance(error, dict):
                error["data"] = details
        self.send_json(status, payload, close=True)

    def send_common_headers(self, *, content_length: int) -> None:
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("MCP-Protocol-Version", PROTOCOL_VERSION)
        origin = self.headers.get("Origin")
        if origin and self.origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def log_message(self, fmt: str, *args: Any) -> None:
        # BaseHTTPRequestHandler reports socket timeouts before it has parsed a
        # request line, so `path` has not been assigned yet.
        path = urlparse(getattr(self, "path", "")).path
        if path in {"/api/dashboard", "/favicon.ico"} or path.startswith(("/mcp-gui/", "/scripts/mcp-gui/", "/scripts/mcp_gui/")):
            return
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)


class MudraHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = HTTP_REQUEST_QUEUE_SIZE
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], mcp_server: Any, *, url_prefix: str = "", remote_auth: RemoteAuthPolicy | None = None) -> None:
        self.mcp_server = mcp_server
        self.url_prefix = normalize_url_prefix(url_prefix)
        self.remote_auth = remote_auth or REMOTE_AUTH_DISABLED
        super().__init__(address, McpHttpHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) in {10053, 10054, 10101}:
            return
        super().handle_error(request, client_address)


def run_http(server: Any, host: str, port: int, *, url_prefix: str = "", remote_auth: RemoteAuthPolicy | None = None) -> None:
    server.ensure_schema()
    announce_remote_auth(remote_auth, project=server.project)
    last_error: OSError | None = None
    httpd: MudraHttpServer | None = None
    bind_host = host
    bind_hosts = mcp_bind_host_candidates(host)
    for candidate_host in bind_hosts:
        try:
            httpd = MudraHttpServer((candidate_host, port), server, url_prefix=url_prefix, remote_auth=remote_auth)
        except OSError as exc:
            last_error = exc
            remaining = bind_hosts[bind_hosts.index(candidate_host) + 1 :]
            if remaining:
                print(f"{server.project.server_name} HTTP endpoint unavailable on {candidate_host}:{port} ({exc}); trying {remaining[0]}:{port}.", file=sys.stderr, flush=True)
                continue
            raise
        bind_host = candidate_host
        break
    if httpd is None:
        raise last_error or OSError(f"Unable to start {server.project.server_name} HTTP server on {host}:{port}")
    print(f"{server.project.server_name} HTTP server listening at http://{bind_host}:{port}{normalize_url_prefix(url_prefix)}/mcp; using {server.db_path}", file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"{server.project.server_name} HTTP server stopped.", file=sys.stderr, flush=True)
    finally:
        httpd.server_close()


def _port_in_use(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
    try:
        with socket.create_connection((probe_host, port), timeout=0.3):
            return True
    except OSError:
        return False


def start_http_thread(server: Any, host: str, port: int, *, url_prefix: str = "", remote_auth: RemoteAuthPolicy | None = None) -> MudraHttpServer | None:
    server.ensure_schema()
    announce_remote_auth(remote_auth, project=server.project)
    for candidate_host in mcp_bind_host_candidates(host):
        if _port_in_use(candidate_host, port):
            print(f"{server.project.server_name} HTTP endpoint already serving on {candidate_host}:{port}; continuing stdio-only and sharing state through mcp.db.", file=sys.stderr, flush=True)
            return None
        try:
            httpd = MudraHttpServer((candidate_host, port), server, url_prefix=url_prefix, remote_auth=remote_auth)
        except OSError as exc:
            remaining = mcp_bind_host_candidates(host)
            remaining = remaining[remaining.index(candidate_host) + 1 :]
            if remaining:
                print(f"{server.project.server_name} HTTP endpoint unavailable on {candidate_host}:{port} ({exc}); trying {remaining[0]}:{port}.", file=sys.stderr, flush=True)
                continue
            print(f"{server.project.server_name} HTTP endpoint unavailable on {candidate_host}:{port} ({exc}); continuing stdio-only. The MCP process may remain alive, but dashboard requests to this address will be refused until an HTTP listener is available.", file=sys.stderr, flush=True)
            return None
        print(f"{server.project.server_name} HTTP server listening at http://{candidate_host}:{port}{normalize_url_prefix(url_prefix)}/mcp; using {server.db_path}", file=sys.stderr, flush=True)
        thread = threading.Thread(target=httpd.serve_forever, name=f"{server.project.key}-mcp-http", daemon=True)
        thread.start()
        return httpd
    return None
