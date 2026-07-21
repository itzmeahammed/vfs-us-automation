"""Unit tests for the Greece text-based OTP extractor (no AI).

Uses the real email wording and proves: the code is read from the body, from an
HTML-only body, the 'expire in 5 minutes' digit is never mistaken for the code,
and a missing code raises (with NO image/AI fallback).

Run: python -m unittest tests.test_greece_otp
"""

import unittest

from src.utils import greece_otp
from src.utils.otp_email import OtpMail
from src.utils.otp_service import OtpError

REAL_EMAIL = ("The OTP for your application with VFS Global is 512157. "
              "The OTP will expire in 5 minutes.")


def _mail(body="", html="", image=None):
    return OtpMail(body_text=body, image=image, image_mime="", received_epoch=0.0,
                   html_text=html)


class TestGreeceOtp(unittest.TestCase):
    def test_reads_code_from_body(self):
        self.assertEqual(greece_otp.extract_code(_mail(body=REAL_EMAIL), 6), "512157")

    def test_ignores_the_5_minutes_digit(self):
        code = greece_otp.extract_code(_mail(body=REAL_EMAIL), 6)
        self.assertNotEqual(code, "5")
        self.assertEqual(code, "512157")

    def test_reads_code_from_html_when_no_plaintext(self):
        html = "<p>The OTP for your application with VFS Global is <b>408991</b>.</p>"
        self.assertEqual(greece_otp.extract_code(_mail(html=html), 6), "408991")

    def test_missing_code_raises_no_ai_fallback(self):
        # Has an image, but text mode must NOT fall back to it — it raises instead.
        m = _mail(body="No code in this text.", image=b"\x89PNG-not-read")
        with self.assertRaises(OtpError):
            greece_otp.extract_code(m, 6)

    def test_respects_otp_length(self):
        m = _mail(body="Your code is 12345 today.")   # 5 digits, expecting 6
        with self.assertRaises(OtpError):
            greece_otp.extract_code(m, 6)
        self.assertEqual(greece_otp.extract_code(m, 5), "12345")


if __name__ == "__main__":
    unittest.main()
