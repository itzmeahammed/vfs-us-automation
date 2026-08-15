"""Tests for the login/slot-check split on VfsBot.

`login()` used to do three unrelated things: authenticate, click Start New
Booking, and run the full slot check over every configured combination. That
forced any caller who only needed an authenticated session to pay for the other
two — which the waitlist runner did, wasting ~20s per unwanted combination,
firing a Telegram notice nobody asked for, and leaving the dropdowns on the wrong
combo so it re-selected them immediately afterwards.

These tests pin down both halves of the fix:
  * login() still does exactly what it always did (the slot checker must not
    change behaviour at all), and
  * the phases are independently callable, so a caller can stop after
    authentication.

Run: python -m unittest tests.test_vfs_bot_phases
"""

import unittest
from unittest import mock

from src.vfs_bot.vfs_bot import VfsBot


class _Bot(VfsBot):
    """Concrete VfsBot (the class is ABC) with the page work stubbed out."""

    def __init__(self):
        super().__init__()
        self.source_country_code = "AE"
        self.destination_country_code = "CHE"
        self.calls = []


class LoginCompositionTests(unittest.TestCase):
    """login() must remain exactly authenticate() + start_slot_check()."""

    def setUp(self):
        self.bot = _Bot()

    def test_login_calls_both_phases_in_order(self):
        with mock.patch.object(_Bot, "authenticate") as auth, \
                mock.patch.object(_Bot, "start_slot_check") as check:
            manager = mock.Mock()
            manager.attach_mock(auth, "authenticate")
            manager.attach_mock(check, "start_slot_check")
            self.bot.login(mock.Mock(), "e@x.com", "pw")

        self.assertEqual([c[0] for c in manager.mock_calls],
                         ["authenticate", "start_slot_check"])

    def test_login_passes_the_credential_through(self):
        page = mock.Mock()
        with mock.patch.object(_Bot, "authenticate") as auth, \
                mock.patch.object(_Bot, "start_slot_check"):
            self.bot.login(page, "e@x.com", "pw")
        auth.assert_called_once_with(page, "e@x.com", "pw")

    def test_signature_is_unchanged(self):
        # The supervisor and main.py both call login(page, email, password);
        # changing this signature would break them silently.
        import inspect
        params = list(inspect.signature(VfsBot.login).parameters)
        self.assertEqual(params, ["self", "page", "email_id", "password"])


class PhaseIndependenceTests(unittest.TestCase):
    """Each phase must be usable on its own — that is the whole point."""

    def setUp(self):
        self.bot = _Bot()

    def test_authenticate_does_not_start_booking(self):
        # The waitlist runner relies on this: authenticate() must leave the
        # browser on the dashboard, having checked nothing.
        page = mock.Mock()
        with mock.patch.object(_Bot, "_wait_for_login_form"), \
                mock.patch.object(_Bot, "pre_login_steps"), \
                mock.patch.object(_Bot, "_pass_turnstile"), \
                mock.patch.object(_Bot, "_fill_credentials"), \
                mock.patch.object(_Bot, "_click_sign_in", return_value=0), \
                mock.patch.object(_Bot, "_check_blocked"), \
                mock.patch.object(_Bot, "_raise_known_login_errors"), \
                mock.patch("src.vfs_bot.vfs_bot.diagnostics"), \
                mock.patch("src.vfs_bot.vfs_bot.turnstile") as turnstile, \
                mock.patch("src.vfs_bot.vfs_bot.slot_check") as slot_check:
            turnstile.await_dashboard_handling_captcha.return_value = True
            self.bot.authenticate(page, "e@x.com", "pw")

        slot_check.start_new_booking.assert_not_called()
        slot_check.run_slot_check.assert_not_called()

    def test_start_booking_does_not_run_the_slot_check(self):
        with mock.patch("src.vfs_bot.vfs_bot.slot_check") as slot_check:
            self.bot.start_booking(mock.Mock())
        slot_check.start_new_booking.assert_called_once()
        slot_check.run_slot_check.assert_not_called()

    def test_start_slot_check_does_both(self):
        with mock.patch("src.vfs_bot.vfs_bot.slot_check") as slot_check:
            slot_check.run_slot_check.return_value = [("combo", "WAITLIST")]
            results = self.bot.start_slot_check(mock.Mock())
        slot_check.start_new_booking.assert_called_once()
        slot_check.run_slot_check.assert_called_once()
        self.assertEqual(results, [("combo", "WAITLIST")])
        self.assertEqual(self.bot.slot_results, [("combo", "WAITLIST")])


class AwaitPageTests(unittest.TestCase):
    """run_slot_check() waits for Appointment Details itself. A caller that
    skips it must be able to ask for the same wait, or it would drive the
    dropdowns before the page exists."""

    def setUp(self):
        self.bot = _Bot()

    def test_default_does_not_wait(self):
        # The slot-check path is unchanged: run_slot_check does its own wait.
        page = mock.Mock()
        with mock.patch("src.vfs_bot.vfs_bot.slot_check"):
            self.bot.start_booking(page)
        page.wait_for_url.assert_not_called()

    def test_await_page_waits_for_the_appointment_url(self):
        page = mock.Mock()
        with mock.patch("src.vfs_bot.vfs_bot.slot_check"), \
                mock.patch("src.vfs_bot.vfs_bot.turnstile"):
            self.bot.start_booking(page, await_page=True)
        page.wait_for_url.assert_called_once()
        self.assertIn("application-detail", page.wait_for_url.call_args[0][0])

    def test_await_page_raises_when_the_page_never_arrives(self):
        from src.vfs_bot.errors import SlotCheckError

        page = mock.Mock()
        page.wait_for_url.side_effect = TimeoutError("never arrived")
        with mock.patch("src.vfs_bot.vfs_bot.slot_check"), \
                mock.patch("src.vfs_bot.vfs_bot.turnstile"):
            with self.assertRaises(SlotCheckError):
                self.bot.start_booking(page, await_page=True)


if __name__ == "__main__":
    unittest.main()
