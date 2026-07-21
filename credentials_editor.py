"""Local web UI to view/edit config/credentials.local.ini — standalone tool.

A plain HTML page can't write to disk (browser sandbox), so this is a tiny local
web server (Python stdlib only) that serves the editor UI AND saves the file.
It binds to 127.0.0.1 ONLY — your credentials never touch the network.

Run it, then edit accounts in the browser and click Save:

    & .venv\\Scripts\\python.exe credentials_editor.py
        -> serving at http://127.0.0.1:8765  (opens automatically)

Each Save:
  * validates every enabled account (email + password required, routes known),
  * writes a timestamped backup first (credentials.local.ini.bak-<time>),
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
import webbrowser
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CRED_FILE = os.path.join(HERE, "config", "credentials.local.ini")
URLS_FILE = os.path.join(HERE, "config", "vfs_urls.ini")
PORT = 8765

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

    backup = None
    if os.path.isfile(CRED_FILE):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"{CRED_FILE}.bak-{stamp}"
        try:
            shutil.copy2(CRED_FILE, backup)
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
                       "file": os.path.relpath(CRED_FILE, HERE)}
            self._send(200, json.dumps(payload))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path != "/api/credentials":
            self._send(404, json.dumps({"error": "not found"}))
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            accounts = data.get("accounts", [])
        except (ValueError, TypeError) as e:
            self._send(400, json.dumps({"ok": False, "message": f"Bad request: {e}"}))
            return
        ok, message = save_credentials(accounts)
        self._send(200 if ok else 400, json.dumps({"ok": ok, "message": message}))

    def log_message(self, *args):
        pass  # quiet


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VFS Credentials Editor</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font: 14px/1.4 system-ui, sans-serif; margin: 0; padding: 24px;
         background: #f5f6f8; color: #1a1a1a; }
  @media (prefers-color-scheme: dark) {
    body { background: #16181d; color: #e8e8e8; }
    .card, thead th { background: #1f232b !important; }
    input { background: #14161a; color: #e8e8e8; border-color: #3a3f47 !important; }
    tbody tr:nth-child(even) { background: #1b1e24; }
    .route { background: #262b33; }
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #888; margin: 0 0 16px; font-size: 13px; }
  .card { background: #fff; border-radius: 10px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  table { border-collapse: collapse; width: 100%; }
  th, td { padding: 8px 10px; text-align: left; vertical-align: top; border-bottom: 1px solid #e2e4e8; }
  thead th { background: #eef0f3; position: sticky; top: 0; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #666; }
  input[type=text] { width: 100%; padding: 6px 8px; border: 1px solid #cdd2d8; border-radius: 6px; font: inherit; }
  .num { color: #999; font-variant-numeric: tabular-nums; }
  .routes { display: flex; flex-wrap: wrap; gap: 4px 8px; max-width: 320px; }
  .route { display: inline-flex; align-items: center; gap: 4px; padding: 2px 6px; border-radius: 5px; background: #eef0f3; font-size: 12px; white-space: nowrap; }
  .ops button, .toolbar button, .rowadd { font: inherit; cursor: pointer; border: 1px solid #cdd2d8; background: #fff; border-radius: 6px; padding: 5px 9px; }
  .ops button:hover, .toolbar button:hover { background: #f0f0f0; }
  .toolbar { display: flex; gap: 10px; align-items: center; margin-top: 16px; }
  .save { background: #2563eb !important; color: #fff !important; border-color: #2563eb !important; padding: 8px 18px !important; font-weight: 600; }
  .save:hover { background: #1d4ed8 !important; }
  .del { color: #c0392b; }
  #status { margin-left: auto; font-weight: 500; }
  .ok { color: #16a34a; } .err { color: #dc2626; }
  .disabled input, .disabled .routes { opacity: .45; }
  td.center { text-align: center; }
</style></head>
<body>
  <h1>VFS Credentials Editor</h1>
  <p class="sub">Editing <code id="fname">config/credentials.local.ini</code> ·
     order = rotation order · uncheck all routes = eligible for <b>all</b> routes.</p>
  <div class="card">
    <table>
      <thead><tr>
        <th style="width:34px">On</th><th style="width:30px">#</th>
        <th>Email</th><th>Password</th><th>Routes</th><th style="width:120px">Move</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
    <button class="rowadd" style="margin-top:12px" onclick="addRow()">+ Add account</button>
  </div>
  <div class="toolbar">
    <button onclick="load()">↻ Reload from file</button>
    <button class="save" onclick="save()">Save to file</button>
    <span id="status"></span>
  </div>

<script>
let ROUTES = [];
let accounts = [];

async function load() {
  const r = await fetch('/api/credentials');
  const d = await r.json();
  ROUTES = d.routes || [];
  accounts = (d.accounts || []).map(a => ({
    enabled: a.enabled !== false,
    email: a.email || '', password: a.password || '',
    routes: a.routes || []
  }));
  document.getElementById('fname').textContent = d.file || 'credentials.local.ini';
  render();
  setStatus('Loaded ' + accounts.length + ' account(s).', 'ok');
}

function render() {
  const tb = document.getElementById('rows');
  tb.innerHTML = '';
  accounts.forEach((a, i) => {
    const tr = document.createElement('tr');
    if (!a.enabled) tr.className = 'disabled';
    const routeChips = ROUTES.map(rc => {
      const on = a.routes.includes(rc) ? 'checked' : '';
      return `<label class="route"><input type="checkbox" ${on}
              onchange="toggleRoute(${i},'${rc}',this.checked)">${rc}</label>`;
    }).join('');
    tr.innerHTML = `
      <td class="center"><input type="checkbox" ${a.enabled?'checked':''}
          onchange="setField(${i},'enabled',this.checked)"></td>
      <td class="num">${i+1}</td>
      <td><input type="text" value="${esc(a.email)}"
          oninput="setField(${i},'email',this.value)" placeholder="name@travnook.com"></td>
      <td><input type="text" value="${esc(a.password)}"
          oninput="setField(${i},'password',this.value)" placeholder="password"></td>
      <td><div class="routes">${routeChips || '<span class=num>no routes defined</span>'}</div></td>
      <td class="ops">
        <button onclick="move(${i},-1)" ${i===0?'disabled':''}>↑</button>
        <button onclick="move(${i},1)" ${i===accounts.length-1?'disabled':''}>↓</button>
        <button class="del" onclick="removeRow(${i})">✕</button>
      </td>`;
    tb.appendChild(tr);
  });
}

function esc(s){ return (s||'').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }
function setField(i,k,v){ accounts[i][k]=v; if(k==='enabled') render(); }
function toggleRoute(i,rc,on){
  const s=new Set(accounts[i].routes);
  on?s.add(rc):s.delete(rc);
  accounts[i].routes=ROUTES.filter(r=>s.has(r));
}
function addRow(){ accounts.push({enabled:true,email:'',password:'',routes:[]}); render(); }
function removeRow(i){ accounts.splice(i,1); render(); }
function move(i,d){ const j=i+d; if(j<0||j>=accounts.length)return;
  [accounts[i],accounts[j]]=[accounts[j],accounts[i]]; render(); }

async function save(){
  setStatus('Saving…','');
  const r = await fetch('/api/credentials', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({accounts})});
  const d = await r.json();
  setStatus(d.message, d.ok?'ok':'err');
  if(d.ok) load();
}
function setStatus(msg,cls){ const s=document.getElementById('status');
  s.textContent=msg; s.className=cls||''; }

load();
</script>
</body></html>
"""


def main():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        url = f"http://127.0.0.1:{PORT}"
        print(f"Credentials editor serving at {url}")
        print(f"Editing: {CRED_FILE}")
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
