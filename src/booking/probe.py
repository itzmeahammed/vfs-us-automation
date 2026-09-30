"""Look at the dashboard and report what is bookable. Clicks NOTHING.

WHY THIS EXISTS BEFORE THE RUNNER
---------------------------------
The booking runner cannot be written yet: the selectors for the pages after
"Book Now" are unknown. But everything BEFORE that button is already knowable —
log in, read the dashboard, find the client's row, confirm the reference matches.

That is worth proving on its own, because it is where most of the risk lives:

    * does login work on this account and this route?
    * does the dashboard render the cards the screenshot showed?
    * do the selectors in config/booking/<ROUTE>.json actually match?
    * does the reference on the row equal the one in the journal?
    * is a row actually invited ("Book Now"), or merely active?

Proving those first means the runner is written against a dashboard that is
already understood, instead of guessing twice.

STRICTLY READ-ONLY
------------------
It logs in, reads the dashboard, and stops. It does not click "Book Now", tick
anything, submit anything, or change VFS state in any way. `--keep-open` leaves
the browser up so the pages after the button can be inspected by hand — which is
exactly how the missing selectors get captured.

The orchestration (Chrome, Cloudflare, OTP, account health, proxy budget) is
reused from src/waitlist/runner.py rather than reimplemented. This module's own
contribution is small on purpose: parse the cards, match them, report.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.booking import config as booking_config
from src.booking.errors import BookingConfigError, BookingSkipped
from src.vfs_bot.errors import AccessRestrictedError
from src.booking.identity import Candidate, resolve

log = logging.getLogger(__name__)

#: Fallbacks used when a route config names no row selectors. Deliberately broad:
#: the point of a probe is to find SOMETHING and report what it saw, not to fail
#: cleanly on an unmapped portal.
DEFAULT_CARD_SELECTORS = (
    "app-appointment-card", ".application-card", "mat-card", ".card"
)

#: "Group Reference Number - GRC127086415238". Left loose — three real prefixes
#: exist (GRC/ITD/SWDB) and a hand-recorded WL-77231 also appears in the journal.
DEFAULT_REFERENCE_PATTERN = r"Reference Number\s*[-:]?\s*([A-Z0-9-]{6,})"

#: The invited state, as shown on the dashboard beside the reference.
SLOTS_AVAILABLE_MARKER = "slots available"


@dataclass
class DashboardRow:
    """One application card, as read off the dashboard.

    `raw_text` is kept because a probe's job is partly to report what it could
    NOT parse — a card whose reference did not match is far more useful shown in
    full than reduced to empty fields.
    """

    index: int
    reference: str = ""
    applicants: List[str] = field(default_factory=list)
    bookable: bool = False
    actions: List[str] = field(default_factory=list)
    raw_text: str = ""

    @property
    def name(self) -> str:
        """The first applicant — what the invitation email's greeting names."""
        return self.applicants[0] if self.applicants else ""

    def summary(self) -> str:
        bits = [f"[{self.index}]"]
        bits.append(self.reference or "(no reference parsed)")
        if self.applicants:
            bits.append(" / ".join(self.applicants))
        bits.append("BOOKABLE" if self.bookable else "not bookable")
        if self.actions:
            bits.append(f"actions: {', '.join(self.actions)}")
        return " · ".join(bits)


@dataclass
class ProbeResult:
    """What one look at the dashboard found."""

    route: str
    account: str = ""
    rows: List[DashboardRow] = field(default_factory=list)
    bookable: List[DashboardRow] = field(default_factory=list)
    matched: Optional[DashboardRow] = None
    match_reason: str = ""
    errors: List[str] = field(default_factory=list)
    walk: Optional[Any] = None
    """A WalkResult, when --walk was asked for. None means the probe only read
    the dashboard, which is the default and the safe case."""

    interrupted: bool = False
    """True when the operator pressed Ctrl-C. Distinct from an error: nothing
    went wrong with the code, and the CLI exits 130 (the shell convention for
    SIGINT) rather than 1, so a wrapper script can tell "the human stopped it"
    from "it failed"."""

    @property
    def ok(self) -> bool:
        """Whether the dashboard was read at all. Finding no rows is a valid
        answer, not a failure — the account may simply hold no applications."""
        return not self.errors


# --------------------------------------------------------------------------- #
# Reading the dashboard                                                        #
# --------------------------------------------------------------------------- #

def _card_selector(route: str) -> str:
    """The CSS that finds application cards, from config or the fallbacks."""
    try:
        step = next(
            (s for s in booking_config.steps_for(route)
             if s.get("type") == "dashboard_resume"), None
        )
        configured = ((step or {}).get("row") or {}).get("container")
        if configured:
            return configured
    except BookingConfigError:
        pass
    return ", ".join(DEFAULT_CARD_SELECTORS)


def _reference_pattern(route: str) -> str:
    try:
        step = next(
            (s for s in booking_config.steps_for(route)
             if s.get("type") == "dashboard_resume"), None
        )
        configured = ((step or {}).get("row") or {}).get("reference_pattern")
        if configured:
            return configured
    except BookingConfigError:
        pass
    return DEFAULT_REFERENCE_PATTERN


def parse_card(text: str, index: int, reference_pattern: str) -> DashboardRow:
    """Turn one card's visible text into a DashboardRow. PURE — testable offline.

    Text rather than DOM on purpose. The screenshot shows the *labels*
    ("Group Reference Number - …", "Applicants:", "Waitlist Status: SLOTS
    AVAILABLE") but not the markup, and labels have proved far more stable than
    VFS's positional ids. When the real DOM is known this can be tightened.
    """
    row = DashboardRow(index=index, raw_text=text)
    flat = " ".join((text or "").split())

    found = re.search(reference_pattern, flat, re.IGNORECASE)
    if found:
        row.reference = found.group(1).strip()

    # "Applicants:" is followed by one or more names, up to the next label.
    # The terminator list must name every label that can FOLLOW "Applicants:",
    # not merely the ones that precede it. On the real AE-CHE dashboard the
    # applicant row is followed by "Visa Application form Status - ..." and
    # "Edit Form", and without those the name captured as
    #   "MUFADDAL MUFADDAL Visa Application form Status - Not Initiated Edit Form"
    # — which still matched by reference, but would have failed every name
    # comparison and rendered unreadably in any report.
    applicants = re.search(
        r"Applicants?\s*:\s*(.+?)(?:\s*(?:Group Reference|Waitlist Status|"
        r"Manage |Book Now|Appointment details|Visa Application|Edit Form|"
        r"Apply For|Travel Medical)|$)",
        flat, re.IGNORECASE,
    )
    if applicants:
        names = [n.strip() for n in re.split(r"\s{2,}|,", applicants.group(1))]
        row.applicants = [n for n in names if n]

    row.bookable = SLOTS_AVAILABLE_MARKER in flat.lower()
    for action in ("Book Now", "Manage Appointment", "Manage Application"):
        if action.lower() in flat.lower():
            row.actions.append(action)

    return row


#: How long to wait for the first application card to render.
#:
#: The dashboard is Angular: the HTML arrives, then the applications are
#: fetched and the cards are drawn. 15s is generous for that round trip over a
#: metered residential proxy, and it is only ever paid in full by an account
#: that really has no applications — where waiting costs nothing, because that
#: run has nothing else to do.
CARD_WAIT_MS = 15000


def read_dashboard(page, route: str) -> List[DashboardRow]:
    """Reads every application card on the dashboard. Clicks nothing."""
    selector = _card_selector(route)
    pattern = _reference_pattern(route)
    log.info(f"Reading dashboard cards with selector: {selector}")

    rows: List[DashboardRow] = []
    try:
        cards = page.locator(selector).filter(visible=True)

        # WAIT for the first card before counting.
        #
        # `.count()` is an INSTANT query — it is one of the few Playwright calls
        # with no auto-waiting, so it returns whatever exists at that moment.
        # This dashboard is Angular and fetches the applications after the page
        # itself has loaded, so counting immediately reliably returned 0 and the
        # probe then reported "this account holds no applications" about an
        # account that demonstrably had one.
        #
        # That was a wrong answer delivered confidently, which is worse than an
        # error: it sent the search off after the wrong problem entirely.
        #
        # A timeout here is NOT a failure — an account genuinely holding nothing
        # will always time out, and that is a real and expected answer. So this
        # falls through to the count either way and lets _diagnose_empty() ask
        # the page which case it is.
        try:
            cards.first.wait_for(state="visible", timeout=CARD_WAIT_MS)
        except Exception:
            log.debug(
                f"No card became visible within {CARD_WAIT_MS}ms — either the "
                "account holds none, or the selector is wrong. Diagnosing.")

        count = cards.count()
    except Exception as e:
        log.warning(f"Could not query cards with '{selector}': {e}")
        return rows

    log.info(f"Found {count} element(s) matching the card selector.")
    if count == 0:
        # "Nothing found" is ambiguous, and the two causes need OPPOSITE fixes:
        # an account with no applications is correct and needs nothing changed,
        # while a wrong selector needs the config fixed. Guessing wrong here
        # wastes a lot of time, so the probe asks the page which it is.
        _diagnose_empty(page)

    for index in range(count):
        try:
            text = cards.nth(index).inner_text(timeout=5000)
        except Exception as e:
            log.debug(f"Card {index}: could not read text: {e}")
            continue
        if not (text or "").strip():
            continue

        row = parse_card(text, index, pattern)
        # A container selector like "mat-card" also matches page furniture. A
        # card with neither a reference nor an applicant is almost certainly
        # not an application, so it is dropped rather than reported as one.
        if row.reference or row.applicants:
            rows.append(row)

    return rows


#: Wording a portal uses when an account genuinely holds no applications. If any
#: of these is on the page, the selector is fine and there is simply nothing to
#: find — which is a completely different problem from a selector that missed.
EMPTY_MARKERS = (
    "no active application", "no application", "no appointment",
    "you have not", "nothing to display", "no records", "no data",
)


def _diagnose_empty(page) -> None:
    """Explain WHY nothing was found: an empty account, or a wrong selector.

    Both look identical in the output ("0 cards") and need opposite fixes, so
    the probe asks the page rather than leaving the reader to guess. Purely
    diagnostic — it reads text and reports; it never changes the page.
    """
    try:
        text = " ".join((page.inner_text("body", timeout=5000) or "").split())
    except Exception as e:
        log.debug(f"Could not read the page text: {e}")
        return

    lowered = text.lower()
    for marker in EMPTY_MARKERS:
        if marker in lowered:
            log.info(
                f"The page says '{marker}' — this account holds no applications "
                f"on this portal. The card selector is not at fault; there is "
                f"simply nothing to find. Probe an account that HAS a waitlist "
                f"entry on this route.")
            return

    # No empty-marker, so the page probably DOES show something we failed to
    # match. Report what is there so the real selector can be written.
    if "reference" in lowered or "applicant" in lowered:
        log.warning(
            "The page mentions 'reference' or 'applicant' but the card selector "
            "matched nothing — the selector is probably WRONG for this portal. "
            "Re-run with --keep-open and inspect the DOM.")

    log.info(f"Page text (first 600 chars, for diagnosis): {text[:600]}")


# --------------------------------------------------------------------------- #
# Matching                                                                     #
# --------------------------------------------------------------------------- #

def match_row(rows: List[DashboardRow], reference: str = "",
              name: str = "") -> tuple:
    """Find the row belonging to one client. Returns (row_or_None, reason).

    Delegates to identity.resolve, so the probe obeys exactly the same rules the
    booking runner will: reference first (exact), name second (scored), and a
    tie resolves to nothing rather than a guess.
    """
    if not rows:
        return None, "no rows on the dashboard"

    candidates = [
        Candidate(key=str(row.index), name=row.name, reference=row.reference)
        for row in rows
    ]
    found = resolve(candidates, reference=reference, name=name)
    if not found.resolved:
        return None, found.reason

    row = next(r for r in rows if str(r.index) == found.candidate.key)
    return row, found.reason


# --------------------------------------------------------------------------- #
# The probe                                                                    #
# --------------------------------------------------------------------------- #

def _assert_account_healthy(email: str, route: str) -> None:
    """Refuse to log in with an account the circuit breaker has benched.

    The probe had no such check, and that is how a restricted account got
    signed into five times in 35 minutes on 2026-09-24 — each attempt renewing
    a block that then outlived the 12-hour invitation it was trying to serve.

    Read-only, like the waitlist's equivalent: this refuses the run rather than
    deepening a cooldown another subsystem is already serving.
    """
    try:
        from src.utils import account_health

        if account_health.is_disabled(email):
            raise BookingSkipped(
                f"Account {_mask(email)} is DISABLED — refusing to sign in.")
        if account_health.is_benched(email, route):
            until = account_health.benched_until(email, route)
            mins = max(1, int((until - time.time()) / 60))
            raise BookingSkipped(
                f"Account {_mask(email)} is benched on {route} for another "
                f"{mins} min. Signing in now would renew the block, not clear "
                "it. To override: python -m src.utils.account_health clear "
                f"{email}")
    except BookingSkipped:
        raise
    except Exception as e:                             # noqa: BLE001
        # A health-file problem must never be what stops a run the user asked
        # for; the breaker is a safety net, not a gate of last resort.
        log.debug(f"Could not read account health: {e}")


def _record_block(email: str, route: str, error: Exception) -> None:
    """Bench the account after a hard block, exactly as the supervisor does.

    Without this the probe learned nothing from a 429: the next run signed
    straight back in and renewed the restriction. The supervisor has benched on
    this for a long time (supervisor.py, AccessRestrictedError) — the probe was
    simply never taught the same lesson.
    """
    try:
        from src.utils import account_health

        hours = account_health.hard_cooldown_hours()
        account_health.bench(email, route, hours, "restricted-429001")
        log.error(
            f"Account benched for {hours}h after a hard block: {error}")
    except Exception as e:                             # noqa: BLE001
        log.warning(f"Could not record the block against account health: {e}")


def _mask(email: str) -> str:
    from src.waitlist.accounts import mask

    try:
        return mask(email)
    except Exception:                                  # noqa: BLE001
        return "(account)"


def run_probe(source: str, dest: str,
              registrant_id: Optional[str] = None,
              email: Optional[str] = None,
              password: Optional[str] = None,
              proxy: Optional[str] = None,
              keep_open: bool = False,
              hold_seconds: int = 0,
              entry: str = "",
              combo: str = "",
              walk: bool = False,
              to_step: Optional[str] = None,
              applicant: Optional[Dict[str, Any]] = None,
              commit: bool = False,
              capture: str = "",
              person: Optional[Any] = None) -> ProbeResult:
    """Log in, read the dashboard, report. Clicks nothing, changes nothing.

    Reuses src/waitlist/runner.py's orchestration — Chrome, the Cloudflare/OTP
    gauntlet, account health, the proxy budget — rather than reimplementing any
    of it. What differs is only where it stops: at the dashboard, before the
    booking flow's first click.
    """
    # Imported here rather than at module scope so `python -m src.booking check`
    # stays importable without playwright installed.
    from src.utils import proxy_pool
    from src.utils.chrome_launcher import ChromeProcess
    from src.utils.config_reader import get_config_value, set_config_value
    from src.vfs_bot.vfs_bot_factory import get_vfs_bot
    from src.waitlist import accounts
    from src.waitlist.errors import WaitlistConfigError
    from src.waitlist.runner import (
        _assert_account_healthy,
        _check_budget,
        _login_and_reach_dashboard,
        _record_usage,
        _report_usage,
        _reset_usage,
        _roster,
        shutdown,
    )
    from src.settings import settings

    route = f"{source.upper()}-{dest.upper()}"
    result = ProbeResult(route=route)
    log.info(f"=== Booking probe: {route} (READ-ONLY — clicks nothing) ===")

    # A probe needs an ACCOUNT; a client is optional. With --email the caller has
    # supplied the account directly, which is the normal way to look at a brand
    # new account before any client file exists for it — so a missing roster is
    # not an error there. _roster raises in that case, hence the catch.
    # `person` given directly is a booking request (src/booking/requests.py):
    # it is NOT on the waitlist roster, and must not fall through to _roster(),
    # which with no registrant id returns every waitlist client on the route
    # and would book request A with client B's account.
    supplied_person = person
    person = None

    # "" means "whatever the walk considers normal" — resolved HERE rather than
    # defaulting the parameter to walk.DEFAULT_CAPTURE, so there is exactly one
    # definition of the default and importing walk stays lazy.
    from src.booking.walk import CAPTURE_MODES, DEFAULT_CAPTURE
    capture = capture or DEFAULT_CAPTURE
    if capture not in CAPTURE_MODES:
        raise ValueError(
            f"capture must be one of {', '.join(CAPTURE_MODES)}, not "
            f"{capture!r}.")

    try:
        if supplied_person is not None:
            person = supplied_person
        else:
            people = _roster(route, registrant_id)
            person = people[0] if people else None
    except WaitlistConfigError:
        if not email:
            raise
        log.info(f"No client file for {route}; probing the account given on the "
                 f"command line. The expected-row match will be skipped.")

    resolved = accounts.resolve(person, cli_email=email, cli_password=password)
    result.account = resolved.masked
    log.info(f"Account: {resolved.masked} ({resolved.source})")

    _assert_account_healthy(resolved.email, route)

    proxy_url, how = accounts.resolve_proxy(resolved, route, cli_proxy=proxy)
    log.info(f"Egress: "
             f"{proxy_pool.label(proxy_url) if proxy_url else 'local IP'} ({how})")
    if proxy_url:
        _check_budget()

    # Which way in. Defaults to whatever the route actually supports, so a
    # live-slot-only route is not silently probed as a waitlist resume (and
    # told it has no rows), and a waitlist route is not asked to create an
    # application nobody wanted.
    supported = booking_config.entry_modes(route)
    if not entry:
        entry = (booking_config.ENTRY_NEW
                 if supported == [booking_config.ENTRY_NEW]
                 else booking_config.ENTRY_WAITLIST)
    entry = entry.strip().lower()
    if supported and entry not in supported:
        raise BookingConfigError(
            f"{route} does not support the '{entry}' flow. It supports: "
            f"{', '.join(supported)}. A flow needs its own entry step, not "
            "just a shared tail.")
    log.info(f"Entry flow: {entry}")

    url = get_config_value("vfs-url", route)
    if not url:
        raise WaitlistConfigError(
            f"No login URL for {route} in config/vfs_urls.ini.")

    _assert_account_healthy(resolved.email, route)

    # ── THE DATE RANGE, CHECKED BEFORE CHROME EXISTS. ───────────────────────
    #
    # A walk that cannot say which dates it is allowed to book must not start.
    # resolve_strategy refuses a record with no window, but it only runs once
    # the walk reaches the calendar — which is after a login, and a login is
    # the scarce resource here: VFS blocks an account after roughly three in a
    # short window, and that block outlives a 12-hour invitation.
    #
    # So the same question is asked here, offline, where the answer costs
    # nothing. Only for runs that will actually pick a slot: a plain dashboard
    # read books nothing and needs no window.
    if walk:
        _assert_bookable_window(route, entry, combo, person, applicant)

    bot = None
    page = None      # bound after login; the error path checks it
    _reset_usage()
    chrome = ChromeProcess(port=settings().retry.cdp_port, url=url,
                           proxy=proxy_url, profile_key=resolved.email)
    try:
        chrome.start()
        set_config_value("browser", "cdp_url", chrome.cdp_url)
        set_config_value("browser", "keep_cf_clearance",
                         "false" if getattr(chrome, "egress_changed", False)
                         else "true")

        bot = get_vfs_bot(source, dest)
        bot.set_credential(resolved.email, resolved.password)

        # Stops ON the dashboard — the page listing existing applications.
        # Deliberately not _login_and_reach_appointment_page: that goes one
        # click further to "Start New Booking" (for creating a NEW application),
        # which would navigate away from exactly the page we came to read and
        # cost a page load of metered proxy traffic to come back from.
        page = _login_and_reach_dashboard(bot, url)
        log.info(f"On the dashboard: {page.url}")

        # Belt and braces: authenticate() lands on the dashboard, but a portal
        # that redirects elsewhere would otherwise be read silently as "no
        # applications" — the one failure this probe must never report wrongly.
        if "dashboard" not in (page.url or "").lower():
            log.warning(f"Expected a dashboard URL, got {page.url} — navigating.")
            _go_to_dashboard(page, url)

        # ═══════ THE 'NEW' FLOW HAS NO DASHBOARD ROW, AND MUST NOT LOOK ═══════
        #
        # Everything in this block is about resuming an application VFS ALREADY
        # CREATED: read the cards, find this client's row, prove it is theirs.
        # A live-slot booking has none of that — the slot is public and "Start
        # New Booking" creates the application — so there is nothing to read.
        #
        # It used to run regardless, and the cost was not just noise:
        #
        #   * CARD_WAIT_MS (15s) spent waiting for cards that cannot exist,
        #     on a metered proxy, while a public slot is being taken by someone
        #     else. This page is a race.
        #   * The run then logged "this account holds no applications on this
        #     portal — probe an account that HAS a waitlist entry", which reads
        #     as a fault and sends the next hour of diagnosis in the wrong
        #     direction. For a 'new' run an empty dashboard is not a finding at
        #     all; it is the expected state.
        #
        # The dashboard is still CAPTURED either way — it costs nothing once
        # the page is already open, and it is the only record of what the
        # account looked like going in.
        # NOTE: booking_config is imported at MODULE level (top of this file).
        # Re-importing it here made it a local of run_probe, so every earlier
        # use in this same function — entry_modes() at the top, which runs
        # first — became a read of an unassigned local and died with
        # "cannot access local variable 'booking_config'". Python decides
        # local-vs-global per function, not per line.
        from src.booking.walk import CAPTURE_FULL, _capture_html

        if entry == booking_config.ENTRY_NEW:
            log.info("Live-slot booking ('new'): skipping the dashboard read — "
                     "there is no existing application to resume. "
                     "'Start New Booking' creates one.")
            if capture == CAPTURE_FULL:
                _capture_html(page, "dashboard", route)
        else:
            result.rows = read_dashboard(page, route)

            # Captured AFTER read_dashboard, not before, and the order is the
            # whole point: this dashboard is Angular and draws its cards from a
            # fetch that completes after load. read_dashboard already waits for
            # the first card, so capturing before it saved the pre-render page
            # — which still says "No Application(s) Found" and contains none of
            # the markup the capture exists to preserve. The empty file then
            # looked like proof the account held nothing.
            if capture == CAPTURE_FULL:
                _capture_html(page, "dashboard", route)
            result.bookable = [r for r in result.rows if r.bookable]

            if person is not None:
                expected_name = _client_name(person)
                expected_reference = _stored_reference(route, person)
                row, reason = match_row(
                    result.rows, reference=expected_reference,
                    name=expected_name)
                result.matched = row
                result.match_reason = reason

        if walk:
            result.walk = _do_walk(page, route, result, to_step, entry,
                                   _walk_values(route, combo, person,
                                                applicant),
                                   commit=commit, capture=capture)

            # A walk that stopped short is exactly when the session is worth
            # most: the page it could not pass is on screen, logged in, one
            # human click from moving on. Tearing it down here is what turns a
            # selector problem into another login.
            if keep_open and result.walk is not None and result.walk.stopped_at:
                _handover(page, route,
                          f"walk stopped at '{result.walk.stopped_at}': "
                          f"{result.walk.reason}", hold_seconds)
                keep_open = False        # already held; do not hold twice

    except KeyboardInterrupt:
        # ── Ctrl-C IS A FIRST-CLASS OUTCOME, NOT A CRASH. ───────────────────
        #
        # Playwright's sync API runs its event loop on a greenlet, so a Ctrl-C
        # during any blocking call (wait_for_url, click, wait_for_timeout) is
        # raised on the DISPATCHER fiber and arrives here only after the loop
        # unwinds. `except Exception` never caught it — KeyboardInterrupt is a
        # BaseException, deliberately — so it printed a 20-frame asyncio
        # traceback AFTER the finally block had already closed Chrome cleanly.
        # The teardown was fine; the traceback made it look like it was not.
        #
        # What the operator actually needs to know is: which step, and whether
        # anything irreversible is in flight.
        # walk.CURRENT_STEP, NOT result.walk. result.walk is assigned only when
        # _do_walk RETURNS, so an interrupt mid-walk leaves it None and this
        # reported "(pre-walk)" for a run that was on the payment step —
        # observed 2026-09-29, four seconds after the payment disclaimer. The
        # marker is updated as each step begins, so it is right even when the
        # walk never finishes, which is the only case this handler runs in.
        from src.booking.walk import current_step

        step = current_step() or (
            result.walk.stopped_at if result.walk else "") or "(pre-walk)"
        log.warning(f"Interrupted by Ctrl-C during '{step}'.")
        result.errors.append(f"interrupted by the operator during '{step}'")
        result.interrupted = True

        # THE ONE THING THAT CANNOT BE UNDONE. A journal row with no recorded
        # outcome means a payment was submitted and we never saw the answer —
        # so the card may have been charged. Say so here, loudly, because this
        # is the moment the operator is looking at the terminal.
        # INTERRUPTED ON THE STEP THAT SPENDS MONEY. The journal below answers
        # "was a payment submitted"; this answers the question that comes
        # first — "was the appointment booked". On Norway the commit boundary
        # is the payment step itself, so a Ctrl-C there can leave a slot taken
        # at VFS with nothing charged, which no local file records.
        try:
            from src.booking import config as _bc
            committing = {st.get("name") for st in
                          _bc.steps_for(route, entry or "")
                          if st.get("commits")}
            if step and step in committing:
                log.warning(
                    f"Ctrl-C landed ON the committing step ('{step}'). The "
                    "appointment may or may not have been created at VFS — "
                    "check the account before re-running, because a second "
                    "run would book a second slot.")
        except Exception:                                   # noqa: BLE001
            pass

        try:
            from src.payment import journal as payment_journal
            pending = payment_journal.unanswered()
            if pending:
                log.error(
                    f"*** {len(pending)} PAYMENT(S) SUBMITTED WITH NO RECORDED "
                    f"OUTCOME. *** Do NOT re-run this booking. Check the "
                    f"gateway and {payment_journal.JOURNAL_FILE} before anything else — "
                    f"a retry double-charges.")
        except Exception:                                   # noqa: BLE001
            # Never let diagnosis swallow the interrupt.
            pass

        # Capture and hold, same as any other stop: the session is the scarce
        # resource and Ctrl-C is usually "something looks wrong, let me see".
        if keep_open and page is not None:
            try:
                _handover(page, route, f"interrupted during '{step}'",
                          hold_seconds)
                keep_open = False
            except Exception:                               # noqa: BLE001
                pass

    except AccessRestrictedError as e:
        # A hard block. Record it so the NEXT run refuses instead of renewing
        # the restriction — which is the mistake that cost a live invitation on
        # 2026-09-24.
        _record_block(resolved.email, route, e)
        log.error(f"Probe blocked: {e}")
        result.errors.append(str(e))
    except Exception as e:
        log.exception(f"Probe failed: {e}")
        result.errors.append(str(e))
        # Same reasoning as a short walk: a failure with the browser still up is
        # recoverable by hand, and the login it holds is the scarce resource.
        if keep_open and page is not None:
            try:
                _handover(page, route, f"probe failed: {e}", hold_seconds)
                keep_open = False
            except Exception:                               # noqa: BLE001
                pass
    finally:
        if keep_open:
            # Deliberately generic. This used to say "click 'Book Now' by hand
            # and capture the selectors", which was written when the walk could
            # not get past the dashboard — and it printed that instruction to a
            # run that had reached the PAYMENT GATEWAY. A stale instruction on
            # the money page is worse than none.
            log.info("Browser left open (--keep-open). Finish or abandon the "
                     "run by hand in the open window; the session is not "
                     "spent, and closing it would cost another login.")
            _hold_open(hold_seconds)
        _report_usage(bot, proxy_url, "probe")
        _record_usage(proxy_url)
        # shutdown(), not chrome.close(): the Playwright driver must disconnect
        # BEFORE Chrome is killed, or Node writes into a dead pipe and reports
        # an unhandled EPIPE after the run has otherwise finished.
        shutdown(bot, chrome)

    return result


def _handover(page, route: str, why: str, seconds: int = 0) -> None:
    """Stop, SAVE THE PAGE, say what is needed, and wait for a human.

    *** RECONNAISSANCE ONLY. NEVER PART OF AN AUTOMATED BOOKING. ***

    This exists to capture the DOM of pages nobody has mapped yet, with a human
    sitting at the keyboard. It is reachable only from the probe, only behind
    --keep-open, and src/booking/runner.py does not import it — an unattended
    run must FAIL, journal the failure and release the browser, never sit
    waiting for an operator who is not there.

    Blocking a scheduled run on human input would hold the account's session and
    the run lock open for as long as the timeout allows, turning one bad
    selector into a stalled queue. If a future runner ever needs to pause for a
    human, that is a queue state to be recorded and picked up later, not a
    sleep() inside the run.

    The alternative — tearing the session down and asking the operator to run
    it again — is what restricted two accounts in two days: every invocation is
    a fresh login, and VFS counts them. So when the walk cannot proceed, the
    session is the most valuable thing in the room and must not be spent.

    What this prints is the handover itself: the page it stopped on, the file
    the DOM was written to, and the reason. The operator clicks the thing the
    bot could not, and the next step runs in the SAME session.
    """
    # A SCREENSHOT, not the DOM. Handover is a human sitting at the keyboard
    # with the page already in front of them — the markup was for writing
    # selectors from, which is done for Norway. The PNG is the record of what
    # they were handed, for the log.
    from src.booking.walk import _capture_shot

    path = ""
    try:
        path = _capture_shot(page, "handover", route)
    except Exception as e:                                  # noqa: BLE001
        log.warning(f"Could not capture the page at handover: {e}")

    log.warning("=" * 68)
    log.warning("HANDOVER — the browser is still open and still logged in.")
    log.warning(f"  reason : {why}")
    try:
        log.warning(f"  page   : {page.url}")
    except Exception:                                       # noqa: BLE001
        pass
    if path:
        log.warning(f"  saved  : {path}")
    log.warning("  Do the step by hand in the open window. The session is NOT")
    log.warning("  spent — closing it would cost another login, and repeated")
    log.warning("  logins are what trigger VFS's 429001.")
    log.warning("=" * 68)

    _hold_open(seconds)


def _hold_open(seconds: int = 0) -> None:
    """Keep the browser up after a run, WITHOUT ending the session.

    EVERY PROBE INVOCATION IS A FRESH LOGIN, and that is what gets an account
    restricted: three runs in five minutes triggered VFS's 429001 on
    2026-09-25, on two different accounts, each time costing a live invitation.
    The fix is to stop starting new sessions, not to slow them down.

    Two ways to wait, because a probe is driven both by hand and by a script:

      * input() when a terminal is attached - press Enter, close, done.
      * a timed sleep otherwise, because a non-interactive caller gets EOF
        immediately and would tear the session down at once, which is exactly
        the behaviour being avoided.
    """
    import sys
    import time as _time

    if seconds > 0:
        log.info(f"Holding the session open for {seconds}s.")
        _time.sleep(seconds)
        return

    if not sys.stdin or not sys.stdin.isatty():
        log.info("No terminal attached - holding for 600s. Pass --hold N to "
                 "choose, or run from a terminal to close with Enter.")
        _time.sleep(600)
        return

    try:
        input("[keep-open] Press Enter to close the browser...")
    except EOFError:
        pass


def _assert_bookable_window(route: str, entry: str, combo: str, person,
                            applicant: Optional[Dict[str, Any]] = None) -> None:
    """Refuse, offline, if this booking has no usable date range.

    Raises BookingConfigError so the CLI reports it as a configuration problem
    and exits before launching a browser — which is the whole point. Every
    message names the client record and the two fields, because the person who
    has to fix it is a sales agent, not a programmer.
    """
    from src.booking import walk as walk_mod

    steps = booking_config.steps_for(route, entry)
    slot_steps = [st for st in steps
                  if st.get("type") == walk_mod.SLOT_STEP_TYPE]
    if not slot_steps:
        return          # a route that picks no slot needs no window

    values = _walk_values(route, combo, person, applicant)
    who = getattr(person, "id", None) or "(the client record)"

    for step in slot_steps:
        try:
            strategy = walk_mod.resolve_strategy(values, step)
        except Exception as e:                              # noqa: BLE001
            raise BookingConfigError(f"{who}: {e}") from e

        if strategy != walk_mod.STRATEGY_IN_RANGE:
            continue

        problems = walk_mod.check_window(values, step)
        if problems:
            raise BookingConfigError(f"{who}: " + " ".join(problems))

        start, end = walk_mod.date_window(values, step)
        log.info(f"Date range: {start} .. {end} (strategy 'in_range' — "
                 "nothing outside this window will be booked).")


def _walk_values(route: str, combo: str, person,
                 applicant: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The mapping a form step's {{placeholders}} resolve against.

    Built with the same waitlist context builder the registration path uses, so
    a combo label becomes the centre/category/sub-category the dropdowns need.
    That split matters here: those three come from the COMBINATION, not from the
    client — which is why a live-slot booking can be walked with no client file
    at all, as long as a combo is named.
    """
    try:
        from src.utils.route_schema import get_route_schema
        from src.waitlist import context as ctx_mod

        parts = {}
        if combo:
            source, _, dest = route.partition("-")
            schema = get_route_schema(source, dest)
            for entry in schema.get("slot_check", {}).get("combinations", []):
                label = str(entry.get("label", "")).strip()
                if label.lower() == combo.strip().lower():
                    parts = dict(entry)
                    break
            if not parts:
                log.warning(
                    f"Combo {combo!r} is not in config/routes/{route}.json — "
                    "the centre/category dropdowns will have no value.")

        # --applicant WINS over the client file, and deliberately: it exists for
        # the live-slot flow, which has no roster at all (there is no invitation
        # to match a client to), and for correcting one field of a real client
        # without editing their stored profile for a single reconnaissance run.
        return ctx_mod.build(person, route=route, combo=combo,
                             combo_parts=parts, extra=applicant or None)
    except Exception as e:                                  # noqa: BLE001
        log.warning(f"Could not build the walk context: {e}")
        return {}


def _do_walk(page, route: str, result: "ProbeResult", to_step: Optional[str],
             entry: str = "waitlist", values: Optional[Dict[str, Any]] = None,
             commit: bool = False, capture: str = ""):
    """Click 'Book Now' on the chosen row, then walk the booking pages.

    Only reached with --walk. Everything it does is REVERSIBLE: confirmed with
    the user that selecting a slot reserves nothing — the slot stays in the
    public pool and the booking exists only once payment completes. So the cost
    of abandoning is the attempt, never the client's waitlist entry.

    It still refuses to submit the committing step (see walk.walk_flow).
    """
    from src.booking import config as booking_config
    from src.booking.walk import WalkResult, walk_flow
    from src.waitlist.register import _click

    # THE LIVE-SLOT FLOW HAS NO DASHBOARD ROW TO OPEN.
    #
    # Everything below this block is about resuming an application VFS already
    # created: find the client's row, check it is invited, click Book Now. A
    # 'new' flow has none of that — the slot is open to anyone and
    # "Start New Booking" CREATES the application — so it goes straight to
    # walk_flow, whose first step is the entry step itself.
    #
    # Without this the probe would refuse a perfectly good Norway run with
    # "0 bookable rows", which is true and completely beside the point.
    if entry == booking_config.ENTRY_NEW:
        log.info(f"Live-slot flow ({entry}) — no dashboard row to resume; "
                 "walking from the entry step.")
        return walk_flow(page, route, to_step=to_step, entry=entry,
                         values=values or {}, dry_run=not commit,
                         capture=capture)

    # Prefer the client's own row; otherwise the only bookable one. Never guess
    # between several — the same rule the runner will follow.
    row = result.matched
    if row is None:
        if len(result.bookable) == 1:
            row = result.bookable[0]
            log.info(f"Walking the single bookable row: {row.summary()}")
        else:
            reason = (f"{len(result.bookable)} bookable rows and no client match "
                      f"— refusing to guess which to open")
            log.warning(reason)
            return WalkResult(stopped_at="dashboard_resume", reason=reason)

    if not row.bookable:
        reason = (f"row {row.index} is not invited "
                  f"(no 'SLOTS AVAILABLE') — nothing to book")
        log.warning(reason)
        return WalkResult(stopped_at="dashboard_resume", reason=reason)

    step = next((s for s in booking_config.steps_for(route, entry)
                 if s.get("type") == "dashboard_resume"), {})

    # INVITED IS NOT BOOKABLE. The card can say "Waitlist Status: SLOTS
    # AVAILABLE" and still render Book Now dead, because VFS requires the visa
    # application form to be completed first — "Visa Application form Status -
    # Not Initiated" sits on the same card, and the span carries
    # class="...disabled-book-now".
    #
    # Checked HERE, on the dashboard, rather than discovered later: without it
    # the walk clicks a dead span, waits out a 30s timeout on a calendar that
    # never loaded, and reports a slot_pick selector failure — sending the
    # investigation after the wrong problem entirely. That happened on
    # 2026-09-25 and cost a login against a rate-limited account.
    blocked = (step.get("row") or {}).get("blocked_marker")
    if blocked:
        try:
            html = page.content()
        except Exception:                                   # noqa: BLE001
            html = ""
        if blocked in html:
            reason = (
                f"row {row.index} shows SLOTS AVAILABLE but Book Now is "
                f"disabled ('{blocked}'). VFS wants the visa application form "
                "completed first — open 'Edit Form' on the card and fill it. "
                "Nothing here is a selector fault.")
            log.warning(reason)
            return WalkResult(stopped_at="dashboard_resume", reason=reason)

    open_spec = (step.get("row") or {}).get("open") or {"role": "button",
                                                        "name": "Book Now"}

    log.info(f"Clicking '{open_spec.get('name', open_spec)}' on row {row.index}...")
    _click(page, open_spec, "dashboard 'Book Now'", 30000)
    page.wait_for_timeout(2000)
    log.info(f"Now at {page.url}")

    return walk_flow(page, route, to_step=to_step, dry_run=not commit,
                     capture=capture)


def _go_to_dashboard(page, login_url: str) -> None:
    """Navigate to the dashboard from wherever login left us.

    Tries the obvious URL first, then falls back to a visible link. VFS's
    post-login landing page differs per portal, so neither alone is reliable.
    """
    from src.vfs_bot import turnstile

    base = login_url.rsplit("/", 1)[0]
    for candidate in (f"{base}/dashboard", f"{base}/application"):
        try:
            page.goto(candidate, timeout=30000, wait_until="domcontentloaded")
            turnstile.wait_for_loader(page)
            if "dashboard" in (page.url or "").lower():
                log.info(f"Dashboard: {page.url}")
                return
        except Exception as e:
            log.debug(f"Could not open {candidate}: {e}")

    for name in ("Dashboard", "My Applications", "Active application"):
        try:
            link = page.get_by_role("link", name=name).first
            if link.is_visible(timeout=2000):
                link.click()
                turnstile.wait_for_loader(page)
                log.info(f"Dashboard via '{name}' link: {page.url}")
                return
        except Exception:
            continue

    log.warning(f"Could not confirm the dashboard; reading whatever is at "
                f"{page.url}")


def _client_name(person) -> str:
    """A client's full name, for matching against the dashboard's applicants."""
    for key in ("full_name", "name"):
        value = person.get(key)
        if value:
            return str(value)
    first = person.get("first_name") or ""
    last = person.get("last_name") or ""
    return f"{first} {last}".strip()


def _stored_reference(route: str, person) -> str:
    """This client's VFS reference, from their file or from the journal.

    THE CLIENT FILE WINS. The journal records what THIS system registered, and
    that is not always the live entry: an entry created by hand never appears in
    it, and an entry it did create can since have expired. On 2026-09-25 the
    journal's only reference for a client was one VFS had already expired, while
    the live invitation belonged to a hand-made entry the journal had never
    seen. Preferring the journal there would have matched the dead row or
    nothing at all.

    Either way this is the exact join: the confirmation email's Unique Reference
    Number is the same value the dashboard shows as Group Reference Number, so a
    stored reference identifies the row with no name matching at all.
    """
    if person is not None:
        own = str(person.get("vfs_reference") or "").strip()
        if own:
            log.info(f"Using the reference stored on the client file: {own}")
            return own
    return _journal_reference(route, getattr(person, "id", "") or "")


def _journal_reference(route: str, registrant_id: str) -> str:
    """The reference this system recorded when it registered the client."""
    if not registrant_id:
        return ""
    try:
        from src.waitlist import journal

        for row in reversed(journal.entries()):
            if (row.get("registrant_id") or "").lower() != registrant_id.lower():
                continue
            if (row.get("route") or "").upper() != route.upper():
                continue
            if row.get("vfs_reference"):
                return str(row["vfs_reference"])
    except Exception as e:
        log.debug(f"Could not read a stored reference: {e}")
    return ""
