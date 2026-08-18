"""Sign In that silently bounces back to the login form must be recognised as
a LOGIN failure, fast — never as a missing OTP field.

The field failure this pins down (2026-08-17): on AE-ITA and AE-GRC the login
POST was refused and VFS re-showed the sign-in form (fields still filled,
Turnstile 'Success!', Sign In re-enabled, no error banner). Nothing looked for
that state, so the flow waited out the 60s OTP poll and raised
OtpVerificationError — which the supervisor answers with NO retry and an
account strike. Two healthy accounts (binthya@, athul@) were struck for a
Cloudflare rejection. The same bounce on non-OTP routes (DEU) burned the full
90s dashboard poll before reporting DashboardNotReachedError.

Run: python -m unittest tests.test_login_bounce
"""

import unittest
from unittest import mock

from src import supervisor
from src.vfs_bot import otp_flow, turnstile
from src.vfs_bot.errors import (
    IpBlockedError,
    LoginBouncedError,
    OtpVerificationError,
    SignInDisabledError,
    TurnstileRejectedError,
)

LOGIN_URL = "https://visa.vfsglobal.com/are/en/ita/login"


class _Loc:
    """Minimal locator stub: `n` controls count(), the flags the rest."""

    def __init__(self, n=0, visible=True, enabled=True):
        self._n, self._visible, self._enabled = n, visible, enabled

    @property
    def first(self):
        return self

    def filter(self, **kwargs):
        if kwargs.get("visible") and not self._visible:
            return _Loc(0)
        return self

    def count(self):
        return self._n

    def is_visible(self):
        return self._n > 0 and self._visible

    def is_enabled(self):
        return self._enabled


class _Page:
    """A page in one of the states the post-Sign-In wait must tell apart.

    Defaults describe the BOUNCE: the login form back on screen, settled and
    usable. Individual tests flip one thing at a time to prove each guard is
    load-bearing.
    """

    def __init__(self, url=LOGIN_URL, password=True, loader=False,
                 signin_enabled=True, otp_field=False, captcha=False):
        self.url = url
        self._password, self._loader = password, loader
        self._signin_enabled, self._otp_field = signin_enabled, otp_field
        self.captcha = captcha
        self.main_frame = self

    def locator(self, selector):
        if "password" in selector:
            return _Loc(1 if self._password else 0)
        if "ngx-ui-loader" in selector:
            return _Loc(1 if self._loader else 0, visible=self._loader)
        if "otp" in selector.lower():
            return _Loc(1 if self._otp_field else 0)
        return _Loc(0)

    def get_by_role(self, role, name=None, **kwargs):
        if name == "Sign In":
            return _Loc(1, enabled=self._signin_enabled)
        return _Loc(0)

    def evaluate(self, script):
        if "heading" in script:
            return {"heading": "Sign in",
                    "msg": "Enter your email and password to continue",
                    "url": self.url}
        return "Sign in Enter your email and password to continue"

    def content(self):
        return "<html>Sign in</html>"

    def wait_for_timeout(self, _ms):
        pass

    def screenshot(self, **kwargs):
        pass


class TestLoginFormShowing(unittest.TestCase):
    """Each clause of the predicate rules out a page that is merely in flight —
    a false positive here would abort a run that was about to succeed."""

    def setUp(self):
        patcher = mock.patch.object(turnstile, "captcha_visible",
                                    side_effect=lambda p: p.captcha)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_settled_login_form_is_a_bounce(self):
        self.assertTrue(turnstile.login_form_showing(_Page()))

    def test_submission_in_flight_is_not_a_bounce(self):
        # The exact state of the 'after_signin' screenshot: form still on screen,
        # loader up, Sign In greyed out. Calling this a bounce would abort every
        # healthy login.
        self.assertFalse(turnstile.login_form_showing(
            _Page(loader=True, signin_enabled=False)))

    def test_loader_alone_is_not_a_bounce(self):
        self.assertFalse(turnstile.login_form_showing(_Page(loader=True)))

    def test_disabled_signin_is_not_a_bounce(self):
        self.assertFalse(turnstile.login_form_showing(_Page(signin_enabled=False)))

    def test_captcha_dialog_is_not_a_bounce(self):
        # A challenge is still in progress — the caller solves it, not us.
        self.assertFalse(turnstile.login_form_showing(_Page(captcha=True)))

    def test_page_without_a_password_field_is_not_the_login_form(self):
        # The OTP step and the dashboard both lack one.
        self.assertFalse(turnstile.login_form_showing(_Page(password=False)))


def _verify_otp(page, on_poll=None):
    """Run verify_otp against `page` with the clock and mail fetch neutralised."""
    with mock.patch.object(otp_flow.turnstile, "dismiss_captcha"), \
         mock.patch.object(otp_flow.turnstile, "captcha_visible",
                           side_effect=lambda p: p.captcha), \
         mock.patch.object(otp_flow.diagnostics, "take_final_screenshot"):
        otp_flow.verify_otp(page, "input#otp", "a@b.com", "pw", 0.0,
                            on_poll=on_poll)


class TestOtpWaitClassifiesTheRealFailure(unittest.TestCase):
    def test_bounce_raises_login_bounced_not_otp_error(self):
        with self.assertRaises(LoginBouncedError):
            _verify_otp(_Page())

    def test_login_bounced_is_not_an_otp_error(self):
        # The whole point: OtpVerificationError means "no retry + strike the
        # account", which must never be the answer to a refused login.
        self.assertFalse(issubclass(LoginBouncedError, OtpVerificationError))

    def test_bounce_is_retryable_and_rotates(self):
        # Subclassing TurnstileRejectedError buys the same-IP reload; subclassing
        # SignInDisabledError through it buys the supervisor's IP rotation.
        self.assertTrue(issubclass(LoginBouncedError, TurnstileRejectedError))
        self.assertTrue(issubclass(LoginBouncedError, SignInDisabledError))

    def test_late_403_is_classified_during_the_wait(self):
        # A login-403 that lands after the post-Sign-In check used to sit
        # unexamined for the whole 60s poll. on_poll must surface it.
        def on_poll():
            raise IpBlockedError("VFS 403201 — IP blocked.")

        with self.assertRaises(IpBlockedError):
            _verify_otp(_Page(), on_poll=on_poll)

    def test_otp_field_present_is_not_treated_as_a_bounce(self):
        # The OTP step rendered on a page that still has the login form behind
        # it: the field wins, and verify_otp proceeds to fetch the code.
        page = _Page(otp_field=True)
        with mock.patch.object(otp_flow.turnstile, "dismiss_captcha"), \
             mock.patch.object(otp_flow.turnstile, "captcha_visible",
                               side_effect=lambda p: p.captcha), \
             mock.patch.object(otp_flow.diagnostics, "take_screenshot"), \
             mock.patch.object(otp_flow.diagnostics, "take_final_screenshot"), \
             mock.patch("src.utils.otp_service.wait_for_otp_mail",
                        side_effect=RuntimeError("mailbox unreachable")):
            with self.assertRaises(OtpVerificationError) as ctx:
                otp_flow.verify_otp(page, "input#otp", "a@b.com", "pw", 0.0)
        # Got past the wait loop to the mail fetch — i.e. no bounce was declared.
        self.assertIn("OTP email", str(ctx.exception))

    def test_dashboard_short_circuits_before_any_bounce_check(self):
        page = _Page(url="https://visa.vfsglobal.com/are/en/ita/dashboard")
        _verify_otp(page)      # returns cleanly: no OTP step needed


class _CountingPage(_Page):
    """Counts wait_for_timeout calls so the settle poll's duration is checkable
    without a real clock."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.slept_ms = 0

    def wait_for_timeout(self, ms):
        self.slept_ms += ms


class TestSettleAfterSignIn(unittest.TestCase):
    """The flat 2s sleep this replaces was a race: a proxied /user/login 403 that
    arrived at 2.5s went unclassified, and the run then spent 60-90s blaming the
    next step. Polling must exit the moment the outcome is knowable — which also
    makes the healthy path faster than the old sleep."""

    def _bot(self, otp=False):
        from src.utils.config_reader import initialize_config
        from src.vfs_bot.vfs_bot_factory import get_vfs_bot
        initialize_config()
        bot = get_vfs_bot("AE", "ITA")
        bot.schema = {"otp": True} if otp else {}
        bot.selectors = dict(bot.selectors, otp="input#otp")
        bot._block_responses = []
        return bot

    def _settle(self, bot, page):
        with mock.patch("src.vfs_bot.vfs_bot.turnstile.captcha_visible",
                        side_effect=lambda p: p.captcha):
            bot._settle_after_signin(page)

    def test_exits_immediately_on_a_captured_403(self):
        bot = self._bot()
        bot._block_responses = ["<a 403>"]
        page = _CountingPage()
        self._settle(bot, page)
        self.assertEqual(page.slept_ms, 0, "a captured 403 must end the wait at once")

    def test_exits_once_the_url_changes(self):
        # Detected as "moved off the URL we submitted from", not by matching
        # '/login' — a route that used a different path would otherwise return
        # on the first poll and reopen the race.
        bot = self._bot()

        class _NavigatingPage(_CountingPage):
            def wait_for_timeout(self, ms):
                super().wait_for_timeout(ms)
                self.url = "https://visa.vfsglobal.com/are/en/ita/dashboard"

        page = _NavigatingPage(url="https://vfs.example/some/other/entry")
        self._settle(bot, page)
        self.assertEqual(page.slept_ms, 250, "should exit on the first poll after nav")

    def test_exits_once_the_otp_field_renders(self):
        bot = self._bot(otp=True)
        page = _CountingPage(otp_field=True)
        self._settle(bot, page)
        self.assertEqual(page.slept_ms, 0)

    def test_exits_when_cloudflare_re_challenges(self):
        bot = self._bot()
        page = _CountingPage(captcha=True)
        self._settle(bot, page)
        self.assertEqual(page.slept_ms, 0)

    def test_gives_up_at_the_configured_ceiling(self):
        from src.settings import settings
        bot = self._bot()
        page = _CountingPage()          # nothing ever happens
        self._settle(bot, page)
        self.assertEqual(page.slept_ms, settings().timeouts.signin_settle_ms)

    def test_a_late_403_still_ends_the_wait(self):
        # The exact race that caused the ITA/GRC failures: the 403 lands well
        # after the old 2s window, but inside the poll.
        bot = self._bot()

        class _LatePage(_CountingPage):
            def wait_for_timeout(self, ms):
                super().wait_for_timeout(ms)
                if self.slept_ms >= 2500:
                    bot._block_responses.append("<a late 403>")

        page = _LatePage()
        self._settle(bot, page)
        self.assertTrue(bot._block_responses, "the late 403 must be seen")
        self.assertLess(page.slept_ms, 4000, "and seen promptly, not at the ceiling")


class TestDashboardWaitCatchesTheBounce(unittest.TestCase):
    """Non-OTP routes hit the same bounce (DEU in the field) and used to burn
    the whole 90s dashboard poll before reporting DashboardNotReachedError."""

    def _await(self, page, on_poll=None):
        with mock.patch.object(turnstile, "dismiss_captcha", return_value=False), \
             mock.patch.object(turnstile, "captcha_visible",
                               side_effect=lambda p: p.captcha), \
             mock.patch.object(turnstile.diagnostics, "take_final_screenshot"):
            return turnstile.await_dashboard_handling_captcha(
                page, timeout_ms=90000, on_poll=on_poll)

    def test_bounce_raises_instead_of_polling_for_90s(self):
        page = _CountingPage()
        with self.assertRaises(LoginBouncedError):
            self._await(page)
        self.assertLessEqual(page.slept_ms, 8000,
                             "must bail around the grace period, not at 90s")

    def test_submission_in_flight_is_not_mistaken_for_a_bounce(self):
        # Loader up, Sign In greyed: a healthy login mid-redirect. It must keep
        # polling (and here simply time out) rather than abort the run.
        page = _CountingPage(loader=True, signin_enabled=False)
        self.assertFalse(self._await(page))
        self.assertGreater(page.slept_ms, 8000)

    def test_late_403_is_classified_during_the_dashboard_poll(self):
        def on_poll():
            raise IpBlockedError("VFS 403201 — IP blocked.")

        with self.assertRaises(IpBlockedError):
            self._await(_CountingPage(), on_poll=on_poll)


class TestSessionExpiredRecovery(unittest.TestCase):
    """'Session Expired or Invalid' IS a stale-cookie symptom, so re-requesting
    the same URL with the same cookies re-serves the same page — which is what
    the FRA logs showed. Each recovery pass must change something first."""

    def _bot_with_context(self):
        from src.utils.config_reader import initialize_config
        from src.vfs_bot.vfs_bot_factory import get_vfs_bot
        initialize_config()
        bot = get_vfs_bot("AE", "FRA")
        bot.source_country_code, bot.destination_country_code = "AE", "FRA"
        bot._context = mock.Mock()
        return bot

    def _refresh(self, bot, attempt):
        page = _Page()
        page.goto = mock.Mock()
        with mock.patch("src.vfs_bot.vfs_bot.session.clear_site_session") as clear:
            bot._refresh_login_url(page, attempt=attempt)
        return clear, page

    def test_first_pass_clears_the_vfs_session_cookies(self):
        bot = self._bot_with_context()
        clear, page = self._refresh(bot, attempt=1)
        clear.assert_called_once()
        # keep_cf=None -> honour the config default (keep Cloudflare's clearance,
        # so Turnstile does not have to be re-solved from scratch).
        self.assertIsNone(clear.call_args.kwargs.get("keep_cf"))
        page.goto.assert_called_once()

    def test_second_pass_escalates_and_drops_cf_clearance(self):
        bot = self._bot_with_context()
        clear, _page = self._refresh(bot, attempt=2)
        self.assertIs(clear.call_args.kwargs.get("keep_cf"), False)

    def test_refresh_still_navigates_when_cookies_cannot_be_cleared(self):
        # No context (or a context that throws) must not abort the recovery —
        # a reload with stale cookies still beats no reload at all.
        bot = self._bot_with_context()
        bot._context = None
        page = _Page()
        page.goto = mock.Mock()
        bot._refresh_login_url(page, attempt=1)
        page.goto.assert_called_once()

    def test_clear_site_session_can_be_forced_to_drop_clearance(self):
        from src.vfs_bot import session
        cookies = [{"name": "cf_clearance", "value": "x"},
                   {"name": "__cflb", "value": "y"},
                   {"name": "VFS_SESSION", "value": "z"}]
        ctx = mock.Mock()
        ctx.cookies.return_value = cookies

        session.clear_site_session(ctx, keep_cf=True)
        ctx.add_cookies.assert_called_once()
        self.assertEqual(len(ctx.add_cookies.call_args.args[0]), 2)  # both cf ones

        ctx.reset_mock()
        ctx.cookies.return_value = cookies
        session.clear_site_session(ctx, keep_cf=False)
        ctx.clear_cookies.assert_called_once()
        ctx.add_cookies.assert_not_called()          # nothing restored


class TestSupervisorTreatsBounceAsInfra(unittest.TestCase):
    def test_bounce_never_strikes_the_account(self):
        # _is_infra_error matches on the exact class name, so the subclass needs
        # its own entry — without it a refused login still costs a strike.
        self.assertTrue(supervisor._is_infra_error(LoginBouncedError("bounced")))

    def test_bounce_rotates_the_ip_and_reports_infra(self):
        from src.utils.config_reader import initialize_config
        initialize_config()
        proxies = [(f"http://ip{i}", f"9.9.9.{i}") for i in range(1, 5)]
        with mock.patch.object(supervisor, "run_once_with_fresh_browser",
                               side_effect=LoginBouncedError("bounced")) as roc, \
             mock.patch.object(supervisor.proxy_pool, "pick_for_run",
                               side_effect=proxies), \
             mock.patch.object(supervisor.proxy_pool, "label", side_effect=lambda p: p), \
             mock.patch.object(supervisor, "_alert_failure"), \
             mock.patch.object(supervisor, "time") as fake_time, \
             mock.patch.object(supervisor.account_health, "record_failure",
                               return_value=False) as fail, \
             mock.patch.object(supervisor.account_health, "record_success"):
            fake_time.sleep = lambda *_a, **_k: None
            outcome = supervisor.run("AE", "ITA", force_email="x@y.com",
                                     force_password="pw")
        used = [c.kwargs.get("proxy", c.args[4] if len(c.args) > 4 else None)
                for c in roc.call_args_list]
        self.assertEqual(len(set(used)), len(used), f"must rotate IPs, got {used}")
        fail.assert_not_called()
        self.assertEqual(outcome["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
