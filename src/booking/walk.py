"""Walk the booking pages, stopping short of anything irreversible.

WHAT THIS IS FOR
----------------
The booking runner cannot be written until every page is known, but the pages
can only be learned by walking them. This module is that walk: it clicks
"Book Now", picks a date and a time, and continues through the pages that
follow — reporting the DOM it finds and stopping at the first page it does not
recognise.

    dashboard "Book Now"  ->  /book-appointment  ->  /services  ->  ???
                              pick date + time       scroll +
                                                     Continue

Everything here is REVERSIBLE. Confirmed with the user: selecting a slot
reserves nothing — the slot stays in the public pool and the booking exists only
once payment completes. So abandoning at any point costs nothing but the
attempt, and the client keeps their waitlist entry and their invitation.

    That is precisely why walking is safe, and why it stops before payment.

WHAT IT WILL NOT DO
-------------------
It refuses to submit the step marked "commits": true. Today that flag sits on
the slot pick for want of a payment step to carry it, so `--to` must be used to
stop earlier; `walk_flow` will not pass a committing submit on its own.

WHY NOT JUST EXTEND doctor.py
-----------------------------
`waitlist/doctor.py` probes a page's selectors against a config. This drives a
flow forwards and reports what it MEETS, including pages no config describes —
the opposite direction. They will likely converge once the flow is known.
"""

from __future__ import annotations

import io
from calendar import monthrange
from datetime import date
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

DEFAULT_STEP_TIMEOUT_MS = 45000

#: How long to wait for a submit button to stop being disabled. VFS gates
#: Continue behind "a date AND a time are chosen", and the button is rendered
#: disabled until both are.
ENABLE_TIMEOUT_MS = 20000

#: How many months forward to search for an available date. VFS opens on the
#: current month and the offered dates are routinely weeks out, so reading only
#: the first month reports "no slots" while the portal is showing plenty.
DEFAULT_MAX_MONTHS_AHEAD = 4

#: How to choose among the dates and times on offer.
#:
#:   earliest  the soonest appointment. The default, and right for a waitlist
#:             invitation: the slot is not held while the remaining pages are
#:             walked, so a competitor can take it at any moment.
#:   latest    the furthest out. Useful when a client needs time to prepare
#:             documents, and safer to TEST with — the far end of the calendar
#:             is the least contended, so a trial run is least likely to take a
#:             slot somebody else wanted today.
SLOT_STRATEGIES = frozenset({"earliest", "latest", "in_range"})

#: "in_range" is the production strategy, and it is a different KIND of thing
#: from the other two.
#:
#:   earliest / latest   describe how to choose among whatever is offered, and
#:                       therefore ALWAYS succeed when the calendar has a date.
#:   in_range            names the dates a sales agent actually asked for, and
#:                       therefore MUST be able to fail.
#:
#: That asymmetry is the whole point. If the agent asks for 10-20 November and
#: VFS offers 3 November and 2 December, the correct outcome is to book nothing
#: and say so. Quietly taking the nearest date books a real client onto a day
#: nobody agreed to — and unlike a missed slot, that is not visible until they
#: turn up at the embassy.
#:
#: Within the window it takes the EARLIEST date: the client attends an office
#: rather than catching a flight, so sooner is strictly better, and earlier
#: dates are the ones a competitor takes first.
STRATEGY_IN_RANGE = "in_range"

#: The step type that picks a date and time. Named here because the offline
#: date-window gate in probe.py has to find those steps BEFORE a browser
#: starts, and a hard-coded string there that drifts from the config would make
#: the gate silently match nothing — which is indistinguishable from "every
#: client has a valid window" and defeats the check entirely. Verified against
#: config/booking/AE-NOR.json, where select_slot is type "slot_pick".
SLOT_STEP_TYPE = "slot_pick"


def _as_date(value: Any) -> Optional[date]:
    """Parse an ISO date. None when absent or unparseable.

    Deliberately strict — ISO only, no "15/11/2026". A sales agent's date
    arrives as text and 03/04 is March in one country and April in another;
    guessing which would book the wrong month. _check_window turns the None
    into a refusal with a message naming the offending value.
    """
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def date_window(values: Optional[Dict[str, Any]],
                step: Optional[Dict[str, Any]] = None) -> tuple:
    """The (from, to) dates this booking may use. (None, None) when unset.

    Read from the CLIENT's values, not the route config: the window is what one
    agent asked for on one booking, while the route file describes a country.
    A window in config/booking/AE-NOR.json would apply to every client Norway
    ever books, which is never what is meant.

    `step` supplies fallback keys only, so a route whose config names them
    differently still works.
    """
    values = values or {}
    step = step or {}
    from_key = step.get("date_from_key") or "date_from"
    to_key = step.get("date_to_key") or "date_to"
    return _as_date(values.get(from_key)), _as_date(values.get(to_key))


def resolve_strategy(values: Optional[Dict[str, Any]],
                    step: Optional[Dict[str, Any]] = None,
                    slots: Optional[Dict[str, Any]] = None) -> str:
    """Which date-picking strategy this ONE booking uses.

        ═══════ A BOOKING NEEDS A DATE RANGE. NO WINDOW, NO RUN. ═══════

    Precedence, most specific first:

        1. the client record's "slot_strategy"   <- an explicit override
        2. date_from/date_to present             -> "in_range"
        3. NOTHING                               -> REFUSE

    There is no fallback to "earliest" and no route-wide default, by decision
    of the operator (2026-09-29). The reasoning is about which mistake is
    cheap:

      * Refusing a client whose dates were never entered costs someone typing
        two fields. It happens before a browser opens, so it costs no login.

      * Booking "whatever VFS offers soonest" for a client whose agent MEANT
        to give a window costs a real appointment on a date nobody agreed to,
        a real card charge, and a slot that is then gone. It is discovered
        after the fact, and it is not reversible.

    A missing window used to mean "earliest", which made those two cases
    indistinguishable: a record with forgotten dates looked exactly like a
    client who genuinely wanted the soonest slot.

    Rule 1 still exists so an agent CAN ask for "earliest" — they just have to
    say so, in writing, on the record. That turns the dangerous default into a
    deliberate statement.

    The route step's "strategy" is now ignored for this decision. It is kept in
    the config only so an existing file does not fail to parse; a strategy that
    is one setting for every client a country ever books cannot express what
    one agent sold to one person.
    """
    from src.waitlist.errors import WaitlistStepError

    values = values or {}
    step = step or {}

    asked = str(values.get("slot_strategy") or "").strip().lower()
    if asked:
        if asked not in SLOT_STRATEGIES:
            raise WaitlistStepError(
                f"The client record asks for slot_strategy {asked!r}, which is "
                f"not a strategy. One of: {', '.join(sorted(SLOT_STRATEGIES))}."
            )
        return asked

    # A window on the record IS the request.
    start, end = date_window(values, step)
    if start is not None or end is not None:
        return STRATEGY_IN_RANGE

    # ── NO WINDOW: REFUSE. ──────────────────────────────────────────────────
    # Deliberately not a fallback to "earliest". See the docstring: booking a
    # date nobody agreed to is irreversible and costs a charge; refusing costs
    # someone typing two fields, before a browser opens.
    from_key = (step or {}).get("date_from_key") or "date_from"
    to_key = (step or {}).get("date_to_key") or "date_to"
    raise WaitlistStepError(
        f"This client has no appointment date range, so there is nothing to "
        f"book towards. Add {from_key} and {to_key} (both YYYY-MM-DD, both "
        f"ends inclusive — the same date twice asks for a single day) to the "
        f"client record. If the client genuinely wants whatever is soonest, "
        f"say so explicitly with \"slot_strategy\": \"earliest\" — a booking "
        f"is never made on a guessed date.")


def check_window(values: Optional[Dict[str, Any]],
                 step: Optional[Dict[str, Any]] = None,
                 today: Optional[date] = None,
                 max_months: int = 0) -> List[str]:
    """Problems with the requested window, as plain sentences. [] when usable.

    OFFLINE, and called before a browser starts. Every one of these is a
    mistake that would otherwise be discovered after a login — and a login is
    the scarce resource here, not CPU: VFS blocks an account after roughly
    three in a short window, and that block outlives a 12-hour invitation.

    Returns a list rather than raising so an operator sees EVERY problem with
    what they typed at once, instead of fixing one and resubmitting to find the
    next.
    """
    values = values or {}
    step = step or {}
    today = today or date.today()

    from_key = step.get("date_from_key") or "date_from"
    to_key = step.get("date_to_key") or "date_to"
    raw_from = values.get(from_key)
    raw_to = values.get(to_key)
    start, end = date_window(values, step)

    problems: List[str] = []

    for raw, parsed, key in ((raw_from, start, from_key), (raw_to, end, to_key)):
        if raw not in (None, "") and parsed is None:
            problems.append(
                f"{key}: {raw!r} is not a date in YYYY-MM-DD form.")

    # An unparseable value was already reported above. Saying "needs BOTH ends"
    # as well would be wrong AND misleading: both ends WERE supplied, one of
    # them just could not be read, and an operator told to add the missing end
    # would go looking for a field they had already filled in.
    if problems:
        return problems

    if start is None and end is None:
        return problems          # no window asked for; another strategy applies

    if start is None or end is None:
        missing = from_key if start is None else to_key
        problems.append(
            f"{missing}: a date range needs BOTH ends. Give the same date "
            "twice to ask for a single day.")
        return problems

    if end < start:
        problems.append(
            f"{from_key} {start} is after {to_key} {end} — the range is "
            "backwards.")

    if end < today:
        problems.append(
            f"the whole range ends {end}, which is in the past.")
    elif start < today:
        # Not fatal: a window that started yesterday and runs another week is
        # still bookable for the part that remains, and refusing it would fail
        # a booking the agent can still honour.
        log.info(f"Requested range starts {start}, before today ({today}) — "
                 f"only {today}..{end} can be booked.")

    if max_months:
        horizon = _months_ahead(today, int(max_months))
        if start > horizon:
            problems.append(
                f"{from_key} {start} is beyond the {max_months} month(s) this "
                f"route searches (to {horizon}). Raise max_months_ahead or "
                "bring the range forward.")

    return problems


def _months_ahead(start: date, months: int) -> date:
    """The last day roughly `months` ahead. Calendar arithmetic, no dateutil."""
    month = start.month - 1 + max(0, int(months))
    year = start.year + month // 12
    month = month % 12 + 1
    day = monthrange(year, month)[1]
    return date(year, month, day)


def dates_in_window(dates: List[str], start: Optional[date],
                    end: Optional[date]) -> List[str]:
    """Those of `dates` that fall inside [start, end]. Order preserved."""
    if start is None and end is None:
        return list(dates)
    keep = []
    for text in dates:
        parsed = _as_date(text)
        if parsed is None:
            continue
        if start is not None and parsed < start:
            continue
        if end is not None and parsed > end:
            continue
        keep.append(text)
    return keep


@dataclass
class StepReport:
    """What happened on one page of the walk.

    Carries the page's own text and URL even on failure — a step that could not
    be completed is far more useful reported with what was actually on screen
    than as a bare error.
    """

    name: str
    url: str = ""
    ok: bool = False
    detail: str = ""
    found: Dict[str, Any] = field(default_factory=dict)
    page_text: str = ""
    html_path: str = ""      # where this page's DOM was saved, if it was
    image_path: str = ""     # screenshot of a FAILED page; the prod artifact

    def summary(self) -> str:
        mark = "OK  " if self.ok else "STOP"
        line = f"[{mark}] {self.name}"
        if self.detail:
            line += f" — {self.detail}"
        return line


@dataclass
class WalkResult:
    steps: List[StepReport] = field(default_factory=list)
    stopped_at: str = ""
    reason: str = ""
    payment_declined: bool = False
    """The gateway answered and REFUSED the payment. The click happened, so
    this is never safe to retry blind: VFS's failure page says funds may still
    have been deducted and the appointment may still confirm."""

    blocked: bool = False
    """VFS refused on ACCOUNT STATE — a booking already in flight — not on a
    selector or a page. Distinct from an ordinary stop because the remedy is to
    check the account, and re-running risks a double booking."""

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)


# --------------------------------------------------------------------------- #
# Calendar                                                                     #
# --------------------------------------------------------------------------- #

def available_dates(page, calendar: Dict[str, Any]) -> List[str]:
    """Every bookable date currently shown, as ISO strings from data-date.

    Reads the attribute rather than the day number: `data-date="2026-09-07"` is
    unambiguous, whereas a day number repeats across months and is blank on the
    greyed leading/trailing cells.

    NOTE the selector is `date-availiable` — three i's. That is VFS's own
    spelling in their markup; matching `date-available` finds nothing at all.
    """
    selector = calendar.get("available_day", "td.fc-daygrid-day.date-availiable")
    attribute = calendar.get("day_date_attribute", "data-date")

    dates: List[str] = []
    try:
        cells = page.locator(selector)
        for index in range(cells.count()):
            value = cells.nth(index).get_attribute(attribute)
            if value:
                dates.append(value)
    except Exception as e:
        log.debug(f"Could not read available dates: {e}")
    return sorted(dates)


#: Pause between deliberate actions. Not anti-detection theatre: VFS's own
#: pages animate, and a click landing mid-transition hits the element that was
#: there a moment ago. It also keeps a walk legible when watched live.
HUMAN_PAUSE_MS = 1200


def _settle(page, why: str = "") -> None:
    """Wait for the page to stop moving before the next deliberate action."""
    try:
        page.wait_for_timeout(HUMAN_PAUSE_MS)
    except Exception:                                       # noqa: BLE001
        pass


def current_month(page, calendar: Dict[str, Any]) -> str:
    """The month the calendar is showing, e.g. "September 2026"."""
    selector = calendar.get("month_title", ".fc-toolbar-title")
    try:
        return (page.locator(selector).first.inner_text(timeout=5000) or "").strip()
    except Exception as e:                                  # noqa: BLE001
        log.debug(f"Could not read the calendar month: {e}")
        return ""


def _page_month(page, calendar: Dict[str, Any], key: str, default: str,
                direction: str, timeout_ms: int) -> bool:
    """Click one of FullCalendar's month arrows. False if the grid did not move.

    Returns a BOOLEAN rather than raising: running out of months is a normal end
    to a search, not a fault. The caller decides whether that is disappointing
    or expected.

    The title is read before and after and compared, because FullCalendar's
    arrows stay in the DOM at the end of their range and simply stop responding —
    clicking one happily "succeeds" while nothing changes, which would make a
    search loop forever.
    """
    before = current_month(page, calendar)
    selector = calendar.get(key, default)

    try:
        button = page.locator(selector).first
        if button.is_disabled(timeout=3000):
            log.info(f"Calendar will not move {direction} past "
                     f"{before or 'this month'}.")
            return False
        button.click(timeout=timeout_ms)
    except Exception as e:                                  # noqa: BLE001
        log.info(f"Could not page the calendar {direction}: {e}")
        return False

    page.wait_for_timeout(900)          # the grid redraws after the click
    after = current_month(page, calendar)
    if after and after == before:
        log.info(f"Calendar did not move {direction} past {before}.")
        return False

    log.info(f"Calendar moved {direction}: {before or '?'} -> {after or '?'}")
    return True


def retreat_month(page, calendar: Dict[str, Any],
                  timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS) -> bool:
    """Page the calendar BACK one month. False if it would not move.

    The counterpart to advance_month, needed because a search that pages forward
    has to be able to return to the month it decided on — only the rendered
    month has clickable cells.
    """
    return _page_month(page, calendar, "prev_month", "button.fc-prev-button",
                       "back", timeout_ms)


def advance_month(page, calendar: Dict[str, Any],
                  timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS) -> bool:
    """Page the calendar forward one month. False if it would not move."""
    return _page_month(page, calendar, "next_month", "button.fc-next-button",
                       "forward", timeout_ms)


def pick_date(page, calendar: Dict[str, Any], date: str,
              timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS) -> None:
    """Click one calendar day by its ISO date."""
    from src.waitlist.errors import WaitlistStepError

    selector = calendar.get("available_day", "td.fc-daygrid-day.date-availiable")
    attribute = calendar.get("day_date_attribute", "data-date")
    cell = page.locator(f'{selector}[{attribute}="{date}"]').first

    try:
        cell.wait_for(state="visible", timeout=timeout_ms)
        cell.scroll_into_view_if_needed(timeout=5000)
        cell.click(timeout=timeout_ms)
    except Exception as e:
        raise WaitlistStepError(f"Could not click the date {date}: {e}") from e


# --------------------------------------------------------------------------- #
# Time slots                                                                   #
# --------------------------------------------------------------------------- #

def available_times(page, slots: Dict[str, Any]) -> List[str]:
    """The times offered for the chosen date, in the order the page lists them.

    The table only populates AFTER a date is clicked, so calling this first
    correctly returns nothing.
    """
    container = slots.get("container", "table.ba-slot-table")
    time_cell = slots.get("time_cell", "td[id^='tv']")

    times: List[str] = []
    try:
        cells = page.locator(f"{container} {time_cell}")
        for index in range(cells.count()):
            text = (cells.nth(index).inner_text(timeout=3000) or "").strip()
            if text:
                times.append(text)
    except Exception as e:
        log.debug(f"Could not read slot times: {e}")
    return times


def _click_slot(label, inner: str, timeout_ms: int) -> str:
    """Click one slot label, working around the radio that sits on top of it.

    THE INPUT INTERCEPTS THE LABEL. The markup is

        <div class="ba-slot-box">
          <input type="radio" tabindex="-1" class="ba-slot-radio" id="STRadio5">
          <label class="ba-slot-radio-label" for="STRadio5">
            <div class="ba-slot-radio-label-inner">
              <div class="ba-slot-radio-label-text1">Select</div>

    and the <input> is a SIBLING that paints over the label's centre point.
    Playwright hit-tests before clicking, sees the input there, and refuses —
    for 45 seconds, ~70 retries, on the 2026-09-28 run:

        <input ... id="STRadio5" ...> intercepts pointer events

    The comment this replaces asserted the input was "visually replaced by"
    the label. It is not; it is merely tabindex=-1, which affects focus order
    and nothing about hit testing.

    So click the INNER text div instead. It is a descendant of the label, so it
    paints above the sibling input, and clicking it still activates the label
    and therefore the radio. The three fallbacks below cover a route whose
    markup differs, in decreasing order of how much the click resembles a
    user's:

      1. the inner "Select" div            — what a person actually clicks
      2. the label, forced past hit testing — right element, no hit test
      3. the radio, checked directly        — last resort; bypasses the label

    Returns which of those worked, for the log: when a slot pick starts failing
    after a VFS redeploy, the first thing worth knowing is whether it stopped
    taking the normal path.
    """
    from src.waitlist.errors import WaitlistStepError

    try:
        label.scroll_into_view_if_needed(timeout=5000)
    except Exception as e:                                      # noqa: BLE001
        log.debug(f"Could not scroll the slot into view: {e}")

    # A SHORT budget for the first attempt, not the step's full 45s. Three
    # attempts at 45s each is a 135s stall on a page where the slot is not held
    # and a competitor is clicking.
    #
    # 4s, specifically, because the failure this ladder exists for is a HIT TEST
    # that fails instantly and identically on every retry — the covering <input>
    # is not going to move. Playwright still spends the whole timeout retrying
    # it: the 2026-09-28 run burned 15s discovering in the first 100ms what it
    # already knew. Waiting longer cannot change the answer; it only delays the
    # fallback that does work.
    attempt_ms = max(4000, min(int(timeout_ms or 0) or 4000, 4000))

    inner_locator = label.locator(inner)
    try:
        if inner_locator.count():
            inner_locator.first.click(timeout=attempt_ms)
            return "inner"
    except Exception as e:                                      # noqa: BLE001
        log.info(f"Clicking the slot's '{inner}' did not take: {e}")

    # force=True skips the actionability checks, the intercept one included.
    # Safe HERE specifically because the element was already confirmed visible
    # and stable by the attempt above — what is being skipped is the hit test
    # that the overlapping input fails, not a check that the element is real.
    try:
        # The full step budget here, not attempt_ms: this is the attempt that
        # currently succeeds on VFS (2026-09-28), and unlike the hit-test above
        # its failures are the ordinary slow-page kind that more time does fix.
        label.click(timeout=timeout_ms, force=True)
        return "label-forced"
    except Exception as e:                                      # noqa: BLE001
        log.info(f"Force-clicking the slot label did not take: {e}")

    # Last resort. check() drives the input directly rather than through the
    # label, so an Angular handler bound to the LABEL would not fire. It is
    # listed last for that reason, not because it is less likely to work.
    try:
        label.evaluate(
            "el => { const i = el.ownerDocument.getElementById("
            "el.getAttribute('for')); if (i) { i.click(); } }")
        return "radio-direct"
    except Exception as e:                                      # noqa: BLE001
        raise WaitlistStepError(
            f"Could not select the time slot: the label's click was "
            f"intercepted and the radio would not take it either ({e})") from e


def pick_time(page, slots: Dict[str, Any], index: int = 0,
              timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS) -> str:
    """Select one time slot by row index. Returns the time chosen.

    Clicks the label's inner text, never the radio or the bare label — see
    _click_slot for why the obvious target does not work.
    """
    from src.waitlist.errors import WaitlistStepError

    container = slots.get("container", "table.ba-slot-table")
    select = slots.get("select", "label.ba-slot-radio-label")
    inner = slots.get("select_inner", "div.ba-slot-radio-label-text1")

    labels = page.locator(f"{container} {select}")
    try:
        count = labels.count()
    except Exception as e:
        raise WaitlistStepError(f"Could not find any time slots: {e}") from e

    if count == 0:
        raise WaitlistStepError(
            "No time slots are offered for this date. The slot may have been "
            "taken between the calendar rendering and this click — which is "
            "normal, not a fault.")
    if index >= count:
        raise WaitlistStepError(f"Asked for slot {index} but only {count} exist.")

    times = available_times(page, slots)
    chosen = times[index] if index < len(times) else f"slot {index}"

    how = _click_slot(labels.nth(index), inner, timeout_ms)
    log.info(f"Selected slot {index} ({chosen}) via {how}")
    return chosen


# --------------------------------------------------------------------------- #
# The walk                                                                     #
# --------------------------------------------------------------------------- #

def _page_text(page, limit: int = 400) -> str:
    try:
        return " ".join((page.inner_text("body", timeout=5000) or "").split())[:limit]
    except Exception:
        return ""


#: Where captured DOM lands, per route: captured/AE-CHE/, captured/AE-ESP/, …
#:
#: Per-route rather than one flat folder because capturing is something every
#: new country goes through, not a one-off for Switzerland. Twelve countries'
#: pages in one directory, distinguishable only by a timestamp prefix, would be
#: unusable exactly when it matters — mid-window, looking for the page that
#: broke last night's walk.
CAPTURE_ROOT = "captured"


def capture_dir(route: str = "") -> str:
    """The capture directory for a route. Falls back to the root if unknown."""
    route = (route or "").strip().upper()
    return os.path.join(CAPTURE_ROOT, route) if route else CAPTURE_ROOT


# --------------------------------------------------------------------------- #
# What a run leaves behind                                                     #
# --------------------------------------------------------------------------- #
#
# Norway is mapped. Capturing the rendered DOM of every page on every run was
# right while the flow was being discovered and is now just noise on disk — the
# operator reads logs, not markup.
#
# But "no artifacts" is wrong too, and the reason is narrow and specific: when
# a payment fails, the appointment is ALREADY BOOKED. That state cannot be
# reconstructed from a log line, cannot be reproduced (the slot is gone), and
# is the one failure that costs real money. So failures still leave a
# screenshot — small, readable at a glance, no directory of DOM dumps.
#
#   "off"     nothing written (unit tests, --no-capture)
#   "failure" a PNG when a step fails             <- THE PRODUCTION DEFAULT
#   "full"    PNG on failure + DOM on every page  <- mapping a new country
#
#: THE STEP CURRENTLY IN PROGRESS, for anything that has to report where a run
#: was when it was interrupted.
#:
#: WalkResult is only returned when walk_flow FINISHES, so a Ctrl-C mid-walk
#: leaves the caller holding None and reporting "(pre-walk)" — observed
#: 2026-09-29, where a run interrupted four seconds after the payment
#: disclaimer reported it had not started walking. On the one step that can
#: spend money, "where was it" is the entire question.
#:
#: A module global rather than a parameter because the reader is an exception
#: handler two frames up that has no access to the walk's locals.
CURRENT_STEP = ""


def current_step() -> str:
    """The step the walk is on right now, or "" if it is not walking."""
    return CURRENT_STEP


CAPTURE_OFF = "off"
CAPTURE_FAILURE = "failure"
CAPTURE_FULL = "full"
CAPTURE_MODES = (CAPTURE_OFF, CAPTURE_FAILURE, CAPTURE_FULL)

DEFAULT_CAPTURE = CAPTURE_FAILURE


#: A screenshot is evidence, never worth delaying a teardown for. Five seconds
#: is comfortably enough for a full-page VFS shot and short enough that it
#: cannot outlive the browser it is reading.
SCREENSHOT_TIMEOUT_MS = 5000


def _capture_shot(page, step_name: str, route: str = "") -> str:
    """Save a PNG of what the page looked like. Returns the path, or "".

    Best-effort, exactly like _capture_html was: a screenshot failure must
    never take down a walk. The walk is irreplaceable, the file is evidence.

    full_page=True because VFS puts the thing that failed below the fold about
    as often as above it — a viewport-only shot of review-pay shows the header
    and none of the consent checkboxes that blocked the submit.
    """
    try:
        directory = capture_dir(route)
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(directory, f"{stamp}_{step_name}.png")
        # BOUNDED. A full-page screenshot of a long VFS page takes real time,
        # and on 2026-09-29 a handover shot was still rendering when Chrome was
        # killed — leaving "Task was destroyed but it is pending" and a
        # TargetClosedError after the run had otherwise finished cleanly. The
        # default timeout is 30s, which is far longer than teardown waits.
        page.screenshot(path=path, full_page=True,
                        timeout=SCREENSHOT_TIMEOUT_MS)
        try:
            url = page.url or ""
        except Exception:                                   # noqa: BLE001
            url = ""
        log.info(f"  screenshot -> {path}" + (f"  ({url})" if url else ""))
        return path
    except Exception as e:                                  # noqa: BLE001
        log.warning(f"  could not screenshot '{step_name}': {e}")
        return ""


def _capture_html(page, step_name: str, route: str = "") -> str:
    """Save this page's rendered DOM. Returns the path, or "" if it could not.

    THE WHOLE POINT OF WALKING IS TO KEEP THE PAGE, NOT TO LOOK AT IT.

    An invitation is the only chance to see these pages, and the Swiss window is
    12 hours. Without this the walk printed a 400-character text summary, left
    the browser open, and expected a human to read selectors off the screen
    before the session died — so a walk run at 3am captured nothing, and the DOM
    was gone until the next invitation, which may be weeks away.

    `page.content()` is the RENDERED DOM, not the server's original HTML. That
    is what is wanted here: this portal is Angular, so the interesting markup —
    the calendar cells, the slot table — does not exist in the served response
    at all.

    Best-effort by design. A capture failure must never stop a walk that is
    racing a deadline: the walk is the irreplaceable part, the file is a
    convenience.
    """
    try:
        directory = capture_dir(route)
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(directory, f"{stamp}_{step_name}.html")
        # RECORD THE URL WITH THE DOM. page.content() is markup only — the
        # address bar is not in it — so seven pages were captured across three
        # runs and every "url_contains" gate in the Norway config still said
        # "this page URL has never been observed". The one exception was
        # review-pay, and only by luck: it happens to carry a hidden
        # <input id="URL"> that OneTrust posts back.
        #
        # An HTML comment rather than a sidecar file: it travels with the
        # capture when one gets copied out or attached to a message, and it
        # cannot go missing separately from the DOM it describes.
        try:
            url = page.url or ""
        except Exception:                                   # noqa: BLE001
            url = ""

        with io.open(path, "w", encoding="utf-8") as fh:
            if url:
                fh.write(f"<!-- captured from: {url} -->\n")
            fh.write(page.content())
        log.info(f"  captured DOM -> {path}" + (f"  ({url})" if url else ""))
        return path
    except Exception as e:
        log.warning(f"  could not capture DOM for '{step_name}': {e}")
        return ""


def _step_page_present(page, step: Dict[str, Any]) -> bool:
    """Is the page this step describes actually on screen right now?

    A SHORT probe, not a wait: an optional step that is absent must cost ~a
    second, not a full step timeout. Uses whichever marker the step gives —
    "present_when" (CSS), else "wait_for_text", else "url_contains".

    A step marked "if_present" with NO marker is treated as present, so the
    flag can never silently skip a step nobody told it how to recognise.
    """
    selector = step.get("present_when")
    if selector:
        try:
            return page.locator(selector).first.is_visible(timeout=3000)
        except Exception as e:                              # noqa: BLE001
            log.debug(f"Could not probe for {selector!r}: {e}")
            return False

    text = step.get("wait_for_text")
    if text:
        try:
            return page.get_by_text(text, exact=False).first.is_visible(
                timeout=3000)
        except Exception as e:                              # noqa: BLE001
            log.debug(f"Could not probe for the text {text!r}: {e}")
            return False

    fragment = step.get("url_contains")
    if fragment:
        return fragment.lower() in (page.url or "").lower()

    log.warning(
        f"Step '{step.get('name', '?')}' is marked \"if_present\" but gives no "
        "\"present_when\", \"wait_for_text\" or \"url_contains\" to recognise it "
        "by — treating it as present.")
    return True


def _picked_slot(result) -> str:
    """The date and time the walk chose, from the step that chose them.

    Read back off the select_slot report rather than tracked separately: that
    report is already the record of what was picked, and a second copy is a
    second thing to keep in step. Returns "" when no slot step has run, which
    is correct for a route whose commit boundary is elsewhere.
    """
    for report in result.steps:
        found = report.found or {}
        date = found.get("chosen_date") or ""
        time_ = found.get("chosen_time") or ""
        if date or time_:
            return f"{date} {time_}".strip()
    return ""


class _PaymentRun:
    """What `_step_payment` needs from a BookingRun, for the walk's use.

    The walk has no BookingRun — it reports through WalkResult. The payment
    step only reads `reference`/`slot` (to label the journal row) and appends
    to `captures`, so this supplies exactly that rather than making the walk
    depend on the runner's result type.
    """

    def __init__(self, slot: str = "", registrant_id: str = ""):
        self.reference = ""
        # Passed in rather than left blank: the journal row is the only record
        # of WHICH booking was paid for, and on the 2026-09-29 live run it read
        # `booking_ref: ""` because this was constructed empty. A row that
        # cannot name its booking still proves a payment happened, but not
        # which one — and that is the question asked when a run has to be
        # resolved by hand.
        self.slot = slot or ""
        self.registrant_id = registrant_id or ""
        self.captures = []


def walk_flow(page, route: str, to_step: Optional[str] = None,
              dry_run: bool = True, entry: str = "waitlist",
              values: Optional[Dict[str, Any]] = None,
              capture: str = DEFAULT_CAPTURE) -> WalkResult:
    """Walk the configured booking steps from wherever the page currently is.

    `dry_run` (default) refuses to submit the committing step — the run stops in
    front of it and says so. That is deliberate: today "commits" sits on the
    slot pick only because the payment step does not exist yet, so honouring it
    keeps the walk from clicking past a boundary whose real position is unknown.

    `to_step` stops after a named step, for capturing one page at a time.

    `capture` decides what a run leaves on disk: "failure" (the default) writes
    a screenshot only when a step fails, "full" adds the rendered DOM of every
    page, "off" writes nothing. See CAPTURE_MODES above for why the failure
    case is not simply dropped.
    """
    from src.booking import config as booking_config
    from src.payment.gateway import PaymentDeclined
    from src.vfs_bot.turnstile import BlockingDialogError
    from src.waitlist.register import _await_page, _click

    result = WalkResult()
    steps = booking_config.steps_for(route, entry)

    global CURRENT_STEP
    for step in steps:
        name = step.get("name", "?")
        CURRENT_STEP = name
        report = StepReport(name=name)

        if step.get("type") in ("dashboard_resume", "identity_assert"):
            # Handled by the probe before the walk starts.
            continue

        try:
            timeout_ms = int(step.get("timeout_ms") or DEFAULT_STEP_TIMEOUT_MS)

            # A step the portal may or may not show. Skipped when its marker is
            # absent, rather than waited for and then fought:
            #
            # Norway's "details_summary" is described from supplied DOM and has
            # never actually been observed. On the 2026-09-26 run the walk sat on
            # it looking for a Continue button that was not there, warned, clicked
            # nothing, and carried on — 20s spent proving a page was missing. If
            # VFS does show it on some sessions, this still fills and submits it.
            if step.get("if_present") and not _step_page_present(page, step):
                report.ok = True
                report.url = page.url
                report.detail = ("not shown by the portal this session — skipped "
                                 "(\"if_present\": true)")
                log.info(f"  {name}: not on screen — skipping (if_present).")
                result.steps.append(report)
                continue

            _await_page(page, step, timeout_ms)
            report.url = page.url
            report.page_text = _page_text(page)
            # Only while MAPPING a route. On a mapped flow this wrote one DOM
            # dump per page per run and nobody read them — the operator reads
            # logs. Captured BEFORE anything is clicked, because the arriving
            # state is what a config has to describe.
            if capture == CAPTURE_FULL:
                report.html_path = _capture_html(page, name, route)

            _dwell(page, step, "settle_seconds",
                   "letting the page settle before filling")

            if step.get("type") == "slot_pick":
                _do_slot_pick(page, step, report, timeout_ms)
            elif step.get("type") == "form" and step.get("fields"):
                _fill_form(page, step, report, values or {}, timeout_ms)
            elif step.get("scroll_to_bottom"):
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(500)

            # THE PORTAL'S OWN STATED MINIMUM, and it is enforced: Norway's
            # "Your Details" renders "Warning: Please wait 4 seconds before
            # saving your details and continuing", and Switzerland's asks for
            # 32. Submitting early is rejected. The walk ignored this key
            # entirely, so a config that set it was silently disobeyed.
            _dwell(page, step, "dwell_seconds",
                   "the portal requires it before submitting")

            # A "payment" step is not a form: it follows VFS into the popup
            # it opens and drives a third-party gateway. Handled by the runner,
            # which owns the journal + commit discipline that step needs, so
            # the walk delegates rather than growing a second copy of it.
            if step.get("type") == "payment" and not dry_run:
                from src.booking import runner as runner_mod

                ctx = runner_mod._StepContext(
                    page=page, step=step, route=route,
                    run=_PaymentRun(
                        slot=_picked_slot(result),
                        registrant_id=str((values or {}).get(
                            "registrant.id", ""))),
                    values=values or {},
                    expected_name="", expected_reference="")
                runner_mod._step_payment(ctx)
                report.ok = True
                report.detail = "payment submitted"
                result.steps.append(report)
                result.stopped_at = name
                result.reason = "payment submitted — see logs/payments.jsonl"
                return result

            if step.get("commits") and dry_run:
                # THE PAGE IS LEFT ARMED. Every field is filled and the submit
                # control has gone live — on Norway's review-pay that means the
                # terms box is ticked and "Pay Online" is no longer
                # disabled="true". One click in the open browser completes the
                # booking and leaves VFS for the payment gateway.
                #
                # Say that explicitly. The old wording ("Re-run with --commit
                # once the payment step exists and this flag has moved to it")
                # was written when nobody had seen this page and the flag was an
                # admitted placeholder on select_slot. Both halves are now
                # false: the flag IS on the last step, and there is no later one
                # to move it to. A stale instruction on the one screen where the
                # next click spends real money is worse than none.
                armed = _submit_is_live(page, step.get("submit"))
                report.ok = True
                report.detail = (
                    "reached the committing step — NOT submitting (dry run). "
                    + ("The page is ready: the submit control is enabled, so "
                       "clicking it by hand in the open browser will commit."
                       if armed else
                       "NOTE: the submit control is still disabled, so a "
                       "required field did not take — check the capture before "
                       "clicking anything."))
                result.steps.append(report)
                result.stopped_at = name
                result.reason = (
                    "dry run stopped at the commit boundary"
                    + (" — page armed, submit enabled" if armed
                       else " — submit still DISABLED, page not ready"))
                return result

            submit = step.get("submit")
            if submit:
                _await_enabled_safe(page, submit)
                _click(page, submit, f"step '{name}' submit", timeout_ms)
                page.wait_for_timeout(1500)

            report.ok = True
            report.detail = f"completed; now at {page.url}"

        except PaymentDeclined as e:
            # THE GATEWAY ANSWERED, AND THE ANSWER WAS NO — but the click
            # happened, so this is not an ordinary step failure and must never
            # be retried automatically. VFS's own failure page says funds may
            # still have been deducted, so "declined" is not "nothing
            # happened". The journal already carries the result row.
            log.error(f"PAYMENT DECLINED at '{name}': {e}")
            report.ok = False
            report.detail = str(e)
            report.url = page.url
            report.page_text = _page_text(page)
            if capture != CAPTURE_OFF:
                report.image_path = _capture_shot(page, f"{name}_DECLINED",
                                                  route)
            result.steps.append(report)
            result.stopped_at = name
            result.reason = str(e)
            result.payment_declined = True
            return result

        except BlockingDialogError as e:
            # NOT an ordinary step failure. VFS is saying this account already
            # has a booking in flight, so the correct response is to stop the
            # whole run and tell a human — never to try the next step, and
            # never to re-run. Logged at ERROR because it is the one failure
            # whose remedy is "go and look at the account", not "fix a
            # selector".
            log.error(f"BLOCKED at '{name}': {e}")
            report.ok = False
            report.detail = str(e)
            report.url = page.url
            report.page_text = _page_text(page)
            if capture != CAPTURE_OFF:
                report.image_path = _capture_shot(page, f"{name}_BLOCKED",
                                                  route)
            result.steps.append(report)
            result.stopped_at = name
            result.reason = str(e)
            result.blocked = True
            return result

        except Exception as e:
            report.ok = False
            report.detail = str(e)
            report.url = page.url
            report.page_text = _page_text(page)
            # The page that BROKE the walk is the one worth keeping. A PNG
            # rather than the DOM: it is readable at a glance, and what the
            # operator needs from a failure is "what was on screen", not
            # markup to write selectors from — that job is done for Norway.
            #
            # "full" still keeps the DOM as well, because a failure while
            # MAPPING is exactly when the markup is the point.
            if capture != CAPTURE_OFF:
                report.image_path = _capture_shot(page, f"{name}_FAILED", route)
                if capture == CAPTURE_FULL and not report.html_path:
                    report.html_path = _capture_html(
                        page, f"{name}_FAILED", route)
            result.steps.append(report)
            result.stopped_at = name
            result.reason = str(e)
            return result

        result.steps.append(report)
        if to_step and name == to_step:
            result.stopped_at = name
            result.reason = f"stopped after '{name}' as asked"
            return result

    return result


#: Where VFS renders its own "please wait N seconds" countdown, and the number
#: in it. The element is a live countdown — it re-renders every second — so it
#: is both the authority on how long to wait and the way to tell when the wait
#: is over.
COUNTDOWN_SELECTOR = "div#mintime"
COUNTDOWN_PATTERN = re.compile(r"wait\s+(\d+)\s+second", re.I)

#: Ceiling on a countdown read off the page, so a misparse cannot hang a run.
MAX_COUNTDOWN_SECONDS = 120


def _stated_wait(page) -> int:
    """The seconds VFS is currently asking for, or 0 if it is not asking.

    Read from the page rather than taken from the config because THE NUMBER
    VARIES BY SESSION, not just by route. Norway's "Your Details" asked for 4
    seconds in the DOM captured on 2026-09-26 and 30 seconds on the run half an
    hour later; the config said 10, so Save was clicked with 12 seconds still on
    the clock and was silently rejected. The walk then spent its next step
    looking for a Continue button on a page it had never left.
    """
    try:
        block = page.locator(COUNTDOWN_SELECTOR).first
        if block.count() == 0:
            return 0
        text = block.inner_text(timeout=3000) or ""
    except Exception as e:                                  # noqa: BLE001
        log.debug(f"Could not read the portal countdown: {e}")
        return 0

    match = COUNTDOWN_PATTERN.search(text)
    if not match:
        return 0
    return min(int(match.group(1)), MAX_COUNTDOWN_SECONDS)


def _await_countdown(page, why: str) -> bool:
    """Wait out VFS's own countdown, re-reading it as it ticks. True if it ran.

    Polls instead of sleeping the first number it sees: the countdown is live, so
    re-reading costs nothing and tells us when it has actually finished rather
    than when we predicted it would.
    """
    seconds = _stated_wait(page)
    if seconds <= 0:
        return False

    log.info(f"  the portal is asking for {seconds}s ({why}) — waiting it out...")
    deadline = time.time() + seconds + 5      # margin for the last tick
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        remaining = _stated_wait(page)
        if remaining <= 0:
            log.info("  the portal's countdown has cleared.")
            return True
        if remaining > seconds:               # a fresh, longer countdown
            deadline = time.time() + remaining + 5
        seconds = remaining

    log.warning(f"  the portal still shows a {_stated_wait(page)}s countdown "
                "after waiting it out — submitting anyway.")
    return True


def _dwell(page, step: Dict[str, Any], key: str, why: str) -> None:
    """Wait as long as the step configures — or as long as the PAGE demands.

    For the pre-submit wait the page wins when it asks for more: VFS states its
    own minimum and enforces it, and that number is not a constant (see
    _stated_wait). The configured value stays the floor, so a portal that gates
    on something it does not display is still honoured.

    Delegates the plain sleep to registration's implementation rather than
    repeating it: both halves are waiting on the same portal for the same
    reason, and two copies of a timing rule is how they drift apart.
    """
    from src.waitlist.register import _dwell as waitlist_dwell

    waitlist_dwell(page, step, key, why)
    if key == "dwell_seconds":
        _await_countdown(page, why)


def _fill_form(page, step: Dict[str, Any], report: StepReport,
               values: Dict[str, Any], timeout_ms: int) -> None:
    """Fill a form step's fields, reusing the waitlist field engine.

    The walk could not fill anything before this, which made it useless on the
    live-slot flow: Norway's first page is three mandatory dropdowns and its
    Continue button stays disabled until all three are chosen, so a walk that
    only reads pages stops dead on step 1 of 5.

    waitlist.fields already drives eight widget types on this exact portal
    across seven routes, including the cdk-overlay dance a mat-select needs. A
    second implementation here would be a second set of bugs on the same pages.
    """
    from src.waitlist import fields as fields_mod

    specs = step.get("fields") or []

    # fill_all's 4th argument is WHERE — a label for log messages — not a
    # timeout. Both booking call sites passed timeout_ms into it, which does
    # not crash; it just writes "Field X (45000)" into every log line, which is
    # worse than useless when a field fails and the log is all you have.
    #
    # Filled ONE AT A TIME with a settle between, because these dropdowns are
    # DEPENDENT: Norway's sub-category list is fetched only after a category is
    # chosen, so filling them back to back races an empty panel. fill_all has
    # no pause between fields.
    for index, spec in enumerate(specs):
        if spec.get("disabled"):
            continue
        fields_mod.fill_one(page, spec, values,
                            where=f"{step.get('name', '?')}.{spec.get('name')}")
        if index < len(specs) - 1:
            _settle(page, "letting a dependent field load")
    report.found["filled"] = [f.get("name") for f in specs]
    log.info(f"  filled {len(specs)} field(s): "
             f"{', '.join(str(f.get('name')) for f in specs)}")


def _reveal_date(page, calendar: Dict[str, Any], date: str,
                 timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
                 max_steps: int = 24) -> bool:
    """Page the calendar back until `date` is rendered. True if it is on screen.

    Only the month currently drawn has cells in the DOM, so a date found earlier
    in a search cannot be clicked once the search has moved on. Walks BACKWARDS
    because every caller arrives here having paged forward past its target.

    Presence is decided with `available_dates` rather than a locator of its own:
    that is the function that already knows how a bookable day is spelled on this
    portal (`date-availiable`, three i's), and a second reading of the same thing
    is a second place for that to go stale.

    Reports rather than raises when it runs out of months: pick_date gives the
    better error — it names the date and the click that failed — and duplicating
    that judgement here would mean two places deciding what a missing day means.
    """
    for _ in range(max_steps):
        if date in available_dates(page, calendar):
            return True
        if not retreat_month(page, calendar, timeout_ms):
            log.info(
                f"Calendar would not page back any further looking for {date}.")
            return False
    return False


#: How long to wait for the slot table to render after a date is clicked. VFS
#: fetches the day's times before drawing it, so this is a network round trip,
#: not an animation.
SLOT_TABLE_TIMEOUT_MS = 30000


def _await_time_table(page, slots: Dict[str, Any],
                      timeout_ms: int = SLOT_TABLE_TIMEOUT_MS) -> bool:
    """Wait for the time table to appear after a date click. True if it did.

    Returns rather than raises when it never arrives: a day really can have no
    times left, and pick_time already says that well. The distinction this draws
    is between "waited properly and there are none" and "read the DOM before it
    was drawn" — only the first is a real answer.
    """
    from src.vfs_bot import turnstile

    container = slots.get("container", "table.ba-slot-table")
    select = slots.get("select", "label.ba-slot-radio-label")
    wait_ms = min(int(timeout_ms or SLOT_TABLE_TIMEOUT_MS),
                  SLOT_TABLE_TIMEOUT_MS)
    try:
        turnstile.wait_for_loader(page)
        # WAIT FOR A CLICKABLE SLOT, NOT THE TABLE. The <table> is drawn with
        # its header rows ("Time", "Standard", "Appointments within usual
        # opening hours") before the fetched slots are appended, so waiting on
        # the container returns while the rows are still arriving. The walk then
        # read a correct list of times and clicked a label that Angular replaced
        # underneath it — "element was detached from the DOM, retrying", the
        # 2026-09-28 run.
        page.locator(f"{container} {select}").first.wait_for(
            state="visible", timeout=wait_ms)
        page.wait_for_timeout(600)
        # Again AFTER the settle: ngx-ui-loader throws its overlay up a second
        # time while the day's allocation tokens are fetched, and that overlay
        # covers the whole page. It was the other interceptor in that same run.
        turnstile.wait_for_loader(page)
        return True
    except Exception as e:                                  # noqa: BLE001
        log.info(f"No bookable time slot appeared within {wait_ms}ms: {e}")
        return False


def _pick_appointment_type(page, spec: Optional[Dict[str, Any]],
                           report: StepReport, timeout_ms: int) -> None:
    """Choose the appointment type, if the route names one.

    Does nothing when the route declares none — most offer a single type. When
    the control is declared "if_present" and absent, that is reported and
    skipped rather than raised: VFS varies which controls it renders, and
    Norway's one radio arrives already checked.

    Clicks only when the radio is not ALREADY selected, so a pre-checked default
    is left alone rather than toggled — and reports what it found either way, so
    a route whose default is not what was asked for is visible in the report.
    """
    from src.waitlist.errors import WaitlistStepError

    if not spec:
        return

    label = spec.get("label")
    selector = spec.get("selector") or (
        f"mat-radio-button:has-text('{label}')" if label else "mat-radio-button")

    try:
        radio = page.locator(selector).first
        if radio.count() == 0:
            if spec.get("if_present"):
                log.info(f"  appointment type {label!r} not on this page — "
                         "skipped (\"if_present\": true).")
                report.found["appointment_type"] = "not present"
                return
            raise WaitlistStepError(
                f"No appointment type matching {selector!r} on this page.")

        classes = (radio.get_attribute("class", timeout=3000) or "")
        if "mat-mdc-radio-checked" in classes:
            log.info(f"  appointment type {label or selector!r} is already "
                     "selected — left as it is.")
            report.found["appointment_type"] = f"{label or selector} (pre-selected)"
            return

        radio.scroll_into_view_if_needed(timeout=5000)
        radio.click(timeout=timeout_ms)
        page.wait_for_timeout(800)
        log.info(f"  picked appointment type {label or selector!r}.")
        report.found["appointment_type"] = label or selector
    except WaitlistStepError:
        raise
    except Exception as e:                                  # noqa: BLE001
        if spec.get("if_present"):
            log.info(f"  could not pick the appointment type, continuing "
                     f"(\"if_present\": true): {e}")
            report.found["appointment_type"] = f"skipped: {e}"
            return
        raise


def _do_slot_pick(page, step: Dict[str, Any], report: StepReport,
                  timeout_ms: int,
                  values: Optional[Dict[str, Any]] = None) -> None:
    """Choose a date and a time, recording what was on offer.

    `values` carries the client's requested date window for the "in_range"
    strategy. Optional so the probe/walk path keeps working without one.
    """
    from src.waitlist.errors import WaitlistStepError

    calendar = step.get("calendar") or {}
    slots = step.get("time_slots") or {}

    # PICK THE APPOINTMENT TYPE FIRST, if the route names one. The config has
    # carried "appointment_type" since it was written and nothing read it —
    # the same inert-key problem "strategy" had. Norway gets away with it
    # because its single radio ("Choose a slot") ships pre-checked, so the
    # calendar responds anyway; a route offering Standard vs Premium would
    # silently book whichever VFS defaulted to.
    _pick_appointment_type(page, step.get("appointment_type"), report,
                           timeout_ms)

    # Search forward month by month. VFS opens the calendar on the current
    # month, which is routinely full — the offered dates sit weeks out — so a
    # walk that only ever read the first month reported "no slots" while the
    # portal was showing plenty one click away.
    max_months = int(step.get("max_months_ahead") or DEFAULT_MAX_MONTHS_AHEAD)
    months_seen: List[str] = []
    dates: List[str] = []

    # WHICH date, from the step's strategy. Resolved BEFORE the calendar search
    # because it decides how far that search has to go, not just which of the
    # results to take. The config carried "strategy" from the day it was written
    # and nothing read it — the walk always took dates[0] — so a route asking
    # for anything else was silently ignored.
    strategy = resolve_strategy(values, step, slots)

    # THE REQUESTED WINDOW, for "in_range". Checked here as well as offline
    # because _do_slot_pick is reachable from the runner, the probe and a test,
    # and a strategy that silently ignored a malformed window would book an
    # arbitrary date — the single worst outcome this function has.
    window_from, window_to = (None, None)
    if strategy == STRATEGY_IN_RANGE:
        problems = check_window(values, step, max_months=max_months)
        if problems:
            raise WaitlistStepError(
                "The requested date range cannot be used: "
                + " ".join(problems))
        window_from, window_to = date_window(values, step)
        if window_from is None or window_to is None:
            raise WaitlistStepError(
                "Strategy 'in_range' needs date_from and date_to on the "
                "client record, and neither was supplied. Set them, or use "
                "the 'earliest' strategy to take whatever is offered.")
        report.found["window"] = f"{window_from}..{window_to}"
        log.info(f"Requested window: {window_from} .. {window_to}")

    # THE SEARCH STOPS AT THE FIRST MONTH THAT OFFERS ANYTHING, under EITHER
    # strategy. Paging on "just in case" is not free and does not pay:
    #
    #   - It cost ~37s per empty month on the 2026-09-26 run (November had 17
    #     dates; December, January and February were then read and all empty),
    #     and a slot is NOT HELD while the walk is on this page. Every second
    #     spent reading months nobody can book is a second a competitor has.
    #   - VFS publishes a rolling window, so the months past the first
    #     available one are empty by construction, not by chance.
    #
    # "latest" therefore means the latest date IN THE MONTH THE PORTAL IS
    # OFFERING, not the latest date reachable by paging — which is the same slot
    # in practice and cheaper to find. Paging forward remains only for the case
    # this loop exists for: the CURRENT month being full, which is routine.
    # WHEN TO STOP PAGING depends on the strategy, and the difference is the
    # reason in_range exists.
    #
    #   earliest / latest   stop at the first month offering ANYTHING. VFS
    #                       publishes a rolling window, so later months are
    #                       empty by construction, and each empty month cost
    #                       ~37s on the 2026-09-26 run while the slot is not
    #                       held.
    #
    #   in_range            a month offering dates that all fall OUTSIDE the
    #                       requested window is no better than an empty one.
    #                       Stopping there would report "no dates in range"
    #                       while the agent's window sat one page ahead. So it
    #                       keeps paging until it finds a date that is actually
    #                       usable — or runs out of months.
    for attempt in range(max_months):
        month = current_month(page, calendar)
        found = available_dates(page, calendar)
        usable = (dates_in_window(found, window_from, window_to)
                  if strategy == STRATEGY_IN_RANGE else found)

        months_seen.append(f"{month or '?'}({len(found)})")
        log.info(f"{month or 'calendar'}: {len(found)} available date(s)"
                 + (f" — {', '.join(found)}" if found else "")
                 + (f" [{len(usable)} in range]"
                    if strategy == STRATEGY_IN_RANGE and found else ""))

        if usable:
            dates = usable
            break

        # PAST THE END OF THE WINDOW — stop, do not spend the remaining months.
        # Calendars run forwards, so once a month's earliest offered date is
        # already beyond window_to, no later month can help.
        if (strategy == STRATEGY_IN_RANGE and found and window_to is not None):
            earliest_here = _as_date(found[0])
            if earliest_here and earliest_here > window_to:
                log.info(f"{month or 'calendar'} already offers "
                         f"{earliest_here}, past the end of the window "
                         f"({window_to}) — no later month can match.")
                break

        if attempt == max_months - 1:
            break
        _settle(page, "before paging the calendar")
        if not advance_month(page, calendar, timeout_ms):
            break

    report.found["months_searched"] = months_seen
    report.found["available_dates"] = dates
    report.found["month"] = current_month(page, calendar)

    if not dates:
        if strategy == STRATEGY_IN_RANGE:
            # A NORMAL OUTCOME, and it must read like one. The agent asked for
            # a window; VFS is not offering it. Nothing is wrong, nothing needs
            # debugging, and the booking must NOT fall back to another date.
            raise WaitlistStepError(
                f"No appointment is available between {window_from} and "
                f"{window_to}. Months searched: {', '.join(months_seen)}. "
                "Nothing was booked — the requested range is simply not on "
                "offer. Ask the agent for a wider range, or try again later.")
        raise WaitlistStepError(
            "No available dates in any of the months searched "
            f"({', '.join(months_seen)}). Another applicant may have taken "
            "them, or they may be further ahead than max_months_ahead.")

    # available_dates() returns them sorted, so the ends of the list are the
    # earliest and latest offered.
    # in_range takes the EARLIEST inside the window: the client attends an
    # office rather than catching a flight, so sooner is strictly better, and
    # the earliest dates are the ones a competitor takes first.
    chosen_date = dates[-1] if strategy == "latest" else dates[0]
    report.found["strategy"] = strategy
    log.info(f"Strategy '{strategy}' of {len(dates)} date(s) -> {chosen_date}")

    _settle(page, "before picking a date")
    # CONFIRM THE CELL IS ON SCREEN BEFORE CLICKING IT. pick_date addresses the
    # day by data-date, and only the month FullCalendar is currently rendering has
    # cells in the DOM — a click on an absent one does not fail, it HANGS. That is
    # how the 2026-09-26 run ended: the search had paged on to February 2027, then
    # tried to click 2026-11-30 and left a pending Locator.click() behind.
    #
    # The search now stops on the month it chooses from, so this is normally a
    # no-op single read. It is kept because the failure it prevents is silent.
    _reveal_date(page, calendar, chosen_date, timeout_ms)
    pick_date(page, calendar, chosen_date, timeout_ms)
    report.found["chosen_date"] = chosen_date
    log.info(f"Picked date {chosen_date}")
    page.wait_for_timeout(1500)   # the slot table renders after the date click

    # WAIT FOR THE TIME TABLE TO ARRIVE, don't sleep and hope. VFS fetches the
    # day's slots after the date is clicked and renders the whole
    # "Choose an appointment time" block only once they land — the table is not
    # in the DOM at all before that, so reading it early sees zero times and
    # reports "the slot was taken", which is a different and alarming thing.
    #
    # That is exactly what happened on 2026-09-26: date clicked at 17:18:03,
    # times read at 17:18:04 after a flat 1500ms, "Date offers 0 time(s)".
    _await_time_table(page, slots, timeout_ms)

    times = available_times(page, slots)
    report.found["available_times"] = times
    log.info(f"Date offers {len(times)} time(s): {', '.join(times) or 'none'}")

    # The same strategy decides the TIME. Times come back in the order the page
    # lists them, which is chronological, so index 0 and -1 are first and last.
    # ANY TIME ON THE DAY, earliest first. The agent specifies a DATE range,
    # never a time: the client is attending an office, so the appointment just
    # has to be on an agreed day. "latest" remains the odd one out, and only
    # because it is a deliberate testing choice (see the route config).
    index = max(0, len(times) - 1) if strategy == "latest" else 0
    chosen_time = pick_time(page, slots, index=index, timeout_ms=timeout_ms)
    report.found["chosen_time"] = chosen_time
    log.info(f"Picked time {chosen_time} (index {index})")


def _submit_locator(page, submit: Any):
    """The submit control, from either spec shape. None if undescribable.

    Mirrors _click's resolution. Deliberately NOT filtered by visible=True: a
    disabled control is still visible, and this is asked precisely when the
    answer may be "disabled".
    """
    if isinstance(submit, str):
        return page.locator(submit).first
    if isinstance(submit, dict):
        if submit.get("selector"):
            return page.locator(submit["selector"]).first
        if submit.get("name"):
            return page.get_by_role(submit.get("role", "button"),
                                    name=submit["name"],
                                    exact=bool(submit.get("exact"))).first
    return None


def _submit_is_live(page, submit: Any) -> bool:
    """Is the submit control present and enabled? Best-effort, never raises.

    Used only to describe the page at the commit boundary, so an unreadable
    control reports as not-armed rather than crashing a walk that has otherwise
    succeeded — the browser is about to be handed to a person, and they can see
    the button for themselves.

    Checks _looks_disabled as well as is_enabled: Angular Material marks a
    logically-off button with mat-mdc-button-disabled while leaving it
    focusable, and "Pay Online" is exactly such a button. is_enabled() alone
    would call the page armed before the terms box was ticked.
    """
    from src.waitlist.register import _looks_disabled

    locator = _submit_locator(page, submit)
    if locator is None:
        return False
    try:
        return bool(locator.is_enabled(timeout=3000)
                    and not _looks_disabled(locator))
    except Exception as e:                                  # noqa: BLE001
        log.debug(f"Could not read the submit control's state: {e}")
        return False


def _await_enabled_safe(page, submit: Any) -> None:
    """Wait for a submit control to stop being disabled, tolerating absence.

    VFS renders Continue with disabled="true" until both a date and a time are
    chosen. Clicking it while disabled silently does nothing, so the flow would
    otherwise appear to hang on a live-looking button.
    """
    from src.waitlist.register import _await_enabled

    try:
        _await_enabled(page, submit, ENABLE_TIMEOUT_MS)
    except Exception as e:
        log.debug(f"Could not confirm the submit control is enabled: {e}")
