"""Local web UI to view/edit config/config.ini — standalone tool.

Same idea as credentials_editor.py, but for the main settings file. config.ini is
a set of sections of `key = value` with lots of explanatory comments, so this is
a COMMENT-PRESERVING form editor: it edits values in place and keeps every
comment, blank line and section exactly as they are. It only changes the values
of keys you touch — it never adds/removes keys or reflows the file.

Stdlib only, binds to 127.0.0.1 ONLY. Run it, edit, Save:

    & .venv\\Scripts\\python.exe config_editor.py
        -> http://127.0.0.1:8766  (opens automatically)

It edits config/config.local.ini — the REAL working file (gitignored, holds
secrets + live values). config.ini is only the committed reference template.

Each Save writes a timestamped backup (config.local.ini.bak-<time>) first, then
atomically rewrites the file.
"""

import http.server
import json
import os
import shutil
import socketserver
import webbrowser
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
# The REAL working config (gitignored, holds secrets + live values). config.ini
# is only the committed reference template, so the editor targets the real file.
CONFIG_FILE = os.path.join(HERE, "config", "config.local.ini")
PORT = 8766


# --------------------------------------------------------------------------- #
# Parse (keeps raw lines so save can preserve everything)                       #
# --------------------------------------------------------------------------- #

def parse_config():
    """
    Returns (sections, lines). `lines` is the raw file. `sections` is
    [{name, keys:[{key, value, help, line}]}] where `line` is the 0-based index
    of that key's line in `lines`, and `help` is the comment block above it.
    """
    sections = []
    lines = []
    if not os.path.isfile(CONFIG_FILE):
        return sections, lines
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        lines = f.read().split("\n")

    cur = None
    pending = []          # accumulated comment lines for the next key(s)
    last_was_key = False
    for idx, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = {"name": s[1:-1], "keys": []}
            sections.append(cur)
            pending = []
            last_was_key = False
        elif s.startswith(";") or s.startswith("#"):
            if last_was_key:          # a comment after a key starts a new block
                pending = []
                last_was_key = False
            pending.append(s.lstrip(";# ").rstrip())
        elif s == "":
            pending = []
            last_was_key = False
        elif "=" in raw and cur is not None:
            key, _, value = raw.partition("=")
            cur["keys"].append({
                "key": key.strip(), "value": value.strip(),
                "help": " ".join(p for p in pending if p), "line": idx,
            })
            last_was_key = True
    return sections, lines


# --------------------------------------------------------------------------- #
# Save (rewrite only changed value lines)                                      #
# --------------------------------------------------------------------------- #

def save_config(updates):
    """
    updates: {"<section>.<key>": "<new value>", ...}. Rewrites only those key
    lines, preserving everything else. Returns (ok, message).
    """
    sections, lines = parse_config()
    if not lines:
        return False, "config.ini not found."

    # Map "section.key" -> line index.
    index = {}
    for sec in sections:
        for k in sec["keys"]:
            index[f"{sec['name']}.{k['key']}"] = k["line"]

    changed = 0
    for dotted, new_value in (updates or {}).items():
        li = index.get(dotted)
        if li is None:
            continue  # unknown key (deleted/renamed) — skip silently
        key = dotted.split(".", 1)[1]
        new_line = f"{key} = {new_value}"
        if lines[li] != new_line:
            lines[li] = new_line
            changed += 1

    if changed == 0:
        return True, "No changes to save."

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{CONFIG_FILE}.bak-{stamp}"
    try:
        shutil.copy2(CONFIG_FILE, backup)
    except OSError as e:
        return False, f"Could not create backup: {e}"

    tmp = CONFIG_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp, CONFIG_FILE)
    except OSError as e:
        return False, f"Write failed: {e}"

    return True, f"Saved {changed} change(s). Backup: {os.path.basename(backup)}"


# --------------------------------------------------------------------------- #
# HTTP                                                                          #
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
        elif self.path == "/api/config":
            sections, _ = parse_config()
            self._send(200, json.dumps({
                "sections": sections,
                "file": os.path.relpath(CONFIG_FILE, HERE),
            }))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path != "/api/config":
            self._send(404, json.dumps({"error": "not found"}))
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            updates = data.get("updates", {})
        except (ValueError, TypeError) as e:
            self._send(400, json.dumps({"ok": False, "message": f"Bad request: {e}"}))
            return
        ok, message = save_config(updates)
        self._send(200 if ok else 400, json.dumps({"ok": ok, "message": message}))

    def log_message(self, *args):
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VFS Config Editor</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font: 14px/1.45 system-ui, sans-serif; margin: 0; padding: 24px;
         background: #f5f6f8; color: #1a1a1a; }
  @media (prefers-color-scheme: dark) {
    body { background: #16181d; color: #e8e8e8; }
    .sec { background: #1f232b !important; }
    input, select { background: #14161a; color: #e8e8e8; border-color: #3a3f47 !important; }
    .help { color: #8b93a0 !important; }
    h2 { border-color: #3a3f47 !important; }
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #888; margin: 0 0 20px; font-size: 13px; }
  .warn { color: #b45309; }
  .sec { background: #fff; border-radius: 10px; padding: 8px 18px 18px;
         box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 18px; }
  h2 { font-size: 14px; text-transform: uppercase; letter-spacing: .05em; color: #2563eb;
       border-bottom: 1px solid #e2e4e8; padding-bottom: 8px; margin: 12px 0 4px; }
  .field { display: grid; grid-template-columns: 220px 1fr; gap: 12px; align-items: start;
           padding: 10px 0; border-bottom: 1px solid #f0f1f3; }
  .field:last-child { border-bottom: 0; }
  .k { font-family: ui-monospace, monospace; font-weight: 600; padding-top: 6px; word-break: break-all; }
  .help { color: #888; font-size: 12px; margin-top: 4px; }
  input[type=text], input[type=number], select { width: 100%; max-width: 420px;
    padding: 6px 8px; border: 1px solid #cdd2d8; border-radius: 6px; font: inherit; }
  .toolbar { position: sticky; bottom: 0; display: flex; gap: 12px; align-items: center;
             padding: 14px 0; background: linear-gradient(transparent, var(--bg, #f5f6f8) 40%); }
  .save { background: #2563eb; color: #fff; border: 1px solid #2563eb; border-radius: 6px;
          padding: 9px 20px; font: inherit; font-weight: 600; cursor: pointer; }
  .save:hover { background: #1d4ed8; }
  .reload { background: #fff; border: 1px solid #cdd2d8; border-radius: 6px;
            padding: 9px 14px; font: inherit; cursor: pointer; }
  #status { margin-left: auto; font-weight: 500; }
  .ok { color: #16a34a; } .err { color: #dc2626; }
</style></head>
<body>
  <h1>VFS Config Editor</h1>
  <p class="sub">Editing <code id="fname">config/config.local.ini</code> ·
     comments preserved · <span class="warn">the real working file (gitignored).</span></p>
  <div id="sections"></div>
  <div class="toolbar">
    <button class="reload" onclick="load()">↻ Reload</button>
    <button class="save" onclick="save()">Save to file</button>
    <span id="status"></span>
  </div>

<script>
let dirty = {};

function inputFor(sec, k) {
  const id = sec + '.' + k.key;
  const v = k.value;
  const low = v.toLowerCase();
  if (low === 'true' || low === 'false') {
    return `<select data-id="${id}" onchange="mark(this)">
      <option ${low==='true'?'selected':''}>true</option>
      <option ${low==='false'?'selected':''}>false</option></select>`;
  }
  if (v !== '' && /^-?\d+$/.test(v)) {
    return `<input type="number" data-id="${id}" value="${esc(v)}" oninput="mark(this)">`;
  }
  return `<input type="text" data-id="${id}" value="${esc(v)}" oninput="mark(this)"
          placeholder="(empty)">`;
}

async function load() {
  dirty = {};
  const d = await (await fetch('/api/config')).json();
  document.getElementById('fname').textContent = d.file;
  const host = document.getElementById('sections');
  host.innerHTML = '';
  (d.sections || []).forEach(sec => {
    const div = document.createElement('div');
    div.className = 'sec';
    let html = `<h2>[${sec.name}]</h2>`;
    sec.keys.forEach(k => {
      html += `<div class="field">
        <div class="k">${esc(k.key)}</div>
        <div>${inputFor(sec.name, k)}${k.help ? `<div class="help">${esc(k.help)}</div>`:''}</div>
      </div>`;
    });
    div.innerHTML = html;
    host.appendChild(div);
  });
  setStatus('Loaded.', 'ok');
}

function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }
function mark(el){ dirty[el.dataset.id] = el.value; setStatus(Object.keys(dirty).length + ' unsaved change(s).',''); }

async function save() {
  if (!Object.keys(dirty).length) { setStatus('Nothing to save.',''); return; }
  setStatus('Saving…','');
  const d = await (await fetch('/api/config', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({updates: dirty})})).json();
  setStatus(d.message, d.ok?'ok':'err');
  if (d.ok) load();
}
function setStatus(msg,cls){ const s=document.getElementById('status'); s.textContent=msg; s.className=cls||''; }

load();
</script>
</body></html>
"""


def main():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        url = f"http://127.0.0.1:{PORT}"
        print(f"Config editor serving at {url}")
        print(f"Editing: {CONFIG_FILE}")
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
