"""Tests for storage isolation, tool handlers, and the stdio protocol."""

from __future__ import annotations

import json
import os

import pytest

from handoff_mcp.project import resolve_project
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
