"""The waitlist's share of the SLOT-CHECKER's safety systems.

The waitlist drives the same VFS accounts and the same metered proxy as the slot
checker, so it must honour the same two protections. It originally honoured
neither: it could sign in with an account the circuit breaker had benched, and it
spent proxy bytes that were never billed to the daily cap (so the checker's own
accounting under-reported by whatever waitlisting used).

These tests pin that integration. Every one of them monkeypatches the shared
state files, so nothing here touches the real account_health.json or
bandwidth_budget.json.
"""

import unittest
from unittest import mock

from src.utils.config_reader import initialize_config
from src.waitlist import runner
from src.waitlist.errors import WaitlistConfigError

initialize_config()

EMAIL = "safety-test@example.com"
ROUTE = "AE-NLD"


class AccountHealthGateTests(unittest.TestCase):
    """_assert_account_healthy refuses to log in with an unsafe account."""

    def test_healthy_account_is_allowed(self):
        with mock.patch("src.utils.account_health.is_disabled", return_value=False), \
             mock.patch("src.utils.account_health.is_benched", return_value=False):
            runner._assert_account_healthy(EMAIL, ROUTE)   # must not raise

    def test_benched_account_is_refused(self):
        import time
        with mock.patch("src.utils.account_health.is_disabled", return_value=False), \
             mock.patch("src.utils.account_health.is_benched", return_value=True), \
             mock.patch("src.utils.account_health.benched_until",
                        return_value=time.time() + 3600):
            with self.assertRaises(WaitlistConfigError) as ctx:
                runner._assert_account_healthy(EMAIL, ROUTE)
        # The message must say what to do, not just that it failed.
        self.assertIn("benched", str(ctx.exception).lower())
        self.assertIn("account_health clear", str(ctx.exception))

    def test_disabled_account_is_refused(self):
        with mock.patch("src.utils.account_health.is_disabled", return_value=True), \
             mock.patch("src.utils.account_health.is_benched", return_value=False):
            with self.assertRaises(WaitlistConfigError) as ctx:
                runner._assert_account_healthy(EMAIL, ROUTE)
        self.assertIn("DISABLED", str(ctx.exception))

    def test_unreadable_health_file_does_not_block_the_run(self):
        """The breaker is a safety net, not a gate of last resort.

        A corrupt or unreadable health file must not strand a registration the
        user explicitly asked for.
        """
        with mock.patch("src.utils.account_health.is_disabled",
                        side_effect=OSError("boom")):
            runner._assert_account_healthy(EMAIL, ROUTE)   # must not raise


class AccountOutcomeTests(unittest.TestCase):
    """_record_account_outcome feeds results back to the shared breaker."""

    def test_success_is_recorded(self):
        with mock.patch("src.utils.account_health.record_success") as ok, \
             mock.patch("src.utils.account_health.record_failure") as bad:
            runner._record_account_outcome(EMAIL, ROUTE, ok=True)
        ok.assert_called_once_with(EMAIL, ROUTE)
        bad.assert_not_called()

    def test_failure_is_recorded_with_a_reason(self):
        with mock.patch("src.utils.account_health.record_success") as ok, \
             mock.patch("src.utils.account_health.record_failure") as bad:
            runner._record_account_outcome(EMAIL, ROUTE, ok=False, reason="login died")
        bad.assert_called_once()
        self.assertEqual(bad.call_args[0][0], EMAIL)
        self.assertEqual(bad.call_args[0][1], ROUTE)
        self.assertIn("login died", bad.call_args[0][2])
        ok.assert_not_called()

    def test_bookkeeping_failure_never_propagates(self):
        """The run's outcome is already journalled by this point."""
        with mock.patch("src.utils.account_health.record_success",
                        side_effect=OSError("disk full")):
            runner._record_account_outcome(EMAIL, ROUTE, ok=True)   # must not raise


class BandwidthBudgetTests(unittest.TestCase):
    """The waitlist bills the SAME daily ledger as the slot checker."""

    def test_exhausted_budget_refuses_to_start(self):
        with mock.patch("src.utils.bandwidth_budget.is_exhausted", return_value=True), \
             mock.patch("src.utils.bandwidth_budget.used_mb", return_value=820.0), \
             mock.patch("src.utils.bandwidth_budget.cap_mb", return_value=800.0):
            with self.assertRaises(WaitlistConfigError) as ctx:
                runner._check_budget()
        self.assertIn("cap spent", str(ctx.exception))

    def test_available_budget_allows_the_run(self):
        with mock.patch("src.utils.bandwidth_budget.is_exhausted", return_value=False):
            runner._check_budget()   # must not raise

    def test_proxied_run_is_billed(self):
        with mock.patch("src.utils.proxy_forwarder.session_mb", return_value=12.5), \
             mock.patch("src.utils.bandwidth_budget.record", return_value=12.5) as rec, \
             mock.patch("src.utils.bandwidth_budget.cap_mb", return_value=800.0), \
             mock.patch("src.utils.bandwidth_budget.percent_used", return_value=1.5), \
             mock.patch("src.utils.bandwidth_budget.remaining_mb", return_value=787.5):
            runner._record_usage("http://user:pass@host:1234")
        rec.assert_called_once_with(12.5)

    def test_local_ip_run_is_not_billed(self):
        """No metered upstream means nothing to charge."""
        with mock.patch("src.utils.bandwidth_budget.record") as rec:
            runner._record_usage(None)
        rec.assert_not_called()

    def test_zero_byte_run_does_not_rewrite_the_ledger(self):
        with mock.patch("src.utils.proxy_forwarder.session_mb", return_value=0.0), \
             mock.patch("src.utils.bandwidth_budget.record") as rec:
            runner._record_usage("http://user:pass@host:1234")
        rec.assert_not_called()

    def test_metering_failure_never_masks_the_run(self):
        with mock.patch("src.utils.proxy_forwarder.session_mb",
                        side_effect=RuntimeError("meter died")):
            runner._record_usage("http://user:pass@host:1234")   # must not raise


if __name__ == "__main__":
    unittest.main()
