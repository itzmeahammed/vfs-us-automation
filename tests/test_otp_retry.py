"""Unit tests for the OTP image-read retry (no network / no OpenAI call).

Proves a transient OCR misread (e.g. a 7-digit answer for a 6-digit OTP) is
recovered in-place by a second read, instead of raising and forcing a browser
relaunch — and that expected_len is passed through to the reader.

Run: python -m unittest tests.test_otp_retry
"""

import types
import unittest

from src.utils import otp_service


def _fake_mail(body="", image=b"img", mime="image/png"):
    return types.SimpleNamespace(body_text=body, image=image, image_mime=mime)


class TestExtractCodeRetry(unittest.TestCase):
    def setUp(self):
        self._orig = otp_service.otp_openai.read_otp_image
        self.calls = []

    def tearDown(self):
        otp_service.otp_openai.read_otp_image = self._orig

    def _patch(self, replies):
        seq = iter(replies)

        def fake(image, mime="image/png", expected_len=None, temperature=0.0):
            self.calls.append({"expected_len": expected_len, "temperature": temperature})
            return next(seq)

        otp_service.otp_openai.read_otp_image = fake

    def test_recovers_on_second_read(self):
        # First read misreads 7 digits; second read gets the real 6-digit code.
        self._patch(["4142127", "414212"])
        code = otp_service._extract_code(_fake_mail(), otp_len=6, read_attempts=3)
        self.assertEqual(code, "414212")
        self.assertEqual(len(self.calls), 2, "should stop as soon as it succeeds")
        # The digit count is handed to the model, and retries vary temperature.
        self.assertEqual(self.calls[0]["expected_len"], 6)
        self.assertEqual(self.calls[0]["temperature"], 0.0)
        self.assertGreater(self.calls[1]["temperature"], 0.0)

    def test_gives_up_after_all_attempts(self):
        self._patch(["4142127", "9999999", "1234567"])
        with self.assertRaises(otp_service.OtpError):
            otp_service._extract_code(_fake_mail(), otp_len=6, read_attempts=3)
        self.assertEqual(len(self.calls), 3)

    def test_body_code_skips_openai(self):
        self._patch(["should-not-be-called"])
        code = otp_service._extract_code(
            _fake_mail(body="Your OTP is 246813 thanks"), otp_len=6, read_attempts=3
        )
        self.assertEqual(code, "246813")
        self.assertEqual(len(self.calls), 0, "body code must not call OpenAI")


if __name__ == "__main__":
    unittest.main()
