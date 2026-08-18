"""Which VFS account a waitlist registration logs in as.

This is deliberately SEPARATE from src/utils/credentials.py, which rotates
accounts hourly for slot checking. Rotation is right for checking (spread the
load, use each account as rarely as possible) and WRONG for waitlisting:

    a waitlist entry belongs to the ACCOUNT it was created under.

If Ahmed is waitlisted under acc1 today and a later run picks acc2, his entry is
invisible from acc2 — you cannot see it, cannot cancel it, and may well register
him a second time. So the account must be a stable, deliberate choice, never
whatever the clock happened to land on.

Waitlist accounts are a SEPARATE POOL from the slot-check accounts in
config/credentials.local.ini. Nothing here ever reads that file: not the email,
not the password. Borrowing a slot-check account would both scatter waitlist
entries across a rotating set of logins and quietly put registration traffic on
accounts whose health the supervisor manages for a different purpose.

Resolution order (first hit wins):

    1. --email + --password on the command line  you override, always wins
    2. "account" + "account_password" in the      pin a client to an account
       client file
    3. [waitlist] account + account_password      one default for all waitlisting
       in config.local.ini
    -- and nothing else --

Each source must supply BOTH the email and its password; there is no lookup
anywhere else. If none of the three is set the run STOPS with an actionable
error, because silently falling back to a rotating slot-check account is exactly
the failure this module exists to prevent.

Sharing an account across clients is allowed and configurable
([waitlist] max_clients_per_account), because VFS's real limit is unknown; see
capacity_verdict() for how that is enforced.
"""

import logging
from typing import List, Tuple

from src.settings import settings
from src.utils.config_reader import get_config_value, initialize_config
from src.waitlist import journal
from src.waitlist.errors import WaitlistConfigError
from src.waitlist.result import Status


def mask(email: str) -> str:
    """'acc1@example.com' -> 'ac***@example.com', for logs and Telegram.

    Deliberately a local copy of credentials.mask() rather than an import: this
    package has NO dependency on the slot-check credential machinery, and a
    display helper is not worth reintroducing one.
    """
    email = (email or "").strip()
    local, sep, domain = email.partition("@")
    if not sep:
        return (local[:2] + "***") if len(local) > 2 else "***"
    keep = local[:2] if len(local) > 2 else local[:1]
    return f"{keep}***{sep}{domain}"


class Account:
    """A resolved (email, password) plus where it came from, for logging.

    `proxy` is an optional per-client exit-IP pin (see resolve_proxy).
    """

    def __init__(self, email: str, password: str, source: str, proxy: str = ""):
        self.email = email
        self.password = password
        self.source = source
        self.proxy = proxy or ""

    @property
    def masked(self) -> str:
        return mask(self.email)

    def __repr__(self) -> str:   # never leak the password
        return f"<Account {self.masked} from {self.source}>"


def resolve(registrant, cli_email: str = None,
            cli_password: str = None) -> Account:
    """
    Returns the Account this registration must use, or raises with guidance.

    Each source must supply BOTH the email and its password — waitlist accounts
    are a separate pool, so nothing is ever looked up in
    config/credentials.local.ini.
    """
    # 1 — command line wins.
    if cli_email:
        if not cli_password:
            raise WaitlistConfigError(
                f"--email {cli_email} needs --password too. Waitlist accounts "
                "are separate from the slot-check accounts in "
                "config/credentials.local.ini, so no password is looked up "
                "there.")
        return Account(cli_email, cli_password, "--email")

    # 2 — pinned in the client file.
    pinned = (registrant.account or "").strip()
    if pinned:
        if not registrant.account_password:
            raise WaitlistConfigError(
                f"Client '{registrant.id}' pins account '{pinned}' but has no "
                "\"account_password\". Add it to "
                f"config/registrants/{registrant.id}.json — waitlist accounts "
                "are a separate pool, so the password is never taken from "
                "config/credentials.local.ini.")
        return Account(pinned, registrant.account_password,
                       f"client file ({registrant.id})",
                       proxy=getattr(registrant, "proxy", ""))

    # 3 — the shared waitlist default.
    # Idempotent; guarantees the INI is loaded even when resolve() is called
    # outside the CLI (a test, the config editor, an interactive session).
    initialize_config()
    default_email = (get_config_value("waitlist", "account", "") or "").strip()
    if default_email:
        password = (
            get_config_value("waitlist", "account_password", "") or "").strip()
        if not password:
            raise WaitlistConfigError(
                f"[waitlist] account = {default_email} is set but "
                "[waitlist] account_password is empty. Set it in "
                "config/config.local.ini — waitlist accounts are a separate "
                "pool, so the password is never taken from "
                "config/credentials.local.ini.")
        return Account(default_email, password, "[waitlist] account")

    # No fallback to the slot-check credentials — by design.
    raise WaitlistConfigError(
        f"No VFS account is configured for waitlisting client "
        f"'{registrant.id}'.\n"
        "Set ONE of (each needs BOTH the email and its password):\n"
        f"  · \"account\" + \"account_password\" in "
        f"config/registrants/{registrant.id}.json (pins this client), or\n"
        "  · [waitlist] account + account_password in config/config.local.ini "
        "(one default for all waitlisting), or\n"
        "  · --email + --password on the command line (one-off).\n"
        "Waitlist accounts are a SEPARATE POOL from the slot-check accounts in "
        "config/credentials.local.ini — nothing is read from there. A waitlist "
        "entry belongs to the account that created it, so the account must be a "
        "stable, deliberate choice, never the hourly rotation."
    )


# --------------------------------------------------------------------------- #
# Capacity — how many clients may share one account                            #
# --------------------------------------------------------------------------- #

def clients_on(email: str) -> List[str]:
    """Distinct clients that already hold a registration under this account.

    Read from the journal, counting only entries that actually reached VFS
    (Status.COMMITTED_STATES) — dry runs and guard skips never occupied a slot.
    """
    want = (email or "").strip().lower()
    seen = []
    for row in journal.entries():
        if (row.get("account") or "").strip().lower() != want:
            continue
        if row.get("status") not in Status.COMMITTED_STATES:
            continue
        client = row.get("registrant_id")
        if client and client not in seen:
            seen.append(client)
    return seen


def clients_on_combo(email: str, route: str, combo: str) -> List[str]:
    """Clients already registered under this account for this SAME combination.

    VFS's real limit here is unknown — an account may or may not be able to hold
    two waitlist entries for one combination. This is what
    [waitlist] one_client_per_account_combo guards against.
    """
    want_account = (email or "").strip().lower()
    key = journal._key(route, combo, "")[:2]
    seen = []
    for row in journal.entries():
        if (row.get("account") or "").strip().lower() != want_account:
            continue
        if row.get("status") not in Status.COMMITTED_STATES:
            continue
        if journal._key(row.get("route"), row.get("combo"), "")[:2] != key:
            continue
        client = row.get("registrant_id")
        if client and client not in seen:
            seen.append(client)
    return seen


def capacity_verdict(account: Account, registrant, route: str,
                     combo: str) -> Tuple[bool, str]:
    """
    Checks whether `account` may take one more registration.

    Returns (allowed, reason). Two independent limits, both configurable because
    VFS's actual behaviour is not documented anywhere we can see:

      max_clients_per_account       total distinct clients per account
                                    (0 = unlimited)
      one_client_per_account_combo  whether an account may hold TWO entries for
                                    the SAME combination. Defaults to warn-only:
                                    we do not yet know it is a problem, and
                                    blocking wrongly is worse than a warning.
    """
    cfg = settings().waitlist

    # -- same account, same combination, different client --------------------
    others = [c for c in clients_on_combo(account.email, route, combo)
              if c != registrant.id]
    if others:
        message = (
            f"account {account.masked} already holds a waitlist entry for "
            f"'{combo}' (client {', '.join(others)}). VFS may not allow two "
            "entries for one combination on one account."
        )
        if cfg.one_client_per_account_combo:
            return False, (
                message + " Blocked by [waitlist] one_client_per_account_combo. "
                "Pin this client to a different account, or set that option to "
                "false if VFS does allow it."
            )
        logging.warning(
            f"{message} Proceeding anyway (set [waitlist] "
            "one_client_per_account_combo = true to block instead) — check the "
            "portal afterwards to confirm both entries exist."
        )

    # -- total clients on this account ---------------------------------------
    limit = cfg.max_clients_per_account
    if limit > 0:
        existing = clients_on(account.email)
        if registrant.id not in existing and len(existing) >= limit:
            return False, (
                f"account {account.masked} already carries {len(existing)} "
                f"client(s) ({', '.join(existing)}) and the limit is {limit} "
                "([waitlist] max_clients_per_account). Pin this client to a "
                "different account, or raise the limit."
            )

    return True, ""


def describe(account: Account, route: str = "") -> str:
    """A one-line summary for the run banner."""
    existing = clients_on(account.email)
    limit = settings().waitlist.max_clients_per_account
    capacity = f"{len(existing)}/{limit}" if limit > 0 else f"{len(existing)}"
    return (f"Account {account.masked} (from {account.source}) — "
            f"{capacity} client(s) registered under it")


# --------------------------------------------------------------------------- #
# Egress — which proxy IP a waitlist account uses                              #
# --------------------------------------------------------------------------- #

def resolve_proxy(account: Account, route: str,
                  cli_proxy: str = None) -> Tuple[str, str]:
    """
    Returns (proxy_url, how) for this account's egress. proxy_url is '' for the
    local IP.

    The pool is config/proxylist.txt (via proxy_pool), same as the slot checker.
    What differs is HOW an account is pinned to an entry in it.

    proxy_pool.account_proxy() indexes accounts by their position in
    config/credentials.local.ini — which waitlist accounts are deliberately NOT
    in. That lookup therefore falls through to an MD5 of the address, so the IP
    an account gets is effectively arbitrary and shifts as the pool changes.
    Harmless with one proxy; wrong the moment there are two, because a waitlist
    entry belongs to the account that made it and a stable identity is the whole
    point.

    So the pin is explicit and stable here:

        1. --proxy-url on the command line   ('' forces the local IP)
        2. "proxy" in the client file        pin one client to one exit IP
        3. [waitlist] proxy in config.ini    one exit IP for all waitlisting
        4. by position in [waitlist] accounts, else a stable hash of the address
           — deterministic, so the same account always lands on the same entry
    """
    from src.utils import proxy_pool

    if cli_proxy is not None:
        if not cli_proxy:
            return "", "forced local (--proxy-url \"\")"
        return proxy_pool._as_url(cli_proxy), "forced (--proxy-url)"

    initialize_config()

    # An EXPLICIT pin beats the [proxy] master switch. That switch governs the
    # hourly slot checker's automatic pool selection; naming a specific IP for a
    # waitlist client or for waitlisting as a whole is a deliberate instruction,
    # and silently ignoring it would send a registration out from an IP the
    # operator did not choose.
    if getattr(account, "proxy", ""):
        return proxy_pool._as_url(account.proxy), "client file pin"

    pinned = (get_config_value("waitlist", "proxy", "") or "").strip()
    if pinned:
        return proxy_pool._as_url(pinned), "[waitlist] proxy"

    # No explicit pin: fall back to the pool, which the master switch governs.
    if not settings().proxy.enabled:
        return "", ("[proxy] enabled = false — set it true in "
                    "config/config.local.ini to use config/proxylist.txt")

    entries = proxy_pool.pool()
    if not entries:
        logging.warning(
            f"[proxy] is enabled but {proxy_pool.PROXYLIST_FILE} is empty — "
            "falling back to the local IP.")
        return "", "no proxies configured"

    index = _pool_index(account.email, len(entries))
    return entries[index], f"pinned to pool entry {index + 1}/{len(entries)}"


def _pool_index(email: str, size: int) -> int:
    """A STABLE index into the proxy pool for a waitlist account.

    Prefers the account's position in the [waitlist] accounts list, so you can
    control which account uses which exit IP simply by ordering them. Falls back
    to a hash of the address — arbitrary, but deterministic, so the account
    keeps the same IP run after run.
    """
    email = (email or "").strip().lower()
    known = [a.strip().lower() for a in
             (get_config_value("waitlist", "accounts", "") or "").split(",")
             if a.strip()]
    if email in known:
        return known.index(email) % size

    import hashlib
    digest = hashlib.md5(email.encode()).hexdigest()
    return int(digest, 16) % size
