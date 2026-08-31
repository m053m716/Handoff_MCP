"""Small config helpers shared by repo-local MCP scripts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from mcp.project import load_project_descriptor

PROJECT = load_project_descriptor()

REPO_ROOT = PROJECT.repo_root
CONFIG_PATH = REPO_ROOT / "config.yaml"
DEFAULT_MCP_SERVER_IP = PROJECT.default_mcp_host
DEFAULT_MCP_SERVER_PORT = PROJECT.default_mcp_port
MCP_HOST_ENV_VAR = f"{PROJECT.cloudflare_env_prefix}_MCP_HOST"
MCP_PORT_ENV_VAR = f"{PROJECT.cloudflare_env_prefix}_MCP_PORT"
MCP_URL_PREFIX_ENV_VAR = f"{PROJECT.cloudflare_env_prefix}_MCP_URL_PREFIX"
LOCAL_MCP_FALLBACK_IP = "127.0.0.1"
LOCAL_MCP_FALLBACK_HOSTS = ("127.0.0.1", "localhost")
LOCAL_MCP_BIND_HOSTS = {"0.0.0.0", "::", "::1", "", "127.0.0.1", "localhost"}


@dataclass(frozen=True)
class McpServerConfig:
    server_ip: str = DEFAULT_MCP_SERVER_IP
    server_port: int = DEFAULT_MCP_SERVER_PORT
    url_prefix: str = ""

    @property
    def base_url(self) -> str:
        return mcp_base_url(self.server_ip, self.server_port, self.url_prefix)


def load_mcp_server_config(
    config_path: Path | None = None,
    *,
    include_env: bool = True,
) -> McpServerConfig:
    path = Path(config_path) if config_path is not None else CONFIG_PATH
    values = _read_mcp_config_values(path)

    server_ip = _as_host(values.get("server_ip"), DEFAULT_MCP_SERVER_IP)
    server_port = _as_port(values.get("server_port"), DEFAULT_MCP_SERVER_PORT)
    url_prefix = _as_url_prefix(values.get("url_prefix"), "")

    if include_env:
        server_ip = _as_host(os.environ.get(MCP_HOST_ENV_VAR), server_ip)
        server_port = _as_port(os.environ.get(MCP_PORT_ENV_VAR), server_port)
        url_prefix = _as_url_prefix(os.environ.get(MCP_URL_PREFIX_ENV_VAR), url_prefix)

    return McpServerConfig(
        server_ip=server_ip,
        server_port=server_port,
        url_prefix=url_prefix,
    )


def mcp_base_url(host: str, port: int, url_prefix: str = "") -> str:
    host_value = host.strip()
    if ":" in host_value and not host_value.startswith("["):
        host_value = f"[{host_value}]"
    return f"http://{host_value}:{int(port)}{_as_url_prefix(url_prefix, '')}"


def mcp_http_url_candidates(
    *,
    config_path: Path | None = None,
    include_env: bool = True,
) -> tuple[str, ...]:
    config = load_mcp_server_config(config_path, include_env=include_env)
    candidates = [
        config.base_url,
        mcp_base_url(LOCAL_MCP_FALLBACK_IP, config.server_port, config.url_prefix),
        mcp_base_url("localhost", config.server_port, config.url_prefix),
    ]
    return _dedupe(candidates)


def mcp_bind_host_candidates(host: str) -> tuple[str, ...]:
    host_value = (host or "").strip() or DEFAULT_MCP_SERVER_IP
    candidates = [host_value]
    if host_value.lower() not in LOCAL_MCP_BIND_HOSTS:
        candidates.append(LOCAL_MCP_FALLBACK_IP)
    return _dedupe(candidates)


def mcp_origin_hosts(
    *,
    config_path: Path | None = None,
    include_env: bool = True,
) -> set[str]:
    config = load_mcp_server_config(config_path, include_env=include_env)
    hosts = {"::1", *LOCAL_MCP_FALLBACK_HOSTS}
    if config.server_ip:
        hosts.add(config.server_ip)
    remote_host = urlsplit(PROJECT.remote_base_url).hostname
    if remote_host:
        hosts.add(remote_host)
    return hosts


def _read_mcp_config_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    in_mcp_section = False
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if raw_line[:1] not in {" ", "\t"}:
            key, separator, _value = stripped.partition(":")
            in_mcp_section = separator == ":" and key.strip() == "mcp"
            continue

        if not in_mcp_section or ":" not in stripped:
            continue

        key, _separator, value = stripped.partition(":")
        key = key.strip()
        if key in {"server_ip", "server_port", "url_prefix"}:
            values[key] = _clean_yaml_scalar(value)

    return values


def _clean_yaml_scalar(value: str) -> str:
    text = value.split("#", 1)[0].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1].strip()
    return text


def _as_host(value: str | None, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _as_port(value: str | int | None, default: int) -> int:
    if value is None:
        return default
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if 1 <= port <= 65535:
        return port
    return default


def _as_url_prefix(value: str | None, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    if not text or text == "/":
        return ""
    return "/" + text.strip("/")


def _dedupe(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return tuple(result)
