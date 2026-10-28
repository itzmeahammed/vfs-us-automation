"""Alerts for combinations the slot check could not CHECK AT ALL.

*** "NO SLOTS" AND "NOT CHECKED" ARE NOT THE SAME ANSWER. ***

send_slot_report() is silent when no combination has availability, which is
right for an empty portal and wrong for a broken one. On 2026-10-16 VFS
reworded two AE-FRA sub-categories ('Short Stay - Business' -> 'Short Stay
(Business)', 'Temporary Location-Damac Hills' -> 'Temporary Location - Damac
Hills'). Both combinations errored on every run from then on, the supervisor
logged "Route AE-FRA OK", and nothing was sent for NINE DAYS while the one
combination still readable reported no availability. France was reporting on a
third of its coverage and looked healthy doing it.

A combination that errored was never read. Availability under it is UNKNOWN,
not absent, and that is worth waking someone for.

WHY IT IS THROTTLED, AND HOW

A stale config fails on EVERY run, so alerting every run would put the same
message in the error channel every few minutes until someone fixed it — and a
channel that cries wolf gets muted, which is the failure this exists to
prevent. So the alert is keyed on WHAT is broken, not on when:

  * a route whose set of failing combinations CHANGES alerts immediately, even
    inside the window — a second combination breaking is news,
  * an unchanged failure repeats at most once per `alert_hours` (default 12),
  * a route that comes back clean forgets its state, so the next break alerts
    at once rather than waiting out a window from the last one.

Config knob: [telegram] combo_alert_hours in config.ini. 0 disables throttling
entirely (every run alerts); a negative value is treated as 0.

State is a small gitignored JSON map under state/, written atomically — the
same shape and the same reasoning as waitlist_cooldown.py, because each
scheduled run is its own process and an in-memory guard would reset every time.
"""

import json
import logging
import os
import time

from src.utils import state_paths
from src.utils.config_reader import get_config_value

STATE_FILE = state_paths.state_file("combo_alerts.json")
DEFAULT_ALERT_HOURS = 12.0

#: The prefix run_slot_check puts on a combination it could not select. Kept
#: here as well as there so a rename breaks a test rather than the alerting.
ERROR_PREFIX = "ERROR:"


def alert_hours() -> float:
    """Repeat window in hours ([telegram] combo_alert_hours; default 12)."""
    try:
        return max(0.0, float(str(get_config_value(
            "telegram", "combo_alert_hours", DEFAULT_ALERT_HOURS)).strip()))
    except (ValueError, TypeError):
        return DEFAULT_ALERT_HOURS


def _load() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (FileNotFoundError, ValueError, OSError):
        # Fail-open: an unreadable store means "nothing is on cooldown", so a
        # real alert is never swallowed by a corrupt file.
        return {}


def _save(data: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    try:
        parent = os.path.dirname(STATE_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)         # atomic on the same filesystem
    except OSError as e:
        logging.warning(f"Could not persist combo alerts to {STATE_FILE}: {e}")


def fingerprint(failures) -> str:
    """What is broken, independent of when.

    `failures` is an iterable of (label, reason). Sorted so the order combos
    happen to be checked in cannot look like a change, and the reason is part
    of it because the SAME combination failing for a NEW reason is new
    information.
    """
    return "|".join(sorted(
        f"{str(label).strip()}=>{str(reason).strip()}" for label, reason in failures))


def should_alert(route: str, failures, now: float = None) -> bool:
    """True when this route's failures deserve a message right now."""
    route = (route or "").strip().upper()
    if not route or not failures:
        return False
    now = time.time() if now is None else now
    window = alert_hours() * 3600
    entry = _load().get(route) or {}
    if entry.get("fingerprint") != fingerprint(failures):
        return True                         # something NEW broke
    if window <= 0:
        return True
    return (now - float(entry.get("at") or 0)) >= window


def record_alerted(route: str, failures, now: float = None) -> None:
    """Remember that this exact set of failures was just reported."""
    route = (route or "").strip().upper()
    if not route:
        return
    data = _load()
    data[route] = {"fingerprint": fingerprint(failures),
                   "at": time.time() if now is None else now}
    _save(data)


def clear(route: str) -> None:
    """Forget a route, so its next failure alerts immediately.

    Called when a route checks every combination cleanly. Without this a route
    that broke, was fixed, and broke again would sit out the rest of the
    original window in silence.
    """
    route = (route or "").strip().upper()
    data = _load()
    if route in data:
        data.pop(route, None)
        _save(data)


def message(route: str, failures, checked: int) -> str:
    """The alert text. States what is unknown, not merely what failed."""
    lines = [f"⚠️ {route}: {len(failures)} of {checked} combination(s) "
             f"could NOT be checked", ""]
    for label, reason in failures:
        lines.append(f"• {label}")
        if reason:
            lines.append(f"    {reason}")
    lines += ["",
              "These were not read and reported empty — they were never read. "
              "Availability under them is UNKNOWN.",
              "A sub-category VFS has reworded is the usual cause: compare the "
              "options in the log against config/routes/" + route + ".json."]
    return "\n".join(lines)


def notify(route: str, entries) -> bool:
    """Alert on every combination in `entries` that could not be selected.

    `entries` is the slot check's list of (combo_dict, message). Returns True
    when a message was sent. Never raises: alerting about a broken check must
    never break the check.
    """
    try:
        from src.vfs_bot.slot_check import combo_label

        failures = [
            (combo_label(combo), str(msg)[len(ERROR_PREFIX):].strip())
            for combo, msg in entries
            if str(msg).startswith(ERROR_PREFIX)
        ]
        if not failures:
            clear(route)
            return False
        if not should_alert(route, failures):
            logging.info(
                f"{route}: {len(failures)} combination(s) still failing "
                f"unchanged — already alerted, next repeat in "
                f"{alert_hours():.0f}h.")
            return False

        text = message(route, failures, len(entries))
        logging.warning(text)

        from src.utils import telegram
        if telegram.is_error_configured():
            telegram.send_error(text)
        else:
            logging.warning(
                "Telegram error channel not configured — the combination "
                "failures above were logged only.")
        record_alerted(route, failures)
        return True
    except Exception as e:                                  # noqa: BLE001
        logging.warning(f"Could not alert on failed combinations: {e}",
                        exc_info=True)
        return False
