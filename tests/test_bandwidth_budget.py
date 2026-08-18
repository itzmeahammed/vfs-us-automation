"""Unit tests for the daily metered-proxy budget (cap + early warning).

Covers the parts that are easy to get subtly wrong and expensive to get wrong in
production: the day rollover, alerting exactly once per threshold per day across
process restarts, and failing OPEN when the state file is unreadable (a broken
counter must never read as 'cap spent' and silently stop every route).

Run: python -m unittest tests.test_bandwidth_budget
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

import src.settings as s
from src.settings import Bandwidth
from src.utils import bandwidth_budget as bb


class _BudgetCase(unittest.TestCase):
    """Runs each test in its own temp cwd so the real bandwidth_budget.json is
    never touched, with the alert transport stubbed out."""

    CAP = 800
    WARN = 60

    def setUp(self):
        # Build settings BEFORE chdir: Settings() resolves config/config.ini
        # relative to the cwd, so it has to happen from the project root.
        from src.utils.config_reader import initialize_config
        initialize_config()
        self._orig = s._cached
        s._cached = s.Settings(bandwidth=Bandwidth(
            daily_cap_mb=self.CAP, warn_at_percent=self.WARN))
        self.addCleanup(lambda: setattr(s, "_cached", self._orig))

        # The ledger is a relative path, so an isolated cwd keeps the real
        # bandwidth_budget.json untouched.
        self._cwd = os.getcwd()
        self._tmp = tempfile.mkdtemp(prefix="bw-budget-test-")
        os.chdir(self._tmp)
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.addCleanup(os.chdir, self._cwd)

        # Capture alerts instead of sending them.
        self.sent = []
        p = mock.patch.object(bb, "_notify",
                              side_effect=lambda msg, level=None: self.sent.append(msg))
        p.start()
        self.addCleanup(p.stop)

    def _write_state(self, **kw):
        state = {"date": date.today().isoformat(), "used_mb": 0.0,
                 "warned": False, "capped": False}
        state.update(kw)
        with open(bb.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)


class TestAccounting(_BudgetCase):
    def test_starts_empty(self):
        self.assertEqual(bb.used_mb(), 0.0)
        self.assertEqual(bb.remaining_mb(), 800.0)
        self.assertFalse(bb.is_exhausted())

    def test_record_accumulates_across_processes(self):
        # Each scheduled run is its own process, so the total must come off disk.
        bb.record(100.0)
        bb.record(50.5)
        self.assertAlmostEqual(bb.used_mb(), 150.5, places=3)
        self.assertAlmostEqual(bb.remaining_mb(), 649.5, places=3)
        self.assertAlmostEqual(bb.percent_used(), 100 * 150.5 / 800, places=3)

    def test_ignores_zero_negative_and_garbage(self):
        # A negative delta would mean session_mb() went backwards; never trust it
        # enough to hand back budget that was already spent.
        bb.record(10.0)
        for bad in (0, -5, None, "abc"):
            bb.record(bad)
        self.assertAlmostEqual(bb.used_mb(), 10.0, places=3)

    def test_exhausted_at_and_above_cap(self):
        bb.record(799.9)
        self.assertFalse(bb.is_exhausted())
        bb.record(0.1)
        self.assertTrue(bb.is_exhausted(), "reaching the cap exactly must count")
        self.assertEqual(bb.remaining_mb(), 0.0)


class TestRollover(_BudgetCase):
    def test_yesterdays_file_rolls_over(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self._write_state(date=yesterday, used_mb=795.0, warned=True, capped=True)
        # A new day starts clean, with both alert flags armed again — no cron job.
        self.assertEqual(bb.used_mb(), 0.0)
        self.assertFalse(bb.is_exhausted())
        bb.record(500.0)
        self.assertEqual(len(self.sent), 1, "the new day must be able to warn again")


class TestThresholds(_BudgetCase):
    def test_warns_once_when_crossing_60_percent(self):
        bb.record(479.0)                       # 59.9% — just under
        self.assertEqual(self.sent, [])
        bb.record(1.0)                         # 480.0 == 60%
        self.assertEqual(len(self.sent), 1)
        self.assertIn("60%", self.sent[0])
        bb.record(50.0)                        # still under the cap
        self.assertEqual(len(self.sent), 1, "the warning must fire only once a day")

    def test_warning_survives_a_process_restart(self):
        bb.record(500.0)
        self.assertEqual(len(self.sent), 1)
        # Simulate the next scheduled run: fresh module state, same file on disk.
        bb.record(10.0)
        self.assertEqual(len(self.sent), 1, "'already warned' must persist on disk")

    def test_cap_notice_once(self):
        bb.record(800.0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("cap reached", self.sent[0].lower())
        bb.record(25.0)
        self.assertEqual(len(self.sent), 1, "the cap notice must fire only once a day")

    def test_single_jump_past_both_reports_the_cap_only(self):
        # One catastrophic route shouldn't report 'you are at 60%' when it is
        # actually over the cap.
        bb.record(900.0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("cap reached", self.sent[0].lower())
        self.assertTrue(bb.snapshot()["warned"], "the warning is moot once capped")


class TestDisabled(_BudgetCase):
    def test_cap_zero_disables_everything(self):
        s._cached = s.Settings(bandwidth=Bandwidth(daily_cap_mb=0, warn_at_percent=60))
        bb.record(5000.0)
        self.assertFalse(bb.is_exhausted())
        self.assertEqual(bb.remaining_mb(), float("inf"))
        self.assertEqual(bb.percent_used(), 0.0)
        self.assertEqual(self.sent, [], "no cap -> nothing to warn about")

    def test_warn_zero_keeps_the_cap(self):
        s._cached = s.Settings(bandwidth=Bandwidth(daily_cap_mb=800, warn_at_percent=0))
        bb.record(700.0)
        self.assertEqual(self.sent, [], "warning disabled")
        bb.record(100.0)
        self.assertTrue(bb.is_exhausted())
        self.assertEqual(len(self.sent), 1, "the cap notice is independent")

    def test_warn_above_100_is_clamped(self):
        # A threshold past the cap could never fire, silently losing the warning.
        s._cached = s.Settings(bandwidth=Bandwidth(daily_cap_mb=800, warn_at_percent=150))
        self.assertEqual(bb.warn_percent(), 100.0)


class TestFailsOpen(_BudgetCase):
    def test_corrupt_state_does_not_stop_runs(self):
        with open(bb.STATE_FILE, "w", encoding="utf-8") as f:
            f.write("{not json at all")
        self.assertEqual(bb.used_mb(), 0.0)
        self.assertFalse(bb.is_exhausted(),
                         "a corrupt counter must never read as 'cap spent'")

    def test_nonsense_used_value_is_ignored(self):
        self._write_state(used_mb="lots")
        self.assertEqual(bb.used_mb(), 0.0)
        self.assertFalse(bb.is_exhausted())

    def test_alert_failure_never_breaks_a_run(self):
        # The real _notify, with Telegram blowing up underneath it.
        mock.patch.stopall()
        with mock.patch("src.utils.telegram.is_error_configured",
                        side_effect=RuntimeError("boom")):
            bb.record(900.0)          # must not raise
        self.assertTrue(bb.is_exhausted(), "the counter still advanced")


class TestReset(_BudgetCase):
    def test_reset_clears_counter_and_flags(self):
        bb.record(900.0)
        self.assertTrue(bb.is_exhausted())
        bb.reset()
        self.assertEqual(bb.used_mb(), 0.0)
        self.assertFalse(bb.is_exhausted())
        self.assertFalse(bb.snapshot()["warned"])
        self.assertFalse(bb.snapshot()["capped"])


class TestSupervisorIntegration(_BudgetCase):
    """The hook that actually saves money: routes must stop being STARTED once
    the cap is spent, and stop as PAUSED rather than FAILED so no account is
    struck and the run still counts as successful."""

    CAP = 10          # tiny cap: trips after two 4 MB routes
    WARN = 60

    def _run_routes(self, n_routes=4, mb_per_route=4.0):
        from src import supervisor

        routes = [("AE", d) for d in ("ITA", "DEU", "HUN", "CZE")][:n_routes]
        spent = {"mb": 0.0}
        started = []

        def _fake_run(source, dest, **kw):
            started.append(f"{source}-{dest}")
            spent["mb"] += mb_per_route      # the route's billed bytes
            return supervisor._outcome(source, dest, "OK", 1)

        with mock.patch.object(supervisor, "_all_routes", return_value=routes), \
             mock.patch.object(supervisor, "run", side_effect=_fake_run), \
             mock.patch.object(supervisor, "_send_run_summary") as summary, \
             mock.patch.object(supervisor.connectivity, "internet_available",
                               return_value=True), \
             mock.patch("src.utils.credentials.warn_unknown_routes"), \
             mock.patch("src.utils.proxy_forwarder.session_mb",
                        side_effect=lambda: spent["mb"]):
            ok = supervisor.run_all_routes()
        sent = summary.call_args[0][0] if summary.call_args else None
        return started, sent, ok

    def test_routes_pause_once_the_cap_is_spent(self):
        started, outcomes, ok = self._run_routes()

        # 4 MB + 4 MB = 8 < 10, so route 3 starts and takes it to 12; route 4 is
        # then refused. The route in flight is never killed mid-run.
        self.assertEqual(started, ["AE-ITA", "AE-DEU", "AE-HUN"])
        self.assertEqual(len(outcomes), 4, "paused routes still appear in the summary")
        self.assertEqual([o["status"] for o in outcomes],
                         ["OK", "OK", "OK", "PAUSED"])
        self.assertTrue(all(o["ok"] for o in outcomes),
                        "a budget pause is not a failure — no account is struck")
        self.assertTrue(ok, "the run itself succeeded")
        self.assertAlmostEqual(bb.used_mb(), 12.0, places=3)

    def test_later_run_the_same_day_exits_silently(self):
        self._run_routes()
        self.assertTrue(bb.is_exhausted())

        started, outcomes, ok = self._run_routes()
        self.assertEqual(started, [], "no route may start once the cap is spent")
        self.assertTrue(ok)
        # No summary: the cap notice already went out once, and ~20 more runs
        # today would each push an identical 'everything paused' message.
        self.assertIsNone(outcomes, "a no-op run must not spam the summary chat")

    def test_the_run_that_spends_the_cap_still_reports(self):
        _started, outcomes, _ok = self._run_routes()
        self.assertIsNotNone(outcomes, "the run that did real work still reports")
        self.assertIn("PAUSED", [o["status"] for o in outcomes])

    def test_no_cap_runs_everything(self):
        s._cached = s.Settings(bandwidth=Bandwidth(daily_cap_mb=0, warn_at_percent=60))
        started, outcomes, ok = self._run_routes()
        self.assertEqual(len(started), 4)
        self.assertTrue(all(o["status"] == "OK" for o in outcomes))


class TestMessages(unittest.TestCase):
    """The alert text is the whole point of the feature — it has to carry the
    numbers and say what happens next."""

    def test_warning_names_usage_and_consequence(self):
        from src.utils import telegram_message
        msg = telegram_message.bandwidth_warning(480.0, 800.0, 60.0)
        self.assertIn("60%", msg)
        self.assertIn("480", msg)
        self.assertIn("800", msg)
        self.assertIn("320", msg)                  # remaining
        self.assertIn("pause", msg.lower())

    def test_cap_message_says_how_to_resume(self):
        from src.utils import telegram_message
        msg = telegram_message.bandwidth_cap_reached(812.0, 800.0)
        self.assertIn("812", msg)
        self.assertIn("PAUSED", msg)
        self.assertIn("midnight", msg)
        self.assertIn("bandwidth_budget reset", msg)


if __name__ == "__main__":
    unittest.main()
