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
