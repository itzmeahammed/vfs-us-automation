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
    """Minimal Playwright locator double: a list of (attrs, text) pairs."""

    def __init__(self, items):
        self._items = items

    def count(self):
        return len(self._items)

    def nth(self, index):
        return FakeElement(*self._items[index])


class FakeElement:
    def __init__(self, attrs, text=""):
        self._attrs = attrs
        self._text = text
        self.clicked = False

    def get_attribute(self, name):
        return self._attrs.get(name)

    def inner_text(self, timeout=None):
        return self._text

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None):
        self.clicked = True


class FakePage:
    def __init__(self, by_selector):
        self._by_selector = by_selector
        self.requested = []

    def locator(self, selector):
        self.requested.append(selector)
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

def test_pick_time_clicks_the_label_not_the_radio():
    """The <input type=radio> is tabindex=-1 and visually replaced by its label,
    so a click on the input is intercepted."""
    page = FakePage({
        "table.ba-slot-table label.ba-slot-radio-label": [({}, "")] * 3,
        "table.ba-slot-table td[id^='tv']": [({}, t) for t in REAL_TIMES],
    })
    assert pick_time(page, {}, index=0) == "11:15"
    assert any("label.ba-slot-radio-label" in s for s in page.requested)


def test_pick_time_can_choose_a_later_slot():
    page = FakePage({
        "table.ba-slot-table label.ba-slot-radio-label": [({}, "")] * 3,
        "table.ba-slot-table td[id^='tv']": [({}, t) for t in REAL_TIMES],
    })
    assert pick_time(page, {}, index=2) == "11:45"


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


def _run_strategy(monkeypatch, strategy, dates, times):
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

    step = {"name": "select_slot", "strategy": strategy,
            "calendar": {}, "time_slots": {}}
    walk_mod._do_slot_pick(page, step, report, 1000)
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

    with pytest.raises(WaitlistStepError, match="Unknown slot strategy"):
        _run_strategy(monkeypatch, "cheapest", ["2026-10-28"], ["11:45"])


def test_the_shipped_norway_config_asks_for_latest():
    """Pinned because it is a TESTING choice that must be revisited.

    'latest' takes the least contended slot, which is right for a trial run and
    wrong for a client: a waitlist slot is not held while the remaining pages
    are walked, so speed is what wins one. This test is where that decision is
    visible.
    """
    from src.utils.config_reader import initialize_config
    from src.booking import config as booking_config

    initialize_config()
    booking_config.clear_cache()
    step = next(s for s in booking_config.steps_for("AE-NOR", "new")
                if s.get("type") == "slot_pick")
    assert step.get("strategy") == "latest"
