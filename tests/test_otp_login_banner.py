"""On an OTP route, VFS can bounce Sign In back to the login page with a
'not registered' / 'wrong password' banner INSTEAD of showing the OTP field.
verify_otp must classify that banner (skip / stop) rather than waiting out the
60s OTP timeout and mislabeling it as a retryable OtpVerificationError.

Regression for the field case: osama@travnook.com not registered for the Italy
portal -> screenshot 'otp_field_missing' -> retryable, wrong. Should be a
neutral EmailNotRegisteredError (supervisor SKIPs the account).
"""

import unittest
from unittest import mock

from src.vfs_bot import otp_flow
from src.vfs_bot.errors import (
    EmailNotRegisteredError,
    InvalidCredentialsError,
    OtpVerificationError,
)


class _NoField:
    """A locator that never resolves to a visible OTP input."""
    def count(self):
        return 0

    def is_visible(self):
        return False

    @property
    def first(self):
        return self


class _Page:
    url = "https://visa.vfsglobal.com/are/en/ita/login"  # never /dashboard

    def locator(self, *_a, **_k):
        return _NoField()

    def wait_for_timeout(self, *_a, **_k):
        pass

    def screenshot(self, *_a, **_k):
        pass


def _run(is_not_registered=False, is_invalid=False):
    page = _Page()
    with mock.patch.object(otp_flow.block_detection, "raise_if_blocked"), \
         mock.patch.object(otp_flow.block_detection, "is_email_not_registered",
                           return_value=is_not_registered), \
         mock.patch.object(otp_flow.block_detection, "is_invalid_credentials",
                           return_value=is_invalid), \
         mock.patch.object(otp_flow.block_detection, "landing_status",
                           return_value="login page"), \
         mock.patch.object(otp_flow.turnstile, "dismiss_captcha"), \
         mock.patch.object(otp_flow.diagnostics, "take_final_screenshot"):
        otp_flow.verify_otp(page, "input#otp", "osama@travnook.com", "pw", 0.0)


class TestOtpLoginBanner(unittest.TestCase):
    def test_not_registered_banner_raises_skip(self):
        with self.assertRaises(EmailNotRegisteredError):
            _run(is_not_registered=True)

    def test_invalid_credentials_banner_raises_stop(self):
        with self.assertRaises(InvalidCredentialsError):
            _run(is_invalid=True)

    def test_no_banner_still_times_out_as_otp_error(self):
        # With neither banner, the old behaviour is preserved: the OTP field
        # never appears -> OtpVerificationError (the generic timeout).
        with self.assertRaises(OtpVerificationError):
            _run()


if __name__ == "__main__":
    unittest.main()
