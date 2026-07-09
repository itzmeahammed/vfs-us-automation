"""Multiple VFS credentials with hour-based, PER-ROUTE rotation.

You can run the bot under several VFS accounts and rotate them by the clock
hour. Not every account is registered on every portal, so each credential may
declare which routes it works on — and each ROUTE then rotates through its own
eligible subset, so no route is ever skipped because "this hour's account"
isn't registered there.

Accounts live in a dedicated, gitignored file `config/credentials.local.ini`:

    [cred1]
    email = a@example.com
    password = your-password-1
    routes = AE-CHE, AE-CZE      ; registered ONLY on these routes

    [cred2]
    email = b@example.com
    password = your-password-2
    ; no "routes" key -> registered on ALL routes (the default)

Rotation (per route): the route's eligible pool is every credential whose
`routes` list contains that route plus every credential without a list, in
file order. The active one is chosen by the clock hour, offset so the FIRST
eligible account is used at the first run hour of the day (06:00):

    index = (hour - START_HOUR) % len(eligible_pool)

Different routes may therefore use different accounts within the same hour —
that's fine, each route gets its own browser and its own login. Both runs
within an hour (e.g. :29 and :59) use the same account per route.

If the credentials file is absent or empty, this falls back to the single
[vfs-credential] account in config.ini / config.local.ini (eligible for every
route) — so existing setups keep working unchanged.
"""

import configparser
import logging
import os

from src.utils.config_reader import get_config_section, get_config_value

# The hour (local time) of the first scheduled run of the day. cred1 maps here.
START_HOUR = 6

CREDENTIALS_FILE = os.path.join("config", "credentials.local.ini")


def _mask(email: str) -> str:
    """Masks an email for logging: 'melicent@web.net' -> 'me***@web.net'."""
    if not email or "@" not in email:
        return "***"
    name, domain = email.split("@", 1)
    head = name[:2] if len(name) > 2 else name[:1]
    return f"{head}***@{domain}"


def mask(email: str) -> str:
    """Public alias of the email masker (for run summaries etc.)."""
    return _mask(email)


def _parse_routes(raw: str) -> frozenset:
    """
    Parses a cred's `routes` value ('AE-CHE, AE-CZE') into a normalized set of
    route keys. An empty/absent value yields an empty set, which means the
    credential is eligible for ALL routes.
    """
    return frozenset(
        token.strip().upper()
        for token in (raw or "").replace(";", ",").split(",")
        if token.strip()
    )


def _load_pool() -> list:
    """
    Reads all [credN] sections from the credentials file, in file order.

    Returns a list of (email, password, routes) tuples — `routes` is a
    frozenset of route keys the account is registered on, empty meaning "all
    routes". Returns [] if the file is missing, unreadable, or has no usable
    credentials.
    """
    if not os.path.isfile(CREDENTIALS_FILE):
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(CREDENTIALS_FILE)
    except configparser.Error as e:
        logging.warning(f"Could not parse {CREDENTIALS_FILE}: {e}")
        return []

    pool = []
    for section in parser.sections():
        email = parser.get(section, "email", fallback="").strip()
        pwd = parser.get(section, "password", fallback="").strip()
        routes = _parse_routes(parser.get(section, "routes", fallback=""))
        if email and pwd:
            pool.append((email, pwd, routes))
    return pool


def _eligible(pool: list, route: str) -> list:
    """
    The subset of `pool` eligible for `route` (keeps file order): creds whose
    routes set contains the route, plus creds with no routes restriction.
    With no route given, every credential is eligible (legacy behaviour).
    """
    if not route:
        return pool
    key = route.strip().upper()
    return [c for c in pool if not c[2] or key in c[2]]


def _available(pool: list, route: str) -> list:
    """
    The subset of `_eligible` that is NOT currently benched by the circuit
    breaker (see src/utils/account_health.py). This is what selection rotates
    through — a struggling/blocked account is skipped until its cooldown clears.
    """
    from src.utils import account_health  # lazy import to avoid a cycle
    return [c for c in _eligible(pool, route) if not account_health.is_benched(c[0])]


def eligible_emails(route: str = None) -> list:
    """
    Emails registered for `route`, IGNORING cooldown — lets a caller tell
    'no account registered' apart from 'all eligible accounts are benched'.
    """
    return [c[0] for c in _eligible(_load_pool(), route)]


def get_credential(hour: int, route: str = None) -> tuple:
    """
    Returns the (email, password) to use for the given clock `hour` (0-23) on
    the given `route` (e.g. 'AE-CHE').

    Rotates through the credentials ELIGIBLE for that route by hour. Falls
    back to the single [vfs-credential] account when no pool file is
    configured. Also logs which account is active (email masked).

    Returns:
        (email, password). (None, None) if the pool exists but NO credential
        is eligible for this route — the caller should skip the route. Either
        may be None if nothing is configured at all.
    """
    pool = _load_pool()

    if pool:
        available = _available(pool, route)
        if not available:
            eligible = _eligible(pool, route)
            if eligible:
                logging.warning(
                    f"All {len(eligible)} account(s) for '{route}' are in cooldown "
                    f"(circuit breaker) — none available this run."
                )
            else:
                logging.warning(
                    f"No credential is registered for route '{route}' "
                    f"(checked {len(pool)} account(s) in {CREDENTIALS_FILE})."
                )
            return None, None
        idx = (hour - START_HOUR) % len(available)
        email, pwd, _routes = available[idx]
        logging.info(
            f"Using credential {idx + 1}/{len(available)} available for "
            f"{route or 'any route'} at hour {hour:02d}: {_mask(email)}"
        )
        return email, pwd

    # Fallback: the original single account (eligible for every route).
    email = get_config_value("vfs-credential", "email")
    pwd = get_config_value("vfs-credential", "password")
    from src.utils import account_health
    if email and account_health.is_benched(email):
        logging.warning(f"Single account {_mask(email)} is in cooldown — skipping this run.")
        return None, None
    logging.info(f"Using single [vfs-credential] account: {_mask(email or '')}")
    return email, pwd


def active_account(hour: int, route: str = None) -> str:
    """
    Returns a masked, human-friendly label for the account active at `hour`
    on `route`, for display in the run summary — e.g.
    'pa***@travnook.com (cred 2/3)', or '' when no credential is eligible.
    """
    pool = _load_pool()
    if pool:
        available = _available(pool, route)
        if not available:
            return ""
        idx = (hour - START_HOUR) % len(available)
        return f"{_mask(available[idx][0])} (cred {idx + 1}/{len(available)})"
    email = get_config_value("vfs-credential", "email") or ""
    return _mask(email)


def warn_unknown_routes() -> None:
    """
    Logs a warning for every `routes` entry that names a route missing from
    [vfs-url] — catching typos (e.g. 'AE-CH') that would otherwise silently
    shrink a route's pool. Call once at startup.
    """
    known = {k.upper() for k in (get_config_section("vfs-url") or {})}
    if not known:
        return
    for email, _pwd, routes in _load_pool():
        for r in routes:
            if r not in known:
                logging.warning(
                    f"Credential {_mask(email)} lists unknown route '{r}' "
                    f"(not in [vfs-url]) — probably a typo in {CREDENTIALS_FILE}."
                )


def rotation_schedule(route: str = None) -> list:
    """
    Returns a preview of which account is used each active hour (06:00-23:00)
    for `route` (or ignoring route restrictions when omitted), as a list of
    (hour, index, masked_email). For verifying the rotation after edits.
    """
    pool = _load_pool()
    eligible = _eligible(pool, route) if pool else []
    out = []
    for hour in range(START_HOUR, 24):
        if eligible:
            idx = (hour - START_HOUR) % len(eligible)
            out.append((hour, idx + 1, _mask(eligible[idx][0])))
        elif pool:
            out.append((hour, 0, "(no eligible credential)"))
        else:
            email = get_config_value("vfs-credential", "email") or ""
            out.append((hour, 1, _mask(email)))
    return out
