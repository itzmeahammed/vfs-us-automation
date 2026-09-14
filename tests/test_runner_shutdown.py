"""Browser teardown ordering.

Playwright's Node driver talks to Python over a pipe. Killing Chrome while that
driver is still connected leaves Node writing into a dead socket, which surfaces
as an unhandled

    Error: EPIPE: broken pipe, write ... at PipeTransport.send

*after* the run has finished — noisy, alarming, and unrelated to what the run
actually did. Order is the whole fix, so order is what these tests pin.
"""

import logging

import pytest

from src.waitlist.runner import shutdown


class FakePlaywright:
    def __init__(self, calls, explode=False):
        self._calls = calls
        self._explode = explode

    def stop(self):
        if self._explode:
            raise RuntimeError("driver already gone")
        self._calls.append("playwright.stop")


class FakeChrome:
    def __init__(self, calls, explode=False):
        self._calls = calls
        self._explode = explode

    def close(self):
        if self._explode:
            raise RuntimeError("could not kill chrome")
        self._calls.append("chrome.close")


class FakeBot:
    pass


def test_playwright_stops_before_chrome_is_killed():
    """THE fix. The reverse order is what produces EPIPE."""
    calls = []
    bot = FakeBot()
    bot._playwright = FakePlaywright(calls)

    shutdown(bot, FakeChrome(calls))

    assert calls == ["playwright.stop", "chrome.close"]


def test_the_playwright_reference_is_cleared():
    """Left set, a second shutdown would stop an already-stopped driver."""
    bot = FakeBot()
    bot._playwright = FakePlaywright([])

    shutdown(bot, FakeChrome([]))

    assert bot._playwright is None


def test_chrome_is_still_killed_when_the_driver_fails_to_stop():
    """A surviving Chrome holds the CDP port and the NEXT run cannot start, so
    killing it must never depend on the driver shutting down cleanly."""
    calls = []
    bot = FakeBot()
    bot._playwright = FakePlaywright(calls, explode=True)

    shutdown(bot, FakeChrome(calls))

    assert calls == ["chrome.close"]


def test_a_bot_with_no_playwright_still_closes_chrome():
    """A run that failed before login has no driver to stop."""
    calls = []
    shutdown(FakeBot(), FakeChrome(calls))
    assert calls == ["chrome.close"]


def test_a_none_bot_still_closes_chrome():
    """The runner binds `bot = None` before the try block, so a failure during
    launch reaches the finally with nothing bound."""
    calls = []
    shutdown(None, FakeChrome(calls))
    assert calls == ["chrome.close"]


def test_a_chrome_that_will_not_close_is_logged_not_raised(caplog):
    """shutdown() runs in a `finally`. Raising there would replace the real
    exception with a teardown one and hide why the run actually failed."""
    with caplog.at_level(logging.WARNING):
        shutdown(FakeBot(), FakeChrome([], explode=True))

    assert any("did not close cleanly" in r.message for r in caplog.records)


def test_shutdown_does_not_recurse():
    """A REAL BUG, caught here: a bulk edit replaced `chrome.close()` INSIDE
    shutdown() with a call to shutdown() itself, so it recursed until the stack
    blew and Chrome was never killed. It surfaced only as a
    'maximum recursion depth exceeded' warning that was easy to dismiss as test
    noise. This asserts chrome.close() is reached exactly once."""
    calls = []
    shutdown(FakeBot(), FakeChrome(calls))
    assert calls.count("chrome.close") == 1
