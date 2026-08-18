"""Unit tests for `doctor` — the config-vs-live-page contract check.

Tested against a fake page rather than Playwright, so the probe logic (found /
missing / ambiguous, and the index-means-ambiguity-is-expected rule) is
verifiable without a browser.

Run: python -m unittest tests.test_waitlist_doctor
"""

import unittest

from src.waitlist import doctor
from src.waitlist.doctor import Finding


class _Locator:
    """Minimal stand-in for a Playwright locator."""

    def __init__(self, count=1, page=None, selector=""):
        self._count = count
        self._page = page
        self._selector = selector

    def count(self):
        return self._count

    def wait_for(self, **kwargs):
        if self._count == 0:
            raise TimeoutError(f"no element for {self._selector}")

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def nth(self, index):
        return self

    def filter(self, **kwargs):
        has_text = kwargs.get("has_text")
        if has_text is not None and self._page is not None:
            return _Locator(self._page.matches.get(has_text, 0),
                            self._page, str(has_text))
        return self

    def locator(self, selector):
        if self._page is not None:
            return _Locator(self._page.matches.get(selector, self._count),
                            self._page, selector)
        return self

    def get_by_role(self, role, name="", exact=False):
        return self


class _FakePage:
    """`matches` maps a selector or label to how many elements it finds."""

    def __init__(self, matches=None):
        self.matches = matches or {}

    def locator(self, selector):
        return _Locator(self.matches.get(selector, 0), self, selector)

    def get_by_role(self, role, name="", exact=False):
        return _Locator(self.matches.get(name, 0), self, str(name))

    def get_by_text(self, text, exact=False):
        return _Locator(self.matches.get(text, 0), self, str(text))

    def wait_for_timeout(self, ms):
        pass


class FieldProbeTests(unittest.TestCase):
    def test_found_field_is_ok(self):
        page = _FakePage({"app-dynamic-control": 1, "input": 1,
                          "First Name": 1})
        finding = doctor._probe(
            page, {"name": "first_name", "label": "First Name",
                   "widget": "text"}, "your_details")
        self.assertEqual(finding.status, Finding.OK)
        self.assertTrue(finding.ok)

    def test_missing_field_is_reported(self):
        finding = doctor._probe(
            page=_FakePage({}),
            spec={"name": "first_name", "label": "First Name"},
            step_name="your_details")
        self.assertEqual(finding.status, Finding.MISSING)
        self.assertFalse(finding.ok)

    def test_disabled_field_is_skipped_not_failed(self):
        finding = doctor._probe(
            _FakePage({}), {"name": "x", "label": "X", "disabled": True}, "s")
        self.assertEqual(finding.status, Finding.SKIPPED)
        self.assertTrue(finding.ok)

    def test_field_without_any_locator_key_is_a_config_error(self):
        finding = doctor._probe(_FakePage({}), {"name": "broken"}, "s")
        self.assertEqual(finding.status, Finding.MISSING)
        self.assertIn("label", finding.detail)

    def test_ambiguous_match_is_flagged(self):
        # Two elements for one field is as broken as none — it silently fills
        # the wrong box. This is the VFS-reskin failure doctor exists to catch.
        page = _FakePage({"app-dynamic-control": 1, "I accept the": 1,
                          "input": 3})
        finding = doctor._probe(
            page, {"name": "terms", "label": "I accept the", "widget": "text"},
            "review_pay")
        self.assertEqual(finding.status, Finding.AMBIGUOUS)
        self.assertEqual(finding.found, 3)

    def test_explicit_index_means_multiple_matches_are_expected(self):
        # The split phone field and the twin consent checkboxes both match
        # several elements BY DESIGN, so a configured "index" suppresses the
        # ambiguity warning.
        page = _FakePage({"app-dynamic-control": 1, "Contact number": 1,
                          "input": 2})
        finding = doctor._probe(
            page, {"name": "phone", "label": "Contact number", "widget": "text",
                   "index": 1}, "your_details")
        self.assertEqual(finding.status, Finding.OK)


class ButtonProbeTests(unittest.TestCase):
    def test_found_button(self):
        finding = doctor._probe_button(
            _FakePage({"Save": 1}), {"role": "button", "name": "Save"},
            "your_details", "submit button")
        self.assertEqual(finding.status, Finding.OK)

    def test_missing_button(self):
        finding = doctor._probe_button(
            _FakePage({}), {"role": "button", "name": "Save"},
            "your_details", "submit button")
        self.assertEqual(finding.status, Finding.MISSING)

    def test_ambiguous_button(self):
        finding = doctor._probe_button(
            _FakePage({"Save": 2}), {"role": "button", "name": "Save"},
            "your_details", "submit button")
        self.assertEqual(finding.status, Finding.AMBIGUOUS)

    def test_no_button_configured_is_skipped(self):
        finding = doctor._probe_button(
            _FakePage({}), None, "step", "submit button")
        self.assertEqual(finding.status, Finding.SKIPPED)
        self.assertTrue(finding.ok)


class CheckboxTests(unittest.TestCase):
    def setUp(self):
        from src.waitlist import config as wcfg
        self._original = wcfg.checkbox_selector
        wcfg.checkbox_selector = lambda route: "mat-checkbox[x]"

    def tearDown(self):
        from src.waitlist import config as wcfg
        wcfg.checkbox_selector = self._original

    def test_present(self):
        finding = doctor.check_checkbox(
            _FakePage({"mat-checkbox[x]": 1}), "AE-CHE")
        self.assertEqual(finding.status, Finding.OK)

    def test_absent_explains_both_possible_causes(self):
        # Absence is ambiguous: VFS may have changed it, OR this combination
        # simply has slots today. Saying so avoids a false alarm.
        finding = doctor.check_checkbox(_FakePage({}), "AE-CHE")
        self.assertEqual(finding.status, Finding.MISSING)
        self.assertIn("slots", finding.detail)


class ReportTests(unittest.TestCase):
    def test_all_ok_reports_success(self):
        findings = [Finding("s", "a", Finding.OK), Finding("s", "b", Finding.OK)]
        self.assertIn("✓ Every configured selector resolves.",
                      doctor.report(findings, "AE-CHE"))

    def test_problems_name_the_file_to_edit(self):
        findings = [Finding("s", "a", Finding.OK),
                    Finding("s", "b", Finding.MISSING, "not found")]
        report = doctor.report(findings, "AE-CHE")
        self.assertIn("1 problem(s)", report)
        self.assertIn("config/waitlist/AE-CHE.json", report)

    def test_findings_are_grouped_by_step(self):
        findings = [Finding("step1", "a", Finding.OK),
                    Finding("step2", "b", Finding.OK)]
        report = doctor.report(findings, "AE-CHE")
        self.assertIn("step1", report)
        self.assertIn("step2", report)

    def test_skipped_does_not_count_as_a_problem(self):
        findings = [Finding("s", "a", Finding.SKIPPED, "disabled")]
        self.assertIn("✓", doctor.report(findings, "AE-CHE"))


if __name__ == "__main__":
    unittest.main()
