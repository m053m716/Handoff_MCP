"""Tests for storage isolation, tool handlers, and the stdio protocol."""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from handoff_mcp.project import (
    PROJECT_KEY_ENV_VAR,
    PROJECT_ROOT_ENV_VAR,
    _key_for,
    resolve_project,
)
from handoff_mcp.gui import PAGE
from handoff_mcp.server import HandoffServer
from handoff_mcp.storage import Store
from handoff_mcp.tools import ToolError, call_tool


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "handoff.db"
    monkeypatch.setenv("HANDOFF_MCP_DB", str(path))
    return path


def test_project_isolation(db):
    a = Store("projaaaaaaaaaaaa")
    b = Store("projbbbbbbbbbbbb")

    a.add_todo(title="only in A")
    b.add_todo(title="only in B")

    a_titles = [t["title"] for t in a.list_todos()]
    b_titles = [t["title"] for t in b.list_todos()]

    assert a_titles == ["only in A"]
    assert b_titles == ["only in B"]


def test_seq_is_per_project(db):
    a = Store("projaaaaaaaaaaaa")
    b = Store("projbbbbbbbbbbbb")
    assert a.add_todo(title="x")["id"] == "T-1"
    assert b.add_todo(title="y")["id"] == "T-1"  # independent counters
    assert a.add_todo(title="z")["id"] == "T-2"


def test_todo_lifecycle(db):
    store = Store("proj000000000000")
    created = call_tool(store, "todo_add", {"title": "do a thing", "priority": 1})["todo"]
    assert created["status"] == "open"
    listed = call_tool(store, "todo_list", {})["todos"]
    assert len(listed) == 1
    call_tool(store, "todo_update", {"id": created["id"], "status": "done"})
    assert call_tool(store, "todo_list", {})["todos"] == []
    assert len(call_tool(store, "todo_list", {"status": "all"})["todos"]) == 1


def test_handoff_lifecycle(db):
    store = Store("proj111111111111")
    h = call_tool(store, "handoff_add", {"summary": "left off here", "next_steps": "do X"})["handoff"]
    assert h["id"] == "H-1"
    assert len(call_tool(store, "handoff_list", {})["handoffs"]) == 1
    call_tool(store, "handoff_resolve", {"id": "H-1"})
    assert call_tool(store, "handoff_list", {})["handoffs"] == []


def test_context_compact_can_persist_handoff(db):
    store = Store("proj222222222222")
    result = call_tool(store, "context_compact", {"summary": "compacted state"})
    assert result["saved_handoff"]["id"] == "H-1"
    assert "procedure" in result


def test_bad_arguments_raise_toolerror(db):
    store = Store("proj333333333333")
    with pytest.raises(ToolError):
        call_tool(store, "todo_add", {})  # missing title
    with pytest.raises(ToolError):
        call_tool(store, "todo_update", {"id": "T-999", "status": "done"})  # unknown id


def test_stdio_protocol_roundtrip(db):
    project = resolve_project()
    server = HandoffServer(project=project, store=Store("proj444444444444"))

    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "handoff-mcp"

    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"todo_add", "handoff_add", "context_compact"} <= names

    call = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "todo_add", "arguments": {"title": "via rpc"}},
        }
    )
    assert call["result"]["isError"] is False
    assert call["result"]["structuredContent"]["todo"]["title"] == "via rpc"

    # Notifications (no id) produce no response.
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tool_error_is_reported_not_raised(db):
    server = HandoffServer(store=Store("proj555555555555"))
    call = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "todo_add", "arguments": {}},
        }
    )
    assert call["result"]["isError"] is True


@pytest.fixture()
def clean_scope_env(monkeypatch):
    """Ensure no ambient scope env leaks into project resolution under test."""
    monkeypatch.delenv(PROJECT_ROOT_ENV_VAR, raising=False)
    monkeypatch.delenv(PROJECT_KEY_ENV_VAR, raising=False)


def _write_mcp_config(directory: Path, env: dict) -> None:
    (directory / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"handoff": {"command": "handoff-mcp", "env": env}}}),
        encoding="utf-8",
    )


def test_gui_scope_reads_project_root_from_mcp_config(tmp_path, clean_scope_env):
    # A repo (non-git tmp dir) whose committed .mcp.json pins its own root.
    repo = tmp_path / "my_repo"
    repo.mkdir()
    _write_mcp_config(repo, {PROJECT_ROOT_ENV_VAR: str(repo)})

    # Resolving from the repo root, and from a nested subdirectory, must both
    # scope to the repo the .mcp.json names via the walked-up config search.
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)

    for start in (repo, nested):
        resolved = resolve_project(start)
        assert resolved.key == _key_for(repo.resolve())
        assert resolved.label == "my_repo"


def test_gui_scope_reads_project_key_from_mcp_config(tmp_path, clean_scope_env):
    repo = tmp_path / "shared_repo"
    repo.mkdir()
    _write_mcp_config(repo, {PROJECT_KEY_ENV_VAR: "sharedkey0000000"})

    resolved = resolve_project(repo)
    assert resolved.key == "sharedkey0000000"


def test_ambient_env_overrides_mcp_config(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_mcp_config(repo, {PROJECT_KEY_ENV_VAR: "fromconfig000000"})

    monkeypatch.setenv(PROJECT_KEY_ENV_VAR, "fromenv000000000")
    monkeypatch.delenv(PROJECT_ROOT_ENV_VAR, raising=False)

    resolved = resolve_project(repo)
    assert resolved.key == "fromenv000000000"


def test_no_mcp_config_falls_back_to_directory(tmp_path, clean_scope_env):
    repo = tmp_path / "plain"
    repo.mkdir()
    resolved = resolve_project(repo)
    assert resolved.key == _key_for(repo.resolve())


def test_viewer_server_does_not_reuse_address():
    # On Windows SO_REUSEADDR lets a second socket shadow a listening one; the
    # viewer must keep this off so a second gui fails/scans instead of showing
    # another repo's items on the same port.
    from handoff_mcp.gui import _ViewerServer

    assert _ViewerServer.allow_reuse_address is False


def test_bind_scans_forward_when_default_port_busy():
    from handoff_mcp.gui import _Handler, _bind_server

    handler = type("H", (_Handler,), {"project": None, "store": None})
    first = _bind_server("127.0.0.1", 0, handler, port_explicit=False)
    busy = first.server_address[1]
    try:
        # A non-explicit bind starting at the busy port must land on a different,
        # free port rather than shadowing the first server.
        second = _bind_server("127.0.0.1", busy, handler, port_explicit=False)
        try:
            assert second.server_address[1] != busy
        finally:
            second.server_close()
    finally:
        first.server_close()


def test_explicit_busy_port_raises_port_in_use():
    from handoff_mcp.gui import _Handler, PortInUse, _bind_server

    handler = type("H", (_Handler,), {"project": None, "store": None})
    first = _bind_server("127.0.0.1", 0, handler, port_explicit=True)
    busy = first.server_address[1]
    try:
        with pytest.raises(PortInUse) as excinfo:
            _bind_server("127.0.0.1", busy, handler, port_explicit=True)
        assert excinfo.value.explicit is True
        assert excinfo.value.port == busy
    finally:
        first.server_close()


def test_derived_port_is_stable_and_distinct():
    from handoff_mcp.__main__ import _derived_port

    assert _derived_port("95906624489c32ff") == _derived_port("95906624489c32ff")
    assert _derived_port("95906624489c32ff") != _derived_port("00000000ffffffff")
    assert 8765 <= _derived_port("95906624489c32ff") < 8765 + 1000


def _read_toml(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        pytest.skip("tomllib unavailable; cannot parse TOML for assertion")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_codex_block_round_trips_as_valid_toml():
    from handoff_mcp.config import codex_server_block

    text = codex_server_block("/abs/repo", trust=True)
    try:
        import tomllib
    except ModuleNotFoundError:
        pytest.skip("tomllib unavailable")
    data = tomllib.loads(text)
    handoff = data["mcp_servers"]["handoff"]
    assert handoff["command"] == "handoff-mcp"
    assert handoff["args"] == ["serve"]
    assert handoff["trust_level"] == "trusted"
    assert handoff["env"]["HANDOFF_MCP_PROJECT_ROOT"] == "/abs/repo"


def test_codex_block_omits_env_and_trust_when_not_requested():
    from handoff_mcp.config import codex_server_block

    text = codex_server_block(None, trust=False)
    assert "trust_level" not in text
    assert "env" not in text
    assert "HANDOFF_MCP_PROJECT_ROOT" not in text


def test_codex_merge_preserves_other_tables_and_upserts_handoff():
    from handoff_mcp.config import merge_codex_config

    existing = (
        "[mcp_servers.other]\n"
        'command = "other"\n'
        "\n"
        "[mcp_servers.handoff]\n"
        'command = "stale"\n'
        "\n"
        "[mcp_servers.handoff.env]\n"
        'HANDOFF_MCP_PROJECT_ROOT = "/old"\n'
    )
    merged = merge_codex_config(existing, "/new/repo", trust=True)

    # Other server survives, and there is exactly one handoff table (no dup).
    assert "[mcp_servers.other]" in merged
    assert merged.count("[mcp_servers.handoff]") == 1
    assert "/old" not in merged
    assert "/new/repo" in merged


def test_codex_merge_empty_input_yields_only_block():
    from handoff_mcp.config import merge_codex_config

    merged = merge_codex_config("   \n\n", "/repo", trust=False)
    assert merged.startswith("[mcp_servers.handoff]")


def test_json_merge_upserts_and_preserves_other_servers():
    from handoff_mcp.config import merge_json_config

    existing = {"mcpServers": {"other": {"command": "x"}, "handoff": {"command": "stale"}}}
    merged = merge_json_config(existing, "/repo")
    assert merged["mcpServers"]["other"] == {"command": "x"}
    assert merged["mcpServers"]["handoff"]["command"] == "handoff-mcp"
    assert merged["mcpServers"]["handoff"]["env"]["HANDOFF_MCP_PROJECT_ROOT"] == "/repo"


def test_json_global_entry_omits_env():
    from handoff_mcp.config import json_server_entry

    assert "env" not in json_server_entry(None)


def test_init_vscode_writes_codex_and_mcp(tmp_path, clean_scope_env, monkeypatch, capsys):
    from handoff_mcp.__main__ import build_parser

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv(PROJECT_ROOT_ENV_VAR, str(repo))

    args = build_parser().parse_args(["init", "--path", str(repo), "--vscode"])
    assert args.func(args) == 0

    assert (repo / ".mcp.json").exists()
    codex = repo / ".codex" / "config.toml"
    assert codex.exists()
    data = _read_toml(codex)
    assert data["mcp_servers"]["handoff"]["trust_level"] == "trusted"
    assert data["mcp_servers"]["handoff"]["env"]["HANDOFF_MCP_PROJECT_ROOT"] == str(repo)
    assert (repo / ".vscode" / "tasks.json").exists()


def test_init_vscode_upserts_existing_codex_without_dup(tmp_path, clean_scope_env, monkeypatch):
    from handoff_mcp.__main__ import build_parser

    repo = tmp_path / "repo"
    (repo / ".codex").mkdir(parents=True)
    (repo / ".codex" / "config.toml").write_text(
        "[mcp_servers.handoff]\ncommand = \"stale\"\n\n[mcp_servers.handoff.env]\n"
        "HANDOFF_MCP_PROJECT_ROOT = \"/old\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(PROJECT_ROOT_ENV_VAR, str(repo))

    args = build_parser().parse_args(["init", "--path", str(repo), "--vscode", "--force"])
    assert args.func(args) == 0

    text = (repo / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert text.count("[mcp_servers.handoff]") == 1
    assert "/old" not in text


def test_guidance_upsert_creates_and_does_not_duplicate():
    from handoff_mcp.config import GUIDANCE_BEGIN, _upsert_guidance

    once = _upsert_guidance("")
    assert once.count(GUIDANCE_BEGIN) == 1
    # Re-applying replaces in place — still exactly one block.
    twice = _upsert_guidance(once)
    assert twice.count(GUIDANCE_BEGIN) == 1
    assert twice == once


def test_guidance_upsert_preserves_surrounding_content():
    from handoff_mcp.config import GUIDANCE_BEGIN, GUIDANCE_END, _upsert_guidance

    original = "# My Repo\n\nSome intro.\n"
    updated = _upsert_guidance(original)
    assert updated.startswith("# My Repo\n\nSome intro.")
    assert GUIDANCE_BEGIN in updated and GUIDANCE_END in updated

    # A second pass with edited guidance replaces only the block, keeping intro
    # and any trailing content the user added after the block.
    with_trailer = updated + "\n## After\n\nmore\n"
    again = _upsert_guidance(with_trailer)
    assert again.count(GUIDANCE_BEGIN) == 1
    assert "Some intro." in again
    assert "## After" in again


def test_write_agent_guidance_skips_missing_when_not_creating(tmp_path):
    from handoff_mcp.config import write_agent_guidance

    readme = tmp_path / "README.md"
    assert write_agent_guidance(readme, create_if_missing=False) is False
    assert not readme.exists()

    agents = tmp_path / "AGENTS.md"
    assert write_agent_guidance(agents, create_if_missing=True) is True
    assert agents.exists()


def test_init_vscode_writes_agent_guidance(tmp_path, clean_scope_env, monkeypatch):
    from handoff_mcp.__main__ import build_parser
    from handoff_mcp.config import GUIDANCE_BEGIN

    repo = tmp_path / "repo"
    repo.mkdir()
    # A pre-existing README should be appended to, not clobbered.
    (repo / "README.md").write_text("# Repo\n\nHello.\n", encoding="utf-8")
    monkeypatch.setenv(PROJECT_ROOT_ENV_VAR, str(repo))

    args = build_parser().parse_args(["init", "--path", str(repo), "--vscode"])
    assert args.func(args) == 0

    agents = (repo / "AGENTS.md").read_text(encoding="utf-8")
    readme = (repo / "README.md").read_text(encoding="utf-8")
    assert agents.count(GUIDANCE_BEGIN) == 1
    assert "handoff_list" in agents
    assert readme.startswith("# Repo\n\nHello.")
    assert GUIDANCE_BEGIN in readme

    # Idempotent: a second run does not duplicate the block in either file.
    args2 = build_parser().parse_args(["init", "--path", str(repo), "--vscode", "--force"])
    assert args2.func(args2) == 0
    assert (repo / "AGENTS.md").read_text(encoding="utf-8").count(GUIDANCE_BEGIN) == 1
    assert (repo / "README.md").read_text(encoding="utf-8").count(GUIDANCE_BEGIN) == 1


def test_install_global_writes_both_configs(tmp_path):
    from handoff_mcp.install import install_global

    written = install_global(tmp_path)
    claude = tmp_path / ".claude.json"
    codex = tmp_path / ".codex" / "config.toml"
    assert set(written) == {claude, codex}

    claude_data = json.loads(claude.read_text(encoding="utf-8"))
    assert claude_data["mcpServers"]["handoff"]["command"] == "handoff-mcp"
    # Global entry: no pinned root.
    assert "env" not in claude_data["mcpServers"]["handoff"]

    codex_data = _read_toml(codex)
    handoff = codex_data["mcp_servers"]["handoff"]
    assert handoff["command"] == "handoff-mcp"
    assert "trust_level" not in handoff  # global entry is not per-project trust


def test_install_global_upserts_and_preserves_existing(tmp_path):
    from handoff_mcp.install import install_global

    # Pre-existing unrelated content in both files.
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}, "misc": 1}), encoding="utf-8"
    )
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        "[some_other_section]\nkey = \"v\"\n", encoding="utf-8"
    )

    install_global(tmp_path, project_root="/pin")

    claude_data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert claude_data["misc"] == 1
    assert claude_data["mcpServers"]["other"] == {"command": "x"}
    assert claude_data["mcpServers"]["handoff"]["env"]["HANDOFF_MCP_PROJECT_ROOT"] == "/pin"

    codex_text = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[some_other_section]" in codex_text
    assert codex_text.count("[mcp_servers.handoff]") == 1


def test_favicon_assets_are_valid_and_linked():
    asset_dir = Path(__file__).parents[1] / "handoff_mcp" / "assets"
    png = (asset_dir / "favicon.png").read_bytes()
    ico = (asset_dir / "favicon.ico").read_bytes()

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (512, 512)
    assert struct.unpack("<HHH", ico[:6]) == (0, 1, 7)
    assert 'href="/favicon.png"' in PAGE
    assert 'href="/favicon.ico"' in PAGE
