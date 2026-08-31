"""Minimal loopback-only viewer for outstanding handoffs and todos.

This is the ONLY HTTP surface in the project, and it is deliberately tiny: a
``http.server`` bound to ``127.0.0.1`` that serves a single self-contained HTML
page plus a couple of read-mostly JSON endpoints. It exists so a human can
glance at outstanding handoffs / todos and copy them into a worker session. It
binds loopback only and is not an MCP transport.

Endpoints
    ``GET  /``                 the HTML page
    ``GET  /api/data``         open handoffs + todos for the active project
    ``POST /api/todo/done``    mark a todo done (body: {"seq": N})
    ``POST /api/handoff/resolve``  mark a handoff resolved (body: {"seq": N})
"""

from __future__ import annotations

import json
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .project import Project, resolve_project
from .storage import Store

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Handoff / TODO viewer</title>
<style>
  :root { color-scheme: light dark; --bg:#faf9f7; --fg:#1c1c1c; --muted:#666;
          --card:#fff; --line:#e3e0da; --accent:#2f6f4f; --chip:#eef1ef; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#16171a; --fg:#e9e9ea; --muted:#9a9a9d; --card:#202226;
            --line:#33353a; --accent:#68b98c; --chip:#2a2d31; }
  }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--fg); }
  header { padding:16px 20px; border-bottom:1px solid var(--line);
           display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }
  header h1 { font-size:16px; margin:0; }
  header .proj { color:var(--muted); font-size:12px; }
  header .spacer { flex:1; }
  button { font:inherit; cursor:pointer; border:1px solid var(--line);
           background:var(--card); color:var(--fg); border-radius:7px; padding:4px 10px; }
  button:hover { border-color:var(--accent); }
  main { max-width:900px; margin:0 auto; padding:20px; }
  section { margin-bottom:28px; }
  section h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em;
               color:var(--muted); border-bottom:1px solid var(--line); padding-bottom:6px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:12px 14px; margin:10px 0; }
  .card .top { display:flex; gap:10px; align-items:baseline; }
  .id { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px;
        color:var(--accent); font-weight:600; }
  .title { font-weight:600; }
  .meta { color:var(--muted); font-size:12px; margin-left:auto; }
  .body { margin-top:6px; white-space:pre-wrap; }
  .chip { display:inline-block; background:var(--chip); border-radius:20px;
          padding:1px 9px; font-size:11px; color:var(--muted); margin-right:4px; }
  .actions { margin-top:10px; display:flex; gap:8px; }
  .empty { color:var(--muted); font-style:italic; padding:8px 0; }
  .toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%);
           background:var(--accent); color:#fff; padding:8px 16px; border-radius:20px;
           opacity:0; transition:opacity .2s; pointer-events:none; }
  .toast.show { opacity:1; }
</style>
</head>
<body>
<header>
  <h1>Handoff / TODO</h1>
  <span class="proj" id="proj"></span>
  <span class="spacer"></span>
  <button onclick="load()">Refresh</button>
</header>
<main>
  <section>
    <h2>Open handoffs</h2>
    <div id="handoffs"></div>
  </section>
  <section>
    <h2>Open todos</h2>
    <div id="todos"></div>
  </section>
</main>
<div class="toast" id="toast"></div>
<script>
const esc = s => (s||"").replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function toast(msg){ const t=document.getElementById('toast'); t.textContent=msg;
  t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),1400); }
function execCopy(text){
  // Fallback for contexts without the async Clipboard API (e.g. the VS Code
  // Simple Browser webview, where navigator.clipboard is unavailable).
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.top = '-1000px';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand('copy'); } catch(e) { ok = false; }
  document.body.removeChild(ta);
  return ok;
}
async function copy(text){
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); toast('Copied'); return; }
    catch(e) { /* fall through to legacy path */ }
  }
  toast(execCopy(text) ? 'Copied' : 'Copy failed');
}
async function post(url, body){ await fetch(url,{method:'POST',
  headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); load(); }

function handoffText(h){
  let out = h.id + ': ' + h.summary + '\\n';
  if(h.next_steps) out += '\\nNext steps:\\n' + h.next_steps + '\\n';
  if(h.context)    out += '\\nContext:\\n' + h.context + '\\n';
  if(h.references) out += '\\nReferences:\\n' + h.references + '\\n';
  return out.trim();
}
function todoText(t){
  let out = t.id + ' [P' + t.priority + ']: ' + t.title;
  if(t.detail) out += '\\n' + t.detail;
  return out;
}

function renderHandoff(h){
  const meta = [h.author, h.created_at].filter(Boolean).join(' · ');
  return `<div class="card">
    <div class="top"><span class="id">${esc(h.id)}</span>
      <span class="title">${esc(h.summary)}</span>
      <span class="meta">${esc(meta)}</span></div>
    ${h.next_steps ? `<div class="body"><b>Next:</b> ${esc(h.next_steps)}</div>`:''}
    ${h.context ? `<div class="body"><b>Context:</b> ${esc(h.context)}</div>`:''}
    ${h.references ? `<div class="body"><b>Refs:</b> ${esc(h.references)}</div>`:''}
    <div class="actions">
      <button onclick='copy(handoffText(${JSON.stringify(h)}))'>Copy</button>
      <button onclick='post("/api/handoff/resolve",{seq:${h.seq}})'>Resolve</button>
    </div></div>`;
}
function renderTodo(t){
  const tags = (t.tags||[]).map(x=>`<span class="chip">${esc(x)}</span>`).join('');
  return `<div class="card">
    <div class="top"><span class="id">${esc(t.id)}</span>
      <span class="title">${esc(t.title)}</span>
      <span class="meta">P${t.priority}</span></div>
    ${t.detail ? `<div class="body">${esc(t.detail)}</div>`:''}
    ${tags ? `<div style="margin-top:6px">${tags}</div>`:''}
    <div class="actions">
      <button onclick='copy(todoText(${JSON.stringify(t)}))'>Copy</button>
      <button onclick='post("/api/todo/done",{seq:${t.seq}})'>Done</button>
    </div></div>`;
}

async function load(){
  const r = await fetch('/api/data'); const d = await r.json();
  document.getElementById('proj').textContent =
    d.project.project_label + '  (' + d.project.project_key + ')  —  ' + d.project.project_root;
  document.getElementById('handoffs').innerHTML =
    d.handoffs.length ? d.handoffs.map(renderHandoff).join('') : '<div class="empty">None open.</div>';
  document.getElementById('todos').innerHTML =
    d.todos.length ? d.todos.map(renderTodo).join('') : '<div class="empty">None open.</div>';
}
load();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    project: Project
    store: Store

    def log_message(self, *args: Any) -> None:  # silence per-request logging
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return {}

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/data":
            self._json(
                {
                    "project": self.project.designation(),
                    "handoffs": self.store.list_handoffs(status="open", limit=200),
                    "todos": self.store.list_todos(status="open", limit=200),
                }
            )
        else:
            self._json({"error": "not found"}, code=404)

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_body()
        seq = body.get("seq")
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            self._json({"error": "seq required"}, code=400)
            return
        try:
            if self.path == "/api/todo/done":
                self._json({"todo": self.store.update_todo(seq, status="done")})
            elif self.path == "/api/handoff/resolve":
                self._json({"handoff": self.store.update_handoff(seq, status="resolved")})
            else:
                self._json({"error": "not found"}, code=404)
        except KeyError as exc:
            self._json({"error": str(exc)}, code=404)


def _running_in_vscode() -> bool:
    return bool(os.environ.get("VSCODE_PID") or os.environ.get("TERM_PROGRAM") == "vscode")


def run_gui(
    host: str = "127.0.0.1",
    port: int = 8765,
    open_target: str = "auto",
) -> None:
    """Serve the loopback-only viewer for the current project.

    ``open_target`` is one of:

    * ``auto``    — inside VS Code, print a Ctrl-clickable URL and skip the
                    external browser; otherwise open the default browser.
    * ``vscode``  — same terminal handoff as auto, without the browser fallback.
    * ``browser`` — force the default external browser.
    * ``none``    — serve only; open nothing.

    Opening the viewer as a *VS Code editor tab* (Simple Browser) cannot be
    triggered from an external process — the ``code`` CLI has no command for it.
    Use the ``Handoff: Open Viewer`` task written by ``handoff-mcp init
    --vscode`` (or Ctrl+Click the printed URL) to get an in-editor tab.
    """

    project = resolve_project()
    store = Store(project.key)

    handler = type("BoundHandler", (_Handler,), {"project": project, "store": store})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"Handoff viewer: {url}")
    print(f"  project: {project.label} ({project.key})")
    print(f"  root:    {project.root}")
    print("  Ctrl+C to stop.")

    _open_viewer(url, open_target)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping viewer.")
    finally:
        httpd.server_close()


def _open_viewer(url: str, open_target: str) -> None:
    if open_target == "none":
        return
    if open_target == "vscode":
        print(f"  Ctrl+Click to open in an editor tab (Simple Browser): {url}")
        return
    if open_target == "browser":
        _open_external(url)
        return
    # auto: inside VS Code, hand off to the terminal link rather than spawning
    # an external browser window; otherwise open the browser.
    if _running_in_vscode():
        print(f"  Ctrl+Click to open in an editor tab (Simple Browser): {url}")
        return
    _open_external(url)


def _open_external(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


__all__ = ["run_gui"]
