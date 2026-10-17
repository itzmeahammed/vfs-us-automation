"""Multiple VFS credentials with hour-based, PER-ROUTE rotation.

You can run the bot under several VFS accounts and rotate them by the clock
hour. Not every account is registered on every portal, so each credential may
declare which routes it works on — and each ROUTE then rotates through its own
eligible subset, so no route is ever skipped because "this hour's account"
isn't registered there.

Accounts live in a dedicated, gitignored file `config/credentials.local.ini`:

    [cred1]
    email = a@example.com
    password = your-vfs-password-1          ; THE VFS PORTAL
    email_password = your-webmail-pw-1      ; THE MAILBOX. Both are REQUIRED.
    routes = AE-CHE, AE-CZE      ; registered ONLY on these routes

    [cred2]
    email = b@example.com
    password = your-vfs-password-2
    email_password = your-webmail-pw-2
    ; no "routes" key -> registered on ALL routes (the default)

    ═══════ TWO PASSWORDS, BECAUSE THERE ARE TWO SYSTEMS. BOTH REQUIRED. ══════

`email` is both a VFS login AND a real mailbox, and the bot signs in to both:

    password        THE VFS PORTAL. Typed into the browser at login.
    email_password  THE MAILBOX (Roundcube/IMAP). Used to read the emailed
                    one-time password on `otp: true` routes, and by
                    `python -m src.inbox watch`.

    ───────────────────── WHY THERE IS NO FALLBACK ─────────────────────

These were ONE value until 2026-10-16, because the two systems happened to
share a password. Rotating the webmail password broke every `otp: true` route
instantly — while VFS login carried on working, so the symptom was "the OTP
email never arrived" and the investigation went to the mail matcher, the clock
skew and the IMAP search before the password.

The obvious repair is `email_password or password`. That is REJECTED here on
purpose: a fallback makes a missing key indistinguishable from a deliberate
one, so the system silently resumes using the wrong credential the moment
someone adds an account and forgets the second line — reproducing the exact
bug, at the exact moment nobody is looking for it.

So an account is only usable when it declares BOTH. An account missing either
is DROPPED FROM THE POOL with a loud error naming it and the fix. Dropped, not
fatal: one malformed entry must not take every route offline with it.

Validate offline, before any browser starts:

    python -m src.utils.credentials check

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

from src.utils.config_reader import (
    commented_section_keys, get_config_section, get_config_value,
)

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
        mailbox = parser.get(section, "email_password", fallback="").strip()
        routes = _parse_routes(parser.get(section, "routes", fallback=""))

        if not (email and pwd):
            continue

        # BOTH PASSWORDS OR NOTHING. See the module docstring: falling back to
        # `password` here is what silently reintroduces the 2026-10-16 bug, so
        # an incomplete account is refused instead. Dropped rather than fatal —
        # one bad section must not take every route offline.
        if not mailbox:
            logging.error(
                f"{CREDENTIALS_FILE} [{section}] ({mask(email)}) has no "
                "'email_password' — the mailbox password used to read OTP "
                "emails. It is REQUIRED and is NOT assumed to equal "
                "'password'. This account is skipped until it is set. "
                "Run: python -m src.utils.credentials check")
            continue

        pool.append((email, pwd, routes))
    return pool


def _pool_sections() -> list:
    """[(section, email, password, email_password, routes_raw)] — every
    [credN] in the file, INCLUDING incomplete ones.

    _load_pool() deliberately hides broken accounts so a run keeps going;
    this is what check() needs in order to report them.
    """
    if not os.path.isfile(CREDENTIALS_FILE):
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(CREDENTIALS_FILE)
    except configparser.Error:
        return []
    out = []
    for section in parser.sections():
        out.append((
            section,
            parser.get(section, "email", fallback="").strip(),
            parser.get(section, "password", fallback="").strip(),
            parser.get(section, "email_password", fallback="").strip(),
            parser.get(section, "routes", fallback="").strip(),
        ))
    return out


def check() -> list:
    """Problems with the credentials file, as human-readable strings. PURE-ish.

    Offline and free: this is what to run after editing the file, and it is
    the difference between finding a missing mailbox password now and finding
    it when an OTP route fails at 06:00 with "no OTP email arrived".
    """
    problems = []
    sections = _pool_sections()
    if not sections:
        return [f"No accounts found in {CREDENTIALS_FILE}."]

    for section, email, pwd, mailbox, _routes in sections:
        where = f"[{section}]" + (f" ({mask(email)})" if email else "")
        if not email:
            problems.append(f"{where}: no 'email'.")
            continue
        if not pwd:
            problems.append(f"{where}: no 'password' (the VFS portal login).")
        if not mailbox:
            problems.append(
                f"{where}: no 'email_password' (the Roundcube/IMAP mailbox "
                "login). REQUIRED — it is not assumed to equal 'password'. "
                "Every otp:true route for this account will fail without it.")
        if pwd and mailbox and pwd == mailbox:
            # Not an error: they are allowed to be equal. Worth saying once,
            # because an account that still shares one password is the one
            # that breaks next time the webmail password is rotated.
            problems.append(
                f"{where}: NOTE — 'password' and 'email_password' are the "
                "same value. Legal, but this account breaks the next time "
                "only one of them is rotated.")
    return problems


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
    return [c for c in _eligible(pool, route) if not account_health.is_benched(c[0], route)]


def password_for(email: str) -> str:
    """The VFS PORTAL password for `email`, or ''.

    Not the mailbox password — see email_password_for(). Used by the --email
    test override to force a specific account.
    """
    key = (email or "").strip().lower()
    for e, pw, _ in _load_pool():
        if e.lower() == key:
            return pw
    # Guarded for the same reason as email_password_for(): get_config_value()
    # dereferences a parser that is None until something initialises it, and
    # this runs on the login path. '' means "not configured here", which every
    # caller already handles; an AttributeError would crash the sign-in.
    try:
        if key and key == (get_config_value("vfs-credential", "email")
                           or "").lower():
            return get_config_value("vfs-credential", "password") or ""
    except Exception as e:                                      # noqa: BLE001
        logging.debug(f"No [vfs-credential] password available: {e}")
    return ""


class MissingMailboxPassword(Exception):
    """An account has no `email_password`, so its mailbox cannot be read.

    Its own exception type because the ONLY correct response is a human
    editing the config. It must never be caught and turned into a retry, and
    it must never be quietly swallowed into "the OTP email has not arrived
    yet" — which is precisely the confusion the split exists to end.
    """


def email_password_for(email: str, required: bool = True) -> str:
    """The MAILBOX (Roundcube/IMAP) password for `email`.

    *** THERE IS NO FALLBACK TO THE VFS PASSWORD. *** See the module
    docstring. With `required` (the default) a missing one raises
    MissingMailboxPassword, naming the account and the fix, BEFORE any IMAP
    connection is attempted. Pass required=False for a diagnostic that wants
    to report rather than raise.

    Read straight from the file rather than through _load_pool(), which
    deliberately still yields (email, password, routes) triples — widening
    that tuple would break every caller that unpacks three values for a change
    none of them care about.
    """
    key = (email or "").strip().lower()
    if not key:
        if required:
            raise MissingMailboxPassword(
                "No email given, so no mailbox password can be looked up.")
        return ""

    for section, found, _pwd, mailbox, _routes in _pool_sections():
        if found.strip().lower() != key:
            continue
        if mailbox:
            return mailbox
        if required:
            raise MissingMailboxPassword(
                f"Account {mask(email)} ([{section}] in {CREDENTIALS_FILE}) "
                "has no 'email_password'. That is the Roundcube/IMAP mailbox "
                "password, and it is NOT assumed to be the same as the VFS "
                "portal password — they are separate secrets and were last "
                "rotated apart on 2026-10-16.\n"
                f"Add to [{section}]:  email_password = <webmail password>\n"
                "Then check: python -m src.utils.credentials check")
        return ""

    # Not in the pool at all. The single-account [vfs-credential] fallback is
    # wrapped because get_config_value() dereferences a parser that is None
    # until something initialises it, and this runs on the LOGIN path.
    try:
        if key == (get_config_value("vfs-credential", "email") or "").lower():
            mailbox = get_config_value("vfs-credential", "email_password")
            if mailbox:
                return mailbox
            if required:
                raise MissingMailboxPassword(
                    f"[vfs-credential] {mask(email)} has no 'email_password'. "
                    "Add it — the mailbox password is not assumed to equal "
                    "the VFS one.")
            return ""
    except MissingMailboxPassword:
        raise
    except Exception as e:                                      # noqa: BLE001
        logging.debug(f"No [vfs-credential] mailbox password available: {e}")

    if required:
        raise MissingMailboxPassword(
            f"No account named {mask(email)} is configured in "
            f"{CREDENTIALS_FILE}, so its mailbox password is unknown.")
    return ""


def eligible_emails(route: str = None) -> list:
    """
    Emails registered for `route`, IGNORING cooldown — lets a caller tell
    'no account registered' apart from 'all eligible accounts are benched'.
    """
    return [c[0] for c in _eligible(_load_pool(), route)]


def _sched():
    """(runs_per_hour, start_hour, end_hour) from [schedule], with safe defaults."""
    def _i(key, dflt):
        try:
            return int(str(get_config_value("schedule", key, str(dflt))).strip())
        except (ValueError, TypeError):
            return dflt
    return max(1, _i("runs_per_hour", 2)), _i("start_hour", 6), _i("end_hour", 24)


def run_index(dt=None) -> int:
    """
    The 0-based index of THIS run within the day's schedule, so the rotation can
    spread accounts across every run (not just every hour). Derived from the
    clock: run_index = (hour - start_hour) * runs_per_hour + slot-within-hour.

    With runs_per_hour=3 the 3 hourly runs (:00/:20/:40) get consecutive indices,
    so each fires a DIFFERENT account; each account then recurs every
    `num_accounts` runs — as widely spaced as possible.
    """
    from datetime import datetime
    dt = dt or datetime.now()
    rph, start_hour, _ = _sched()
    step = max(1, 60 // rph)
    slot = min(dt.minute // step, rph - 1)
    return max(0, dt.hour - start_hour) * rph + slot


def get_credential(route: str = None, dt=None) -> tuple:
    """
    Returns the (email, password) to use for `route` on THIS run, spread across
    the day's runs (see run_index). Skips benched/disabled accounts. Falls back
    to the single [vfs-credential] account when no pool file is configured.

    Returns (None, None) if the pool exists but no account is available for this
    route (none registered, or all benched).
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
        idx = run_index(dt) % len(available)
        email, pwd, _routes = available[idx]
        logging.info(
            f"Using credential {idx + 1}/{len(available)} available for "
            f"{route or 'any route'} (run #{run_index(dt)}): {_mask(email)}"
        )
        return email, pwd

    # Fallback: the original single account (eligible for every route).
    email = get_config_value("vfs-credential", "email")
    pwd = get_config_value("vfs-credential", "password")
    from src.utils import account_health
    if email and account_health.is_benched(email, route):
        logging.warning(f"Single account {_mask(email)} is in cooldown — skipping this run.")
        return None, None
    logging.info(f"Using single [vfs-credential] account: {_mask(email or '')}")
    return email, pwd


def active_account(route: str = None, dt=None) -> str:
    """
    Masked label for the account active on `route` this run — e.g.
    'pa***@travnook.com (cred 2/3)', or '' when none is available.
    """
    pool = _load_pool()
    if pool:
        available = _available(pool, route)
        if not available:
            return ""
        idx = run_index(dt) % len(available)
        return f"{_mask(available[idx][0])} (cred {idx + 1}/{len(available)})"
    email = get_config_value("vfs-credential", "email") or ""
    return _mask(email)


def warn_unknown_routes() -> None:
    """
    Logs a warning for every `routes` entry that names a route missing from
    [vfs-url] — catching typos (e.g. 'AE-CH') that would otherwise silently
    shrink a route's pool. Call once at startup.

    A route that EXISTS in [vfs-url] but is commented out (deliberately disabled,
    e.g. '; AE-FRA = ...') is NOT a typo — warning about it would spam the log
    every run while a route is paused. Those are logged at DEBUG instead, so only
    genuine typos surface as warnings.
    """
    known = {k.upper() for k in (get_config_section("vfs-url") or {})}
    if not known:
        return
    disabled = commented_section_keys("vfs-url")
    for email, _pwd, routes in _load_pool():
        for r in routes:
            if r in known:
                continue
            if r in disabled:
                logging.debug(
                    f"Credential {_mask(email)} lists disabled route '{r}' "
                    "(commented out in [vfs-url]) — skipped until re-enabled."
                )
                continue
            logging.warning(
                f"Credential {_mask(email)} lists unknown route '{r}' "
                f"(not in [vfs-url]) — probably a typo in {CREDENTIALS_FILE}."
            )


def rotation_schedule(route: str = None) -> list:
    """
    Preview of which account each RUN of the day uses for `route`, as a list of
    (time_str, run_index, masked_email) across the configured window. Uses the
    currently-available pool (benched accounts excluded). For verifying the
    spread after edits.
    """
    pool = _load_pool()
    avail = _available(pool, route) if pool else []
    rph, start_hour, end_hour = _sched()
    step = max(1, 60 // rph)
    out = []
    ri = 0
    for hour in range(start_hour, end_hour):
        for k in range(rph):
            if avail:
                who = avail[ri % len(avail)][0]   # full email (local preview)
            elif pool:
                who = "(no available account)"
            else:
                who = get_config_value("vfs-credential", "email") or ""
            out.append((f"{hour:02d}:{k * step:02d}", ri, who))
            ri += 1
    return out


# --------------------------------------------------------------------------- #
# `python -m src.utils.credentials check`                                      #
# --------------------------------------------------------------------------- #

def _main(argv=None) -> int:
    """Validate the credentials file offline. Prints no password, ever.

    Exists because the alternative place to discover a missing mailbox
    password is a 06:00 scheduled run reporting "no OTP email arrived".
    """
    import sys

    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] not in ("check", "-h", "--help"):
        print(f"usage: python -m src.utils.credentials check")
        return 2
    if argv[:1] in (["-h"], ["--help"]):
        print("usage: python -m src.utils.credentials check")
        return 0

    print()
    print("=" * 68)
    print("CREDENTIALS")
    print("=" * 68)
    print(f"file: {CREDENTIALS_FILE}")

    sections = _pool_sections()
    usable = _load_pool()
    print(f"accounts: {len(sections)} defined, {len(usable)} usable")
    print()

    for section, email, pwd, mailbox, routes in sections:
        marks = ("vfs " if pwd else "VFS? ") + ("mail" if mailbox else "MAIL?")
        state = "ok " if (pwd and mailbox and email) else "BAD"
        print(f"  [{state}] {section:8s} {mask(email) or '(no email)':22s} "
              f"{marks:10s} {routes or '(all routes)'}")

    problems = check()
    print()
    if not problems:
        print("No problems. Every account declares both passwords.")
        return 0

    print("-" * 68)
    for problem in problems:
        print(f"  {problem}")
    print("-" * 68)
    # NOTEs are advisory; only real problems fail the check.
    fatal = [p for p in problems if ": NOTE —" not in p]
    if fatal:
        print(f"{len(fatal)} problem(s). Accounts marked BAD are SKIPPED by "
              "the bot until fixed.")
        return 1
    print("Advisories only — nothing is broken.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
