"""Fetch mail from the VFS account mailboxes and classify it.

This is the only module in the package that touches the network. Everything it
learns is handed to matcher.py (pure) to interpret, so the interesting logic
stays testable offline.

OBSERVATIONAL. It reads, classifies, records and reports. It triggers nothing,
mutates no VFS state, and opens no browser. Wiring it to the booking flow is a
later, separate, explicitly-gated step.

    IMAP is opened READ-ONLY (readonly=True), so messages are never marked as
    seen. A human reading the same mailbox must find it exactly as VFS left it —
    the watcher is an observer, and \\Seen flags belong to the person.

WHICH MAILBOXES
---------------
The waitlist accounts, from src/waitlist/accounts.py — the accounts that own
waitlist entries. Their mailboxes are where VFS's mail lands. The hourly
slot-check credentials are a separate pool and are NOT read: those accounts hold
no waitlist entries, so their mail is not ours to look at.

Mailbox credentials are the VFS account's own email and password, exactly as
otp_email.py already assumes (each VFS account's email is a real mailbox on the
configured IMAP host). Host and port come from [otp] in the INI, the one place
they are already configured.

WHY NOT EXTEND otp_email.py
---------------------------
That module is one-shot, synchronous, called mid-login, and hardcoded to an OTP
search string. This is long-lived, multi-mailbox and multi-pattern. They share
an IMAP library, not a shape. Its parsing helpers are reused; its entry point is
not extended.
"""

from __future__ import annotations

import email as email_lib
import imaplib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from src.inbox import config as inbox_config
from src.inbox import seen as seen_state
from src.inbox.matcher import Email, Match, classify_all

log = logging.getLogger(__name__)

#: Fallbacks, used only if the settings layer is unavailable. The live values
#: come from [inbox] in the INI — see src/settings.py for what each one is for.
FIRST_PASS_DAYS = 30
MAX_PER_PASS = 200
DEFAULT_POLL_SECONDS = 300


def _tunables() -> Tuple[int, int, int]:
    """(first_pass_days, max_per_pass, poll_seconds) from [inbox].

    Falls back to the module constants if settings cannot be built, so the
    watcher still runs on a machine whose INI predates the [inbox] section.
    """
    try:
        from src.settings import settings

        inbox = settings().inbox
        return (
            int(inbox.first_pass_days),
            int(inbox.max_per_pass),
            int(inbox.poll_seconds),
        )
    except Exception as e:
        log.debug(f"Falling back to built-in inbox tunables: {e}")
        return FIRST_PASS_DAYS, MAX_PER_PASS, DEFAULT_POLL_SECONDS


@dataclass
class Observation:
    """One classified message, plus where it came from.

    The Email is kept for the digest and for saving fixtures, but note that
    anything rendered for a human goes through waitlist.redaction first — see
    report(). Bodies contain client names.
    """

    email: Email
    match: Match
    account: str = ""

    @property
    def route(self) -> str:
        return self.match.route

    def expires_at(self) -> Optional[float]:
        """When an invitation's window closes, as epoch seconds.

        Measured from the server's INTERNALDATE, never from now: a watcher that
        was down for a day must not silently extend a 48-hour deadline.
        """
        if not self.match.is_invitation or not self.match.validity_hours:
            return None
        if not self.email.received_epoch:
            return None
        return self.email.received_epoch + self.match.validity_hours * 3600

    def hours_left(self) -> Optional[float]:
        expiry = self.expires_at()
        if expiry is None:
            return None
        return (expiry - time.time()) / 3600.0


@dataclass
class PassResult:
    """What one sweep over all mailboxes found."""

    observations: List[Observation] = field(default_factory=list)
    mailboxes_checked: int = 0
    mailboxes_failed: List[str] = field(default_factory=list)
    messages_seen: int = 0

    def invitations(self) -> List[Observation]:
        return [o for o in self.observations if o.match.is_invitation]

    def confirmations(self) -> List[Observation]:
        return [o for o in self.observations if o.match.is_confirmation]


# --------------------------------------------------------------------------- #
# IMAP                                                                         #
# --------------------------------------------------------------------------- #

def _ago(epoch: float) -> str:
    """'12m ago' / '3h ago' / '2d ago' — for logs and the status line."""
    if not epoch:
        return "never"
    seconds = max(0.0, time.time() - epoch)
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds / 60)}m ago"
    if seconds < 172800:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def _strip_html(html: str) -> str:
    """Crude tag-strip, for mail that has no text/plain part.

    Not a parser and not trying to be: matching only needs the words. Script and
    style contents are dropped first (they are never prose), tags are removed,
    then the handful of entities that show up in real mail are decoded.
    """
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    for entity, char in (
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'"),
    ):
        text = text.replace(entity, char)
    # Numeric entities (&#8217; and friends) appear in VFS's HTML mail.
    text = re.sub(r"&#\d+;", " ", text)
    return " ".join(text.split())


def _decode_header(raw: Optional[str]) -> str:
    """Decodes an RFC 2047 header (=?utf-8?B?...?=) to plain text."""
    if not raw:
        return ""
    try:
        parts = email_lib.header.decode_header(raw)
    except Exception:
        return str(raw)
    out = []
    for value, charset in parts:
        if isinstance(value, bytes):
            try:
                out.append(value.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                out.append(value.decode("utf-8", errors="replace"))
        else:
            out.append(value)
    return " ".join(" ".join(out).split())


def _parse_message(raw: bytes, uid: str, mailbox: str, received: float) -> Email:
    """Turns a raw RFC822 message into the plain Email value matcher.py wants.

    Prefers text/plain; falls back to a tag-stripped text/html, because VFS
    sends HTML-only mail to some portals and a body-less Email would match on
    subject alone.
    """
    message = email_lib.message_from_bytes(raw)

    body_text = ""
    html_text = ""
    for part in message.walk():
        ctype = (part.get_content_type() or "").lower()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError, TypeError):
            continue
        if ctype == "text/plain" and not body_text:
            body_text = decoded
        elif ctype == "text/html" and not html_text:
            html_text = decoded

    return Email(
        subject=_decode_header(message.get("Subject")),
        sender=_decode_header(message.get("From")),
        body=body_text or _strip_html(html_text),
        uid=str(uid),
        mailbox=mailbox,
        received_epoch=received,
    )


def _internal_date(imap, uid: str) -> float:
    """The server's INTERNALDATE for a UID, epoch seconds (0.0 if unavailable)."""
    try:
        status, data = imap.uid("FETCH", uid, "(INTERNALDATE)")
        if status != "OK" or not data or data[0] is None:
            return 0.0
        parsed = imaplib.Internaldate2tuple(
            data[0] if isinstance(data[0], bytes) else str(data[0]).encode()
        )
        return time.mktime(parsed) if parsed else 0.0
    except Exception:
        return 0.0


def _search_uids(imap, since_epoch: Optional[float]) -> List[str]:
    """UIDs to consider, newest last, narrowed server-side by date.

    TWO FILTERS, DOING DIFFERENT JOBS — this is the part worth understanding:

      SINCE (here)    NARROWS. Keeps the server from listing the whole mailbox
                      on every pass. Day granularity only, so it is deliberately
                      generous: one day BEFORE the target, to cover timezone
                      differences between this box and the mail server.
      the UID mark    DECIDES what has actually been seen (see seen.py).

    Time must never be the decider. Clocks skew, servers deliver out of order,
    and a message can be filed with an INTERNALDATE earlier than the pass that
    missed it. A UID is monotonic per mailbox, so it is the only safe test.
    Narrow with time; decide with UIDs.
    """
    criteria = "ALL"
    if since_epoch:
        day = (datetime.fromtimestamp(since_epoch) - timedelta(days=1)).strftime("%d-%b-%Y")
        criteria = f"(SINCE {day})"
    status, data = imap.uid("SEARCH", None, criteria)
    if status != "OK" or not data or not data[0]:
        return []
    return [uid.decode() if isinstance(uid, bytes) else str(uid) for uid in data[0].split()]


def fetch_mailbox(
    host: str,
    port: int,
    user: str,
    password: str,
    state: seen_state.SeenState,
    matchers_by_route: Dict[str, List[Dict[str, Any]]],
    account_label: str = "",
) -> Tuple[List[Observation], int]:
    """Reads one mailbox and classifies everything new in it.

    Returns (observations, messages_examined). Only MATCHED messages become
    observations; unmatched ones are counted and skipped, because a VFS account
    mailbox also holds ordinary mail that is none of our business.

    Never raises: a mailbox that will not open is logged and reported as a
    failure so the other accounts still get read. One bad password must not
    blind the whole watcher.
    """
    mailbox_key = user
    observations: List[Observation] = []
    examined = 0

    try:
        imap = imaplib.IMAP4_SSL(host, port, timeout=30)
    except Exception as e:
        log.warning(f"IMAP connect to {host}:{port} for {account_label} failed: {e}")
        raise

    try:
        imap.login(user, password)
        # READ-ONLY: never mark VFS's mail as seen on the user's behalf.
        status, data = imap.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError(f"could not select INBOX: {status}")

        try:
            uidvalidity_status, uidvalidity_data = imap.response("UIDVALIDITY")
            uidvalidity = (
                uidvalidity_data[0].decode()
                if uidvalidity_data and uidvalidity_data[0] else None
            )
        except Exception:
            uidvalidity = None
        state.check_uidvalidity(mailbox_key, uidvalidity)

        first_pass_days, max_per_pass, _ = _tunables()
        started_at = time.time()

        # INCREMENTAL: a mailbox read before is searched only from its last pass;
        # a fresh one is bounded by first_pass_days so a mailbox with years of
        # history is not downloaded whole on day one. _search_uids already backs
        # the date off by a day, so a pass never queries right up to its own edge.
        last = state.last_pass(mailbox_key)
        if last:
            since = last
            window = f"since the last pass ({_ago(last)})"
        else:
            since = started_at - first_pass_days * 86400
            window = f"first pass — last {first_pass_days} days"

        uids = _search_uids(imap, since)
        fresh = [u for u in uids if not state.is_seen(mailbox_key, u)]

        log.info(
            f"{account_label}: {window}; server offered {len(uids)} message(s), "
            f"{len(fresh)} new."
        )

        if len(fresh) > max_per_pass:
            log.info(
                f"{account_label}: capping this pass at the newest {max_per_pass}; "
                f"the rest follow next pass."
            )
            fresh = sorted(fresh, key=int)[-max_per_pass:]

        for uid in sorted(fresh, key=int):
            try:
                status, msg_data = imap.uid("FETCH", uid, "(RFC822)")
                if status != "OK" or not msg_data or msg_data[0] is None:
                    state.mark(mailbox_key, uid)
                    continue
                raw = msg_data[0][1]
                received = _internal_date(imap, uid)
                message = _parse_message(raw, uid, mailbox_key, received)
                examined += 1

                found = classify_all(message, matchers_by_route)

                # One line per message examined, at DEBUG. This is the answer to
                # "what is it actually reading?" — run with -v to see it. It
                # stays at DEBUG because a mailbox with 300 messages would
                # otherwise bury the digest on a first pass.
                log.debug(
                    f"  {account_label} uid {uid}: "
                    f"{found.matcher_name or 'unmatched':22} "
                    f"| {(message.subject or '(no subject)')[:60]}"
                )

                if found.matched:
                    observations.append(
                        Observation(email=message, match=found, account=account_label)
                    )
            except Exception as e:
                # One unreadable message must not abort the mailbox.
                log.debug(f"{account_label}: skipping UID {uid}: {e}")
            finally:
                # Marked either way: a message that cannot be parsed now will not
                # parse next pass either, and retrying it forever is a loop.
                state.mark(mailbox_key, uid)

        # Stamped only on a clean pass. An exception above leaves the previous
        # timestamp in place, so the next pass re-covers the window this one
        # failed on rather than skipping over it.
        state.record_pass(mailbox_key, started_at)
        return observations, examined

    finally:
        try:
            imap.logout()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# One pass over every account                                                  #
# --------------------------------------------------------------------------- #

def _imap_settings() -> Tuple[str, int]:
    """IMAP host/port from [otp] — the one place they are already configured."""
    from src.settings import settings

    otp = settings().otp
    return otp.imap_host, int(otp.imap_port or 993)


def _extra_mailboxes() -> Dict[str, str]:
    """Mailboxes listed explicitly in [inbox] mailboxes, as {email: password}.

    Client files are the normal source, but they only cover accounts that
    someone is registered under. A mailbox worth watching may have no client at
    all — a shared inbox VFS mail is forwarded to, or an account being trialled
    before any client is assigned to it. Without this there is no way to watch
    one short of inventing a fake client file.

    Format, in config/config.local.ini (NOT the committed config.ini — these are
    real passwords):

        [inbox]
        mailboxes = someone@example.com:secret, other@example.com:secret2

    A password containing a comma cannot be expressed here; use a client file
    for that account instead. Malformed entries are skipped with a warning
    rather than raising — one bad entry must not stop every other mailbox being
    watched.
    """
    from src.utils.config_reader import get_config_value, initialize_config

    initialize_config()
    raw = (get_config_value("inbox", "mailboxes", "") or "").strip()
    if not raw:
        return {}

    found: Dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        email, sep, password = entry.partition(":")
        email, password = email.strip(), password.strip()
        if not sep or not email or not password:
            log.warning(
                f"[inbox] mailboxes: skipping malformed entry {entry!r} — "
                f"expected 'email:password'.")
            continue
        found[email] = password
    return found


def mailbox_accounts() -> List[Tuple[str, str]]:
    """(email, password) for every mailbox to watch, de-duplicated.

    Two sources, in priority order:

      1. [inbox] mailboxes   explicit, for accounts with no client file
      2. client files        every config/registrants/*.json's waitlist account

    Several clients may share one account, so the same mailbox would otherwise
    be opened once per client. Explicit entries win on a collision — someone who
    has named a mailbox and its password in the config means that password.

    Order is stable so runs are reproducible.
    """
    from src.waitlist import accounts as waitlist_accounts
    from src.waitlist import registrant as registrant_mod

    found: Dict[str, str] = dict(_extra_mailboxes())
    if found:
        log.debug(f"{len(found)} mailbox(es) from [inbox] mailboxes.")

    for client in registrant_mod.load_all(skip_invalid=True):
        try:
            account = waitlist_accounts.resolve(client)
        except Exception as e:
            log.debug(f"No account resolved for client {client.id}: {e}")
            continue
        if account and account.email and account.password:
            found.setdefault(account.email, account.password)
    return sorted(found.items())


def run_pass(state: Optional[seen_state.SeenState] = None) -> PassResult:
    """One sweep: every waitlist mailbox, classified, state saved.

    State is saved after EACH mailbox rather than once at the end, so a crash
    part-way through does not re-report the mailboxes already done.
    """
    result = PassResult()
    host, port = _imap_settings()
    if not host:
        log.error(
            "No IMAP host configured ([otp] imap_host). The inbox watcher cannot "
            "run without one."
        )
        return result

    matchers_by_route = inbox_config.all_matchers()
    if not matchers_by_route:
        log.error(
            "No inbox configs found in config/inbox/. Nothing can be classified."
        )
        return result

    state = state or seen_state.load()
    accounts = mailbox_accounts()
    if not accounts:
        log.warning(
            "No waitlist accounts with credentials were found — nothing to watch. "
            "Check config/registrants/ and the [waitlist] account settings."
        )
        return result

    log.info(
        f"Inbox pass: {len(accounts)} mailbox(es), "
        f"{len(matchers_by_route)} route config(s) "
        f"({', '.join(sorted(matchers_by_route))})."
    )

    for user, password in accounts:
        from src.waitlist.accounts import mask

        label = mask(user)
        try:
            observations, examined = fetch_mailbox(
                host, port, user, password, state, matchers_by_route, label
            )
            result.observations.extend(observations)
            result.messages_seen += examined
            result.mailboxes_checked += 1
            if observations:
                log.info(f"{label}: {len(observations)} VFS message(s) matched.")
        except Exception as e:
            log.warning(f"{label}: mailbox pass failed: {e}")
            result.mailboxes_failed.append(label)
        finally:
            seen_state.save(state)

    return result


def watch(poll_seconds: Optional[int] = None, once: bool = False) -> PassResult:
    """Passes forever (or once), reporting what each finds.

    No IMAP IDLE. It was considered and rejected for now: IDLE means holding a
    connection open per mailbox and reconnecting on every server timeout, which
    is real complexity bought for latency that does not matter when the deadline
    it serves is 48 hours away. Revisit only if that deadline ever shrinks.
    """
    from src.inbox.report import report

    if poll_seconds is None:
        poll_seconds = _tunables()[2]

    state = seen_state.load()
    while True:
        try:
            result = run_pass(state)
            report(result)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            # The watcher is long-lived; an unexpected error must not end it.
            log.exception(f"Inbox pass failed unexpectedly: {e}")
            result = PassResult()

        if once:
            return result
        time.sleep(poll_seconds)
