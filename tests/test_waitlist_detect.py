"""Unit tests for waitlist checkbox detection.

The hard case this covers: VFS portals do NOT agree on how the waitlist checkbox
is rendered. Some bind it (formcontrolname="agreeToWaitlist"); Switzerland emits
a bare <mat-checkbox> whose only id is a positional 'mat-mdc-checkbox-0' that
shifts whenever the page gains a control. detect.locate() has to handle both, and
must never confuse the waitlist checkbox with the review-pay consent boxes.

Run: python -m unittest tests.test_waitlist_detect
"""

import unittest

from src.waitlist import detect


class _Locator:
    def __init__(self, count=0, text="", page=None):
        self._count = count
        self._text = text
        self._page = page

    def count(self):
        return self._count

    def is_visible(self):
        return self._count > 0

    @property
    def first(self):
        return self

    def nth(self, index):
        return self

    def filter(self, has_text=None, **kwargs):
        if has_text is None:
            return self
        # Simulates Playwright's substring, case-insensitive text filter.
        matched = [t for t in (self._page.texts if self._page else [])
                   if str(has_text).lower() in t.lower()]
        return _Locator(len(matched), has_text, self._page)

    def locator(self, selector):
        return _Locator(1 if self._count else 0, self._text, self._page)


class _FakePage:
    """`selectors` maps a CSS selector to a match count.
    `texts` is the text of each <mat-checkbox> on the page."""

    def __init__(self, selectors=None, texts=(), visible_text=()):
        self.selectors = selectors or {}
        self.texts = list(texts)
        self.visible_text = set(visible_text)

    def locator(self, selector):
        if selector == "mat-checkbox":
            return _Locator(len(self.texts), page=self)
        return _Locator(self.selectors.get(selector, 0), page=self)

    def get_by_text(self, text, exact=False):
        return _Locator(1 if text in self.visible_text else 0, page=self)


class BoundControlTests(unittest.TestCase):
    """When a portal DOES bind the control, that must win — it names the
    control rather than inferring it from position or wording."""

    def test_agree_to_waitlist_is_found(self):
        page = _FakePage({'mat-checkbox[formcontrolname="agreeToWaitlist"]': 1})
        self.assertIsNotNone(detect.locate(page))
        self.assertTrue(detect.is_offered(page))

    def test_partial_formcontrol_match_is_found(self):
        page = _FakePage({'mat-checkbox[formcontrolname*="waitlist" i]': 1})
        self.assertIsNotNone(detect.locate(page))


class BareCheckboxTests(unittest.TestCase):
    """Switzerland: no formcontrolname, only a positional id."""

    def test_lone_bare_checkbox_is_the_waitlist_one(self):
        # On Appointment Details a single checkbox IS the waitlist offer.
        page = _FakePage(texts=["Currently No slots are available"])
        self.assertIsNotNone(detect.locate(page))
        self.assertTrue(detect.is_offered(page))

    def test_no_checkbox_at_all(self):
        page = _FakePage(texts=[])
        self.assertIsNone(detect.locate(page))
        self.assertFalse(detect.is_offered(page))

    def test_several_checkboxes_pick_the_waitlist_one_by_wording(self):
        # The review-pay page has consent checkboxes that must NOT be mistaken
        # for the waitlist one.
        page = _FakePage(texts=["I accept the Terms and Conditions",
                                "please confirm waitlist",
                                "I accept the VAS T&Cs"])
        found = detect.locate(page)
        self.assertIsNotNone(found)
        self.assertEqual(found.count(), 1)

    def test_several_checkboxes_none_mentioning_waitlist_falls_back(self):
        # Degrades to the first rather than failing outright, and logs a hint to
        # pin "checkbox" in the route config.
        page = _FakePage(texts=["I accept the Terms", "I accept the VAS T&Cs"])
        self.assertIsNotNone(detect.locate(page))


class PinnedSelectorTests(unittest.TestCase):
    def test_pinned_selector_is_used_verbatim(self):
        page = _FakePage({"#my-box": 1}, texts=["something else"])
        self.assertIsNotNone(detect.locate(page, "#my-box"))

    def test_pinned_selector_that_matches_nothing_returns_none(self):
        # A pin is an explicit instruction — do NOT silently fall back to
        # guessing, or a wrong pin would look like it worked.
        page = _FakePage({}, texts=["please confirm waitlist"])
        self.assertIsNone(detect.locate(page, "#missing"))


class TextFallbackTests(unittest.TestCase):
    def test_wording_fallback_when_no_mat_checkbox_renders(self):
        page = _FakePage(texts=[], visible_text=["confirm waitlist"])
        self.assertTrue(detect.is_offered(page))


class CheckedStateTests(unittest.TestCase):
    def test_missing_checkbox_reads_as_unchecked(self):
        self.assertFalse(detect.is_checked(_FakePage(texts=[])))

    def test_never_raises_on_a_broken_page(self):
        class _Broken:
            def locator(self, selector):
                raise RuntimeError("page died")

            def get_by_text(self, text, exact=False):
                raise RuntimeError("page died")

        # Detection runs on every slot-check run; it must never be the thing
        # that breaks one.
        self.assertFalse(detect.is_offered(_Broken()))
        self.assertFalse(detect.is_checked(_Broken()))
        self.assertIsNone(detect.locate(_Broken()))


if __name__ == "__main__":
    unittest.main()
