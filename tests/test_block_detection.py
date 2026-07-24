"""Unit tests for VFS/Cloudflare block-page classification
(src/vfs_bot/block_detection.py).

Every predicate here is a pure function of `page` (reads text, returns a
bool/str, never mutates, never raises), so it can be exercised with a stub
object exposing only `.evaluate()` / `.content()` / `.frames` — no browser,
no network.

Run: python -m unittest tests.test_block_detection
"""

import unittest

from src.vfs_bot import block_detection
from src.vfs_bot.errors import (
    AccessRestrictedError,
    AccountBlockedError,
    AccountLockedError,
    IpBlockedError,
)


class _FakePage:
    """`body_text` backs every single-frame innerText read; `html` backs the
    full page-source scan (is_ip_blocked); `frames` are extra sub-frames, each
    themselves a `_FakePage`."""

    def __init__(self, body_text="", html="", frames=()):
        self.body_text = body_text
        self.html = html
        self.frames = list(frames)
        self.url = "https://example.vfsglobal.com/mt/en/mlt/login"

    def evaluate(self, script):
        # landing_status() runs a different script (returns a heading/msg/url
        # object); every other caller here just wants body innerText.
        if "heading" in script:
            return {"heading": "", "msg": self.body_text, "url": self.url}
        return self.body_text

    def content(self):
        return self.html


class TestIndividualPredicates(unittest.TestCase):
    def test_geo_blocked_by_code(self):
        self.assertTrue(block_detection.is_geo_blocked(_FakePage("Error 403203")))

    def test_geo_blocked_by_heading(self):
        self.assertTrue(block_detection.is_geo_blocked(_FakePage("Permission Issues")))

    def test_geo_blocked_false_on_normal_page(self):
        self.assertFalse(block_detection.is_geo_blocked(_FakePage("Welcome back")))

    def test_session_expired_by_heading(self):
        page = _FakePage("Session Expired or Invalid — sign in again")
        self.assertTrue(block_detection.is_session_expired(page))

    def test_session_expired_by_sentence(self):
        page = _FakePage("It looks like your session has expired or become invalid.")
        self.assertTrue(block_detection.is_session_expired(page))

    def test_session_expired_false_on_login(self):
        self.assertFalse(block_detection.is_session_expired(_FakePage("Sign In")))

    def test_email_not_registered(self):
        page = _FakePage("The entered email id is Not Registered With Us.")
        self.assertTrue(block_detection.is_email_not_registered(page))

    def test_invalid_credentials_variants(self):
        for text in (
            "The email or password you have entered is incorrect.",
            "Incorrect email or password",
            "Invalid username or password",
        ):
            with self.subTest(text=text):
                self.assertTrue(block_detection.is_invalid_credentials(_FakePage(text)))

    def test_invalid_credentials_excludes_not_registered(self):
        # 'not registered' must NOT also read as 'invalid credentials' — they're
        # different, separately-handled outcomes.
        page = _FakePage("The entered email id is not registered with us.")
        self.assertFalse(block_detection.is_invalid_credentials(page))

    def test_access_restricted(self):
        self.assertTrue(block_detection.is_access_restricted(_FakePage("Access Restricted")))

    def test_access_denied(self):
        self.assertTrue(block_detection.is_access_denied(
            _FakePage("Access Denied Due to Unauthorised Activity (429002)")))

    def test_account_locked(self):
        self.assertTrue(block_detection.is_account_locked(_FakePage("Account Locked (429202)")))

    def test_no_false_positives_across_predicates(self):
        page = _FakePage("Welcome to your dashboard")
        for predicate in (
            block_detection.is_geo_blocked,
            block_detection.is_email_not_registered,
            block_detection.is_invalid_credentials,
            block_detection.is_access_restricted,
            block_detection.is_access_denied,
            block_detection.is_account_locked,
        ):
            with self.subTest(predicate=predicate.__name__):
                self.assertFalse(predicate(page))


class TestIpBlockScansFullPage(unittest.TestCase):
    """403201 is rendered as raw JSON that may not surface in body innerText,
    so is_ip_blocked must also scan page.content() and every sub-frame."""

    def test_detected_in_body_text(self):
        self.assertTrue(block_detection.is_ip_blocked(_FakePage(body_text='{"code":"403201"}')))

    def test_detected_in_raw_html_only(self):
        page = _FakePage(body_text="", html='<pre>{"code":"403201"}</pre>')
        self.assertTrue(block_detection.is_ip_blocked(page))

    def test_detected_in_subframe_only(self):
        frame = _FakePage(body_text='{"code":"403201"}')
        page = _FakePage(body_text="", html="", frames=[frame])
        self.assertTrue(block_detection.is_ip_blocked(page))

    def test_not_detected_when_absent_everywhere(self):
        page = _FakePage(body_text="ok", html="<html>ok</html>", frames=[_FakePage("ok")])
        self.assertFalse(block_detection.is_ip_blocked(page))


class TestRaiseIfBlocked(unittest.TestCase):
    """raise_if_blocked must raise the SPECIFIC error class for each block
    page, and stay silent otherwise — the supervisor branches on identity."""

    def test_no_op_on_normal_page(self):
        block_detection.raise_if_blocked(_FakePage("Welcome"))  # must not raise

    def test_ip_block_raises_ip_blocked_error(self):
        with self.assertRaises(IpBlockedError):
            block_detection.raise_if_blocked(_FakePage('{"code":"403201"}'))

    def test_access_denied_raises_account_blocked_error(self):
        with self.assertRaises(AccountBlockedError):
            block_detection.raise_if_blocked(_FakePage("429002 Access Denied Due to Unauthorised Activity"))

    def test_account_locked_raises_account_locked_error(self):
        with self.assertRaises(AccountLockedError):
            block_detection.raise_if_blocked(_FakePage("Account Locked (429202)"))

    def test_access_restricted_raises_access_restricted_error(self):
        with self.assertRaises(AccessRestrictedError):
            block_detection.raise_if_blocked(_FakePage("Access Restricted"))

    def test_ip_block_takes_priority_over_account_block(self):
        # Both codes present: the IP block is the network-level condition and
        # must win, since rotating the IP is the correct remedy either way.
        page = _FakePage('{"code":"403201"} Account Locked (429202)')
        with self.assertRaises(IpBlockedError):
            block_detection.raise_if_blocked(page)


if __name__ == "__main__":
    unittest.main()
