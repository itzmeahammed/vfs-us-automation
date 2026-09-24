"""Phase 3: the bot records what it checks — and a broken database costs nothing.

The isolation tests matter more than the happy path. Recording history is a
side benefit; checking slots is the job. If the database is locked, missing or
corrupt, the route must complete exactly as it does today, with the reading
still safe in the log.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.slots import store as slot_store  # noqa: E402
from src.slots.store import SlotStore  # noqa: E402
from src.vfs_bot import slot_check  # noqa: E402

GST = timezone(timedelta(hours=4))

COMBOS = [
    {"centre": "Norway Visa Application Center - Dubai", "category": "Short Stay",
     "sub_category": "Tourist", "label": "Norway - Dubai - Tourist"},
]


@pytest.fixture
def fake_page():
    page = MagicMock()
    page.locator.return_value.first.wait_for.return_value = None
    return page


def _run_slot_check(page, combos=None):
    """Drives run_slot_check with the browser and Telegram stubbed out."""
    schema = {"slot_check": {"combinations": combos or COMBOS}}
    with patch.object(slot_check, "_select_combo", return_value=(True, None)), \
         patch.object(slot_check, "read_slot_message",
                      return_value="Earliest available slot for 1 Applicants is : 20-09-2026"), \
         patch.object(slot_check, "send_slot_report"), \
         patch.object(slot_check.diagnostics, "take_screenshot"), \
         patch.object(slot_check.page_guard, "assert_alive"), \
         patch("src.vfs_bot.waitlist.notify"), \
         patch("src.utils.config_reader.get_config_value", return_value=""):
        return slot_check.run_slot_check(page, schema, "AE", "NOR")


def test_a_checked_combination_is_recorded(fake_page):
    results = _run_slot_check(fake_page)
    assert results, "the slot check itself must still return its results"

    store = slot_store.get_store()
    row = store.conn.execute(
        "SELECT c.outcome, co.country_name, co.city, sd.slot_date"
        " FROM checks c JOIN combos co ON co.id = c.combo_id"
        " LEFT JOIN slot_dates sd ON sd.check_id = c.id").fetchone()
    assert (row["outcome"], row["country_name"], row["city"], row["slot_date"]) == \
           ("slot", "Norway", "Dubai", "2026-09-20")


def test_a_broken_database_does_not_break_the_check(fake_page, tmp_path, monkeypatch):
    """The point of the whole failure-isolation layer."""
    unusable = tmp_path / "nope"
    unusable.mkdir()
    monkeypatch.setenv("VFS_SLOTS_DB", str(unusable))   # a directory, not a file
    slot_store.reset()

    results = _run_slot_check(fake_page)

    assert len(results) == 1
    assert "20-09-2026" in results[0][1]


def test_a_failing_record_call_does_not_break_the_check(fake_page):
    with patch.object(slot_store, "record_check", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            # Sanity: the patch really does raise...
            slot_store.record_check("AE-NOR", "x")
    # ...and through the real, wrapped path, a store that raises is swallowed.
    with patch.object(SlotStore, "record_check", side_effect=RuntimeError("boom")):
        results = _run_slot_check(fake_page)
    assert len(results) == 1


def test_recording_can_be_switched_off(fake_page, monkeypatch):
    monkeypatch.setattr(slot_store, "enabled", lambda: False)
    slot_store.reset()
    results = _run_slot_check(fake_page)
    assert len(results) == 1
    assert slot_store.get_store() is None


def test_checks_are_linked_to_the_run_the_supervisor_opened():
    store = SlotStore.open(os.environ["VFS_SLOTS_DB"])
    try:
        run_id = store.start_run("AE-NOR", started_at=datetime(2026, 9, 14, 10, tzinfo=GST))
        slot_store._store = store          # the process-wide store, for record_check
        slot_store.set_current_run(run_id)
        try:
            store.sync_combos()
            slot_store.record_check(
                "AE-NOR", "No slot message shown (no availability?).",
                combo=COMBOS[0], ts=datetime(2026, 9, 14, 10, 5, tzinfo=GST))
        finally:
            slot_store.set_current_run(None)

        row = store.conn.execute("SELECT run_id FROM checks").fetchone()
        assert row["run_id"] == run_id
    finally:
        slot_store._store = None
        store.close()


def test_supervisor_opens_and_closes_a_run_row():
    """`run()` must record the outcome even when the route never starts."""
    from src import supervisor

    outcome = {"status": "PAUSED", "attempts": 0, "account": "ab***@x.com",
               "proxy": "local", "error": "all eligible accounts are in cooldown"}
    with patch.object(supervisor, "_run_route", return_value=outcome) as inner:
        got = supervisor.run("AE", "NOR")

    assert got is outcome
    assert inner.called
    store = slot_store.get_store()
    row = store.conn.execute("SELECT * FROM runs").fetchone()
    assert row["route"] == "AE-NOR"
    assert row["status"] == "PAUSED"
    assert row["account_masked"] == "ab***@x.com"
    assert row["finished_at_utc"]
    assert slot_store.current_run() is None


def test_a_crashing_route_still_closes_its_run_row():
    from src import supervisor

    with patch.object(supervisor, "_run_route", side_effect=RuntimeError("kaboom")):
        with pytest.raises(RuntimeError):
            supervisor.run("AE", "NOR")

    store = slot_store.get_store()
    row = store.conn.execute("SELECT status, finished_at_utc FROM runs").fetchone()
    assert row["status"] == "CRASHED"
    assert row["finished_at_utc"]
    assert slot_store.current_run() is None
