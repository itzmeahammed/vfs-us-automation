"""Unit tests for the OTP vision reader and how its failures are charged.

VFS's OTP email is a deliberate anti-OCR captcha: three rows of digits, only one
of which the green 'OTP' arrow points at, with recoloured glyphs, decorative
curves, speckle noise and a translucent panel over part of it. On 2026-08-18 the
reader returned the SAME rejected code three times in a row, because every retry
sent a byte-identical prompt and only nudged temperature. These tests pin the
fixes: the model is told what was rejected, retries vary the prompt, the answer
survives a transcription pass, and a reader miss never strikes the account.

Run: python -m unittest tests.test_otp_reader
"""

import unittest
from unittest import mock

from src.utils import otp_openai, otp_service


class TestPromptTellsTheModelWhatWasRejected(unittest.TestCase):
    def test_rejected_codes_appear_in_the_prompt(self):
        p = otp_openai.build_prompt(expected_len=6, rejected={"337713"})
        self.assertIn("337713", p)
        self.assertIn("rejected", p.lower())
        self.assertIn("Do not return them again", p)

    def test_several_rejected_codes_all_named(self):
        p = otp_openai.build_prompt(expected_len=6, rejected=["337713", "913469"])
        self.assertIn("337713", p)
        self.assertIn("913469", p)

    def test_clean_prompt_has_no_rejection_block(self):
        p = otp_openai.build_prompt(expected_len=6)
        self.assertNotIn("rejected them as incorrect", p)

    def test_prompt_describes_the_actual_captcha(self):
        # The old prompt mentioned only the ribbon and let the model read a
        # decoy row. These are the traps in the real image.
        p = otp_openai.build_prompt(expected_len=6).lower()
        for needle in ("three", "decoy", "arrow", "colour", "translucent"):
            self.assertIn(needle, p, f"prompt should warn about: {needle}")

    def test_expected_length_is_stated(self):
        self.assertIn("exactly 6 digits", otp_openai.build_prompt(expected_len=6))


class TestRetriesVaryThePrompt(unittest.TestCase):
    def test_each_style_is_distinct(self):
        seen = {otp_openai.build_prompt(6, style=i) for i in range(3)}
        self.assertEqual(len(seen), 3, "each retry must ask a different question")

    def test_style_index_wraps(self):
        self.assertEqual(otp_openai.build_prompt(6, style=0),
                         otp_openai.build_prompt(6, style=3))


class TestTranscriptionParsing(unittest.TestCase):
    """The model now shows its working before answering, so the reply holds
    several digit runs. The answer must come from the OTP: line."""

    def test_answer_taken_from_otp_line(self):
        reply = "DIGITS: 3 9 7 1 9 9\nOTP: 397199"
        self.assertEqual(otp_openai._answer_from(reply, 6), "397199")

    def test_working_line_does_not_win(self):
        # A naive 'first run of 6 digits' would grab 208420 off the decoy note.
        reply = ("The decoy rows read 208420 and 098484.\n"
                 "DIGITS: 3 9 7 1 9 9\nOTP: 397199")
        self.assertEqual(otp_openai._answer_from(reply, 6), "397199")

    def test_spaces_in_the_answer_are_stripped(self):
        self.assertEqual(otp_openai._answer_from("OTP: 3 9 7 1 9 9", 6), "397199")

    def test_falls_back_to_last_run_without_an_otp_line(self):
        self.assertEqual(otp_openai._answer_from("I think it is 397199", 6), "397199")

    def test_no_digits_returns_text(self):
        self.assertEqual(otp_openai._answer_from("cannot read", 6), "cannot read")


class _Mail:
    body_text = "no code in the body"
    image = b"\x89PNG-fake"
    image_mime = "image/png"


class TestServicePassesRejectsAndVariesStyle(unittest.TestCase):
    def test_rejected_code_is_forwarded_and_style_advances(self):
        calls = []

        def fake_read(image, mime, expected_len=None, temperature=0.0,
                      rejected=None, style=0):
            calls.append({"rejected": set(rejected or []), "style": style,
                          "temperature": temperature})
            return "337713" if style == 0 else "397199"

        with mock.patch.object(otp_openai, "read_otp_image", side_effect=fake_read):
            code = otp_service.extract_code(_Mail(), 6, read_attempts=3,
                                            exclude={"337713"})
        self.assertEqual(code, "397199")
        # Pass 1 already knows VFS refused 337713...
        self.assertIn("337713", calls[0]["rejected"])
        # ...and pass 2 asks a different question, not a hotter one.
        self.assertEqual([c["style"] for c in calls], [0, 1])
        self.assertTrue(all(c["temperature"] == 0.0 for c in calls),
                        "temperature must not be the retry lever any more")

    def test_a_repeat_answer_is_fed_back_into_the_next_prompt(self):
        seen = []

        def fake_read(image, mime, expected_len=None, temperature=0.0,
                      rejected=None, style=0):
            seen.append(set(rejected or []))
            return "337713"          # the model keeps repeating itself

        with mock.patch.object(otp_openai, "read_otp_image", side_effect=fake_read):
            with self.assertRaises(otp_service.OtpError):
                otp_service.extract_code(_Mail(), 6, read_attempts=3,
                                         exclude={"337713"})
        self.assertEqual(len(seen), 3)
        self.assertTrue(all("337713" in s for s in seen))

    def test_body_text_code_still_short_circuits(self):
        class M(_Mail):
            body_text = "Your OTP is 512157 and expires soon."
        with mock.patch.object(otp_openai, "read_otp_image") as read:
            self.assertEqual(otp_service.extract_code(M(), 6), "512157")
        read.assert_not_called()


class TestReaderMissDoesNotStrikeTheAccount(unittest.TestCase):
    """A miss on a hostile captcha is our OCR failing, not the account."""

    def test_otp_read_error_classifies_as_infra(self):
        from src import supervisor
        from src.vfs_bot.errors import OtpReadError, OtpVerificationError

        self.assertTrue(supervisor._is_infra_error(
            OtpReadError("Could not read the OTP: no usable 6-digit code")))
        # A genuine OTP failure (VFS refused every submitted code) still counts.
        self.assertFalse(supervisor._is_infra_error(
            OtpVerificationError("VFS rejected the OTP on every submit")))

    def test_read_error_is_still_an_otp_error_for_callers(self):
        from src.vfs_bot.errors import OtpReadError, OtpVerificationError
        self.assertTrue(issubclass(OtpReadError, OtpVerificationError))


if __name__ == "__main__":
    unittest.main()
