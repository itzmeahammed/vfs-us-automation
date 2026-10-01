"""Readiness: can this machine actually do what its switches say?

/health answers "is the process up". It says nothing about whether a booking
can pay, whether invitations can be read, or whether the slot checker has run
today — and those are the failures that cost a client their slot. Each check
here returns ok/warn/fail with a sentence a human can act on. A check only
FAILS when the flow it serves is switched on: no card is fine while live
booking is off.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

OK, WARN, FAIL = "ok", "warn", "fail"

#: Slot checks run every 30 min inside the schedule window. Twice that with
#: no finished run, inside the window, means the scheduler is not running.
STALE_RUN_SECONDS = 75 * 60


def _check(name: str, state: str, detail: str) -> Dict[str, str]:
    return {"name": name, "state": state, "detail": detail}


def _severity(on: bool) -> str:
    return FAIL if on else WARN


def checks() -> List[Dict[str, str]]:
    from src.settings import settings

    sw = settings().switches
    out: List[Dict[str, str]] = []

    out.append(_check("switches", OK, ", ".join(
        f"{name}={'on' if getattr(sw, name) else 'off'}"
        for name in ("waitlist", "invite_booking", "live_booking", "test_booking"))))

    # -- the company card (live and invite booking pay with it) -------------
    try:
        from src.booking.autobook import card_problem
        problem = card_problem()
    except Exception as exc:                                # noqa: BLE001
        problem = f"card check failed: {exc}"
    paying = sw.live_booking or sw.invite_booking
    out.append(_check("company_card", _severity(paying) if problem else OK,
                      problem or "configured"))

    # -- a VFS account for bookings and registrations -----------------------
    try:
        from src.waitlist import accounts
        from src.waitlist.registrant import Registrant
        accounts.resolve(Registrant("readiness", {"route": "AE-XXX", "combos": ["x"]}))
        out.append(_check("shared_vfs_account", OK, "configured"))
    except Exception:                                       # noqa: BLE001
        out.append(_check("shared_vfs_account", WARN,
                          "no shared account ([waitlist] account); every client "
                          "and request must carry its own"))

    # -- invitation mail -----------------------------------------------------
    try:
        from src.utils.config_reader import get_config_value
        host = get_config_value("otp", "imap_host", "")
    except Exception:                                       # noqa: BLE001
        host = ""
    out.append(_check("imap", OK if host else _severity(sw.invite_booking),
                      "configured" if host else
                      "no [otp] imap_host: invitation emails cannot be read"))

    # -- Telegram ------------------------------------------------------------
    try:
        from src.utils import telegram
        tg = telegram.is_error_configured()
    except Exception:                                       # noqa: BLE001
        tg = False
    out.append(_check("telegram", OK if tg else WARN,
                      "configured" if tg else "testing-bot chat not configured: "
                                              "booking alerts go nowhere"))

    # -- the slot checker is actually running -------------------------------
    out.append(_slot_checker())
    return out


def _slot_checker() -> Dict[str, str]:
    from src.settings import settings

    try:
        from src.slots import store
        path = store.db_path()
        if not os.path.exists(path):
            return _check("slot_checker", WARN, "no slot database yet")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute("select max(finished_at_utc) from runs").fetchone()
        finally:
            con.close()
        last = row[0] if row else None
    except Exception as exc:                                # noqa: BLE001
        return _check("slot_checker", WARN, f"could not read the slot database: {exc}")
    if not last:
        return _check("slot_checker", WARN, "no finished slot-check run recorded")

    try:
        finished = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        age = time.time() - finished.timestamp()
    except ValueError:
        return _check("slot_checker", WARN, f"unreadable last-run time {last!r}")

    sched = settings().schedule
    hour = datetime.now().hour
    in_window = int(getattr(sched, "start_hour", 0)) <= hour < int(getattr(sched, "end_hour", 24))
    minutes = int(age // 60)
    if age > STALE_RUN_SECONDS and in_window:
        return _check("slot_checker", FAIL,
                      f"last route finished {minutes} min ago, inside the "
                      "schedule window: the scheduled task may not be running")
    return _check("slot_checker", OK, f"last route finished {minutes} min ago")


def overall(results: List[Dict[str, str]]) -> str:
    states = {r["state"] for r in results}
    if FAIL in states:
        return "not_ready"
    if WARN in states:
        return "degraded"
    return "ready"


def switches_view() -> Dict[str, Any]:
    from src.settings import settings

    sw = settings().switches
    return {
        "waitlist": sw.waitlist,
        "invite_booking": sw.invite_booking,
        "live_booking": sw.live_booking,
        "test_booking": sw.test_booking,
        "note": ("A slot-check run reads the switches once, when it starts "
                 "(every 30 min). A change applies from the NEXT run; a run "
                 "already in progress keeps the values it started with."),
    }
