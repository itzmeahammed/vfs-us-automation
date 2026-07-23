"""The OTP page's own Cloudflare Turnstile is now gated on the token, exactly
like the login page: if it never passes (Sign In stays disabled), submit_otp
raises TurnstileRejectedError so login() refreshes and re-logs-in on the same
IP — instead of force-clicking a dead button and hanging ~2 min for a dashboard
that never loads (the old spurious DashboardNotReachedError path).
"""

import unittest
from unittest import mock

from src.vfs_bot import otp_flow
from src.vfs_bot.errors import TurnstileRejectedError


class _Button:
    def __init__(self, enabled=True, present=True):
        self._enabled = enabled
        self.present = present
        self.clicked = False

    def count(self):
        return 1 if self.present else 0

    def is_visible(self):
        return self.present

    def is_enabled(self):
        return self._enabled

    def click(self, **_kw):
        self.clicked = True

    def evaluate(self, *_a, **_kw):
        self.clicked = True


class _Locator:
    def __init__(self, button):
        self._button = button

    @property
    def first(self):
        return self._button


class _Page:
    """A page whose only 'Sign In' button behaves as configured; every other
    label the ladder tries reports absent (count 0)."""

    def __init__(self, button, label="Sign In"):
        self._button = button
        self._label = label
        self.url = "https://visa.vfsglobal.com/are/en/grc/login"

    def get_by_role(self, _role, name=None):
        if name == self._label:
            return _Locator(self._button)
        return _Locator(_Button(present=False))

    def screenshot(self, *_a, **_kw):
        pass


class TestOtpTurnstile(unittest.TestCase):
    def test_disabled_button_that_never_solves_raises(self):
        btn = _Button(enabled=False)
        page = _Page(btn)
        with mock.patch.object(otp_flow.turnstile, "wait_for_turnstile_passed",
                               return_value=False), \
             mock.patch.object(otp_flow.turnstile, "click_turnstile_by_coords",
                               return_value=False), \
             mock.patch.object(otp_flow.turnstile, "wait_for_signin_enabled",
                               return_value=False), \
             mock.patch.object(otp_flow.diagnostics, "take_final_screenshot"):
            with self.assertRaises(TurnstileRejectedError):
                otp_flow.submit_otp(page)
        self.assertFalse(btn.clicked)  # never force-clicked a dead button

    def test_enabled_button_submits_normally(self):
        btn = _Button(enabled=True)
        page = _Page(btn)
        with mock.patch.object(otp_flow.diagnostics, "take_screenshot"):
            self.assertTrue(otp_flow.submit_otp(page))
        self.assertTrue(btn.clicked)

    def test_turnstile_solves_then_button_enables(self):
        # Disabled at first; the token lands and the button then enables.
        btn = _Button(enabled=False)

        def _enable(*_a, **_kw):
            btn._enabled = True
            return True

        page = _Page(btn)
        with mock.patch.object(otp_flow.turnstile, "wait_for_turnstile_passed",
                               return_value=True), \
             mock.patch.object(otp_flow.turnstile, "wait_for_signin_enabled",
                               side_effect=_enable), \
             mock.patch.object(otp_flow.diagnostics, "take_screenshot"):
            self.assertTrue(otp_flow.submit_otp(page))
        self.assertTrue(btn.clicked)


if __name__ == "__main__":
    unittest.main()
