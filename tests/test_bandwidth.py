"""Unit tests for the bandwidth-saving resource filter (Tier 2).

Proves the filter aborts image/media/font and lets script/stylesheet/document
through — without launching a browser — by faking Playwright's route object.

Run: python -m unittest tests.test_bandwidth
"""

import unittest

from src.settings import Bandwidth
from src.vfs_bot.vfs_bot_factory import get_vfs_bot


class _FakeRequest:
    def __init__(self, resource_type):
        self.resource_type = resource_type


class _FakeRoute:
    def __init__(self, resource_type):
        self.request = _FakeRequest(resource_type)
        self.aborted = False
        self.continued = False

    def abort(self):
        self.aborted = True

    def continue_(self):
        self.continued = True


class _FakeContext:
    """Captures the handler registered via context.route(pattern, handler)."""

    def __init__(self):
        self.handler = None

    def route(self, pattern, handler):
        self.handler = handler


class TestResourceFilter(unittest.TestCase):
    def _handler_for(self, blocked_csv="image,media,font"):
        from src.utils.config_reader import initialize_config

        initialize_config()
        bot = get_vfs_bot("AE", "ITA")
        ctx = _FakeContext()
        # Patch the settings the method reads by temporarily building our own.
        import src.settings as s

        original = s._cached
        s._cached = s.Settings(bandwidth=Bandwidth(block_resource_types=blocked_csv))
        try:
            bot._install_resource_blocking(ctx)
        finally:
            s._cached = original
        return bot, ctx.handler

    def test_blocks_heavy_types(self):
        bot, handler = self._handler_for()
        self.assertIsNotNone(handler, "filter was not installed")
        for rtype in ("image", "media", "font"):
            r = _FakeRoute(rtype)
            handler(r)
            self.assertTrue(r.aborted, f"{rtype} should be aborted")
            self.assertFalse(r.continued)

    def test_allows_essential_types(self):
        bot, handler = self._handler_for()
        for rtype in ("script", "stylesheet", "document", "xhr", "fetch"):
            r = _FakeRoute(rtype)
            handler(r)
            self.assertFalse(r.aborted, f"{rtype} must NOT be blocked (breaks flow)")
            self.assertTrue(r.continued)

    def test_empty_list_installs_nothing(self):
        bot, handler = self._handler_for(blocked_csv="")
        self.assertIsNone(handler, "no filter should be installed when list is empty")


if __name__ == "__main__":
    unittest.main()
