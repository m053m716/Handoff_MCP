"""Sync local Mudra MCP advisory state through gateway MCP-state endpoints.

The tool intentionally speaks only JSON over HTTP. It never reads or writes the
local mcp.db file directly; the local server remains the owner of that state.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import platform
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.config import mcp_http_url_candidates
from mcp.project import load_project_descriptor

PROJECT = load_project_descriptor()


SCHEMA_VERSION = "mudra.mcp-state.v1"
DEFAULT_MCP_URLS = mcp_http_url_candidates()
DEFAULT_GATEWAY_URL = "http://127.0.0.1:2999"
# Header/env-var names match mudra/app/config.py and mudra-gateway/tools/mudra_browser.py
# so a single set of Cloudflare Access service-token credentials works across tools.
CF_ACCESS_CLIENT_ID_ENV_VARS = (
    f"{PROJECT.cloudflare_env_prefix}_CF_ACCESS_CLIENT_ID",
    "CF_ACCESS_CLIENT_ID",
    "CF_CLIENT_ID",
)
CF_ACCESS_CLIENT_SECRET_ENV_VARS = (
    f"{PROJECT.cloudflare_env_prefix}_CF_ACCESS_CLIENT_SECRET",
    "CF_ACCESS_CLIENT_SECRET",
    "CF_CLIENT_SECRET",
)
CF_ACCESS_CLIENT_ID_HEADER = "CF-Access-Client-Id"
CF_ACCESS_CLIENT_SECRET_HEADER = "CF-Access-Client-Secret"
# The default urllib User-Agent ("Python-urllib/x.y") trips Cloudflare's Bot Fight
# Mode/WAF (error 1010 browser_signature_banned) even with valid Access credentials.
DEFAULT_USER_AGENT = f"{PROJECT.key}-mcp-state-sync/1.0"
TODO_ACTIVE_STATUSES = {"suggested", "accepted", "in_progress", "blocked", "queued"}
TODO_TERMINAL_STATUSES = {"done", "dropped"}
TODO_TRANSPORT_FIELDS = (
    "todo_key",
    "app_scope",
    "title",
    "detail",
    "status",
    "priority",
    "source",
    "source_task_key",
    "tags",
    "code_paths",
    "symbol_refs",
    "doc_keys",
    "route_refs",
    "test_refs",
    "search_queries",
    "created_at",
    "updated_at",
    "completed_at",
)
TODO_LIST_FIELDS = {
    "tags",
    "code_paths",
    "symbol_refs",
    "doc_keys",
    "route_refs",
    "test_refs",
    "search_queries",
}
TODO_REQUIRED_FIELDS = {"todo_key", "title"}
TODO_SIGNATURE_EXCLUDED = {
    "id",
    "is_open",
    "reference_advisories",
    "created_at",
    "updated_at",
    "completed_at",
}
DOC_APPLY_SOURCE_TYPES = {"manual", "inferred"}
REPO_ROOT = PROJECT.repo_root


class SyncError(RuntimeError):
    """User-facing sync error."""


class HttpError(SyncError):
    def __init__(self, method: str, url: str, status: int, body: str) -> None:
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        message = f"{method} {url} failed with HTTP {status}"
        if body:
            message += f": {body[:800]}"
        super().__init__(message)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_base_url(url: str) -> str:
    value = url.strip()
    if not value:
        raise SyncError("URL cannot be empty.")
    parsed = urllib.parse.urlparse(value)
    if not parsed.scheme:
        value = "http://" + value
        parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SyncError(f"Invalid HTTP URL: {url}")
    return value.rstrip("/")


def join_url(base_url: str, path: str) -> str:
    return normalize_base_url(base_url) + "/" + path.lstrip("/")


def resolve_secret(value: str | None, env_names: tuple[str, ...]) -> str:
    text = (value or "").strip()
    if text:
        return text
    for name in env_names:
        env_value = os.environ.get(name, "").strip()
        if env_value:
            return env_value
    return ""


def cloudflare_access_headers(args: argparse.Namespace) -> dict[str, str]:
    client_id = resolve_secret(getattr(args, "cf_id", None), CF_ACCESS_CLIENT_ID_ENV_VARS)
    client_secret = resolve_secret(getattr(args, "cf_secret", None), CF_ACCESS_CLIENT_SECRET_ENV_VARS)
    if not client_id or not client_secret:
        return {}
    return {
        CF_ACCESS_CLIENT_ID_HEADER: client_id,
        CF_ACCESS_CLIENT_SECRET_HEADER: client_secret,
    }


def request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    headers = {"Accept": "application/json", "User-Agent": DEFAULT_USER_AGENT}
    body: bytes | None = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            if not data:
                return {}
            text = data.decode("utf-8")
            return json.loads(text)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        body_text = raw.decode("utf-8", errors="replace") if raw else ""
        raise HttpError(method, url, exc.code, body_text) from None
    except urllib.error.URLError as exc:
        raise SyncError(f"{method} {url} failed: {exc.reason}") from None
    except TimeoutError:
        raise SyncError(f"{method} {url} timed out after {timeout:g}s") from None
    except json.JSONDecodeError as exc:
        raise SyncError(f"{method} {url} did not return JSON: {exc}") from None


def write_json_file(path: str, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pretty_json(payload) + "\n", encoding="utf-8")


def read_json_file(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise SyncError(f"Could not read {path}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise SyncError(f"{path} is not valid JSON: {exc}") from None


def pretty_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True)


def compact_json(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True, sort_keys=True)


def stable_hash(payload: Any) -> str:
    return hashlib.sha256(compact_json(payload).encode("utf-8")).hexdigest()


def workspace_id() -> str:
    rel = str(REPO_ROOT.resolve()).lower()
    return hashlib.sha256(rel.encode("utf-8", errors="replace")).hexdigest()[:16]


def client_metadata(client_id: str | None = None) -> dict[str, Any]:
    hostname = socket.gethostname()
    return {
        "client_id": client_id or f"{getpass.getuser()}@{hostname}",
        "hostname": hostname,
        "user": getpass.getuser(),
        "platform": platform.platform(),
        "workspace_root": str(REPO_ROOT),
        "workspace_id": workspace_id(),
        "tool": "mcp.state_sync",
    }


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def normalize_todo(raw: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "key": "todo_key",
        "description": "detail",
        "scope": "app_scope",
    }
    source = dict(raw)
    for old, new in aliases.items():
        if new not in source and old in source:
            source[new] = source[old]

    todo: dict[str, Any] = {}
    for field in TODO_TRANSPORT_FIELDS:
        value = source.get(field)
        if field in TODO_LIST_FIELDS:
            todo[field] = [str(item) for item in as_list(value) if str(item).strip()]
        elif value is None:
            todo[field] = "" if field != "status" else "suggested"
        else:
            todo[field] = str(value)

    todo["todo_key"] = todo["todo_key"].strip()
    todo["title"] = todo["title"].strip()
    todo["status"] = (todo["status"] or "suggested").strip()
    todo["priority"] = (todo["priority"] or "P2").strip()
    todo["source"] = (todo["source"] or "mcp-state-sync").strip()
    return todo


def validate_todo_for_apply(todo: dict[str, Any]) -> list[str]:
    missing = [field for field in sorted(TODO_REQUIRED_FIELDS) if not todo.get(field)]
    if todo.get("status") not in TODO_ACTIVE_STATUSES | TODO_TERMINAL_STATUSES:
        missing.append("valid status")
    return missing


def todo_signature(todo: dict[str, Any]) -> str:
    normalized = normalize_todo(todo)
    comparable = {
        key: value
        for key, value in normalized.items()
        if key not in TODO_SIGNATURE_EXCLUDED
    }
    return stable_hash(comparable)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def extract_todos(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [normalize_todo(item) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("todos", "items", "open_todos"):
        items = payload.get(key)
        if isinstance(items, list):
            return [normalize_todo(item) for item in items if isinstance(item, dict)]
    state = payload.get("state")
    if isinstance(state, dict):
        return extract_todos(state)
    data = payload.get("data")
    if isinstance(data, dict):
        return extract_todos(data)
    return []


def extract_documents(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("documents", "docs"):
        items = payload.get(key)
        if isinstance(items, list):
            return [dict(item) for item in items if isinstance(item, dict)]
    docs = payload.get("docs")
    if isinstance(docs, dict):
        return extract_documents(docs)
    state = payload.get("state")
    if isinstance(state, dict):
        return extract_documents(state)
    data = payload.get("data")
    if isinstance(data, dict):
        return extract_documents(data)
    return []


def summarize_documents(docs: list[dict[str, Any]]) -> dict[str, Any]:
    with_content = [doc for doc in docs if isinstance(doc.get("content"), str)]
    by_scope: dict[str, int] = {}
    for doc in docs:
        scope = str(doc.get("app_scope") or "")
        by_scope[scope] = by_scope.get(scope, 0) + 1
    return {
        "document_count": len(docs),
        "with_content_count": len(with_content),
        "by_scope": by_scope,
    }


class LocalMcpClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        url = join_url(self.base_url, path)
        if query:
            encoded = urllib.parse.urlencode(
                {key: value for key, value in query.items() if value not in (None, "")}
            )
            if encoded:
                url += "?" + encoded
        return request_json("GET", url, timeout=self.timeout)

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        return request_json("POST", join_url(self.base_url, path), payload=payload, timeout=self.timeout)

    def list_todos(self, status: str = "open", app_scope: str = "", limit: int = 100) -> list[dict[str, Any]]:
        payload = self.get("/api/dashboard/todos", {"status": status, "app_scope": app_scope, "limit": limit})
        return extract_todos(payload)

    def get_todo(self, todo_key: str) -> dict[str, Any]:
        payload = self.get(f"/api/dashboard/todos/{urllib.parse.quote(todo_key, safe='')}")
        if isinstance(payload, dict):
            return payload
        return {"found": False}

    def add_todo(self, todo: dict[str, Any]) -> Any:
        args = {
            key: value
            for key, value in normalize_todo(todo).items()
            if key not in {"id", "created_at", "updated_at", "completed_at"}
        }
        args["source"] = "mcp-state-sync"
        return self.post("/api/dashboard/todos/add", args)

    def prune_todo(self, todo: dict[str, Any], detail: str) -> Any:
        normalized = normalize_todo(todo)
        return self.post(
            "/api/dashboard/todos/prune",
            {
                "todo_key": normalized["todo_key"],
                "status": normalized.get("status") if normalized.get("status") in TODO_TERMINAL_STATUSES else "done",
                "actor": "mcp-state-sync",
                "detail": detail,
            },
        )

    def doc_scopes(self) -> list[dict[str, Any]]:
        payload = self.get("/docs/scopes")
        scopes = payload.get("scopes", []) if isinstance(payload, dict) else []
        return [dict(scope) for scope in scopes if isinstance(scope, dict)]

    def doc_scope_entries(self, app_scope: str) -> list[dict[str, Any]]:
        payload = self.get(f"/docs/scope/{urllib.parse.quote(app_scope, safe='')}")
        docs = payload.get("documents", []) if isinstance(payload, dict) else []
        return [dict(doc) for doc in docs if isinstance(doc, dict)]

    def doc_get(self, doc_key: str, max_chars: int) -> dict[str, Any] | None:
        payload = self.get("/docs/doc", {"key": doc_key, "max_chars": max_chars})
        if isinstance(payload, dict) and payload.get("found") and isinstance(payload.get("document"), dict):
            return dict(payload["document"])
        return None

    def tool_call(self, name: str, arguments: dict[str, Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": f"mcp-state-sync-{stable_hash([name, arguments])[:12]}",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        response = self.post("/mcp", payload)
        if isinstance(response, dict) and "error" in response:
            raise SyncError(f"Local MCP tool {name} failed: {response['error']}")
        result = response.get("result") if isinstance(response, dict) else None
        if isinstance(result, dict) and result.get("isError"):
            text = ""
            content = result.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                text = str(content[0].get("text", ""))
            raise SyncError(f"Local MCP tool {name} failed: {text or result}")
        if isinstance(result, dict) and "structuredContent" in result:
            return result["structuredContent"]
        return result

    def doc_upsert(self, doc: dict[str, Any]) -> Any:
        args = {
            "doc_key": str(doc.get("doc_key", "")).strip(),
            "app_scope": str(doc.get("app_scope", "")).strip(),
            "title": str(doc.get("title", "")).strip() or str(doc.get("doc_key", "")).strip(),
            "content": str(doc.get("content", "")),
            "summary": str(doc.get("summary", "")),
            "source_type": str(doc.get("source_type", "manual") or "manual"),
            "source_path": str(doc.get("source_path", "")),
            "tags": [str(tag) for tag in as_list(doc.get("tags")) if str(tag).strip()],
        }
        return self.tool_call("mudra_doc_upsert", args)


class GatewayClient:
    def __init__(self, base_url: str, timeout: float, cf_headers: dict[str, str] | None = None) -> None:
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout
        self.cf_headers = cf_headers or {}

    def get_todos(self) -> Any:
        return request_json(
            "GET",
            join_url(self.base_url, "/api/v1/mcp-state/todos"),
            timeout=self.timeout,
            extra_headers=self.cf_headers,
        )

    def patch_todos(self, payload: dict[str, Any]) -> Any:
        return request_json(
            "PATCH",
            join_url(self.base_url, "/api/v1/mcp-state/todos"),
            payload=payload,
            timeout=self.timeout,
            extra_headers=self.cf_headers,
        )

    def docs_sync(self, payload: dict[str, Any]) -> Any:
        return request_json(
            "POST",
            join_url(self.base_url, "/api/v1/mcp-state/docs:sync"),
            payload=payload,
            timeout=self.timeout,
            extra_headers=self.cf_headers,
        )


def discover_mcp_url(explicit_url: str | None, timeout: float) -> str:
    candidates: list[str] = []
    if explicit_url:
        candidates.append(explicit_url)
    env_url = os.environ.get("MUDRA_MCP_URL")
    if env_url and env_url not in candidates:
        candidates.append(env_url)
    candidates.extend(url for url in DEFAULT_MCP_URLS if url not in candidates)

    errors: list[str] = []
    for candidate in candidates:
        base_url = normalize_base_url(candidate)
        try:
            request_json("GET", join_url(base_url, "/docs/scopes"), timeout=timeout)
            return base_url
        except SyncError as exc:
            errors.append(f"{base_url}: {exc}")
    detail = "\n".join(f"  - {error}" for error in errors)
    raise SyncError(
        "Could not find a running local MCP HTTP server. Start it with "
        "`python -m mcp.server --http` "
        "(uses config.yaml mcp.server_ip/mcp.server_port, with localhost fallback) "
        "or pass --mcp-url.\n" + detail
    )


def build_state_envelope(
    *,
    client: dict[str, Any],
    todos: list[dict[str, Any]] | None = None,
    docs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "client": client,
    }
    if todos is not None:
        state["todos"] = todos
        state["todo_count"] = len(todos)
    if docs is not None:
        state["docs"] = docs
        state["docs_summary"] = summarize_documents(docs.get("documents", []))
    return state


def export_local_todos(client: LocalMcpClient, status: str, app_scope: str, limit: int) -> list[dict[str, Any]]:
    todos = client.list_todos(status=status, app_scope=app_scope, limit=limit)
    return [normalize_todo(todo) for todo in todos]


def export_local_docs(
    client: LocalMcpClient,
    *,
    scopes: list[str],
    todo_doc_refs: dict[str, list[str]],
    include_content: bool,
    include_all_content: bool,
    content_doc_keys: list[str],
    max_chars: int,
) -> dict[str, Any]:
    scope_rows = client.doc_scopes()
    available_scopes = [str(scope.get("scope", "")) for scope in scope_rows if scope.get("scope")]
    selected_scopes = scopes or available_scopes

    documents: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for scope in selected_scopes:
        for doc in client.doc_scope_entries(scope):
            doc_key = str(doc.get("doc_key", ""))
            if doc_key:
                by_key[doc_key] = doc
                documents.append(doc)

    referenced_doc_keys = {
        doc_key
        for refs in todo_doc_refs.values()
        for doc_key in refs
        if isinstance(doc_key, str) and doc_key.strip()
    }
    requested_content = set(content_doc_keys)
    if include_content:
        requested_content.update(referenced_doc_keys)
    if include_all_content:
        requested_content.update(by_key)

    for doc_key in sorted(requested_content):
        document = client.doc_get(doc_key, max_chars=max_chars)
        if not document:
            continue
        existing = by_key.get(doc_key)
        if existing is not None:
            existing.update(document)
        else:
            by_key[doc_key] = document
            documents.append(document)

    return {
        "scopes": scope_rows,
        "selected_scopes": selected_scopes,
        "documents": documents,
        "todo_doc_refs": [
            {"todo_key": todo_key, "doc_keys": refs}
            for todo_key, refs in sorted(todo_doc_refs.items())
            if refs
        ],
        "content_policy": {
            "include_content": include_content,
            "include_all_content": include_all_content,
            "content_doc_keys": sorted(set(content_doc_keys)),
            "max_chars": max_chars,
        },
    }


def todo_doc_refs(todos: list[dict[str, Any]]) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    for todo in todos:
        key = str(todo.get("todo_key", ""))
        doc_keys = [str(item) for item in as_list(todo.get("doc_keys")) if str(item).strip()]
        if key and doc_keys:
            refs[key] = doc_keys
    return refs


def build_todo_report(
    local_todos: list[dict[str, Any]],
    gateway_todos: list[dict[str, Any]],
    *,
    direction: str,
) -> dict[str, Any]:
    local_by_key = {todo["todo_key"]: todo for todo in local_todos if todo.get("todo_key")}
    gateway_by_key = {todo["todo_key"]: todo for todo in gateway_todos if todo.get("todo_key")}
    all_keys = sorted(set(local_by_key) | set(gateway_by_key))

    report: dict[str, Any] = {
        "direction": direction,
        "local_count": len(local_by_key),
        "gateway_count": len(gateway_by_key),
        "same": [],
        "local_only": [],
        "gateway_only": [],
        "changed": [],
        "conflicts": [],
    }
    for key in all_keys:
        local = local_by_key.get(key)
        remote = gateway_by_key.get(key)
        if local is None and remote is not None:
            report["gateway_only"].append(todo_summary(remote))
            continue
        if remote is None and local is not None:
            report["local_only"].append(todo_summary(local))
            continue
        if local is None or remote is None:
            continue
        if todo_signature(local) == todo_signature(remote):
            report["same"].append(todo_summary(local))
            continue

        local_time = parse_timestamp(local.get("updated_at"))
        remote_time = parse_timestamp(remote.get("updated_at"))
        if local_time and remote_time:
            if local_time > remote_time:
                change_kind = "local_newer"
            elif remote_time > local_time:
                change_kind = "gateway_newer"
            else:
                change_kind = "same_timestamp_changed"
        else:
            change_kind = "changed"

        item = {
            "todo_key": key,
            "title": local.get("title") or remote.get("title") or "",
            "change_kind": change_kind,
            "local_updated_at": local.get("updated_at", ""),
            "gateway_updated_at": remote.get("updated_at", ""),
            "local_status": local.get("status", ""),
            "gateway_status": remote.get("status", ""),
        }
        report["changed"].append(item)
        if (direction == "push" and change_kind == "gateway_newer") or (
            direction == "pull" and change_kind == "local_newer"
        ):
            report["conflicts"].append(item)
    report["summary"] = {
        "same": len(report["same"]),
        "local_only": len(report["local_only"]),
        "gateway_only": len(report["gateway_only"]),
        "changed": len(report["changed"]),
        "conflicts": len(report["conflicts"]),
    }
    return report


def todo_summary(todo: dict[str, Any]) -> dict[str, Any]:
    return {
        "todo_key": str(todo.get("todo_key", "")),
        "title": str(todo.get("title", "")),
        "status": str(todo.get("status", "")),
        "priority": str(todo.get("priority", "")),
        "app_scope": str(todo.get("app_scope", "")),
        "updated_at": str(todo.get("updated_at", "")),
    }


def print_report(report: dict[str, Any]) -> None:
    summary = report.get("summary", {})
    print(
        "Todo merge report "
        f"({report.get('direction')}): "
        f"local={report.get('local_count')} gateway={report.get('gateway_count')} "
        f"same={summary.get('same', 0)} local_only={summary.get('local_only', 0)} "
        f"gateway_only={summary.get('gateway_only', 0)} changed={summary.get('changed', 0)} "
        f"conflicts={summary.get('conflicts', 0)}"
    )
    for label in ("conflicts", "changed", "local_only", "gateway_only"):
        items = report.get(label) or []
        if not items:
            continue
        print(f"\n{label}:")
        for item in items[:20]:
            title = item.get("title") or ""
            suffix = f" - {title}" if title else ""
            extra = f" [{item.get('change_kind')}]" if item.get("change_kind") else ""
            print(f"  - {item.get('todo_key')}{extra}{suffix}")
        if len(items) > 20:
            print(f"  ... {len(items) - 20} more")


def build_todo_patch_payload(
    *,
    client: dict[str, Any],
    todos: list[dict[str, Any]],
    mode: str,
    dry_run: bool,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "client": client,
        "mode": mode,
        "dry_run": dry_run,
        "todos": todos,
    }
    if report is not None:
        payload["client_merge_report"] = report
    return payload


def build_docs_sync_payload(
    *,
    client: dict[str, Any],
    docs: dict[str, Any] | None,
    mode: str,
    dry_run: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "client": client,
        "mode": mode,
        "dry_run": dry_run,
    }
    if docs is not None:
        payload["docs"] = docs
        payload["docs_summary"] = summarize_documents(docs.get("documents", []))
    return payload


def select_push_candidates(
    local_todos: list[dict[str, Any]],
    report: dict[str, Any],
    *,
    force: bool,
) -> list[dict[str, Any]]:
    if force:
        return local_todos
    conflict_keys = {item["todo_key"] for item in report.get("conflicts", [])}
    return [todo for todo in local_todos if todo.get("todo_key") not in conflict_keys]


def select_pull_candidates(
    gateway_todos: list[dict[str, Any]],
    report: dict[str, Any],
    *,
    force: bool,
) -> list[dict[str, Any]]:
    if force:
        return gateway_todos
    conflict_keys = {item["todo_key"] for item in report.get("conflicts", [])}
    return [todo for todo in gateway_todos if todo.get("todo_key") not in conflict_keys]


def apply_todos_to_local(
    client: LocalMcpClient,
    todos: list[dict[str, Any]],
    *,
    apply_closed: bool,
) -> dict[str, Any]:
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for todo in todos:
        normalized = normalize_todo(todo)
        missing = validate_todo_for_apply(normalized)
        if missing:
            skipped.append({"todo_key": normalized.get("todo_key"), "reason": "missing " + ", ".join(missing)})
            continue
        status = normalized.get("status")
        try:
            if status in TODO_ACTIVE_STATUSES:
                client.add_todo(normalized)
                applied.append({"todo_key": normalized["todo_key"], "action": "upsert", "status": status})
            elif status in TODO_TERMINAL_STATUSES and apply_closed:
                existing = client.get_todo(normalized["todo_key"])
                if not existing.get("found"):
                    skipped.append({"todo_key": normalized["todo_key"], "reason": "terminal todo missing locally"})
                    continue
                client.prune_todo(
                    normalized,
                    f"Applied terminal state from gateway MCP-state sync ({status}).",
                )
                applied.append({"todo_key": normalized["todo_key"], "action": "prune", "status": status})
            else:
                skipped.append({"todo_key": normalized["todo_key"], "reason": f"terminal status {status} not applied"})
        except SyncError as exc:
            errors.append({"todo_key": normalized["todo_key"], "error": str(exc)})
    return {"applied": applied, "skipped": skipped, "errors": errors}


def apply_docs_to_local(client: LocalMcpClient, docs: list[dict[str, Any]]) -> dict[str, Any]:
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for doc in docs:
        doc_key = str(doc.get("doc_key", "")).strip()
        if not doc_key:
            skipped.append({"doc_key": "", "reason": "missing doc_key"})
            continue
        if not isinstance(doc.get("content"), str) or not doc.get("content"):
            skipped.append({"doc_key": doc_key, "reason": "missing content"})
            continue
        source_type = str(doc.get("source_type") or "manual")
        if source_type not in DOC_APPLY_SOURCE_TYPES:
            skipped.append({"doc_key": doc_key, "reason": f"source_type {source_type} is not locally upsertable"})
            continue
        try:
            client.doc_upsert(doc)
            applied.append({"doc_key": doc_key, "action": "upsert", "source_type": source_type})
        except SyncError as exc:
            errors.append({"doc_key": doc_key, "error": str(exc)})
    return {"applied": applied, "skipped": skipped, "errors": errors}


def resolve_local_client(args: argparse.Namespace, *, required: bool = True) -> LocalMcpClient | None:
    try:
        mcp_url = discover_mcp_url(args.mcp_url, args.timeout)
        if args.verbose:
            print(f"Using local MCP server: {mcp_url}", file=sys.stderr)
        return LocalMcpClient(mcp_url, args.timeout)
    except SyncError:
        if required:
            raise
        return None


def gateway_client(args: argparse.Namespace) -> GatewayClient:
    url = args.gateway_url or os.environ.get("MUDRA_GATEWAY_URL") or DEFAULT_GATEWAY_URL
    cf_headers = cloudflare_access_headers(args)
    hostname = (urllib.parse.urlsplit(normalize_base_url(url)).hostname or "").strip().lower()
    access_hostname = (
        urllib.parse.urlsplit(PROJECT.legacy_gateway_url).hostname or ""
    ).strip().lower()
    if access_hostname and hostname.endswith(access_hostname) and not cf_headers:
        raise SyncError(
            f"{url} is behind Cloudflare Access; requests without a service token get a 403. "
            f"Pass --cf-id/--cf-secret, or set {CF_ACCESS_CLIENT_ID_ENV_VARS[0]} and "
            f"{CF_ACCESS_CLIENT_SECRET_ENV_VARS[0]} (CF_ACCESS_CLIENT_ID/CF_ACCESS_CLIENT_SECRET "
            "or CF_CLIENT_ID/CF_CLIENT_SECRET also work)."
        )
    return GatewayClient(url, args.timeout, cf_headers=cf_headers)


def load_or_export_local_state(args: argparse.Namespace, *, include_docs: bool) -> dict[str, Any]:
    if args.state_file:
        state = read_json_file(args.state_file)
        if not isinstance(state, dict):
            raise SyncError("--state-file must contain a JSON object.")
        return state

    local = resolve_local_client(args, required=True)
    assert local is not None
    todos = export_local_todos(local, status=args.todo_status, app_scope=args.todo_scope, limit=args.todo_limit)
    docs = None
    if include_docs:
        docs = export_local_docs(
            local,
            scopes=args.doc_scope,
            todo_doc_refs=todo_doc_refs(todos),
            include_content=args.include_doc_content,
            include_all_content=args.include_all_doc_content,
            content_doc_keys=args.doc_key,
            max_chars=args.doc_max_chars,
        )
    return build_state_envelope(client=client_metadata(args.client_id), todos=todos, docs=docs)


def command_export(args: argparse.Namespace) -> int:
    state = load_or_export_local_state(args, include_docs=not args.no_docs)
    emit_payload(state, args)
    return 0


def command_preview(args: argparse.Namespace) -> int:
    state = load_or_export_local_state(args, include_docs=not args.no_docs)
    gateway = gateway_client(args)
    gateway_payload = gateway.get_todos()
    report = build_todo_report(
        extract_todos(state),
        extract_todos(gateway_payload),
        direction="push",
    )
    print_report(report)
    payload = {"local_state": state, "gateway_todos": gateway_payload, "todo_merge_report": report}
    emit_payload(payload, args, default_print_json=args.json)
    return 1 if args.fail_on_conflict and report["conflicts"] else 0


def command_push(args: argparse.Namespace) -> int:
    state = load_or_export_local_state(args, include_docs=args.with_docs)
    local_todos = extract_todos(state)
    gateway = gateway_client(args)
    try:
        gateway_todos_payload = gateway.get_todos()
    except SyncError as exc:
        if not args.dry_run:
            raise
        print(f"Dry run: gateway todo retrieval failed, so conflict reporting is local-only: {exc}")
        gateway_todos_payload = {"todos": [], "warning": str(exc)}
    gateway_todos = extract_todos(gateway_todos_payload)
    report = build_todo_report(local_todos, gateway_todos, direction="push")
    print_report(report)

    candidates = select_push_candidates(local_todos, report, force=args.force)
    patch_payload = build_todo_patch_payload(
        client=client_metadata(args.client_id),
        todos=candidates,
        mode=args.patch_mode,
        dry_run=args.server_dry_run,
        report=report,
    )
    result: Any = {"dry_run": True, "skipped_request": "PATCH /api/v1/mcp-state/todos"}
    if args.dry_run:
        print(f"\nDry run: would PATCH {len(candidates)} todo(s) to the gateway.")
    else:
        result = gateway.patch_todos(patch_payload)
        print(f"\nPatched {len(candidates)} todo(s) to the gateway.")

    docs_result: Any = None
    if args.with_docs:
        docs_payload = build_docs_sync_payload(
            client=client_metadata(args.client_id),
            docs=state.get("docs") if isinstance(state.get("docs"), dict) else None,
            mode="push",
            dry_run=args.server_dry_run,
        )
        if args.dry_run:
            docs_result = {"dry_run": True, "skipped_request": "POST /api/v1/mcp-state/docs:sync"}
            print("Dry run: would POST docs sync payload to the gateway.")
        else:
            docs_result = gateway.docs_sync(docs_payload)
            print("Posted docs sync payload to the gateway.")

    payload = {
        "todo_merge_report": report,
        "patched_todo_count": len(candidates),
        "patch_payload": patch_payload,
        "gateway_response": result,
        "docs_response": docs_result,
    }
    emit_payload(payload, args, default_print_json=args.json)
    return 1 if args.fail_on_conflict and report["conflicts"] else 0


def command_pull(args: argparse.Namespace) -> int:
    gateway = gateway_client(args)
    gateway_payload = gateway.get_todos()
    gateway_todos = extract_todos(gateway_payload)
    local_todos: list[dict[str, Any]] = []
    local = None
    if args.apply_local or args.compare_local:
        local = resolve_local_client(args, required=True)
        assert local is not None
        local_todos = export_local_todos(local, status="open", app_scope=args.todo_scope, limit=args.todo_limit)

    report = build_todo_report(local_todos, gateway_todos, direction="pull") if local_todos else {
        "direction": "pull",
        "local_count": 0,
        "gateway_count": len(gateway_todos),
        "summary": {"same": 0, "local_only": 0, "gateway_only": len(gateway_todos), "changed": 0, "conflicts": 0},
        "same": [],
        "local_only": [],
        "gateway_only": [todo_summary(todo) for todo in gateway_todos],
        "changed": [],
        "conflicts": [],
    }
    print_report(report)

    apply_result = None
    if args.apply_local:
        if args.dry_run:
            print(f"\nDry run: would apply up to {len(gateway_todos)} gateway todo(s) locally.")
            apply_result = {"dry_run": True}
        else:
            assert local is not None
            candidates = select_pull_candidates(gateway_todos, report, force=args.force)
            apply_result = apply_todos_to_local(local, candidates, apply_closed=args.apply_closed)
            print(
                "\nApplied locally: "
                f"{len(apply_result['applied'])} applied, "
                f"{len(apply_result['skipped'])} skipped, "
                f"{len(apply_result['errors'])} errors."
            )

    payload = {
        "gateway_todos": gateway_payload,
        "todo_merge_report": report,
        "local_apply_result": apply_result,
    }
    emit_payload(payload, args, default_print_json=args.json)
    return 1 if args.fail_on_conflict and report["conflicts"] else 0


def command_patch(args: argparse.Namespace) -> int:
    raw = read_json_file(args.patch_file)
    if isinstance(raw, dict) and "todos" in raw:
        payload = raw
    elif isinstance(raw, list):
        payload = build_todo_patch_payload(
            client=client_metadata(args.client_id),
            todos=[normalize_todo(item) for item in raw if isinstance(item, dict)],
            mode=args.patch_mode,
            dry_run=args.server_dry_run,
        )
    elif isinstance(raw, dict):
        payload = build_todo_patch_payload(
            client=client_metadata(args.client_id),
            todos=[normalize_todo(raw)],
            mode=args.patch_mode,
            dry_run=args.server_dry_run,
        )
    else:
        raise SyncError("--patch-file must contain a todo object, a list of todo objects, or a patch object.")
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("generated_at", utc_now_iso())
    payload.setdefault("client", client_metadata(args.client_id))
    payload.setdefault("dry_run", args.server_dry_run)
    payload.setdefault("mode", args.patch_mode)

    if args.dry_run:
        result: Any = {"dry_run": True, "skipped_request": "PATCH /api/v1/mcp-state/todos"}
        print(f"Dry run: would PATCH {len(extract_todos(payload))} todo(s) to the gateway.")
    else:
        result = gateway_client(args).patch_todos(payload)
        print(f"Patched {len(extract_todos(payload))} todo(s) to the gateway.")
    emit_payload({"patch_payload": payload, "gateway_response": result}, args, default_print_json=args.json)
    return 0


def command_docs_sync(args: argparse.Namespace) -> int:
    docs_state: dict[str, Any] | None = None
    if args.state_file:
        state = read_json_file(args.state_file)
        if not isinstance(state, dict):
            raise SyncError("--state-file must contain a JSON object.")
        docs = state.get("docs")
        if isinstance(docs, dict):
            docs_state = docs
        elif "documents" in state:
            docs_state = state
    elif args.docs_mode in {"push", "merge"}:
        local = resolve_local_client(args, required=True)
        assert local is not None
        todos = export_local_todos(local, status=args.todo_status, app_scope=args.todo_scope, limit=args.todo_limit)
        docs_state = export_local_docs(
            local,
            scopes=args.doc_scope,
            todo_doc_refs=todo_doc_refs(todos),
            include_content=args.include_doc_content,
            include_all_content=args.include_all_doc_content,
            content_doc_keys=args.doc_key,
            max_chars=args.doc_max_chars,
        )

    payload = build_docs_sync_payload(
        client=client_metadata(args.client_id),
        docs=docs_state,
        mode=args.docs_mode,
        dry_run=args.server_dry_run,
    )
    if args.dry_run:
        result: Any = {"dry_run": True, "skipped_request": "POST /api/v1/mcp-state/docs:sync"}
        print("Dry run: would POST docs sync payload to the gateway.")
    else:
        result = gateway_client(args).docs_sync(payload)
        print("Posted docs sync payload to the gateway.")

    apply_result = None
    if args.apply_local:
        local = resolve_local_client(args, required=True)
        assert local is not None
        docs_to_apply = extract_documents(result)
        if args.dry_run:
            apply_result = {"dry_run": True, "document_count": len(docs_to_apply)}
            print(f"Dry run: would apply {len(docs_to_apply)} pulled doc(s) locally.")
        else:
            apply_result = apply_docs_to_local(local, docs_to_apply)
            print(
                "Applied docs locally: "
                f"{len(apply_result['applied'])} applied, "
                f"{len(apply_result['skipped'])} skipped, "
                f"{len(apply_result['errors'])} errors."
            )

    emit_payload(
        {"docs_sync_payload": payload, "gateway_response": result, "local_apply_result": apply_result},
        args,
        default_print_json=args.json,
    )
    return 0


def emit_payload(payload: Any, args: argparse.Namespace, *, default_print_json: bool = True) -> None:
    if args.output:
        write_json_file(args.output, payload)
        print(f"Wrote {args.output}")
    if default_print_json:
        print(pretty_json(payload))


def add_common_args(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    bool_default = argparse.SUPPRESS if suppress_defaults else False
    timeout_default: float | str = argparse.SUPPRESS if suppress_defaults else 10.0
    parser.add_argument(
        "--mcp-url",
        default=default,
        help=(
            "Local MCP HTTP base URL. Defaults to MUDRA_MCP_URL, then "
            "config.yaml mcp.server_ip/mcp.server_port, then localhost probing."
        ),
    )
    parser.add_argument(
        "--gateway-url",
        default=default,
        help=f"Gateway base URL. Defaults to MUDRA_GATEWAY_URL or {DEFAULT_GATEWAY_URL}.",
    )
    parser.add_argument(
        "--cf-id",
        default=default,
        help=(
            "Cloudflare Access service-token Client ID, required for gateway hosts behind "
            "Access (e.g. mudra.nml.wtf). Defaults to MUDRA_CF_ACCESS_CLIENT_ID, "
            "CF_ACCESS_CLIENT_ID, or CF_CLIENT_ID."
        ),
    )
    parser.add_argument(
        "--cf-secret",
        default=default,
        help=(
            "Cloudflare Access service-token Client Secret. Defaults to "
            "MUDRA_CF_ACCESS_CLIENT_SECRET, CF_ACCESS_CLIENT_SECRET, or CF_CLIENT_SECRET."
        ),
    )
    parser.add_argument("--timeout", type=float, default=timeout_default, help="HTTP timeout in seconds.")
    parser.add_argument("--client-id", default=default, help="Client id recorded in gateway payload metadata.")
    parser.add_argument("--output", default=default, help="Write full JSON output to this file.")
    parser.add_argument(
        "--json",
        action="store_true",
        default=bool_default,
        help="Print full JSON output after the human-readable summary.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=bool_default,
        help="Print discovery details to stderr.",
    )


def add_local_export_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-file", help="Use a previously exported state JSON file instead of reading local MCP.")
    parser.add_argument("--todo-status", default="open", help="Local todo status filter for export/push/docs refs.")
    parser.add_argument("--todo-scope", default="", help="Local todo app_scope filter.")
    parser.add_argument("--todo-limit", type=int, default=100, help="Maximum local todos to request from /api/dashboard/todos.")
    parser.add_argument("--doc-scope", action="append", default=[], help="Documentation scope to export. Repeatable.")
    parser.add_argument("--doc-key", action="append", default=[], help="Specific doc_key whose content should be exported. Repeatable.")
    parser.add_argument("--doc-max-chars", type=int, default=50_000, help="Maximum chars fetched for each content-bearing doc.")
    parser.add_argument("--include-doc-content", action="store_true", help="Fetch content for todo-referenced docs and --doc-key docs.")
    parser.add_argument("--include-all-doc-content", action="store_true", help="Fetch content for every exported doc entry.")


def add_gateway_write_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Preview without mutating local MCP or gateway state.")
    parser.add_argument("--server-dry-run", action="store_true", help="Send dry_run=true in gateway payloads when a request is made.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync local Mudra MCP todos and docs state through gateway MCP-state endpoints.",
        epilog=(
            "Examples:\n"
            "  python -m mcp.state_sync export --output mcp-state.json\n"
            "  python -m mcp.state_sync preview --gateway-url http://127.0.0.1:2999\n"
            "  python -m mcp.state_sync push --dry-run\n"
            "  python -m mcp.state_sync pull --apply-local --dry-run\n"
            "  python -m mcp.state_sync docs-sync --docs-mode push --include-doc-content\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common_args(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export local MCP todos/docs state as JSON.")
    add_common_args(export_parser, suppress_defaults=True)
    add_local_export_args(export_parser)
    export_parser.add_argument("--no-docs", action="store_true", help="Export todos only.")
    export_parser.set_defaults(func=command_export)

    preview_parser = subparsers.add_parser("preview", help="Compare local todos with gateway todos without writing.")
    add_common_args(preview_parser, suppress_defaults=True)
    add_local_export_args(preview_parser)
    preview_parser.add_argument("--no-docs", action="store_true", help="Skip docs in the local preview envelope.")
    preview_parser.add_argument("--fail-on-conflict", action="store_true", help="Exit 1 if merge conflicts are detected.")
    preview_parser.set_defaults(func=command_preview)

    push_parser = subparsers.add_parser("push", help="Patch gateway todos from local/exported MCP state.")
    add_common_args(push_parser, suppress_defaults=True)
    add_local_export_args(push_parser)
    add_gateway_write_args(push_parser)
    push_parser.add_argument("--patch-mode", choices=("merge", "replace"), default="merge", help="Gateway patch mode.")
    push_parser.add_argument("--force", action="store_true", help="Include local todos even when gateway appears newer.")
    push_parser.add_argument("--fail-on-conflict", action="store_true", help="Exit 1 if merge conflicts are detected.")
    push_parser.add_argument("--with-docs", action="store_true", help="Also send a docs sync push payload after todo patch.")
    push_parser.set_defaults(func=command_push)

    pull_parser = subparsers.add_parser("pull", help="Retrieve gateway todos and optionally apply them locally.")
    add_common_args(pull_parser, suppress_defaults=True)
    add_gateway_write_args(pull_parser)
    pull_parser.add_argument("--todo-scope", default="", help="Local todo app_scope filter used for comparison.")
    pull_parser.add_argument("--todo-limit", type=int, default=100, help="Maximum local todos to request for comparison.")
    pull_parser.add_argument("--compare-local", action="store_true", help="Compare gateway todos to local open todos.")
    pull_parser.add_argument("--apply-local", action="store_true", help="Apply pulled open todos to local MCP.")
    pull_parser.add_argument("--apply-closed", action="store_true", help="Also apply terminal done/dropped states locally.")
    pull_parser.add_argument("--force", action="store_true", help="Apply gateway todos even when local appears newer.")
    pull_parser.add_argument("--fail-on-conflict", action="store_true", help="Exit 1 if merge conflicts are detected.")
    pull_parser.set_defaults(func=command_pull)

    patch_parser = subparsers.add_parser("patch", help="Send a JSON todo patch file to the gateway.")
    add_common_args(patch_parser, suppress_defaults=True)
    add_gateway_write_args(patch_parser)
    patch_parser.add_argument("--patch-file", required=True, help="JSON todo object/list or gateway patch object.")
    patch_parser.add_argument("--patch-mode", choices=("merge", "replace"), default="merge", help="Gateway patch mode.")
    patch_parser.set_defaults(func=command_patch)

    docs_parser = subparsers.add_parser("docs-sync", help="Sync documentation state through the gateway docs endpoint.")
    add_common_args(docs_parser, suppress_defaults=True)
    add_local_export_args(docs_parser)
    add_gateway_write_args(docs_parser)
    docs_parser.add_argument("--docs-mode", choices=("push", "pull", "merge"), default="push", help="Gateway docs sync mode.")
    docs_parser.add_argument("--apply-local", action="store_true", help="Apply content-bearing pulled manual/inferred docs locally.")
    docs_parser.set_defaults(func=command_docs_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
