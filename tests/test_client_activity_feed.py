"""What the web app reads to show one client's progress.

Three things had to be true before an activity feed was possible, and none of
them was:

  1. every change is RECORDED — including the ticks that skipped the client,
     which used to reach Telegram and the log only ("waiting", no reason);
  2. every entry has a STABLE id, so a push and a later poll are the same
     event rather than two;
  3. an AUTOMATIC booking is visible, which means a job record and a log the
     jobs API already serves.
"""

from __future__ import annotations

import json

import pytest

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
# 1. A cursor the web app can trust                                            #
# --------------------------------------------------------------------------- #

def test_every_entry_gets_a_number_that_only_goes_up(store_dir):
    _make()
    for event in ("attempt_started", "attempt_failed", "skipped"):
        store.record_event(CLIENT, event, "x")

    history = store.get(CLIENT).data["history"]
    assert [e["seq"] for e in history] == [1, 2, 3, 4]      # created + three


def test_two_events_in_the_same_second_stay_distinguishable(store_dir,
                                                            monkeypatch):
    """`at` has second resolution. A cursor built on it would either repeat an
    event or lose one.

    The clock is frozen rather than hoped at: two record_event calls normally
    land in the same second, but when they straddled a boundary the
    "same second" precondition failed and the test reported a flake in the
    cursor rather than in its own setup.
    """
    monkeypatch.setattr(store, "_now", lambda: "2026-10-29T09:00:00Z")
    _make()
    store.record_event(CLIENT, "enabled")
    store.record_event(CLIENT, "disabled")

    history = store.get(CLIENT).data["history"]
    assert history[-1]["at"] == history[-2]["at"]           # same second
    assert history[-1]["seq"] != history[-2]["seq"]


def test_a_record_written_before_seq_existed_keeps_its_order():
    """The counter must continue PAST the old entries, not restart at 1 and
    hand a web app an id it has already seen."""
    old = {"history": [{"at": "t1", "event": "created"},
                       {"at": "t2", "event": "attempt_started"}]}
    entry = store._append(old, "attempt_failed", "boom")

    assert entry["seq"] == 3
    assert [e["seq"] for e in old["history"]] == [1, 2, 3]


def test_trimming_history_never_reuses_an_id(monkeypatch):
    """HISTORY_LIMIT drops the oldest entries; a length-based id would then be
    handed out twice and the web app would overwrite a live event with an old
    one."""
    monkeypatch.setattr(store, "HISTORY_LIMIT", 3)
    record = {}
    seen = [store._append(record, "e%d" % n)["seq"] for n in range(6)]

    assert seen == [1, 2, 3, 4, 5, 6]
    assert len(record["history"]) == 3


# --------------------------------------------------------------------------- #
# 2. The ticks that did NOT book                                               #
# --------------------------------------------------------------------------- #

def test_a_skipped_tick_says_why_and_changes_nothing(store_dir):
    """'Why was my client not booked?' must be answerable from the API."""
    from src.booking import autobook

    _make()
    autobook._skipped(CLIENT, "the card is not loadable", "2026-11-05")

    req = store.get(CLIENT)
    entry = req.data["history"][-1]
    assert entry["event"] == "skipped"
    assert entry["detail"] == "the card is not loadable"
    assert entry["seen_date"] == "2026-11-05"
    # Nothing was attempted, so nothing is spent and it stays bookable.
    assert req.status == store.WAITING and req.is_armed()
    assert req.data["attempts"] == 0


def test_a_skip_can_never_break_the_tick_that_reports_it(store_dir):
    from src.booking import autobook

    autobook._skipped("no-such-request", "a reason")       # must not raise


# --------------------------------------------------------------------------- #
# 3. The push                                                                  #
# --------------------------------------------------------------------------- #

def test_each_entry_is_pushed_to_the_web_app(store_dir, monkeypatch):
    from src.utils import webhook

    sent = []
    monkeypatch.setattr(webhook, "is_configured", lambda: True)
    monkeypatch.setattr(webhook, "notify_booking_update", sent.append)

    _make()
    store.record_event(CLIENT, "attempt_started", "earliest 2026-11-05",
                       status=store.BOOKING, run_id="6adb1aa3d6b455")

    assert len(sent) == 2                      # created, then the attempt
    pushed = sent[-1]
    assert pushed["event"] == "attempt_started"
    assert pushed["status"] == "booking"
    assert pushed["seq"] == 2 and pushed["at"]
    assert pushed["run_id"] == "6adb1aa3d6b455"
    assert pushed["request_id"] == CLIENT


def test_a_dead_web_app_never_stops_a_booking(store_dir, monkeypatch):
    """The push is freshness; the timeline is correctness."""
    from src.utils import webhook

    def explode(payload):
        raise RuntimeError("web app is down")

    monkeypatch.setattr(webhook, "is_configured", lambda: True)
    monkeypatch.setattr(webhook, "notify_booking_update", explode)
    _make()
    store.record_event(CLIENT, "booked", "booked and paid", status=store.BOOKED)

    assert store.get(CLIENT).status == store.BOOKED


def test_nothing_is_pushed_when_no_webhook_is_configured(store_dir, monkeypatch):
    from src.utils import webhook

    calls = []
    monkeypatch.setattr(webhook, "is_configured", lambda: False)
    monkeypatch.setattr(webhook, "notify_booking_update", calls.append)
    _make()
    store.record_event(CLIENT, "enabled")

    assert calls == []


# --------------------------------------------------------------------------- #
# 4. An automatic booking is a job                                             #
# --------------------------------------------------------------------------- #

def test_an_automatic_attempt_writes_the_job_record_the_api_serves(tmp_path,
                                                                  monkeypatch):
    from src.api.modules.jobs.manager import JobRecord
    from src.booking import attempt_job

    jobs_file = tmp_path / "jobs.jsonl"
    monkeypatch.setattr(attempt_job, "JOBS_FILE", str(jobs_file))
    monkeypatch.setattr(attempt_job, "LOG_DIR", str(tmp_path))

    attempt_job.started("6adb1aa3d6b455", request_id=CLIENT, route="AE-NOR",
                        combo="Dubai")
    attempt_job.finished("6adb1aa3d6b455", request_id=CLIENT, route="AE-NOR",
                         combo="Dubai", status="failed",
                         detail="VFS did not respond in time.",
                         outcome="failed")

    rows = [json.loads(line) for line in
            jobs_file.read_text(encoding="utf-8").splitlines()]
    assert [r["status"] for r in rows] == ["running", "failed"]

    # The API must be able to rebuild them, and to join them to the client.
    for row in rows:
        record = JobRecord.from_record(row)
        assert record is not None
        assert record.payload["request_id"] == CLIENT
        assert record.payload["lane"] == "booking"
        assert record.run_id == "6adb1aa3d6b455"
    assert rows[1]["detail"] == "VFS did not respond in time."


def test_a_job_record_is_never_worth_failing_a_booking_for(monkeypatch):
    """An unwritable history file must not take the booking down with it."""
    from src.booking import attempt_job

    monkeypatch.setattr(attempt_job, "JOBS_FILE", "")
    attempt_job.started("x", request_id=CLIENT, route="AE-NOR")


def test_each_queued_request_gets_its_own_run_id():
    """One spawn books several requests. Sharing one id would put one
    client's screenshots in another's folder."""
    from src.utils import run_context

    try:
        first = run_context.begin_run()
        second = run_context.begin_run()
        assert first != second
        assert run_context.run_id() == second
    finally:
        run_context.reset_for_tests()


# --------------------------------------------------------------------------- #
# 5. The endpoint the web app calls                                            #
# --------------------------------------------------------------------------- #

def test_the_timeline_returns_only_what_is_new(store_dir, monkeypatch):
    """The whole point of the cursor: a catch-up poll after a missed push
    must not re-send the client's entire history."""
    from src.api.modules.clients import service

    monkeypatch.setattr(service, "require_flow", lambda cid: service.LIVE)
    _make()
    store.record_event(CLIENT, "attempt_started", "going", status=store.BOOKING)
    store.record_event(CLIENT, "attempt_failed", "VFS did not respond in time.",
                       status=store.WAITING)

    everything = service.timeline(CLIENT)
    assert [e["id"] for e in everything] == ["req:3", "req:2", "req:1"]

    newest = int(everything[0]["id"].split(":")[1])
    assert service.timeline(CLIENT, since=newest) == []
    assert [e["id"] for e in service.timeline(CLIENT, since=1)] == \
        ["req:3", "req:2"]


def test_timeline_errors_are_one_public_sentence(store_dir, monkeypatch):
    """The feed is rendered to a sales agent, so it must never carry a
    Playwright call log."""
    from src.api.modules.clients import service

    monkeypatch.setattr(service, "require_flow", lambda cid: service.LIVE)
    _make()
    store.record_event(
        CLIENT, "attempt_failed",
        "Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x\nCall log:",
        status=store.WAITING)

    newest = service.timeline(CLIENT)[0]
    assert "\n" not in newest["detail"] and "net::" not in newest["detail"]
