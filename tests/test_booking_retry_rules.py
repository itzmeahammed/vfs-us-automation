"""When the auto-booker tries again, when it gives up, and when it gives in.

Every test here is one of the flaws found reading the end-to-end flow on
2026-10-24. They are about spending two scarce things wisely: LOGINS, which
VFS blocks an account for using too often, and the operator's attention.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from src.booking import autobook
from src.booking import requests as store

CLIENT = "acme-ae-nor-0001"


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "REQUEST_DIR", str(tmp_path))
    return tmp_path


def _make(rid=CLIENT):
    return store.create(rid, {
        "route": "AE-NOR", "combo": "Norway Visa Application Center - Dubai",
        "date_from": "2026-11-01", "date_to": "2026-11-30",
        "first_name": "A", "last_name": "B", "enabled": True})


# --------------------------------------------------------------------------- #
# The retry budget belongs to the BOT                                          #
# --------------------------------------------------------------------------- #

def test_a_manual_run_never_spends_the_automatic_budget(store_dir):
    """Testing a client by hand used to exhaust max_attempts, after which the
    bot refused to book that client on its own ever again."""
    _make()
    for _ in range(3):
        store.record_event(CLIENT, "attempt_started", "by hand",
                           status=store.BOOKING, trigger="manual")
        store.record_event(CLIENT, "attempt_failed", "VFS did not respond.",
                           status=store.WAITING)

    req = store.get(CLIENT)
    assert req.data["attempts"] == 3              # shown to the operator
    assert req.data["consecutive_failures"] == 0  # the bot's budget, untouched


def test_the_budget_counts_only_unbroken_automatic_failures(store_dir):
    _make()
    for _ in range(2):
        store.record_event(CLIENT, "attempt_started", "", status=store.BOOKING,
                           trigger="auto")
        store.record_event(CLIENT, "attempt_failed", "VFS was slow.",
                           status=store.WAITING)
    assert store.get(CLIENT).data["consecutive_failures"] == 2

    store.record_event(CLIENT, "booked", "booked and paid", status=store.BOOKED)
    assert store.get(CLIENT).data["consecutive_failures"] == 0


@pytest.mark.parametrize("how", ["resolved", "edited", "enabled"])
def test_a_human_touching_the_request_restores_its_budget(store_dir, how):
    """A corrected field, or a person who looked, is a new situation."""
    _make()
    store.record_event(CLIENT, "attempt_started", "", status=store.BOOKING)
    store.record_event(CLIENT, "attempt_failed", "boom", status=store.WAITING)
    assert store.get(CLIENT).data["consecutive_failures"] == 1

    if how == "edited":
        store.replace_fields(CLIENT, {
            "route": "AE-NOR", "combo": "Norway Visa Application Center - Dubai",
            "date_from": "2026-12-01", "date_to": "2026-12-31",
            "first_name": "A", "last_name": "B"})
    elif how == "enabled":
        store.set_enabled(CLIENT, True)
    else:
        store.record_event(CLIENT, "resolved", "nothing was booked",
                           status=store.WAITING)
    assert store.get(CLIENT).data["consecutive_failures"] == 0


# --------------------------------------------------------------------------- #
# Failures no retry can fix                                                    #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("reason", [
    "stopped at 'login': Account x is DISABLED (wrong credentials)",
    "Required for booking: passport_number.",
    "stopped at 'details': Could not select 'Abu Dhabi' in dropdown.",
    "No VFS account to book with.",
])
def test_a_failure_that_cannot_come_right_stops_retrying(reason):
    """Each retry is a login, and VFS blocks an account after a few."""
    assert autobook.permanent_problem(reason)


@pytest.mark.parametrize("reason", [
    "Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED",
    "no date inside the window",
    "VFS's login page did not load.",
    "",
])
def test_an_ordinary_failure_still_retries(reason):
    assert autobook.permanent_problem(reason) == ""


# --------------------------------------------------------------------------- #
# A booking whose process died                                                 #
# --------------------------------------------------------------------------- #

def test_a_dead_booking_process_is_noticed_in_minutes(store_dir, monkeypatch,
                                                      tmp_path):
    """It used to take three hours, during which the request was not armed and
    the web app showed a booking that had already died."""
    from src.booking import attempt_job

    monkeypatch.setattr(attempt_job, "JOBS_FILE", str(tmp_path / "jobs.jsonl"))
    _make()
    store.record_event(CLIENT, "attempt_started", "going",
                       status=store.BOOKING, run_id="deadrun1")
    attempt_job.started("deadrun1", request_id=CLIENT, route="AE-NOR")
    monkeypatch.setattr(attempt_job, "is_running", lambda pid: False)

    # Inside the grace period: still believed to be working.
    assert store.flag_stale(now=time.time()) == []

    later = time.time() + store.DEAD_PROCESS_GRACE_SECONDS + 60
    assert store.flag_stale(now=later) == [CLIENT]
    req = store.get(CLIENT)
    assert req.status == store.NEEDS_ATTENTION
    # Never back to waiting: it may have paid.
    assert "booked or paid is unknown" in req.data["last_error"]


def test_a_booking_still_running_is_left_alone(store_dir, monkeypatch, tmp_path):
    from src.booking import attempt_job

    monkeypatch.setattr(attempt_job, "JOBS_FILE", str(tmp_path / "jobs.jsonl"))
    _make()
    store.record_event(CLIENT, "attempt_started", "going",
                       status=store.BOOKING, run_id="liverun1")
    attempt_job.started("liverun1", request_id=CLIENT, route="AE-NOR")
    monkeypatch.setattr(attempt_job, "is_running", lambda pid: True)

    later = time.time() + store.DEAD_PROCESS_GRACE_SECONDS + 60
    assert store.flag_stale(now=later) == []
    assert store.get(CLIENT).status == store.BOOKING


def test_maintenance_runs_without_a_slot_check(store_dir, monkeypatch):
    """It used to happen only inside a route check, so with the scheduler off
    nothing ever expired or unstuck."""
    monkeypatch.setattr(autobook, "_LAST_MAINTENANCE", 0.0)
    monkeypatch.setattr(autobook, "_telegram", lambda text: None)
    store.create("old-ae-nor-0002", {
        "route": "AE-NOR", "combo": "x", "date_from": "2020-01-01",
        "date_to": "2020-01-31", "first_name": "A", "last_name": "B"})

    assert autobook.maintain(force=True)["expired"] == ["old-ae-nor-0002"]
    assert store.get("old-ae-nor-0002").status == store.EXPIRED


def test_maintenance_is_cheap_enough_to_call_on_every_read(store_dir,
                                                           monkeypatch):
    calls = []
    monkeypatch.setattr(autobook, "_LAST_MAINTENANCE", 0.0)
    monkeypatch.setattr(store, "expire_past", lambda: calls.append(1) or [])
    monkeypatch.setattr(store, "flag_stale", lambda: [])

    autobook.maintain()
    autobook.maintain()
    autobook.maintain()
    assert len(calls) == 1          # rate-limited, not re-scanned each time


def test_maintenance_never_raises(store_dir, monkeypatch):
    monkeypatch.setattr(autobook, "_LAST_MAINTENANCE", 0.0)
    monkeypatch.setattr(store, "expire_past",
                        lambda: (_ for _ in ()).throw(OSError("disk")))
    monkeypatch.setattr(store, "flag_stale", lambda: [])
    assert autobook.maintain()["expired"] == []


# --------------------------------------------------------------------------- #
# Money is matched by run, not by position                                     #
# --------------------------------------------------------------------------- #

def test_payment_rows_are_matched_to_the_run_that_wrote_them():
    """Slicing the journal by position is only right while exactly one process
    can write — and this decides whether a card was charged."""
    rows = [{"run_id": "mine", "event": "payment_submitting"},
            {"run_id": "someone-else", "event": "payment_submitting"},
            {"run_id": "mine", "event": "payment_result", "outcome": "success"}]

    mine = autobook.rows_for_run(rows, "mine")
    assert [r["event"] for r in mine] == ["payment_submitting", "payment_result"]
    assert autobook.rows_for_run(rows, "nobody") == []


def test_a_payment_row_with_no_run_id_is_never_dropped():
    """An unattributed payment row is the one worth being loud about."""
    rows = [{"event": "payment_submitting"}]
    assert autobook.rows_for_run(rows, "mine") == rows


# --------------------------------------------------------------------------- #
# History keeps what matters                                                   #
# --------------------------------------------------------------------------- #

def test_trimming_keeps_the_money_and_drops_the_noise(monkeypatch):
    monkeypatch.setattr(store, "HISTORY_LIMIT", 5)
    record = {}
    store._append(record, "created")
    store._append(record, "booked", "booked and paid")
    for n in range(20):
        store._append(record, "skipped", "nothing to do")

    events = [e["event"] for e in record["history"]]
    assert "created" in events and "booked" in events
    assert len(record["history"]) == 7          # 5 recent + the 2 kept forever


def test_last_event_at_moves_even_when_the_status_does_not(store_dir):
    """Two failures in a row both leave the status at 'waiting', so a web app
    sorting by activity saw nothing move."""
    _make()
    store.record_event(CLIENT, "attempt_failed", "one", status=store.WAITING)
    first = store.get(CLIENT).data["last_event_at"]
    store.record_event(CLIENT, "skipped", "two")
    req = store.get(CLIENT)

    assert req.data["last_event_at"] >= first
    assert req.data["last_event_at"] == req.data["history"][-1]["at"]


# --------------------------------------------------------------------------- #
# One shape of timestamp                                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("given, want", [
    ("2026-10-23T08:31:16Z", "2026-10-23T08:31:16Z"),      # history
    ("2026-10-24T06:28:19.123456+00:00", "2026-10-24T06:28:19Z"),  # job record
    ("2026-10-24T10:28:19+04:00", "2026-10-24T06:28:19Z"),  # a local stamp
    ("2026-10-24T06:28:19", "2026-10-24T06:28:19Z"),        # naive
])
def test_every_timestamp_the_web_app_sees_is_utc(given, want):
    """The feed merges three sources that each spell time differently, and the
    bot's own logs are in local time. One shape, so a web app converts once."""
    from src.api.modules.clients import service

    assert service._utc_z(given) == want


def test_an_unreadable_timestamp_is_passed_through_not_invented():
    from src.api.modules.clients import service

    assert service._utc_z("sometime") == "sometime"
    assert service._utc_z(None) == ""


def test_minutes_since_last_attempt_reads_the_history(store_dir):
    _make()
    store.record_event(CLIENT, "attempt_started", "", status=store.BOOKING)
    req = store.get(CLIENT)

    assert autobook.minutes_since_last_attempt(req) < 1
    assert autobook.minutes_since_last_attempt(
        req, now=datetime.now(timezone.utc).replace(year=2027)) > 1000
    assert autobook.minutes_since_last_attempt(
        store.BookingRequest("x", {})) == float("inf")
