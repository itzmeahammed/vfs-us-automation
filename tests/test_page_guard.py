"""Unit tests for the always-on session sentinel (src/vfs_bot/page_guard.py).

These reproduce the field failure the guard exists to stop: a run reached the
dashboard, checked one combination, and was then served VFS's 'Permission
Issues (403)' page mid-slot-check. Nothing noticed, so the flow ground ~97s of
dropdown timeouts against a form that no longer existed and reported the route
OK with "no availability".

The guard is a pure function of the page plus a flag, so all of it runs against
a stub exposing only `.url` / `.evaluate()` / `.content()` / `.on()` — no
browser, no network.

Run: python -m unittest tests.test_page_guard
"""

import unittest

from src.vfs_bot import page_guard
from src.vfs_bot.errors import (
    AccountLockedError,
    GeoBlockedError,
    IpBlockedError,
    PageBlockedError,
)

# The live text of the page that killed the AE-HUN run (see screenshots/).
PERMISSION_ISSUES = (
    "Permission Issues (403)\nIt seems like you're encountering a permission issue."
)

APP_URL = "https://visa.vfsglobal.com/are/en/hun/application-detail"
ERROR_URL = "https://visa.vfsglobal.com/are/en/hun/page-not-found"


class _FakePage:
    """Stub page. `body_text` backs every innerText read, `html` the full-source
    scan, `url` the (free) route check. `events` records .on() registrations."""

    def __init__(self, body_text="", html="", url=APP_URL):
        self.body_text = body_text
        self.html = html
        self.url = url
        self.events = {}
        self.main_frame = self

    def evaluate(self, script):
        if "heading" in script:      # landing_status()'s different script
            return {"heading": "", "msg": self.body_text, "url": self.url}
        return self.body_text

    def content(self):
        return self.html

    def on(self, event, handler):
        self.events.setdefault(event, []).append(handler)

    def screenshot(self, **kwargs):
        pass


class _FakeRequest:
    def __init__(self, navigation=True):
        self._navigation = navigation

    def is_navigation_request(self):
        return self._navigation


class _FakeResponse:
    def __init__(self, status, url, navigation=True):
        self.status = status
        self.url = url
        self.request = _FakeRequest(navigation)


class _FakeFrame:
    def __init__(self, url):
        self.url = url


def _install(page):
    page_guard.install(page)
    return page


class TestUrlClassification(unittest.TestCase):
    """The free layer: page.url is locally cached, so this runs on every
    checkpoint and must be both accurate and quiet on healthy pages."""

    def test_healthy_app_url_is_not_an_error(self):
        self.assertEqual(page_guard.error_url_reason(_FakePage(url=APP_URL)), "")

    def test_dashboard_and_login_are_not_errors(self):
        for url in ("https://visa.vfsglobal.com/are/en/hun/dashboard",
                    "https://visa.vfsglobal.com/are/en/hun/login"):
            self.assertEqual(page_guard.error_url_reason(_FakePage(url=url)), "",
                             f"{url} must not read as an error route")

    def test_error_routes_are_flagged(self):
        for path in ("page-not-found", "error", "access-denied", "session-expired"):
            url = f"https://visa.vfsglobal.com/are/en/hun/{path}"
            self.assertTrue(page_guard.error_url_reason(_FakePage(url=url)),
                            f"{url} should read as an error route")

    def test_query_string_cannot_fake_an_error(self):
        # Only the PATH is inspected, so a harmless ?returnUrl=/error is ignored.
        page = _FakePage(url=f"{APP_URL}?returnUrl=%2Ferror")
        self.assertEqual(page_guard.error_url_reason(page), "")


class TestSentinel(unittest.TestCase):
    """Layer 1: the passive listeners only ever SET a flag (Playwright swallows
    exceptions raised inside event handlers, so they must never raise)."""

    def test_install_hooks_navigation_and_response(self):
        page = _install(_FakePage())
        self.assertIn("framenavigated", page.events)
        self.assertIn("response", page.events)

    def test_healthy_page_has_no_suspicion(self):
        page = _install(_FakePage())
        self.assertEqual(page_guard.suspicion(page), "")

    def test_navigation_to_error_route_flags(self):
        page = _install(_FakePage())
        page.events["framenavigated"][0](page.main_frame)   # still on APP_URL
        self.assertEqual(page_guard.suspicion(page), "")
        page.url = ERROR_URL
        page.main_frame = _FakeFrame(ERROR_URL)
        page.events["framenavigated"][0](page.main_frame)
        self.assertIn("page-not-found", page_guard.suspicion(page))

    def test_subframe_navigation_is_ignored(self):
        # Turnstile/tracker iframes navigate constantly and say nothing about
        # whether OUR session is alive.
        page = _install(_FakePage())
        page.events["framenavigated"][0](_FakeFrame(ERROR_URL))
        self.assertEqual(page_guard.suspicion(page), "")

    def test_vfs_document_403_flags(self):
        page = _install(_FakePage())
        page.events["response"][0](_FakeResponse(403, APP_URL))
        self.assertIn("403", page_guard.suspicion(page))

    def test_third_party_failure_does_not_flag(self):
        page = _install(_FakePage())
        page.events["response"][0](
            _FakeResponse(404, "https://www.googletagmanager.com/gtm.js"))
        self.assertEqual(page_guard.suspicion(page), "")

    def test_non_navigation_response_does_not_flag(self):
        # Sub-resource/XHR failures are handled by the bot's own 403 watcher;
        # the app routinely recovers from them, so they must not flag here.
        page = _install(_FakePage())
        page.events["response"][0](_FakeResponse(403, APP_URL, navigation=False))
        self.assertEqual(page_guard.suspicion(page), "")

    def test_healthy_response_does_not_flag(self):
        page = _install(_FakePage())
        page.events["response"][0](_FakeResponse(200, APP_URL))
        self.assertEqual(page_guard.suspicion(page), "")


class TestAssertAlive(unittest.TestCase):
    """Layer 2: confirmation. Must be silent on a healthy page and raise the
    SPECIFIC error the supervisor already knows how to act on."""

    def test_healthy_page_never_raises(self):
        page = _install(_FakePage(body_text="Appointment Details"))
        page_guard.assert_alive(page, "checking")
        page_guard.assert_alive(page, "checking", deep=True)

    def test_permission_issues_raises_geo_blocked(self):
        # The exact page from the AE-HUN failure. Note it reads '(403)', not
        # '403203' — classification must key off the heading text too.
        page = _install(_FakePage(body_text=PERMISSION_ISSUES, url=APP_URL))
        with self.assertRaises(GeoBlockedError):
            page_guard.assert_alive(page, "selecting centre", deep=True)

    def test_ip_block_wins_over_the_generic_error_page(self):
        page = _install(_FakePage(body_text="blocked", html='{"code":"403201"}',
                                  url=ERROR_URL))
        with self.assertRaises(IpBlockedError):
            page_guard.assert_alive(page, "selecting centre")

    def test_account_lock_is_classified_not_generic(self):
        page = _install(_FakePage(body_text="Account Locked (429202)"))
        with self.assertRaises(AccountLockedError):
            page_guard.assert_alive(page, "selecting centre", deep=True)

    def test_unrecognised_error_route_raises_page_blocked(self):
        page = _install(_FakePage(body_text="Something went wrong", url=ERROR_URL))
        with self.assertRaises(PageBlockedError):
            page_guard.assert_alive(page, "selecting centre")

    def test_session_expired_raises_page_blocked(self):
        page = _install(_FakePage(body_text="Session Expired or Invalid"))
        with self.assertRaises(PageBlockedError):
            page_guard.assert_alive(page, "selecting centre", deep=True)

    def test_unconfirmed_suspicion_is_cleared_not_raised(self):
        # A VFS API 403 the app recovered from: flagged, but the DOM shows a
        # healthy page. It must NOT kill the run, and must not re-fire.
        page = _install(_FakePage(body_text="Appointment Details"))
        page_guard.suspect(page, "VFS API returned 403 for /availability")
        page_guard.assert_alive(page, "selecting centre")
        self.assertEqual(page_guard.suspicion(page), "",
                         "an unconfirmed suspicion must be cleared, not left armed")

    def test_shallow_check_skips_the_dom_when_nothing_is_suspicious(self):
        # The healthy path must cost no DOM read at all — that is what makes it
        # safe to call on every dropdown, every combo, every loader wait.
        class _Exploding(_FakePage):
            def evaluate(self, script):
                raise AssertionError("assert_alive read the DOM on a clean page")

        page = _install(_Exploding())
        page_guard.assert_alive(page, "selecting centre")

    def test_deep_probe_on_a_clean_page_skips_the_full_source_scan(self):
        # The deep probe runs once per no-availability combination, so it must
        # not serialise the whole DOM (page.content()) to prove a healthy page is
        # healthy. The full scan is reserved for pages something already flagged.
        page = _install(_FakePage(body_text="Appointment Details"))
        page.content = lambda: (_ for _ in ()).throw(
            AssertionError("deep probe paid for a full page-source scan"))
        page_guard.assert_alive(page, "reading the slot banner", deep=True)

    def test_flagged_page_does_get_the_full_source_scan(self):
        # ...but once something IS flagged, the expensive scan must run: a raw
        # 403201 JSON body shows up nowhere else.
        page = _install(_FakePage(body_text="", html='{"code":"403201"}'))
        page_guard.suspect(page, "VFS returned HTTP 403 for /application-detail")
        with self.assertRaises(IpBlockedError):
            page_guard.assert_alive(page, "selecting centre")

    def test_uninstalled_page_degrades_to_url_only(self):
        # No guard installed (e.g. a stub in another test): no crash, and the
        # free URL check still works.
        page = _FakePage(body_text="Something went wrong", url=ERROR_URL)
        with self.assertRaises(PageBlockedError):
            page_guard.assert_alive(page, "selecting centre")


if __name__ == "__main__":
    unittest.main()
