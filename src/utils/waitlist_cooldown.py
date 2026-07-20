"""Per-country cooldown for waitlist notifications.

After a waitlist message is sent for a destination country, further waitlist
messages for that SAME country are suppressed for `cooldown_hours` (default 2) —
so a country that sits on the waitlist for a whole day doesn't spam the chat
every 30-minute run.

State is a tiny gitignored JSON map {COUNTRY_CODE: last_sent_epoch} at the
project root. It lives on disk (not in memory) because each scheduled run is its
OWN process — an in-memory guard would reset every single run. This mirrors
account_health.py deliberately: same atomic-write JSON pattern, right-sized for
this app (a handful of countries, a few messages per run).

Scaling note — if this ever outgrows a single machine or needs many concurrent
writers, the natural next steps are: SQLite (single file, ACID, real locking) to
keep zero infra while gaining safe concurrent writes; or Redis modelling each
cooldown as a native TTL key (`SET <country> 1 EX 7200 NX`) so expiry is
automatic and the check-and-set is atomic. Both keep the same {country -> until}
mental model; only the store changes.

Config knob: [waitlist] cooldown_hours in config.ini.
"""

import json
import logging
import os
import time

from src.utils.config_reader import get_config_value

STATE_FILE = "waitlist_cooldown.json"
DEFAULT_COOLDOWN_HOURS = 2.0


def cooldown_hours() -> float:
    """Cooldown window in hours ([waitlist] cooldown_hours; default 2). 0 disables."""
    try:
        return max(0.0, float(
            str(get_config_value("waitlist", "cooldown_hours", DEFAULT_COOLDOWN_HOURS)).strip()
        ))
    except (ValueError, TypeError):
        return DEFAULT_COOLDOWN_HOURS


def _key(country: str) -> str:
    """Normalise a destination code so 'ita'/'ITA'/' ITA ' share one cooldown."""
    return (country or "").strip().upper()


def _load() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (FileNotFoundError, ValueError, OSError):
        # Fail-open: an unreadable/missing store means 'no cooldown', so a real
        # waitlist alert is never silently swallowed by a corrupt file.
        return {}


def _save(data: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)  # atomic on the same filesystem
    except OSError as e:
        logging.warning(f"Could not persist waitlist cooldown to {STATE_FILE}: {e}")


def is_on_cooldown(country: str, now: float = None) -> bool:
    """
    True if a waitlist message for `country` was sent within the cooldown window
    (so the caller should skip sending). False if never sent, expired, or the
    cooldown is disabled (0 hours).
    """
    window = cooldown_hours() * 3600
    if window <= 0:
        return False
    k = _key(country)
    if not k:
        return False
    now = time.time() if now is None else now
    data = _load()
    if k not in data:                       # never sent -> not on cooldown
        return False
    return (now - float(data[k])) < window


def seconds_left(country: str, now: float = None) -> float:
    """Seconds remaining on `country`'s cooldown (0 if not on cooldown)."""
    k = _key(country)
    if not k:
        return 0.0
    now = time.time() if now is None else now
    data = _load()
    if k not in data:
        return 0.0
    return max(0.0, cooldown_hours() * 3600 - (now - float(data[k])))


def record_sent(country: str, now: float = None) -> None:
    """
    Mark that a waitlist message for `country` was just sent, starting its
    cooldown. Also prunes entries whose cooldown has already expired, so the file
    stays bounded as the number of countries grows.
    """
    k = _key(country)
    if not k:
        return
    now = time.time() if now is None else now
    window = cooldown_hours() * 3600
    data = _load()
    data[k] = now
    # Keep only countries still within their cooldown (plus the one we just set).
    data = {c: t for c, t in data.items()
            if c == k or (window > 0 and (now - float(t)) < window)}
    _save(data)


def clear(country: str = None) -> None:
    """Clear one country's cooldown, or all of them when country is None."""
    if country is None:
        _save({})
        return
    data = _load()
    if _key(country) in data:
        del data[_key(country)]
        _save(data)


def snapshot() -> dict:
    """Raw {country: last_sent_epoch} map (for listing/inspection)."""
    return _load()


if __name__ == "__main__":
    # Inspect / manage waitlist cooldowns:
    #   python -m src.utils.waitlist_cooldown            # list active cooldowns
    #   python -m src.utils.waitlist_cooldown clear ITA
    #   python -m src.utils.waitlist_cooldown clear-all
    import sys
    from datetime import datetime

    from src.utils.config_reader import initialize_config
    initialize_config()

    argv = sys.argv[1:]
    if not argv:
        data = snapshot()
        if not data:
            print("No waitlist cooldowns active.")
        for country, last in sorted(data.items()):
            left = seconds_left(country)
            when = datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M")
            state = f"{left / 60:.0f} min left" if left > 0 else "expired"
            print(f"{country}  ->  last sent {when}  ({state})")
    elif argv[0] == "clear" and len(argv) == 2:
        clear(argv[1])
        print(f"Cleared cooldown for {argv[1].upper()}.")
    elif argv[0] == "clear-all":
        clear()
        print("All waitlist cooldowns cleared.")
    else:
        print("Usage: python -m src.utils.waitlist_cooldown [clear <COUNTRY> | clear-all]")
        sys.exit(2)
