"""Daily cap on metered proxy traffic, with an early Telegram warning.

The proxy is billed per byte, so a regression (or a bad retry day) can burn a
month's allowance in an afternoon — exactly what happened on 2026-08-17, when a
request-interception change disabled Chromium's HTTP cache and the bill tripled
to 2.19 GB before anyone noticed. This module is the backstop: it keeps a
running total for the calendar day, warns once when usage crosses a percentage
of the cap, and pauses the remaining routes once the cap is spent.

What counts: PROXY bytes only — what the provider actually bills, as metered by
proxy_forwarder (both directions of the tunnel, plus the short-lived IP probes).
A direct/local-IP run costs nothing and is not counted.

State is a tiny gitignored JSON file at the project root:

    {"date": "2026-08-19", "used_mb": 412.7, "warned": true, "capped": false}

It lives on disk, not in memory, because each scheduled run is its OWN process —
an in-memory counter would reset every 30 minutes. `date` is the local calendar
day; the first call on a new day rolls the file over and clears both flags, so
there is no cron job to forget. This mirrors waitlist_cooldown.py and
account_health.py deliberately: same atomic-write JSON pattern, right-sized for
this app.

Granularity: the cap is checked BETWEEN routes, never mid-route. Killing a
browser halfway through would waste the bytes already spent and leave a
half-driven session behind, so the last route is always allowed to finish. Worst
case the day ends one route over the cap (~2-3 MB normally, up to ~20 MB if that
route burns every retry) — bounded, and cheaper than the alternative.

Scaling note — same as waitlist_cooldown.py: if this ever needs concurrent
writers, SQLite gives real locking with zero infra, or Redis models the day as a
native TTL key (INCRBYFLOAT + EXPIRE at midnight) so rollover is automatic.

Config knobs: [bandwidth] daily_cap_mb, warn_at_percent.
"""

import json
import logging
import os
from datetime import date

STATE_FILE = "bandwidth_budget.json"


def _bw():
    """The [bandwidth] settings section, or None if settings aren't loaded."""
    try:
        from src.settings import settings
        return settings().bandwidth
    except Exception:
        return None


def cap_mb() -> float:
    """Daily ceiling in MB ([bandwidth] daily_cap_mb). 0 disables the cap."""
    bw = _bw()
    try:
        return max(0.0, float(bw.daily_cap_mb)) if bw else 0.0
    except (TypeError, ValueError):
        return 0.0


def warn_percent() -> float:
    """Warn once at this percent of the cap ([bandwidth] warn_at_percent).

    0 (or no cap) disables the warning. Clamped to 100 — a threshold above the
    cap could never fire, and would silently disable the early warning.
    """
    bw = _bw()
    try:
        return min(100.0, max(0.0, float(bw.warn_at_percent))) if bw else 0.0
    except (TypeError, ValueError):
        return 0.0


def _today() -> str:
    return date.today().isoformat()


def _blank(day: str = None) -> dict:
    return {"date": day or _today(), "used_mb": 0.0, "warned": False, "capped": False}


def _load() -> dict:
    """Today's ledger, rolling over automatically if the file is from an
    earlier day. Fail-OPEN on a missing/corrupt file: a broken counter must
    never be read as 'cap already spent' and silently stop every route."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (FileNotFoundError, ValueError, OSError):
        return _blank()
    if not isinstance(data, dict) or data.get("date") != _today():
        return _blank()
    try:
        data["used_mb"] = max(0.0, float(data.get("used_mb") or 0.0))
    except (TypeError, ValueError):
        data["used_mb"] = 0.0
    data["warned"] = bool(data.get("warned"))
    data["capped"] = bool(data.get("capped"))
    return data


def _save(data: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)  # atomic on the same filesystem
    except OSError as e:
        logging.warning(f"Could not persist bandwidth budget to {STATE_FILE}: {e}")


# --------------------------------------------------------------------------- #
# Reading the ledger                                                          #
# --------------------------------------------------------------------------- #

def used_mb() -> float:
    """MB of metered proxy traffic recorded so far today."""
    return _load()["used_mb"]


def remaining_mb() -> float:
    """MB left before the cap. float('inf') when no cap is configured."""
    cap = cap_mb()
    return float("inf") if cap <= 0 else max(0.0, cap - used_mb())


def percent_used() -> float:
    """Today's usage as a percent of the cap (0.0 when no cap is configured)."""
    cap = cap_mb()
    return 0.0 if cap <= 0 else 100.0 * used_mb() / cap


def is_exhausted() -> bool:
    """True once today's usage has reached the cap (always False if disabled)."""
    cap = cap_mb()
    return cap > 0 and used_mb() >= cap


def snapshot() -> dict:
    """Today's raw ledger (for listing/inspection)."""
    return _load()


# --------------------------------------------------------------------------- #
# Recording + alerting                                                        #
# --------------------------------------------------------------------------- #

def record(mb: float) -> float:
    """Add `mb` of billed proxy traffic to today's total; returns the new total.

    Then fires at most ONE Telegram message per threshold per day: a warning the
    first time usage crosses warn_at_percent, and a notice the first time it
    reaches the cap. Both flags live in the same file as the counter, so the
    'already sent' memory survives the process exiting between runs.
    """
    try:
        mb = float(mb)
    except (TypeError, ValueError):
        return used_mb()
    if mb <= 0:
        return used_mb()

    data = _load()
    data["used_mb"] = round(data["used_mb"] + mb, 3)

    cap = cap_mb()
    if cap > 0:
        pct = 100.0 * data["used_mb"] / cap
        threshold = warn_percent()
        # Cap first: a single route that vaults straight past both thresholds
        # should report the more serious one, not the warning it also crossed.
        if data["used_mb"] >= cap and not data["capped"]:
            data["capped"] = True
            data["warned"] = True     # the warning is moot once the cap is hit
            _save(data)
            _notify_cap(data["used_mb"], cap)
            return data["used_mb"]
        if threshold > 0 and pct >= threshold and not data["warned"]:
            data["warned"] = True
            _save(data)
            _notify_warning(data["used_mb"], cap, pct)
            return data["used_mb"]

    _save(data)
    return data["used_mb"]


def _notify(msg: str, level=logging.WARNING) -> None:
    """Log, then push to the Telegram error channel (best-effort, never raises —
    a bandwidth alert must not be able to break a run)."""
    logging.log(level, msg)
    try:
        from src.utils import telegram
        if telegram.is_error_configured():
            telegram.send_error(msg)
        else:
            logging.warning("Telegram error channel not configured — "
                            "bandwidth alert logged only.")
    except Exception as e:
        logging.warning(f"Could not send bandwidth alert to Telegram: {e}")


def _notify_warning(used: float, cap: float, pct: float) -> None:
    from src.utils import telegram_message
    _notify(telegram_message.bandwidth_warning(used, cap, pct))


def _notify_cap(used: float, cap: float) -> None:
    from src.utils import telegram_message
    _notify(telegram_message.bandwidth_cap_reached(used, cap), logging.ERROR)


def reset(day: str = None) -> None:
    """Clear today's counter and both alert flags (manual override / testing)."""
    _save(_blank(day))


if __name__ == "__main__":
    # Inspect / manage today's bandwidth budget:
    #   python -m src.utils.bandwidth_budget          # show today's usage
    #   python -m src.utils.bandwidth_budget reset    # clear it (re-enables runs)
    import sys

    from src.utils.config_reader import initialize_config
    initialize_config()

    argv = sys.argv[1:]
    if not argv:
        d = snapshot()
        cap = cap_mb()
        if cap <= 0:
            print(f"{d['date']}: {d['used_mb']:.1f} MB used (no cap configured).")
        else:
            bar = int(round(20 * min(1.0, d["used_mb"] / cap)))
            print(f"{d['date']}: {d['used_mb']:.1f} / {cap:.0f} MB "
                  f"({percent_used():.0f}%)  [{'#' * bar}{'.' * (20 - bar)}]")
            print(f"  remaining : {remaining_mb():.1f} MB")
            print(f"  warned    : {'yes' if d['warned'] else 'no'} "
                  f"(at {warn_percent():.0f}%)")
            print(f"  capped    : {'YES — routes paused' if d['capped'] else 'no'}")
    elif argv[0] == "reset":
        reset()
        print("Bandwidth budget reset — routes will run again today.")
    else:
        print("Usage: python -m src.utils.bandwidth_budget [reset]")
        sys.exit(2)
