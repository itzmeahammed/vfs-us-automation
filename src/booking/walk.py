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

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

DEFAULT_STEP_TIMEOUT_MS = 45000

#: How long to wait for a submit button to stop being disabled. VFS gates
#: Continue behind "a date AND a time are chosen", and the button is rendered
#: disabled until both are.
ENABLE_TIMEOUT_MS = 20000


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


def walk_flow(page, route: str, to_step: Optional[str] = None,
              dry_run: bool = True) -> WalkResult:
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
    steps = booking_config.steps_for(route)

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

            if step.get("type") == "slot_pick":
                _do_slot_pick(page, step, report, timeout_ms)
            elif step.get("scroll_to_bottom"):
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(500)

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


def _do_slot_pick(page, step: Dict[str, Any], report: StepReport,
                  timeout_ms: int) -> None:
    """Choose a date and a time, recording what was on offer."""
    from src.waitlist.errors import WaitlistStepError

    calendar = step.get("calendar") or {}
    slots = step.get("time_slots") or {}

    dates = available_dates(page, calendar)
    report.found["available_dates"] = dates
    log.info(f"Calendar offers {len(dates)} date(s): {', '.join(dates) or 'none'}")

    if not dates:
        raise WaitlistStepError(
            "The calendar shows no available dates this month. Another applicant "
            "may have taken them, or they may be in a later month (the walk does "
            "not page forward yet).")

    chosen_date = dates[0]        # 'earliest' — the slot is not held, so speed wins
    pick_date(page, calendar, chosen_date, timeout_ms)
    report.found["chosen_date"] = chosen_date
    log.info(f"Picked date {chosen_date}")
    page.wait_for_timeout(1500)   # the slot table renders after the date click

    times = available_times(page, slots)
    report.found["available_times"] = times
    log.info(f"Date offers {len(times)} time(s): {', '.join(times) or 'none'}")

    chosen_time = pick_time(page, slots, index=0, timeout_ms=timeout_ms)
    report.found["chosen_time"] = chosen_time
    log.info(f"Picked time {chosen_time}")


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
