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
import sys
import urllib.parse
import time
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

# --------------------------------------------------------------------------- #
# Account health (/account-health)                                            #
#                                                                             #
# The circuit-breaker state lives in src/utils/account_health.py, so THIS page #
# is the one part of the editor that needs the bot's own code. The import is   #
# deliberately lazy and optional: the Accounts and Routes panels keep working  #
# standalone (the stated design of this tool) and only the health page reports #
# that it is unavailable.                                                      #
# --------------------------------------------------------------------------- #

_health_mod = None
_health_err = ""


def health_module():
    """The bot's account_health module, or None with the reason in _health_err."""
    global _health_mod, _health_err
    if _health_mod is not None or _health_err:
        return _health_mod
    try:
        # account_health resolves account_health.json relative to the CWD, and
        # this tool may be launched from anywhere.
        os.chdir(HERE)
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        from src.utils.config_reader import initialize_config
        initialize_config()
        from src.utils import account_health
        _health_mod = account_health
    except Exception as e:                                   # pragma: no cover
        _health_err = f"{type(e).__name__}: {e}"
    return _health_mod


def _state_of(rec: dict, enabled: bool, now: float):
    """Collapse one account's health record to a single state + when it frees.

    Precedence matters: an account switched OFF in the INI is not in the pool at
    all, so that outranks anything the breaker recorded; an indefinite disable
    outranks a timed bench; a bench outranks unspent strikes.
    """
    routes = rec.get("routes") or {}
    until = max([rec.get("cooldown_until", 0) or 0]
                + [r.get("cooldown_until", 0) or 0 for r in routes.values()])
    strikes = max([0] + [int(r.get("fails", 0) or 0) for r in routes.values()])
    if not enabled:
        return "off", until, strikes
    if rec.get("disabled"):
        return "disabled", 0, strikes
    if until > now:
        return "benched", until, strikes
    if strikes:
        return "strikes", 0, strikes
    return "healthy", 0, strikes


def health_payload():
    """Accounts joined with their health record, plus the thresholds in force."""
    ah = health_module()
    accounts = parse_credentials()
    universe = route_universe(accounts)
    if ah is None:
        return {"available": False, "error": _health_err, "accounts": [],
                "routes": universe, "names": COUNTRY_NAMES}
    snap = ah.snapshot()
    now = time.time()
    rows = []
    for a in accounts:
        email = a.get("email", "")
        rec = snap.get(email, {}) or {}
        rroutes = rec.get("routes") or {}
        # Routes the account is eligible for; an empty list in the INI means all.
        eligible = a.get("routes") or universe
        detail = []
        for code in eligible:
            r = rroutes.get(code, {}) or {}
            until = r.get("cooldown_until", 0) or 0
            detail.append({
                "route": code,
                "benched": until > now,
                "until": until,
                "strikes": int(r.get("fails", 0) or 0),
                "reason": r.get("last_reason", "") or "",
                "updated": r.get("updated_at", 0) or 0,
            })
        # A cooldown on a route the account is no longer assigned to still
        # matters — surface it rather than hiding it.
        for code, r in sorted(rroutes.items()):
            if code in eligible:
                continue
            until = r.get("cooldown_until", 0) or 0
            detail.append({
                "route": code, "benched": until > now, "until": until,
                "strikes": int(r.get("fails", 0) or 0),
                "reason": r.get("last_reason", "") or "",
                "updated": r.get("updated_at", 0) or 0, "unassigned": True,
            })
        state, until, strikes = _state_of(rec, a.get("enabled", True), now)
        last = max(detail, key=lambda d: d["updated"], default=None) if detail else None
        rows.append({
            "email": email, "enabled": a.get("enabled", True),
            "state": state, "until": until, "strikes": strikes,
            "disabled_reason": rec.get("disabled_reason", "") or "",
            "disabled_at": rec.get("disabled_at", 0) or 0,
            "reason": (last or {}).get("reason", "") if last else "",
            "routes": detail,
        })
    # Health records with no matching credential — orphans worth cleaning up.
    known = {a.get("email", "") for a in accounts}
    orphans = [{"email": e, "record": r} for e, r in snap.items() if e not in known]
    return {
        "available": True, "accounts": rows, "orphans": orphans,
        "routes": universe, "names": COUNTRY_NAMES, "now": now,
        "rules": {"fail_threshold": ah.fail_threshold(),
                  "soft_hours": ah.soft_cooldown_hours(),
                  "hard_hours": ah.hard_cooldown_hours()},
        "file": "account_health.json",
    }


def health_action(data: dict):
    """Apply one health/credential action. Returns (ok, message).

    clear / bench / disable / enable touch account_health.json only. add /
    remove / toggle change WHICH accounts exist, so they go through the same
    validate + rolling-backup writer the Accounts panel uses — one writer for
    that file, not two.
    """
    ah = health_module()
    action = (data.get("action") or "").strip().lower()
    email = (data.get("email") or "").strip()
    route = (data.get("route") or "").strip().upper() or None

    if action in ("add", "remove", "toggle"):
        accounts = parse_credentials()
        if action == "add":
            if not email:
                return False, "Email is required."
            if any(a.get("email", "").lower() == email.lower() for a in accounts):
                return False, f"{email} is already listed."
            accounts.append({"enabled": True, "email": email,
                             "password": data.get("password", ""), "routes": []})
            ok, msg = save_credentials(accounts)
            return ok, (f"Added {email}." if ok else msg)
        target = [a for a in accounts if a.get("email", "").lower() == email.lower()]
        if not target:
            return False, f"{email} is not in the credentials file."
        if action == "remove":
            accounts = [a for a in accounts if a is not target[0]]
            ok, msg = save_credentials(accounts)
            # Drop the health record too, so re-adding the account later starts clean.
            if ok and ah is not None:
                try:
                    ah.clear(email)
                except Exception:
                    pass
            return ok, (f"Removed {email}." if ok else msg)
        target[0]["enabled"] = not target[0].get("enabled", True)
        ok, msg = save_credentials(accounts)
        state = "in rotation" if target[0]["enabled"] else "out of rotation"
        return ok, (f"{email} is now {state}." if ok else msg)

    if ah is None:
        return False, f"Account health is unavailable ({_health_err})."
    if not email:
        return False, "Email is required."

    if action == "clear":
        changed = ah.clear(email, route)
        where = f" on {route}" if route else ""
        return True, (f"Cleared {email}{where}." if changed
                      else f"{email} had nothing to clear{where}.")
    if action == "enable":
        ah.clear(email)
        return True, f"{email} re-enabled."
    if action == "disable":
        reason = (data.get("reason") or "").strip() or "disabled from the editor"
        ah.disable(email, reason)
        return True, f"{email} disabled — {reason}."
    if action == "bench":
        try:
            hours = float(data.get("hours", 2))
        except (TypeError, ValueError):
            return False, "Hours must be a number."
        if hours <= 0:
            return False, "Hours must be greater than zero."
        reason = (data.get("reason") or "").strip() or "benched from the editor"
        targets = [route] if route else (
            [a.get("routes") for a in parse_credentials()
             if a.get("email", "").lower() == email.lower()] or [[]])[0] \
            or route_universe(parse_credentials())
        if isinstance(targets, str):
            targets = [targets]
        for code in targets:
            ah.bench(email, code, hours, reason)
        scope = route if route else f"{len(targets)} route(s)"
        return True, f"Benched {email} on {scope} for {hours:g}h."
    if action == "clear_all":
        n = 0
        for e in list(ah.snapshot()):
            if ah.clear(e):
                n += 1
        return True, f"Cleared {n} account record(s)."
    return False, f"Unknown action '{action}'."


# --------------------------------------------------------------------------- #
# Stats (/stats)                                                              #
#                                                                             #
# Everything on this page is DERIVED from files the bot already writes:       #
# logs/app-YYYY-MM-DD.log, account_health.json and bandwidth_budget.json.     #
# Nothing new is instrumented, so the page can never disagree with the logs   #
# and costs the bot nothing at runtime.                                       #
#                                                                             #
# Parsing every log on every request would mean re-reading ~30 MB, so results #
# are cached per file and invalidated on (mtime, size) — today's log re-parses #
# as it grows, finished days are parsed exactly once.                         #
# --------------------------------------------------------------------------- #

LOG_DIR = os.path.join(HERE, "logs")
BUDGET_FILE = os.path.join(HERE, "bandwidth_budget.json")

_LOG_NAME_RE = re.compile(r"^app-(\d{4}-\d{2}-\d{2})\.log$")
_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})")

# Message-text patterns, never line numbers — the emitting line moves when the
# source is edited, which silently broke an earlier version of this analysis.
_PAT = {
    "route":     re.compile(r"Route \d+/\d+: ([A-Z]+-[A-Z]+)"),
    "attempt":   re.compile(r"=== Attempt (\d+)/(\d+) \(ip ([^)]*)\)"),
    "proxy_mb":  re.compile(r"Proxy traffic this route: ([\d.]+) MB"),
    "brow_mb":   re.compile(r"Browser traffic this route: ([\d.]+) MB"),
    "run_mb":    re.compile(r"Total proxy traffic this run: ([\d.]+) MB"),
    "host_mb":   re.compile(r"^\s+([\d.]+) MB  (\S+)\s*$"),
    "outcome":   re.compile(r"Route ([A-Z]+-[A-Z]+) ([A-Z]+)\."),
    "lasterr":   re.compile(r"All \d+ attempts? failed.*?Last error: (.+)"),
    # Anchored on the "  -> " prefix that slot_check emits ONCE per combination.
    # The unanchored text also appears in the Telegram message body, which would
    # count every find two or three times over.
    "slot":      re.compile(r"->\s+Earliest available slot for ([\d,]+) Applicants"
                            r" is : ([\d-]+)"),
    "combo":     re.compile(r"Checking slot for: (.+?)\s*$"),
    "denied":    re.compile(r"Denylisted hosts refused this route: (\d+)"),
    "restrict":  re.compile(r"Access restricted for ([A-Z-]+) \[([^]]+)\]"),
    "strike":    re.compile(r"Account (\S+) strike (\d+)/(\d+) on ([A-Z-]+)"),
}


def _blank_day(day):
    return {
        "day": day, "first": "", "last": "",
        "runs": [], "attempts": [], "browser": [],
        "attempt_no": {}, "status": {}, "fail_reason": {}, "hosts": {},
        "hourly": {}, "routes": {}, "ports": {},
        "slots": [], "restricted": [], "strikes": [],
        "turnstile": {"widget": 0, "challenged": 0, "click_ok": 0,
                      "click_fail": 0, "gave_up": 0, "dashboard": 0,
                      "dialog": 0, "dialog_ok": 0, "cf_dropped": 0,
                      "session_expired": 0},
        "otp": {"reads": 0, "rejected": 0, "gave_up": 0, "text_mode": 0,
                "no_strike": 0},
        "infra": {"proxy_err": 0, "rotations": 0, "denylisted": 0,
                  "nav_budget": 0, "geo": 0},
    }


def _route_slot(d, code):
    return d["routes"].setdefault(code, {
        "attempts": 0, "mb": 0.0, "ok": 0, "fail": 0, "other": 0,
        "slot": 0, "waitlist": 0, "none": 0, "error": 0, "dates": [],
    })


def parse_log(path, day):
    """One pass over a day's log -> every aggregate the stats page needs."""
    d = _blank_day(day)
    route = None
    att_no = None
    port = None
    combo = ""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            ts = _TS_RE.match(line)
            if ts:
                stamp = f"{ts.group(1)} {ts.group(2)}:{ts.group(3)}:{ts.group(4)}"
                if not d["first"]:
                    d["first"] = stamp
                d["last"] = stamp
                hour = f"{ts.group(1)[5:]} {ts.group(2)}"
            else:
                hour = None

            m = _PAT["route"].search(line)
            if m:
                route = m.group(1)
                _route_slot(d, route)
                continue
            m = _PAT["attempt"].search(line)
            if m:
                att_no = m.group(1)
                port = (m.group(3) or "").rsplit(":", 1)[-1]
                d["attempt_no"][att_no] = d["attempt_no"].get(att_no, 0) + 1
                p = d["ports"].setdefault(port, {"attempts": 0, "challenged": 0})
                p["attempts"] += 1
                continue
            m = _PAT["proxy_mb"].search(line)
            if m:
                mb = float(m.group(1))
                d["attempts"].append(mb)
                if hour:
                    d["hourly"][hour] = round(d["hourly"].get(hour, 0.0) + mb, 3)
                if route:
                    r = _route_slot(d, route)
                    r["attempts"] += 1
                    r["mb"] = round(r["mb"] + mb, 3)
                continue
            m = _PAT["brow_mb"].search(line)
            if m:
                d["browser"].append(float(m.group(1)))
                continue
            m = _PAT["run_mb"].search(line)
            if m:
                d["runs"].append(float(m.group(1)))
                continue
            m = _PAT["host_mb"].match(line.split("] ", 2)[-1] if "] " in line else line)
            if m:
                d["hosts"][m.group(2)] = round(
                    d["hosts"].get(m.group(2), 0.0) + float(m.group(1)), 3)
                continue
            m = _PAT["outcome"].search(line)
            if m:
                code, status = m.group(1), m.group(2)
                d["status"][status] = d["status"].get(status, 0) + 1
                r = _route_slot(d, code)
                r["ok" if status == "OK" else
                  ("fail" if status == "FAILED" else "other")] += 1
                continue
            m = _PAT["lasterr"].search(line)
            if m:
                key = re.sub(r"\d{3,}", "N", m.group(1).strip())[:70]
                d["fail_reason"][key] = d["fail_reason"].get(key, 0) + 1
                continue
            m = _PAT["combo"].search(line)
            if m:
                combo = m.group(1)[:60]
                continue
            m = _PAT["slot"].search(line)
            if m and route:
                r = _route_slot(d, route)
                r["slot"] += 1
                if m.group(2) not in r["dates"]:
                    r["dates"].append(m.group(2))
                d["slots"].append({"at": d["last"][11:16], "route": route,
                                   "combo": combo, "applicants": m.group(1),
                                   "date": m.group(2)})
                continue
            if route and "->" in line and "WAITLIST" in line:
                _route_slot(d, route)["waitlist"] += 1
                continue
            if route and "->" in line and "No slot message shown" in line:
                _route_slot(d, route)["none"] += 1
                continue
            if route and "  -> ERROR" in line:
                _route_slot(d, route)["error"] += 1
                continue

            t, o, i = d["turnstile"], d["otp"], d["infra"]
            if "Turnstile SOLVED" in line:
                t["widget"] += 1
            elif "did not auto-pass (login page)" in line:
                t["widget"] += 1
                t["challenged"] += 1
                if port:
                    d["ports"][port]["challenged"] += 1
            elif "Turnstile passed after checkbox click" in line:
                t["click_ok"] += 1
            elif "still not passed after checkbox click" in line:
                t["click_fail"] += 1
            elif "Turnstile NOT solved" in line:
                t["gave_up"] += 1
            elif "Dashboard content rendered" in line:
                t["dashboard"] += 1
            elif "'Verify Captcha' popup present" in line:
                t["dialog"] += 1
            elif "'Verify Captcha' popup SOLVED" in line:
                t["dialog_ok"] += 1
            elif "Egress IP changed for this profile" in line:
                t["cf_dropped"] += 1
            elif "Session Expired or Invalid" in line:
                t["session_expired"] += 1
            elif "OTP read from the email image" in line:
                o["reads"] += 1
            elif "VFS REJECTED the OTP" in line:
                o["rejected"] += 1
            elif "could not produce a usable" in line and "ERROR" in line:
                o["gave_up"] += 1
            elif "entered (text mode)" in line:
                o["text_mode"] += 1
            elif "not striking" in line:
                o["no_strike"] += 1
            elif "Proxy forwarder: upstream" in line:
                i["proxy_err"] += 1
            elif "rotating to a different IP" in line:
                i["rotations"] += 1
            elif "Page-load budget exceeded" in line:
                i["nav_budget"] += 1
            else:
                m = _PAT["denied"].search(line)
                if m:
                    i["denylisted"] += int(m.group(1))
                    continue
                m = _PAT["restrict"].search(line)
                if m:
                    d["restricted"].append({"route": m.group(1), "account": m.group(2)})
                    continue
                m = _PAT["strike"].search(line)
                if m:
                    d["strikes"].append({"account": m.group(1), "n": int(m.group(2)),
                                         "of": int(m.group(3)), "route": m.group(4)})
    return d


_log_cache = {}


def day_stats(day):
    """Parsed aggregates for one day, cached on (mtime, size)."""
    path = os.path.join(LOG_DIR, f"app-{day}.log")
    if not os.path.isfile(path):
        return _blank_day(day)
    st = os.stat(path)
    key = (st.st_mtime_ns, st.st_size)
    hit = _log_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    data = parse_log(path, day)
    _log_cache[path] = (key, data)
    return data


def available_days():
    try:
        names = os.listdir(LOG_DIR)
    except OSError:
        return []
    days = [m.group(1) for m in (_LOG_NAME_RE.match(n) for n in names) if m]
    return sorted(days, reverse=True)


def _summary(d):
    """The handful of numbers the trend chart and the header need."""
    att = d["attempts"]
    total = round(sum(att), 1)
    ok = d["status"].get("OK", 0)
    fail = d["status"].get("FAILED", 0)
    slots = sum(r["slot"] for r in d["routes"].values())
    cf = d["hosts"].get("challenges.cloudflare.com", 0.0)
    return {
        "day": d["day"], "runs": len(d["runs"]), "attempts": len(att),
        "mb": total, "per_attempt": round(total / len(att), 3) if att else 0,
        "ok": ok, "fail": fail,
        "success": round(100.0 * ok / (ok + fail), 1) if (ok + fail) else None,
        "slots": slots, "cf_share": round(100.0 * cf / total, 1) if total else 0,
        "paused": d["status"].get("PAUSED", 0),
        "restricted": d["status"].get("RESTRICTED", 0),
        "first": d["first"][11:16], "last": d["last"][11:16],
    }


def _budget():
    try:
        with open(BUDGET_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def _cap_mb():
    ah = health_module()
    if ah is None:
        return 0
    try:
        from src.settings import settings
        return float(settings().bandwidth.daily_cap_mb)
    except Exception:
        return 0


def _alerts(today, rows, cap, budget):
    """The point of the page: what needs a human, surfaced before any chart."""
    out = []
    ah = health_module()
    # Routes that cannot run because every eligible account is cooling down.
    if ah is not None:
        accounts = [a for a in parse_credentials() if a.get("enabled")]
        for code in route_universe(accounts):
            elig = [a["email"] for a in accounts
                    if not a.get("routes") or code in a["routes"]]
            healthy = [e for e in elig if not ah.is_benched(e, code)]
            if not elig:
                out.append(("crit", f"{code} has no accounts assigned — it can never run."))
            elif not healthy:
                out.append(("crit", f"{code} has 0 healthy accounts "
                                    f"({len(elig)} assigned, all benched) — route is dark."))
            elif len(elig) <= 2:
                out.append(("warn", f"{code} has only {len(elig)} account(s) — "
                                    "one restriction takes the route out."))
    used = float(budget.get("used_mb") or 0)
    if cap and used >= cap:
        out.append(("crit", f"Daily data cap spent ({used:.0f}/{cap:.0f} MB) — routes paused."))
    elif cap and used >= 0.6 * cap:
        out.append(("warn", f"Data at {100 * used / cap:.0f}% of the {cap:.0f} MB cap."))
    s = _summary(today)
    if s["success"] is not None and s["success"] < 85 and (s["ok"] + s["fail"]) >= 20:
        out.append(("warn", f"Success rate {s['success']:.0f}% today — below 85%."))
    if today["otp"]["gave_up"]:
        out.append(("warn", f"{today['otp']['gave_up']} OTP read(s) gave up today."))
    if not out:
        out.append(("ok", "All clear — no routes dark, data within budget, "
                          "success rate healthy."))
    return out


def stats_payload(day=None):
    days = available_days()
    if not days:
        return {"days": [], "error": "No logs found in logs/."}
    day = day if day in days else days[0]
    detail = day_stats(day)
    today = day_stats(days[0])
    trend = [_summary(day_stats(d)) for d in days[:14]][::-1]
    cap = _cap_mb()
    budget = _budget()
    accounts = [a for a in parse_credentials() if a.get("enabled")]
    ah = health_module()
    n_avail = 0
    if ah is not None:
        n_avail = sum(1 for a in accounts if not ah.is_benched(a["email"]))
    else:
        n_avail = len(accounts)
    pool = {}
    for code in route_universe(accounts):
        elig = [a["email"] for a in accounts
                if not a.get("routes") or code in a["routes"]]
        healthy = ([e for e in elig if not ah.is_benched(e, code)]
                   if ah is not None else elig)
        pool[code] = {"eligible": len(elig), "healthy": len(healthy)}
    return {
        "days": days, "day": day, "detail": detail, "summary": _summary(detail),
        "today": _summary(today), "trend": trend, "pool": pool,
        "accounts_total": len(accounts), "accounts_available": n_avail,
        "cap": cap, "budget": budget,
        "alerts": _alerts(today, trend, cap, budget),
        "names": COUNTRY_NAMES,
    }


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

# --------------------------------------------------------------------------- #
# Shared page navigation                                                      #
#                                                                             #
# Defined ONCE and substituted into every page at serve time (each page holds #
# a <!--NAV--> marker), so a new page can never end up unreachable because a  #
# link was added to two of the three templates. Carries its own scoped style  #
# block, light and dark, because the three pages don't share a stylesheet.    #
# --------------------------------------------------------------------------- #

NAV_ITEMS = (
    ("/", "Accounts &amp; Routes"),
    ("/account-health", "Account Health"),
    ("/stats", "Stats"),
)

_NAV_CSS = """<style>
.vnav { display:flex; gap:0; margin:0 0 18px; flex-wrap:wrap;
        border:1px solid #cdd2d8; border-radius:8px; overflow:hidden; width:max-content;
        box-shadow:0 1px 2px rgba(0,0,0,.06); }
.vnav a { font:600 13px/1 system-ui,sans-serif; padding:10px 18px; text-decoration:none;
          color:#2563eb; background:#fff; border-right:1px solid #cdd2d8;
          display:inline-block; }
.vnav a:last-child { border-right:none; }
.vnav a:hover { background:#eef2ff; }
.vnav a.on { background:#2563eb; color:#fff; cursor:default; }
.vnav a.on:hover { background:#2563eb; }
@media (prefers-color-scheme: dark) {
  .vnav { border-color:#3a3f47; }
  .vnav a { background:#23272f; color:#7aa7ff; border-right-color:#3a3f47; }
  .vnav a:hover { background:#2c313a; }
  .vnav a.on { background:#2563eb; color:#fff; }
}
</style>"""


def nav_html(active: str) -> str:
    """The button bar, with `active` (a path) rendered as the current page."""
    links = "".join(
        f'<a href="{href}"{" class=\'on\'" if href == active else ""}>{label}</a>'
        for href, label in NAV_ITEMS
    )
    return f'{_NAV_CSS}<nav class="vnav">{links}</nav>'



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
            self._send(200, PAGE.replace("<!--NAV-->", nav_html("/")),
                       "text/html; charset=utf-8")
        elif self.path in ("/account-health", "/account-health/"):
            self._send(200, PAGE_HEALTH.replace(
                "<!--NAV-->", nav_html("/account-health")),
                       "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self._send(200, json.dumps(health_payload()))
        elif self.path in ("/stats", "/stats/"):
            self._send(200, PAGE_STATS.replace("<!--NAV-->", nav_html("/stats")),
                       "text/html; charset=utf-8")
        elif self.path.startswith("/api/stats"):
            q = urllib.parse.urlparse(self.path).query
            day = urllib.parse.parse_qs(q).get("day", [None])[0]
            try:
                payload = stats_payload(day)
            except Exception as e:                       # never 500 the page
                payload = {"days": [], "error": f"{type(e).__name__}: {e}"}
            self._send(200, json.dumps(payload))
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
        elif self.path == "/api/health":
            try:
                ok, message = health_action(data)
            except Exception as e:                       # never 500 the page
                ok, message = False, f"{type(e).__name__}: {e}"
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
  <!--NAV-->

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


PAGE_HEALTH = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Account Health</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font: 14px/1.4 system-ui, sans-serif; margin: 0; padding: 24px;
         background: #f5f6f8; color: #1a1a1a; }
  @media (prefers-color-scheme: dark) {
    body { background: #16181d; color: #e8e8e8; }
    .panel, thead th, .statusbar { background: #1f232b !important; }
    tbody tr:nth-child(even) { background: #1b1e24; }
    input { background: #14161a; color: #e8e8e8; border-color: #3a3f47 !important; }
    tr.drawer > td { background: #14171c !important; }
    td, th { border-color: #2b3038 !important; }
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .nav { margin: 0 0 16px; font-size: 13px; }
  .nav a { color: #2563eb; text-decoration: none; margin-right: 14px; }
  .nav a.here { color: inherit; font-weight: 600; text-decoration: none; }
  .sub { color: #888; font-size: 13px; }
  .statusbar { background: #fff; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
               padding: 14px 18px; margin-bottom: 14px; display: flex; flex-wrap: wrap;
               align-items: center; gap: 10px 18px; }
  .counts { display: flex; flex-wrap: wrap; gap: 8px; }
  .badge { font-weight: 700; padding: 3px 10px; border-radius: 999px; font-size: 12px;
           background: #6b7280; color: #fff; white-space: nowrap; }
  .badge.healthy { background: #16a34a; } .badge.strikes { background: #b45309; }
  .badge.benched { background: #d97706; } .badge.disabled { background: #dc2626; }
  .badge.off { background: #9ca3af; }
  .rules { color: #888; font-size: 12px; }
  .sbtns { display: flex; gap: 8px; align-items: center; margin-left: auto; }
  button { font: inherit; cursor: pointer; border: 1px solid #cdd2d8; background: #fff;
           border-radius: 6px; padding: 5px 9px; }
  button:hover { background: #f0f0f0; }
  @media (prefers-color-scheme: dark) {
    button { background: #23272f; color: #e8e8e8; border-color: #3a3f47; }
    button:hover { background: #2c313a; }
  }
  button.danger { color: #c0392b; } button.primary { background: #2563eb; color: #fff;
           border-color: #2563eb; font-weight: 600; }
  button.primary:hover { background: #1d4ed8; }
  .panel { background: #fff; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
           margin-bottom: 18px; overflow: hidden; }
  .body { padding: 16px; }
  .filters { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
             margin-bottom: 12px; }
  input[type=text] { padding: 6px 8px; border: 1px solid #cdd2d8; border-radius: 6px;
                     font: inherit; }
  .chip { border: 1px solid transparent; padding: 3px 10px; border-radius: 999px;
          font-size: 12px; background: #eef0f3; cursor: pointer; user-select: none; }
  .chip.on { border-color: #2563eb; font-weight: 600; }
  table { border-collapse: collapse; width: 100%; }
  th, td { padding: 8px 10px; text-align: left; vertical-align: top;
           border-bottom: 1px solid #e2e4e8; }
  thead th { background: #eef0f3; font-size: 12px; text-transform: uppercase;
             letter-spacing: .04em; color: #666; }
  .mono { font-variant-numeric: tabular-nums;
          font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; }
  .em { color: #888; }
  tr.acct { cursor: pointer; }
  tr.acct:hover > td { background: rgba(37,99,235,.06); }
  .caret { display: inline-block; width: 1em; color: #888; }
  td .badge { vertical-align: middle; }
  tr.acct > td:first-child { white-space: nowrap; }
  .rt { display: inline-block; padding: 1px 6px; border-radius: 5px; background: #eef0f3;
        font-size: 11.5px; margin: 1px 3px 1px 0; white-space: nowrap; }
  .rt.ok { color: #15803d; } .rt.warn { color: #b45309; font-weight: 600; }
  .rt.bench { background: #dc2626; color: #fff; font-weight: 600; }
  .rt.un { opacity: .55; font-style: italic; }
  .ops { display: flex; gap: 5px; flex-wrap: nowrap; align-items: center; }
  .ops button { padding: 4px 8px; white-space: nowrap; }
  .ops select { font: inherit; padding: 3px 4px; border-radius: 6px;
                border: 1px solid #cdd2d8; }
  @media (prefers-color-scheme: dark) { .ops select { background: #23272f; color: #e8e8e8;
                border-color: #3a3f47; } }
  tr.drawer > td { background: #fafbfc; padding: 0 10px 14px 40px; }
  tr.drawer table { width: auto; min-width: 520px; margin-top: 6px; }
  tr.drawer th, tr.drawer td { padding: 5px 12px; font-size: 12.5px; }
  .reason { color: #888; font-size: 12px; max-width: 460px; overflow: hidden;
            text-overflow: ellipsis; white-space: nowrap; display: inline-block;
            vertical-align: bottom; }
  .st { margin-left: auto; font-weight: 500; }
  .ok { color: #16a34a; } .err { color: #dc2626; }
  .addrow { display: flex; gap: 8px; align-items: center; margin-top: 14px;
            flex-wrap: wrap; }
  .warnbox { background: #fef3c7; color: #92400e; border-radius: 8px; padding: 12px 16px;
             margin-bottom: 14px; font-size: 13px; }
  @media (prefers-color-scheme: dark) { .warnbox { background: #3a2f12; color: #fcd34d; }
    tr.drawer > td { background: #14171c; } }
  /* Dark overrides for elements defined ABOVE — these must come last, or the
     base light rules win on source order and the text goes invisible. */
  @media (prefers-color-scheme: dark) {
    .chip { background: #262b33; color: #d5dae2; }
    .chip.on { border-color: #60a5fa; color: #fff; }
    .rt { background: #262b33; color: #cbd2dc; }
    .rt.ok { color: #4ade80; } .rt.warn { color: #fbbf24; }
    .rt.bench { background: #dc2626; color: #fff; }
    thead th { color: #9aa4b2; }
    tr.acct:hover > td { background: rgba(96,165,250,.10); }
  }
</style></head>
<body>
  <h1>Account Health</h1>
  <!--NAV-->
  <p class="sub" style="margin:-10px 0 16px">Live circuit-breaker state from
    <code>account_health.json</code></p>

  <div class="statusbar">
    <div class="counts" id="counts"></div>
    <span class="rules" id="rules"></span>
    <div class="sbtns">
      <button onclick="load()" title="Refresh now">&#8635;</button>
      <label class="sub" style="display:flex;gap:5px;align-items:center;cursor:pointer">
        <input type="checkbox" id="auto" checked onchange="setAuto()"> auto 15s
      </label>
      <button class="danger" onclick="clearAll()">Clear all cooldowns</button>
      <span class="st" id="status"></span>
    </div>
  </div>

  <div class="panel"><div class="body">
    <div class="filters">
      <input type="text" id="q" placeholder="Search email or route…" oninput="render()"
             style="min-width:220px">
      <span class="chip on" data-s="all"    onclick="pick(this)">All</span>
      <span class="chip"    data-s="healthy" onclick="pick(this)">&#9679; Healthy</span>
      <span class="chip"    data-s="strikes" onclick="pick(this)">&#9650; Strikes</span>
      <span class="chip"    data-s="benched" onclick="pick(this)">&#9203; Benched</span>
      <span class="chip"    data-s="disabled" onclick="pick(this)">&#10006; Disabled</span>
      <span class="chip"    data-s="off"     onclick="pick(this)">&#9675; Not in pool</span>
    </div>
    <table>
      <thead><tr>
        <th style="width:142px">State</th>
        <th>Account</th>
        <th>Route health</th>
        <th style="width:74px">Strikes</th>
        <th style="width:96px">Free in</th>
        <th style="width:330px">Actions</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
    <div class="addrow">
      <input type="text" id="ne" placeholder="new account email" style="min-width:230px">
      <input type="text" id="np" placeholder="password" style="min-width:150px">
      <button class="primary" onclick="addAcct()">+ Add account</button>
      <span class="sub">writes to <code>config/credentials.local.ini</code> (backed up first)</span>
    </div>
  </div></div>

  <div id="orphanwrap"></div>

<script>
let DATA = null, FILTER = 'all', OPEN = new Set(), TIMER = null;

const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function dur(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + 's';
  const m = Math.round(sec / 60);
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60);
  return h + 'h ' + String(m % 60).padStart(2, '0') + 'm';
}
function when(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleString([], { month: 'short', day: '2-digit',
                                hour: '2-digit', minute: '2-digit' });
}

function say(msg, ok) {
  const el = document.getElementById('status');
  el.textContent = msg;
  el.className = 'st ' + (ok ? 'ok' : 'err');
  if (ok) setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 4000);
}

async function load() {
  const r = await fetch('/api/health');
  DATA = await r.json();
  render();
}

function setAuto() {
  if (TIMER) { clearInterval(TIMER); TIMER = null; }
  if (document.getElementById('auto').checked) {
    // Pause while a drawer is open so a refresh can't yank the row being read.
    TIMER = setInterval(() => { if (OPEN.size === 0) load(); }, 15000);
  }
}

function pick(el) {
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('on'));
  el.classList.add('on');
  FILTER = el.dataset.s;
  render();
}

const LABEL = { healthy: 'Healthy', strikes: 'Strikes', benched: 'Benched',
                disabled: 'Disabled', off: 'Not in pool' };

function render() {
  if (!DATA) return;
  if (!DATA.available) {
    document.getElementById('rows').innerHTML =
      '<tr><td colspan="6" class="err">Account health unavailable — ' +
      esc(DATA.error) + '</td></tr>';
    return;
  }
  const now = Date.now() / 1000;
  const counts = {};
  DATA.accounts.forEach(a => counts[a.state] = (counts[a.state] || 0) + 1);
  document.getElementById('counts').innerHTML =
    ['healthy', 'strikes', 'benched', 'disabled', 'off']
      .map(s => `<span class="badge ${s}">${counts[s] || 0} ${LABEL[s]}</span>`).join('');

  const next = DATA.accounts.filter(a => a.until > now)
                            .sort((x, y) => x.until - y.until)[0];
  const R = DATA.rules;
  document.getElementById('rules').textContent =
    `${R.fail_threshold} strikes → ${R.soft_hours}h bench · hard block ${R.hard_hours}h`
    + (next ? ` · next free ${when(next.until)} (${next.email.split('@')[0]})` : '');

  const q = document.getElementById('q').value.trim().toLowerCase();
  const out = [];
  DATA.accounts.forEach(a => {
    if (FILTER !== 'all' && a.state !== FILTER) return;
    if (q && !a.email.toLowerCase().includes(q) &&
        !a.routes.some(r => r.route.toLowerCase().includes(q))) return;
    const open = OPEN.has(a.email);
    const chips = a.routes.map(r => {
      const cls = r.benched ? 'bench' : (r.strikes ? 'warn' : 'ok');
      const txt = r.route.replace(/^AE-/, '')
        + (r.benched ? ' ' + dur(r.until - now) : (r.strikes ? ' ' + r.strikes : ''));
      return `<span class="rt ${cls}${r.unassigned ? ' un' : ''}" title="${esc(r.route)}${
        r.reason ? ' — ' + esc(r.reason) : ''}">${esc(txt)}</span>`;
    }).join('');
    out.push(`<tr class="acct" onclick="toggle('${esc(a.email)}')">
      <td><span class="caret">${open ? '&#9662;' : '&#9656;'}</span>
          <span class="badge ${a.state}">${LABEL[a.state]}</span></td>
      <td><b>${esc(a.email)}</b>${a.disabled_reason
            ? `<div class="reason">${esc(a.disabled_reason)}</div>` : ''}</td>
      <td>${chips || '<span class="em">no routes</span>'}</td>
      <td class="mono">${a.strikes ? a.strikes + '/' + R.fail_threshold
                                   : '<span class="em">0</span>'}</td>
      <td class="mono">${a.state === 'disabled' ? '<span class="em">never</span>'
            : (a.until > now ? dur(a.until - now) : '<span class="em">—</span>')}</td>
      <td onclick="event.stopPropagation()">${actions(a)}</td>
    </tr>`);
    if (open) out.push(drawer(a, now));
  });
  document.getElementById('rows').innerHTML = out.join('')
    || '<tr><td colspan="6" class="em">No accounts match.</td></tr>';

  const orp = DATA.orphans || [];
  document.getElementById('orphanwrap').innerHTML = orp.length
    ? `<div class="warnbox"><b>${orp.length} health record(s) with no credential</b> —
       ${orp.map(o => esc(o.email)).join(', ')}.
       <button onclick="act('clear','${esc(orp[0].email)}')">Clear first</button></div>`
    : '';
}

function actions(a) {
  const e = esc(a.email);
  const bench = `<select onchange="benchIt('${e}', this)">
      <option value="">Bench…</option><option value="1">1 hour</option>
      <option value="2">2 hours</option><option value="12">12 hours</option>
      <option value="24">24 hours</option></select>`;
  const rot = a.enabled
    ? `<button onclick="confirmIt(this,'toggle','${e}','Take out of rotation?')">Out</button>`
    : `<button onclick="act('toggle','${e}')">In</button>`;
  const main = a.state === 'disabled'
    ? `<button class="primary" onclick="act('enable','${e}')">Enable</button>`
    : `<button onclick="act('clear','${e}')">Clear</button>`;
  const dis = a.state === 'disabled' ? ''
    : `<button class="danger" onclick="confirmIt(this,'disable','${e}','Disable indefinitely?')">Disable</button>`;
  return `<div class="ops">${main}${bench}${dis}${rot}
    <button class="danger"
      onclick="confirmIt(this,'remove','${e}','Delete this account?')">Remove</button></div>`;
}

function drawer(a, now) {
  const rows = a.routes.map(r => `<tr>
      <td class="mono">${esc(r.route)}${r.unassigned
          ? ' <span class="em">(unassigned)</span>' : ''}</td>
      <td>${r.benched ? `<span class="badge benched">${dur(r.until - now)}</span>`
                      : (r.strikes ? '<span class="badge strikes">strikes</span>'
                                   : '<span class="badge healthy">ok</span>')}</td>
      <td class="mono">${r.strikes}/${DATA.rules.fail_threshold}</td>
      <td class="mono em">${when(r.updated)}</td>
      <td><span class="reason" title="${esc(r.reason)}">${esc(r.reason) || '—'}</span></td>
      <td><button onclick="act('clear','${esc(a.email)}','${esc(r.route)}')">Clear</button>
          <button onclick="benchOne('${esc(a.email)}','${esc(r.route)}')">Bench 2h</button></td>
    </tr>`).join('');
  return `<tr class="drawer"><td colspan="6">
    <table><thead><tr><th>Route</th><th>State</th><th>Strikes</th><th>Updated</th>
      <th>Last reason</th><th></th></tr></thead><tbody>${rows
        || '<tr><td colspan="6" class="em">No route records.</td></tr>'}</tbody></table>
  </td></tr>`;
}

function toggle(email) {
  if (OPEN.has(email)) OPEN.delete(email); else OPEN.add(email);
  render();
}

async function post(body) {
  const r = await fetch('/api/health', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const j = await r.json();
  say(j.message, j.ok);
  await load();
}

const act = (action, email, route) => post({ action, email, route });

function benchIt(email, sel) {
  const h = sel.value; sel.value = '';
  if (h) post({ action: 'bench', email, hours: Number(h) });
}
const benchOne = (email, route) => post({ action: 'bench', email, route, hours: 2 });

/* Inline confirm — destructive actions only, no modals. */
function confirmIt(btn, action, email, question) {
  const cell = btn.parentNode;
  const prev = cell.innerHTML;
  cell.innerHTML = `<span class="sub">${esc(question)}</span>
    <button class="danger" id="yes">Yes</button><button id="no">No</button>`;
  cell.querySelector('#yes').onclick = () => post({ action, email });
  cell.querySelector('#no').onclick = () => { cell.innerHTML = prev; };
}

function clearAll() {
  if (!confirm('Clear cooldowns, strikes and disables for EVERY account?')) return;
  post({ action: 'clear_all' });
}

function addAcct() {
  const email = document.getElementById('ne').value.trim();
  const password = document.getElementById('np').value.trim();
  if (!email) { say('Email is required.', false); return; }
  document.getElementById('ne').value = '';
  document.getElementById('np').value = '';
  post({ action: 'add', email, password });
}

load();
setAuto();
</script>
</body></html>
"""

PAGE_STATS = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VFS Stats</title>
<style>
  :root { color-scheme: light dark; --line:#e2e4e8; --card:#fff; --mut:#888;
          --ink:#1a1a1a; --sunk:#eef0f3; --acc:#2563eb;
          --ok:#16a34a; --warn:#b45309; --crit:#dc2626; }
  * { box-sizing: border-box; }
  body { font: 14px/1.45 system-ui, sans-serif; margin: 0; padding: 24px;
         background: #f5f6f8; color: var(--ink); }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .nav { margin: 0 0 16px; font-size: 13px; }
  .nav a { color: var(--acc); text-decoration: none; margin-right: 14px; }
  .nav a.here { color: inherit; font-weight: 600; }
  .sub { color: var(--mut); font-size: 13px; }
  .card { background: var(--card); border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
          margin-bottom: 16px; overflow: hidden; }
  .card > h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .05em;
               color: var(--mut); margin: 0; padding: 12px 18px; background: var(--sunk);
               font-weight: 600; }
  .card > .in { padding: 16px 18px; }
  .grid { display: grid; gap: 16px; grid-template-columns: 1fr 1fr; }
  @media (max-width: 1080px) { .grid { grid-template-columns: 1fr; } }
  /* alerts */
  .alert { display: flex; gap: 10px; align-items: flex-start; padding: 9px 14px;
           border-radius: 8px; margin-bottom: 6px; font-size: 13.5px; }
  .alert.ok   { background: #dcfce7; color: #14532d; }
  .alert.warn { background: #fef3c7; color: #7c2d12; }
  .alert.crit { background: #fee2e2; color: #7f1d1d; font-weight: 600; }
  /* KPI strip */
  .kpis { display: grid; gap: 1px; background: var(--line);
          grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
          margin-bottom: 16px; }
  .kpi { background: var(--card); padding: 13px 16px; }
  .kpi .k { font-size: 10.5px; letter-spacing: .09em; text-transform: uppercase;
            color: var(--mut); display: block; margin-bottom: 6px; }
  .kpi .v { font-size: 24px; font-weight: 650; line-height: 1;
            font-variant-numeric: tabular-nums; }
  .kpi .v em { font-style: normal; font-size: 12px; color: var(--mut); margin-left: 3px;
               font-weight: 500; }
  .kpi .s { font-size: 12px; color: var(--mut); display: block; margin-top: 6px; }
  .kpi.good .v { color: var(--ok); } .kpi.bad .v { color: var(--crit); }
  .kpi.warn .v { color: var(--warn); }
  /* tables */
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid var(--line); }
  th { font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
       color: var(--mut); font-weight: 600; }
  td.n, th.n { text-align: right; font-variant-numeric: tabular-nums;
               font-family: ui-monospace, Consolas, monospace; }
  tbody tr:last-child td { border-bottom: none; }
  .bar { height: 7px; border-radius: 4px; background: var(--acc); display: block; }
  .bar.g { background: var(--ok); } .bar.r { background: var(--crit); }
  .bar.a { background: var(--warn); }
  .barcell { min-width: 90px; }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11.5px;
          font-weight: 600; color: #fff; background: #6b7280; }
  .pill.ok { background: var(--ok); } .pill.warn { background: var(--warn); }
  .pill.crit { background: var(--crit); } .pill.mut { background: #9ca3af; }
  .muted { color: var(--mut); }
  svg { display: block; width: 100%; height: auto; overflow: visible; }
  .gridline { stroke: var(--line); }
  .axl { fill: var(--mut); font-size: 9px;
         font-family: ui-monospace, Consolas, monospace; }
  .toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
             margin-bottom: 14px; }
  select, button { font: inherit; padding: 5px 9px; border-radius: 6px;
                   border: 1px solid #cdd2d8; background: #fff; cursor: pointer; }
  .legend { font-size: 11.5px; color: var(--mut); display: flex; gap: 14px;
            flex-wrap: wrap; margin-bottom: 8px; }
  .sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
        margin-right: 5px; vertical-align: -1px; }
  @media (prefers-color-scheme: dark) {
    :root { --line:#2b3038; --card:#1f232b; --mut:#8b949e; --ink:#e8e8e8;
            --sunk:#23272f; }
    body { background: #16181d; }
    select, button { background: #23272f; color: #e8e8e8; border-color: #3a3f47; }
    .alert.ok   { background: #14321f; color: #86efac; }
    .alert.warn { background: #3a2f12; color: #fcd34d; }
    .alert.crit { background: #3f1414; color: #fca5a5; }
  }
</style></head>
<body>
  <h1>VFS Stats</h1>
  <!--NAV-->
  <p class="sub" style="margin:-10px 0 16px">Derived from <code>logs/</code>,
    <code>account_health.json</code> and <code>bandwidth_budget.json</code></p>

  <div id="alerts"></div>
  <div class="kpis" id="kpis"></div>

  <div class="toolbar">
    <label class="sub">Day</label>
    <select id="day" onchange="load(this.value)"></select>
    <span class="sub" id="span"></span>
    <button onclick="load(document.getElementById('day').value)">&#8635; Refresh</button>
    <span class="sub" id="upd"></span>
  </div>

  <div class="card"><h2>Slots found — the reason the bot runs</h2>
    <div class="in"><div id="slots"></div></div></div>

  <div class="grid">
    <div class="card"><h2>Data usage — last 14 days</h2><div class="in">
      <div class="legend">
        <span><i class="sw" style="background:var(--acc)"></i>MB billed</span>
        <span><i class="sw" style="background:var(--crit)"></i>over cap</span>
        <span><i class="sw" style="background:var(--ok)"></i>MB per attempt</span>
      </div>
      <div id="chartMb"></div></div></div>
    <div class="card"><h2>Where the data went</h2><div class="in">
      <div id="hosts"></div></div></div>
  </div>

  <div class="grid">
    <div class="card"><h2>Reliability — last 14 days</h2><div class="in">
      <div class="legend">
        <span><i class="sw" style="background:var(--ok)"></i>route success %</span>
        <span><i class="sw" style="background:var(--warn)"></i>slots found</span>
      </div>
      <div id="chartOk"></div></div></div>
    <div class="card"><h2>Why routes failed</h2><div class="in">
      <div id="fails"></div></div></div>
  </div>

  <div class="card"><h2>Route matrix — traffic, reliability and account supply</h2>
    <div class="in"><div id="matrix"></div></div></div>

  <div class="grid">
    <div class="card"><h2>Cloudflare &amp; Turnstile</h2><div class="in">
      <div id="turnstile"></div></div></div>
    <div class="card"><h2>OTP &amp; infrastructure</h2><div class="in">
      <div id="otp"></div></div></div>
  </div>

  <div class="card"><h2>Hour by hour</h2><div class="in"><div id="hourly"></div></div></div>

<script>
let D = null;
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const n1 = v => (Math.round(v * 10) / 10).toLocaleString();
const pct = (a, b) => b ? Math.round(100 * a / b) : 0;

async function load(day) {
  const r = await fetch('/api/stats' + (day ? '?day=' + encodeURIComponent(day) : ''));
  D = await r.json();
  draw();
}

function draw() {
  if (!D || D.error) {
    document.getElementById('alerts').innerHTML =
      `<div class="alert crit">${esc((D && D.error) || 'No data')}</div>`;
    return;
  }
  const d = D.detail, s = D.summary, t = D.today;

  document.getElementById('alerts').innerHTML = D.alerts
    .map(([k, m]) => `<div class="alert ${k}">${esc(m)}</div>`).join('');

  const sel = document.getElementById('day');
  if (sel.options.length !== D.days.length) {
    sel.innerHTML = D.days.map(x => `<option value="${x}">${x}</option>`).join('');
  }
  sel.value = D.day;
  document.getElementById('span').textContent =
    `${s.first || '–'} → ${s.last || '–'} · ${s.runs} runs`;
  document.getElementById('upd').textContent = 'updated ' + new Date().toLocaleTimeString();

  /* ---------- KPI strip: today, not the selected day ---------- */
  const used = (D.budget && D.budget.used_mb) || 0, cap = D.cap || 0;
  const capCls = cap && used >= cap ? 'bad' : (cap && used >= .6 * cap ? 'warn' : 'good');
  const proj = t.runs ? used + (t.mb / Math.max(t.runs, 1)) *
                 Math.max(0, Math.round((24 - new Date().getHours()) * 2)) : used;
  document.getElementById('kpis').innerHTML = [
    kpi(t.slots, '', 'Slots found today', topSlotRoutes(), t.slots ? 'good' : ''),
    kpi(n1(used), 'MB', 'Data used today',
        cap ? `${pct(used, cap)}% of ${cap} MB cap · ~${n1(proj)} projected` : 'no cap set',
        capCls),
    kpi(t.per_attempt.toFixed(2), 'MB', 'Per route-attempt',
        `${t.attempts} attempts in ${t.runs} runs`),
    kpi(t.success == null ? '–' : t.success.toFixed(0) + '%', '', 'Route success today',
        `${t.ok} ok · ${t.fail} failed`,
        t.success == null ? '' : (t.success >= 92 ? 'good' : t.success >= 85 ? 'warn' : 'bad')),
    kpi(D.accounts_available, '', 'Accounts available', poolLine(), poolCls()),
    kpi(t.last || '–', '', 'Last activity', `${t.paused} paused · ${t.restricted} restricted`),
  ].join('');

  drawSlots(d);
  drawMbChart();
  drawHosts(d, s);
  drawOkChart();
  drawFails(d);
  drawMatrix(d);
  drawTurnstile(d);
  drawOtp(d);
  drawHourly(d);
}

function kpi(v, unit, k, sub, cls) {
  return `<div class="kpi ${cls || ''}"><span class="k">${esc(k)}</span>
    <span class="v">${esc(v)}${unit ? `<em>${unit}</em>` : ''}</span>
    <span class="s">${esc(sub || '')}</span></div>`;
}
function topSlotRoutes() {
  const r = Object.entries(D.detail.routes).filter(([, v]) => v.slot)
    .sort((a, b) => b[1].slot - a[1].slot).slice(0, 3)
    .map(([k, v]) => k.replace('AE-', '') + ' ' + v.slot);
  return r.length ? r.join(' · ') : 'none today';
}
function poolLine() {
  const dark = Object.entries(D.pool).filter(([, p]) => p.eligible && !p.healthy).length;
  const thin = Object.entries(D.pool)
    .filter(([, p]) => p.healthy && p.healthy <= 1).length;
  return `of ${D.accounts_total} enabled`
    + (dark ? ` · ${dark} route(s) dark` : '')
    + (thin ? ` · ${thin} on the last account` : '');
}
function poolCls() {
  return Object.entries(D.pool).some(([, p]) => p.eligible && !p.healthy) ? 'bad' : 'good';
}

/* ---------- slots ---------- */
function drawSlots(d) {
  const rows = Object.entries(d.routes)
    .map(([k, v]) => ({ k, ...v }))
    .filter(r => r.slot || r.waitlist || r.none || r.error)
    .sort((a, b) => b.slot - a.slot || b.waitlist - a.waitlist);
  if (!rows.length) {
    document.getElementById('slots').innerHTML =
      '<p class="muted">No slot checks completed on this day.</p>'; return;
  }
  const max = Math.max(1, ...rows.map(r => r.slot + r.waitlist + r.none + r.error));
  const body = rows.map(r => {
    const tot = r.slot + r.waitlist + r.none + r.error;
    const seg = (v, c) => v ? `<i class="bar ${c}" style="display:inline-block;
       width:${100 * v / max}%;vertical-align:middle"></i>` : '';
    return `<tr>
      <td><b>${esc(r.k)}</b> <span class="muted">${esc(D.names[r.k.split('-')[1]] || '')}</span></td>
      <td class="n">${r.slot ? `<span class="pill ok">${r.slot}</span>` : '<span class="muted">0</span>'}</td>
      <td class="n">${r.waitlist || '<span class="muted">–</span>'}</td>
      <td class="n muted">${r.none || '–'}</td>
      <td class="n">${r.error ? `<span class="pill crit">${r.error}</span>` : '<span class="muted">–</span>'}</td>
      <td class="barcell">${seg(r.slot, 'g')}${seg(r.waitlist, 'a')}${seg(r.none, '')}${seg(r.error, 'r')}</td>
      <td class="muted">${esc((r.dates || []).slice(0, 4).join(', ')) || '–'}</td></tr>`;
  }).join('');
  const recent = (d.slots || []).slice(-8).reverse().map(x =>
    `<tr><td class="n muted">${esc(x.at)}</td><td><b>${esc(x.route)}</b></td>
     <td>${esc(x.combo)}</td><td class="n">${esc(x.applicants)} appl.</td>
     <td class="n"><span class="pill ok">${esc(x.date)}</span></td></tr>`).join('');
  document.getElementById('slots').innerHTML = `
    <table><thead><tr><th>Route</th><th class="n">Slots</th><th class="n">Waitlist</th>
      <th class="n">No slot</th><th class="n">Errors</th><th>Mix</th>
      <th>Earliest dates seen</th></tr></thead><tbody>${body}</tbody></table>
    ${recent ? `<h3 style="font-size:12px;text-transform:uppercase;color:var(--mut);
       letter-spacing:.05em;margin:18px 0 6px">Most recent finds</h3>
      <table><tbody>${recent}</tbody></table>` : ''}`;
}

/* ---------- charts ---------- */
function bars(el, series, opts) {
  const W = 720, H = 170, ml = 42, mr = 34, mt = 10, mb = 26;
  const pw = W - ml - mr, ph = H - mt - mb, n = series.length || 1, bw = pw / n;
  const maxL = Math.max(...series.map(s => s.left), opts.capLine || 0, 1);
  const maxR = Math.max(...series.map(s => s.right || 0), 1);
  let g = '';
  for (let i = 0; i <= 3; i++) {
    const y = mt + ph - ph * i / 3;
    g += `<line class="gridline" x1="${ml}" y1="${y}" x2="${ml + pw}" y2="${y}"/>
          <text class="axl" x="${ml - 6}" y="${y + 3}" text-anchor="end">${
            Math.round(maxL * i / 3)}</text>
          <text class="axl" x="${ml + pw + 6}" y="${y + 3}" fill="currentColor"
            opacity=".55">${(maxR * i / 3).toFixed(opts.rightDp || 0)}</text>`;
  }
  if (opts.capLine) {
    const y = mt + ph - ph * opts.capLine / maxL;
    g += `<line x1="${ml}" y1="${y}" x2="${ml + pw}" y2="${y}" stroke="var(--crit)"
       stroke-dasharray="4 3" opacity=".8"/>
       <text class="axl" x="${ml + pw}" y="${y - 4}" text-anchor="end"
         fill="var(--crit)">cap ${opts.capLine}</text>`;
  }
  series.forEach((s, i) => {
    const h = ph * s.left / maxL, x = ml + bw * i;
    const over = opts.capLine && s.left > opts.capLine;
    g += `<rect x="${x + bw * .18}" y="${mt + ph - h}" width="${bw * .64}" height="${h}"
       rx="2" fill="${over ? 'var(--crit)' : 'var(--acc)'}"><title>${esc(s.label)} — ${
       n1(s.left)}${opts.leftUnit || ''}</title></rect>`;
    if (i % 2 === 0 || n <= 8) {
      g += `<text class="axl" x="${x + bw / 2}" y="${mt + ph + 14}"
        text-anchor="middle">${esc(s.label)}</text>`;
    }
  });
  if (series.some(s => s.right != null)) {
    const pts = series.map((s, i) =>
      `${ml + bw * i + bw / 2},${mt + ph - ph * (s.right || 0) / maxR}`).join(' ');
    g += `<polyline points="${pts}" fill="none" stroke="var(--ok)" stroke-width="2"/>`;
    series.forEach((s, i) => {
      g += `<circle cx="${ml + bw * i + bw / 2}"
        cy="${mt + ph - ph * (s.right || 0) / maxR}" r="2.6" fill="var(--ok)">
        <title>${esc(s.label)} — ${s.right}${opts.rightUnit || ''}</title></circle>`;
    });
  }
  document.getElementById(el).innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" role="img">${g}</svg>`;
}

const drawMbChart = () => bars('chartMb', D.trend.map(x => ({
  label: x.day.slice(5), left: x.mb, right: x.per_attempt })),
  { capLine: D.cap || 0, leftUnit: ' MB', rightUnit: ' MB/att', rightDp: 2 });

const drawOkChart = () => bars('chartOk', D.trend.map(x => ({
  label: x.day.slice(5), left: x.slots, right: x.success || 0 })),
  { leftUnit: ' slots', rightUnit: '%' });

function drawHosts(d, s) {
  const hs = Object.entries(d.hosts).sort((a, b) => b[1] - a[1]);
  const tot = s.mb || 1;
  const named = hs.reduce((a, [, v]) => a + v, 0);
  const rows = hs.filter(([, v]) => v >= .05).slice(0, 8).map(([h, v]) =>
    `<tr><td>${esc(h)}</td><td class="n">${n1(v)}</td>
     <td class="n">${pct(v, tot)}%</td>
     <td class="barcell"><i class="bar" style="width:${100 * v / (hs[0][1] || 1)}%"></i></td></tr>`
  ).join('');
  const tail = tot - named;
  document.getElementById('hosts').innerHTML = hs.length ? `
    <table><thead><tr><th>Host</th><th class="n">MB</th><th class="n">Share</th>
      <th></th></tr></thead><tbody>${rows}
      <tr><td class="muted">tail beyond top-6 logged per route</td>
        <td class="n muted">${n1(tail)}</td><td class="n muted">${pct(tail, tot)}%</td>
        <td></td></tr></tbody></table>
    <p class="sub" style="margin:10px 0 0">Cloudflare is the Turnstile widget, re-downloaded
      on every login page load — it does not cache. Reducing it means loading fewer
      login pages, not solving challenges faster.</p>` :
    '<p class="muted">No per-host data for this day.</p>';
}

function drawFails(d) {
  const f = Object.entries(d.fail_reason).sort((a, b) => b[1] - a[1]);
  if (!f.length) {
    document.getElementById('fails').innerHTML =
      '<p class="muted">No route failures on this day. 🎉</p>'; return;
  }
  const max = f[0][1];
  document.getElementById('fails').innerHTML = `<table><tbody>${f.map(([r, c]) =>
    `<tr><td class="n"><span class="pill ${c >= max ? 'crit' : 'warn'}">${c}</span></td>
     <td>${esc(r)}</td>
     <td class="barcell"><i class="bar r" style="width:${100 * c / max}%"></i></td></tr>`
  ).join('')}</tbody></table>`;
}

function drawMatrix(d) {
  const rows = Object.entries(d.routes).map(([k, v]) => ({ k, ...v,
    pool: D.pool[k] || { eligible: 0, healthy: 0 } }))
    .sort((a, b) => b.mb - a.mb);
  document.getElementById('matrix').innerHTML = `
    <table><thead><tr><th>Route</th><th class="n">Att</th><th class="n">MB</th>
      <th class="n">MB/att</th><th class="n">OK</th><th class="n">Fail</th>
      <th class="n">Success</th><th class="n">Slots</th>
      <th class="n">Accounts</th><th>Supply</th></tr></thead><tbody>
      ${rows.map(r => {
        const tot = r.ok + r.fail, sc = tot ? Math.round(100 * r.ok / tot) : null;
        const p = r.pool;
        const supply = !p.eligible ? '<span class="pill crit">none assigned</span>'
          : !p.healthy ? '<span class="pill crit">all benched</span>'
          : p.eligible <= 2 ? '<span class="pill warn">thin</span>'
          : '<span class="pill ok">ok</span>';
        return `<tr><td><b>${esc(r.k)}</b></td>
          <td class="n">${r.attempts}</td><td class="n">${n1(r.mb)}</td>
          <td class="n">${r.attempts ? (r.mb / r.attempts).toFixed(2) : '–'}</td>
          <td class="n">${r.ok}</td>
          <td class="n">${r.fail ? `<span class="pill crit">${r.fail}</span>` : '0'}</td>
          <td class="n">${sc == null ? '–' : sc + '%'}</td>
          <td class="n">${r.slot ? `<span class="pill ok">${r.slot}</span>` : '–'}</td>
          <td class="n">${p.healthy}/${p.eligible}</td><td>${supply}</td></tr>`;
      }).join('')}</tbody></table>`;
}

function kv(rows) {
  return `<table><tbody>${rows.map(([k, v, cls]) =>
    `<tr><td>${esc(k)}</td><td class="n"${cls ? ` style="color:var(--${cls})"` : ''}>
      <b>${v}</b></td></tr>`).join('')}</tbody></table>`;
}

function drawTurnstile(d) {
  const t = d.turnstile, ch = t.challenged, w = t.widget || 1;
  const clicks = t.click_ok + t.click_fail;
  document.getElementById('turnstile').innerHTML = kv([
    ['Login pages that loaded the widget', t.widget],
    ['Interactive challenge shown', `${ch} (${pct(ch, w)}%)`, ch / w > .25 ? 'warn' : 'ok'],
    ['Checkbox click cleared it', clicks ? `${t.click_ok} (${pct(t.click_ok, clicks)}%)`
      : '–', clicks && t.click_ok / clicks < .4 ? 'warn' : 'ok'],
    ['Click failed → page reloaded', t.click_fail],
    ['Gave up entirely', t.gave_up, t.gave_up ? 'crit' : ''],
    ['Post-Sign-In captcha dialog', `${t.dialog_ok}/${t.dialog} solved`],
    ['Dashboards reached', t.dashboard],
    ['cf_clearance dropped (IP changed)', t.cf_dropped],
    ['"Session Expired" pages', t.session_expired],
  ]);
}

function drawOtp(d) {
  const o = d.otp, i = d.infra;
  document.getElementById('otp').innerHTML = kv([
    ['OTP images read (AI)', o.reads],
    ['Read free from text (Greece)', o.text_mode],
    ['VFS rejected the code', o.rejected, o.rejected ? 'warn' : ''],
    ['Reader gave up', o.gave_up, o.gave_up ? 'crit' : ''],
    ['Accounts spared a strike', o.no_strike],
    ['Proxy upstream errors', i.proxy_err, i.proxy_err > 30 ? 'warn' : ''],
    ['IP rotations', i.rotations],
    ['Page-load budget hits', i.nav_budget, i.nav_budget ? 'warn' : ''],
    ['Denylisted requests refused', i.denylisted.toLocaleString()],
  ]);
}

function drawHourly(d) {
  const ks = Object.keys(d.hourly).sort();
  if (!ks.length) { document.getElementById('hourly').innerHTML =
    '<p class="muted">No traffic recorded.</p>'; return; }
  bars('hourly', ks.map(k => ({ label: k.slice(-2), left: d.hourly[k] })),
       { leftUnit: ' MB' });
}

load();
setInterval(() => { if (document.getElementById('day').value === D.days[0]) load(); }, 60000);
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
        print(f"  health:   {url}/account-health")
        print(f"  stats:    {url}/stats")
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
