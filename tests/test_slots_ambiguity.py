"""Attributing a log line that names only its centre.

Until mid-August 2026 `slot_check` logged `Checking slot for: <centre>`, so a
centre with two categories produced two IDENTICAL lines per run — France's
'Abu Dhabi' covers both 'Short Stay - Business' and 'Short Stay (any purpose)'.

Two rules are encoded here, and they pull in opposite directions on purpose:

  * Such a label must never resolve by text. It used to: every combination
    claimed the bare centre as an alias and the primary key handed it to
    whichever was inserted first, filing the second category's readings under
    the first and inventing open/close transitions as the two series
    interleaved.
  * It may resolve by POSITION, because the bot walks a route file top to
    bottom — but only when the run holds as many readings of that label as the
    file has candidates. 85 runs in the real logs checked one of France's two,
    and there position proves nothing.
"""

import json
import os
import sys
from datetime import timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.slots import db, logreader, registry, seed  # noqa: E402
from src.slots.store import SlotStore  # noqa: E402

GST = timezone(timedelta(hours=4))

# France as the route file shipped it: one centre, two categories, so the bare
# centre cannot say which.
FRA = [
    {"label": "Abu Dhabi - Short Stay - Business",
     "centre": "Abu Dhabi", "category": "Short Stay - Business", "sub_category": ""},
    {"label": "Abu Dhabi - Short Stay (any purpose)",
     "centre": "Abu Dhabi", "category": "Short Stay (any purpose)", "sub_category": ""},
]


def _routes(tmp_path, combos=FRA, route="AE-FRA"):
    d = tmp_path / "routes"
    d.mkdir(exist_ok=True)
    (d / f"{route}.json").write_text(
        json.dumps({"mode": "slot-check", "slot_check": {"combinations": combos}}),
        encoding="utf-8")
    return str(d)


def _synced(tmp_path, combos=FRA, route="AE-FRA"):
    conn = db.connect(str(tmp_path / "a.db"))
    registry.sync(conn, _routes(tmp_path, combos, route))
    return conn


# ===== the alias must be refused, not awarded ==============================


def test_bare_centre_is_not_awarded_to_the_first_combination(tmp_path):
    conn = _synced(tmp_path)
    assert registry.resolve(conn, "AE-FRA", "Abu Dhabi") is None


def test_bare_centre_alias_is_absent_from_the_table(tmp_path):
    """Not merely unresolved — the ambiguous key must not be stored at all."""
    conn = _synced(tmp_path)
    row = conn.execute(
        "SELECT combo_id FROM label_aliases WHERE route='AE-FRA' AND label_key='abudhabi'"
    ).fetchone()
    assert row is None


def test_sync_reports_how_many_labels_it_refused(tmp_path):
    conn = db.connect(str(tmp_path / "a.db"))
    stats = registry.sync(conn, _routes(tmp_path))
    assert stats["ambiguous"] >= 1


def test_an_explicit_label_still_resolves(tmp_path):
    """Refusing the bare centre must not cost us the unambiguous spellings."""
    conn = _synced(tmp_path)
    business = registry.resolve(conn, "AE-FRA", "Abu Dhabi - Short Stay - Business")
    any_purpose = registry.resolve(conn, "AE-FRA", "Abu Dhabi - Short Stay (any purpose)")
    assert business and any_purpose and business != any_purpose


def test_a_single_category_centre_keeps_its_bare_alias(tmp_path):
    """Ambiguity is the trigger, not bareness: one candidate is still a match."""
    conn = _synced(tmp_path, combos=[
        {"label": "Dubai - Short Stay", "centre": "Dubai",
         "category": "Short Stay", "sub_category": ""},
    ], route="AE-SWE")
    assert registry.resolve(conn, "AE-SWE", "Dubai") is not None


def test_an_ambiguous_alias_left_by_an_older_build_is_cleared(tmp_path):
    """Databases in the field already hold the bad row; sync must repair them."""
    conn = _synced(tmp_path)
    first = conn.execute(
        "SELECT id FROM combos WHERE route='AE-FRA' ORDER BY config_order"
    ).fetchone()["id"]
    conn.execute("INSERT INTO label_aliases (label_key, route, combo_id, origin)"
                 " VALUES ('abudhabi', 'AE-FRA', ?, 'config')", (first,))
    conn.commit()
    assert registry.resolve(conn, "AE-FRA", "Abu Dhabi") == first   # poisoned

    registry.sync(conn, _routes(tmp_path))
    assert registry.resolve(conn, "AE-FRA", "Abu Dhabi") is None    # repaired


def test_a_manual_alias_is_left_alone(tmp_path):
    """A human who has decided what a label means outranks this heuristic."""
    conn = _synced(tmp_path)
    chosen = conn.execute(
        "SELECT id FROM combos WHERE route='AE-FRA' ORDER BY config_order DESC"
    ).fetchone()["id"]
    conn.execute("INSERT INTO label_aliases (label_key, route, combo_id, origin)"
                 " VALUES ('abudhabi', 'AE-FRA', ?, 'manual')", (chosen,))
    conn.commit()
    registry.sync(conn, _routes(tmp_path))
    assert registry.resolve(conn, "AE-FRA", "Abu Dhabi") == chosen


# ===== config_order ========================================================


def test_config_order_follows_the_route_file(tmp_path):
    conn = _synced(tmp_path)
    rows = conn.execute(
        "SELECT category, config_order FROM combos WHERE route='AE-FRA'"
        " ORDER BY config_order").fetchall()
    assert [r["config_order"] for r in rows] == [0, 1]
    assert rows[0]["category"] == "Short Stay - Business"


def test_a_disabled_entry_still_takes_its_position(tmp_path):
    """Position must mean the same in an old log as it does now.

    A disabled row is skipped by the bot today, but it was live when the old log
    was written, so renumbering around it would shift every later combination.
    """
    conn = _synced(tmp_path, combos=[
        {"label": "A", "centre": "Dubai", "category": "One", "sub_category": "",
         "disabled": True},
        {"label": "B", "centre": "Dubai", "category": "Two", "sub_category": ""},
    ], route="AE-CZE")
    rows = conn.execute(
        "SELECT category, config_order, enabled FROM combos WHERE route='AE-CZE'"
        " ORDER BY config_order").fetchall()
    assert [(r["category"], r["config_order"]) for r in rows] == [("One", 0), ("Two", 1)]
    assert rows[0]["enabled"] == 0


# ===== resolution by position ==============================================


def test_position_separates_the_two_readings(tmp_path):
    conn = _synced(tmp_path)
    first = registry.resolve(conn, "AE-FRA", "Abu Dhabi",
                             occurrence=1, occurrence_total=2)
    second = registry.resolve(conn, "AE-FRA", "Abu Dhabi",
                              occurrence=2, occurrence_total=2)
    assert first and second and first != second

    order = [r["id"] for r in conn.execute(
        "SELECT id FROM combos WHERE route='AE-FRA' ORDER BY config_order")]
    assert [first, second] == order


def test_a_partial_run_is_refused_rather_than_guessed(tmp_path):
    """One reading where two were expected: which one is unknowable."""
    conn = _synced(tmp_path)
    assert registry.resolve(conn, "AE-FRA", "Abu Dhabi",
                            occurrence=1, occurrence_total=1) is None


def test_more_readings_than_candidates_is_refused(tmp_path):
    conn = _synced(tmp_path)
    assert registry.resolve(conn, "AE-FRA", "Abu Dhabi",
                            occurrence=3, occurrence_total=3) is None


def test_position_never_overrides_an_explicit_label(tmp_path):
    """A label that names its category is resolved by that, whatever its slot."""
    conn = _synced(tmp_path)
    by_text = registry.resolve(conn, "AE-FRA", "Abu Dhabi - Short Stay (any purpose)")
    with_position = registry.resolve(
        conn, "AE-FRA", "Abu Dhabi - Short Stay (any purpose)",
        occurrence=1, occurrence_total=2)
    assert with_position == by_text


def test_position_is_not_remembered_as_an_alias(tmp_path):
    """It resolved one reading, not the label — the next run may differ."""
    conn = _synced(tmp_path)
    registry.resolve(conn, "AE-FRA", "Abu Dhabi", occurrence=2, occurrence_total=2)
    row = conn.execute(
        "SELECT combo_id FROM label_aliases WHERE route='AE-FRA' AND label_key='abudhabi'"
    ).fetchone()
    assert row is None


def test_a_label_naming_another_city_is_still_refused(tmp_path):
    """Position is a tie-breaker between candidates, never a way to invent one."""
    conn = _synced(tmp_path)
    assert registry.resolve(conn, "AE-FRA", "Mars", occurrence=1, occurrence_total=2) is None


# ===== the log reader numbers what it reads ================================


def _log(*lines):
    return list(lines)


RUN = _log(
    "[2026-07-22 12:34:00,000] INFO [supervisor.py:421] ########## Route 1/6: AE-FRA ##########",
    "[2026-07-22 12:34:20,762] INFO [slot_check.py:233] Checking slot for: Abu Dhabi",
    "[2026-07-22 12:34:43,197] INFO [slot_check.py:256]   -> No slot message shown (no availability?).",
    "[2026-07-22 12:34:43,604] INFO [slot_check.py:233] Checking slot for: Abu Dhabi",
    "[2026-07-22 12:35:02,854] INFO [slot_check.py:256]   -> Earliest available slot for 1 Applicants is : 16-09-2026",
    "[2026-07-22 12:35:10,000] INFO [supervisor.py:432] Route AE-FRA OK.",
)


def test_checks_are_numbered_within_their_run():
    checks = [r for r in logreader.iter_events(RUN, GST) if r["kind"] == "check"]
    assert [(c["occurrence"], c["occurrence_total"]) for c in checks] == [(1, 2), (2, 2)]


def test_run_boundaries_still_bracket_the_checks():
    """Buffering must not reorder the stream the seeder depends on."""
    kinds = [r["kind"] for r in logreader.iter_events(RUN, GST)]
    assert kinds == ["run_start", "check", "check", "run_end"]


def test_multi_line_results_survive_buffering():
    lines = _log(
        "[2026-07-22 12:34:00,000] INFO [supervisor.py:421] ########## Route 1/6: AE-FRA ##########",
        "[2026-07-22 12:34:20,762] INFO [slot_check.py:233] Checking slot for: Abu Dhabi",
        "[2026-07-22 12:34:43,197] INFO [slot_check.py:256]   -> Earliest available slot for 1 Applicants is : 16-09-2026",
        "Earliest available slot for 2 Applicants is : 21-09-2026",
        "[2026-07-22 12:35:10,000] INFO [supervisor.py:432] Route AE-FRA OK.",
    )
    checks = [r for r in logreader.iter_events(lines, GST) if r["kind"] == "check"]
    assert len(checks) == 1
    assert "21-09-2026" in checks[0]["message"]


def test_a_run_killed_before_its_end_line_still_yields_its_checks():
    lines = RUN[:-1] + _log(
        "[2026-07-22 12:40:00,000] INFO [supervisor.py:421] ########## Route 2/6: AE-NOR ##########")
    kinds = [r["kind"] for r in logreader.iter_events(lines, GST)]
    assert kinds == ["run_start", "check", "check", "run_start"]


def test_counts_are_per_run_not_per_file():
    lines = RUN + RUN
    checks = [r for r in logreader.iter_events(lines, GST) if r["kind"] == "check"]
    assert [c["occurrence"] for c in checks] == [1, 2, 1, 2]
    assert all(c["occurrence_total"] == 2 for c in checks)


# ===== end to end ==========================================================


def test_seeding_an_old_log_splits_the_two_categories(tmp_path):
    """The regression this whole module exists for.

    Both readings used to land on one combination, 19 seconds apart, which the
    transition detector then read as the slot opening and closing.
    """
    log = tmp_path / "app-2026-07-22.log"
    log.write_text("\n".join(RUN) + "\n", encoding="utf-8")

    store = SlotStore.open(str(tmp_path / "a.db"))
    registry.sync(store.conn, _routes(tmp_path))
    stats = seed.seed(store, [str(log)], tz=GST)

    assert stats["stored"] == 2
    rows = store.conn.execute(
        "SELECT cb.category, ch.outcome FROM checks ch"
        " JOIN combos cb ON cb.id = ch.combo_id ORDER BY cb.config_order").fetchall()
    assert [r["category"] for r in rows] == ["Short Stay - Business",
                                            "Short Stay (any purpose)"]
    assert [r["outcome"] for r in rows] == ["none", "slot"]

    # Two different combinations, so there is no transition to report.
    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    store.close()


def test_a_partial_old_run_is_parked_not_misfiled(tmp_path):
    lines = _log(
        "[2026-07-22 12:34:00,000] INFO [supervisor.py:421] ########## Route 1/6: AE-FRA ##########",
        "[2026-07-22 12:34:20,762] INFO [slot_check.py:233] Checking slot for: Abu Dhabi",
        "[2026-07-22 12:34:43,197] INFO [slot_check.py:256]   -> No slot message shown (no availability?).",
        "[2026-07-22 12:35:10,000] INFO [supervisor.py:432] Route AE-FRA OK.",
    )
    log = tmp_path / "app-2026-07-23.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    store = SlotStore.open(str(tmp_path / "a.db"))
    registry.sync(store.conn, _routes(tmp_path))
    seed.seed(store, [str(log)], tz=GST)

    assert store.conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == 0
    parked = store.conn.execute(
        "SELECT label, hits FROM unmapped_labels").fetchone()
    assert parked["label"] == "Abu Dhabi" and parked["hits"] == 1
    store.close()


def test_a_modern_log_is_unaffected(tmp_path):
    """The current format names its category and must not touch any of this."""
    lines = _log(
        "[2026-09-17 00:04:02,000] INFO [supervisor.py:685] ########## Route 3/10: AE-FRA ##########",
        "[2026-09-17 00:04:35,946] INFO [slot_check.py:429] Checking slot for: Abu Dhabi - Short Stay - Business",
        "[2026-09-17 00:04:44,886] INFO [slot_check.py:466]   -> Earliest available slot for 1,2 applicants is : 12-10-2026",
        "[2026-09-17 00:04:45,308] INFO [slot_check.py:429] Checking slot for: Abu Dhabi - Short Stay (any purpose)",
        "[2026-09-17 00:05:35,630] INFO [slot_check.py:466]   -> No slot message shown (no availability?).",
        "[2026-09-17 00:06:31,932] INFO [supervisor.py:722] Route AE-FRA OK.",
    )
    log = tmp_path / "app-2026-09-17.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    store = SlotStore.open(str(tmp_path / "a.db"))
    registry.sync(store.conn, _routes(tmp_path))
    stats = seed.seed(store, [str(log)], tz=GST)

    assert stats["stored"] == 2
    rows = store.conn.execute(
        "SELECT cb.category, ch.outcome FROM checks ch"
        " JOIN combos cb ON cb.id = ch.combo_id ORDER BY cb.config_order").fetchall()
    assert [r["outcome"] for r in rows] == ["slot", "none"]
    store.close()


# ===== a route file's own label outranks a derived variant ==================

# Greece names one combination after its centre, and that same string is the
# bare centre of the other Dubai combination. The explicit label is not in
# doubt and must not be refused as though it were.
GRC = [
    {"label": "Greece Visa Application Centre - Abu Dhabi",
     "centre": "Greece Visa Application Centre - Abu Dhabi",
     "category": "Short Stay", "sub_category": "General"},
    {"label": "Greece Visa Application center-Dubai",
     "centre": "Greece Visa Application center-Dubai",
     "category": "Prime Time", "sub_category": "Primetime"},
    {"label": "Dubai - Short Stay - General",
     "centre": "Greece Visa Application center-Dubai",
     "category": "Short Stay", "sub_category": "General"},
]


def test_a_config_label_outranks_another_combos_bare_centre(tmp_path):
    conn = _synced(tmp_path, combos=GRC, route="AE-GRC")
    resolved = registry.resolve(conn, "AE-GRC", "Greece Visa Application center-Dubai")
    expected = conn.execute(
        "SELECT id FROM combos WHERE route='AE-GRC' AND category='Prime Time'"
    ).fetchone()["id"]
    assert resolved == expected


def test_two_combos_sharing_one_config_label_are_still_refused(tmp_path):
    """Precedence resolves a clash with a derived variant, not a real duplicate."""
    conn = _synced(tmp_path, combos=[
        {"label": "Dubai", "centre": "Dubai", "category": "One", "sub_category": ""},
        {"label": "Dubai", "centre": "Dubai", "category": "Two", "sub_category": ""},
    ], route="AE-LUX")
    assert registry.resolve(conn, "AE-LUX", "Dubai") is None


# ===== disabled combinations and the reading count =========================

# Norway's shape: two categories per centre, the business one switched off. The
# bot therefore logs the centre ONCE per run, so a count of one is not a partial
# run — it is the whole run.
NOR = [
    {"label": "Norway Visa Application Center - Dubai - Business", "disabled": True,
     "centre": "Norway Visa Application Center - Dubai",
     "category": "Short Stay", "sub_category": "Business"},
    {"label": "Norway Visa Application Center - Dubai - Tourist",
     "centre": "Norway Visa Application Center - Dubai",
     "category": "Short Stay", "sub_category": "Tourist"},
]


def test_one_reading_lands_on_the_only_enabled_candidate(tmp_path):
    conn = _synced(tmp_path, combos=NOR, route="AE-NOR")
    resolved = registry.resolve(conn, "AE-NOR", "Norway Visa Application Center - Dubai",
                                occurrence=1, occurrence_total=1)
    tourist = conn.execute(
        "SELECT id FROM combos WHERE route='AE-NOR' AND sub_category='Tourist'"
    ).fetchone()["id"]
    assert resolved == tourist


def test_two_readings_fall_back_to_every_candidate(tmp_path):
    """An old log predates the switch-off, so both rows were live then."""
    conn = _synced(tmp_path, combos=NOR, route="AE-NOR")
    first = registry.resolve(conn, "AE-NOR", "Norway Visa Application Center - Dubai",
                             occurrence=1, occurrence_total=2)
    second = registry.resolve(conn, "AE-NOR", "Norway Visa Application Center - Dubai",
                              occurrence=2, occurrence_total=2)
    order = [r["id"] for r in conn.execute(
        "SELECT id FROM combos WHERE route='AE-NOR' ORDER BY config_order")]
    assert [first, second] == order


def test_a_count_matching_neither_set_is_refused(tmp_path):
    conn = _synced(tmp_path, combos=NOR, route="AE-NOR")
    assert registry.resolve(conn, "AE-NOR", "Norway Visa Application Center - Dubai",
                            occurrence=3, occurrence_total=3) is None
