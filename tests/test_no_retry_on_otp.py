"""An OtpVerificationError (a REAL OTP failure — field missing / email never
arrived / code rejected) must fail the route on attempt 1 with NO attempt 2:
a fresh browser just burns another OTP email and usually fails the same way.
A Turnstile problem on the OTP form is a different class (TurnstileRejectedError)
and still retries — this test guards that only OTP failures skip the retry.
"""

import unittest
from unittest import mock

from src import supervisor
from src.vfs_bot.errors import LoginFormNotReadyError, OtpVerificationError


def _run_with(exc):
    """Drive supervisor.run() for one forced route where every browser attempt
    raises `exc`; returns (attempt_count, outcome) with all side effects mocked."""
    from src.utils.config_reader import initialize_config
    initialize_config()
    with mock.patch.object(supervisor, "run_once_with_fresh_browser",
                           side_effect=exc) as roc, \
         mock.patch.object(supervisor, "_alert_failure"), \
         mock.patch.object(supervisor, "time") as fake_time, \
         mock.patch.object(supervisor.account_health, "record_failure",
                           return_value=False), \
         mock.patch.object(supervisor.account_health, "record_success"):
        fake_time.sleep = lambda *_a, **_k: None  # no real backoff wait
        outcome = supervisor.run("AE", "FRA", force_email="x@y.com",
                                 force_password="pw", force_proxy="")
    return roc.call_count, outcome


class TestNoRetryOnOtp(unittest.TestCase):
    def test_otp_error_has_no_second_attempt(self):
        attempts, outcome = _run_with(OtpVerificationError("no OTP field"))
        self.assertEqual(attempts, 1)                 # attempt 1 only — no retry
        self.assertEqual(outcome["status"], "FAILED")

    def test_other_retryable_uses_every_attempt(self):
        # A non-OTP retryable (stuck login form) still exhausts the configured
        # budget. Asserted against the setting, not a literal, so retuning
        # [account_safety] max_attempts doesn't break a test about OTP.
        from src.settings import settings

        attempts, outcome = _run_with(LoginFormNotReadyError("stuck"))
        self.assertEqual(attempts, settings().account_safety.max_attempts)
        self.assertGreater(attempts, 1, "non-OTP failures must still retry")
        self.assertEqual(outcome["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
