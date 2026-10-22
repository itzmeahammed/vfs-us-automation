"""Waitlist runner: what happens when attempt 1 fails to LOG IN.

The rules that must hold:
  * an IP-side login failure is retried on a DIFFERENT exit IP with the SAME
    account, up to [waitlist] login_ip_tries;
  * an account-side failure (wrong password, locked) is never retried and
    counts as a strike; an IP-side failure strikes nobody;
  * when every login attempt fails, one FAILED journal row per planned
    registration is written, so the attempt is visible and countable;
  * after N failed rows in a day the client is left alone until tomorrow —
    by the guard, by the runner before Chrome starts, and by the auto-trigger;
  * nothing past login is ever retried (register() is reached at most once).
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "d" * 64)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

from src.vfs_bot.errors import (  # noqa: E402
    InvalidCredentialsError,
    IpBlockedError,
    SignInDisabledError,
)
from src.waitlist import guards, journal, runner  # noqa: E402
from src.waitlist.accounts import Account  # noqa: E402
from src.waitlist.registrant import Registrant  # noqa: E402
from src.waitlist.result import Status, WaitlistResult  # noqa: E402

ROUTE = "AE-CHE"
COMBO = "Dubai - SCHENGEN"


class FakeChrome:
    started: List[str] = []

    def __init__(self, port=None, url=None, proxy=None, profile_key=None, **_):
        self.proxy = proxy
        self.cdp_url = "http://127.0.0.1:9222"
        self.closed = False

    def start(self):
        FakeChrome.started.append(self.proxy or "local")
        return self

    def close(self):
        self.closed = True


class FakeBot:
    def set_credential(self, email, password):
        self.email = email


def _person(cid: str = "ahmed") -> Registrant:
    return Registrant(cid, {"route": ROUTE, "combos": [COMBO],
                            "first_name": "Ahmed", "last_name": "Khan",
                            "account": "holder@example.com",
                            "account_password": "pw"})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A runner wired to fakes: Chrome, bot, login, journal, settings."""
    FakeChrome.started = []
    monkeypatch.setattr(journal, "JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(journal, "JOURNAL_FILE", str(tmp_path / "journal.jsonl"))
    monkeypatch.setattr(runner, "ChromeProcess", FakeChrome)
    monkeypatch.setattr(runner, "get_vfs_bot", lambda s, d: FakeBot())
    monkeypatch.setattr(runner, "set_config_value", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_reset_usage", lambda: None)
    monkeypatch.setattr(runner, "_report_usage", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_record_usage", lambda *a, **k: None)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)

    strikes: List[tuple] = []
    monkeypatch.setattr(runner, "_record_account_outcome",
                        lambda email, route, ok, reason="": strikes.append((ok, reason)))

    from src.settings import settings
    sw = settings().waitlist
    monkeypatch.setattr(sw, "login_ip_tries", 2)
    monkeypatch.setattr(sw, "max_failures_per_client_per_day", 3)

    class Env:
        pass

    e = Env()
    e.strikes = strikes
    e.resolved = Account("holder@example.com", "pw", "test")
    e.plan = [(_person(), COMBO)]

    def run(login, proxy_url="http://p1:1", proxy_forced=False):
        monkeypatch.setattr(runner, "_login_and_reach_appointment_page", login)
        results: List[WaitlistResult] = []
        return runner._run_registration_locked(
            route=ROUTE, source="AE", dest="CHE", plan=e.plan, url="https://x",
            resolved=e.resolved, account="ho***", proxy_url=proxy_url,
            force_dry_run=True, keep_open=False, results=results,
            committed_this_run=0, bot=None, proxy_forced=proxy_forced)

    e.run = run
    return e


def _registered_once(monkeypatch, calls):
    """register() stub that records it ran and returns a dry-run result."""
    from src.waitlist import register as register_mod

    def fake_register(page, *, route, combo, registrant, account, **kw):
        calls.append(combo)
        return WaitlistResult(route=route, combo=combo, registrant_id=registrant.id,
                              status=Status.DRY_RUN, account=account).finish(Status.DRY_RUN)

    monkeypatch.setattr(register_mod, "register", fake_register)
    monkeypatch.setattr(runner, "_ensure_appointment_page", lambda bot, page, url: page)
    monkeypatch.setattr(runner, "_combo_parts", lambda c, r: {"centre": "Dubai"})
    monkeypatch.setattr(runner, "_select_combo", lambda *a, **k: None)
    monkeypatch.setattr(runner.slot_check, "read_slot_message", lambda *a, **k: "")
    monkeypatch.setattr(runner.detect, "is_offered", lambda *a, **k: True)
    monkeypatch.setattr(runner.accounts, "capacity_verdict", lambda *a, **k: (True, ""))
    monkeypatch.setattr(runner.waitlist_config, "checkbox_selector", lambda r: "#cb")


# --------------------------------------------------------------------------- #
# Retry on a different IP                                                      #
# --------------------------------------------------------------------------- #

def test_an_ip_block_on_attempt_1_is_retried_on_another_ip_same_account(env, monkeypatch):
    attempts = []

    def login(bot, url):
        attempts.append(bot.email)
        if len(attempts) == 1:
            raise IpBlockedError("403201")
        return object()

    monkeypatch.setattr(runner, "_next_proxy", lambda route, email, tried: "http://p2:1")
    calls: List[str] = []
    _registered_once(monkeypatch, calls)

    results = env.run(login)

    assert attempts == ["holder@example.com", "holder@example.com"]   # same account
    assert FakeChrome.started == ["http://p1:1", "http://p2:1"]        # different IP
    assert calls == [COMBO]                                            # registered ONCE
    assert [r.status for r in results] == [Status.DRY_RUN]
    assert env.strikes == [(True, "")]                                 # login counted OK


def test_a_turnstile_failure_with_no_alternate_ip_retries_the_same_ip(env, monkeypatch):
    attempts = []

    def login(bot, url):
        attempts.append(1)
        if len(attempts) == 1:
            raise SignInDisabledError("turnstile")
        return object()

    monkeypatch.setattr(runner, "_next_proxy", lambda route, email, tried: None)
    _registered_once(monkeypatch, [])

    env.run(login)

    assert FakeChrome.started == ["http://p1:1", "http://p1:1"]


def test_a_forced_proxy_is_never_rotated(env, monkeypatch):
    attempts = []

    def login(bot, url):
        attempts.append(1)
        if len(attempts) == 1:
            raise IpBlockedError("403201")
        return object()

    monkeypatch.setattr(runner, "_next_proxy",
                        lambda *a: pytest.fail("must not pick a proxy when forced"))
    _registered_once(monkeypatch, [])

    env.run(login, proxy_forced=True)

    assert FakeChrome.started == ["http://p1:1", "http://p1:1"]


# --------------------------------------------------------------------------- #
# Giving up: journalled, visible, no strike for the IP                         #
# --------------------------------------------------------------------------- #

def test_when_every_login_fails_a_failed_row_is_journalled_and_nobody_is_struck(env, monkeypatch):
    def login(bot, url):
        raise IpBlockedError("403201")

    monkeypatch.setattr(runner, "_next_proxy", lambda route, email, tried: "http://p2:1")

    results = env.run(login)

    assert [r.status for r in results] == [Status.FAILED]
    assert results[0].reason.startswith("login failed:")
    rows = journal.entries()
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert rows[0]["registrant_id"] == "ahmed" and rows[0]["combo"] == COMBO
    assert env.strikes == []                      # the IP's fault, not the account's
    assert len(FakeChrome.started) == 2           # login_ip_tries honoured


def test_wrong_credentials_are_not_retried_and_do_strike_the_account(env, monkeypatch):
    def login(bot, url):
        raise InvalidCredentialsError("bad password")

    monkeypatch.setattr(runner, "_next_proxy",
                        lambda *a: pytest.fail("an account fault must not rotate IPs"))

    results = env.run(login)

    assert [r.status for r in results] == [Status.FAILED]
    assert len(FakeChrome.started) == 1
    assert env.strikes and env.strikes[0][0] is False
    assert journal.entries()[0]["status"] == "failed"


def test_a_failed_login_row_does_not_block_the_client(env, monkeypatch):
    """'failed' means nothing was submitted; the next trigger may try again."""
    def login(bot, url):
        raise IpBlockedError("403201")

    monkeypatch.setattr(runner, "_next_proxy", lambda *a: None)
    env.run(login)

    assert journal.blocking_entry(ROUTE, COMBO, "ahmed") is None


# --------------------------------------------------------------------------- #
# ...but not forever: the per-client daily failure cap                         #
# --------------------------------------------------------------------------- #

def _fail_rows(n: int, when: str = None):
    from datetime import date
    for _ in range(n):
        r = WaitlistResult(route=ROUTE, combo=COMBO, registrant_id="ahmed",
                           status=Status.FAILED, account="x")
        if when:
            r.started_at = when
        journal.append(r.finish(Status.FAILED, "login failed: 403201"))


def test_failures_since_counts_only_todays_failed_rows_for_the_triple(env):
    _fail_rows(2)
    _fail_rows(1, when="2020-01-01T00:00:00")                  # yesterday's
    journal.append(WaitlistResult(route=ROUTE, combo=COMBO, registrant_id="other",
                                  status=Status.FAILED).finish(Status.FAILED))
    from datetime import date
    assert journal.failures_since(ROUTE, COMBO, "ahmed", date.today().isoformat()) == 2


def test_the_cap_stops_the_runner_before_chrome_starts(env, monkeypatch):
    _fail_rows(3)
    assert guards.failure_cap_problem(ROUTE, COMBO, "ahmed")
    assert not guards.failure_cap_problem(ROUTE, COMBO, "someone-else")

    from src.waitlist.errors import WaitlistConfigError
    monkeypatch.setattr(runner, "_roster", lambda route, rid: [_person()])
    monkeypatch.setattr(runner.waitlist_config, "get", lambda r: {})
    with pytest.raises(WaitlistConfigError, match="failure cap"):
        runner.run_registration("AE", "CHE")
    assert FakeChrome.started == []


def test_the_cap_is_a_guard_too(env, monkeypatch):
    """Inside a run (another client in the same plan got us logged in)."""
    from src.settings import settings
    monkeypatch.setattr(settings().switches, "waitlist", True)
    monkeypatch.setattr(settings().waitlist, "register_enabled", True)
    monkeypatch.setattr(guards.waitlist_config, "is_enabled", lambda r: True)
    from src.utils import route_pause
    monkeypatch.setattr(route_pause, "problem", lambda *a: "")

    verdict = guards.check(ROUTE, COMBO, _person())
    assert verdict.allowed
    _fail_rows(3)
    verdict = guards.check(ROUTE, COMBO, _person())
    assert not verdict.allowed and "failed 3 time(s) today" in verdict.reason


def test_zero_disables_the_cap(env, monkeypatch):
    from src.settings import settings
    monkeypatch.setattr(settings().waitlist, "max_failures_per_client_per_day", 0)
    _fail_rows(10)
    assert guards.failure_cap_problem(ROUTE, COMBO, "ahmed") == ""


# --------------------------------------------------------------------------- #
# Preference order: one entry per client per route                             #
# --------------------------------------------------------------------------- #

def _three_combo_plan(env):
    person = _person()
    env.plan = [(person, "Abu Dhabi - Tourist Visa"), (person, "Dubai - Tourist Visa"),
                (person, "Abu Dhabi - Schengen Visa")]


def _register_with(monkeypatch, outcomes, calls):
    """register() stub returning the given status per call, in order."""
    from src.waitlist import register as register_mod
    _registered_once(monkeypatch, [])
    it = iter(outcomes)

    def fake_register(page, *, route, combo, registrant, account, **kw):
        calls.append(combo)
        status = next(it)
        return WaitlistResult(route=route, combo=combo, registrant_id=registrant.id,
                              status=status, account=account).finish(status)

    monkeypatch.setattr(register_mod, "register", fake_register)
    nav = []
    monkeypatch.setattr(runner, "_ensure_appointment_page",
                        lambda bot, page, url: nav.append(1) or page)
    return nav


def test_after_a_success_lower_preferences_are_not_touched(env, monkeypatch):
    """THE live run of 2026-10-20 18:53: registered on preference 1, then went
    back to the portal for preferences 2 and 3."""
    _three_combo_plan(env)
    calls: List[str] = []
    nav = _register_with(monkeypatch, [Status.SUCCESS], calls)

    results = env.run(lambda bot, url: object())

    assert calls == ["Abu Dhabi - Tourist Visa"]
    assert len(nav) == 1                         # never navigated again
    assert [r.status for r in results] == [Status.SUCCESS]


def test_a_failed_preference_falls_through_to_the_next(env, monkeypatch):
    _three_combo_plan(env)
    calls: List[str] = []
    _register_with(monkeypatch, [Status.FAILED, Status.SUCCESS], calls)

    env.run(lambda bot, url: object())

    assert calls == ["Abu Dhabi - Tourist Visa", "Dubai - Tourist Visa"]


def test_a_rehearsal_stops_at_the_first_preference_it_could_register(env, monkeypatch):
    _three_combo_plan(env)
    calls: List[str] = []
    _register_with(monkeypatch, [Status.DRY_RUN], calls)
    env.run(lambda bot, url: object())
    assert calls == ["Abu Dhabi - Tourist Visa"]


def test_a_client_who_already_holds_an_entry_costs_no_login(env, monkeypatch):
    journal.append(WaitlistResult(route=ROUTE, combo=COMBO, registrant_id="ahmed",
                                  status=Status.SUCCESS, vfs_reference="NEU1"
                                  ).finish(Status.SUCCESS))
    from src.waitlist.errors import WaitlistConfigError
    monkeypatch.setattr(runner, "_roster", lambda route, rid: [_person()])
    monkeypatch.setattr(runner.waitlist_config, "get", lambda r: {})
    with pytest.raises(WaitlistConfigError, match="already holds"):
        runner.run_registration("AE", "CHE")
    assert FakeChrome.started == []
