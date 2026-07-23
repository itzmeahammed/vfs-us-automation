"""The VFS 403 classification fix: a 403 on a VFS API call is only a 403201 IP
block if its BODY actually says so; any other 403 (a rejected/stale Turnstile
token) must NOT be mislabeled as an IP block.

Regression guard for the bug where `_attach_block_watcher` treated EVERY
vfsglobal XHR 403 as a 403201 IP block, wasting a proxy IP rotation on what was
really a Turnstile hiccup. See VfsBot._classify_403_responses.
"""

import unittest

from src.vfs_bot.errors import IpBlockedError, TurnstileRejectedError
from src.vfs_bot.vfs_bot_factory import get_vfs_bot


class _FakeResp:
    """Minimal stand-in for a Playwright Response: only .text() is read."""

    def __init__(self, body="", raise_on_read=False):
        self._body = body
        self._raise = raise_on_read

    def text(self):
        if self._raise:
            raise RuntimeError("body evicted (page navigated)")
        return self._body


class _FakePage:
    """A page whose DOM is a normal page (raise_if_blocked is a no-op); only the
    captured network responses drive classification here."""

    def evaluate(self, *_a, **_k):
        return ""

    def content(self):
        return "<html><body>normal login page</body></html>"

    @property
    def frames(self):
        return []

    def screenshot(self, *_a, **_k):  # take_final_screenshot -> no-op
        pass

    url = "https://visa.vfsglobal.com/are/en/xyz/login"


def _bot():
    from src.utils.config_reader import initialize_config
    initialize_config()
    bot = get_vfs_bot("AE", "MT")
    bot._block_responses = []
    return bot


class Test403Classification(unittest.TestCase):
    def test_403201_body_is_ip_block(self):
        bot = _bot()
        bot._block_responses = [_FakeResp('{"code":"403201"}')]
        with self.assertRaises(IpBlockedError):
            bot._classify_403_responses(_FakePage())

    def test_non_403201_body_is_turnstile_reject(self):
        bot = _bot()
        bot._block_responses = [_FakeResp('{"code":"401001","error":"bad token"}')]
        with self.assertRaises(TurnstileRejectedError):
            bot._classify_403_responses(_FakePage())

    def test_unreadable_body_is_not_ip_block(self):
        # Can't read the body -> must NOT assume 403201 (that was the whole bug).
        bot = _bot()
        bot._block_responses = [_FakeResp(raise_on_read=True)]
        with self.assertRaises(TurnstileRejectedError):
            bot._classify_403_responses(_FakePage())

    def test_no_captured_403_is_noop(self):
        bot = _bot()
        bot._block_responses = []
        bot._classify_403_responses(_FakePage())  # does not raise

    def test_403201_wins_when_mixed(self):
        bot = _bot()
        bot._block_responses = [_FakeResp("just a token error"),
                                _FakeResp('{"code":"403201"}')]
        with self.assertRaises(IpBlockedError):
            bot._classify_403_responses(_FakePage())

    def test_responses_consumed_after_classify(self):
        # After classifying, the list is cleared so a later check can't re-raise.
        bot = _bot()
        bot._block_responses = [_FakeResp("token error")]
        with self.assertRaises(TurnstileRejectedError):
            bot._classify_403_responses(_FakePage())
        self.assertEqual(bot._block_responses, [])

    def test_turnstile_reject_is_a_signin_disabled_subclass(self):
        # The supervisor catches SignInDisabledError; TurnstileRejectedError must
        # be caught by that handler (-> rotate IP, log-only, no Telegram).
        from src.vfs_bot.errors import SignInDisabledError
        self.assertTrue(issubclass(TurnstileRejectedError, SignInDisabledError))


if __name__ == "__main__":
    unittest.main()
