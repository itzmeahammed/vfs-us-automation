"""await_dashboard_handling_captcha must give up on a Cloudflare RE-CHALLENGE
loop — where the 'Verify Captcha' dialog re-appears after every solve — instead
of grinding the full 90s timeout (and re-downloading the challenge each cycle,
which is what burned ~6.9 MB in the field). It bails after
retry.dashboard_captcha_cycles solves and returns False.
"""

import unittest
from unittest import mock

from src.vfs_bot import turnstile


class _Page:
    def __init__(self):
        self.url = "https://visa.vfsglobal.com/are/en/ita/login"  # never /dashboard
        self.waits = 0

    def wait_for_timeout(self, _ms):
        self.waits += 1


class TestCaptchaLoopCap(unittest.TestCase):
    def test_bails_after_max_cycles(self):
        from src.utils.config_reader import initialize_config
        initialize_config()
        max_cycles = turnstile.settings().retry.dashboard_captcha_cycles

        page = _Page()
        with mock.patch.object(turnstile, "dismiss_captcha", return_value=True) as dc, \
             mock.patch.object(turnstile.block_detection, "is_geo_blocked",
                               return_value=False), \
             mock.patch.object(turnstile.block_detection, "raise_if_blocked"), \
             mock.patch.object(turnstile.block_detection, "is_email_not_registered",
                               return_value=False), \
             mock.patch.object(turnstile.block_detection, "is_invalid_credentials",
                               return_value=False), \
             mock.patch.object(turnstile.diagnostics, "take_final_screenshot"):
            result = turnstile.await_dashboard_handling_captcha(page, timeout_ms=90000)

        self.assertFalse(result)                      # gave up, no dashboard
        self.assertEqual(dc.call_count, max_cycles)   # exactly N solves, not ~90
        self.assertLess(page.waits, max_cycles + 2)   # did NOT grind the timeout

    def test_no_dialog_still_times_out_normally(self):
        # When there's never a dialog, the counter never trips — it just waits
        # for the dashboard and returns False on timeout (unchanged behaviour).
        from src.utils.config_reader import initialize_config
        initialize_config()
        page = _Page()
        with mock.patch.object(turnstile, "dismiss_captcha", return_value=False), \
             mock.patch.object(turnstile.block_detection, "is_geo_blocked",
                               return_value=False), \
             mock.patch.object(turnstile.block_detection, "raise_if_blocked"), \
             mock.patch.object(turnstile.block_detection, "is_email_not_registered",
                               return_value=False), \
             mock.patch.object(turnstile.block_detection, "is_invalid_credentials",
                               return_value=False):
            result = turnstile.await_dashboard_handling_captcha(page, timeout_ms=3000)
        self.assertFalse(result)


class _DashPage:
    """Minimal page whose URL is the dashboard but whose content presence and
    captcha-dialog visibility are configurable, to exercise dashboard_content_ready."""

    def __init__(self, url, has_button, captcha_up=False):
        self.url = url
        self._has_button = has_button
        self._captcha_up = captcha_up

    def locator(self, selector):
        page = self

        class _Loc:
            def count(_self):
                return 1 if page._has_button else 0

        return _Loc()


class TestDashboardVerification(unittest.TestCase):
    def test_url_alone_is_not_reached(self):
        # The bug: Cloudflare parks us on /dashboard with a blank body. URL-only
        # used to report 'reached'; content readiness must reject it.
        page = _DashPage("https://visa.vfsglobal.com/are/en/fra/dashboard",
                         has_button=False)
        with mock.patch.object(turnstile, "captcha_visible", return_value=False):
            self.assertTrue(turnstile.dashboard_url_reached(page))
            self.assertFalse(turnstile.dashboard_content_ready(page))

    def test_url_plus_rendered_button_is_reached(self):
        page = _DashPage("https://visa.vfsglobal.com/are/en/fra/dashboard",
                         has_button=True)
        with mock.patch.object(turnstile, "captcha_visible", return_value=False):
            self.assertTrue(turnstile.dashboard_content_ready(page))

    def test_captcha_still_up_is_not_reached(self):
        # Button present but the 'Verify Captcha' dialog is still showing — the
        # redirect hasn't truly settled, so this is NOT the dashboard yet.
        page = _DashPage("https://visa.vfsglobal.com/are/en/fra/dashboard",
                         has_button=True, captcha_up=True)
        with mock.patch.object(turnstile, "captcha_visible", return_value=True):
            self.assertFalse(turnstile.dashboard_content_ready(page))

    def test_non_dashboard_url_is_not_reached(self):
        page = _DashPage("https://visa.vfsglobal.com/are/en/fra/login",
                         has_button=True)
        self.assertFalse(turnstile.dashboard_url_reached(page))
        self.assertFalse(turnstile.dashboard_content_ready(page))


if __name__ == "__main__":
    unittest.main()
