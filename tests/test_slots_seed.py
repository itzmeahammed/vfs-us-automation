"""Phase 2: reading checks back out of the bot's log files.

The fixture below is copied from a real run (AE-NOR and AE-FRA, 14 Sep 2026),
including the continuation lines that carry the second applicant's date — that
detail is the one most likely to be broken by a well-meaning refactor, so it is
asserted explicitly.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.slots import logreader, seed  # noqa: E402
from src.slots.store import SlotStore  # noqa: E402

GST = timezone(timedelta(hours=4))

LOG = """\
[2026-09-14 10:39:11,726] INFO [supervisor.py:648] ########## Route 8/10: AE-NOR ##########
[2026-09-14 10:39:11,729] INFO [credentials.py:204] Using credential 4/8 available for AE-NOR (run #3): pa***@travnook.com
[2026-09-14 10:39:12,591] INFO [proxy_pool.py:261] proxyseller ip used here : 86.97.65.29:10005  [AE]  (patricia on AE-NOR)
[2026-09-14 10:39:44,971] INFO [slot_check.py:380] Skipping 2 disabled combination(s).
[2026-09-14 10:39:46,340] INFO [slot_check.py:428] Checking slot for: Norway Visa Application Center - Abu Dhabi - Tourist
[2026-09-14 10:40:00,174] INFO [slot_check.py:465]   -> Earliest available slot for 1 Applicants is : 16-09-2026
Earliest available slot for 2 Applicants is : 21-09-2026
[2026-09-14 10:40:00,581] INFO [slot_check.py:428] Checking slot for: Norway Visa Application Center - Dubai - Tourist
[2026-09-14 10:40:44,153] INFO [slot_check.py:465]   -> WAITLIST — no slots; waitlist sign-up available
[2026-09-14 10:40:44,571] INFO [slot_check.py:517] Slot report:
🇳🇴 Norway - Abu Dhabi - Short Stay - Tourist:
  slot for 1 On : 16-09-2026
[2026-09-14 10:40:45,296] INFO [supervisor.py:685] Route AE-NOR OK.
[2026-09-14 10:40:45,343] INFO [supervisor.py:648] ########## Route 9/10: AE-FRA ##########
[2026-09-14 10:41:36,900] INFO [slot_check.py:428] Checking slot for: Abu Dhabi - Short Stay (any purpose)
[2026-09-14 10:41:50,000] INFO [slot_check.py:465]   -> ERROR: could not select centre 'Dubai'
[2026-09-14 10:42:23,852] INFO [supervisor.py:685] Route AE-FRA FAILED.
"""

ROUTES = {
    "AE-NOR": [
        {"centre": "Norway Visa Application Center - Abu Dhabi",
         "category": "Short Stay", "sub_category": "Tourist"},
        {"centre": "Norway Visa Application Center - Dubai",
         "category": "Short Stay", "sub_category": "Tourist"},
    ],
    "AE-FRA": [
        {"centre": "Abu Dhabi", "category": "Short Stay (any purpose)",
         "sub_category": ""},
    ],
}


@pytest.fixture
def routes_dir(tmp_path):
    d = tmp_path / "routes"
    d.mkdir()
    for route, combos in ROUTES.items():
        (d / f"{route}.json").write_text(
            json.dumps({"mode": "slot-check", "slot_check": {"combinations": combos}}),
            encoding="utf-8")
    return str(d)


@pytest.fixture
def log_path(tmp_path):
    p = tmp_path / "logs" / "app-2026-09-14.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(LOG, encoding="utf-8")
    return str(p)


@pytest.fixture
def store(tmp_path, routes_dir):
    s = SlotStore.open(str(tmp_path / "slots.db"))
    s.sync_combos(routes_dir)
    yield s
    s.close()


# ===== the reader ==========================================================


def test_reads_runs_and_checks_in_order():
    kinds = [e["kind"] for e in logreader.iter_events(LOG.splitlines(), GST)]
    assert kinds == ["run_start", "run_meta", "run_meta", "check", "check",
                     "run_end", "run_start", "check", "run_end"]


def test_continuation_lines_are_glued_back_on():
    """The second applicant's date has no timestamp of its own."""
    checks = [e for e in logreader.iter_events(LOG.splitlines(), GST)
              if e["kind"] == "check"]
    assert "1 Applicants is : 16-09-2026" in checks[0]["message"]
    assert "2 Applicants is : 21-09-2026" in checks[0]["message"]


def test_the_slot_report_block_is_not_mistaken_for_a_result():
    """'Slot report:' is followed by unstamped lines too — they must be ignored."""
    checks = [e for e in logreader.iter_events(LOG.splitlines(), GST)
              if e["kind"] == "check"]
    assert len(checks) == 3
    assert all("slot for 1 On" not in c["message"] for c in checks)


def test_a_result_is_stamped_when_the_banner_was_read():
    check = next(e for e in logreader.iter_events(LOG.splitlines(), GST)
                 if e["kind"] == "check")
    assert check["ts"] == datetime(2026, 9, 14, 10, 40, 0, tzinfo=GST)


def test_run_status_is_picked_up():
    ends = [e for e in logreader.iter_events(LOG.splitlines(), GST)
            if e["kind"] == "run_end"]
    assert [(e["route"], e["status"]) for e in ends] == [("AE-NOR", "OK"),
                                                         ("AE-FRA", "FAILED")]


def test_log_files_are_filtered_by_their_date(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    for day in ("2026-09-08", "2026-09-12", "2026-09-14"):
        (logs / f"app-{day}.log").write_text("", encoding="utf-8")
    picked = logreader.log_files(str(logs / "app-*.log"), days=3,
                                 today=datetime(2026, 9, 14))
    assert [os.path.basename(p) for p in picked] == ["app-2026-09-12.log",
                                                     "app-2026-09-14.log"]


# ===== seeding =============================================================


def test_seeds_checks_runs_and_dates(store, log_path):
    stats = seed.seed(store, [log_path], tz=GST)
    assert stats["checks"] == 3
    assert stats["stored"] == 3
    assert stats["unmapped"] == 0

    rows = store.conn.execute(
        "SELECT c.outcome, co.country_name, co.city, co.visa_type"
        " FROM checks c JOIN combos co ON co.id = c.combo_id"
        " ORDER BY c.ts_utc").fetchall()
    assert [tuple(r) for r in rows] == [
        ("slot", "Norway", "Abu Dhabi", "Short Stay - Tourist"),
        ("waitlist", "Norway", "Dubai", "Short Stay - Tourist"),
        ("error", "France", "Abu Dhabi", "Short Stay (any purpose)"),
    ]

    dates = store.conn.execute(
        "SELECT applicants, slot_date, lead_days FROM slot_dates ORDER BY applicants"
    ).fetchall()
    assert [tuple(d) for d in dates] == [(1, "2026-09-16", 2), (2, "2026-09-21", 7)]


def test_checks_are_linked_to_their_run(store, log_path):
    seed.seed(store, [log_path], tz=GST)
    row = store.conn.execute(
        "SELECT r.route, r.status, r.account_masked, r.proxy_label, COUNT(c.id) AS n"
        " FROM runs r LEFT JOIN checks c ON c.run_id = r.id"
        " WHERE r.route = 'AE-NOR' GROUP BY r.id").fetchone()
    assert row["status"] == "OK"
    assert row["account_masked"] == "pa***@travnook.com"
    assert row["proxy_label"] == "86.97.65.29:10005"
    assert row["n"] == 2


def test_error_checks_keep_their_reason(store, log_path):
    seed.seed(store, [log_path], tz=GST)
    row = store.conn.execute(
        "SELECT error_reason FROM checks WHERE outcome = 'error'").fetchone()
    assert row["error_reason"] == "could not select centre 'Dubai'"


def test_seeding_twice_changes_nothing(store, log_path):
    first = seed.seed(store, [log_path], tz=GST)
    before = store.counts()
    second = seed.seed(store, [log_path], tz=GST)
    assert second["stored"] == 0
    assert second["duplicates"] == first["stored"]
    assert store.counts() == before


def test_a_backfill_does_not_collide_with_live_rows(store, log_path):
    """The live path recorded a check; the seeder must not double it."""
    store.record_check(
        "AE-NOR",
        "Earliest available slot for 1 Applicants is : 16-09-2026\n"
        "Earliest available slot for 2 Applicants is : 21-09-2026",
        combo=ROUTES["AE-NOR"][0],
        ts=datetime(2026, 9, 14, 10, 40, 0, tzinfo=GST))
    stats = seed.seed(store, [log_path], tz=GST)
    assert stats["stored"] == 2          # the other two, not this one
    assert store.counts()["checks"] == 3


def test_transitions_appear_across_seeded_runs(store, tmp_path):
    """Two runs an hour apart: nothing, then a slot -> one opening."""
    log = tmp_path / "logs" / "app-2026-09-14.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("""\
[2026-09-14 09:00:00,000] INFO [supervisor.py:648] ########## Route 1/1: AE-NOR ##########
[2026-09-14 09:00:10,000] INFO [slot_check.py:428] Checking slot for: Norway Visa Application Center - Dubai - Tourist
[2026-09-14 09:00:20,000] INFO [slot_check.py:465]   -> No slot message shown (no availability?).
[2026-09-14 09:00:30,000] INFO [supervisor.py:685] Route AE-NOR OK.
[2026-09-14 10:00:00,000] INFO [supervisor.py:648] ########## Route 1/1: AE-NOR ##########
[2026-09-14 10:00:10,000] INFO [slot_check.py:428] Checking slot for: Norway Visa Application Center - Dubai - Tourist
[2026-09-14 10:00:20,000] INFO [slot_check.py:465]   -> Earliest available slot for 1 Applicants is : 20-09-2026
[2026-09-14 10:00:30,000] INFO [supervisor.py:685] Route AE-NOR OK.
""", encoding="utf-8")
    seed.seed(store, [str(log)], tz=GST)
    row = store.conn.execute(
        "SELECT kind, new_date, hour_local, gap_hours FROM events").fetchone()
    assert (row["kind"], row["new_date"], row["hour_local"]) == ("opened", "2026-09-20", 10)
    assert row["gap_hours"] == 1.0


def test_labels_outside_the_config_are_parked(store, tmp_path):
    log = tmp_path / "logs" / "app-2026-09-14.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("""\
[2026-09-14 09:00:00,000] INFO [supervisor.py:648] ########## Route 1/1: AE-NOR ##########
[2026-09-14 09:00:10,000] INFO [slot_check.py:428] Checking slot for: Sharjah - Short Stay - Tourist
[2026-09-14 09:00:20,000] INFO [slot_check.py:465]   -> No slot message shown (no availability?).
[2026-09-14 09:00:30,000] INFO [supervisor.py:685] Route AE-NOR OK.
""", encoding="utf-8")
    stats = seed.seed(store, [str(log)], tz=GST)
    assert stats["stored"] == 0
    assert stats["unmapped"] == 1
    assert store.counts()["checks"] == 0
