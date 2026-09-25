"""Render the watcher's state as a local HTML page.

WHY A FILE AND NOT A SERVER
---------------------------
`python -m src.inbox status` already prints everything here, and for a quick
check the terminal wins. What it cannot do is show the shape of things at a
glance: which mailboxes have gone quiet, which client is waiting on which
reference, how long an invitation has left.

So this writes a self-contained page instead of serving one. No port, no
process to remember to kill, nothing listening on a machine that also holds
passport numbers. Open it, read it, close it; re-run to refresh.

WHAT IT MAY SHOW
----------------
Everything user-facing goes through `waitlist.redaction.scrub()`, exactly as
the Telegram digest does — the page sits in the working directory, and a
working directory is not a safe place for a client's full name. Reference
numbers are shown in full: they identify an application, not a person, and
matching one against the portal by eye is the main reason to open this at all.
"""

from __future__ import annotations

import html
import io
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

DEFAULT_OUTPUT = os.path.join("state", "inbox_panel.html")


def _scrub(text: str) -> str:
    """Redact, then escape. Both, in that order, always."""
    from src.waitlist import redaction

    try:
        cleaned = redaction.scrub(str(text or ""))
    except Exception:
        # Redaction failing must never leak the raw value, and must never be
        # the reason the page does not render.
        cleaned = "[redacted]"
    return html.escape(cleaned)


def _ago(epoch: Optional[float]) -> str:
    if not epoch:
        return "never"
    seconds = max(0, time.time() - epoch)
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds / 60)}m ago"
    if seconds < 172800:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def collect() -> Dict[str, Any]:
    """Gather everything the page shows. No I/O beyond local files.

    Deliberately does NOT open a mailbox. The panel reports what the watcher has
    already recorded; making it fetch would mean a page refresh could trip VFS's
    per-account rate limit, which is how an account gets restricted for 12 hours.
    """
    from src.inbox import config as inbox_config
    from src.inbox import seen as seen_mod
    from src.inbox.watcher import mailbox_accounts
    from src.waitlist.accounts import mask

    data: Dict[str, Any] = {
        "generated_at": time.time(),
        "routes": [],
        "mailboxes": [],
        "clients": [],
        "journal": [],
        "warnings": [],
    }

    # --- routes -------------------------------------------------------------
    for route in inbox_config.configured_routes():
        entry: Dict[str, Any] = {"route": route, "matchers": [], "hours": None}
        try:
            for matcher in inbox_config.matchers_for(route):
                entry["matchers"].append({
                    "name": matcher.get("name", "?"),
                    "classify": matcher.get("classify", "?"),
                })
                if matcher.get("name") == "waitlist_invitation":
                    entry["hours"] = matcher.get("validity_hours")
        except Exception as e:
            entry["error"] = str(e)
            data["warnings"].append(f"{route}: config unusable — {e}")
        data["routes"].append(entry)

    # --- mailboxes ----------------------------------------------------------
    try:
        state = seen_mod.load()
        for user, _ in mailbox_accounts():
            data["mailboxes"].append({
                "mailbox": mask(user),
                "last_pass": state.last_pass(user),
                "high_water": state.high_water(user),
            })
    except Exception as e:
        data["warnings"].append(f"Could not read the watcher state: {e}")

    # --- clients ------------------------------------------------------------
    try:
        from src.waitlist import accounts as waitlist_accounts
        from src.waitlist import registrant as registrant_mod

        for client in registrant_mod.load_all(skip_invalid=True):
            try:
                account = waitlist_accounts.resolve(client)
                account_label = mask(account.email)
                mismatch = bool(
                    (client.get("email") or "").strip().lower()
                    and (client.get("email") or "").strip().lower()
                    != (account.email or "").strip().lower()
                )
            except Exception:
                account_label, mismatch = "(unresolved)", False

            data["clients"].append({
                "id": client.id,
                "route": client.get("route") or "?",
                "enabled": bool(client.get("enabled")),
                "account": account_label,
                "email_mismatch": mismatch,
            })
            if mismatch:
                data["warnings"].append(
                    f"{client.id}: the form email differs from the account, so "
                    "VFS sends the invitation to a mailbox nothing watches.")
    except Exception as e:
        data["warnings"].append(f"Could not read the clients: {e}")

    # --- journal ------------------------------------------------------------
    try:
        from src.waitlist import journal

        for row in journal.entries():
            data["journal"].append({
                "route": row.get("route", ""),
                "combo": row.get("combo", ""),
                "client": row.get("registrant_id", ""),
                "status": row.get("status", ""),
                "reference": row.get("vfs_reference") or "",
                "when": row.get("started_at", ""),
                "reason": row.get("reason", ""),
            })
    except Exception as e:
        data["warnings"].append(f"Could not read the journal: {e}")

    return data


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #

_CSS = """
:root { color-scheme: light dark;
  --bg:#f6f7f9; --card:#fff; --ink:#1a1c1e; --muted:#5f6571;
  --line:#e3e6ea; --ok:#0a7d32; --warn:#9a6700; --bad:#b3261e; --accent:#12507e; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#14161a; --card:#1c1f24; --ink:#e7e9ec; --muted:#9aa1ac;
  --line:#2c3037; --ok:#5dd47f; --warn:#e3b341; --bad:#ff6b6b; --accent:#7cc0f5; } }
* { box-sizing:border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--ink);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
h1 { font-size:20px; margin:0 0 2px; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); margin:28px 0 10px; }
.sub { color:var(--muted); margin-bottom:8px; }
.card { background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:2px 14px; }
table { width:100%; border-collapse:collapse; }
th { text-align:left; font-size:12px; text-transform:uppercase;
  letter-spacing:.04em; color:var(--muted); font-weight:600;
  padding:10px 8px; border-bottom:1px solid var(--line); }
td { padding:9px 8px; border-bottom:1px solid var(--line); }
tr:last-child td { border-bottom:0; }
code, .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:13px; }
.pill { display:inline-block; padding:1px 8px; border-radius:20px;
  font-size:12px; font-weight:600; border:1px solid currentColor; }
.ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
.muted{color:var(--muted)}
.banner { border-left:3px solid var(--warn); background:var(--card);
  border-radius:0 8px 8px 0; padding:10px 14px; margin-bottom:8px; }
.wrap { overflow-x:auto; }
"""


def _pill(text: str, tone: str = "muted") -> str:
    return f'<span class="pill {tone}">{html.escape(text)}</span>'


def _status_tone(status: str) -> str:
    status = (status or "").lower()
    if status in ("success", "booked"):
        return "ok"
    if status in ("pending", "unknown", "booking_unknown"):
        return "bad"
    if status in ("failed", "expired", "slot_gone"):
        return "warn"
    return "muted"


def render(data: Dict[str, Any]) -> str:
    """Build the page. Pure — takes collected data, returns HTML."""
    parts: List[str] = []
    generated = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(data["generated_at"]))

    parts.append("<!doctype html><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append("<title>VFS inbox watcher</title>")
    parts.append(f"<style>{_CSS}</style>")
    parts.append("<h1>VFS inbox watcher</h1>")
    parts.append(
        f"<div class='sub'>Snapshot taken {html.escape(generated)}. "
        "This page is static — re-run <code>python -m src.inbox panel</code> "
        "to refresh it.</div>")

    # --- warnings first: they are the reason to open this at all ------------
    if data["warnings"]:
        parts.append("<h2>Needs attention</h2>")
        for warning in data["warnings"]:
            parts.append(f"<div class='banner'>{_scrub(warning)}</div>")

    # --- mailboxes ----------------------------------------------------------
    parts.append("<h2>Mailboxes under watch</h2>")
    parts.append("<div class='card wrap'><table><tr>"
                 "<th>Mailbox</th><th>Last read</th><th>Up to UID</th></tr>")
    if not data["mailboxes"]:
        parts.append("<tr><td colspan='3' class='muted'>None resolved — check "
                     "config/registrants/ and [waitlist].</td></tr>")
    for box in data["mailboxes"]:
        never = not box["last_pass"]
        parts.append(
            "<tr>"
            f"<td class='mono'>{html.escape(str(box['mailbox']))}</td>"
            f"<td>{_pill(_ago(box['last_pass']), 'warn' if never else 'ok')}</td>"
            f"<td class='mono muted'>{html.escape(str(box['high_water']))}</td>"
            "</tr>")
    parts.append("</table></div>")

    # --- routes -------------------------------------------------------------
    parts.append("<h2>Routes configured for email</h2>")
    parts.append("<div class='card wrap'><table><tr>"
                 "<th>Route</th><th>Invitation window</th><th>Matchers</th></tr>")
    for route in data["routes"]:
        hours = route.get("hours")
        if hours:
            window = _pill(f"{hours}h", "bad" if int(hours) <= 12 else "muted")
        else:
            window = _pill("unknown", "warn")
        names = ", ".join(m["name"] for m in route["matchers"]) or "—"
        parts.append(
            "<tr>"
            f"<td class='mono'>{html.escape(route['route'])}</td>"
            f"<td>{window}</td>"
            f"<td class='muted'>{html.escape(names)}</td>"
            "</tr>")
    parts.append("</table></div>")

    # --- clients ------------------------------------------------------------
    parts.append("<h2>Clients</h2>")
    parts.append("<div class='card wrap'><table><tr>"
                 "<th>Client</th><th>Route</th><th>Armed</th>"
                 "<th>Account watched</th></tr>")
    for client in data["clients"]:
        armed = (_pill("enabled", "ok") if client["enabled"]
                 else _pill("parked", "muted"))
        account = html.escape(str(client["account"]))
        if client["email_mismatch"]:
            account += " " + _pill("invitation goes elsewhere", "bad")
        parts.append(
            "<tr>"
            f"<td class='mono'>{html.escape(client['id'])}</td>"
            f"<td class='mono'>{html.escape(client['route'])}</td>"
            f"<td>{armed}</td><td>{account}</td>"
            "</tr>")
    parts.append("</table></div>")

    # --- journal ------------------------------------------------------------
    parts.append("<h2>Registration history</h2>")
    parts.append("<div class='card wrap'><table><tr>"
                 "<th>When</th><th>Route</th><th>Client</th>"
                 "<th>Status</th><th>Reference</th></tr>")
    for row in reversed(data["journal"][-25:]):
        parts.append(
            "<tr>"
            f"<td class='muted mono'>{html.escape(str(row['when'])[:16])}</td>"
            f"<td class='mono'>{html.escape(row['route'])}</td>"
            f"<td class='mono'>{_scrub(row['client'])}</td>"
            f"<td>{_pill(row['status'] or '?', _status_tone(row['status']))}</td>"
            f"<td class='mono'>{html.escape(row['reference'] or '—')}</td>"
            "</tr>")
    parts.append("</table></div>")

    parts.append(
        "<h2>What this page is not</h2>"
        "<div class='card' style='padding:12px 14px'>"
        "<p class='muted' style='margin:6px 0'>The watcher is "
        "<strong>observational</strong>: it reads mail read-only and triggers "
        "nothing. Nothing here books an appointment, and no booking route is "
        "enabled yet.</p></div>")

    return "\n".join(parts)


def write(path: str = DEFAULT_OUTPUT) -> str:
    """Collect, render and save. Returns the path written."""
    data = collect()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(render(data))
    log.info(f"Panel written to {path}")
    return path
