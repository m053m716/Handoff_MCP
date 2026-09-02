"""Command-line entry point.

    handoff-mcp serve            # run the stdio MCP server (for agent clients)
    handoff-mcp gui [--port N]   # open the loopback viewer in a browser
    handoff-mcp list             # print open handoffs/todos for this project
    handoff-mcp whoami           # print the resolved project identity
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .project import resolve_project
from .storage import Store, default_db_path


def _cmd_serve(_args: argparse.Namespace) -> int:
    from .server import run_stdio

    run_stdio()
    return 0


#: Default viewer port, shared by the CLI and the generated VS Code task.
DEFAULT_GUI_PORT = 8765

#: Range for per-repo derived viewer ports (avoids the low registered range).
_DERIVED_PORT_BASE = 8765
_DERIVED_PORT_SPAN = 1000


def _derived_port(project_key: str) -> int:
    """A stable viewer port for a repo, derived from its project key.

    Distinct repos get distinct ports (within a 1000-port window above the
    default), so each repo's committed VS Code task binds its own port and two
    viewers can be open at once without shadowing each other.
    """

    offset = int(project_key[:8], 16) % _DERIVED_PORT_SPAN if project_key else 0
    return _DERIVED_PORT_BASE + offset


def _cmd_gui(args: argparse.Namespace) -> int:
    from .gui import run_gui

    # A None port means the flag was not supplied, so a busy default port may
    # advance to the next free one; an explicit --port stays put or fails.
    port_explicit = args.port is not None
    port = args.port if port_explicit else DEFAULT_GUI_PORT
    run_gui(host=args.host, port=port, open_target=args.open, port_explicit=port_explicit)
    return 0


def _cmd_list(_args: argparse.Namespace) -> int:
    project = resolve_project()
    store = Store(project.key)
    print(f"Project: {project.label} ({project.key})")
    print(f"Root:    {project.root}")
    print(f"DB:      {default_db_path()}\n")

    handoffs = store.list_handoffs(status="open", limit=200)
    print(f"Open handoffs ({len(handoffs)}):")
    for h in handoffs:
        print(f"  {h['id']}: {h['summary']}")
        if h["next_steps"]:
            print(f"       next: {h['next_steps']}")
    if not handoffs:
        print("  (none)")

    todos = store.list_todos(status="open", limit=200)
    print(f"\nOpen todos ({len(todos)}):")
    for t in todos:
        print(f"  {t['id']} [P{t['priority']}]: {t['title']}")
    if not todos:
        print("  (none)")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    """Write a project-local `.mcp.json` that pins this repo's scope.

    Placing the resolved repo root in `HANDOFF_MCP_PROJECT_ROOT` makes the
    project key correct regardless of the working directory the MCP client
    happens to launch the server from. The file is safe to commit.
    """

    project = resolve_project(args.path)
    config_path = project.root / ".mcp.json"

    entry = {
        "command": "handoff-mcp",
        "args": ["serve"],
        "env": {"HANDOFF_MCP_PROJECT_ROOT": str(project.root)},
    }

    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            print(f"Refusing to overwrite unreadable {config_path}", file=sys.stderr)
            return 1

    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        print(f"{config_path} has a non-object 'mcpServers'; not modifying.", file=sys.stderr)
        return 1
    if "handoff" in servers and not args.force:
        print(
            f"'handoff' is already registered in {config_path}. "
            f"Re-run with --force to overwrite.",
        )
        return 0

    servers["handoff"] = entry
    config_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {config_path}")
    print(f"  project_key:  {project.key}")
    print(f"  project_root: {project.root}")
    print("\nThe 'handoff' MCP server is now scoped to this repo for any client")
    print("that reads project-local .mcp.json. Commit the file to share the setup.")

    if args.vscode:
        port = args.port if args.port is not None else _derived_port(project.key)
        _write_vscode_task(project.root, port=port, force=args.force)
    return 0


def _write_vscode_task(root: Path, *, port: int, force: bool) -> None:
    """Write a .vscode/tasks.json task that opens the viewer as an editor tab.

    The compound task starts the loopback viewer as a background process, then
    runs the built-in ``simpleBrowser.show`` command against it — which opens
    the page as a VS Code editor tab (webview), not an external browser.
    """

    url = f"http://127.0.0.1:{port}/"
    server_task = {
        "label": "Handoff: Serve Viewer",
        "type": "shell",
        "command": "handoff-mcp",
        "args": ["gui", "--open", "none", "--port", str(port)],
        "isBackground": True,
        # Pin this repo's scope so the viewer shows only this project's items,
        # regardless of the directory the task launches from. This mirrors the
        # HANDOFF_MCP_PROJECT_ROOT that .mcp.json sets for the serve process.
        "options": {"env": {"HANDOFF_MCP_PROJECT_ROOT": str(root)}},
        "problemMatcher": [
            {
                "pattern": [{"regexp": ".", "file": 1, "location": 2, "message": 3}],
                "background": {
                    "activeOnStart": True,
                    "beginsPattern": "Handoff viewer",
                    "endsPattern": "Ctrl\\+C to stop",
                },
            }
        ],
        "presentation": {"reveal": "silent", "panel": "dedicated"},
    }
    open_task = {
        "label": "Handoff: Open Viewer",
        "dependsOn": ["Handoff: Serve Viewer"],
        "type": "shell",
        # runCommands-style single command: open the URL in the Simple Browser tab.
        "command": "${input:openHandoffViewer}",
        "problemMatcher": [],
    }
    tasks_doc: dict = {
        "version": "2.0.0",
        "tasks": [server_task, open_task],
        "inputs": [
            {
                "id": "openHandoffViewer",
                "type": "command",
                "command": "simpleBrowser.show",
                "args": url,
            }
        ],
    }

    vscode_dir = root / ".vscode"
    vscode_dir.mkdir(exist_ok=True)
    tasks_path = vscode_dir / "tasks.json"

    if tasks_path.exists() and not force:
        print(f"\n{tasks_path} already exists; not overwriting (use --force).")
        print("  To add it manually, run the 'Handoff: Open Viewer' task shape from the docs.")
        return

    tasks_path.write_text(json.dumps(tasks_doc, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {tasks_path}")
    print("  Run it from the Command Palette: 'Tasks: Run Task' -> 'Handoff: Open Viewer'.")
    print("  It starts the loopback viewer and opens it as a VS Code editor tab.")


def _cmd_whoami(_args: argparse.Namespace) -> int:
    project = resolve_project()
    print(f"project_key:   {project.key}")
    print(f"project_label: {project.label}")
    print(f"project_root:  {project.root}")
    print(f"database:      {default_db_path()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoff-mcp", description=__doc__)
    parser.add_argument("--version", action="version", version=f"handoff-mcp {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Run the stdio MCP server.")
    p_serve.set_defaults(func=_cmd_serve)

    p_gui = sub.add_parser("gui", help="Open the loopback viewer.")
    p_gui.add_argument("--host", default="127.0.0.1", help="Loopback host (default 127.0.0.1).")
    p_gui.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            f"Port (default {DEFAULT_GUI_PORT}). Without --port, a busy default "
            "advances to the next free port so viewers for different repos do not "
            "collide; an explicit --port that is busy fails instead of moving."
        ),
    )
    p_gui.add_argument(
        "--open",
        choices=["auto", "vscode", "browser", "none"],
        default="auto",
        help=(
            "Where to open the viewer: auto (VS Code tab when inside VS Code, else "
            "browser), vscode (force a VS Code editor tab), browser, or none."
        ),
    )
    p_gui.set_defaults(func=_cmd_gui)

    p_init = sub.add_parser(
        "init",
        help="Write a project-local .mcp.json registering this server, scoped to this repo.",
    )
    p_init.add_argument(
        "--path", default=".", help="Project directory to initialise (default: current directory)."
    )
    p_init.add_argument(
        "--force", action="store_true", help="Overwrite an existing handoff entry in .mcp.json."
    )
    p_init.add_argument(
        "--vscode",
        action="store_true",
        help="Also write a .vscode/tasks.json task that opens the viewer in an editor tab.",
    )
    p_init.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Viewer port for the VS Code task. Default: a stable port derived "
            "from this repo's project key, so each repo's task gets a distinct "
            "port and two open viewers do not collide."
        ),
    )
    p_init.set_defaults(func=_cmd_init)

    p_list = sub.add_parser("list", help="Print open handoffs/todos for this project.")
    p_list.set_defaults(func=_cmd_list)

    p_who = sub.add_parser("whoami", help="Print the resolved project identity.")
    p_who.set_defaults(func=_cmd_whoami)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
