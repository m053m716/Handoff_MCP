"""Project descriptor types and selection for the reusable MCP engine.

The engine modules (``mcp.config``, ``mcp.server``, ``mcp.state_sync``) resolve
their active :class:`ProjectDescriptor` through :func:`load_project_descriptor`
instead of importing a concrete descriptor such as ``MUDRA_PROJECT`` directly.
Another repository reuses the engine by shipping its own descriptor module and
selecting it with the ``MCP_PROJECT_MODULE`` environment variable (or the
``--project-module`` server flag), for example::

    MCP_PROJECT_MODULE=acme_mcp_project:ACME_PROJECT python -m mcp.server --http
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

PROJECT_MODULE_ENV_VAR = "MCP_PROJECT_MODULE"
DEFAULT_PROJECT_MODULE = "mcp.projects.mudra:MUDRA_PROJECT"
DEFAULT_PROJECT_ATTR = "PROJECT"
PROJECT_MODULE_FLAG = "--project-module"


@dataclass(frozen=True)
class ProjectDescriptor:
    """Configuration supplied by a repo that hosts the reusable MCP engine."""

    key: str
    display_name: str
    server_name: str
    resource_scheme: str
    repo_root: Path
    db_path: Path
    gui_dir: Path
    doc_scopes: Mapping[str, Mapping[str, Any]]
    doc_drift_audit_scope: str
    default_mcp_host: str
    default_mcp_port: int
    remote_base_url: str
    legacy_gateway_url: str
    cloudflare_env_prefix: str


def load_project_descriptor(module_path: str | None = None) -> ProjectDescriptor:
    """Resolve the active project descriptor.

    ``module_path`` is a ``package.module:ATTR`` spec. When ``ATTR`` is omitted
    the module must expose either a ``PROJECT`` attribute or exactly one
    :class:`ProjectDescriptor` instance. Resolution order: explicit argument,
    then the ``MCP_PROJECT_MODULE`` environment variable, then the built-in
    Mudra default.
    """

    spec = (module_path or "").strip()
    if not spec:
        spec = os.environ.get(PROJECT_MODULE_ENV_VAR, "").strip()
    if not spec:
        spec = DEFAULT_PROJECT_MODULE

    module_name, _, attr_name = spec.partition(":")
    module_name = module_name.strip()
    attr_name = attr_name.strip()
    if not module_name:
        raise ValueError(
            f"Invalid project module spec {spec!r}; expected 'package.module:ATTR'."
        )

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(
            f"Unable to import project descriptor module {module_name!r} "
            f"(from spec {spec!r}): {exc}"
        ) from exc

    if attr_name:
        descriptor = getattr(module, attr_name, None)
        if descriptor is None:
            raise AttributeError(
                f"Project descriptor module {module_name!r} has no attribute "
                f"{attr_name!r}. Available ProjectDescriptor attributes: "
                f"{_descriptor_attr_names(module) or 'none'}."
            )
    else:
        descriptor = getattr(module, DEFAULT_PROJECT_ATTR, None)
        if descriptor is None:
            candidates = _descriptor_attrs(module)
            if len(candidates) != 1:
                raise AttributeError(
                    f"Project descriptor module {module_name!r} defines no "
                    f"'{DEFAULT_PROJECT_ATTR}' attribute and "
                    f"{len(candidates)} ProjectDescriptor instance(s); use an "
                    f"explicit '{module_name}:ATTR' spec."
                )
            descriptor = candidates[0][1]

    if not isinstance(descriptor, ProjectDescriptor):
        raise TypeError(
            f"Project module spec {spec!r} resolved to "
            f"{type(descriptor).__name__}, not ProjectDescriptor."
        )
    return descriptor


def project_module_from_argv(argv: list[str] | None) -> str | None:
    """Extract the last ``--project-module`` value from ``argv``, if any.

    This runs before argparse so descriptor-derived module constants can be
    bound at import time; the flag is still declared in the server CLI for
    ``--help`` discoverability.
    """

    value: str | None = None
    args = list(argv or [])
    for index, arg in enumerate(args):
        if arg == PROJECT_MODULE_FLAG:
            if index + 1 < len(args):
                value = args[index + 1]
        elif arg.startswith(PROJECT_MODULE_FLAG + "="):
            value = arg.split("=", 1)[1]
    value = (value or "").strip()
    return value or None


def _descriptor_attrs(module: Any) -> list[tuple[str, ProjectDescriptor]]:
    return [
        (name, value)
        for name, value in sorted(vars(module).items())
        if isinstance(value, ProjectDescriptor)
    ]


def _descriptor_attr_names(module: Any) -> str:
    return ", ".join(name for name, _value in _descriptor_attrs(module))

