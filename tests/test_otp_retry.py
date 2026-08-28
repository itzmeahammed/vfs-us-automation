"""Unit tests for the OTP image-read retry (no network / no OpenAI call).

Proves a transient OCR misread (e.g. a 7-digit answer for a 6-digit OTP) is
recovered in-place by a second read, instead of raising and forcing a browser
relaunch — that expected_len is passed through to the reader, and that retries
vary the prompt style while feeding already-rejected codes back to the model.

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

        def fake(image, mime="image/png", expected_len=None, temperature=0.0,
                 rejected=None, style=0):
            self.calls.append({"expected_len": expected_len,
                               "temperature": temperature,
                               "rejected": set(rejected or []), "style": style})
            return next(seq)

        otp_service.otp_openai.read_otp_image = fake

    def test_recovers_on_second_read(self):
        # First read misreads 7 digits; second read gets the real 6-digit code.
        self._patch(["4142127", "414212"])
        code = otp_service.extract_code(_fake_mail(), otp_len=6, read_attempts=3)
        self.assertEqual(code, "414212")
        self.assertEqual(len(self.calls), 2, "should stop as soon as it succeeds")
        # The digit count is handed to the model, and retries vary the PROMPT
        # (style), not the temperature — on a short digit read the argmax barely
        # moves with temperature, so the old ladder just repeated the misread.
        self.assertEqual(self.calls[0]["expected_len"], 6)
        self.assertEqual(self.calls[0]["temperature"], 0.0)
        self.assertEqual(self.calls[1]["temperature"], 0.0)
        self.assertNotEqual(self.calls[0]["style"], self.calls[1]["style"])

    def test_gives_up_after_all_attempts(self):
        self._patch(["4142127", "9999999", "1234567"])
        with self.assertRaises(otp_service.OtpError):
            otp_service.extract_code(_fake_mail(), otp_len=6, read_attempts=3)
        self.assertEqual(len(self.calls), 3)

    def test_excludes_already_rejected_code(self):
        # First image read returns a code VFS already rejected; the retry must
        # skip it and return the next, different reading.
        self._patch(["596916", "596915"])
        code = otp_service.extract_code(
            _fake_mail(), otp_len=6, read_attempts=3, exclude={"596916"}
        )
        self.assertEqual(code, "596915")
        self.assertEqual(len(self.calls), 2)
        # The rejected code is NAMED in the prompt, so the model is told which
        # reading was wrong instead of being re-asked the identical question.
        self.assertIn("596916", self.calls[0]["rejected"])

    def test_body_code_skips_openai(self):
        self._patch(["should-not-be-called"])
        code = otp_service.extract_code(
            _fake_mail(body="Your OTP is 246813 thanks"), otp_len=6, read_attempts=3
        )
        self.assertEqual(code, "246813")
        self.assertEqual(len(self.calls), 0, "body code must not call OpenAI")


if __name__ == "__main__":
    unittest.main()
