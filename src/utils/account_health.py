"""Persistent per-account health / circuit breaker.

Protects VFS accounts from being hammered into a temporary block or a permanent
ban. Each account (keyed by email) has a small record:

    {"cooldown_until": <epoch>, "fails": <int>, "last_reason": <str>}

- `cooldown_until` — while now < this, the account is BENCHED: credential
  selection skips it and rotates to the next available account.
- `fails` — consecutive failed runs; reset to 0 on any success.

Two ways an account gets benched:
  * HARD block  — a 429001 'access restricted' or 429202 'account locked' page,
                  or wrong credentials: benched for hard_cooldown_hours at once.
  * SOFT strike — a run that stayed stuck for any reason (Turnstile never passed,
                  Sign In disabled, dashboard not reached, OTP failed, timeouts):
                  after fail_threshold consecutive strikes, benched for
                  soft_cooldown_hours.

State lives in a gitignored JSON file at the project root so it SURVIVES across
the separate scheduled processes (each :29/:59 run is its own process).

Config knobs live in [account_safety] in config.ini.
"""

import json
import logging
import os
import time

from src.utils.config_reader import get_config_value

STATE_FILE = "account_health.json"


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(str(get_config_value("account_safety", key, str(default))).strip())
    except (ValueError, TypeError):
        return default


def hard_cooldown_hours() -> int:
    return _cfg_int("hard_cooldown_hours", 2)


def soft_cooldown_hours() -> int:
    return _cfg_int("soft_cooldown_hours", 2)


def fail_threshold() -> int:
    return max(1, _cfg_int("fail_threshold", 3))


def _mask(email: str) -> str:
    # Lazy import to avoid a circular import at module load.
    from src.utils.credentials import mask
    return mask(email or "")


def _load() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save(data: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)  # atomic on the same filesystem
    except OSError as e:
        logging.warning(f"Could not persist account health to {STATE_FILE}: {e}")


def _rec(data: dict, email: str) -> dict:
    return data.setdefault(email, {"cooldown_until": 0, "fails": 0, "last_reason": ""})


def is_benched(email: str) -> bool:
    """
    True if `email` should be skipped by selection — either an indefinite manual
    DISABLE (wrong creds / 429002, needs a human fix + clear) or an active
    time-based cooldown.
    """
    if not email:
        return False
    rec = _load().get(email)
    if not rec:
        return False
    return bool(rec.get("disabled")) or rec.get("cooldown_until", 0) > time.time()


def benched_until(email: str) -> float:
    """Epoch when `email`'s cooldown ends (0 if not benched)."""
    rec = _load().get(email or "")
    return float(rec.get("cooldown_until", 0)) if rec else 0.0


def bench(email: str, hours: int, reason: str) -> None:
    """Bench `email` for `hours` immediately (a hard block), clearing its strikes."""
    if not email:
        return
    data = _load()
    rec = _rec(data, email)
    rec["cooldown_until"] = time.time() + hours * 3600
    rec["fails"] = 0
    rec["last_reason"] = reason
    _save(data)
    logging.warning(f"Account {_mask(email)} benched {hours}h — {reason}.")


def record_success(email: str) -> None:
    """Clear an account's strikes and any cooldown — it's healthy again."""
    if not email:
        return
    data = _load()
    if email in data and (data[email].get("fails") or data[email].get("cooldown_until")):
        data[email] = {"cooldown_until": 0, "fails": 0, "last_reason": ""}
        _save(data)


def record_failure(email: str, reason: str) -> bool:
    """
    Count a consecutive strike for `email`; bench it (soft) if it reaches the
    threshold. Returns True if the account got benched by this strike.
    """
    if not email:
        return False
    data = _load()
    rec = _rec(data, email)
    rec["fails"] = int(rec.get("fails", 0)) + 1
    rec["last_reason"] = reason
    threshold = fail_threshold()
    if rec["fails"] >= threshold:
        hrs = soft_cooldown_hours()
        rec["cooldown_until"] = time.time() + hrs * 3600
        rec["fails"] = 0
        _save(data)
        logging.warning(
            f"Account {_mask(email)} benched {hrs}h after {threshold} "
            f"consecutive failures — {reason}."
        )
        return True
    _save(data)
    logging.info(f"Account {_mask(email)} strike {rec['fails']}/{threshold} — {reason}.")
    return False


def disable(email: str, reason: str) -> None:
    """
    Disable `email` INDEFINITELY — for a state that needs a human fix, not a
    timed cooldown: wrong credentials, or VFS's 'Access Denied Due to
    Unauthorised Activity (429002)'. The account stays skipped until someone
    fixes it and runs `clear()` (flags it healthy).
    """
    if not email:
        return
    data = _load()
    rec = _rec(data, email)
    rec["disabled"] = True
    rec["cooldown_until"] = 0
    rec["fails"] = 0
    rec["last_reason"] = reason
    rec["disabled_at"] = int(time.time())
    _save(data)
    logging.error(f"Account {_mask(email)} DISABLED until manually cleared — {reason}.")


def is_disabled(email: str) -> bool:
    """True if `email` is under an indefinite manual disable."""
    rec = _load().get(email or "")
    return bool(rec and rec.get("disabled"))


def clear(email: str) -> bool:
    """
    Flag an account HEALTHY again: remove its record (clears a disable or a
    cooldown). Returns True if a record was removed.
    """
    data = _load()
    if email in data:
        del data[email]
        _save(data)
        logging.warning(f"Account {_mask(email)} cleared — flagged healthy.")
        return True
    return False


def snapshot() -> dict:
    """Returns the raw health map (for listing/inspection)."""
    return _load()


if __name__ == "__main__":
    # Small CLI to inspect and manage account health:
    #   python -m src.utils.account_health              # list all records
    #   python -m src.utils.account_health clear <email>
    #   python -m src.utils.account_health clear-all
    import sys
    from datetime import datetime

    from src.utils.config_reader import initialize_config
    initialize_config()

    args = sys.argv[1:]
    if not args:
        data = snapshot()
        if not data:
            print("No account-health records — all accounts healthy.")
        for email, rec in data.items():
            if rec.get("disabled"):
                state = "DISABLED (manual clear needed)"
            elif rec.get("cooldown_until", 0) > time.time():
                until = datetime.fromtimestamp(rec["cooldown_until"]).strftime("%Y-%m-%d %H:%M")
                state = f"cooldown until {until}"
            else:
                state = f"ok (strikes: {rec.get('fails', 0)})"
            print(f"{_mask(email)}  ->  {state}   [{rec.get('last_reason', '')}]")
    elif args[0] == "clear" and len(args) == 2:
        print("Cleared." if clear(args[1]) else "No such record.")
    elif args[0] == "clear-all":
        _save({})
        print("All account-health records cleared.")
    else:
        print("Usage: python -m src.utils.account_health [clear <email> | clear-all]")
        sys.exit(2)
