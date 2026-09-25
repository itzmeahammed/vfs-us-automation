"""Render the whole pipeline's state as one local HTML page.

WHY A FILE AND NOT A SERVER
---------------------------
`python -m src.inbox status` already prints most of this, and for a quick check
the terminal wins. What it cannot do is show the shape of things at a glance:
which application is waiting on which reference, how long an invitation has
left, which route is armed and which is not.

So this writes a self-contained page instead of serving one. No port, no
process to remember to kill, nothing listening on a machine that also holds
passport numbers. Open it, read it, close it; re-run to refresh.

WHAT IT COVERS
--------------
All four stages on one page, because the interesting questions cross them:

    clients      who we act for, and where their mail lands
    waitlist     registrations, their references, and their live status
    email        what VFS has said, and what is still being watched for
    booking      routes, their commit boundary, and whether they are armed

VALUES ARE SHOWN IN FULL. This page is local, is gitignored, and exists to be
read by the person who owns the data — masking an address they typed
themselves only makes it useless for spotting that the wrong one is configured.
The Telegram digest still redacts, because that leaves the machine.
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

#: Statuses that mean a waitlist row is spoken for at VFS.
_LIVE_STATUSES = {"success", "pending", "unknown"}


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


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


def _parse_when(value: Any) -> Optional[float]:
    """Journal timestamps are ISO strings; epoch floats appear elsewhere."""
    if isinstance(value, (int, float)) and value:
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return time.mktime(time.strptime(text[:19], fmt))
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Collecting                                                                   #
# --------------------------------------------------------------------------- #

def collect() -> Dict[str, Any]:
    """Gather everything the page shows. Local files only.

    Deliberately opens NO mailbox and NO browser. The panel reports what has
    already been recorded; making a refresh fetch would let opening a page trip
    VFS's per-account rate limit, which is how an account gets restricted for
    12 hours.
    """
    data: Dict[str, Any] = {
        "generated_at": time.time(),
        "clients": [],
        "waitlist_rows": [],
        "waitlist_routes": [],
        "booking_routes": [],
        "inbox_routes": [],
        "mailboxes": [],
        "health": [],
        "warnings": [],
        "totals": {},
    }

    _collect_clients(data)
    _collect_journal(data)
    _collect_routes(data)
    _collect_mailboxes(data)
    _collect_health(data)
    _summarise(data)
    return data


def _collect_clients(data: Dict[str, Any]) -> None:
    try:
        from src.waitlist import accounts as waitlist_accounts
        from src.waitlist import registrant as registrant_mod

        for client in registrant_mod.load_all(skip_invalid=True):
            try:
                resolved = waitlist_accounts.resolve(client)
                account, source = resolved.email, resolved.source
            except Exception as e:                      # noqa: BLE001
                account, source = "(unresolved)", str(e)[:60]

            form_email = (client.get("email") or "").strip()
            mismatch = bool(form_email and account
                            and form_email.lower() != account.lower())

            # route/combos/enabled are ATTRIBUTES on Registrant, not form
            # fields: .get() reaches the client's data only, so asking it for
            # "route" returns None and every client renders as "?".
            data["clients"].append({
                "id": client.id,
                "name": " ".join(x for x in (client.get("first_name"),
                                             client.get("last_name")) if x),
                "route": getattr(client, "route", "") or "?",
                "combos": list(getattr(client, "combos", []) or []),
                "enabled": bool(getattr(client, "enabled", False)),
                "account": account,
                "account_source": source,
                "form_email": form_email,
                "email_mismatch": mismatch,
            })
            if mismatch:
                data["warnings"].append(
                    f"{client.id}: VFS sends the invitation to {form_email}, "
                    f"but the watcher reads {account}. The invitation would "
                    "arrive where nothing is looking.")
    except Exception as e:                              # noqa: BLE001
        data["warnings"].append(f"Could not read the clients: {e}")


def _collect_journal(data: Dict[str, Any]) -> None:
    """Every registration attempt, newest first, with the live ones marked.

    Groups by (route, combo, client) and keeps the LATEST row per group, which
    is the same rule `journal.blocking_entry` uses to decide whether a client
    already holds an entry — so what this shows as live is what the runner will
    treat as live.
    """
    try:
        from src.waitlist import journal

        latest: Dict[Any, Dict[str, Any]] = {}
        rows: List[Dict[str, Any]] = []

        for row in journal.entries():
            when = _parse_when(row.get("started_at"))
            record = {
                "route": row.get("route", ""),
                "combo": row.get("combo", ""),
                "client": row.get("registrant_id", ""),
                "status": row.get("status", ""),
                "reference": row.get("vfs_reference") or "",
                "account": row.get("account", ""),
                "reason": row.get("reason", ""),
                "when": when,
                "when_text": str(row.get("started_at") or "")[:16],
            }
            rows.append(record)
            key = (record["route"], record["combo"], record["client"])
            latest[key] = record

        known_clients = {c["id"] for c in data["clients"]}
        for record in rows:
            key = (record["route"], record["combo"], record["client"])
            record["is_latest"] = latest.get(key) is record
            record["orphaned"] = record["client"] not in known_clients
            # An entry belonging to a client file that no longer exists is
            # history, not something being waited on — counting it as live
            # would mean the panel reports work in flight for somebody nobody
            # is acting for.
            record["live"] = (record["is_latest"]
                              and record["status"] in _LIVE_STATUSES
                              and not record["orphaned"])

        data["waitlist_rows"] = rows
    except Exception as e:                              # noqa: BLE001
        data["warnings"].append(f"Could not read the journal: {e}")


def _collect_routes(data: Dict[str, Any]) -> None:
    # --- waitlist -----------------------------------------------------------
    try:
        from src.waitlist import config as waitlist_config

        for route in waitlist_config.configured_routes():
            try:
                cfg = waitlist_config.get(route)
                data["waitlist_routes"].append({
                    "route": route,
                    "enabled": bool(cfg.get("enabled")),
                    "steps": len(cfg.get("steps") or []),
                })
            except Exception as e:                      # noqa: BLE001
                data["waitlist_routes"].append(
                    {"route": route, "error": str(e)[:80]})
    except Exception as e:                              # noqa: BLE001
        data["warnings"].append(f"Could not read the waitlist configs: {e}")

    # --- booking ------------------------------------------------------------
    try:
        from src.booking import config as booking_config

        for route in booking_config.configured_routes():
            try:
                cfg = booking_config.get(route)
                steps = booking_config.steps_for(route)
                data["booking_routes"].append({
                    "route": route,
                    "enabled": bool(cfg.get("enabled")),
                    "commit_step": booking_config.commit_step_name(route),
                    "steps": [{"name": s.get("name", "?"),
                               "type": s.get("type", "?"),
                               "commits": bool(s.get("commits"))}
                              for s in steps],
                })
            except Exception as e:                      # noqa: BLE001
                data["booking_routes"].append(
                    {"route": route, "error": str(e)[:80], "steps": []})
    except Exception as e:                              # noqa: BLE001
        data["warnings"].append(f"Could not read the booking configs: {e}")

    # --- inbox --------------------------------------------------------------
    try:
        from src.inbox import config as inbox_config

        for route in inbox_config.configured_routes():
            entry: Dict[str, Any] = {"route": route, "hours": None,
                                     "matchers": []}
            try:
                for matcher in inbox_config.matchers_for(route):
                    entry["matchers"].append(matcher.get("name", "?"))
                    if matcher.get("name") == "waitlist_invitation":
                        entry["hours"] = matcher.get("validity_hours")
            except Exception as e:                      # noqa: BLE001
                entry["error"] = str(e)[:80]
                data["warnings"].append(f"{route}: inbox config unusable — {e}")
            data["inbox_routes"].append(entry)
    except Exception as e:                              # noqa: BLE001
        data["warnings"].append(f"Could not read the inbox configs: {e}")


def _collect_mailboxes(data: Dict[str, Any]) -> None:
    try:
        from src.inbox import seen as seen_mod
        from src.inbox.watcher import mailbox_accounts

        state = seen_mod.load()
        watched = {user for user, _ in mailbox_accounts()}

        for user in sorted(watched):
            data["mailboxes"].append({
                "mailbox": user,
                "last_pass": state.last_pass(user),
                "high_water": state.high_water(user),
            })

        # An account a client REGISTERS under but whose mailbox is not watched
        # is the silent failure this whole package exists to prevent.
        for client in data["clients"]:
            account = client["account"]
            if account and account != "(unresolved)" and account not in watched:
                data["warnings"].append(
                    f"{account} holds registrations but is NOT being watched — "
                    "no credentials for it. Add them to [inbox] mailboxes in "
                    "config/config.local.ini.")
    except Exception as e:                              # noqa: BLE001
        data["warnings"].append(f"Could not read the watcher state: {e}")


def _collect_health(data: Dict[str, Any]) -> None:
    try:
        from src.utils import account_health

        snapshot = account_health.snapshot() or {}
        for email, record in snapshot.items():
            for route, detail in (record.get("routes") or {}).items():
                until = float(detail.get("cooldown_until", 0) or 0)
                if until <= time.time():
                    continue
                data["health"].append({
                    "account": email,
                    "route": route,
                    "until": until,
                    "reason": detail.get("last_reason", ""),
                })
                data["warnings"].append(
                    f"{email} is benched on {route} until "
                    f"{time.strftime('%d %b %H:%M', time.localtime(until))} — "
                    f"{detail.get('last_reason', 'no reason recorded')}.")
    except Exception as e:                              # noqa: BLE001
        log.debug(f"Could not read account health: {e}")


def _summarise(data: Dict[str, Any]) -> None:
    live = [r for r in data["waitlist_rows"] if r.get("live")]
    data["totals"] = {
        "clients": len(data["clients"]),
        "armed_clients": sum(1 for c in data["clients"] if c["enabled"]),
        "live_entries": len(live),
        "mailboxes": len(data["mailboxes"]),
        "booking_enabled": sum(1 for r in data["booking_routes"]
                               if r.get("enabled")),
        "booking_routes": len(data["booking_routes"]),
    }


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #

_CSS = """
:root { color-scheme: light dark;
  --bg:#f5f6f8; --card:#fff; --ink:#1a1c1e; --muted:#606772;
  --line:#e4e7eb; --ok:#0a7d32; --warn:#8a5a00; --bad:#b3261e; --accent:#12507e; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#131519; --card:#1b1e23; --ink:#e8eaed; --muted:#9aa1ac;
  --line:#2b2f36; --ok:#5dd47f; --warn:#e3b341; --bad:#ff6b6b; --accent:#7cc0f5; } }
* { box-sizing:border-box; }
body { margin:0; padding:22px; background:var(--bg); color:var(--ink);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
h1 { font-size:21px; margin:0 0 2px; }
h2 { font-size:12px; text-transform:uppercase; letter-spacing:.07em;
  color:var(--muted); margin:26px 0 9px; font-weight:700; }
.sub { color:var(--muted); margin-bottom:6px; font-size:13px; }
.card { background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:0 14px; }
table { width:100%; border-collapse:collapse; }
th { text-align:left; font-size:11px; text-transform:uppercase;
  letter-spacing:.05em; color:var(--muted); font-weight:700;
  padding:10px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
td { padding:9px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
tr:last-child td { border-bottom:0; }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:12.5px; }
.pill { display:inline-block; padding:1px 8px; border-radius:20px;
  font-size:11.5px; font-weight:700; border:1px solid currentColor;
  white-space:nowrap; }
.ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
.muted{color:var(--muted)} .accent{color:var(--accent)}
.banner { border-left:3px solid var(--warn); background:var(--card);
  border-radius:0 8px 8px 0; padding:9px 13px; margin-bottom:7px; font-size:13px; }
.wrap { overflow-x:auto; }
.tiles { display:flex; gap:10px; flex-wrap:wrap; margin:14px 0 4px; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:11px 16px; min-width:104px; }
.tile .n { font-size:23px; font-weight:700; line-height:1.15; }
.tile .l { font-size:11px; text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); font-weight:600; }
.dim td { opacity:.5; }
.note { font-size:12.5px; color:var(--muted); margin:7px 0 0; }
"""


def _pill(text: str, tone: str = "muted") -> str:
    return f'<span class="pill {tone}">{_esc(text)}</span>'


def _status_tone(status: str) -> str:
    status = (status or "").lower()
    if status in ("success", "booked"):
        return "ok"
    if status in ("pending", "unknown", "booking_unknown"):
        return "bad"
    if status in ("failed", "expired", "slot_gone", "cancelled"):
        return "warn"
    return "muted"


def _tile(number: Any, label: str, tone: str = "") -> str:
    return (f"<div class='tile'><div class='n {tone}'>{_esc(number)}</div>"
            f"<div class='l'>{_esc(label)}</div></div>")


def render(data: Dict[str, Any]) -> str:
    """Build the page. Pure — data in, HTML out."""
    out: List[str] = []
    totals = data["totals"]
    generated = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(data["generated_at"]))

    out.append("<!doctype html><meta charset='utf-8'>")
    out.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    out.append("<title>VFS pipeline</title>")
    out.append(f"<style>{_CSS}</style>")
    out.append("<h1>VFS pipeline</h1>")
    out.append(f"<div class='sub'>Snapshot {_esc(generated)} · static page, "
               "re-run <span class='mono'>python -m src.inbox panel</span> "
               "to refresh</div>")

    # --- tiles --------------------------------------------------------------
    out.append("<div class='tiles'>")
    out.append(_tile(totals.get("clients", 0), "clients"))
    out.append(_tile(totals.get("armed_clients", 0), "armed",
                     "ok" if totals.get("armed_clients") else "muted"))
    out.append(_tile(totals.get("live_entries", 0), "live entries", "accent"))
    out.append(_tile(totals.get("mailboxes", 0), "mailboxes watched"))
    out.append(_tile(f"{totals.get('booking_enabled', 0)}/"
                     f"{totals.get('booking_routes', 0)}", "booking armed",
                     "muted"))
    out.append("</div>")

    # --- warnings -----------------------------------------------------------
    if data["warnings"]:
        out.append("<h2>Needs attention</h2>")
        for warning in data["warnings"]:
            out.append(f"<div class='banner'>{_esc(warning)}</div>")

    # --- clients ------------------------------------------------------------
    out.append("<h2>Clients</h2>")
    out.append("<div class='card wrap'><table><tr>"
               "<th>Client</th><th>Name</th><th>Route</th><th>Combos</th>"
               "<th>Armed</th><th>VFS account</th><th>Form email</th></tr>")
    if not data["clients"]:
        out.append("<tr><td colspan='7' class='muted'>No client files in "
                   "config/registrants/.</td></tr>")
    for client in data["clients"]:
        armed = (_pill("enabled", "ok") if client["enabled"]
                 else _pill("parked", "muted"))
        form = _esc(client["form_email"] or "—")
        if client["email_mismatch"]:
            form += " " + _pill("unwatched", "bad")
        out.append(
            "<tr>"
            f"<td class='mono'>{_esc(client['id'])}</td>"
            f"<td>{_esc(client['name'])}</td>"
            f"<td class='mono'>{_esc(client['route'])}</td>"
            f"<td class='muted'>{_esc(', '.join(client['combos']))}</td>"
            f"<td>{armed}</td>"
            f"<td class='mono'>{_esc(client['account'])}</td>"
            f"<td class='mono'>{form}</td>"
            "</tr>")
    out.append("</table></div>")

    # --- live waitlist entries ---------------------------------------------
    live = [r for r in data["waitlist_rows"] if r.get("live")]
    out.append("<h2>Live waitlist entries</h2>")
    out.append("<div class='card wrap'><table><tr>"
               "<th>Reference</th><th>Route</th><th>Combo</th><th>Client</th>"
               "<th>Account</th><th>Status</th><th>Registered</th></tr>")
    if not live:
        out.append("<tr><td colspan='7' class='muted'>Nothing queued at VFS "
                   "right now.</td></tr>")
    for row in live:
        out.append(
            "<tr>"
            f"<td class='mono accent'>{_esc(row['reference'] or '—')}</td>"
            f"<td class='mono'>{_esc(row['route'])}</td>"
            f"<td class='muted'>{_esc(row['combo'])}</td>"
            f"<td class='mono'>{_esc(row['client'])}</td>"
            f"<td class='mono muted'>{_esc(row['account'])}</td>"
            f"<td>{_pill(row['status'], _status_tone(row['status']))}</td>"
            f"<td class='muted mono'>{_esc(row['when_text'])}</td>"
            "</tr>")
    out.append("</table></div>")
    out.append("<p class='note'>Live means the latest row for that "
               "(route, combo, client) is success, pending or unknown — the "
               "same test the runner uses to refuse a duplicate registration."
               "</p>")

    # --- mailboxes ----------------------------------------------------------
    out.append("<h2>Mailboxes under watch</h2>")
    out.append("<div class='card wrap'><table><tr>"
               "<th>Mailbox</th><th>Last read</th><th>Up to UID</th></tr>")
    if not data["mailboxes"]:
        out.append("<tr><td colspan='3' class='muted'>None resolved.</td></tr>")
    for box in data["mailboxes"]:
        never = not box["last_pass"]
        out.append(
            "<tr>"
            f"<td class='mono'>{_esc(box['mailbox'])}</td>"
            f"<td>{_pill(_ago(box['last_pass']), 'warn' if never else 'ok')}</td>"
            f"<td class='mono muted'>{_esc(box['high_water'])}</td>"
            "</tr>")
    out.append("</table></div>")

    # --- email rules --------------------------------------------------------
    out.append("<h2>Email rules per route</h2>")
    out.append("<div class='card wrap'><table><tr>"
               "<th>Route</th><th>Invitation window</th><th>Matchers</th></tr>")
    for route in data["inbox_routes"]:
        hours = route.get("hours")
        if hours:
            window = _pill(f"{hours}h",
                           "bad" if int(hours) <= 12 else "muted")
        else:
            window = _pill("unknown", "warn")
        out.append(
            "<tr>"
            f"<td class='mono'>{_esc(route['route'])}</td>"
            f"<td>{window}</td>"
            f"<td class='muted'>{_esc(', '.join(route['matchers']))}</td>"
            "</tr>")
    out.append("</table></div>")

    # --- routes: waitlist + booking ----------------------------------------
    out.append("<h2>Routes</h2>")
    out.append("<div class='card wrap'><table><tr>"
               "<th>Route</th><th>Waitlist</th><th>Booking</th>"
               "<th>Booking flow</th><th>Commits at</th></tr>")
    booking_by_route = {r["route"]: r for r in data["booking_routes"]}
    seen_routes = {r["route"] for r in data["waitlist_routes"]} | set(booking_by_route)
    for name in sorted(seen_routes):
        wl = next((r for r in data["waitlist_routes"] if r["route"] == name), None)
        bk = booking_by_route.get(name)

        if wl is None:
            wl_cell = _pill("not configured", "muted")
        elif wl.get("error"):
            wl_cell = _pill("broken", "bad")
        else:
            wl_cell = (_pill("armed", "ok") if wl["enabled"]
                       else _pill("off", "muted"))

        if bk is None:
            bk_cell, flow, commit = _pill("not configured", "muted"), "—", "—"
        else:
            bk_cell = (_pill("armed", "ok") if bk.get("enabled")
                       else _pill("off", "warn"))
            flow = " → ".join(
                (s["name"] + ("*" if s["commits"] else ""))
                for s in bk.get("steps", [])) or "—"
            commit = bk.get("commit_step") or "—"

        out.append(
            "<tr>"
            f"<td class='mono'>{_esc(name)}</td>"
            f"<td>{wl_cell}</td><td>{bk_cell}</td>"
            f"<td class='muted mono'>{_esc(flow)}</td>"
            f"<td class='mono'>{_esc(commit)}</td>"
            "</tr>")
    out.append("</table></div>")
    out.append("<p class='note'>* marks the committing step — the point of no "
               "return. Every booking route ships off until its whole flow has "
               "been walked in a browser.</p>")

    # --- full history -------------------------------------------------------
    out.append("<h2>Registration history</h2>")
    out.append("<div class='card wrap'><table><tr>"
               "<th>When</th><th>Route</th><th>Combo</th><th>Client</th>"
               "<th>Status</th><th>Reference</th><th>Detail</th></tr>")
    history = sorted(data["waitlist_rows"],
                     key=lambda r: r.get("when") or 0, reverse=True)
    for row in history[:40]:
        css = "" if row.get("is_latest") else " class='dim'"
        out.append(
            f"<tr{css}>"
            f"<td class='muted mono'>{_esc(row['when_text'])}</td>"
            f"<td class='mono'>{_esc(row['route'])}</td>"
            f"<td class='muted'>{_esc(row['combo'])}</td>"
            f"<td class='mono'>{_esc(row['client'])}</td>"
            f"<td>{_pill(row['status'] or '?', _status_tone(row['status']))}</td>"
            f"<td class='mono'>{_esc(row['reference'] or '—')}</td>"
            f"<td class='muted'>{_esc(row['reason'][:70])}"
            + (" " + _pill("client deleted", "muted")
               if row.get("orphaned") else "")
            + "</td>"
            "</tr>")
    out.append("</table></div>")
    out.append("<p class='note'>Superseded rows are dimmed: the journal is "
               "append-only, so a correction is a new row rather than an "
               "edit.</p>")

    out.append(
        "<h2>What this page is not</h2>"
        "<div class='card' style='padding:11px 14px'>"
        "<p class='note' style='margin:5px 0'>A snapshot of local state. The "
        "watcher is observational — it reads mail read-only and triggers "
        "nothing — and no booking route is armed, so nothing here books an "
        "appointment.</p></div>")

    return "\n".join(out)


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
