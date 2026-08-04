"""Persistent per-account, PER-ROUTE health / circuit breaker.

Protects VFS accounts from being hammered into a temporary block or a permanent
ban. VFS runs a SEPARATE portal per destination country, so a block on one
portal (Norway) usually does NOT affect the same account on another (Switzerland).
Health is therefore tracked per `(email, route)`, EXCEPT for account-global states
(wrong credentials / 429002 unauthorised), which disable the account everywhere.

Record shape (keyed by email):

    {
      "disabled": true, "disabled_at": <epoch>, "disabled_reason": <str>,  # GLOBAL
      "routes": {
        "AE-NOR": {"cooldown_until": <epoch>, "fails": <int>, "last_reason": <str>,
                   "updated_at": <epoch>}
      }
    }

- A ROUTE's `cooldown_until` — while now < this, the account is BENCHED FOR THAT
  ROUTE only: credential selection for that route skips it; other routes are
  unaffected.
- A ROUTE's `fails` — consecutive failed runs on that route; reset to 0 on a
  success there.
- Top-level `disabled` — account-global, indefinite, needs a human fix + clear.
- Top-level `cooldown_until` — a legacy GLOBAL cooldown (only produced when
  migrating old route-less records); new code never writes it.

Two ways an account gets benched on a route:
  * HARD block  — a 429001 'access restricted' or 429202 'account locked' page:
                  benched on that route for hard_cooldown_hours at once.
  * SOFT strike — a run that stayed stuck for any reason (Turnstile never passed,
                  Sign In disabled, dashboard not reached, session expired, OTP
                  failed, timeouts): after fail_threshold consecutive strikes ON
                  THAT ROUTE, benched on that route for soft_cooldown_hours.

Wrong credentials / 429002 call `disable()` — an account-GLOBAL indefinite skip.

Old flat records (no "routes" key) are migrated on read: a `disabled` record
stays a global disable; a route-less cooldown becomes a legacy global cooldown.

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


def _norm_route(route: str) -> str:
    return (route or "").strip().upper()


def _normalize(rec: dict) -> dict:
    """Return `rec` in the new per-route shape, migrating a legacy flat record.

    New records already have a "routes" dict and are returned as-is. A legacy
    record (top-level cooldown_until/fails/disabled, no "routes") is converted: a
    disable stays global; a route-less cooldown becomes a legacy global cooldown
    (kept until it expires so we never accidentally un-bench). Route-less strikes
    can't be attributed to a route, so they're dropped (safe — nothing benched)."""
    if not isinstance(rec, dict):
        return {"routes": {}}
    if isinstance(rec.get("routes"), dict):
        rec.setdefault("routes", {})
        return rec
    out = {"routes": {}}
    if rec.get("disabled"):
        out["disabled"] = True
        out["disabled_at"] = rec.get("disabled_at", int(time.time()))
        out["disabled_reason"] = rec.get("disabled_reason") or rec.get("last_reason", "")
    elif rec.get("cooldown_until", 0):
        out["cooldown_until"] = rec.get("cooldown_until", 0)
        out["last_reason"] = rec.get("last_reason", "")
    return out


def _load() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return {email: _normalize(rec) for email, rec in raw.items()}


def _save(data: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)  # atomic on the same filesystem
    except OSError as e:
        logging.warning(f"Could not persist account health to {STATE_FILE}: {e}")


def _rec(data: dict, email: str) -> dict:
    rec = data.setdefault(email, {"routes": {}})
    rec.setdefault("routes", {})
    return rec


def _route_rec(data: dict, email: str, route: str) -> dict:
    routes = _rec(data, email)["routes"]
    return routes.setdefault(_norm_route(route),
                             {"cooldown_until": 0, "fails": 0, "last_reason": ""})


def _prune(data: dict, email: str) -> None:
    """Drop an account's record entirely if nothing benches it anymore."""
    rec = data.get(email)
    if rec and not rec.get("disabled") and not rec.get("cooldown_until") \
            and not rec.get("routes"):
        del data[email]


def is_benched(email: str, route: str = None) -> bool:
    """
    True if `email` should be skipped by selection FOR `route` — an indefinite
    manual DISABLE (account-global), a legacy global cooldown, or an active
    cooldown on this specific route. With no route, only the global states count.
    """
    if not email:
        return False
    rec = _load().get(email)
    if not rec:
        return False
    if rec.get("disabled"):
        return True
    now = time.time()
    if rec.get("cooldown_until", 0) > now:          # legacy global cooldown
        return True
    key = _norm_route(route)
    if key:
        r = rec.get("routes", {}).get(key)
        if r and r.get("cooldown_until", 0) > now:
            return True
    return False


def benched_until(email: str, route: str = None) -> float:
    """Epoch when `email`'s bench for `route` ends (0 if not benched). Takes the
    later of any global cooldown and the route's own cooldown."""
    rec = _load().get(email or "")
    if not rec:
        return 0.0
    vals = [float(rec.get("cooldown_until", 0) or 0)]
    key = _norm_route(route)
    if key:
        r = rec.get("routes", {}).get(key)
        if r:
            vals.append(float(r.get("cooldown_until", 0) or 0))
    return max(vals)


def bench(email: str, route: str, hours: int, reason: str) -> None:
    """Bench `email` on `route` for `hours` immediately (a hard block), clearing
    that route's strikes. Other routes are unaffected."""
    if not email:
        return
    data = _load()
    r = _route_rec(data, email, route)
    r["cooldown_until"] = time.time() + hours * 3600
    r["fails"] = 0
    r["last_reason"] = reason
    r["updated_at"] = int(time.time())
    _save(data)
    logging.warning(f"Account {_mask(email)} benched {hours}h on {route} — {reason}.")


def record_success(email: str, route: str = None) -> None:
    """Clear `email`'s strikes/cooldown FOR `route` (it's healthy there again),
    plus any legacy global cooldown. Does NOT lift an account-global disable."""
    if not email:
        return
    data = _load()
    rec = data.get(email)
    if not rec:
        return
    changed = False
    if rec.get("cooldown_until", 0):                 # legacy global cooldown
        rec.pop("cooldown_until", None)
        rec.pop("last_reason", None)
        changed = True
    key = _norm_route(route)
    routes = rec.get("routes", {})
    if key and key in routes and (routes[key].get("fails")
                                  or routes[key].get("cooldown_until")):
        del routes[key]
        changed = True
    if changed:
        _prune(data, email)
        _save(data)


def record_failure(email: str, route: str, reason: str) -> bool:
    """
    Count a consecutive strike for `email` ON `route`; soft-bench that route if it
    reaches the threshold. Returns True if the route got benched by this strike.
    """
    if not email:
        return False
    data = _load()
    r = _route_rec(data, email, route)
    r["fails"] = int(r.get("fails", 0)) + 1
    r["last_reason"] = reason
    r["updated_at"] = int(time.time())
    threshold = fail_threshold()
    if r["fails"] >= threshold:
        hrs = soft_cooldown_hours()
        r["cooldown_until"] = time.time() + hrs * 3600
        r["fails"] = 0
        _save(data)
        logging.warning(
            f"Account {_mask(email)} benched {hrs}h on {route} after {threshold} "
            f"consecutive failures — {reason}."
        )
        return True
    _save(data)
    logging.info(
        f"Account {_mask(email)} strike {r['fails']}/{threshold} on {route} — {reason}."
    )
    return False


def disable(email: str, reason: str) -> None:
    """
    Disable `email` INDEFINITELY on ALL routes — for an account-global state that
    needs a human fix, not a timed cooldown: wrong credentials, or VFS's 'Access
    Denied Due to Unauthorised Activity (429002)'. The account stays skipped
    everywhere until someone fixes it and runs `clear()` (flags it healthy).
    """
    if not email:
        return
    data = _load()
    rec = _rec(data, email)
    rec["disabled"] = True
    rec["disabled_at"] = int(time.time())
    rec["disabled_reason"] = reason
    _save(data)
    logging.error(
        f"Account {_mask(email)} DISABLED on all routes until manually cleared — {reason}."
    )


def is_disabled(email: str) -> bool:
    """True if `email` is under an indefinite account-global manual disable."""
    rec = _load().get(email or "")
    return bool(rec and rec.get("disabled"))


def clear(email: str, route: str = None) -> bool:
    """
    Flag an account HEALTHY again. With `route`, clear only that route's cooldown/
    strikes (leaving other routes and any global disable intact). Without a route,
    remove the whole record (clears a global disable and every route). Returns
    True if anything was removed.
    """
    data = _load()
    if email not in data:
        return False
    if route:
        key = _norm_route(route)
        routes = data[email].get("routes", {})
        if key not in routes:
            return False
        del routes[key]
        _prune(data, email)
        _save(data)
        logging.warning(f"Account {_mask(email)} cleared on {key} — flagged healthy.")
        return True
    del data[email]
    _save(data)
    logging.warning(f"Account {_mask(email)} cleared on all routes — flagged healthy.")
    return True


def snapshot() -> dict:
    """Returns the raw health map, normalized to the per-route shape."""
    return _load()


if __name__ == "__main__":
    # Small CLI to inspect and manage account health:
    #   python -m src.utils.account_health                     # list all records
    #   python -m src.utils.account_health clear <email>            # all routes
    #   python -m src.utils.account_health clear <email> <route>    # one route
    #   python -m src.utils.account_health clear-all
    #   python -m src.utils.account_health bench <email> <route> [hours]
    import sys
    from datetime import datetime

    from src.utils.config_reader import initialize_config
    initialize_config()

    def _fmt(ts):
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    args = sys.argv[1:]
    if not args:
        data = snapshot()
        if not data:
            print("No account-health records — all accounts healthy.")
        now = time.time()
        for email, rec in data.items():
            label = _mask(email)
            printed = False
            if rec.get("disabled"):
                print(f"{label}  ->  DISABLED (all routes, manual clear needed)   "
                      f"[{rec.get('disabled_reason', '')}]")
                printed = True
            if rec.get("cooldown_until", 0) > now:      # legacy global cooldown
                print(f"{label}  ->  cooldown ALL routes until "
                      f"{_fmt(rec['cooldown_until'])}   [{rec.get('last_reason', '')}]")
                printed = True
            for rt, r in sorted(rec.get("routes", {}).items()):
                if r.get("cooldown_until", 0) > now:
                    state = f"cooldown until {_fmt(r['cooldown_until'])}"
                else:
                    state = f"ok (strikes: {r.get('fails', 0)})"
                print(f"{label}  [{rt}]  ->  {state}   [{r.get('last_reason', '')}]")
                printed = True
            if not printed:
                print(f"{label}  ->  ok")
    elif args[0] == "clear" and len(args) in (2, 3):
        route = args[2] if len(args) == 3 else None
        print("Cleared." if clear(args[1], route) else "No such record.")
    elif args[0] == "clear-all":
        _save({})
        print("All account-health records cleared.")
    elif args[0] == "bench" and len(args) in (3, 4):
        route = args[2]
        hours = int(args[3]) if len(args) == 4 else hard_cooldown_hours()
        bench(args[1], route, hours, "manual bench")
        until = _fmt(benched_until(args[1], route))
        print(f"Benched {args[1]} on {route} for {hours}h (until {until}).")
    else:
        print("Usage: python -m src.utils.account_health "
              "[clear <email> [route] | clear-all | bench <email> <route> [hours]]")
        sys.exit(2)
