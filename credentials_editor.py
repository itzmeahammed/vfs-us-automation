"""Local web UI to view/edit VFS accounts + routes — standalone tool.

Two collapsible panels, each saving its own file:
  * Accounts  -> config/credentials.local.ini  (add/edit/reorder accounts)
  * Countries -> config/vfs_urls.ini            (toggle which routes the bot runs)

A plain HTML page can't write to disk (browser sandbox), so this is a tiny local
web server (Python stdlib only) that serves the editor UI AND saves the files.
It binds to 127.0.0.1 ONLY — your credentials never touch the network.

Run it, then edit in the browser and click Save:

    & .venv\\Scripts\\python.exe credentials_editor.py
        -> serving at http://127.0.0.1:8765  (opens automatically)

Each Save:
  * validates every enabled account (email + password required, routes known),
  * writes a single rolling backup first (credentials.local.ini.bak, overwritten
    each save — no pile-up of timestamped files),
  * rewrites the file, renumbering cred1..N in the on-screen order (the bot
    rotates by ORDER, not by the number — labels are cosmetic).

Disabled accounts are stored as commented-out '; [credN]' blocks, exactly how
the bot already treats them (a commented section is skipped).
"""

import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
CRED_FILE = os.path.join(HERE, "config", "credentials.local.ini")
URLS_FILE = os.path.join(HERE, "config", "vfs_urls.ini")


def _rolling_backup(path):
    """Copy `path` to a SINGLE rolling '<path>.bak', overwritten on every save.

    One backup per file (the previous version) instead of a fresh timestamped
    file each save — so backups never pile up. Returns the backup path, or None
    if there was nothing to back up. Raises OSError on copy failure.
    """
    if not os.path.isfile(path):
        return None
    backup = f"{path}.bak"
    shutil.copy2(path, backup)
    return backup
PORT = 8765
TASK_NAME = "VFS Slot Checker"   # the Windows scheduled task (see setup_task.ps1)

_HEADER = [
    "; Multiple VFS accounts, rotated by clock hour.",
    "; cred1 -> 06:00, cred2 -> 07:00, ... wrapping after the last one.",
    "; This file is gitignored — real credentials only.",
    "; Managed by credentials_editor.py (order = rotation order).",
]

_SECTION_RE = re.compile(r"^\s*(;+)?\s*\[(?P<name>[^\]]+)\]\s*$")
_KEY_RE = re.compile(r"^\s*(;+)?\s*(?P<k>email|password|routes)\s*=\s*(?P<v>.*?)\s*$",
                     re.IGNORECASE)
_ROUTE_DEF_RE = re.compile(r"^\s*;?\s*(?P<code>AE-[A-Z]{2,4})\s*=", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Read                                                                         #
# --------------------------------------------------------------------------- #

def parse_credentials():
    """Read the INI into [{enabled, email, password, routes:[...]}, ...] in file
    order. Handles both enabled '[credN]' and disabled '; [credN]' blocks."""
    accounts = []
    if not os.path.isfile(CRED_FILE):
        return accounts
    cur = None
    with open(CRED_FILE, "r", encoding="utf-8") as f:
        for line in f:
            m = _SECTION_RE.match(line)
            if m:
                if cur is not None:
                    accounts.append(cur)
                cur = {"enabled": m.group(1) is None, "email": "",
                       "password": "", "routes": []}
                continue
            if cur is None:
                continue
            km = _KEY_RE.match(line)
            if not km:
                continue
            key, val = km.group("k").lower(), km.group("v").strip()
            if key == "routes":
                cur["routes"] = [r.strip().upper() for r in val.split(",") if r.strip()]
            else:
                cur[key] = val
    if cur is not None:
        accounts.append(cur)
    return accounts


def known_routes():
    """All route codes defined in vfs_urls.ini (commented or not)."""
    routes = []
    if os.path.isfile(URLS_FILE):
        with open(URLS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                m = _ROUTE_DEF_RE.match(line)
                if m:
                    code = m.group("code").upper()
                    if code not in routes:
                        routes.append(code)
    return routes


def route_universe(accounts):
    """Known routes plus any route already referenced by an account (so existing
    assignments to a currently-commented route are never lost)."""
    routes = known_routes()
    for a in accounts:
        for r in a.get("routes", []):
            if r not in routes:
                routes.append(r)
    return sorted(routes)


# Dest-code -> country name, for the per-country account-count summary in the UI.
# Mirrors src/utils/telegram_message.DESTINATION_NAMES (kept local so this editor
# stays import-light and runnable standalone).
COUNTRY_NAMES = {
    "MT": "Malta", "MLT": "Malta", "LUX": "Luxembourg", "CHE": "Switzerland",
    "DNK": "Denmark", "HUN": "Hungary", "CZE": "Czech Republic", "ITA": "Italy",
    "FRA": "France", "GRC": "Greece", "DEU": "Germany", "NOR": "Norway",
}


# --------------------------------------------------------------------------- #
# Write                                                                        #
# --------------------------------------------------------------------------- #

def validate(accounts):
    """Return a list of human-readable errors (empty = OK). Fully-empty rows are
    ignored (dropped on save); any row with data must have email + password."""
    errors = []
    for i, a in enumerate(accounts, 1):
        email = (a.get("email") or "").strip()
        pwd = (a.get("password") or "").strip()
        if not email and not pwd and not a.get("routes"):
            continue  # blank row -> dropped
        if not email:
            errors.append(f"Row {i}: email is required.")
        if not pwd:
            errors.append(f"Row {i}: password is required.")
    return errors


def render_ini(accounts):
    """Serialize accounts back to INI text, renumbering cred1..N in order."""
    out = list(_HEADER)
    n = 0
    for a in accounts:
        email = (a.get("email") or "").strip()
        pwd = (a.get("password") or "").strip()
        if not email and not pwd and not a.get("routes"):
            continue  # skip blank rows
        n += 1
        routes = ", ".join(r.strip().upper() for r in a.get("routes", []) if r.strip())
        block = [f"[cred{n}]", f"email = {email}", f"password = {pwd}",
                 f"routes = {routes}"]
        if not a.get("enabled", True):
            block = ["; " + b for b in block]
        out.append("")
        out.extend(block)
    return "\n".join(out) + "\n"


def save_credentials(accounts):
    """Backup then atomically rewrite the file. Returns (ok, message)."""
    errors = validate(accounts)
    if errors:
        return False, " ".join(errors)

    try:
        backup = _rolling_backup(CRED_FILE)
    except OSError as e:
        return False, f"Could not create backup: {e}"

    text = render_ini(accounts)
    tmp = CRED_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(CRED_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, CRED_FILE)
    except OSError as e:
        return False, f"Write failed: {e}"

    enabled = sum(1 for a in accounts if a.get("enabled") and (a.get("email") or "").strip())
    msg = f"Saved {enabled} enabled account(s)."
    if backup:
        msg += f" Backup: {os.path.basename(backup)}"
    return True, msg


# --------------------------------------------------------------------------- #
# Routes / countries (config/vfs_urls.ini) — toggle what the bot runs          #
# --------------------------------------------------------------------------- #

# 'AE-XXX = https://...' (enabled) or ';AE-XXX = ...' (disabled/commented).
_ROUTE_LINE_RE = re.compile(
    r"^\s*(?P<c>;+)?\s*(?P<code>AE-[A-Z]{2,5})\s*=\s*(?P<url>\S.*?)\s*$", re.IGNORECASE)
_CODE_RE = re.compile(r"^AE-[A-Z]{2,5}$")


def parse_routes():
    """Read vfs_urls.ini into [{code, url, enabled}, ...] in file order. A
    commented line ('; AE-XXX = ...') is a DISABLED (paused) route."""
    routes = []
    if not os.path.isfile(URLS_FILE):
        return routes
    with open(URLS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            m = _ROUTE_LINE_RE.match(line)
            if m:
                routes.append({
                    "code": m.group("code").upper(),
                    "url": m.group("url").strip(),
                    "enabled": m.group("c") is None,
                })
    return routes


def validate_routes(routes):
    """Errors for the routes list ([] = OK). Blank rows are dropped."""
    errors, seen = [], set()
    for i, r in enumerate(routes, 1):
        code = (r.get("code") or "").strip().upper()
        url = (r.get("url") or "").strip()
        if not code and not url:
            continue
        if not _CODE_RE.match(code):
            errors.append(f"Row {i}: code '{code or '(blank)'}' must look like AE-XXX.")
        if not url.lower().startswith("http"):
            errors.append(f"Row {i}: {code or 'route'} URL must start with http.")
        if code in seen:
            errors.append(f"Row {i}: duplicate route {code}.")
        seen.add(code)
    return errors


def render_routes_ini(routes):
    """Serialize routes back to vfs_urls.ini (enabled = uncommented line)."""
    out = [
        "; VFS portal login URLs — one per route. UNCOMMENT a line to ENABLE that",
        "; country (the bot runs every enabled AE-XXX route); COMMENT it to pause.",
        "; Managed by credentials_editor.py.",
        "",
        "[vfs-url]",
    ]
    for r in routes:
        code = (r.get("code") or "").strip().upper()
        url = (r.get("url") or "").strip()
        if not code or not url:
            continue
        line = f"{code} = {url}"
        out.append(line if r.get("enabled", True) else "; " + line)
    return "\n".join(out) + "\n"


def save_routes(routes):
    """Backup then atomically rewrite vfs_urls.ini. Returns (ok, message)."""
    errors = validate_routes(routes)
    if errors:
        return False, " ".join(errors)

    try:
        backup = _rolling_backup(URLS_FILE)
    except OSError as e:
        return False, f"Could not create backup: {e}"

    text = render_routes_ini(routes)
    tmp = URLS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, URLS_FILE)
    except OSError as e:
        return False, f"Write failed: {e}"

    enabled = sum(1 for r in routes if r.get("enabled") and (r.get("url") or "").strip())
    msg = f"Saved {enabled} enabled route(s)."
    if backup:
        msg += f" Backup: {os.path.basename(backup)}"
    return True, msg


# --------------------------------------------------------------------------- #
# Scheduler control (Windows Task Scheduler) — status + start/stop/enable/etc.  #
# --------------------------------------------------------------------------- #

_SCHED_ACTIONS = {
    "start": "Start-ScheduledTask",       # 'run now' (respects the overlap lock)
    "stop": "Stop-ScheduledTask",         # kill an in-progress run
    "enable": "Enable-ScheduledTask",     # resume the schedule
    "disable": "Disable-ScheduledTask",   # pause the schedule
}


def _ps(cmd: str, timeout: int = 30):
    """Run a PowerShell command; return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


def _powercfg_index(subgroup_setting: str):
    """The 'Current AC Power Setting Index' (int) for a powercfg setting, or None."""
    _, out, _ = _ps(f"powercfg /query SCHEME_CURRENT SUB_SLEEP {subgroup_setting}")
    m = re.search(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", out)
    return int(m.group(1), 16) if m else None


def scheduler_status() -> dict:
    """Task state + next/last run + sleep/wake diagnostics (best-effort)."""
    ps = (
        f"$ErrorActionPreference='SilentlyContinue';"
        f"$t=Get-ScheduledTask -TaskName '{TASK_NAME}';"
        f"if(-not $t){{Write-Output '{{\"exists\":false}}';exit}};"
        f"$i=Get-ScheduledTaskInfo -TaskName '{TASK_NAME}';"
        f"[ordered]@{{exists=$true;state=[string]$t.State;"
        f"nextRun=$(if($i.NextRunTime){{$i.NextRunTime.ToString('yyyy-MM-dd HH:mm')}}else{{''}});"
        f"lastRun=$(if($i.LastRunTime){{$i.LastRunTime.ToString('yyyy-MM-dd HH:mm')}}else{{''}});"
        f"lastResult=[int]$i.LastTaskResult;missed=[int]$i.NumberOfMissedRuns}}"
        f"|ConvertTo-Json -Compress"
    )
    _, out, err = _ps(ps)
    try:
        data = json.loads(out) if out else {"exists": False}
    except ValueError:
        data = {"exists": False, "error": (err or out or "query failed")[:200]}

    # Sleep/wake diagnostics (why ticks get missed on a sleeping laptop).
    secs = _powercfg_index("STANDBYIDLE")
    data["sleepAcMinutes"] = None if secs is None else secs // 60
    wake = _powercfg_index("RTCWAKE")
    data["wakeTimers"] = {0: "Disabled", 1: "Enabled", 2: "Important only"}.get(
        wake, None if wake is None else str(wake))
    return data


def scheduler_action(action: str):
    """Run a start/stop/enable/disable action on the task. Returns (ok, message)."""
    verb = _SCHED_ACTIONS.get(action)
    if not verb:
        return False, f"Unknown action: {action}"
    rc, out, err = _ps(f"{verb} -TaskName '{TASK_NAME}'")
    if rc == 0:
        return True, f"{action.capitalize()} OK."
    detail = (err or out or f"{action} failed").splitlines()
    return False, (detail[0] if detail else f"{action} failed")[:200]


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #

class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/credentials":
            accounts = parse_credentials()
            payload = {"accounts": accounts, "routes": route_universe(accounts),
                       "names": COUNTRY_NAMES,
                       "file": os.path.relpath(CRED_FILE, HERE)}
            self._send(200, json.dumps(payload))
        elif self.path == "/api/routes":
            payload = {"routes": parse_routes(),
                       "file": os.path.relpath(URLS_FILE, HERE)}
            self._send(200, json.dumps(payload))
        elif self.path == "/api/scheduler":
            self._send(200, json.dumps(scheduler_status()))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError) as e:
            self._send(400, json.dumps({"ok": False, "message": f"Bad request: {e}"}))
            return
        if self.path == "/api/credentials":
            ok, message = save_credentials(data.get("accounts", []))
        elif self.path == "/api/routes":
            ok, message = save_routes(data.get("routes", []))
        elif self.path == "/api/scheduler":
            ok, message = scheduler_action(data.get("action", ""))
        else:
            self._send(404, json.dumps({"error": "not found"}))
            return
        self._send(200 if ok else 400, json.dumps({"ok": ok, "message": message}))

    def log_message(self, *args):
        pass  # quiet


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VFS Editor</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font: 14px/1.4 system-ui, sans-serif; margin: 0; padding: 24px;
         background: #f5f6f8; color: #1a1a1a; }
  @media (prefers-color-scheme: dark) {
    body { background: #16181d; color: #e8e8e8; }
    details.panel, thead th { background: #1f232b !important; }
    details.panel > summary { background: #23272f !important; }
    input { background: #14161a; color: #e8e8e8; border-color: #3a3f47 !important; }
    tbody tr:nth-child(even) { background: #1b1e24; }
    .route { background: #262b33; }
  }
  h1 { font-size: 20px; margin: 0 0 16px; }
  .sub { color: #888; font-size: 13px; }
  details.panel { background: #fff; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
                  margin-bottom: 18px; overflow: hidden; }
  details.panel > summary { cursor: pointer; padding: 14px 18px; font-size: 15px; font-weight: 600;
                            background: #eef0f3; list-style: none; user-select: none; }
  details.panel > summary::-webkit-details-marker { display: none; }
  details.panel > summary::before { content: '▸'; display: inline-block; width: 1.2em; color: #888; }
  details.panel[open] > summary::before { content: '▾'; }
  details.panel > summary .sub { font-weight: 400; margin-left: 6px; }
  .body { padding: 16px; }
  table { border-collapse: collapse; width: 100%; }
  th, td { padding: 8px 10px; text-align: left; vertical-align: top; border-bottom: 1px solid #e2e4e8; }
  thead th { background: #eef0f3; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #666; }
  input[type=text] { width: 100%; padding: 6px 8px; border: 1px solid #cdd2d8; border-radius: 6px; font: inherit; }
  .num { color: #999; font-variant-numeric: tabular-nums; }
  .routes { display: flex; flex-wrap: wrap; gap: 4px 8px; max-width: 340px; }
  .route { display: inline-flex; align-items: center; gap: 4px; padding: 2px 6px; border-radius: 5px; background: #eef0f3; font-size: 12px; white-space: nowrap; }
  .ops button, .toolbar button, .rowadd { font: inherit; cursor: pointer; border: 1px solid #cdd2d8; background: #fff; border-radius: 6px; padding: 5px 9px; }
  .ops button:hover, .toolbar button:hover, .rowadd:hover { background: #f0f0f0; }
  .toolbar { display: flex; gap: 10px; align-items: center; margin-top: 16px; }
  .save { background: #2563eb !important; color: #fff !important; border-color: #2563eb !important; padding: 8px 18px !important; font-weight: 600; }
  .save:hover { background: #1d4ed8 !important; }
  .del { color: #c0392b; }
  .st { margin-left: auto; font-weight: 500; }
  .ok { color: #16a34a; } .err { color: #dc2626; }
  .disabled input, .disabled .routes { opacity: .45; }
  td.center { text-align: center; }
  /* Accounts-per-country summary */
  .sumwrap { margin-bottom: 20px; }
  .sumtitle { font-weight: 600; font-size: 13px; margin-bottom: 8px; }
  table.summary { width: auto; min-width: 320px; border: 1px solid #e2e4e8; border-radius: 8px; overflow: hidden; }
  table.summary td, table.summary th { padding: 5px 14px; }
  table.summary td.num { color: #333; font-weight: 600; }
  @media (prefers-color-scheme: dark) { table.summary td.num { color: #e6e6e6; } table.summary { border-color: #333; } }
  code { background: rgba(127,127,127,.15); padding: 1px 5px; border-radius: 4px; }
  /* Scheduler header */
  .statusbar { background: #fff; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
               padding: 14px 18px; margin-bottom: 18px; display: flex; flex-wrap: wrap;
               align-items: center; gap: 10px 18px; }
  @media (prefers-color-scheme: dark) { .statusbar { background: #1f232b; } }
  .statusbar .facts { display: flex; flex-wrap: wrap; gap: 6px 18px; align-items: center; font-size: 13px; }
  .badge { font-weight: 700; padding: 3px 10px; border-radius: 999px; font-size: 12px;
           background: #6b7280; color: #fff; }
  .badge.ready { background: #16a34a; } .badge.running { background: #2563eb; }
  .badge.disabled { background: #9ca3af; } .badge.err { background: #dc2626; }
  .diag { color: #888; font-size: 12px; }
  .diag.warn { color: #b45309; font-weight: 600; }
  .sbtns { display: flex; gap: 8px; align-items: center; margin-left: auto; }
  .sbtns button { font: inherit; cursor: pointer; border: 1px solid #cdd2d8; background: #fff;
                  border-radius: 6px; padding: 6px 11px; }
  .sbtns button:hover { background: #f0f0f0; }
  .sbtns .run { background: #16a34a; color: #fff; border-color: #16a34a; font-weight: 600; }
  .sbtns .run:hover { background: #15803d; }
  .sbtns .stop { color: #c0392b; }
</style></head>
<body>
  <h1>VFS Editor</h1>

  <div class="statusbar" id="sched">
    <span class="badge" id="sstate">…</span>
    <div class="facts">
      <span>Next run: <b id="snext">–</b></span>
      <span>Last: <b id="slast">–</b> <span id="sresult"></span></span>
      <span>Missed: <b id="smissed">–</b></span>
      <span class="diag" id="sdiag"></span>
    </div>
    <div class="sbtns">
      <button class="run" onclick="schedAct('start')" title="Run the check now">▶ Run now</button>
      <button class="stop" onclick="schedAct('stop')" title="Kill an in-progress run">■ Stop</button>
      <button onclick="schedAct('enable')">Enable</button>
      <button onclick="schedAct('disable')">Disable</button>
      <button onclick="loadSched()" title="Refresh status">↻</button>
      <span class="st" id="sstatus"></span>
    </div>
  </div>

  <details class="panel" open>
    <summary>Accounts <span class="sub">— <code id="cfile">config/credentials.local.ini</code> · order = rotation order</span></summary>
    <div class="body">
      <div class="sumwrap">
        <div class="sumtitle">Accounts per country <span class="num">(enabled accounts eligible for each route; no routes = all)</span></div>
        <table class="summary">
          <thead><tr><th>Country</th><th style="width:80px">Code</th><th style="width:90px" class="num">Accounts</th></tr></thead>
          <tbody id="srows"></tbody>
        </table>
      </div>
      <table>
        <thead><tr>
          <th style="width:34px">On</th><th style="width:30px">#</th>
          <th>Email</th><th>Password</th>
          <th>Routes <span class="num">(none = all)</span></th><th style="width:120px">Move</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
      <button class="rowadd" style="margin-top:12px" onclick="addAcct()">+ Add account</button>
      <div class="toolbar">
        <button onclick="loadCreds()">↻ Reload</button>
        <button class="save" onclick="saveCreds()">Save accounts</button>
        <span class="st" id="cstatus"></span>
      </div>
    </div>
  </details>

  <details class="panel" open>
    <summary>Countries / Routes <span class="sub">— <code id="rfile">config/vfs_urls.ini</code> · toggle what the bot runs</span></summary>
    <div class="body">
      <table>
        <thead><tr>
          <th style="width:60px">Run</th><th style="width:120px">Code</th>
          <th>Login URL</th><th style="width:60px"></th>
        </tr></thead>
        <tbody id="rrows"></tbody>
      </table>
      <button class="rowadd" style="margin-top:12px" onclick="addRoute()">+ Add country</button>
      <div class="toolbar">
        <button onclick="loadRoutes()">↻ Reload</button>
        <button class="save" onclick="saveRoutes()">Save routes</button>
        <span class="st" id="rstatus"></span>
      </div>
    </div>
  </details>

<script>
function esc(s){ return (s||'').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }
function setStatus(id,msg,cls){ const s=document.getElementById(id); s.textContent=msg; s.className='st '+(cls||''); }

/* ---------- Scheduler control ---------- */
async function loadSched() {
  let d;
  try { d = await (await fetch('/api/scheduler')).json(); }
  catch(e){ setStatus('sstatus','status query failed','err'); return; }
  const badge = document.getElementById('sstate');
  if (!d.exists) {
    badge.textContent = 'NOT REGISTERED'; badge.className = 'badge err';
    document.getElementById('sdiag').textContent =
      'Task not registered — run setup_task.ps1';
    return;
  }
  const st = (d.state || '').toLowerCase();
  badge.textContent = d.state || '?';
  badge.className = 'badge ' + (st==='ready'?'ready':st==='running'?'running':st==='disabled'?'disabled':'');
  document.getElementById('snext').textContent = d.nextRun || '–';
  document.getElementById('slast').textContent = d.lastRun || '–';
  const res = document.getElementById('sresult');
  // 267009 (0x41301) = "task is currently running", not a failure.
  const lr = d.lastResult;
  res.textContent = (lr===0?'✅':(lr===267009?'⏳ running':(d.lastRun?'❌ '+lr:'')));
  document.getElementById('smissed').textContent = (d.missed==null?'–':d.missed);
  // Sleep/wake diagnostics — flag a laptop that sleeps during the run window.
  const diag = document.getElementById('sdiag');
  const parts = [];
  if (d.sleepAcMinutes!=null) parts.push('Sleep(AC): ' + (d.sleepAcMinutes===0?'never':d.sleepAcMinutes+' min'));
  if (d.wakeTimers) parts.push('Wake timers: ' + d.wakeTimers);
  diag.textContent = parts.join(' · ');
  diag.className = 'diag' + ((d.sleepAcMinutes && d.sleepAcMinutes>0 && d.sleepAcMinutes<60) ? ' warn' : '');
  if (d.sleepAcMinutes && d.sleepAcMinutes>0 && d.sleepAcMinutes<60)
    diag.textContent += ' — may miss ticks while asleep';
}
async function schedAct(action) {
  setStatus('sstatus', action + '…', '');
  const d = await (await fetch('/api/scheduler',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({action})})).json();
  setStatus('sstatus', d.message, d.ok?'ok':'err');
  loadSched();
}

/* ---------- Accounts ---------- */
let ROUTES = [], accounts = [], NAMES = {};

async function loadCreds() {
  const d = await (await fetch('/api/credentials')).json();
  ROUTES = d.routes || [];
  NAMES = d.names || {};
  accounts = (d.accounts || []).map(a => ({
    enabled: a.enabled !== false, email: a.email || '',
    password: a.password || '', routes: a.routes || []
  }));
  document.getElementById('cfile').textContent = d.file || 'credentials.local.ini';
  renderCreds();
  setStatus('cstatus', 'Loaded ' + accounts.length + ' account(s).', 'ok');
}

/* Per-country count: enabled accounts (with an email) eligible for each route —
   an account with NO routes is eligible for ALL of them. Recomputed live. */
function renderSummary() {
  const tb = document.getElementById('srows'); if (!tb) return;
  const counts = {}; ROUTES.forEach(rc => counts[rc] = 0);
  accounts.forEach(a => {
    if (!a.enabled || !(a.email || '').trim()) return;
    ROUTES.forEach(rc => { if (a.routes.length === 0 || a.routes.includes(rc)) counts[rc]++; });
  });
  tb.innerHTML = ROUTES.map(rc => {
    const code = rc.replace(/^AE-/, '');
    return `<tr><td>${esc(NAMES[code] || code)}</td>`
         + `<td class="num">${code}</td><td class="num">${counts[rc]}</td></tr>`;
  }).join('') || '<tr><td colspan="3" class="num">no routes defined</td></tr>';
}

function renderCreds() {
  const tb = document.getElementById('rows'); tb.innerHTML = '';
  accounts.forEach((a, i) => {
    const tr = document.createElement('tr');
    if (!a.enabled) tr.className = 'disabled';
    const chips = ROUTES.map(rc => {
      const on = a.routes.includes(rc) ? 'checked' : '';
      return `<label class="route"><input type="checkbox" ${on}
              onchange="toggleRoute(${i},'${rc}',this.checked)">${rc}</label>`;
    }).join('');
    tr.innerHTML = `
      <td class="center"><input type="checkbox" ${a.enabled?'checked':''}
          onchange="setAcct(${i},'enabled',this.checked)"></td>
      <td class="num">${i+1}</td>
      <td><input type="text" value="${esc(a.email)}"
          oninput="setAcct(${i},'email',this.value)" placeholder="name@travnook.com"></td>
      <td><input type="text" value="${esc(a.password)}"
          oninput="setAcct(${i},'password',this.value)" placeholder="password"></td>
      <td><div class="routes">${chips || '<span class=num>no routes defined</span>'}</div></td>
      <td class="ops">
        <button onclick="moveAcct(${i},-1)" ${i===0?'disabled':''}>↑</button>
        <button onclick="moveAcct(${i},1)" ${i===accounts.length-1?'disabled':''}>↓</button>
        <button class="del" onclick="delAcct(${i})">✕</button>
      </td>`;
    tb.appendChild(tr);
  });
  renderSummary();
}
function setAcct(i,k,v){ accounts[i][k]=v; if(k==='enabled') renderCreds(); else renderSummary(); }
function toggleRoute(i,rc,on){ const s=new Set(accounts[i].routes);
  on?s.add(rc):s.delete(rc); accounts[i].routes=ROUTES.filter(r=>s.has(r)); renderSummary(); }
function addAcct(){ accounts.push({enabled:true,email:'',password:'',routes:[]}); renderCreds(); }
function delAcct(i){ accounts.splice(i,1); renderCreds(); }
function moveAcct(i,d){ const j=i+d; if(j<0||j>=accounts.length)return;
  [accounts[i],accounts[j]]=[accounts[j],accounts[i]]; renderCreds(); }
async function saveCreds(){
  setStatus('cstatus','Saving…','');
  const d = await (await fetch('/api/credentials',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({accounts})})).json();
  setStatus('cstatus', d.message, d.ok?'ok':'err');
  if(d.ok) loadCreds();
}

/* ---------- Countries / Routes ---------- */
let routes = [];

async function loadRoutes() {
  const d = await (await fetch('/api/routes')).json();
  routes = (d.routes || []).map(r => ({enabled:r.enabled!==false, code:r.code||'', url:r.url||''}));
  document.getElementById('rfile').textContent = d.file || 'vfs_urls.ini';
  renderRoutes();
  const on = routes.filter(r=>r.enabled).length;
  setStatus('rstatus', `Loaded ${routes.length} route(s), ${on} enabled.`, 'ok');
}

function renderRoutes() {
  const tb = document.getElementById('rrows'); tb.innerHTML = '';
  routes.forEach((r, i) => {
    const tr = document.createElement('tr');
    if (!r.enabled) tr.className = 'disabled';
    tr.innerHTML = `
      <td class="center"><input type="checkbox" ${r.enabled?'checked':''}
          onchange="setRoute(${i},'enabled',this.checked)" title="run this country"></td>
      <td><input type="text" value="${esc(r.code)}" placeholder="AE-XXX"
          oninput="setRoute(${i},'code',this.value.toUpperCase())"></td>
      <td><input type="text" value="${esc(r.url)}"
          oninput="setRoute(${i},'url',this.value)"
          placeholder="https://visa.vfsglobal.com/are/en/xxx/login"></td>
      <td class="ops"><button class="del" onclick="delRoute(${i})">✕</button></td>`;
    tb.appendChild(tr);
  });
}
function setRoute(i,k,v){ routes[i][k]=v; if(k==='enabled') renderRoutes(); }
function addRoute(){ routes.push({enabled:true,code:'AE-',
  url:'https://visa.vfsglobal.com/are/en/xxx/login'}); renderRoutes(); }
function delRoute(i){ routes.splice(i,1); renderRoutes(); }
async function saveRoutes(){
  setStatus('rstatus','Saving…','');
  const d = await (await fetch('/api/routes',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({routes})})).json();
  setStatus('rstatus', d.message, d.ok?'ok':'err');
  if(d.ok){ loadRoutes(); loadCreds(); }   /* refresh account route chips too */
}

loadSched();
loadCreds();
loadRoutes();
</script>
</body></html>
"""


def main():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        url = f"http://127.0.0.1:{PORT}"
        print(f"Accounts + routes editor serving at {url}")
        print(f"  accounts: {CRED_FILE}")
        print(f"  routes:   {URLS_FILE}")
        print("Press Ctrl+C to stop.")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
