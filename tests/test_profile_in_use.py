"""Two runs on one account must not share a Chrome profile.

On 2026-10-02 a booking probe on mu***@travnook.com died mid-login with
"Target page, context or browser has been closed". The slot checker had just
reached AE-GRC with the same account, and ChromeProcess.start() kills any
Chrome on the profile it is about to use. That Chrome was the probe's.
"""

import logging
from unittest import mock

import pytest

from src.utils import chrome_launcher
from src.utils.chrome_launcher import ChromeProcess, ProfileInUseError
from src.utils.log_setup import QUIET_TRACEBACK, ConsoleFormatter


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(chrome_launcher, "PROFILE_WAIT_S", 0)


def _chrome(profile_dir):
    return ChromeProcess(profile_dir=str(profile_dir))


def test_a_second_run_on_a_busy_profile_is_refused(tmp_path, no_wait):
    first = _chrome(tmp_path / "acct")
    first._acquire_profile()
    try:
        with pytest.raises(ProfileInUseError, match="in use by another run"):
            _chrome(tmp_path / "acct")._acquire_profile()
    finally:
        first.close()


def test_refusal_happens_before_any_chrome_is_killed(tmp_path, no_wait):
    first = _chrome(tmp_path / "acct")
    first._acquire_profile()
    try:
        with mock.patch.object(chrome_launcher, "kill_stale_bot_chrome") as kill:
            with pytest.raises(ProfileInUseError):
                _chrome(tmp_path / "acct").start()
        kill.assert_not_called()
    finally:
        first.close()


def test_close_frees_the_profile(tmp_path, no_wait):
    first = _chrome(tmp_path / "acct")
    first._acquire_profile()
    first.close()

    second = _chrome(tmp_path / "acct")
    second._acquire_profile()
    second.close()


def test_other_accounts_are_not_affected(tmp_path, no_wait):
    first = _chrome(tmp_path / "acct-a")
    first._acquire_profile()
    try:
        other = _chrome(tmp_path / "acct-b")
        other._acquire_profile()
        other.close()
    finally:
        first.close()


def test_the_refusal_names_the_holder_and_never_the_command_line(tmp_path, no_wait):
    first = _chrome(tmp_path / "acct")
    first._acquire_profile()
    try:
        with pytest.raises(ProfileInUseError) as err:
            _chrome(tmp_path / "acct")._acquire_profile()
        assert "pid" in str(err.value)
        assert "--password" not in str(err.value)
    finally:
        first.close()


def test_supervisor_skips_the_route_without_striking_the_account():
    from src import supervisor

    with mock.patch.object(supervisor, "run_once_with_fresh_browser",
                           side_effect=ProfileInUseError("busy")), \
         mock.patch.object(supervisor.proxy_pool, "pick_for_run",
                           return_value=("http://p:1", "1.2.3.4")), \
         mock.patch.object(supervisor.proxy_pool, "label", side_effect=lambda p: p), \
         mock.patch.object(supervisor, "_alert_failure") as alert, \
         mock.patch.object(supervisor.account_health, "record_failure") as strike:
        outcome = supervisor.run("AE", "GRC", force_email="x@y.com",
                                 force_password="pw")

    assert outcome["status"] == "SKIPPED"
    strike.assert_not_called()
    alert.assert_not_called()


# --------------------------------------------------------------------------- #
# The job log: a known failure is one line                                    #
# --------------------------------------------------------------------------- #

def _record(quiet: bool) -> logging.LogRecord:
    try:
        raise RuntimeError("deep inside playwright")
    except RuntimeError:
        import sys
        exc = sys.exc_info()
    record = logging.LogRecord("x", logging.ERROR, __file__, 1,
                               "Probe failed: browser closed", None, exc)
    if quiet:
        setattr(record, QUIET_TRACEBACK, True)
    return record


def test_console_drops_the_traceback_of_a_quiet_record():
    out = ConsoleFormatter("%(message)s").format(_record(quiet=True))
    assert out == "Probe failed: browser closed"


def test_the_files_still_get_the_traceback():
    record = _record(quiet=True)
    ConsoleFormatter("%(message)s").format(record)
    assert "Traceback" in logging.Formatter("%(message)s").format(record)


def test_an_unexpected_failure_keeps_its_traceback_on_the_console():
    assert "Traceback" in ConsoleFormatter("%(message)s").format(_record(quiet=False))


def test_probe_explains_the_failures_it_understands():
    from src.booking.probe import _explain_failure
    from src.vfs_bot.errors import EmailNotRegisteredError

    TargetClosedError = type("TargetClosedError", (Exception,), {})
    reason, known = _explain_failure(TargetClosedError("Target page closed"))
    assert known and "Chrome was closed" in reason

    reason, known = _explain_failure(EmailNotRegisteredError("not registered"))
    assert known and "client/request file" in reason

    assert _explain_failure(ProfileInUseError("busy")) == ("busy", True)
    assert _explain_failure(KeyError("x"))[1] is False
