from __future__ import annotations

"""Local web UI for nasdiag. Pure stdlib, no extra deps."""
import http.server
import io
import json
import logging
import os
import queue
import re
import socketserver
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from . import discover
from .config import DEFAULTS

log = logging.getLogger(__name__)

_RUNS: dict[str, dict] = {}
_RUNS_LOCK = threading.Lock()
_HTML_PATH_RE = re.compile(r"html report:\s*(.+)$")


def _discover_payload() -> dict:
    shares = [{"path": m.path, "host": m.host or "", "type": m.type}
              for m in discover.list_shares()]
    externals = [{"path": m.path, "type": m.type}
                 for m in discover.list_externals()]
    host = ""
    for s in shares:
        if s["host"]:
            host = s["host"]
            break
    return {
        "shares": shares,
        "externals": externals,
        "host": host or DEFAULTS["host"],
        "default_nas_user": DEFAULTS["nas_user"] or os.environ.get("USER", ""),
    }


def _build_cmd(cfg: dict) -> list[str]:
    cmd = [sys.executable, "-m", "nasdiag", "suite",
           "--mode", cfg.get("mode") or "polite"]
    if cfg.get("host"):
        cmd += ["--host", cfg["host"]]
    if cfg.get("nas_user"):
        cmd += ["--nas-user", cfg["nas_user"]]
    if cfg.get("nas_nic"):
        cmd += ["--nas-nic", cfg["nas_nic"]]
    for s in cfg.get("shares", []):
        cmd += ["--share-path", s]
    if cfg.get("external"):
        cmd += ["--external-path", cfg["external"]]
    else:
        cmd += ["--skip-external"]
    return cmd


def _start_run(cfg: dict) -> str:
    run_id = uuid.uuid4().hex[:12]
    cmd = _build_cmd(cfg)
    log.info("starting run %s: %s", run_id, " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    q: queue.Queue = queue.Queue()
    state = {
        "proc": proc,
        "queue": q,
        "started": time.time(),
        "cmd": cmd,
        "html_path": None,
        "exit_code": None,
        "done": False,
    }
    with _RUNS_LOCK:
        _RUNS[run_id] = state

    def reader():
        assert proc.stdout
        for line in proc.stdout:
            q.put(("line", line.rstrip("\n")))
            m = _HTML_PATH_RE.search(line)
            if m:
                state["html_path"] = m.group(1).strip()
        proc.wait()
        state["exit_code"] = proc.returncode
        state["done"] = True
        q.put(("done", proc.returncode))

    threading.Thread(target=reader, daemon=True).start()
    return run_id


def _stop_run(run_id: str) -> bool:
    with _RUNS_LOCK:
        state = _RUNS.get(run_id)
    if not state or state["done"]:
        return False
    try:
        state["proc"].terminate()
    except OSError:
        return False
    return True


INDEX_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>nasdiag</title>
<style>
* { box-sizing: border-box; }
body { background: #0d0d0d; color: #e4e4e4; font-family: -apple-system, BlinkMacSystemFont, 'Inter', sans-serif; margin: 0; padding: 24px; }
.container { max-width: 900px; margin: 0 auto; }
h1 { color: #fff; margin: 0 0 4px; font-size: 22px; letter-spacing: -0.01em; }
.tagline { color: #888; font-size: 12px; margin-bottom: 24px; }
.card { background: #161616; border: 1px solid #222; border-radius: 6px; padding: 20px; margin-bottom: 16px; }
h2 { margin: 0 0 12px; font-size: 14px; color: #ccc; font-weight: 500; }
label { display: block; font-size: 12px; color: #888; margin: 12px 0 4px; }
input[type=text], select { background: #0d0d0d; border: 1px solid #333; color: #e4e4e4; padding: 8px 10px; border-radius: 4px; font-size: 13px; width: 100%; font-family: inherit; }
.row { display: flex; gap: 12px; }
.row > * { flex: 1; }
.check { display: flex; align-items: center; gap: 8px; padding: 8px 10px; background: #0d0d0d; border: 1px solid #222; border-radius: 4px; margin: 4px 0; cursor: pointer; font-size: 13px; }
.check:hover { border-color: #444; }
.check input { margin: 0; accent-color: #E37222; }
.check .meta { color: #666; font-size: 11px; margin-left: auto; }
.modes { display: flex; gap: 8px; margin-top: 4px; }
.modes label { background: #0d0d0d; border: 1px solid #222; padding: 8px 14px; border-radius: 4px; cursor: pointer; margin: 0; color: #ccc; font-size: 12px; }
.modes label:hover { border-color: #444; }
.modes input { display: none; }
.modes input:checked + span { color: #E37222; font-weight: 600; }
button { background: #E37222; border: none; color: #fff; padding: 10px 22px; border-radius: 4px; font-size: 13px; font-weight: 600; cursor: pointer; font-family: inherit; }
button:hover { background: #f08332; }
button:disabled { background: #333; color: #888; cursor: not-allowed; }
button.stop { background: #b03030; }
button.stop:hover { background: #c03838; }
.log { background: #0a0a0a; border: 1px solid #222; border-radius: 4px; padding: 14px; font-family: 'SF Mono', Menlo, monospace; font-size: 12px; line-height: 1.45; max-height: 420px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; color: #b8b8b8; }
.log .new { color: #E37222; }
.status { font-size: 12px; color: #888; margin: 8px 0; }
.status .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; background: #888; vertical-align: middle; }
.status.running .dot { background: #E37222; animation: pulse 1.4s infinite; }
.status.done .dot { background: #6cc167; }
.status.error .dot { background: #b03030; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
iframe { width: 100%; height: 720px; border: 1px solid #222; border-radius: 4px; margin-top: 12px; background: #0d0d0d; }
.hidden { display: none; }
a { color: #E37222; }
</style></head>
<body>
<div class="container">
  <h1>nasdiag</h1>
  <div class="tagline">bisection diagnostics for shared-NAS video editing</div>

  <div class="card" id="config-card">
    <h2>1 — connection</h2>
    <div class="row">
      <div>
        <label>NAS host / IP</label>
        <input id="host" type="text" placeholder="auto-detected from share"/>
      </div>
      <div>
        <label>NAS SSH user (for telemetry)</label>
        <input id="nas_user" type="text" placeholder="brandy"/>
      </div>
    </div>

    <h2 style="margin-top:24px">2 — NAS volumes to test</h2>
    <div id="shares"></div>

    <h2 style="margin-top:24px">3 — external SSD <span style="color:#666;font-weight:400">(Resolve cache, optional)</span></h2>
    <div id="externals"></div>

    <h2 style="margin-top:24px">4 — mode</h2>
    <div class="modes">
      <label><input type="radio" name="mode" value="polite" checked><span>polite (1 GB / 15s — safe during work)</span></label>
      <label><input type="radio" name="mode" value="full"><span>full (16 GB / 30s — real numbers, ~15 min)</span></label>
    </div>

    <div style="margin-top:24px">
      <button id="run-btn">Run suite</button>
    </div>
  </div>

  <div class="card hidden" id="run-card">
    <h2>output</h2>
    <div id="status" class="status"><span class="dot"></span><span id="status-text">starting…</span></div>
    <div id="log" class="log"></div>
    <div style="margin-top:12px">
      <button id="stop-btn" class="stop">Stop</button>
      <button id="reset-btn" class="hidden">New run</button>
    </div>
  </div>

  <div class="card hidden" id="report-card">
    <h2>report</h2>
    <iframe id="report" src=""></iframe>
  </div>
</div>

<script>
let runId = null;
let evtSrc = null;

async function loadConfig() {
  const r = await fetch('/api/discover');
  const data = await r.json();
  document.getElementById('host').value = data.host || '';
  document.getElementById('nas_user').value = data.default_nas_user || '';
  renderShares(data.shares);
  renderExternals(data.externals);
}

function renderShares(shares) {
  const root = document.getElementById('shares');
  if (!shares.length) { root.innerHTML = '<div style="color:#888;font-size:12px">no SMB/NFS shares mounted</div>'; return; }
  root.innerHTML = shares.map((s, i) => `
    <label class="check">
      <input type="checkbox" name="share" value="${s.path}" data-host="${s.host || ''}" ${i === 0 ? 'checked' : ''}/>
      <span>${s.path}</span>
      <span class="meta">${s.type}${s.host ? ' · ' + s.host : ''}</span>
    </label>
  `).join('');
  document.querySelectorAll('input[name=share]').forEach(cb => {
    cb.addEventListener('change', syncHostFromShares);
  });
  syncHostFromShares();
}

function syncHostFromShares() {
  const first = document.querySelector('input[name=share]:checked');
  if (first && first.dataset.host) {
    document.getElementById('host').value = first.dataset.host;
  }
}

function renderExternals(exts) {
  const root = document.getElementById('externals');
  let html = `<label class="check"><input type="radio" name="external" value="" checked/><span>skip — no external SSD</span></label>`;
  html += exts.map(e => `
    <label class="check">
      <input type="radio" name="external" value="${e.path}"/>
      <span>${e.path}</span>
      <span class="meta">${e.type}</span>
    </label>
  `).join('');
  root.innerHTML = html;
}

function collectConfig() {
  const shares = [...document.querySelectorAll('input[name=share]:checked')].map(i => i.value);
  const external = document.querySelector('input[name=external]:checked').value;
  const mode = document.querySelector('input[name=mode]:checked').value;
  return {
    host: document.getElementById('host').value.trim(),
    nas_user: document.getElementById('nas_user').value.trim(),
    shares, external, mode,
  };
}

document.getElementById('run-btn').onclick = async () => {
  const cfg = collectConfig();
  if (!cfg.shares.length) { alert('Pick at least one NAS volume.'); return; }
  document.getElementById('config-card').classList.add('hidden');
  document.getElementById('run-card').classList.remove('hidden');
  document.getElementById('log').textContent = '';
  setStatus('running', 'running suite…');
  const r = await fetch('/api/run', { method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(cfg) });
  const data = await r.json();
  runId = data.run_id;
  streamEvents();
};

function streamEvents() {
  evtSrc = new EventSource('/api/events/' + runId);
  const logEl = document.getElementById('log');
  evtSrc.onmessage = (e) => {
    const line = document.createElement('div');
    line.textContent = e.data;
    logEl.appendChild(line);
    logEl.scrollTop = logEl.scrollHeight;
  };
  evtSrc.addEventListener('done', (e) => {
    evtSrc.close();
    const code = parseInt(e.data, 10);
    if (code === 0) { setStatus('done', 'finished'); loadReport(); }
    else { setStatus('error', 'failed (exit ' + code + ') — see log above'); }
    document.getElementById('stop-btn').classList.add('hidden');
    document.getElementById('reset-btn').classList.remove('hidden');
  });
}

function setStatus(cls, text) {
  const s = document.getElementById('status');
  s.className = 'status ' + cls;
  document.getElementById('status-text').textContent = text;
}

document.getElementById('stop-btn').onclick = async () => {
  await fetch('/api/stop/' + runId, { method: 'POST' });
};

document.getElementById('reset-btn').onclick = () => {
  document.getElementById('config-card').classList.remove('hidden');
  document.getElementById('run-card').classList.add('hidden');
  document.getElementById('report-card').classList.add('hidden');
  document.getElementById('stop-btn').classList.remove('hidden');
  document.getElementById('reset-btn').classList.add('hidden');
};

async function loadReport() {
  const r = await fetch('/api/report/' + runId);
  if (!r.ok) return;
  const card = document.getElementById('report-card');
  card.classList.remove('hidden');
  document.getElementById('report').src = '/api/report/' + runId + '?raw=1';
}

loadConfig();
</script>
</body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug("http: " + fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str = "text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/discover":
            self._send_json(200, _discover_payload())
        elif path.startswith("/api/events/"):
            self._stream_events(path.rsplit("/", 1)[-1])
        elif path.startswith("/api/report/"):
            self._serve_report(path.rsplit("/", 1)[-1])
        else:
            self._send(404, b"not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/run":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            cfg = json.loads(body) if body else {}
            run_id = _start_run(cfg)
            self._send_json(200, {"run_id": run_id})
        elif path.startswith("/api/stop/"):
            run_id = path.rsplit("/", 1)[-1]
            ok = _stop_run(run_id)
            self._send_json(200, {"stopped": ok})
        else:
            self._send(404, b"not found")

    def _stream_events(self, run_id: str):
        with _RUNS_LOCK:
            state = _RUNS.get(run_id)
        if not state:
            self._send(404, b"unknown run")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q: queue.Queue = state["queue"]
        try:
            while True:
                kind, payload = q.get()
                if kind == "line":
                    msg = f"data: {payload}\n\n"
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                elif kind == "done":
                    msg = f"event: done\ndata: {payload}\n\n"
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_report(self, run_id: str):
        with _RUNS_LOCK:
            state = _RUNS.get(run_id)
        if not state or not state.get("html_path"):
            self._send(404, b"report not ready")
            return
        try:
            data = Path(state["html_path"]).read_bytes()
        except OSError as e:
            self._send(500, str(e).encode("utf-8"))
            return
        self._send(200, data, "text/html; charset=utf-8")


class _ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True):
    server = _ThreadedServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"\nnasdiag web UI: {url}")
    print(f"  ctrl-c to stop\n")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
        server.server_close()
