"""One client gets ONE waitlist entry per route (guard 6b).

The bug this closes: a client listing two combos on one route
(`["Dubai - SCHENGEN", "Abu Dhabi - SCHENGEN"]`) would register for BOTH. One
person wants one appointment — two entries hold two slots for one need, deny one
to somebody else, and risk VFS voiding both as duplicates.

`max_per_run = 1` happened to mask this, but that is a volume throttle, not a
per-client rule: raise it to serve two different clients in one run and the
duplicate reappears.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

from src.waitlist import guards, journal  # noqa: E402
from src.waitlist.result import Status  # noqa: E402

ROUTE = "AE-CHE"
COMBO_A = "Dubai - SCHENGEN"
COMBO_B = "Abu Dhabi - SCHENGEN"


class _FakeRegistrant:
    def __init__(self, pid="test", combos=None, enabled=True):
        self.id = pid
        self.combos = combos or [COMBO_A, COMBO_B]
        self.enabled = enabled

    def wants(self, combo):
        return any(c.strip().lower() == combo.strip().lower() for c in self.combos)


def _row(combo, status, registrant_id="test", route=ROUTE, ref=None):
    return {
        "route": route, "combo": combo, "registrant_id": registrant_id,
        "status": status, "vfs_reference": ref,
        "started_at": "2026-08-20T10:00:00",
        "finished_at": "2026-08-20T10:01:00",
    }


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    """Open every gate ABOVE 6b, so the tests isolate 6b itself."""
    from src.settings import settings
    cfg = settings()
    monkeypatch.setattr(cfg.waitlist, "register_enabled", True, raising=False)
    monkeypatch.setattr(cfg.waitlist, "max_per_run", 10, raising=False)
    monkeypatch.setattr(cfg.waitlist, "max_per_day", 50, raising=False)
    monkeypatch.setattr("src.settings.settings", lambda: cfg)
    monkeypatch.setattr("src.waitlist.config.is_enabled", lambda r: True)
    monkeypatch.setattr(journal, "count_since", lambda iso: 0)


# --------------------------------------------------------------------------
# journal.blocking_entry_for_route
# --------------------------------------------------------------------------


def test_no_entries_means_nothing_blocks(monkeypatch):
    monkeypatch.setattr(journal, "entries", lambda: [])
    assert journal.blocking_entry_for_route(ROUTE, "test") is None


def test_success_on_one_combo_blocks_the_route(monkeypatch):
    monkeypatch.setattr(journal, "entries",
                        lambda: [_row(COMBO_A, Status.SUCCESS, ref="WL-1")])
    held = journal.blocking_entry_for_route(ROUTE, "test")
    assert held is not None
    assert held["combo"] == COMBO_A


def test_pending_also_blocks_the_route(monkeypatch):
    """A submit in flight may already have landed — treat it as held."""
    monkeypatch.setattr(journal, "entries",
                        lambda: [_row(COMBO_A, Status.PENDING)])
    assert journal.blocking_entry_for_route(ROUTE, "test") is not None


def test_failed_and_skipped_do_not_block(monkeypatch):
    """Nothing was submitted, so the client is still free."""
    monkeypatch.setattr(journal, "entries", lambda: [
        _row(COMBO_A, Status.FAILED),
        _row(COMBO_B, Status.SKIPPED),
    ])
    assert journal.blocking_entry_for_route(ROUTE, "test") is None


def test_a_resolved_entry_frees_the_client(monkeypatch):
    """The LATEST row per combo wins: success → later failed = not held."""
    monkeypatch.setattr(journal, "entries", lambda: [
        _row(COMBO_A, Status.SUCCESS),
        _row(COMBO_A, Status.FAILED),        # cancelled and resolved
    ])
    assert journal.blocking_entry_for_route(ROUTE, "test") is None


def test_a_different_client_is_unaffected(monkeypatch):
    """Multi-tenant: one user's entry must never block another's."""
    monkeypatch.setattr(journal, "entries",
                        lambda: [_row(COMBO_A, Status.SUCCESS, registrant_id="other")])
    assert journal.blocking_entry_for_route(ROUTE, "test") is None


def test_a_different_route_is_unaffected(monkeypatch):
    """One appointment per ROUTE — a Swiss entry must not block an Italian one."""
    monkeypatch.setattr(journal, "entries", lambda: [
        _row("Dubai - Schengen Visa", Status.SUCCESS, route="AE-ITA"),
    ])
    assert journal.blocking_entry_for_route(ROUTE, "test") is None


# --------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------


def test_second_combo_is_blocked_after_the_first_registers(monkeypatch):
    """THE BUG: the same client must not register for both combos."""
    monkeypatch.setattr(journal, "entries",
                        lambda: [_row(COMBO_A, Status.SUCCESS, ref="WL-1")])
    monkeypatch.setattr(journal, "blocking_entry", lambda *a, **k: None)

    verdict = guards.check(ROUTE, COMBO_B, _FakeRegistrant())
    assert not verdict.allowed
    assert "already holds" in verdict.reason
    assert COMBO_A in verdict.reason
    assert "preference order" in verdict.reason


def test_the_first_combo_is_allowed(monkeypatch):
    """Nothing held yet, so the preferred combo proceeds."""
    monkeypatch.setattr(journal, "entries", lambda: [])
    monkeypatch.setattr(journal, "blocking_entry", lambda *a, **k: None)
    assert guards.check(ROUTE, COMBO_A, _FakeRegistrant()).allowed


def test_the_same_combo_still_reports_already_registered(monkeypatch):
    """Re-attempting the SAME combo keeps the more specific gate-6 message."""
    row = _row(COMBO_A, Status.SUCCESS, ref="WL-1")
    monkeypatch.setattr(journal, "entries", lambda: [row])
    monkeypatch.setattr(journal, "blocking_entry",
                        lambda r, c, i: row if c == COMBO_A else None)

    verdict = guards.check(ROUTE, COMBO_A, _FakeRegistrant())
    assert not verdict.allowed
    assert "already registered" in verdict.reason


def test_a_dangling_entry_keeps_its_own_message(monkeypatch):
    """Gate 5 must win over 6b — the human needs the resolve instructions."""
    row = _row(COMBO_A, Status.UNKNOWN)
    monkeypatch.setattr(journal, "entries", lambda: [row])
    monkeypatch.setattr(journal, "blocking_entry",
                        lambda r, c, i: row if c == COMBO_A else None)

    verdict = guards.check(ROUTE, COMBO_A, _FakeRegistrant())
    assert not verdict.allowed
    assert verdict.needs_human is True
    assert "unresolved" in verdict.reason


def test_another_client_may_still_register_the_same_combo(monkeypatch):
    """Guard 6b is per-CLIENT: it must not turn into a per-combo lock."""
    monkeypatch.setattr(journal, "entries",
                        lambda: [_row(COMBO_A, Status.SUCCESS, registrant_id="other")])
    monkeypatch.setattr(journal, "blocking_entry", lambda *a, **k: None)

    verdict = guards.check(ROUTE, COMBO_A, _FakeRegistrant(pid="test"))
    assert verdict.allowed, "one tenant's entry blocked another tenant"


def test_raising_max_per_run_no_longer_permits_a_duplicate(monkeypatch):
    """Regression: max_per_run=1 was masking this.

    It is a volume throttle, not a per-client rule. With it raised — which you
    would do to serve two different clients in one run — guard 6b must still
    stop one client taking two entries.
    """
    from src.settings import settings
    monkeypatch.setattr(settings().waitlist, "max_per_run", 5, raising=False)
    monkeypatch.setattr(journal, "entries",
                        lambda: [_row(COMBO_A, Status.SUCCESS)])
    monkeypatch.setattr(journal, "blocking_entry", lambda *a, **k: None)

    verdict = guards.check(ROUTE, COMBO_B, _FakeRegistrant(), attempted_this_run=1)
    assert not verdict.allowed
    assert "already holds" in verdict.reason
