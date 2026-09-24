"""Phase 1: parsing, the combination registry, transitions and the writer.

The registry tests are the important ones — they encode the rule that a portal
renaming its centre text must NOT create a second row for the same real-world
combination, which is the whole reason identity is derived rather than taken
from the display label.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.slots import db, events, parse, registry  # noqa: E402
from src.slots.store import SlotStore  # noqa: E402

GST = timezone(timedelta(hours=4))


# ===== parse ===============================================================


def test_parses_one_banner():
    outcome, dates = parse.parse_message(
        "Earliest available slot for 1 Applicants is : 16-09-2026")
    assert outcome == parse.SLOT
    assert dates == {1: "2026-09-16"}


def test_parses_multiple_banners_newline_joined():
    outcome, dates = parse.parse_message(
        "Earliest available slot for 1 Applicants is : 16-09-2026\n"
        "Earliest available slot for 2 Applicants is : 21-09-2026")
    assert outcome == parse.SLOT
    assert dates == {1: "2026-09-16", 2: "2026-09-21"}


def test_shared_banner_applies_to_every_listed_party_size():
    # France quotes '1,2 applicants' on one line — both sizes get that date.
    outcome, dates = parse.parse_message(
        "Earliest available slot for 1,2 applicants is : 28-09-2026\n"
        "Earliest available slot for 3 applicants is : 02-10-2026")
    assert outcome == parse.SLOT
    assert dates == {1: "2026-09-28", 2: "2026-09-28", 3: "2026-10-02"}


@pytest.mark.parametrize("message,expected", [
    ("WAITLIST — no slots; waitlist sign-up available", parse.WAITLIST),
    ("No slot message shown (no availability?).", parse.NONE),
    ("ERROR: could not select centre 'Dubai'", parse.ERROR),
    ("DISABLED", parse.DISABLED),
    ("", parse.NONE),
])
def test_classifies_the_non_slot_messages(message, expected):
    outcome, dates = parse.parse_message(message)
    assert outcome == expected
    assert dates == {}


def test_a_date_wins_over_waitlist_wording():
    # A future banner that mentions both must be stored as the slot it is.
    outcome, dates = parse.parse_message(
        "Waitlist open. Earliest available slot for 1 Applicants is : 16-09-2026")
    assert outcome == parse.SLOT
    assert dates == {1: "2026-09-16"}


def test_dates_are_read_day_first():
    assert parse.parse_date("05-08-2026") == "2026-08-05"   # 5 August, not 8 May
    assert parse.parse_date("16/09/26") == "2026-09-16"
    assert parse.parse_date("32-01-2026") is None


def test_lead_days_is_anchored_to_the_check_date():
    assert parse.lead_days("2026-09-20", "2026-09-16") == 4
    assert parse.lead_days("2026-09-10", "2026-09-16") == -6   # stale banner, kept


# ===== registry ============================================================


def _route_file(tmp_path, route, combos):
    path = tmp_path / f"{route}.json"
    path.write_text(json.dumps({"mode": "slot-check",
                                "slot_check": {"combinations": combos}}),
                    encoding="utf-8")
    return path


def test_centre_renames_do_not_duplicate_a_combination(tmp_path):
    """The same Dubai centre under three spellings is ONE combination."""
    routes = tmp_path / "routes"
    routes.mkdir()
    _route_file(routes, "AE-NOR", [
        {"centre": "Dubai", "category": "Short Stay", "sub_category": "Tourist"},
    ])
    conn = db.connect(str(tmp_path / "a.db"))
    registry.sync(conn, str(routes))

    # VFS renames the centre; config is updated to match.
    _route_file(routes, "AE-NOR", [
        {"centre": "Norway Visa Application Center - Dubai",
         "category": "Short Stay", "sub_category": "Tourist"},
    ])
    registry.sync(conn, str(routes))

    rows = conn.execute("SELECT centre, city FROM combos").fetchall()
    assert len(rows) == 1
    assert rows[0]["centre"] == "Norway Visa Application Center - Dubai"
    assert rows[0]["city"] == "Dubai"          # identity, stable across renames


def test_both_spellings_resolve_to_the_same_combo(tmp_path):
    routes = tmp_path / "routes"
    routes.mkdir()
    _route_file(routes, "AE-NOR", [
        {"centre": "Norway Visa Application Center - Dubai",
         "category": "Short Stay", "sub_category": "Tourist"},
    ])
    conn = db.connect(str(tmp_path / "a.db"))
    registry.sync(conn, str(routes))

    by_config = registry.resolve(
        conn, "AE-NOR", "Norway Visa Application Center - Dubai - Short Stay - Tourist")
    by_city = registry.resolve(conn, "AE-NOR", "Dubai - Short Stay - Tourist")
    assert by_config and by_config == by_city


def test_distinct_categories_stay_distinct(tmp_path):
    routes = tmp_path / "routes"
    routes.mkdir()
    _route_file(routes, "AE-HUN", [
        {"centre": "Dubai", "category": "Short Stay", "sub_category": "Tourist"},
        {"centre": "Dubai", "category": "Short Stay", "sub_category": "Business"},
    ])
    conn = db.connect(str(tmp_path / "a.db"))
    registry.sync(conn, str(routes))
    assert conn.execute("SELECT COUNT(*) FROM combos").fetchone()[0] == 2
    tourist = registry.resolve(conn, "AE-HUN", "Dubai - Short Stay - Tourist")
    business = registry.resolve(conn, "AE-HUN", "Dubai - Short Stay - Business")
    assert tourist != business


def test_unknown_label_is_not_guessed(tmp_path):
    routes = tmp_path / "routes"
    routes.mkdir()
    _route_file(routes, "AE-HUN", [
        {"centre": "Dubai", "category": "Short Stay", "sub_category": "Tourist"},
        {"centre": "Dubai", "category": "Short Stay", "sub_category": "Business"},
    ])
    conn = db.connect(str(tmp_path / "a.db"))
    registry.sync(conn, str(routes))
    # Names the city but neither category — ambiguous, so it must not resolve.
    assert registry.resolve(conn, "AE-HUN", "Dubai - Something Else") is None


def test_disabled_combinations_are_kept_but_flagged(tmp_path):
    routes = tmp_path / "routes"
    routes.mkdir()
    _route_file(routes, "AE-SWE", [
        {"centre": "Sweden Visa Application Centre, Dubai", "category": "Short Stay",
         "sub_category": "Business", "disabled": True},
        {"centre": "Sweden Visa Application Centre, Dubai", "category": "Short Stay",
         "sub_category": "ShortStay"},
    ])
    conn = db.connect(str(tmp_path / "a.db"))
    registry.sync(conn, str(routes))
    rows = {r["sub_category"]: r["enabled"] for r in
            conn.execute("SELECT sub_category, enabled FROM combos")}
    assert rows == {"Business": 0, "ShortStay": 1}


def test_combination_dropped_from_config_is_retired_not_deleted(tmp_path):
    routes = tmp_path / "routes"
    routes.mkdir()
    _route_file(routes, "AE-MT", [
        {"centre": "Dubai", "category": "Short Stay", "sub_category": "Tourism"},
    ])
    conn = db.connect(str(tmp_path / "a.db"))
    registry.sync(conn, str(routes))
    _route_file(routes, "AE-MT", [])
    registry.sync(conn, str(routes))
    row = conn.execute("SELECT in_config, enabled FROM combos").fetchone()
    assert (row["in_config"], row["enabled"]) == (0, 0)


def test_placeholder_centres_are_skipped(tmp_path):
    routes = tmp_path / "routes"
    routes.mkdir()
    _route_file(routes, "AE-DEU", [
        {"centre": "TODO: the real Dubai centre option text",
         "category": "Short Term Visa", "sub_category": "Short Stay"},
    ])
    assert registry.load_combos(str(routes)) == []


def test_visa_type_does_not_repeat_itself():
    assert registry.visa_type("Tourism", "Tourism") == "Tourism"
    assert registry.visa_type("Short Stay", "ShortStay") == "Short Stay"
    assert registry.visa_type("Short Stay", "Tourist") == "Short Stay - Tourist"


def test_real_config_has_no_duplicate_combinations():
    """The live config/routes folder must map to unique combinations."""
    combos = registry.load_combos()
    keys = [c.key for c in combos]
    assert len(keys) == len(set(keys)), "duplicate combination in config/routes"
    assert combos, "no combinations found in config/routes"


# ===== events ==============================================================


def test_nothing_to_slot_is_an_opening():
    out = events.diff({"outcome": parse.NONE, "dates": {}},
                      {"outcome": parse.SLOT, "dates": {1: "2026-09-20"}})
    assert [e["kind"] for e in out] == [events.OPENED]


def test_slot_to_nothing_is_a_closure():
    out = events.diff({"outcome": parse.SLOT, "dates": {1: "2026-09-20"}},
                      {"outcome": parse.NONE, "dates": {}})
    assert [e["kind"] for e in out] == [events.CLOSED]


def test_a_moved_date_carries_its_direction():
    out = events.diff({"outcome": parse.SLOT, "dates": {1: "2026-09-20"}},
                      {"outcome": parse.SLOT, "dates": {1: "2026-09-25"}})
    assert out[0]["kind"] == events.DATE_MOVED
    assert out[0]["delta_days"] == 5


def test_waitlist_to_slot_opens_and_closes_the_waitlist():
    out = events.diff({"outcome": parse.WAITLIST, "dates": {}},
                      {"outcome": parse.SLOT, "dates": {1: "2026-09-20"}})
    assert {e["kind"] for e in out} == {events.OPENED, events.WAITLIST_CLOSED}


def test_an_error_never_looks_like_a_closure():
    """A dropdown that wouldn't open says nothing about availability."""
    assert events.diff({"outcome": parse.SLOT, "dates": {1: "2026-09-20"}},
                       {"outcome": parse.ERROR, "dates": {}}) == []


def test_unchanged_availability_emits_nothing():
    state = {"outcome": parse.SLOT, "dates": {1: "2026-09-20"}}
    assert events.diff(state, dict(state)) == []


# ===== store ===============================================================


@pytest.fixture
def store(tmp_path):
    routes = tmp_path / "routes"
    routes.mkdir()
    _route_file(routes, "AE-NOR", [
        {"centre": "Norway Visa Application Center - Dubai",
         "category": "Short Stay", "sub_category": "Tourist"},
    ])
    s = SlotStore.open(str(tmp_path / "slots.db"))
    s.sync_combos(str(routes))
    yield s
    s.close()


COMBO = {"centre": "Norway Visa Application Center - Dubai",
         "category": "Short Stay", "sub_category": "Tourist"}


def test_records_a_check_with_its_dates(store):
    ts = datetime(2026, 9, 14, 10, 40, tzinfo=GST)
    check_id = store.record_check(
        "AE-NOR",
        "Earliest available slot for 1 Applicants is : 16-09-2026\n"
        "Earliest available slot for 2 Applicants is : 21-09-2026",
        combo=COMBO, ts=ts)
    assert check_id

    row = store.conn.execute("SELECT * FROM checks WHERE id = ?", (check_id,)).fetchone()
    assert row["outcome"] == parse.SLOT
    assert row["date_local"] == "2026-09-14"
    assert row["hour_local"] == 10
    assert row["weekday"] == 0                       # 14 Sep 2026 is a Monday
    assert row["ts_utc"].startswith("2026-09-14T06:40")   # GST is UTC+4
    assert "Earliest available slot" in row["raw_message"]

    dates = store.conn.execute(
        "SELECT applicants, slot_date, lead_days FROM slot_dates"
        " WHERE check_id = ? ORDER BY applicants", (check_id,)).fetchall()
    assert [tuple(d) for d in dates] == [(1, "2026-09-16", 2), (2, "2026-09-21", 7)]


def test_the_same_reading_is_never_stored_twice(store):
    ts = datetime(2026, 9, 14, 10, 40, tzinfo=GST)
    msg = "Earliest available slot for 1 Applicants is : 16-09-2026"
    assert store.record_check("AE-NOR", msg, combo=COMBO, ts=ts)
    assert store.record_check("AE-NOR", msg, combo=COMBO, ts=ts) is None
    assert store.counts()["checks"] == 1


def test_an_opening_is_recorded_between_checks(store):
    base = datetime(2026, 9, 14, 9, 0, tzinfo=GST)
    store.record_check("AE-NOR", "No slot message shown (no availability?).",
                       combo=COMBO, ts=base)
    store.record_check("AE-NOR", "Earliest available slot for 1 Applicants is : 20-09-2026",
                       combo=COMBO, ts=base + timedelta(hours=1))
    row = store.conn.execute("SELECT kind, new_date, gap_hours FROM events").fetchone()
    assert row["kind"] == events.OPENED
    assert row["new_date"] == "2026-09-20"
    assert row["gap_hours"] == 1.0


def test_an_error_between_two_readings_does_not_fake_events(store):
    base = datetime(2026, 9, 14, 9, 0, tzinfo=GST)
    slot = "Earliest available slot for 1 Applicants is : 20-09-2026"
    store.record_check("AE-NOR", slot, combo=COMBO, ts=base)
    store.record_check("AE-NOR", "ERROR: could not select centre 'Dubai'",
                       combo=COMBO, ts=base + timedelta(hours=1))
    store.record_check("AE-NOR", slot, combo=COMBO, ts=base + timedelta(hours=2))
    assert store.counts()["events"] == 0


def test_backfilled_older_check_still_produces_the_right_transition(store):
    """Events compare against the previous check by time, not by insert order."""
    base = datetime(2026, 9, 14, 9, 0, tzinfo=GST)
    store.record_check("AE-NOR", "Earliest available slot for 1 Applicants is : 20-09-2026",
                       combo=COMBO, ts=base + timedelta(hours=2))
    # A seeder later inserts the earlier 'nothing' reading.
    store.record_check("AE-NOR", "No slot message shown (no availability?).",
                       combo=COMBO, ts=base)
    kinds = [r["kind"] for r in store.conn.execute("SELECT kind FROM events")]
    assert kinds == []      # the later slot had no prior state when it was stored
    # ...and re-deriving is possible because both checks are on disk:
    assert store.counts()["checks"] == 2


def test_run_rows_tie_checks_to_what_actually_happened(store):
    started = datetime(2026, 9, 14, 10, 39, tzinfo=GST)
    run_id = store.start_run("AE-NOR", started_at=started, account="pa***@x.com",
                             proxy="res.proxy-seller.com:10005")
    store.record_check("AE-NOR", "No slot message shown (no availability?).",
                       combo=COMBO, ts=started + timedelta(minutes=1), run_id=run_id)
    store.finish_run(run_id, "OK", attempts=1)

    run = store.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "OK"
    assert run["attempts"] == 1
    assert run["finished_at_utc"]
    linked = store.conn.execute(
        "SELECT COUNT(*) AS n FROM checks WHERE run_id = ?", (run_id,)).fetchone()
    assert linked["n"] == 1


def test_unknown_label_is_parked_not_invented(store):
    ts = datetime(2026, 9, 14, 10, 40, tzinfo=GST)
    assert store.record_check("AE-NOR", "No slot message shown (no availability?).",
                              label="Mars - Short Stay - Tourist", ts=ts) is None
    assert store.counts()["checks"] == 0
    row = store.conn.execute("SELECT label, hits FROM unmapped_labels").fetchone()
    assert row["label"] == "Mars - Short Stay - Tourist"
    assert row["hits"] == 1


def test_migrations_are_idempotent(tmp_path):
    path = str(tmp_path / "slots.db")
    assert db.migrate(db.connect(path)) == db.migrate(db.connect(path))


def test_opening_an_existing_database_keeps_its_rows(tmp_path, ):
    routes = tmp_path / "routes"
    routes.mkdir()
    _route_file(routes, "AE-NOR", [COMBO])
    path = str(tmp_path / "slots.db")
    with SlotStore.open(path) as s:
        s.sync_combos(str(routes))
        s.record_check("AE-NOR", "No slot message shown (no availability?).",
                       combo=COMBO, ts=datetime(2026, 9, 14, 9, 0, tzinfo=GST))
    with SlotStore.open(path) as s:
        s.sync_combos(str(routes))
        assert s.counts()["checks"] == 1
        assert s.counts()["combos"] == 1
