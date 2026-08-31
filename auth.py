"""Remote-origin authorization policy for the MCP HTTP transport.

The policy deliberately does not verify JWT signatures. Cloudflare Access is
the trust boundary; this module only enforces the origin-side evidence and
service-token allowlist required when the origin is exposed remotely.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from mcp.project import load_project_descriptor


PROJECT = load_project_descriptor()
REMOTE_AUTH_MODES = {"off", "cloudflare"}
REMOTE_AUTH_ENV_VAR = f"{PROJECT.cloudflare_env_prefix}_MCP_REMOTE_AUTH"
SERVICE_TOKEN_IDS_ENV_VAR = f"{PROJECT.cloudflare_env_prefix}_MCP_SERVICE_TOKEN_IDS"
CF_ACCESS_CLIENT_ID_HEADER = "CF-Access-Client-Id"
CF_ACCESS_JWT_HEADER = "Cf-Access-Jwt-Assertion"


def jwt_unverified_claims(token: str) -> dict[str, Any]:
    """Decode JWT payload claims as an attribution hint, never as proof."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _split_service_token_ids(value: str) -> list[str]:
    return [item for item in re.split(r"[,\s]+", value.strip()) if item]


@dataclass(frozen=True)
class RemoteAuthPolicy:
    """Cloudflare Access origin policy used by the HTTP transport."""

    mode: str = "off"
    service_token_ids: frozenset[str] = frozenset()

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @classmethod
    def from_env(
        cls,
        *,
        mode: str | None = None,
        extra_service_token_ids: list[str] | None = None,
    ) -> "RemoteAuthPolicy":
        env_mode = (os.environ.get(REMOTE_AUTH_ENV_VAR) or "").strip().lower()
        chosen = (mode or env_mode or "off").strip().lower()
        if chosen not in REMOTE_AUTH_MODES:
            chosen = "off"
        ids = _split_service_token_ids(os.environ.get(SERVICE_TOKEN_IDS_ENV_VAR) or "")
        for extra in extra_service_token_ids or []:
            ids.extend(_split_service_token_ids(extra))
        return cls(mode=chosen, service_token_ids=frozenset(ids))


REMOTE_AUTH_DISABLED = RemoteAuthPolicy()
