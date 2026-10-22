"""A client's API status reflects what they HOLD, not their latest journal row.

Live, 2026-10-20: registered on preference 1 (NEU129134422688), then the run
journalled 'skipped' for preferences 2 and 3, and GET /v1/clients reported
status 'waiting' next to a real reference number.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "d" * 64)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

from src.api.modules.clients.service import waitlist_status  # noqa: E402
from src.waitlist import journal  # noqa: E402
from src.waitlist.result import Status, WaitlistResult  # noqa: E402

R = "AE-NLD"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(journal, "JOURNAL_FILE", str(tmp_path / "j.jsonl"))


def _row(combo, status, ref=None, at="2026-10-20T18:00:00", cid="c1"):
    r = WaitlistResult(route=R, combo=combo, registrant_id=cid, status=status,
                       vfs_reference=ref)
    r.started_at = at
    r.finish(status)
    r.finished_at = at
    journal.append(r)


def test_registered_then_skipped_lower_preferences_is_still_registered():
    _row("Abu Dhabi - Tourist Visa", Status.FAILED, at="2026-10-20T18:43:18")
    _row("Abu Dhabi - Tourist Visa", Status.PENDING, at="2026-10-20T18:52:24")
    _row("Abu Dhabi - Tourist Visa", Status.SUCCESS, ref="NEU129134422688",
         at="2026-10-20T18:53:20")
    _row("Dubai - Tourist Visa", Status.SKIPPED, at="2026-10-20T18:54:08")
    _row("Abu Dhabi - Schengen Visa", Status.SKIPPED, at="2026-10-20T18:55:04")

    s = journal.summarise_by_client()["c1"]
    assert waitlist_status(s) == "registered"
    assert s["vfs_reference"] == "NEU129134422688"
    assert s["holding_combo"] == "Abu Dhabi - Tourist Visa"
    assert s["last_status"] == "success"          # skips are not attempts
    assert s["run_count"] == 3


def test_a_later_failure_on_another_combo_does_not_undo_a_registration():
    _row("A", Status.SUCCESS, ref="NEU1", at="2026-10-20T10:00:00")
    _row("B", Status.FAILED, at="2026-10-20T11:00:00")
    assert waitlist_status(journal.summarise_by_client()["c1"]) == "registered"


def test_an_unresolved_submit_needs_attention_even_after_a_skip():
    _row("A", Status.PENDING, at="2026-10-20T10:00:00")
    _row("B", Status.SKIPPED, at="2026-10-20T11:00:00")
    assert waitlist_status(journal.summarise_by_client()["c1"]) == "needs_attention"


def test_a_registration_resolved_as_failed_frees_the_client():
    _row("A", Status.SUCCESS, ref="NEU1", at="2026-10-20T10:00:00")
    _row("A", Status.FAILED, at="2026-10-20T12:00:00")   # human: cancelled
    s = journal.summarise_by_client()["c1"]
    assert waitlist_status(s) == "waiting"
    assert s["holding_combo"] is None


def test_only_skips_or_failures_is_waiting():
    _row("A", Status.FAILED)
    _row("B", Status.SKIPPED)
    assert waitlist_status(journal.summarise_by_client()["c1"]) == "waiting"


def test_an_invitation_state_still_wins():
    _row("A", Status.SUCCESS, ref="NEU1")
    s = journal.summarise_by_client()["c1"]
    assert waitlist_status(s, "pending") == "invited"
    assert waitlist_status(s, "booked") == "booked"
