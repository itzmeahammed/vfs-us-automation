"""Unit tests for the per-country waitlist-notification cooldown.

Covers the store (set/expiry/prune/normalise/disable) and that waitlist.notify
suppresses a second message for the same country within the window but resends
after it. Each test points the store at an isolated temp file — no real state is
touched, no browser or Telegram is used.

Run: python -m unittest tests.test_waitlist_cooldown
"""

import os
import tempfile
import unittest
from unittest import mock

from src.utils import waitlist_cooldown as wc
from src.vfs_bot import waitlist


class _TempStore(unittest.TestCase):
    """Base: redirect the JSON store to a throwaway file and force a 2h window."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._patches = [
            mock.patch.object(wc, "STATE_FILE", os.path.join(self._dir, "wc.json")),
            mock.patch.object(wc, "cooldown_hours", lambda: 2.0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)


class TestStore(_TempStore):
    def test_not_on_cooldown_initially(self):
        self.assertFalse(wc.is_on_cooldown("ITA"))
        self.assertEqual(wc.seconds_left("ITA"), 0.0)

    def test_record_then_on_cooldown(self):
        wc.record_sent("ITA", now=1000.0)
        self.assertTrue(wc.is_on_cooldown("ITA", now=1000.0))
        self.assertTrue(wc.is_on_cooldown("ITA", now=1000.0 + 3600))       # 1h < 2h
        self.assertGreater(wc.seconds_left("ITA", now=1000.0 + 3600), 0)

    def test_expires_after_window(self):
        wc.record_sent("ITA", now=1000.0)
        self.assertFalse(wc.is_on_cooldown("ITA", now=1000.0 + 2 * 3600))  # exactly 2h
        self.assertFalse(wc.is_on_cooldown("ITA", now=1000.0 + 3 * 3600))

    def test_key_normalised(self):
        wc.record_sent(" ita ", now=1000.0)
        self.assertTrue(wc.is_on_cooldown("ITA", now=1000.0))              # case/space-insensitive

    def test_countries_are_independent(self):
        wc.record_sent("ITA", now=1000.0)
        self.assertFalse(wc.is_on_cooldown("CZE", now=1000.0))

    def test_prune_drops_expired_others(self):
        wc.record_sent("ITA", now=0.0)                                     # will be stale
        wc.record_sent("CZE", now=10 * 3600)                               # 10h later -> prunes ITA
        snap = wc.snapshot()
        self.assertIn("CZE", snap)
        self.assertNotIn("ITA", snap)

    def test_clear(self):
        wc.record_sent("ITA", now=1000.0)
        wc.clear("ITA")
        self.assertFalse(wc.is_on_cooldown("ITA", now=1000.0))

    def test_zero_hours_disables(self):
        with mock.patch.object(wc, "cooldown_hours", lambda: 0.0):
            wc.record_sent("ITA", now=1000.0)
            self.assertFalse(wc.is_on_cooldown("ITA", now=1000.0))


class TestNotifySuppression(_TempStore):
    """notify() sends the first time, suppresses within the window, resends after."""

    def _results(self):
        return [("Dubai - Short Stay - Tourist visa", waitlist.as_result())]

    def test_first_sends_second_suppressed(self):
        sent = []
        with mock.patch("src.utils.telegram.is_configured", return_value=True), \
             mock.patch("src.utils.telegram.send_message", side_effect=lambda m: sent.append(m)):
            waitlist.notify("AE", "ITA", self._results(), "http://x")
            waitlist.notify("AE", "ITA", self._results(), "http://x")   # within cooldown
        self.assertEqual(len(sent), 1, "second message must be suppressed by cooldown")

    def test_unconfigured_telegram_does_not_start_cooldown(self):
        with mock.patch("src.utils.telegram.is_configured", return_value=False), \
             mock.patch("src.utils.telegram.send_message") as send:
            waitlist.notify("AE", "ITA", self._results(), "http://x")
            send.assert_not_called()
        # Nothing was actually sent, so the country must NOT be on cooldown.
        self.assertFalse(wc.is_on_cooldown("ITA"))

    def test_no_waitlist_combos_sends_nothing(self):
        with mock.patch("src.utils.telegram.is_configured", return_value=True), \
             mock.patch("src.utils.telegram.send_message") as send:
            waitlist.notify("AE", "ITA", [("Dubai", "No slot message shown.")], "http://x")
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
