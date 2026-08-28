"""Tests for the slot-bot → waitlist-bot auto-trigger (Phase 3).

The headline test is `test_ae_nld_label_mapping`: the supervisor's outcome
carries `result_label()` strings, but clients name the route file's `"label"`.
For AE-NLD those diverge completely, and a naive string match would silently
find no clients — indistinguishable from "nobody is waiting".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

from src.waitlist import autotrigger  # noqa: E402


# --------------------------------------------------------------------------
# The label trap
# --------------------------------------------------------------------------


def test_ae_che_label_mapping():
    """AE-CHE: the two label forms happen to be identical."""
    assert autotrigger.resolve_combo_label(
        "AE-CHE", "Dubai - SCHENGEN") == "Dubai - SCHENGEN"


def test_ae_nld_label_mapping():
    """AE-NLD: the forms DIVERGE. This is the bug this module exists to avoid.

    A client writes "Dubai - Tourist Visa"; the supervisor reports
    "Netherlands Visa application center- Dubai - Tourist Visa - Tourist
    Purpose". Matching those as strings finds nothing.
    """
    result_label = ("Netherlands Visa application center- Dubai - "
                    "Tourist Visa - Tourist Purpose")
    assert autotrigger.resolve_combo_label("AE-NLD", result_label) == \
        "Dubai - Tourist Visa"


def test_ae_nld_abu_dhabi_mapping():
    result_label = ("Netherlands Visa Application Center-Abu Dhabi - "
                    "Schengen Visa - Schengen Short Stay")
    assert autotrigger.resolve_combo_label("AE-NLD", result_label) == \
        "Abu Dhabi - Schengen Visa"


def test_label_mapping_is_whitespace_and_case_insensitive():
    assert autotrigger.resolve_combo_label(
        "AE-CHE", "  dubai  -  schengen  ") == "Dubai - SCHENGEN"


def test_unmappable_label_returns_none():
    """Unknown must be None — never silently treated as 'no clients'."""
    assert autotrigger.resolve_combo_label("AE-CHE", "Atlantis - Moon Visa") is None


def test_unknown_route_returns_none():
    assert autotrigger.resolve_combo_label("ZZ-ZZZ", "anything") is None


def test_plan_reports_an_unmappable_label(monkeypatch):
    """A label we cannot map is an explicit, reported condition."""
    plan = autotrigger.plan_for("AE-CHE", "Atlantis - Moon Visa")
    assert plan.will_run is False
    assert "could not map" in plan.skipped_reason


# --------------------------------------------------------------------------
# Client matching
# --------------------------------------------------------------------------


class _FakePerson:
    def __init__(self, pid, combos, enabled=True):
        self.id = pid
        self.combos = combos
        self.enabled = enabled


def test_no_clients_means_no_run(monkeypatch):
    """The common case must be cheap and must NOT launch a browser."""
    monkeypatch.setattr("src.waitlist.registrant.for_route", lambda *a, **k: [])
    plan = autotrigger.plan_for("AE-CHE", "Dubai - SCHENGEN")
    assert plan.will_run is False
    assert "no enabled clients" in plan.skipped_reason


def test_client_on_a_different_combo_is_not_matched(monkeypatch):
    monkeypatch.setattr("src.waitlist.registrant.for_route",
                        lambda *a, **k: [_FakePerson("x", ["Abu Dhabi - SCHENGEN"])])
    plan = autotrigger.plan_for("AE-CHE", "Dubai - SCHENGEN")
    assert plan.will_run is False


def test_already_registered_client_is_skipped(monkeypatch):
    """A committed journal entry means registering again would duplicate."""
    monkeypatch.setattr("src.waitlist.registrant.for_route",
                        lambda *a, **k: [_FakePerson("x", ["Dubai - SCHENGEN"])])
    monkeypatch.setattr("src.waitlist.journal.blocking_entry",
                        lambda *a, **k: {"status": "success"})
    people = autotrigger.find_waiting_clients("AE-CHE", "Dubai - SCHENGEN")
    assert people == []


def test_pending_entry_also_blocks(monkeypatch):
    """A submit in flight must never be re-attempted."""
    monkeypatch.setattr("src.waitlist.registrant.for_route",
                        lambda *a, **k: [_FakePerson("x", ["Dubai - SCHENGEN"])])
    monkeypatch.setattr("src.waitlist.journal.blocking_entry",
                        lambda *a, **k: {"status": "pending"})
    assert autotrigger.find_waiting_clients("AE-CHE", "Dubai - SCHENGEN") == []


def test_matching_client_is_found(monkeypatch):
    monkeypatch.setattr("src.waitlist.registrant.for_route",
                        lambda *a, **k: [_FakePerson("x", ["Dubai - SCHENGEN"])])
    monkeypatch.setattr("src.waitlist.journal.blocking_entry", lambda *a, **k: None)
    people = autotrigger.find_waiting_clients("AE-CHE", "Dubai - SCHENGEN")
    assert [p.id for p in people] == ["x"]


# --------------------------------------------------------------------------
# Account grouping — one run is one login
# --------------------------------------------------------------------------


class _FakeAccount:
    def __init__(self, email):
        self.email = email


def test_clients_are_grouped_by_account(monkeypatch):
    """Different accounts must become different runs, not one mixed run."""
    people = [_FakePerson("a", []), _FakePerson("b", []), _FakePerson("c", [])]
    emails = {"a": "one@x.com", "b": "two@x.com", "c": "one@x.com"}
    monkeypatch.setattr("src.waitlist.accounts.resolve",
                        lambda p, **k: _FakeAccount(emails[p.id]))

    groups = autotrigger.group_by_account(people)
    assert set(groups) == {"one@x.com", "two@x.com"}
    assert {p.id for p in groups["one@x.com"]} == {"a", "c"}
    assert [p.id for p in groups["two@x.com"]] == ["b"]


def test_client_without_an_account_is_skipped(monkeypatch):
    """An unresolvable account is reported, not crashed on."""
    def _resolve(person, **kwargs):
        if person.id == "bad":
            raise ValueError("no account")
        return _FakeAccount("ok@x.com")

    monkeypatch.setattr("src.waitlist.accounts.resolve", _resolve)
    groups = autotrigger.group_by_account([_FakePerson("bad", []),
                                           _FakePerson("good", [])])
    assert list(groups) == ["ok@x.com"]
    assert [p.id for p in groups["ok@x.com"]] == ["good"]


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------


def test_disabled_by_default(monkeypatch):
    """The master switch is OFF, so nothing fires without a deliberate opt-in."""
    called = []
    monkeypatch.setattr(autotrigger, "_handle_one",
                        lambda *a, **k: called.append(a))
    plans = autotrigger.handle_waitlist_opened("AE-CHE", ["Dubai - SCHENGEN"])
    assert plans == []
    assert called == [], "auto-trigger ran while disabled"


def _enable(monkeypatch, dry_run=True):
    """Turn the auto-trigger on for one test."""
    from src.settings import settings
    cfg = settings()
    monkeypatch.setattr(cfg.waitlist, "auto_trigger_enabled", True,
                        raising=False)
    monkeypatch.setattr(cfg.waitlist, "auto_trigger_dry_run", dry_run,
                        raising=False)
    monkeypatch.setattr("src.settings.settings", lambda: cfg)


def test_enabled_runs_the_plan(monkeypatch):
    _enable(monkeypatch)
    seen = []
    monkeypatch.setattr(autotrigger, "_handle_one",
                        lambda route, label: seen.append((route, label)))
    autotrigger.handle_waitlist_opened("AE-CHE", ["Dubai - SCHENGEN"])
    assert seen == [("AE-CHE", "Dubai - SCHENGEN")]


def test_debounce_suppresses_a_repeat(monkeypatch):
    """A waitlist stays open for hours; we check twice an hour."""
    _enable(monkeypatch)
    monkeypatch.setattr(autotrigger, "plan_for",
                        lambda r, l: autotrigger.TriggerPlan(
                            route=r, combo="Dubai - SCHENGEN",
                            clients=["x"], account_groups={"a@x.com": ["x"]}))
    monkeypatch.setattr(autotrigger, "_on_cooldown", lambda *a: True)
    ran = []
    monkeypatch.setattr(autotrigger, "_run_groups",
                        lambda *a, **k: ran.append(True))

    plan = autotrigger._handle_one("AE-CHE", "Dubai - SCHENGEN")
    assert ran == [], "a debounced trigger still ran"
    assert "debounced" in plan.skipped_reason


def test_dry_run_is_the_default_for_auto_triggered_runs(monkeypatch):
    """Auto-triggered runs must not commit until deliberately switched live."""
    _enable(monkeypatch, dry_run=True)
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("src.waitlist.runner.run_registration", fake_run)
    plan = autotrigger.TriggerPlan(route="AE-CHE", combo="Dubai - SCHENGEN",
                                   clients=["x"],
                                   account_groups={"a@x.com": ["x"]})
    autotrigger._run_groups("AE-CHE", plan)
    assert captured["force_dry_run"] is True, "auto-trigger would have gone live"
    assert captured["registrant_id"] == "x"
    assert captured["only_combo"] == "Dubai - SCHENGEN"


def test_live_mode_is_honoured_when_explicitly_set(monkeypatch):
    _enable(monkeypatch, dry_run=False)
    captured = {}
    monkeypatch.setattr("src.waitlist.runner.run_registration",
                        lambda **kw: captured.update(kw) or [])
    plan = autotrigger.TriggerPlan(route="AE-CHE", combo="Dubai - SCHENGEN",
                                   clients=["x"],
                                   account_groups={"a@x.com": ["x"]})
    autotrigger._run_groups("AE-CHE", plan)
    assert captured["force_dry_run"] is False


# --------------------------------------------------------------------------
# Failure isolation
# --------------------------------------------------------------------------


def test_one_client_failing_does_not_stop_the_others(monkeypatch):
    _enable(monkeypatch)
    attempted = []

    def fake_run(**kwargs):
        attempted.append(kwargs["registrant_id"])
        if kwargs["registrant_id"] == "b":
            raise RuntimeError("this client exploded")
        return []

    monkeypatch.setattr("src.waitlist.runner.run_registration", fake_run)
    plan = autotrigger.TriggerPlan(route="AE-CHE", combo="Dubai - SCHENGEN",
                                   clients=["a", "b", "c"],
                                   account_groups={"x@x.com": ["a", "b", "c"]})
    autotrigger._run_groups("AE-CHE", plan)
    assert attempted == ["a", "b", "c"], "a failure stopped the queue"


def test_slots_available_stops_the_run(monkeypatch):
    """A bookable slot means waitlisting is wrong — stop, do not continue."""
    _enable(monkeypatch)
    from src.waitlist.runner import SlotsAvailable

    attempted = []

    def fake_run(**kwargs):
        attempted.append(kwargs["registrant_id"])
        raise SlotsAvailable("Dubai - SCHENGEN", "Earliest slot: 2026-09-01")

    monkeypatch.setattr("src.waitlist.runner.run_registration", fake_run)
    plan = autotrigger.TriggerPlan(route="AE-CHE", combo="Dubai - SCHENGEN",
                                   clients=["a", "b"],
                                   account_groups={"x@x.com": ["a", "b"]})
    autotrigger._run_groups("AE-CHE", plan)
    assert attempted == ["a"], "the run continued past SlotsAvailable"


def test_handle_never_raises(monkeypatch):
    """A fault here must not fail the slot-check run that called it."""
    _enable(monkeypatch)
    monkeypatch.setattr(autotrigger, "_handle_one",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    # Must not raise.
    assert autotrigger.handle_waitlist_opened("AE-CHE", ["Dubai - SCHENGEN"]) == []


# --------------------------------------------------------------------------
# Multi-tenancy — several of YOUR users on the same route+combo
# --------------------------------------------------------------------------


def test_two_tenants_on_the_same_combo_get_separate_runs(monkeypatch):
    """travnooker.com is multi-tenant: two users may want the same combination.

    This is safe ONLY because each client brings their own VFS account. Grouping
    keeps them apart, so they become two logins run sequentially — never one
    mixed run, which runner.py:399 rejects as a config error.
    """
    people = [_FakePerson("tenant-a", ["Dubai - SCHENGEN"]),
              _FakePerson("tenant-b", ["Dubai - SCHENGEN"])]
    emails = {"tenant-a": "a@example.com", "tenant-b": "b@example.com"}
    monkeypatch.setattr("src.waitlist.accounts.resolve",
                        lambda p, **k: _FakeAccount(emails[p.id]))

    groups = autotrigger.group_by_account(people)
    assert len(groups) == 2, "two tenants were merged into one run"
    assert [p.id for p in groups["a@example.com"]] == ["tenant-a"]
    assert [p.id for p in groups["b@example.com"]] == ["tenant-b"]


def test_two_tenants_sharing_one_account_run_together(monkeypatch):
    """If two tenants DO share a VFS account they must be one run, not two.

    One run is one login; splitting them would log into the same account twice
    in a row, which is exactly the pattern that escalates Cloudflare.
    """
    people = [_FakePerson("tenant-a", ["Dubai - SCHENGEN"]),
              _FakePerson("tenant-b", ["Dubai - SCHENGEN"])]
    monkeypatch.setattr("src.waitlist.accounts.resolve",
                        lambda p, **k: _FakeAccount("shared@example.com"))

    groups = autotrigger.group_by_account(people)
    assert len(groups) == 1
    assert {p.id for p in groups["shared@example.com"]} == {"tenant-a", "tenant-b"}


def test_each_tenant_is_journalled_independently(monkeypatch):
    """One tenant already registered must not block a different tenant.

    The journal key is (route, combo, registrant_id) — the client id is part of
    it, so two tenants on the same combination are separate entries.
    """
    calls = []

    def fake_blocking(route, combo, registrant_id):
        calls.append(registrant_id)
        return {"status": "success"} if registrant_id == "tenant-a" else None

    monkeypatch.setattr("src.waitlist.registrant.for_route", lambda *a, **k: [
        _FakePerson("tenant-a", ["Dubai - SCHENGEN"]),
        _FakePerson("tenant-b", ["Dubai - SCHENGEN"]),
    ])
    monkeypatch.setattr("src.waitlist.journal.blocking_entry", fake_blocking)

    waiting = autotrigger.find_waiting_clients("AE-CHE", "Dubai - SCHENGEN")
    assert [p.id for p in waiting] == ["tenant-b"], (
        "one tenant's existing registration blocked another tenant")
    assert calls == ["tenant-a", "tenant-b"]
