"""Drive one client's booking from the dashboard to the confirmation.

This is the booking half's equivalent of `waitlist/register.py`, and it is built
around the same single rule:

        ═══════════ THE POINT OF NO RETURN ═══════════

    Before the step flagged "commits": true is submitted, a failure is
    BookingStepError — abandon quietly, nothing on VFS's side changed.

    After it, a failure is BookingCommittedError, which is deliberately NOT
    retryable. A submit that may have landed must never be replayed.

Everything else serves that rule: the write-ahead row is fsync'd before the
committing click, and the post-commit path only ever READS the page.

WHAT IS DIFFERENT FROM REGISTRATION
-----------------------------------
Registration creates something new; booking resumes something that already
exists. That adds two steps with no waitlist equivalent, and they run BEFORE
anything is filled:

    dashboard_resume   find this client's waitlisted row and open it
    identity_assert    prove the opened row is really theirs

Both are pre-commit and both can abort for free — opening a dashboard row
commits nothing. That is the whole reason identity is checked by CLICKING and
then verifying, rather than by trying to be certain in advance.

    Booking one client's appointment under another's passport is
    unrecoverable and costs a real person a real slot. Missing an invitation
    is a bad day. The asymmetry is encoded: when in doubt, do nothing.

ONE RUNNER, EVERY COUNTRY
-------------------------
There is no country-specific code here, exactly as `register.py` has none
across seven waitlist routes. Steps, selectors, fields and the commit boundary
all come from `config/booking/<ROUTE>.json`. The step TYPE selects a handler
from the table below; adding a country is a JSON file, and adding a genuinely
new kind of page is one handler.

    dashboard_resume  ->  _step_dashboard_resume
    identity_assert   ->  _step_identity_assert
    form              ->  _step_form
    slot_pick         ->  _step_slot_pick
    confirm           ->  _step_confirm

STATUS: ORCHESTRATION ONLY — NOT YET ARMED
------------------------------------------
The skeleton, the ordering, the journalling and the commit discipline are here
and tested. What is NOT here is anything that depends on pages nobody has
opened: `config/booking/*.json` describes the flow only as far as
`/che/services`, the payment step does not exist, and `commits: true` therefore
sits on the slot pick as a placeholder rather than where it belongs.

So `book()` refuses to submit a committing step unless `live=True`, and no
route sets `enabled: true`. Capture the remaining pages, move the flag to
payment, then arm. See TASKS.md.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from src.booking import config as booking_config
from src.booking import identity as identity_mod
from src.booking import lifecycle
from src.booking.errors import (
    AmbiguousIdentityError,
    ApplicationNotFoundError,
    BookingCommittedError,
    BookingConfigError,
    BookingDisabled,
    BookingSkipped,
    BookingStepError,
    BookingUnconfirmedError,
    IdentityMismatchError,
    InvitationExpiredError,
    SlotGoneError,
)

log = logging.getLogger(__name__)

DEFAULT_STEP_TIMEOUT_MS = 45000


# --------------------------------------------------------------------------- #
# Result                                                                       #
# --------------------------------------------------------------------------- #

class BookingRun:
    """What one booking attempt did. Returned in every case, including failure.

    Mirrors `WaitlistResult` rather than reusing it: that class is read by the
    always-on slot-check path, and giving it booking fields would push booking
    concepts into code that never books.
    """

    def __init__(self, route: str, registrant_id: str, account: str = ""):
        self.route = route
        self.registrant_id = registrant_id
        self.account = account
        self.status: str = lifecycle.BookingStatus.INVITED
        self.reason: str = ""
        self.reference: str = ""
        self.slot: str = ""
        self.steps_completed: List[str] = []
        self.captures: List[str] = []
        self.started_at: float = time.time()
        self.finished_at: Optional[float] = None

    def finish(self, status: str, reason: str = "") -> "BookingRun":
        self.status = status
        self.reason = reason
        self.finished_at = time.time()
        return self

    @property
    def committed(self) -> bool:
        """Whether VFS state may have changed. Drives what a caller may retry."""
        return lifecycle.is_committed(self.status, lifecycle.PHASE_BOOKING)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route": self.route,
            "registrant_id": self.registrant_id,
            "account": self.account,
            "status": self.status,
            "reason": self.reason,
            "vfs_reference": self.reference or None,
            "slot": self.slot or None,
            "steps_completed": list(self.steps_completed),
            "captures": list(self.captures),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def summary(self) -> str:
        bits = [f"[{self.status.upper()}]", self.route, self.registrant_id]
        if self.slot:
            bits.append(self.slot)
        if self.reference:
            bits.append(f"ref {self.reference}")
        if self.reason:
            bits.append(f"— {self.reason}")
        return " · ".join(bits)


# --------------------------------------------------------------------------- #
# Step handlers                                                                #
# --------------------------------------------------------------------------- #
#
# Each takes the same arguments and raises on failure. They are dispatched by
# the step's "type", so a route's JSON decides which run and in what order.
#
# Handlers that need real page selectors are deliberately thin: the selectors
# for the pages after "Book Now" have never been captured, and a handler
# written against a guess would be worse than one that says it cannot proceed.

def _step_dashboard_resume(ctx: "_StepContext") -> None:
    """Find this client's waitlisted row on the dashboard and open it.

    Reuses `probe.read_dashboard` and `probe.match_row` rather than
    reimplementing them: the probe already parses these cards and its parsing is
    covered by offline tests against real captured text. The probe READS and the
    runner CLICKS — that is the only difference, and it is why they share code
    rather than duplicating selectors.
    """
    from src.booking import probe

    rows = probe.read_dashboard(ctx.page, ctx.route)
    if not rows:
        raise ApplicationNotFoundError(
            f"No application rows on the dashboard for {ctx.route}. Either this "
            "account holds none, or the card selector needs updating.")

    row, reason = probe.match_row(
        rows,
        reference=ctx.expected_reference,
        name=ctx.expected_name,
    )
    if row is None:
        # resolve() refuses on ambiguity rather than picking. Surfaced as a
        # distinct error because a tie needs a human, not a retry.
        if "ambiguous" in (reason or "").lower():
            raise AmbiguousIdentityError(
                f"Two or more dashboard rows match this client: {reason}. "
                "Booking neither — an ambiguous match must never be guessed.")
        raise ApplicationNotFoundError(
            f"No dashboard row matched this client: {reason}")

    ctx.run.matched_row = row                      # type: ignore[attr-defined]
    log.info(f"  matched row: {row.summary()}")

    if not row.bookable:
        raise BookingSkipped(
            f"Row {row.reference or row.index} is not bookable yet — its "
            "waitlist status does not say slots are available.")

    open_spec = (ctx.step.get("row") or {}).get("open")
    if not open_spec:
        raise BookingConfigError(
            f"Step '{ctx.name}' has no row.open — nothing to click.")
    _click(ctx.page, open_spec, f"step '{ctx.name}' open row", ctx.timeout_ms)


def _step_identity_assert(ctx: "_StepContext") -> None:
    """Prove the opened application really belongs to this client.

    CLICK-THEN-CHECK. Opening a row commits nothing, so a wrong click is free
    PROVIDED it is detected — and this is that detection. `identity.verify`
    tolerates silence (a field the page does not show) but never contradiction
    (a field that disagrees). Silence is normal; a mismatch is fatal.
    """
    row = getattr(ctx.run, "matched_row", None)
    if row is None:
        raise BookingStepError(
            f"Step '{ctx.name}' ran before any row was matched — "
            "'identity_assert' must follow 'dashboard_resume'.")

    candidate = identity_mod.Candidate(
        key=str(row.index), name=row.name, reference=row.reference)
    verdict = identity_mod.verify(
        candidate,
        reference=ctx.expected_reference,
        name=ctx.expected_name,
    )
    if not verdict.resolved:
        raise IdentityMismatchError(
            f"The opened application does not match this client: "
            f"{verdict.reason}. Abandoning — nothing has been committed.")
    log.info(f"  identity verified: {verdict.describe()}")


def _step_form(ctx: "_StepContext") -> None:
    """Fill a page's fields and submit it.

    Reuses `waitlist.fields`, which already drives eight widget types across
    seven waitlist routes. Booking forms are the same Angular components on the
    same portal, so a second implementation would be a second set of bugs.
    """
    from src.waitlist import fields as fields_mod

    specs = ctx.step.get("fields") or []
    if specs:
        fields_mod.fill_all(ctx.page, specs, ctx.values, ctx.timeout_ms)

    if ctx.step.get("scroll_to_bottom"):
        # Some pages put Continue below the fold; without this the click misses.
        ctx.page.mouse.wheel(0, 20000)
        ctx.page.wait_for_timeout(500)


def _step_slot_pick(ctx: "_StepContext") -> None:
    """Choose a date and a time.

    Delegates to `walk.py`, which already reads the calendar and slot table from
    config and is asserted against real captured DOM — including VFS's own
    `date-availiable` misspelling, which has three i's and must be matched
    verbatim.
    """
    from src.booking import walk

    calendar = ctx.step.get("calendar") or {}
    slots = ctx.step.get("time_slots") or {}

    dates = walk.available_dates(ctx.page, calendar)
    if not dates:
        # Not a failure: first-come-first-served with many invitees means an
        # empty calendar is an expected outcome, and it must read as normal so
        # real faults stay visible.
        raise_slot_gone("The calendar offers no available dates.")

    chosen_date = dates[0]
    walk.pick_date(ctx.page, calendar, chosen_date, ctx.timeout_ms)
    ctx.page.wait_for_timeout(1000)          # the slot table loads after a date

    times = walk.available_times(ctx.page, slots)
    if not times:
        raise_slot_gone(
            f"{chosen_date} showed as available but offers no time slots — "
            "most likely taken between the calendar rendering and this click.")

    chosen_time = walk.pick_time(ctx.page, slots, 0, ctx.timeout_ms)
    ctx.run.slot = f"{chosen_date} {chosen_time}".strip()
    log.info(f"  chose slot: {ctx.run.slot}")


def _step_confirm(ctx: "_StepContext") -> None:
    """Read the booking reference off the confirmation page.

    POST-COMMIT, so it only ever READS. If the reference cannot be found the run
    is `booking_unknown`, never a retry: the booking may well exist.
    """
    pattern = ctx.step.get("reference_pattern")
    if not pattern:
        raise BookingConfigError(
            f"Step '{ctx.name}' has no reference_pattern — a confirm step that "
            "cannot read a reference cannot confirm anything.")

    import re

    try:
        text = ctx.page.inner_text("body", timeout=ctx.timeout_ms)
    except Exception as e:
        raise BookingUnconfirmedError(
            f"Could not read the confirmation page: {e}. The booking may have "
            "succeeded — check the portal by hand.") from e

    found = re.search(pattern, " ".join((text or "").split()), re.IGNORECASE)
    if not found:
        raise BookingUnconfirmedError(
            "The confirmation page carried no reference matching "
            f"{pattern!r}. The booking may have succeeded — check by hand.")

    ctx.run.reference = found.group(1).strip()
    log.info(f"  booking reference: {ctx.run.reference}")


def raise_slot_gone(detail: str) -> None:
    """Raise SlotGone with a consistent message.

    A helper rather than an inline raise because `slot_gone` is NOT a failure —
    it is the expected result of losing a race — and it must be phrased that way
    everywhere, or it starts reading like a fault in the summaries.
    """
    raise SlotGoneError(f"{detail} This is a lost race, not a fault.")


#: Step type -> handler. The single place a new kind of page is registered.
STEP_HANDLERS: Dict[str, Callable[["_StepContext"], None]] = {
    "dashboard_resume": _step_dashboard_resume,
    "identity_assert": _step_identity_assert,
    "form": _step_form,
    "slot_pick": _step_slot_pick,
    "confirm": _step_confirm,
}


# --------------------------------------------------------------------------- #
# Step plumbing                                                                #
# --------------------------------------------------------------------------- #

class _StepContext:
    """Everything one step handler needs. Keeps their signatures identical."""

    def __init__(self, page, step: Dict[str, Any], route: str,
                 run: BookingRun, values: Dict[str, Any],
                 expected_name: str, expected_reference: str):
        self.page = page
        self.step = step
        self.route = route
        self.run = run
        self.values = values
        self.expected_name = expected_name
        self.expected_reference = expected_reference

    @property
    def name(self) -> str:
        return self.step.get("name", "?")

    @property
    def timeout_ms(self) -> int:
        return int(self.step.get("timeout_ms") or DEFAULT_STEP_TIMEOUT_MS)


def _click(page, spec: Any, what: str, timeout_ms: int) -> None:
    """Click, reusing registration's ladder (normal -> force -> JS).

    VFS renders buttons as spans and labels that ignore a plain click, and that
    ladder is already tuned for it across seven routes.
    """
    from src.waitlist.register import _click as waitlist_click

    waitlist_click(page, spec, what, timeout_ms)


def _await_page(page, step: Dict[str, Any], timeout_ms: int) -> None:
    """Wait for the step's page gate, translating the error to a booking one."""
    from src.waitlist.errors import WaitlistStepError
    from src.waitlist.register import _await_page as waitlist_await

    try:
        waitlist_await(page, step, timeout_ms)
    except WaitlistStepError as e:
        raise BookingStepError(str(e)) from e


def _submit(ctx: "_StepContext") -> None:
    """Submit a step, waiting for the button to become enabled first.

    VFS gates Continue behind "a date AND a time are chosen" and renders it
    disabled until both are, so clicking blind fails intermittently — which is
    the worst way for it to fail.
    """
    submit = ctx.step.get("submit")
    if not submit:
        return

    from src.booking.walk import _await_enabled_safe

    _await_enabled_safe(ctx.page, submit)
    _click(ctx.page, submit, f"step '{ctx.name}' submit", ctx.timeout_ms)
    ctx.page.wait_for_timeout(1500)


def _capture(ctx: "_StepContext", label: str) -> None:
    """Save the page, best-effort. Never the reason a run fails."""
    from src.booking.walk import _capture_html

    path = _capture_html(ctx.page, label, ctx.route)
    if path:
        ctx.run.captures.append(path)


# --------------------------------------------------------------------------- #
# The run                                                                      #
# --------------------------------------------------------------------------- #

def book(page, route: str, registrant, *, account: str = "",
         expected_reference: str = "", deadline_epoch: Optional[float] = None,
         values: Optional[Dict[str, Any]] = None,
         live: bool = False) -> BookingRun:
    """Book one client's appointment, starting from the dashboard.

    Returns a BookingRun in every case EXCEPT a post-commit failure, which is
    raised: a `BookingCommittedError` must never be quietly folded into a return
    value, because the caller has to know it may not retry.

    `live=False` (the default) walks everything up to the committing step and
    stops in front of it, having changed nothing. That is not a debug mode —
    it is the only safe mode until the payment step exists and `commits: true`
    has moved to it.
    """
    run = BookingRun(route=route, registrant_id=registrant.id, account=account)

    # THE SAME CLIENT FILE REGISTRATION USED. There is no separate "booking
    # data": config/registrants/<id>.json is the one record of a client, and
    # ctx.build() flattens it into the mapping {{placeholders}} resolve against
    # — exactly as the waitlist runner does.
    #
    # That is deliberate, not an economy. The client typed their passport
    # number once, and VFS matches the booking against the waitlist entry it
    # already holds; a second copy that could drift from the first is how a
    # booking ends up under details the waitlist does not recognise.
    #
    # If a country's booking pages ask for something registration never
    # collected, the missing key surfaces as an unresolved {{placeholder}} at
    # validation time — before a browser starts — rather than as a half-filled
    # form three pages in.
    if values is None:
        from src.waitlist import context as ctx_mod

        values = ctx_mod.build(registrant, route=route)

    cfg = booking_config.get(route)
    if not cfg.get("enabled"):
        raise BookingDisabled(
            f"Booking is disabled for {route}. Every route ships disabled until "
            "its whole flow has been walked in a browser.")

    # The deadline is checked BEFORE the browser does anything: a lapsed
    # invitation cannot be booked, and finding that out after a login wastes a
    # session on an account that is rate-limited for reuse.
    if deadline_epoch is not None and lifecycle.is_expired(deadline_epoch,
                                                           time.time()):
        run.finish(lifecycle.BookingStatus.EXPIRED,
                   "the invitation window closed before this run started")
        raise InvitationExpiredError(run.reason)

    expected_name = _client_name(registrant)
    commit_name = booking_config.commit_step_name(route)
    log.info(
        f"Booking: {route} / {registrant.id} "
        f"{'[LIVE]' if live else '[DRY RUN]'} (commit step: '{commit_name}')"
    )

    run.status = lifecycle.BookingStatus.BOOKING
    committed = False

    try:
        for step in booking_config.steps_for(route):
            name = step.get("name", "?")
            if step.get("disabled"):
                log.debug(f"Step '{name}' disabled — skipped.")
                continue

            handler = STEP_HANDLERS.get(step.get("type", ""))
            if handler is None:
                raise BookingConfigError(
                    f"Step '{name}' has type {step.get('type')!r}, which has no "
                    f"handler. Known types: {', '.join(sorted(STEP_HANDLERS))}.")

            ctx = _StepContext(page, step, route, run, values,
                               expected_name, expected_reference)

            # dashboard_resume starts wherever login left us; the rest gate on
            # their own URL or text.
            if step.get("type") != "dashboard_resume":
                _await_page(page, step, ctx.timeout_ms)

            log.info(f"Booking step '{name}' ({step.get('type')})...")
            handler(ctx)
            _capture(ctx, name)

            if step.get("commits"):
                if not live:
                    run.finish(
                        lifecycle.BookingStatus.INVITED,
                        f"dry run — reached the commit boundary at '{name}' "
                        "and stopped. Nothing was submitted.")
                    run.steps_completed.append(name)
                    return run
                committed = _commit(ctx, run)
            else:
                _submit(ctx)

            run.steps_completed.append(name)

        if not run.reference:
            # Every step ran but nothing read a reference. With a confirm step
            # configured that is BookingUnconfirmedError (raised there); without
            # one, the flow is simply incomplete and must not claim success.
            run.finish(
                lifecycle.BookingStatus.BOOKING_UNKNOWN,
                "every configured step completed but no booking reference was "
                "read — the flow has no confirm step, so success cannot be "
                "asserted. Check the portal by hand.")
            return run

        run.finish(lifecycle.BookingStatus.BOOKED,
                   f"booked (ref {run.reference})")
        return run

    except BookingCommittedError:
        # Post-commit. Never swallowed, never retried — the caller must see it.
        run.finish(lifecycle.BookingStatus.BOOKING_UNKNOWN,
                   "failed after the commit — needs a human")
        raise
    except BookingSkipped as e:
        run.finish(lifecycle.BookingStatus.INVITED, str(e))
        return run
    except SlotGoneError as e:
        # EXPECTED, and the only pre-commit error that is. First-come-first-
        # served with many invitees means losing a race is a normal outcome, so
        # it is returned as a result rather than raised — and recorded under its
        # own status so a report can render it as routine. If this read as a
        # failure, genuine faults would drown in noise from a healthy system.
        run.finish(lifecycle.BookingStatus.SLOT_GONE, str(e))
        return run
    except BookingStepError as e:
        if committed:
            # Defensive: a pre-commit error class raised after the commit would
            # otherwise be reported as safely abandoned, which is the one thing
            # it is not.
            run.finish(lifecycle.BookingStatus.BOOKING_UNKNOWN, str(e))
            raise BookingCommittedError(
                f"A step failed after the commit boundary: {e}") from e

        # Everything else pre-commit RAISES rather than returning quietly.
        #
        # They all subclass BookingStepError, but they are not the same kind of
        # event: an ambiguous identity or a contradicted reference is the safety
        # machinery REFUSING, and a refusal that is folded into a returned
        # result looks exactly like "nothing to do today". The caller must be
        # made to handle it. Nothing was submitted in any of these cases, so
        # raising costs nothing but attention — which is the point.
        run.finish(lifecycle.BookingStatus.INVITED, str(e))
        raise


def _commit(ctx: "_StepContext", run: BookingRun) -> bool:
    """Submit the committing step, with the write-ahead marker on disk first.

    The ordering is the whole point and it is not negotiable:

        1. journal BOOKING_PENDING and fsync   evidence a submit is imminent
        2. capture the page                    what it looked like beforehand
        3. click submit                        the point of no return
        4. everything after this only READS

    A crash between 1 and 3 leaves a row saying "a submit was about to happen",
    which is exactly what a human needs to check the portal. A crash after 3
    with no marker would be indistinguishable from never having run.
    """
    run.status = lifecycle.BookingStatus.BOOKING_PENDING
    _journal(run)                                   # fsync'd by journal.append
    _capture(ctx, f"{ctx.name}_before_commit")

    try:
        _submit(ctx)
    except Exception as e:
        # The click itself failed. It may still have reached VFS, so this is
        # committed-state, not retryable.
        raise BookingCommittedError(
            f"Submitting the committing step '{ctx.name}' failed: {e}. The "
            "booking may or may not exist — check the portal.") from e

    _capture(ctx, f"{ctx.name}_after_commit")
    return True


def _journal(run: BookingRun) -> None:
    """Append this run's current state. Best-effort EXCEPT for the marker.

    NOTE: booking rows do not yet share the waitlist journal — that wiring
    lands with the queue, alongside the lane locking that makes a second writer
    safe. Until then this logs, so the ordering above is exercised and correct
    when the store arrives.
    """
    log.info(f"JOURNAL: {run.status} {run.route}/{run.registrant_id}"
             + (f" ref={run.reference}" if run.reference else ""))


def _client_name(registrant) -> str:
    """The client's full name, as the dashboard and the invitation render it."""
    first = (registrant.get("first_name") or "").strip()
    last = (registrant.get("last_name") or "").strip()
    return " ".join(part for part in (first, last) if part)
