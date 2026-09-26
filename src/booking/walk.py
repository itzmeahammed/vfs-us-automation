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
SLOT_STRATEGIES = frozenset({"earliest", "latest"})


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


def advance_month(page, calendar: Dict[str, Any],
                  timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS) -> bool:
    """Page the calendar forward one month. False if it would not move.

    Returns a BOOLEAN rather than raising: running out of months is a normal
    end to a search, not a fault. The caller decides whether that is
    disappointing or expected.

    The title is read before and after and compared, because FullCalendar's
    next button stays in the DOM at the end of its range and simply stops
    responding — clicking it happily "succeeds" while nothing changes, which
    would make a search loop forever.
    """
    before = current_month(page, calendar)
    selector = calendar.get("next_month", "button.fc-next-button")

    try:
        button = page.locator(selector).first
        if button.is_disabled(timeout=3000):
            log.info(f"Calendar will not advance past {before or 'this month'}.")
            return False
        button.click(timeout=timeout_ms)
    except Exception as e:                                  # noqa: BLE001
        log.info(f"Could not advance the calendar: {e}")
        return False

    page.wait_for_timeout(900)          # the grid redraws after the click
    after = current_month(page, calendar)
    if after and after == before:
        log.info(f"Calendar did not move past {before}.")
        return False

    log.info(f"Calendar advanced: {before or '?'} -> {after or '?'}")
    return True


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


def pick_time(page, slots: Dict[str, Any], index: int = 0,
              timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS) -> str:
    """Select one time slot by row index. Returns the time chosen.

    Clicks the LABEL, never the radio: the <input> is tabindex="-1" and visually
    replaced by its label, so a click on the input is intercepted.
    """
    from src.waitlist.errors import WaitlistStepError

    container = slots.get("container", "table.ba-slot-table")
    select = slots.get("select", "label.ba-slot-radio-label")

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

    label = labels.nth(index)
    label.scroll_into_view_if_needed(timeout=5000)
    label.click(timeout=timeout_ms)
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
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(page.content())
        log.info(f"  captured DOM -> {path}")
        return path
    except Exception as e:
        log.warning(f"  could not capture DOM for '{step_name}': {e}")
        return ""


def walk_flow(page, route: str, to_step: Optional[str] = None,
              dry_run: bool = True, entry: str = "waitlist",
              values: Optional[Dict[str, Any]] = None) -> WalkResult:
    """Walk the configured booking steps from wherever the page currently is.

    `dry_run` (default) refuses to submit the committing step — the run stops in
    front of it and says so. That is deliberate: today "commits" sits on the
    slot pick only because the payment step does not exist yet, so honouring it
    keeps the walk from clicking past a boundary whose real position is unknown.

    `to_step` stops after a named step, for capturing one page at a time.
    """
    from src.booking import config as booking_config
    from src.waitlist.register import _await_page, _click

    result = WalkResult()
    steps = booking_config.steps_for(route, entry)

    for step in steps:
        name = step.get("name", "?")
        report = StepReport(name=name)

        if step.get("type") in ("dashboard_resume", "identity_assert"):
            # Handled by the probe before the walk starts.
            continue

        try:
            timeout_ms = int(step.get("timeout_ms") or DEFAULT_STEP_TIMEOUT_MS)
            _await_page(page, step, timeout_ms)
            report.url = page.url
            report.page_text = _page_text(page)
            # Captured BEFORE anything is clicked: this is the page as it
            # arrives, which is the state a config has to describe.
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

            if step.get("commits") and dry_run:
                report.ok = True
                report.detail = (
                    "reached the committing step — NOT submitting (dry run). "
                    "Re-run with --commit once the payment step exists and this "
                    "flag has moved to it.")
                result.steps.append(report)
                result.stopped_at = name
                result.reason = "dry run stopped at the commit boundary"
                return result

            submit = step.get("submit")
            if submit:
                _await_enabled_safe(page, submit)
                _click(page, submit, f"step '{name}' submit", timeout_ms)
                page.wait_for_timeout(1500)

            report.ok = True
            report.detail = f"completed; now at {page.url}"

        except Exception as e:
            report.ok = False
            report.detail = str(e)
            report.url = page.url
            report.page_text = _page_text(page)
            # The page that BROKE the walk is the one worth keeping most: it is
            # either a page no config describes, or a selector that has drifted.
            if not report.html_path:
                report.html_path = _capture_html(page, f"{name}_FAILED", route)
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


def _dwell(page, step: Dict[str, Any], key: str, why: str) -> None:
    """Wait the number of seconds a step configures, if any.

    Delegates to registration's implementation rather than repeating it: both
    halves are waiting on the same portal for the same reason, and two copies
    of a timing rule is how they drift apart.
    """
    from src.waitlist.register import _dwell as waitlist_dwell

    waitlist_dwell(page, step, key, why)


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


def _do_slot_pick(page, step: Dict[str, Any], report: StepReport,
                  timeout_ms: int) -> None:
    """Choose a date and a time, recording what was on offer."""
    from src.waitlist.errors import WaitlistStepError

    calendar = step.get("calendar") or {}
    slots = step.get("time_slots") or {}

    # Search forward month by month. VFS opens the calendar on the current
    # month, which is routinely full — the offered dates sit weeks out — so a
    # walk that only ever read the first month reported "no slots" while the
    # portal was showing plenty one click away.
    max_months = int(step.get("max_months_ahead") or DEFAULT_MAX_MONTHS_AHEAD)
    months_seen: List[str] = []
    dates: List[str] = []

    for attempt in range(max_months):
        month = current_month(page, calendar)
        dates = available_dates(page, calendar)
        months_seen.append(f"{month or '?'}({len(dates)})")
        log.info(f"{month or 'calendar'}: {len(dates)} available date(s)"
                 + (f" — {', '.join(dates)}" if dates else ""))
        if dates:
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
        raise WaitlistStepError(
            "No available dates in any of the months searched "
            f"({', '.join(months_seen)}). Another applicant may have taken "
            "them, or they may be further ahead than max_months_ahead.")

    # WHICH date, from the step's strategy. The config carried "strategy" from
    # the day it was written and nothing read it — the walk always took
    # dates[0] — so a route asking for anything else was silently ignored.
    strategy = str(step.get("strategy")
                   or (slots or {}).get("strategy")
                   or "earliest").strip().lower()
    if strategy not in SLOT_STRATEGIES:
        raise WaitlistStepError(
            f"Unknown slot strategy {strategy!r}. One of: "
            f"{', '.join(sorted(SLOT_STRATEGIES))}.")

    # available_dates() returns them sorted, so the ends of the list are the
    # earliest and latest offered.
    chosen_date = dates[0] if strategy == "earliest" else dates[-1]
    report.found["strategy"] = strategy
    log.info(f"Strategy '{strategy}' of {len(dates)} date(s) -> {chosen_date}")

    _settle(page, "before picking a date")
    pick_date(page, calendar, chosen_date, timeout_ms)
    report.found["chosen_date"] = chosen_date
    log.info(f"Picked date {chosen_date}")
    page.wait_for_timeout(1500)   # the slot table renders after the date click

    times = available_times(page, slots)
    report.found["available_times"] = times
    log.info(f"Date offers {len(times)} time(s): {', '.join(times) or 'none'}")

    # The same strategy decides the TIME. Times come back in the order the page
    # lists them, which is chronological, so index 0 and -1 are first and last.
    index = 0 if strategy == "earliest" else max(0, len(times) - 1)
    chosen_time = pick_time(page, slots, index=index, timeout_ms=timeout_ms)
    report.found["chosen_time"] = chosen_time
    log.info(f"Picked time {chosen_time} (index {index})")


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
