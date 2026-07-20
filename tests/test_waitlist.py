"""Unit tests for the waitlist feature (src/vfs_bot/waitlist.py).

Covers the pure marker/predicate helpers, the success-chat message builder, the
summary 'no slots' -> 'waitlist' swap, and read-only checkbox detection against a
faked Playwright page. No browser is launched.

Run: python -m unittest tests.test_waitlist
"""

import unittest

from src.vfs_bot import waitlist
from src.utils import telegram_message


class _FakeLocator:
    def __init__(self, visible):
        self._visible = visible

    @property
    def first(self):
        return self

    def is_visible(self):
        return self._visible


class _FakePage:
    """Returns a visible locator only for the selectors named in `visible_for`."""

    def __init__(self, visible_for=()):
        self.visible_for = set(visible_for)

    def locator(self, selector):
        return _FakeLocator(selector in self.visible_for)

    def get_by_text(self, text, exact=False):
        return _FakeLocator(text in self.visible_for)


class TestMarkerHelpers(unittest.TestCase):
    def test_marker_roundtrip(self):
        msg = waitlist.as_result()
        self.assertTrue(waitlist.is_waitlist(msg))

    def test_non_waitlist_messages(self):
        for m in ("", None, "DISABLED", "ERROR: could not select centre",
                  "Earliest available slot: 12 August 2026"):
            self.assertFalse(waitlist.is_waitlist(m))

    def test_marker_has_no_date_so_not_a_slot(self):
        # Critical: a waitlist combo must NOT be counted as an available slot.
        self.assertFalse(telegram_message._has_slot(waitlist.as_result()))

    def test_count_waitlist(self):
        results = [
            ("Dubai / Short Stay / Tourist", waitlist.as_result()),
            ("Dubai / Business", "DISABLED"),
            ("Dubai / Long Stay", "No slot message shown (no availability?)."),
            ("Dubai / Student", waitlist.as_result()),
        ]
        self.assertEqual(waitlist.count_waitlist(results), 2)
        self.assertEqual(waitlist.count_waitlist([]), 0)
        self.assertEqual(waitlist.count_waitlist(None), 0)


class TestBuildMessage(unittest.TestCase):
    def test_empty_when_no_waitlist(self):
        results = [("Dubai / Tourist", "No slot message shown (no availability?).")]
        self.assertEqual(waitlist.build_message("AE", "ITA", results), "")

    def test_names_waitlist_combos_and_link(self):
        results = [
            ("Italy Visa Application Center ,Dubai / Short Stay / Tourist visa",
             waitlist.as_result()),
            ("Dubai / Business", "DISABLED"),
        ]
        msg = waitlist.build_message("AE", "ITA", results,
                                     "https://visa.vfsglobal.com/are/en/ita/login")
        self.assertIn("- Waitlist", msg)           # bullet under each combo
        self.assertIn("Tourist visa", msg)
        self.assertIn("Italy", msg)                # country substituted into label
        self.assertNotIn("Business", msg)          # non-waitlist combo excluded
        self.assertIn("visa.vfsglobal.com", msg)   # login link appended


class TestSummarySwap(unittest.TestCase):
    """run_summary shows 'waitlist' in place of 'no slots' when flagged, and a
    real slot still wins over a waitlist."""

    def _line(self, outcome):
        return telegram_message.run_summary([outcome], "", "2026-07-20 12:00")

    def _base(self, **over):
        o = {"source": "AE", "dest": "ITA", "status": "OK", "attempts": 1,
             "slots": 0, "slot_types": [], "waitlist": 0}
        o.update(over)
        return o

    def test_waitlist_replaces_no_slots(self):
        out = self._line(self._base(waitlist=1))
        self.assertIn("waitlist", out)
        self.assertNotIn("no slots", out)

    def test_no_waitlist_still_says_no_slots(self):
        out = self._line(self._base(waitlist=0))
        self.assertIn("no slots", out)

    def test_real_slot_beats_waitlist(self):
        out = self._line(self._base(slots=2, slot_types=[["Tourist", 2]], waitlist=1))
        self.assertIn("slot(s)", out)
        self.assertNotIn("waitlist", out)


class TestDetection(unittest.TestCase):
    """is_offered is read-only and matches on the stable form-control selector or
    the on-page wording, and is False otherwise."""

    def test_detects_by_formcontrol(self):
        page = _FakePage(visible_for=['mat-checkbox[formcontrolname="agreeToWaitlist"]'])
        self.assertTrue(waitlist.is_offered(page))

    def test_detects_by_text_fallback(self):
        page = _FakePage(visible_for=["confirm waitlist"])
        self.assertTrue(waitlist.is_offered(page))

    def test_absent(self):
        self.assertFalse(waitlist.is_offered(_FakePage()))


if __name__ == "__main__":
    unittest.main()
