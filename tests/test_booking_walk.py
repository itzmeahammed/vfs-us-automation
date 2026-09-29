"""Reading the Switzerland booking calendar and slot table.

Asserted against the REAL captured DOM (switzerland_booking/switzerland.html,
2026-09-03) rather than invented markup, so a selector that would fail on the
live page fails here first.

The page is FullCalendar plus a plain table — nothing Angular Material — so none
of the mat-calendar selectors elsewhere in this codebase apply.
"""

import os
import re

import pytest

from src.booking.walk import available_dates, available_times, pick_time

HTML_PATH = os.path.join("switzerland_booking", "switzerland.html")

# What the real page (and the screenshot) actually show.
REAL_DATES = ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-11"]
REAL_TIMES = ["11:15", "11:30", "11:45"]


@pytest.fixture(scope="module")
def html():
    if not os.path.isfile(HTML_PATH):
        pytest.skip(f"{HTML_PATH} not present")
    with open(HTML_PATH, encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------- #
# The captured DOM says what we think it says                                  #
# --------------------------------------------------------------------------- #

def test_the_available_day_class_is_misspelled_in_vfs_markup(html):
    """*** date-availiable — THREE i's. *** VFS's own typo. Matching the correct
    spelling finds NOTHING, and the run reports no slots at all while the page
    is showing four. The single most breakable detail on this page."""
    assert "date-availiable" in html
    assert "date-available" not in html


def test_the_real_available_dates_are_present(html):
    """Four available days, matching the screenshot: 7, 8, 9 and 11 September."""
    found = re.findall(r'data-date="([0-9-]+)"[^>]*date-availiable', html)
    assert found == REAL_DATES


def test_unavailable_days_carry_a_date_but_not_the_class(html):
    """10 September sits between two available days and is NOT bookable — proof
    the class, not the presence of data-date, is what marks availability."""
    assert 'data-date="2026-09-10"' in html
    assert not re.search(r'data-date="2026-09-10"[^>]*date-availiable', html)


def test_the_real_slot_times_are_present(html):
    found = re.findall(r'id="tv\d+">([^<]+)</td>', html)
    assert found == REAL_TIMES


def test_the_continue_button_starts_disabled(html):
    """Rendered disabled until a date AND a time are chosen. Clicking it early
    silently does nothing, so the flow must wait for it to enable rather than
    clicking blind."""
    assert 'disabled="true"' in html
    assert "Continue" in html


def test_book_now_is_a_span_not_a_link(html):
    """<span class="c-brand-orange ... cursor-pointer"> Book Now </span>. A plain
    click can be ignored by a span, which is why the click ladder
    (normal -> force -> JS) matters here."""
    assert re.search(r'<span class="c-brand-orange[^"]*cursor-pointer">\s*Book Now', html)


# --------------------------------------------------------------------------- #
# Reading the page                                                             #
# --------------------------------------------------------------------------- #

class FakeLocator:
    """Minimal Playwright locator double: a list of (attrs, text) pairs.

    `elements` overrides that construction with ready-made FakeElements, which
    is how a label's child "Select" div gets a click the test can inspect.
    """

    def __init__(self, items, elements=None):
        self._items = items
        self._elements = elements

    def count(self):
        return len(self._elements if self._elements is not None else self._items)

    def nth(self, index):
        if self._elements is not None:
            return self._elements[index]
        return FakeElement(*self._items[index])

    @property
    def first(self):
        return self.nth(0)


class FakeElement:
    """A slot label. Holds a child locator so the inner-"Select" click can be
    modelled: the production code clicks label.locator(inner), not the label."""

    def __init__(self, attrs, text="", children=None, inner_fails=False,
                 force_fails=False):
        self._attrs = attrs
        self._text = text
        self._children = children
        self.inner_fails = inner_fails
        self.force_fails = force_fails
        self.clicked = False
        self.clicked_forced = False
        self.evaluated = None
        self.requested = []

    def get_attribute(self, name):
        return self._attrs.get(name)

    def inner_text(self, timeout=None):
        return self._text

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None, force=False):
        if force:
            if self.force_fails:
                raise RuntimeError("intercepts pointer events")
            self.clicked_forced = True
        else:
            self.clicked = True

    def evaluate(self, script):
        self.evaluated = script

    def locator(self, selector):
        self.requested.append(selector)
        if self._children is None:
            # Default: one inner div, present and clickable.
            child = FakeElement({}, "Select")
            child.force_fails = False
            if self.inner_fails:
                child.click = _raises("intercepts pointer events")
            return FakeLocator([(None, None)], elements=[child])
        return FakeLocator([], elements=list(self._children))


def _raises(message):
    def _click(timeout=None, force=False):
        raise RuntimeError(message)
    return _click


class FakePage:
    def __init__(self, by_selector):
        self._by_selector = by_selector
        self._elements = {}
        self.requested = []

    def locator(self, selector):
        self.requested.append(selector)
        if selector in self._elements:
            return FakeLocator([], elements=self._elements[selector])
        return FakeLocator(self._by_selector.get(selector, []))


def test_available_dates_reads_the_data_date_attribute():
    """The attribute, not the day number: a number repeats across months and is
    blank on the greyed leading/trailing cells."""
    page = FakePage({
        "td.fc-daygrid-day.date-availiable": [({"data-date": d}, "") for d in REAL_DATES]
    })
    assert available_dates(page, {}) == REAL_DATES


def test_available_dates_are_sorted():
    """The DOM order is grid order; a caller taking [0] as 'earliest' needs them
    sorted, not laid out."""
    page = FakePage({
        "td.fc-daygrid-day.date-availiable": [
            ({"data-date": "2026-09-11"}, ""), ({"data-date": "2026-09-07"}, "")
        ]
    })
    assert available_dates(page, {}) == ["2026-09-07", "2026-09-11"]


def test_a_cell_without_a_date_is_skipped():
    """Leading/trailing greyed cells carry the class in some renders but no date."""
    page = FakePage({
        "td.fc-daygrid-day.date-availiable": [({}, ""), ({"data-date": "2026-09-07"}, "")]
    })
    assert available_dates(page, {}) == ["2026-09-07"]


def test_an_empty_calendar_yields_no_dates():
    assert available_dates(FakePage({}), {}) == []


def test_a_custom_selector_from_config_is_honoured():
    page = FakePage({"td.custom": [({"data-date": "2026-10-01"}, "")]})
    assert available_dates(page, {"available_day": "td.custom"}) == ["2026-10-01"]


def test_available_times_reads_the_slot_table():
    page = FakePage({
        "table.ba-slot-table td[id^='tv']": [({}, t) for t in REAL_TIMES]
    })
    assert available_times(page, {}) == REAL_TIMES


def test_times_are_empty_before_a_date_is_chosen():
    """The table only populates after the date click, so this correctly returns
    nothing rather than raising."""
    assert available_times(FakePage({}), {}) == []


# --------------------------------------------------------------------------- #
# Choosing a slot                                                              #
# --------------------------------------------------------------------------- #

def _slot_page(count=3, **label_kwargs):
    """A slot table whose labels are inspectable FakeElements."""
    labels = [FakeElement({}, "", **label_kwargs) for _ in range(count)]
    page = FakePage({
        "table.ba-slot-table td[id^='tv']": [({}, t) for t in REAL_TIMES],
    })
    page._by_selector["table.ba-slot-table label.ba-slot-radio-label"] = []
    page._elements["table.ba-slot-table label.ba-slot-radio-label"] = labels
    return page, labels


def test_pick_time_clicks_the_labels_inner_select_not_the_label():
    """THE REGRESSION TEST for 2026-09-28.

    The <input type=radio> is a SIBLING of the label that paints over the
    label's centre point, so Playwright's hit test finds the input there and
    refuses to click — for 45 seconds and ~70 retries on the live run:

        <input ... id="STRadio5" ...> intercepts pointer events

    The inner "Select" div is a DESCENDANT of the label, so it paints above the
    input. Clicking it still activates the label, and therefore the radio.
    """
    page, labels = _slot_page()
    assert pick_time(page, {}, index=0) == "11:15"

    # The label itself was NEVER clicked — neither normally nor forced.
    assert labels[0].clicked is False
    assert labels[0].clicked_forced is False
    # Its inner div was addressed instead.
    assert labels[0].requested == ["div.ba-slot-radio-label-text1"]


def test_pick_time_can_choose_a_later_slot():
    page, labels = _slot_page()
    assert pick_time(page, {}, index=2) == "11:45"
    assert labels[2].requested and not labels[0].requested


def test_a_route_can_name_its_own_inner_target():
    page, labels = _slot_page()
    pick_time(page, {"select_inner": "span.pick"}, index=0)
    assert labels[0].requested == ["span.pick"]


def test_the_label_is_force_clicked_when_the_inner_click_is_intercepted():
    """Fallback 2. force=True skips the actionability checks, the intercept one
    included — safe here only because the element was already confirmed visible
    by the attempt that just failed."""
    page, labels = _slot_page(inner_fails=True)
    assert pick_time(page, {}, index=0) == "11:15"
    assert labels[0].clicked_forced is True
    assert labels[0].evaluated is None      # did not need the last resort


def test_the_radio_is_driven_directly_when_even_a_forced_click_fails():
    """Fallback 3, last because it bypasses the LABEL: an Angular handler bound
    to the label would not fire. Listed last for that reason, not because it is
    less likely to work."""
    page, labels = _slot_page(inner_fails=True, force_fails=True)
    assert pick_time(page, {}, index=0) == "11:15"
    assert labels[0].evaluated is not None
    assert "getElementById" in labels[0].evaluated


def test_a_slot_that_cannot_be_clicked_at_all_says_so_plainly():
    """Distinct from 'no slots offered': the slot IS there and would not take
    the click. Reporting that as 'taken' would send the next diagnosis in
    entirely the wrong direction."""
    from src.waitlist.errors import WaitlistStepError

    page, labels = _slot_page(inner_fails=True, force_fails=True)
    labels[0].evaluate = _raises("detached")
    with pytest.raises(WaitlistStepError, match="intercepted"):
        pick_time(page, {}, index=0)


def test_no_slots_raises_a_clear_and_NON_ALARMING_error():
    """A slot taken between the calendar rendering and the click is normal, not
    a fault — the message must say so or it reads as a bug."""
    from src.waitlist.errors import WaitlistStepError

    with pytest.raises(WaitlistStepError, match="normal, not a fault"):
        pick_time(FakePage({}), {}, index=0)


def test_asking_for_a_slot_beyond_the_list_raises():
    page = FakePage({"table.ba-slot-table label.ba-slot-radio-label": [({}, "")]})
    from src.waitlist.errors import WaitlistStepError

    with pytest.raises(WaitlistStepError, match="only 1 exist"):
        pick_time(page, {}, index=5)


# --------------------------------------------------------------------------- #
# The config matches the DOM                                                   #
# --------------------------------------------------------------------------- #

def test_the_che_config_uses_the_misspelled_class():
    """Guards the config file itself: a well-meaning 'fix' to date-available
    would silently match nothing on the live page."""
    from src.booking import config as booking_config

    booking_config.clear_cache()
    step = next(s for s in booking_config.steps_for("AE-CHE")
                if s.get("type") == "slot_pick")
    assert "date-availiable" in step["calendar"]["available_day"]


def test_the_che_flow_drops_appointment_details_and_confirmation():
    """Switzerland goes straight from 'Book Now' to the calendar, and nothing
    past /services has been captured — so a guessed confirmation step would read
    some other page as success."""
    from src.booking import config as booking_config

    booking_config.clear_cache()
    names = [s["name"] for s in booking_config.steps_for("AE-CHE")]
    assert names == ["dashboard_resume", "verify_identity", "select_slot", "services"]


def test_the_services_step_scrolls_before_continuing():
    """The Continue button sits below the fold; without scrolling the click
    misses."""
    from src.booking import config as booking_config

    booking_config.clear_cache()
    step = next(s for s in booking_config.steps_for("AE-CHE") if s["name"] == "services")
    assert step["scroll_to_bottom"] is True


# --------------------------------------------------------------------------- #
# Slot strategy                                                                #
# --------------------------------------------------------------------------- #
#
# "strategy" sat in the configs from the day they were written and nothing read
# it: _do_slot_pick always took dates[0]. A route asking for anything else was
# silently ignored, which is the worst kind of config key — present, documented,
# and inert.


class _StrategyPage:
    """Records which date and time index were chosen."""

    def __init__(self, dates, times):
        self._dates = dates
        self._times = times
        self.picked_date = None
        self.picked_index = None

    def wait_for_timeout(self, _ms):
        pass


def _run_strategy(monkeypatch, strategy, dates, times, values=None):
    from src.booking import walk as walk_mod

    page = _StrategyPage(dates, times)
    report = walk_mod.StepReport(name="select_slot")

    monkeypatch.setattr(walk_mod, "available_dates", lambda p, c: list(dates))
    monkeypatch.setattr(walk_mod, "available_times", lambda p, s: list(times))
    monkeypatch.setattr(walk_mod, "current_month", lambda p, c: "October 2026")
    monkeypatch.setattr(walk_mod, "_settle", lambda p, why="": None)

    def fake_pick_date(p, calendar, date, timeout_ms):
        p.picked_date = date

    def fake_pick_time(p, slots, index=0, timeout_ms=0):
        p.picked_index = index
        return times[index]

    monkeypatch.setattr(walk_mod, "pick_date", fake_pick_date)
    monkeypatch.setattr(walk_mod, "pick_time", fake_pick_time)

    # THE STRATEGY IS THE CLIENT'S, NOT THE STEP'S. A route-level "strategy"
    # is one setting for every client that country books, and is now ignored:
    # a booking takes its window (or an explicit slot_strategy) from the
    # client record. These tests therefore supply it the way a sales agent
    # does. `values` passed by a caller wins, so window tests are unaffected.
    merged = dict(values or {})
    merged.setdefault("slot_strategy", strategy)

    step = {"name": "select_slot", "calendar": {}, "time_slots": {}}
    walk_mod._do_slot_pick(page, step, report, 1000, values=merged)
    return page, report


def test_earliest_takes_the_first_date_and_time(monkeypatch):
    dates = ["2026-10-05", "2026-10-20", "2026-10-28"]
    page, report = _run_strategy(monkeypatch, "earliest", dates,
                                 ["09:00", "11:45", "14:30"])

    assert page.picked_date == "2026-10-05"
    assert page.picked_index == 0
    assert report.found["chosen_time"] == "09:00"


def test_latest_takes_the_last_date_and_time(monkeypatch):
    """The far end of the calendar, for both the date AND the time.

    Choosing the latest date but the earliest time would be a plausible
    half-implementation and quietly wrong.
    """
    dates = ["2026-10-05", "2026-10-20", "2026-10-28"]
    page, report = _run_strategy(monkeypatch, "latest", dates,
                                 ["09:00", "11:45", "14:30"])

    assert page.picked_date == "2026-10-28"
    assert page.picked_index == 2
    assert report.found["chosen_time"] == "14:30"


def test_a_single_offer_works_under_either_strategy(monkeypatch):
    """One date and one time is the common real case — Norway had exactly that.

    An off-by-one in the 'latest' index would pass the multi-slot test above
    and fail here, which is the run that actually matters.
    """
    for strategy in ("earliest", "latest"):
        page, _ = _run_strategy(monkeypatch, strategy,
                                ["2026-10-28"], ["11:45"])
        assert page.picked_date == "2026-10-28"
        assert page.picked_index == 0


def test_an_unknown_strategy_is_refused(monkeypatch):
    from src.waitlist.errors import WaitlistStepError

    # The message names the CLIENT RECORD, because that is where a bad value
    # now comes from — the route-level "strategy" is no longer consulted.
    with pytest.raises(WaitlistStepError, match="not a strategy"):
        _run_strategy(monkeypatch, "cheapest", ["2026-10-28"], ["11:45"])


def test_the_shipped_norway_config_uses_a_safe_slot_strategy():
    """Norway books for real, so it must book the dates an AGENT asked for.

    This replaces a pin on "latest", which was an admitted testing choice —
    take the far end of the calendar so a trial run would not take a slot a
    real applicant wanted today. That tripwire fired correctly when the route
    was armed; this is its production successor.

    The distinction being pinned is not cosmetic. earliest/latest always
    succeed when the calendar has anything at all; in_range refuses to
    substitute a date nobody agreed to.
    """
    from src.utils.config_reader import initialize_config
    from src.booking import config as booking_config

    initialize_config()
    booking_config.clear_cache()
    step = next(s for s in booking_config.steps_for("AE-NOR", "new")
                if s.get("type") == "slot_pick")
    # EITHER of two, and only these two:
    #
    #   in_range   the production strategy. Books the earliest date inside the
    #              window a sales agent asked for, and books NOTHING if that
    #              window is not offered.
    #   earliest   commissioning only. Takes whatever VFS offers soonest, so
    #              the booking path can be exercised without the date window
    #              gating it.
    #
    # "latest" is excluded deliberately: it was the trial-run choice that took
    # the least-contended end of the calendar, and on an ARMED route it would
    # book a real client onto the furthest available date for no reason.
    assert step.get("strategy") in ("in_range", "earliest"), step.get("strategy")

    # And NOT a second, disagreeing strategy at the time_slots level.
    assert "strategy" not in (step.get("time_slots") or {})


def test_the_walk_honours_the_portals_stated_dwell(monkeypatch):
    """Norway prints "wait 4 seconds before saving"; Switzerland asks for 32.

    The walk ignored dwell_seconds entirely, so a config setting it was
    silently disobeyed and Save would be rejected for submitting too early.
    """
    from src.booking import walk as walk_mod

    waited = []
    monkeypatch.setattr(walk_mod, "_dwell",
                        lambda page, step, key, why: waited.append(
                            (key, step.get(key))))
    monkeypatch.setattr(walk_mod, "_capture_html", lambda *a, **k: "")
    # _await_page and _click are imported INSIDE walk_flow, so they must be
    # patched where they are defined rather than on this module.
    monkeypatch.setattr("src.waitlist.register._await_page",
                        lambda *a, **k: None)
    monkeypatch.setattr(walk_mod, "_await_enabled_safe", lambda *a, **k: None)
    monkeypatch.setattr(walk_mod, "_fill_form", lambda *a, **k: None)
    monkeypatch.setattr("src.waitlist.register._click", lambda *a, **k: None)

    step = {"name": "your_details", "type": "form", "dwell_seconds": 10,
            "fields": [{"name": "x", "label": "X", "widget": "text"}],
            "submit": {"role": "button", "name": "Save"}}
    monkeypatch.setattr("src.booking.config.steps_for",
                        lambda route, entry="waitlist": [step])

    page = type("P", (), {"url": "u", "wait_for_timeout": lambda s, ms: None,
                          "mouse": None})()
    monkeypatch.setattr(walk_mod, "_page_text", lambda p, limit=400: "")

    walk_mod.walk_flow(page, "AE-NOR", entry="new", values={})

    assert ("dwell_seconds", 10) in waited, (
        f"the portal's stated wait was not honoured; saw {waited}")


# --------------------------------------------------------------------------- #
# The month search stops at the first month that offers anything               #
# --------------------------------------------------------------------------- #
#
# Under BOTH strategies, and the cost is the reason. On the 2026-09-26 run
# November offered 17 dates and the walk paged on to December, January and
# February anyway — ~37s each, all three empty, because VFS publishes a rolling
# window so the months past the first available one are empty by construction.
# A slot is not held while the walk is on this page, so that was pure exposure.


def _run_multi_month(monkeypatch, strategy, months, max_months=4, values=None):
    """Drive _do_slot_pick over a calendar whose months hold different dates.

    `months` is a list of date-lists, one per month, read in order as the search
    pages forward. Records the month the calendar was left on so that clicking a
    date on a month that is no longer rendered shows up as a failure.
    """
    from src.booking import walk as walk_mod

    state = {"index": 0}

    class Page:
        picked_date = None
        picked_index = None

        def wait_for_timeout(self, _ms):
            pass

    page = Page()
    report = walk_mod.StepReport(name="select_slot")
    times = ["09:00", "11:45", "15:30"]

    monkeypatch.setattr(walk_mod, "available_dates",
                        lambda p, c: list(months[state["index"]]))
    monkeypatch.setattr(walk_mod, "available_times", lambda p, s: list(times))
    monkeypatch.setattr(walk_mod, "current_month",
                        lambda p, c: f"month {state['index']}")
    monkeypatch.setattr(walk_mod, "_settle", lambda p, why="": None)

    def advance(p, calendar, timeout_ms=0):
        if state["index"] + 1 >= len(months):
            return False
        state["index"] += 1
        return True

    def retreat(p, calendar, timeout_ms=0):
        if state["index"] == 0:
            return False
        state["index"] -= 1
        return True

    monkeypatch.setattr(walk_mod, "advance_month", advance)
    monkeypatch.setattr(walk_mod, "retreat_month", retreat)

    def fake_pick_date(p, calendar, date, timeout_ms):
        # The real pick_date can only click a cell the calendar is RENDERING.
        if date not in months[state["index"]]:
            raise AssertionError(
                f"tried to click {date} while showing month {state['index']} "
                f"({months[state['index']]}) — the cell is not in the DOM")
        p.picked_date = date

    monkeypatch.setattr(walk_mod, "pick_date", fake_pick_date)
    monkeypatch.setattr(walk_mod, "pick_time",
                        lambda p, slots, index=0, timeout_ms=0: (
                            setattr(p, "picked_index", index), times[index])[1])

    # Same as _run_strategy: the strategy belongs to the CLIENT now, so it is
    # supplied the way a sales agent supplies it. A caller's own `values` wins,
    # which is what the in_range window tests rely on.
    merged = dict(values or {})
    merged.setdefault("slot_strategy", strategy)

    step = {"name": "select_slot", "calendar": {},
            "time_slots": {}, "max_months_ahead": max_months}
    walk_mod._do_slot_pick(page, step, report, 1000, values=merged)
    return page, report


def test_the_search_stops_on_the_first_month_with_dates(monkeypatch):
    """November has 17 dates, so December onwards must never be read. Asserted
    via months_searched, because the cost of paging is the whole point."""
    page, report = _run_multi_month(
        monkeypatch, "latest",
        [["2026-11-05", "2026-11-30"], ["2026-12-14"], ["2027-01-20"]])

    assert report.found["months_searched"] == ["month 0(2)"]
    assert report.found["available_dates"] == ["2026-11-05", "2026-11-30"]


def test_latest_takes_the_last_date_of_that_month(monkeypatch):
    """'latest' means the latest date the portal is OFFERING, not the latest
    reachable by paging forward into empty months."""
    page, _ = _run_multi_month(
        monkeypatch, "latest",
        [["2026-11-05", "2026-11-30"], ["2026-12-14"]])

    assert page.picked_date == "2026-11-30"


def test_a_full_current_month_still_pages_forward(monkeypatch):
    """The case the loop exists for: this month is full, which is routine. It
    must still page on to find the month that is not."""
    page, report = _run_multi_month(
        monkeypatch, "earliest", [[], [], ["2027-01-20"]])

    assert page.picked_date == "2027-01-20"
    assert report.found["months_searched"] == ["month 0(0)", "month 1(0)",
                                               "month 2(1)"]


def test_the_chosen_date_is_on_the_month_left_displayed(monkeypatch):
    """pick_date addresses the cell by data-date, which only matches a month the
    calendar is rendering. The fake pick_date refuses any other date, so this
    fails if the search ever leaves the calendar somewhere else."""
    for strategy in ("earliest", "latest"):
        page, _ = _run_multi_month(
            monkeypatch, strategy, [[], ["2026-11-19", "2026-11-30"], []])
        assert page.picked_date in ("2026-11-19", "2026-11-30")


def test_latest_takes_the_last_time_on_the_chosen_date(monkeypatch):
    """Choosing the latest date but the earliest time would be a plausible
    half-fix, so the time index is asserted separately."""
    page, _ = _run_multi_month(
        monkeypatch, "latest", [["2026-11-19", "2026-11-30"]])

    assert page.picked_index == 2


# --------------------------------------------------------------------------- #
# The appointment-details dropdowns are addressed by the RIGHT control          #
# --------------------------------------------------------------------------- #


def test_norway_dropdowns_match_the_cascade_the_slot_checker_drives():
    """VFS named these controls the wrong way round in their own markup:
    'Choose your appointment category' is selectedSubvisaCategory and
    'Choose your sub-category' is visaCategoryCode.

    AE-NOR had them SWAPPED, so the walk picked the centre and then opened
    visaCategoryCode — the GRANDCHILD dropdown, whose options VFS does not fetch
    until its parent is chosen. The panel was therefore empty every time, three
    attempts deep, and the walk never got past page 1 of 7.

    Pinned against slot_check._CASCADE_LEVELS rather than against a literal,
    because the hourly slot checker drives these same three dropdowns on this
    same page and is the standing proof of the order: if the two ever disagree
    again, one of them is wrong.
    """
    from src.utils.config_reader import initialize_config
    from src.booking import config as booking_config
    from src.vfs_bot.slot_check import _CASCADE_LEVELS

    initialize_config()
    booking_config.clear_cache()
    step = next(s for s in booking_config.steps_for("AE-NOR", "new")
                if s.get("name") == "appointment_details")

    by_name = {f["name"]: f for f in step["fields"]}
    for control, key in _CASCADE_LEVELS:
        assert by_name[key]["control"] == control, (
            f"{key} is addressed by {by_name[key]['control']!r} but the slot "
            f"checker drives it as {control!r}")

    # And in the DOM's own order, which is the order the walk fills them in:
    # a dependent dropdown opened before its parent is chosen stays empty.
    assert [f["name"] for f in step["fields"]] == [k for _, k in _CASCADE_LEVELS]


def test_norway_dropdown_controls_match_the_captured_dom():
    """The captured page is the ground truth, so assert against it directly:
    the three mat-selects appear in DOM order centerCode, selectedSubvisaCategory,
    visaCategoryCode, each under the label the config claims for it."""
    import os
    import re

    capture = os.path.join(
        "captured", "AE-NOR", "20260926_162933_appointment_details.html")
    if not os.path.exists(capture):
        pytest.skip("the captured appointment-details page is not in this tree")

    from src.utils.config_reader import initialize_config
    from src.booking import config as booking_config

    initialize_config()
    booking_config.clear_cache()
    step = next(s for s in booking_config.steps_for("AE-NOR", "new")
                if s.get("name") == "appointment_details")

    with open(capture, encoding="utf-8", errors="replace") as handle:
        html = handle.read()

    order = re.findall(r'<mat-select[^>]*formcontrolname="([^"]+)"', html)
    assert order == ["centerCode", "selectedSubvisaCategory", "visaCategoryCode"]
    assert [f["control"] for f in step["fields"]] == order

    # Each control sits under the label the config uses to describe it.
    for field in step["fields"]:
        at = html.index(f'formcontrolname="{field["control"]}"')
        preceding = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[at - 1200:at]))
        assert field["label"] in preceding, (
            f"{field['control']} is not under the label {field['label']!r}")


# --------------------------------------------------------------------------- #
# The portal's own countdown is read, not guessed                              #
# --------------------------------------------------------------------------- #
#
# Norway's "Your Details" renders 'Please wait N seconds before saving' in
# div#mintime, as a live countdown. N was 4 in one capture and 30 half an hour
# later, so it varies by SESSION and no configured number can be right. The
# config said 10; on the 30s session Save was clicked with 12 seconds left, was
# silently rejected, and the walk spent its next step looking for a Continue
# button on a page it had never left.


class _CountdownPage:
    """A page whose div#mintime counts down one second per read."""

    def __init__(self, seconds, template="Warning: Please wait {} seconds "
                                         "before saving your details"):
        self.remaining = seconds
        self.template = template
        self.reads = 0
        self.waited_ms = 0

    def wait_for_timeout(self, ms):
        self.waited_ms += ms

    def locator(self, selector):
        assert selector == "div#mintime", selector
        page = self

        class Block:
            first = None

            def count(self):
                return 1 if page.remaining is not None else 0

            def inner_text(self, timeout=0):
                page.reads += 1
                if page.remaining is None:
                    return ""
                if page.remaining <= 0:
                    return "Warning: you may now save your details"
                text = page.template.format(page.remaining)
                page.remaining -= 1
                return text

        block = Block()
        block.first = block
        return block


def test_the_stated_wait_is_read_off_the_page():
    from src.booking import walk as walk_mod

    assert walk_mod._stated_wait(_CountdownPage(30)) == 30
    assert walk_mod._stated_wait(_CountdownPage(4)) == 4


def test_no_countdown_on_the_page_means_no_wait():
    from src.booking import walk as walk_mod

    assert walk_mod._stated_wait(_CountdownPage(None)) == 0


def test_a_countdown_is_capped():
    """A misparse must not be able to hang a run for an hour."""
    from src.booking import walk as walk_mod

    assert walk_mod._stated_wait(_CountdownPage(99999)) == \
        walk_mod.MAX_COUNTDOWN_SECONDS


def test_the_countdown_is_waited_out_until_it_clears():
    """Polls rather than sleeping the first number it sees, so it submits when
    the portal is actually ready instead of when we predicted it would be."""
    from src.booking import walk as walk_mod

    page = _CountdownPage(5)
    assert walk_mod._await_countdown(page, "test") is True
    assert page.remaining <= 0
    assert page.waited_ms >= 5000


def test_await_countdown_reports_false_when_the_portal_is_not_asking():
    from src.booking import walk as walk_mod

    assert walk_mod._await_countdown(_CountdownPage(None), "test") is False


def test_the_dwell_waits_out_the_page_even_when_the_config_is_short(monkeypatch):
    """The config value is a FLOOR, not the answer: the page wins when it asks
    for more. This is the actual 2026-09-26 failure, as a test."""
    from src.booking import walk as walk_mod

    page = _CountdownPage(30)
    monkeypatch.setattr("src.waitlist.register._dwell",
                        lambda p, step, key, why: None)

    walk_mod._dwell(page, {"dwell_seconds": 5}, "dwell_seconds", "test")
    assert page.remaining <= 0, "submitted while the portal was still counting"


def test_the_settle_dwell_does_not_read_the_countdown(monkeypatch):
    """settle_seconds runs BEFORE the fields are filled, when the countdown has
    not started and is not what is being waited for."""
    from src.booking import walk as walk_mod

    page = _CountdownPage(30)
    monkeypatch.setattr("src.waitlist.register._dwell",
                        lambda p, step, key, why: None)

    walk_mod._dwell(page, {"settle_seconds": 2}, "settle_seconds", "test")
    assert page.reads == 0


# --------------------------------------------------------------------------- #
# A step the portal may not show is skipped, not fought                        #
# --------------------------------------------------------------------------- #


class _PresencePage:
    def __init__(self, url="https://x/are/en/nor/application-detail",
                 visible_text=None, visible_css=None):
        self.url = url
        self._text = visible_text
        self._css = visible_css

    def locator(self, selector):
        page = self

        class L:
            first = None

            def is_visible(self, timeout=0):
                return selector == page._css

        el = L()
        el.first = el
        return el

    def get_by_text(self, text, exact=False):
        page = self

        class L:
            first = None

            def is_visible(self, timeout=0):
                return page._text is not None and text in page._text

        el = L()
        el.first = el
        return el


def test_a_step_is_present_when_its_text_is_on_screen():
    from src.booking import walk as walk_mod

    step = {"name": "details_summary", "if_present": True,
            "wait_for_text": "Add another applicant"}
    assert walk_mod._step_page_present(
        _PresencePage(visible_text="Add another applicant"), step) is True
    assert walk_mod._step_page_present(
        _PresencePage(visible_text="Your Details"), step) is False


def test_present_when_css_takes_priority():
    from src.booking import walk as walk_mod

    step = {"name": "s", "if_present": True, "present_when": "div#summary",
            "wait_for_text": "ignored"}
    assert walk_mod._step_page_present(
        _PresencePage(visible_css="div#summary"), step) is True


def test_presence_falls_back_to_the_url():
    from src.booking import walk as walk_mod

    step = {"name": "s", "if_present": True, "url_contains": "application-detail"}
    assert walk_mod._step_page_present(_PresencePage(), step) is True
    step = {"name": "s", "if_present": True, "url_contains": "review-pay"}
    assert walk_mod._step_page_present(_PresencePage(), step) is False


def test_if_present_without_a_marker_assumes_present():
    """The flag must never silently skip a step nobody said how to recognise."""
    from src.booking import walk as walk_mod

    assert walk_mod._step_page_present(
        _PresencePage(), {"name": "s", "if_present": True}) is True


def test_norway_waits_for_the_summary_page_and_continues():
    """CONFIRMED REAL from a screenshot: 'Your Details Summary' always sits
    between Your Details and Book Appointment, with Go Back / Continue.

    It is deliberately NOT "if_present". It was briefly marked optional because
    no capture contained it — but that absence was a symptom: the run never got
    a Save accepted, so it never reached this page. The gate is what makes the
    walk WAIT for the summary instead of racing ahead while Save is still in
    flight, which is exactly how the page came to look nonexistent."""
    from src.utils.config_reader import initialize_config
    from src.booking import config as booking_config

    initialize_config()
    booking_config.clear_cache()
    step = next(s for s in booking_config.steps_for("AE-NOR", "new")
                if s.get("name") == "details_summary")

    assert not step.get("if_present"), "the summary page is always shown"

    # A LIST means "any of these". Asserting the exact string pinned wording
    # that turned out not to be what VFS renders — the live page identified
    # itself as "Applicant 1" — so this asserts the PROPERTY that matters
    # (the gate names the summary, and does so distinctly) rather than one
    # phrasing that a portal revision can invalidate.
    gate = step["wait_for_text"]
    phrases = gate if isinstance(gate, list) else [gate]
    assert phrases, "the summary page needs a gate"
    assert "Add another applicant" in phrases, (
        "the originally confirmed marker must stay among the accepted ones")
    assert step["submit"]["name"] == "Continue"


def test_the_summary_gate_cannot_match_the_previous_page():
    """'Your Details Summary' CONTAINS 'Your Details', and the gate is a
    substring match — so a heading-based gate would report arrival while still
    sitting on the form. The chosen marker must not appear on Your Details."""
    from src.utils.config_reader import initialize_config
    from src.booking import config as booking_config

    initialize_config()
    booking_config.clear_cache()
    steps = booking_config.steps_for("AE-NOR", "new")
    gate = next(s for s in steps
                if s.get("name") == "details_summary")["wait_for_text"]
    phrases = gate if isinstance(gate, list) else [gate]

    # EVERY accepted phrase must be distinct to the summary. A list makes the
    # gate easier to satisfy, so each addition has to clear the same bar the
    # original did — otherwise widening it quietly reintroduces the bug where
    # the walk reports arrival while still on the form.
    for phrase in phrases:
        assert "Your Details" not in phrase, (
            f"{phrase!r} contains the PREVIOUS page's heading")

        capture = os.path.join(
            "captured", "AE-NOR", "20260926_165217_your_details.html")
        if os.path.exists(capture):
            with open(capture, encoding="utf-8", errors="replace") as handle:
                assert phrase not in handle.read(), (
                    f"{phrase!r} appears on Your Details, so the gate would "
                    "pass before Save has been accepted")


def test_norway_saves_rather_than_continues_on_your_details():
    """The page offers Cancel / Save / Update and no Continue at all."""
    from src.utils.config_reader import initialize_config
    from src.booking import config as booking_config

    initialize_config()
    booking_config.clear_cache()
    step = next(s for s in booking_config.steps_for("AE-NOR", "new")
                if s.get("name") == "your_details")

    assert step["submit"]["name"] == "Save"


# --------------------------------------------------------------------------- #
# The appointment type is chosen, not assumed                                  #
# --------------------------------------------------------------------------- #
#
# "appointment_type" sat in the configs from the day they were written and
# nothing read it — the same inert-key problem "strategy" had. Norway gets away
# with it because its single radio ships pre-checked, so the calendar responds
# anyway; a route offering Standard vs Premium would silently book whichever VFS
# defaulted to.


class _RadioPage:
    def __init__(self, count=1, checked=False):
        self._count = count
        self._checked = checked
        self.clicked = False

    def wait_for_timeout(self, _ms):
        pass

    def locator(self, selector):
        page = self

        class L:
            first = None

            def count(self):
                return page._count

            def get_attribute(self, name, timeout=0):
                assert name == "class"
                return ("mat-mdc-radio-button mat-mdc-radio-checked"
                        if page._checked else "mat-mdc-radio-button")

            def scroll_into_view_if_needed(self, timeout=0):
                pass

            def click(self, timeout=0):
                page.clicked = True

        el = L()
        el.first = el
        return el


def _pick_type(page, spec):
    from src.booking import walk as walk_mod

    report = walk_mod.StepReport(name="select_slot")
    walk_mod._pick_appointment_type(page, spec, report, 1000)
    return report


def test_an_unchecked_appointment_type_is_clicked():
    page = _RadioPage(count=1, checked=False)
    report = _pick_type(page, {"label": "Premium"})
    assert page.clicked is True
    assert report.found["appointment_type"] == "Premium"


def test_a_pre_checked_appointment_type_is_left_alone():
    """Norway's single radio arrives checked. Clicking it again is a needless
    interaction with a page where the slot is not held."""
    page = _RadioPage(count=1, checked=True)
    report = _pick_type(page, {"label": "Choose a slot"})
    assert page.clicked is False
    assert "pre-selected" in report.found["appointment_type"]


def test_no_appointment_type_declared_does_nothing():
    page = _RadioPage(count=0)
    report = _pick_type(page, None)
    assert page.clicked is False
    assert "appointment_type" not in report.found


def test_an_absent_optional_appointment_type_is_skipped():
    page = _RadioPage(count=0)
    report = _pick_type(page, {"label": "Choose a slot", "if_present": True})
    assert report.found["appointment_type"] == "not present"


def test_an_absent_required_appointment_type_is_an_error():
    from src.waitlist.errors import WaitlistStepError

    with pytest.raises(WaitlistStepError, match="No appointment type"):
        _pick_type(_RadioPage(count=0), {"label": "Premium"})


def test_norway_declares_its_appointment_type():
    from src.utils.config_reader import initialize_config
    from src.booking import config as booking_config

    initialize_config()
    booking_config.clear_cache()
    step = next(s for s in booking_config.steps_for("AE-NOR", "new")
                if s.get("type") == "slot_pick")
    assert step["appointment_type"]["label"] == "Choose a slot"
    assert step["appointment_type"]["if_present"] is True


# --------------------------------------------------------------------------- #
# The time table is waited for, not slept on                                   #
# --------------------------------------------------------------------------- #
#
# VFS fetches the day's slots after the date is clicked and renders the whole
# "Choose an appointment time" block only once they land — the table is not in
# the DOM at all before that. The walk slept a flat 1500ms and then read it, so
# on 2026-09-26 it clicked the date at 17:18:03, read at 17:18:04, and reported
# "Date offers 0 time(s)" — which pick_time renders as "the slot may have been
# taken", an alarming and wrong conclusion about a page that was still loading.


class _LateTablePage:
    """A page whose slot table appears only after N visibility checks."""

    def __init__(self, appears_after=2):
        self.appears_after = appears_after
        self.checks = 0
        self.waited_ms = 0

    def wait_for_timeout(self, ms):
        self.waited_ms += ms

    def locator(self, selector):
        page = self

        class L:
            first = None

            def wait_for(self, state=None, timeout=0):
                page.checks += 1
                if page.checks < page.appears_after:
                    raise AssertionError("not visible yet")

        el = L()
        el.first = el
        return el


def test_the_walk_waits_for_a_late_slot_table(monkeypatch):
    from src.booking import walk as walk_mod

    monkeypatch.setattr("src.vfs_bot.turnstile.wait_for_loader",
                        lambda page: None)
    page = _LateTablePage(appears_after=1)
    assert walk_mod._await_time_table(page, {}, 5000) is True


def test_a_table_that_never_appears_is_reported_not_raised(monkeypatch):
    """A day really can have no times left; pick_time says that better. What
    this must not do is raise and lose the distinction."""
    from src.booking import walk as walk_mod

    monkeypatch.setattr("src.vfs_bot.turnstile.wait_for_loader",
                        lambda page: None)
    page = _LateTablePage(appears_after=99)
    assert walk_mod._await_time_table(page, {}, 5000) is False


def test_the_slot_pick_waits_before_reading_times(monkeypatch):
    """The ordering is the fix: _await_time_table must run BEFORE the times are
    read, or the wait buys nothing."""
    from src.booking import walk as walk_mod

    order = []

    monkeypatch.setattr(walk_mod, "available_dates",
                        lambda p, c: ["2026-11-30"])
    monkeypatch.setattr(walk_mod, "current_month", lambda p, c: "November 2026")
    monkeypatch.setattr(walk_mod, "_settle", lambda p, why="": None)
    monkeypatch.setattr(walk_mod, "_reveal_date",
                        lambda p, c, d, t=0: True)
    monkeypatch.setattr(walk_mod, "_pick_appointment_type",
                        lambda p, s, r, t: None)
    monkeypatch.setattr(walk_mod, "pick_date",
                        lambda p, c, d, t: order.append("pick_date"))
    monkeypatch.setattr(walk_mod, "_await_time_table",
                        lambda p, s, t=0: order.append("await_table") or True)

    def times(p, s):
        order.append("read_times")
        return ["11:45"]

    monkeypatch.setattr(walk_mod, "available_times", times)
    monkeypatch.setattr(walk_mod, "pick_time",
                        lambda p, s, index=0, timeout_ms=0: "11:45")

    class Page:
        def wait_for_timeout(self, _ms):
            pass

    report = walk_mod.StepReport(name="select_slot")
    # Every booking needs a strategy now; this test is about ORDERING, so it
    # states the simplest one rather than a window.
    walk_mod._do_slot_pick(Page(), {"name": "select_slot", "calendar": {},
                                    "time_slots": {}}, report, 1000,
                           values={"slot_strategy": "earliest"})

    assert order == ["pick_date", "await_table", "read_times"]


# --------------------------------------------------------------------------- #
# The commit boundary reports whether the page is ARMED                        #
#                                                                              #
# The 2026-09-28 run stopped correctly at review_pay, but said only "dry run   #
# stopped at the commit boundary" — which does not distinguish the two states  #
# that matter to whoever takes the open browser:                               #
#                                                                              #
#   armed     every field took, Pay Online is live, ONE CLICK SPENDS MONEY     #
#   not armed something did not take, the button is dead, look at the capture  #
#                                                                              #
# On the one page where the next click is irreversible, that is the whole      #
# message.                                                                     #
# --------------------------------------------------------------------------- #

class FakeSubmit:
    def __init__(self, enabled=True, classes="", aria=None):
        self._enabled = enabled
        self._attrs = {"class": classes}
        if aria is not None:
            self._attrs["aria-disabled"] = aria

    def is_enabled(self, timeout=None):
        return self._enabled

    def get_attribute(self, name):
        return self._attrs.get(name)


def _submit_page(submit_element):
    page = FakePage({})
    page._elements["button#trigger"] = [submit_element]
    return page


def test_a_live_submit_reports_the_page_as_armed():
    from src.booking import walk as walk_mod

    page = _submit_page(FakeSubmit(enabled=True))
    assert walk_mod._submit_is_live(page, {"selector": "button#trigger"}) is True


def test_a_disabled_submit_reports_the_page_as_not_armed():
    from src.booking import walk as walk_mod

    page = _submit_page(FakeSubmit(enabled=False))
    assert walk_mod._submit_is_live(page, {"selector": "button#trigger"}) is False


def test_angular_materials_disabled_class_counts_as_disabled():
    """THE ONE THAT MATTERS FOR PAY ONLINE. Material marks a logically-off
    button with mat-mdc-button-disabled while leaving it focusable, so
    is_enabled() alone returns True and the page reads as armed before the
    terms box is ticked. The captured review-pay page carries exactly this
    class before the tick and loses it after."""
    from src.booking import walk as walk_mod

    page = _submit_page(FakeSubmit(
        enabled=True, classes="btn ot-submit-button mat-mdc-button-disabled"))
    assert walk_mod._submit_is_live(page, {"selector": "button#trigger"}) is False


def test_an_unreadable_submit_is_reported_as_not_armed():
    """Never raises: the walk has otherwise succeeded and the browser is about
    to be handed to a person who can see the button themselves."""
    from src.booking import walk as walk_mod

    assert walk_mod._submit_is_live(FakePage({}), None) is False
    assert walk_mod._submit_is_live(FakePage({}), {}) is False


def test_the_submit_locator_handles_both_spec_shapes():
    from src.booking import walk as walk_mod

    page = _submit_page(FakeSubmit(enabled=True))
    assert walk_mod._submit_is_live(page, "button#trigger") is True
    assert walk_mod._submit_is_live(
        page, {"selector": "button#trigger"}) is True
    assert page.requested == ["button#trigger", "button#trigger"]


# --------------------------------------------------------------------------- #
# A capture records WHERE it came from                                         #
# --------------------------------------------------------------------------- #

class _CapturePage:
    def __init__(self, url="https://visa.vfsglobal.com/are/en/nor/services",
                 raises=False):
        self._url = url
        self._raises = raises

    @property
    def url(self):
        if self._raises:
            raise RuntimeError("page is closed")
        return self._url

    def content(self):
        return "<html><body>hi</body></html>"


def test_a_capture_records_the_url_it_came_from(tmp_path, monkeypatch):
    """page.content() is MARKUP ONLY — the address bar is not in it.

    Seven Norway pages were captured across three runs and four url_contains
    gates still read "this page URL has never been observed", because nothing
    wrote the URL down. review-pay was the sole exception, and only by luck: it
    carries a hidden <input id="URL"> that OneTrust posts back.
    """
    from src.booking import walk as walk_mod

    monkeypatch.setattr(walk_mod, "CAPTURE_ROOT", str(tmp_path))
    path = walk_mod._capture_html(_CapturePage(), "services", "AE-NOR")

    saved = open(path, encoding="utf-8").read()
    assert saved.startswith(
        "<!-- captured from: https://visa.vfsglobal.com/are/en/nor/services -->")
    # The DOM itself is still intact below the comment.
    assert "<body>hi</body>" in saved


def test_a_capture_still_saves_when_the_url_cannot_be_read(tmp_path, monkeypatch):
    """Best-effort: the DOM is the irreplaceable part. A walk racing a 12-hour
    invitation must not lose the page over a missing annotation."""
    from src.booking import walk as walk_mod

    monkeypatch.setattr(walk_mod, "CAPTURE_ROOT", str(tmp_path))
    path = walk_mod._capture_html(_CapturePage(raises=True), "services", "AE-NOR")

    saved = open(path, encoding="utf-8").read()
    assert "<body>hi</body>" in saved
    assert "captured from" not in saved


# --------------------------------------------------------------------------- #
# The requested date window ("in_range")                                       #
#                                                                              #
# THE ASYMMETRY THESE TESTS EXIST TO PROTECT:                                  #
#                                                                              #
#   earliest / latest  choose among whatever is offered -> ALWAYS succeed      #
#   in_range           names the dates an agent asked for -> MUST be able to   #
#                      fail, and must never substitute a different date.       #
#                                                                              #
# A missed slot is a bad day. A client booked onto a day nobody agreed to is   #
# not discovered until they turn up at the embassy.                            #
# --------------------------------------------------------------------------- #

from datetime import date as _date


def test_a_good_window_has_no_problems():
    from src.booking import walk as walk_mod

    assert walk_mod.check_window(
        {"date_from": "2026-11-10", "date_to": "2026-11-20"},
        today=_date(2026, 9, 28), max_months=6) == []


def test_a_non_iso_date_is_refused_by_name():
    """15/11/2026 is March-or-April ambiguous across locales. Guessing which
    would book the wrong month, so it is refused rather than parsed."""
    from src.booking import walk as walk_mod

    problems = walk_mod.check_window(
        {"date_from": "15/11/2026", "date_to": "2026-11-20"})
    assert len(problems) == 1
    assert "date_from" in problems[0] and "YYYY-MM-DD" in problems[0]


def test_an_unparseable_end_does_not_also_claim_the_end_is_missing():
    """Both ends WERE supplied; one could not be read. Telling the operator to
    add a field they already filled in sends them the wrong way."""
    from src.booking import walk as walk_mod

    problems = walk_mod.check_window(
        {"date_from": "2026-11-10", "date_to": "not-a-date"})
    assert not any("BOTH ends" in p for p in problems)


def test_one_end_alone_is_refused():
    from src.booking import walk as walk_mod

    problems = walk_mod.check_window({"date_from": "2026-11-10"})
    assert any("BOTH ends" in p for p in problems)


def test_a_backwards_range_is_refused():
    from src.booking import walk as walk_mod

    problems = walk_mod.check_window(
        {"date_from": "2026-11-20", "date_to": "2026-11-10"})
    assert any("backwards" in p for p in problems)


def test_a_range_entirely_in_the_past_is_refused():
    from src.booking import walk as walk_mod

    problems = walk_mod.check_window(
        {"date_from": "2026-01-01", "date_to": "2026-01-31"},
        today=_date(2026, 9, 28))
    assert any("in the past" in p for p in problems)


def test_a_range_that_has_only_started_is_still_usable():
    """Started yesterday, runs another week — the remaining part is bookable,
    and refusing it would fail a booking the agent can still honour."""
    from src.booking import walk as walk_mod

    assert walk_mod.check_window(
        {"date_from": "2026-09-20", "date_to": "2026-10-05"},
        today=_date(2026, 9, 28)) == []


def test_a_range_beyond_the_search_horizon_is_refused_offline():
    """Caught BEFORE a browser starts. A login is the scarce resource — VFS
    blocks an account after ~3 in a short window, and that block outlives a
    12-hour invitation."""
    from src.booking import walk as walk_mod

    problems = walk_mod.check_window(
        {"date_from": "2028-01-01", "date_to": "2028-01-31"},
        today=_date(2026, 9, 28), max_months=6)
    assert any("beyond" in p and "max_months_ahead" in p for p in problems)


def test_no_window_at_all_is_not_a_problem():
    """earliest/latest need no window; check_window must not demand one."""
    from src.booking import walk as walk_mod

    assert walk_mod.check_window({}) == []


def test_dates_in_window_keeps_only_what_was_asked_for():
    from src.booking import walk as walk_mod

    kept = walk_mod.dates_in_window(
        ["2026-11-03", "2026-11-12", "2026-11-20", "2026-12-02"],
        _date(2026, 11, 10), _date(2026, 11, 20))
    assert kept == ["2026-11-12", "2026-11-20"]      # both ends INCLUSIVE


# --------------------------------------------------------------------------- #
# in_range: how the SEARCH behaves                                             #
# --------------------------------------------------------------------------- #

def _window(start, end):
    return {"date_from": start, "date_to": end}


def test_in_range_keeps_paging_past_a_month_whose_dates_are_all_too_early(
        monkeypatch):
    """THE REASON in_range NEEDS ITS OWN SEARCH RULE.

    earliest/latest stop at the first month offering anything. For a window
    that is the wrong rule: October offers dates, but none the agent asked for,
    so stopping there would report "not available" while November — the month
    actually requested — sat one page ahead.
    """
    page, report = _run_multi_month(
        monkeypatch, "in_range",
        [["2026-10-03", "2026-10-09"],           # offered, all before window
         ["2026-11-12", "2026-11-18"]],          # the window
        values=_window("2026-11-10", "2026-11-20"))

    assert len(report.found["months_searched"]) == 2
    assert page.picked_date == "2026-11-12"      # earliest INSIDE the window


def test_in_range_stops_once_the_calendar_is_past_the_window(monkeypatch):
    """Calendars run forwards, so a month already offering dates beyond the
    window means no later month can help. Paging on would burn ~37s a month
    while the slot is not held.

    Refusing is the correct outcome here — the point of the test is that it
    refuses having read ONE month, not three.
    """
    from src.waitlist.errors import WaitlistStepError

    with pytest.raises(WaitlistStepError) as caught:
        _run_multi_month(
            monkeypatch, "in_range",
            [["2026-12-01"], ["2027-01-05"], ["2027-02-05"]],
            values=_window("2026-11-10", "2026-11-20"))

    # December was already past the window, so January and February were never
    # read. The message names only the one month that was.
    assert "month 0(1)" in str(caught.value)
    assert "month 1" not in str(caught.value)


def test_in_range_takes_the_earliest_date_inside_the_window(monkeypatch):
    """Sooner is strictly better — the client attends an office, not a flight,
    and the earliest dates are the ones a competitor takes first."""
    page, _ = _run_multi_month(
        monkeypatch, "in_range",
        [["2026-11-12", "2026-11-15", "2026-11-19"]],
        values=_window("2026-11-10", "2026-11-20"))

    assert page.picked_date == "2026-11-12"


def test_in_range_takes_the_earliest_TIME_on_that_day(monkeypatch):
    """The agent specifies a DATE range, never a time: any time on an agreed
    day is acceptable."""
    page, _ = _run_multi_month(
        monkeypatch, "in_range", [["2026-11-12"]],
        values=_window("2026-11-10", "2026-11-20"))

    assert page.picked_index == 0


def test_in_range_books_NOTHING_when_the_window_is_not_offered(monkeypatch):
    """*** THE ONE THAT MATTERS. *** Dates are on offer either side of the
    window and the bot must take neither. Booking a client onto a day nobody
    agreed to is not discovered until they turn up at the embassy."""
    from src.waitlist.errors import WaitlistStepError

    with pytest.raises(WaitlistStepError) as caught:
        _run_multi_month(
            monkeypatch, "in_range",
            [["2026-10-03"], ["2026-12-14"]],
            values=_window("2026-11-10", "2026-11-20"))

    message = str(caught.value)
    assert "2026-11-10" in message and "2026-11-20" in message
    # Must read as a normal outcome, not a fault to be debugged.
    assert "Nothing was booked" in message


def test_in_range_without_a_window_refuses_rather_than_guessing(monkeypatch):
    """A route asking for in_range with no dates on the client record must NOT
    silently fall back to 'earliest' — that books an arbitrary date."""
    from src.waitlist.errors import WaitlistStepError

    with pytest.raises(WaitlistStepError, match="needs date_from and date_to"):
        _run_multi_month(monkeypatch, "in_range", [["2026-11-12"]], values={})


def test_in_range_refuses_a_malformed_window_before_clicking_anything(
        monkeypatch):
    from src.waitlist.errors import WaitlistStepError

    with pytest.raises(WaitlistStepError, match="cannot be used"):
        _run_multi_month(monkeypatch, "in_range", [["2026-11-12"]],
                         values=_window("2026-11-20", "2026-11-10"))


def test_a_single_day_is_expressed_as_both_ends_the_same(monkeypatch):
    """The agent wants exactly 12 November: date_from == date_to."""
    page, _ = _run_multi_month(
        monkeypatch, "in_range",
        [["2026-11-11", "2026-11-12", "2026-11-13"]],
        values=_window("2026-11-12", "2026-11-12"))

    assert page.picked_date == "2026-11-12"
