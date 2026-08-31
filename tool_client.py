"""Reusable named-tool client for the Mudra MCP JSON-RPC transport.

Runtime code should depend on :class:`McpToolClient` rather than constructing
JSON-RPC envelopes or importing the HTTP helpers directly.  The optional
``transport`` hook keeps contract tests independent of a live MCP server; it
accepts ``(base_url, method, params, timeout)`` and returns the decoded JSON
response envelope.
"""

from __future__ import annotations

import json
import os
import random
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Protocol
from urllib.parse import urlsplit

from mcp.config import mcp_http_url_candidates
from mcp.project import load_project_descriptor
from mcp.state_sync import (
    CF_ACCESS_CLIENT_ID_ENV_VARS,
    CF_ACCESS_CLIENT_ID_HEADER,
    CF_ACCESS_CLIENT_SECRET_ENV_VARS,
    CF_ACCESS_CLIENT_SECRET_HEADER,
    HttpError,
    SyncError,
    join_url,
    normalize_base_url,
    pretty_json,
    request_json,
    resolve_secret,
)

PROJECT = load_project_descriptor()
REMOTE_HOSTNAME = (urlsplit(PROJECT.remote_base_url).hostname or "").lower()
DEFAULT_TIMEOUT = 30.0
FALLTHROUGH_HTTP_STATUSES = {400, 404, 405}
RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class ToolCallError(SyncError):
    """The server answered but the named tool call failed."""


class Transport(Protocol):
    def __call__(
        self,
        base_url: str,
        method: str,
        params: dict[str, Any] | None,
        timeout: float,
    ) -> Any: ...


def cloudflare_headers_available() -> bool:
    return bool(
        resolve_secret(None, CF_ACCESS_CLIENT_ID_ENV_VARS)
        and resolve_secret(None, CF_ACCESS_CLIENT_SECRET_ENV_VARS)
    )


def is_remote_url(base_url: str) -> bool:
    host = (urlsplit(normalize_base_url(base_url)).hostname or "").lower()
    return bool(REMOTE_HOSTNAME) and host == REMOTE_HOSTNAME


def headers_for(base_url: str) -> dict[str, str]:
    """Return transport headers without exposing credentials to callers."""

    headers = {"Accept": "application/json, text/event-stream"}
    if is_remote_url(base_url):
        client_id = resolve_secret(None, CF_ACCESS_CLIENT_ID_ENV_VARS)
        client_secret = resolve_secret(None, CF_ACCESS_CLIENT_SECRET_ENV_VARS)
        if not client_id or not client_secret:
            raise SyncError(
                f"{base_url} needs Cloudflare Access credentials; set "
                f"{CF_ACCESS_CLIENT_ID_ENV_VARS[-1]} and {CF_ACCESS_CLIENT_SECRET_ENV_VARS[-1]} "
                "(or their MUDRA_/CF_ACCESS_ aliases)."
            )
        headers[CF_ACCESS_CLIENT_ID_HEADER] = client_id
        headers[CF_ACCESS_CLIENT_SECRET_HEADER] = client_secret
    return headers


def remote_candidates() -> list[str]:
    """Return the canonical deployment root and configured MCP prefix."""

    parts = urlsplit(PROJECT.remote_base_url)
    root = f"{parts.scheme}://{parts.netloc}"
    candidates = [root]
    if PROJECT.remote_base_url.rstrip("/") != root:
        candidates.append(PROJECT.remote_base_url)
    return candidates


def endpoint_candidates(explicit_url: str | None, prefer_local: bool) -> list[str]:
    ordered: list[str] = []
    if explicit_url:
        ordered.append(explicit_url)
    env_url = os.environ.get("MUDRA_MCP_URL", "").strip()
    if env_url:
        ordered.append(env_url)
    local = list(mcp_http_url_candidates())
    remote = remote_candidates()
    ordered.extend(local + remote if prefer_local else remote + local)

    deduped: list[str] = []
    for candidate in ordered:
        base_url = normalize_base_url(candidate)
        if base_url not in deduped:
            deduped.append(base_url)
    return deduped


def rpc_request(
    base_url: str,
    method: str,
    params: dict[str, Any] | None,
    timeout: float,
) -> Any:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": f"mudra-rpc-{os.getpid()}",
        "method": method,
    }
    if params is not None:
        payload["params"] = params
    return request_json(
        "POST",
        join_url(base_url, "/mcp"),
        payload=payload,
        timeout=timeout,
        extra_headers=headers_for(base_url),
    )


def rpc_request_with_fallback(
    candidates: Sequence[str],
    method: str,
    params: dict[str, Any] | None,
    timeout: float,
    errors_out: list[str] | None = None,
    *,
    request_fn: Transport | None = None,
) -> tuple[str, Any]:
    """Call the first usable endpoint, retaining the CLI's error behavior."""

    request = request_fn or rpc_request
    errors: list[str] = errors_out if errors_out is not None else []
    for base_url in candidates:
        try:
            return base_url, request(base_url, method, params, timeout)
        except HttpError as exc:
            if exc.status not in FALLTHROUGH_HTTP_STATUSES:
                raise
            errors.append(f"{base_url}: HTTP {exc.status}")
        except SyncError as exc:
            errors.append(f"{base_url}: {exc}")
    detail = "\n".join(f"  - {error}" for error in errors)
    raise SyncError(
        "No Mudra MCP endpoint answered. Tried:\n" + detail + "\n"
        "Set MUDRA_MCP_URL or pass --url; for the remote deployment also set "
        "CF_CLIENT_ID / CF_CLIENT_SECRET."
    )


def compactify(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def unwrap_tool_result(name: str, response: Any) -> Any:
    if isinstance(response, dict) and "error" in response:
        raise ToolCallError(f"MCP tool {name} failed: {compactify(response['error'])}")
    result = response.get("result") if isinstance(response, dict) else None
    if isinstance(result, dict) and result.get("isError"):
        text = ""
        content = result.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text = str(content[0].get("text", ""))
        raise ToolCallError(f"MCP tool {name} failed: {text or compactify(result)}")
    if isinstance(result, dict) and "structuredContent" in result:
        return result["structuredContent"]
    return result


def _tool_names(response: Any) -> set[str]:
    if isinstance(response, dict) and "error" in response:
        raise SyncError(f"tools/list failed: {compactify(response['error'])}")
    result = response.get("result") if isinstance(response, dict) else None
    tools = result.get("tools", []) if isinstance(result, dict) else []
    return {
        str(tool["name"])
        for tool in tools
        if isinstance(tool, dict) and tool.get("name")
    }


class McpToolClient:
    """Call named MCP tools through the configured remote/local endpoints."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        candidates: Sequence[str] | None = None,
        prefer_local: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | Any | None = None,
        required_tools: Iterable[str] = (),
        negotiate_tools: bool = True,
        max_attempts: int = 3,
        backoff_initial: float = 0.1,
        backoff_max: float = 1.0,
        sleeper: Callable[[float], None] = time.sleep,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.candidates = (
            [normalize_base_url(candidate) for candidate in candidates]
            if candidates is not None
            else endpoint_candidates(base_url, prefer_local)
        )
        self.timeout = timeout
        self.transport = transport
        self.required_tools = tuple(str(name) for name in required_tools)
        self.negotiate_tools = negotiate_tools
        self.max_attempts = max_attempts
        self.backoff_initial = max(0.0, backoff_initial)
        self.backoff_max = max(self.backoff_initial, backoff_max)
        self._sleep = sleeper
        self._random = random_fn
        self._available_tools: set[str] | None = None
        self._last_endpoint: str | None = None

    @property
    def last_endpoint(self) -> str | None:
        """The endpoint used by the most recent successful request."""

        return self._last_endpoint

    def _request(self, base_url: str, method: str, params: dict[str, Any] | None, timeout: float) -> Any:
        if self.transport is None:
            return rpc_request(base_url, method, params, timeout)
        request = self.transport
        if not callable(request):
            request = getattr(request, "request", None)
        if not callable(request):
            raise TypeError("transport must be callable or provide request()")
        return request(base_url, method, params, timeout)

    def _request_with_retry(self, method: str, params: dict[str, Any] | None) -> Any:
        deadline = time.monotonic() + self.timeout
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                endpoint, response = rpc_request_with_fallback(
                    self.candidates,
                    method,
                    params,
                    remaining,
                    request_fn=self._request,
                )
                self._last_endpoint = endpoint
                return response
            except HttpError as exc:
                last_error = exc
                retryable = exc.status in RETRYABLE_HTTP_STATUSES
            except SyncError as exc:
                last_error = exc
                retryable = True
            if not retryable or attempt + 1 >= self.max_attempts:
                raise
            delay = min(self.backoff_max, self.backoff_initial * (2**attempt))
            delay *= 0.5 + self._random()
            self._sleep(min(delay, max(0.0, deadline - time.monotonic())))
        if last_error is not None:
            raise last_error
        raise SyncError("MCP tool call deadline expired.")

    def list_tools(self, *, force: bool = False) -> set[str]:
        if self._available_tools is None or force:
            self._available_tools = _tool_names(self._request_with_retry("tools/list", {}))
        return set(self._available_tools)

    def _validate_tools(self, tool_name: str) -> None:
        if not self.negotiate_tools:
            return
        required = set(self.required_tools)
        required.add(tool_name)
        available = self.list_tools()
        missing = sorted(required - available)
        if missing:
            raise SyncError(f"MCP server is missing required tool(s): {', '.join(missing)}")

    def call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call one named MCP tool and return structured content when present."""

        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("tool_name must be a non-empty string")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise TypeError("arguments must be a JSON object")
        self._validate_tools(tool_name)
        response = self._request_with_retry(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
        )
        return unwrap_tool_result(tool_name, response)


__all__ = [
    "DEFAULT_TIMEOUT",
    "FALLTHROUGH_HTTP_STATUSES",
    "McpToolClient",
    "PROJECT",
    "ToolCallError",
    "cloudflare_headers_available",
    "compactify",
    "endpoint_candidates",
    "headers_for",
    "is_remote_url",
    "remote_candidates",
    "rpc_request",
    "rpc_request_with_fallback",
    "unwrap_tool_result",
]
