"""Tests for declarative field filling, and specifically for "if_present".

Each portal renders a different subset of fields, and VFS varies it BETWEEN
SESSIONS on the same portal — the Switzerland "Your Details" form has been
captured both with and without its two address lines. A route config therefore
lists every field it may need and marks the conditional ones "if_present": true.

These tests exist because the first implementation of that had a real bug: the
skip lived in `except WaitlistStepError`, but a missing control raises
Playwright's own TimeoutError, which matched the later `except Exception` and was
re-raised. The flag silently did nothing, and each absent field burned the full
15s fill timeout before failing the whole step.

Run: python -m unittest tests.test_waitlist_fields
"""

import os
import time
import unittest
from unittest import mock

from src.waitlist import fields
from src.waitlist.errors import WaitlistConfigError, WaitlistStepError


class _Locator:
    """Playwright-ish locator. `present=False` times out like a real one."""

    def __init__(self, present=True, fill_error=None):
        self.present = present
        self.fill_error = fill_error
        self.filled = None

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def nth(self, index):
        return self

    def filter(self, **kwargs):
        return self

    def locator(self, selector):
        return self

    def count(self):
        return 1 if self.present else 0

    def wait_for(self, state=None, timeout=0):
        if not self.present:
            # Mimic a real timeout: it costs the full budget, which is exactly
            # what the presence probe exists to avoid paying per absent field.
            time.sleep(timeout / 1000.0)
            raise TimeoutError(f"waiting for locator (state={state})")

    def scroll_into_view_if_needed(self, timeout=0):
        pass

    def fill(self, value, timeout=0):
        if self.fill_error:
            raise self.fill_error
        self.filled = value

    def evaluate(self, script, *args):
        if self.fill_error:
            raise self.fill_error


class _Page:
    def __init__(self, present=True, fill_error=None):
        self.locator_obj = _Locator(present, fill_error)

    def locator(self, selector):
        return self.locator_obj

    def wait_for_timeout(self, ms):
        pass


class _NoLoader:
    """Stubs the Turnstile spinner wait, which is irrelevant here."""

    @staticmethod
    def wait_for_loader(page, **kwargs):
        pass


class _FieldTestCase(unittest.TestCase):
    def setUp(self):
        self._turnstile = fields.turnstile
        fields.turnstile = _NoLoader()

    def tearDown(self):
        fields.turnstile = self._turnstile

    def _spec(self, **overrides):
        # timeout_ms is deliberately small: the fake locator SLEEPS for the full
        # budget to mimic a real Playwright timeout, so using the 15s production
        # default would make this suite take half a minute for no extra coverage.
        # The timing test below asserts against the real default separately.
        spec = {"name": "address_line_1", "label": "Address line 1",
                "widget": "text", "value": "{{addr}}", "required": True,
                "timeout_ms": 300}
        spec.update(overrides)
        return spec

    CONTEXT = {"addr": "Flat 101"}


class IfPresentTests(_FieldTestCase):
    def test_present_field_is_filled(self):
        page = _Page(present=True)
        result = fields.fill_one(page, self._spec(if_present=True), self.CONTEXT)
        self.assertEqual(result, "Flat 101")
        self.assertEqual(page.locator_obj.filled, "Flat 101")

    def test_absent_field_is_skipped_not_failed(self):
        # THE REGRESSION: this used to raise, failing the whole step.
        result = fields.fill_one(_Page(present=False),
                                 self._spec(if_present=True), self.CONTEXT)
        self.assertIsNone(result)

    def test_absent_field_is_skipped_quickly(self):
        # The probe must be far cheaper than the PRODUCTION fill timeout, or
        # every absent field costs 15s of dead time — the original bug's real
        # cost. Uses the production timeout deliberately (no timeout_ms
        # override) so this asserts the shipped behaviour.
        spec = self._spec(if_present=True)
        spec.pop("timeout_ms")
        started = time.time()
        fields.fill_one(_Page(present=False), spec, self.CONTEXT)
        elapsed = time.time() - started

        self.assertLess(elapsed, fields.DEFAULT_TIMEOUT_MS / 1000.0 / 4)
        self.assertLess(elapsed, fields.PRESENCE_PROBE_MS / 1000.0 + 1.0)

    def test_absent_field_WITHOUT_the_flag_still_fails(self):
        # if_present must not weaken required fields.
        with self.assertRaises(WaitlistStepError):
            fields.fill_one(_Page(present=False), self._spec(), self.CONTEXT)

    def test_present_but_unfillable_field_still_fails(self):
        # "not on this page" (skip) and "on the page but broken" (fail) are
        # genuinely different outcomes — a timeout could not tell them apart,
        # which is why presence is probed separately.
        page = _Page(present=True, fill_error=RuntimeError("intercepted"))
        with self.assertRaises(WaitlistStepError):
            fields.fill_one(page, self._spec(if_present=True), self.CONTEXT)

    def test_skipped_field_does_not_count_as_written(self):
        specs = [self._spec(if_present=True),
                 self._spec(name="other", label="Other", if_present=True)]
        written = fields.fill_all(_Page(present=False), specs, self.CONTEXT)
        self.assertEqual(written, 0)

    def test_dry_run_does_not_probe_or_fill(self):
        page = _Page(present=False)
        result = fields.fill_one(page, self._spec(if_present=True),
                                 self.CONTEXT, dry_run=True)
        # A dry run validates config + data; it must not depend on the live page.
        self.assertEqual(result, "Flat 101")
        self.assertIsNone(page.locator_obj.filled)


class ExistenceProbeTests(_FieldTestCase):
    def test_exists_true_when_attached(self):
        self.assertTrue(fields._exists(_Page(True), self._spec(), "text"))

    def test_exists_false_when_absent(self):
        self.assertFalse(fields._exists(_Page(False), self._spec(), "text"))

    def test_exists_false_for_a_malformed_spec(self):
        # No label/control/selector — cannot be located, so cannot be present.
        self.assertFalse(fields._exists(_Page(True), {"name": "broken"}, "text"))

    def test_probe_checks_attached_not_visible(self):
        # A control below the fold or in a collapsed section is present and
        # fillable (fill_field scrolls to it); requiring visibility here would
        # silently skip real data.
        page = _Page(True)
        with mock.patch.object(page.locator_obj, "wait_for") as wait_for:
            fields._exists(page, self._spec(), "text")
        self.assertEqual(wait_for.call_args.kwargs.get("state"), "attached")


class _CheckboxLocator:
    """Material checkbox stand-in.

    `ticks_via` names the ONLY sub-locator whose click actually changes the
    state; every other click "succeeds" but does nothing — which is exactly how
    the real page behaves when the click lands on the label's <a> link or is
    swallowed by Material's ripple overlay.
    """

    def __init__(self, ticks_via="input", checked=False, link_opens=True):
        self.ticks_via = ticks_via
        self.checked = checked
        self.link_opens = link_opens
        self.clicks = []
        self._path = "root"

    def _child(self, path):
        child = _CheckboxLocator(self.ticks_via, self.checked, self.link_opens)
        child._path = path
        child.parent = self
        return child

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def nth(self, index):
        return self

    def filter(self, **kwargs):
        return self

    def locator(self, selector):
        if "native-control" in selector or "input" in selector:
            return self._child("input")
        if "touch-target" in selector:
            return self._child("touch")
        if "label span" in selector:
            return self._child("label-span")
        if "label" in selector:
            return self._child("label")
        return self._child(selector)

    def wait_for(self, state=None, timeout=0):
        pass

    def scroll_into_view_if_needed(self, timeout=0):
        pass

    def is_checked(self):
        return _CheckboxLocator._state.get(id(self._root()), False)

    def _root(self):
        node = self
        while hasattr(node, "parent"):
            node = node.parent
        return node

    def click(self, timeout=0, force=False):
        root = self._root()
        root.clicks.append(self._path)
        # The bare label click hits the <a> and navigates away instead.
        if self._path == "label" and root.link_opens:
            return
        if self._path == root.ticks_via:
            _CheckboxLocator._state[id(root)] = True

    def evaluate(self, script, *args):
        root = self._root()
        root.clicks.append("js")
        if root.ticks_via == "js":
            _CheckboxLocator._state[id(root)] = True

    _state = {}


class _CheckboxPage:
    def __init__(self, locator):
        self.locator_obj = locator

    def locator(self, selector):
        return self.locator_obj

    def wait_for_timeout(self, ms):
        pass


class CheckboxClickTests(_FieldTestCase):
    """The consent checkboxes are the trickiest control on the whole flow.

    VFS's T&C label wraps a link:
        <label><span>I accept the</span><a target="_blank">Terms...</a></label>
    Playwright clicks an element's CENTRE, which on that label is the anchor —
    so the original label-first ladder opened the T&Cs in a new tab and left the
    box unticked, failing the committing step.
    """

    def setUp(self):
        super().setUp()
        _CheckboxLocator._state.clear()

    def _spec(self, **overrides):
        spec = {"name": "accept_terms", "label": "I accept the",
                "widget": "checkbox", "value": True, "required": True,
                "timeout_ms": 300}
        spec.update(overrides)
        return spec

    def _fill(self, locator):
        fields.fill_one(_CheckboxPage(locator), self._spec(), {})

    def test_native_input_is_tried_first(self):
        locator = _CheckboxLocator(ticks_via="input")
        self._fill(locator)
        self.assertEqual(locator.clicks[0], "input")

    def test_label_with_a_link_does_not_defeat_it(self):
        # THE REGRESSION: only the native input ticks this box; a label click
        # navigates. The ladder must still succeed.
        locator = _CheckboxLocator(ticks_via="input", link_opens=True)
        self._fill(locator)
        self.assertTrue(locator.is_checked())

    def test_falls_through_to_the_touch_target(self):
        locator = _CheckboxLocator(ticks_via="touch")
        self._fill(locator)
        self.assertTrue(locator.is_checked())
        self.assertIn("touch", locator.clicks)

    def test_falls_through_to_js_dispatch_as_a_last_resort(self):
        locator = _CheckboxLocator(ticks_via="js")
        self._fill(locator)
        self.assertTrue(locator.is_checked())

    def test_state_is_verified_after_each_strategy(self):
        # A click that "succeeds" but changes nothing must NOT be treated as
        # done — that was precisely the original failure.
        locator = _CheckboxLocator(ticks_via="touch")
        self._fill(locator)
        self.assertGreater(len(locator.clicks), 1)

    def test_raises_when_nothing_works(self):
        locator = _CheckboxLocator(ticks_via="nothing")
        with self.assertRaises(WaitlistStepError) as cm:
            self._fill(locator)
        self.assertIn("did not change state", str(cm.exception))

    def test_already_ticked_is_left_alone(self):
        # Never toggle blindly: a box already ticked by a previous partial
        # attempt must not be turned back OFF.
        locator = _CheckboxLocator(ticks_via="input")
        _CheckboxLocator._state[id(locator)] = True
        self._fill(locator)
        self.assertEqual(locator.clicks, [])
        self.assertTrue(locator.is_checked())


class _UploadLocator(_Locator):
    def __init__(self, present=True):
        super().__init__(present=present)
        self.uploaded = None
        self.clicked = False

    def set_input_files(self, path, timeout=0):
        self.uploaded = path

    def click(self, timeout=0, force=False):
        self.clicked = True

    def filter(self, **kwargs):
        return self


class _UploadPage:
    def __init__(self, locator):
        self.locator_obj = locator
        self.waited_ms = 0

    def locator(self, selector):
        return self.locator_obj

    def get_by_role(self, role, name="", exact=False):
        return self.locator_obj

    def wait_for_timeout(self, ms):
        self.waited_ms += ms


#: A minimal but REAL PNG: 8-byte signature + enough bytes to be non-empty.
#: documents.validate() sniffs magic bytes and rejects empty files, so an
#: upload fixture has to actually look like a PNG.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class FileUploadTests(_FieldTestCase):
    """Italy takes the applicant's details FROM an uploaded passport scan
    rather than from typed fields: upload, confirm the preview, wait for VFS to
    OCR it, then Save."""

    def _png(self):
        """Writes a throwaway PNG and returns its path; cleaned up on teardown."""
        import tempfile

        handle, path = tempfile.mkstemp(suffix=".png")
        with os.fdopen(handle, "wb") as f:
            f.write(_PNG_BYTES)
        self._temp_files.append(path)
        return path

    def setUp(self):
        super().setUp()
        self._temp_files = []

    def tearDown(self):
        for path in self._temp_files:
            try:
                os.remove(path)
            except OSError:
                pass
        super().tearDown()

    def _spec(self, **overrides):
        spec = {"name": "passport_scan",
                "selector": "app-file-upload input[type='file']",
                "widget": "file", "value": "{{passport_scan}}",
                "required": True, "timeout_ms": 300}
        spec.update(overrides)
        return spec

    def _context(self, path):
        return {"passport_scan": path}

    def test_uploads_the_file(self):
        path = self._png()
        locator = _UploadLocator()
        fields.fill_one(_UploadPage(locator), self._spec(), self._context(path))
        self.assertEqual(locator.uploaded, path)

    def test_missing_file_fails_early_with_a_clear_message(self):
        # Better than letting Playwright report a cryptic upload error.
        with self.assertRaises(WaitlistStepError) as cm:
            fields.fill_one(_UploadPage(_UploadLocator()), self._spec(),
                            self._context("/no/such/passport.png"))
        self.assertIn("no file at", str(cm.exception))

    def test_confirms_the_upload_preview(self):
        path = self._png()
        locator = _UploadLocator()
        spec = self._spec(after_upload={"role": "button", "name": "Continue"})
        fields.fill_one(_UploadPage(locator), spec, self._context(path))
        self.assertTrue(locator.clicked)

    def test_waits_for_the_portal_to_read_the_document(self):
        # Save would be rejected against a form VFS has not populated yet.
        path = self._png()
        page = _UploadPage(_UploadLocator())
        fields.fill_one(page, self._spec(wait_after_ms=5000),
                        self._context(path))
        self.assertGreaterEqual(page.waited_ms, 5000)


class FillAllTests(_FieldTestCase):
    def test_disabled_fields_are_skipped(self):
        specs = [self._spec(disabled=True)]
        self.assertEqual(fields.fill_all(_Page(True), specs, self.CONTEXT), 0)

    def test_counts_only_fields_actually_written(self):
        specs = [self._spec(), self._spec(name="b", label="B", value=None,
                                          required=False)]
        self.assertEqual(fields.fill_all(_Page(True), specs, self.CONTEXT), 1)

    def test_missing_placeholder_is_a_config_error_not_a_step_error(self):
        # A blank passport number must fail loudly at resolve time, before the
        # page is ever touched.
        with self.assertRaises(WaitlistConfigError):
            fields.fill_one(_Page(True), self._spec(value="{{nope}}"), {})


if __name__ == "__main__":
    unittest.main()
