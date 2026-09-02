# handoff-mcp

A minimal, project-scoped **TODO / Handoff** MCP server. It gives agent sessions a durable place to leave breadcrumbs ("here is where I stopped, here is what to do next") and track next-step todos, then reload them in a later session instead of re-deriving context.

Design goals:

- **Standard library only.** No third-party runtime dependencies. Persistence is a single SQLite file.
- **No network / HTTP for the MCP transport.** The server speaks the standard MCP **stdio** protocol (line-delimited JSON-RPC). The only HTTP surface is an optional, loopback-only viewer you launch by hand.
- **Strict project isolation.** One installation and one database serve many repositories; every record is tagged with a `project_key` derived from the Git repository root. A repository can never see another's handoffs or todos.
- **Context hygiene built in.** Tools that hand the agent a concrete procedure for noticing and shrinking a bloated context window.

## Install

```bash
cd c:\MyRepos\Python\Handoff_MCP
pip install -e .
```

This installs the `handoff-mcp` command. (Requires Python 3.9+.)

## Setup model — who runs what

You never run `handoff-mcp serve` by hand. The MCP **client** (your agent's harness) spawns it, once per session, and it exits when the session ends.

- **Once per machine:** `pip install -e .`.
- **Once per repository:** run `handoff-mcp init` from the repo root. This writes a project-local `.mcp.json` that registers the server *and pins this repo's scope*, so the project key is correct no matter which directory the client launches from. Commit the file.
- **Per session:** nothing. The client reads `.mcp.json`, starts `handoff-mcp serve`, and the tools are available. Agents call the tools; they do not start the server.

```bash
cd /path/to/your/repo
handoff-mcp init            # writes ./.mcp.json (scoped to this repo)
handoff-mcp init --vscode   # also writes a .vscode task to open the viewer in an editor tab
```

The generated `.mcp.json`:

```json
{
  "mcpServers": {
    "handoff": {
      "command": "handoff-mcp",
      "args": ["serve"],
      "env": { "HANDOFF_MCP_PROJECT_ROOT": "/path/to/your/repo" }
    }
  }
}
```

If your client does not read project-local `.mcp.json`, add the same entry to that client's own MCP config. Without the pinned `HANDOFF_MCP_PROJECT_ROOT`, scoping falls back to Git detection from the launch directory — correct in most setups, but `init` removes the guesswork.

## Client configuration reference

Two supported clients, each with a global (user) config and a per-repository config. The rule of thumb:

- **Define the server** either globally (once, reused everywhere) or per-repo (committed with the code). Do not do both for the same repo, or the client sees two servers.
- **Scope it** by setting `HANDOFF_MCP_PROJECT_ROOT` in the server's `env`. Per-repo configs set it to that repo; a global config either omits it (falls back to Git detection from the launch cwd) or uses a variable like `${PWD}`.

`handoff-mcp init` writes the Claude Code / generic `.mcp.json` for you; the blocks below show every file so you can wire up either client by hand. Replace `/absolute/path/to/repo` with the real path (forward slashes work on Windows too).

### Which file holds what

| Client | Server definitions | Permissions / approval | Env / scope |
| --- | --- | --- | --- |
| **Claude Code** | `~/.claude.json` (global) or `.mcp.json` (repo) | `.claude/settings.json` | `env` on the server entry, or `.claude/settings.json` → `env` |
| **Codex CLI** | `~/.codex/config.toml` (global) or `.codex/config.toml` (repo) | trust prompt (no per-tool gate) | `[mcp_servers.handoff.env]` table |

Note: `.claude/settings.json` does **not** hold server definitions — only permissions, hooks, and env. Codex `config.toml` has no per-tool allowlist; it gates by trusting the project.

### Claude Code

**Global server** — `~/.claude.json` (available in every repo; scope resolved from the launch directory unless you pin a root):

```json
{
  "mcpServers": {
    "handoff": {
      "type": "stdio",
      "command": "handoff-mcp",
      "args": ["serve"]
    }
  }
}
```

**Per-repository server** — `.mcp.json` at the repo root (committed; this is what `handoff-mcp init` writes):

```json
{
  "mcpServers": {
    "handoff": {
      "type": "stdio",
      "command": "handoff-mcp",
      "args": ["serve"],
      "env": { "HANDOFF_MCP_PROJECT_ROOT": "/absolute/path/to/repo" }
    }
  }
}
```

**Per-repository permissions** — `.claude/settings.json` (committed). Approves the project `.mcp.json` server and auto-allows its tools so agents are not prompted on every call:

```json
{
  "enableAllProjectMcpServers": true,
  "enabledMcpjsonServers": ["handoff"],
  "permissions": {
    "allow": ["mcp__handoff__*"]
  }
}
```

- `enableAllProjectMcpServers: true` approves every server in `.mcp.json` without a prompt; `enabledMcpjsonServers: ["handoff"]` approves just this one — use one or the other.
- `mcp__handoff__*` is the tool-permission glob: `mcp__<server-name>__<tool>`. The server name here is `handoff`, so all its tools match.
- You can also set `"env": { "HANDOFF_MCP_PROJECT_ROOT": "/absolute/path/to/repo" }` here instead of on the server entry; settings env takes literal values only.

### Codex CLI

Codex uses TOML. Servers live under `[mcp_servers.<name>]` with an `[mcp_servers.<name>.env]` sub-table for environment variables.

**Global server** — `~/.codex/config.toml`:

```toml
[mcp_servers.handoff]
type = "stdio"
command = "handoff-mcp"
args = ["serve"]

# Optional: pin a fixed repo. Omit the env table to fall back to Git detection.
[mcp_servers.handoff.env]
HANDOFF_MCP_PROJECT_ROOT = "/absolute/path/to/repo"
```

**Per-repository server** — `.codex/config.toml` at the repo root. Codex loads project config only when the project is **trusted** (a one-time trust prompt / `trust_level`), and merges nearest-file-wins from repo root down to your cwd:

```toml
[mcp_servers.handoff]
type = "stdio"
command = "handoff-mcp"
args = ["serve"]
trust_level = "trusted"

[mcp_servers.handoff.env]
HANDOFF_MCP_PROJECT_ROOT = "${PWD}"
```

`${PWD}` scopes to wherever Codex is launched; replace it with the absolute repo path if you prefer a fixed root. Codex `env` values support `${VAR}` expansion but not `${VAR:-default}` defaults.

> The project-scoped `.codex/config.toml`, the `trust_level` key, and `${PWD}` expansion come from Codex community documentation rather than the core reference — verify against your installed Codex version. The global `~/.codex/config.toml` form is the officially documented path; when in doubt, define the server globally and rely on Git detection or a fixed `HANDOFF_MCP_PROJECT_ROOT`.

### Sharing one store across a repo and its worktrees

Because the key derives from the Git top-level, a repo and a linked worktree get different keys by default. To share handoffs, set the **same** `HANDOFF_MCP_PROJECT_KEY` in each checkout's config `env` (in addition to, or instead of, `HANDOFF_MCP_PROJECT_ROOT`).

## The loopback viewer

To glance at outstanding handoffs / todos and copy them into a worker session:

```bash
handoff-mcp gui                   # auto: VS Code hands off to an editor tab; else browser
handoff-mcp gui --open browser    # force the external default browser
handoff-mcp gui --open vscode     # print a Ctrl-clickable URL for an editor tab
handoff-mcp gui --open none       # serve only; open nothing
handoff-mcp gui --port 9000       # different port
```

The viewer binds `127.0.0.1` only, with a **Copy** button per item (paste-ready text for an agent) and buttons to mark items done / resolved. There is no auth and no remote access by design — it is a local convenience window, not a service.

**Running one viewer per repo.** Each `handoff-mcp gui` binds its own port and shows one project. Ports never overlap:

- Without `--port`, a busy default port (8765) automatically advances to the next free port, and the chosen port is printed. So you can open a viewer in several repos at once and each gets a distinct port — no collision, no wrong-project page.
- With an explicit `--port`, a busy port fails with a clear message instead of moving (so the port keeps matching any URL wired to it, e.g. a VS Code task). Pick another port and retry.

The viewer never shares a port with an already-running viewer: `SO_REUSEADDR` shadow-binding (which on Windows would let a second viewer silently attach to a port another repo's viewer already holds, then serve that other repo's items) is disabled.

**Scope.** The viewer shows one project's items. It resolves that project the same way the server does, and in the same order:

1. `HANDOFF_MCP_PROJECT_ROOT` / `HANDOFF_MCP_PROJECT_KEY` in the process environment, if set.
2. Otherwise, those same keys read from the `handoff` server's `env` block in the repo's `.mcp.json` (what `handoff-mcp init` writes). Because the MCP client applies that `env` only to the `serve` process, a hand-launched `handoff-mcp gui` would not otherwise inherit it — reading it back from `.mcp.json` makes the viewer scope to the repo you configured rather than to whatever Git root the launch directory happens to sit in. The file is found at the Git top-level or by walking up from the launch directory.
3. Otherwise, Git detection of the launch directory.

So once a repo's `.mcp.json` pins its scope, `handoff-mcp gui` shows only that repo's handoffs and todos regardless of where you launch it from.

### Opening it inside VS Code (editor tab, not a separate window)

VS Code shows local web pages in an editor tab via its built-in **Simple Browser**. An external process cannot trigger that (the `code` CLI has no command for it), so there are two supported ways to get a tab:

1. **Ctrl+Click the URL.** Run `handoff-mcp gui` in the VS Code integrated terminal; when inside VS Code the command prints a Ctrl-clickable URL instead of launching an external browser, and VS Code opens it as a Simple Browser tab.
2. **Run the generated task.** `handoff-mcp init --vscode` writes `.vscode/tasks.json` with a **Handoff: Open Viewer** task. From the Command Palette choose *Tasks: Run Task → Handoff: Open Viewer*; it starts the loopback server and opens the viewer as an editor tab in one step. The task pins this repo's `HANDOFF_MCP_PROJECT_ROOT` and uses a port derived from the repo's project key (override with `--port`), so two repos' tasks get distinct ports and never show each other's items. Commit `.vscode/tasks.json` to share the setup.

## CLI

```bash
handoff-mcp init      # write project-local .mcp.json scoped to this repo (add --vscode for a viewer task)
handoff-mcp serve     # run the stdio MCP server (normally launched by the client, not you)
handoff-mcp gui       # open the loopback viewer
handoff-mcp list      # print open handoffs/todos for this project to the terminal
handoff-mcp whoami    # show the resolved project_key / root / database path
```

## Tools

| Tool | Purpose |
| --- | --- |
| `todo_add` | Add a next-step TODO. |
| `todo_list` | List todos (default: open), ordered by priority. |
| `todo_update` | Mark done/dropped, re-prioritise, or edit a TODO. |
| `handoff_add` | Record a session handoff (summary + next steps + context). |
| `handoff_list` | List handoffs (call at session start to reload breadcrumbs). |
| `handoff_resolve` | Mark a handoff resolved. |
| `project_status` | Counts of open todos and handoffs. |
| `context_report` | Checklist for judging whether context is bloated. |
| `context_compact` | Procedure to reduce session context; can persist a handoff. |

See [docs/TOOL_GUIDE.md](docs/TOOL_GUIDE.md) for detailed usage, argument reference, recommended workflow, and the context-reduction guidance.

## Project isolation

Every record carries a `project_key`. Resolution, per working directory:

1. `HANDOFF_MCP_PROJECT_ROOT` / `HANDOFF_MCP_PROJECT_KEY` from the process environment (what the client applies to `serve` from `.mcp.json`).
2. The same keys read back from the repo's `.mcp.json` `handoff` server `env`, when absent from the environment — this is what lets a hand-launched `handoff-mcp gui` scope to the configured repo rather than the launch directory.
3. **Git top-level** (`git rev-parse --show-toplevel`) if inside a work tree — stable across branches and subdirectories.
4. Otherwise the resolved working-directory path.

The root path is case-folded (so `C:\Repo` and `c:\repo` match) and hashed to a short opaque key.

**Recommended:** run `handoff-mcp init` per repo so the correct root is pinned in `.mcp.json` and detection never depends on the launch directory. The environment overrides it uses are also available directly:

- `HANDOFF_MCP_PROJECT_ROOT` — pin the root directory explicitly (what `init` sets; use when the server's cwd differs from the repo).
- `HANDOFF_MCP_PROJECT_KEY` — set the key outright; give two checkouts (e.g. a repo and a linked worktree) the same value to share state deliberately.
- `HANDOFF_MCP_DB` — override the database path (default `~/.handoff-mcp/handoff.db`).

## Data

A single SQLite database at `~/.handoff-mcp/handoff.db` (WAL mode). Tables: `todos`, `handoffs`, `project_counters`. Ids are short and per-project (`T-3`, `H-7`) so they are easy to quote and paste.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT.
