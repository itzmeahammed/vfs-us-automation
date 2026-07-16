"""Behaviour-preservation guard for the typed settings layer.

Each assertion pins a Settings default to the value that was previously HARDCODED
in the code, so the refactor cannot silently change runtime behaviour. If you
intentionally change a default, update BOTH the code default and the number here.

Run: python -m unittest tests.test_settings
"""

import unittest

from src.settings import (
    AccountSafety,
    Browser,
    Otp,
    Retry,
    Schedule,
    Timeouts,
    Turnstile,
)


class TestDefaultsMatchOldConstants(unittest.TestCase):
    """The defaults below equal the constants that lived in the code pre-refactor."""

    def test_timeouts(self):
        t = Timeouts()
        # Old inline values in vfs_bot.py.
        self.assertEqual(t.page_load_ms, 60000)      # page.goto(..., timeout=60000)
        self.assertEqual(t.login_wait_ms, 120000)    # login-form poll loop < 120000
        self.assertEqual(t.dashboard_ms, 90000)      # _await_dashboard timeout_ms=90000
        self.assertEqual(t.slot_read_ms, 12000)      # _read_slot_message timeout=12000

    def test_retry(self):
        r = Retry()
        self.assertEqual(r.backoff_seconds, 15)            # BACKOFF_SECONDS
        self.assertEqual(r.max_ip_tries, 2)                # MAX_IP_TRIES
        self.assertEqual(r.turnstile_refresh_attempts, 2)  # TURNSTILE_REFRESH_ATTEMPTS
        self.assertEqual(r.cdp_port, 9222)                 # CDP_PORT

    def test_browser(self):
        b = Browser()
        self.assertEqual(b.type, "chromium")
        self.assertIs(b.headless, True)
        self.assertIs(b.screenshots_enabled, False)  # SCREENSHOTS_ENABLED = False

    def test_turnstile(self):
        self.assertEqual(Turnstile().manual_wait_seconds, 0)

    def test_account_safety(self):
        a = AccountSafety()
        self.assertEqual(a.hard_cooldown_hours, 24)
        self.assertEqual(a.soft_cooldown_hours, 2)
        self.assertEqual(a.fail_threshold, 3)
        self.assertEqual(a.max_attempts, 2)

    def test_schedule(self):
        s = Schedule()
        self.assertEqual(s.runs_per_hour, 2)
        self.assertEqual(s.start_hour, 9)
        self.assertEqual(s.end_hour, 19)

    def test_otp(self):
        o = Otp()
        self.assertEqual(o.imap_port, 993)
        self.assertEqual(o.timeout_seconds, 120)
        self.assertEqual(o.poll_seconds, 5)
        self.assertEqual(o.otp_length, 6)


class TestTypeCoercion(unittest.TestCase):
    """INI values arrive as strings; the model must hand back real types."""

    def test_ini_strings_coerce(self):
        t = Timeouts(page_load_ms="45000")
        self.assertIsInstance(t.page_load_ms, int)
        self.assertEqual(t.page_load_ms, 45000)
        b = Browser(headless="true", screenshots_enabled="false")
        self.assertIs(b.headless, True)
        self.assertIs(b.screenshots_enabled, False)

    def test_bad_value_raises(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            Retry(backoff_seconds="not-a-number")


if __name__ == "__main__":
    unittest.main()
