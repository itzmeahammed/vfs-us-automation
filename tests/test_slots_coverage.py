"""Coverage measurement and the poll budget it feeds.

The rules worth protecting here are the ones that keep the numbers honest rather
than flattering:

  * A closed day is not a blind spot. The bot does not run on it and nothing
    opens, so counting it as unwatched invents a gap that costs nothing.
  * An episode seen once has a bracketed length, never a known one.
  * A combination with a handful of readings is not described. One lucky reading
    must not read as 'always open' — the scheduler would act on it.
  * The cadence a combination ACHIEVED is not its median gap. The median
    describes the healthy stretches; comparing a median before against a planned
    average after would credit a reallocation with a gain that is really two
    numbers meaning different things.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.slots import coverage, db, schedule  # noqa: E402

GST = timezone(timedelta(hours=4))
SUNDAY = frozenset({6})
NO_CLOSED = frozenset()


# ===== closed days =========================================================


def test_a_gap_entirely_on_a_closed_day_is_not_blind():
    # 2026-09-13 is a Sunday.
    start = datetime(2026, 9, 13, 2, 0, tzinfo=GST)
    end = datetime(2026, 9, 13, 20, 0, tzinfo=GST)
    assert coverage.closed_hours_between(start, end, SUNDAY) == pytest.approx(18.0)


def test_only_the_closed_part_of_a_gap_is_removed():
    # Saturday 22:00 to Monday 02:00 — 24 of those 28 hours are the Sunday.
    start = datetime(2026, 9, 12, 22, 0, tzinfo=GST)
    end = datetime(2026, 9, 14, 2, 0, tzinfo=GST)
    assert coverage.closed_hours_between(start, end, SUNDAY) == pytest.approx(24.0)


def test_no_closed_days_configured_removes_nothing():
    start = datetime(2026, 9, 13, 0, 0, tzinfo=GST)
    end = datetime(2026, 9, 14, 0, 0, tzinfo=GST)
    assert coverage.closed_hours_between(start, end, NO_CLOSED) == 0.0


def test_a_backwards_interval_is_zero():
    late = datetime(2026, 9, 13, 12, 0, tzinfo=GST)
    early = datetime(2026, 9, 13, 6, 0, tzinfo=GST)
    assert coverage.closed_hours_between(late, early, SUNDAY) == 0.0


# ===== a database to measure ===============================================


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    conn.execute(
        "INSERT INTO combos (combo_key, route, source_code, dest_code, country_name,"
        " centre, city, category, sub_category, visa_type, purpose, config_label,"
        " enabled, config_order, in_config)"
        " VALUES ('k1','AE-XXX','AE','XXX','Example','Dubai','Dubai','Short Stay',"
        " '','Short Stay','any','Dubai',1,0,1)")
    conn.commit()
    return conn


def _readings(conn, combo_id, start, outcomes, *, minutes=30):
    """Writes one reading per outcome, `minutes` apart, from `start` (local)."""
    when = start
    for outcome in outcomes:
        utc = when.astimezone(timezone.utc)
        conn.execute(
            "INSERT INTO checks (combo_id, ts_utc, ts_local, tz_offset_min,"
            " date_local, hour_local, weekday, outcome, raw_message, source)"
            " VALUES (?,?,?,?,?,?,?,?,'',?)",
            (combo_id, utc.isoformat(timespec="seconds"),
             when.isoformat(timespec="seconds"), 240,
             when.date().isoformat(), when.hour, when.weekday(), outcome, "live"),
        )
        when += timedelta(minutes=minutes)
    conn.commit()


def _recent(hours_ago: float) -> datetime:
    return datetime.now(GST) - timedelta(hours=hours_ago)


# ===== observation =========================================================


def test_regular_readings_inside_the_bar_are_fully_covered(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(10), ["none"] * 20, minutes=30)
    seen = coverage.observation(conn, days=7, resolution_hours=1.0, closed=NO_CLOSED)
    assert seen[1]["blind_hours"] == 0.0
    assert seen[1]["coverage"] == 1.0


def test_a_long_gap_is_mostly_blind(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(30), ["none"] * 3, minutes=600)   # 10h apart
    seen = coverage.observation(conn, days=7, resolution_hours=1.0, closed=NO_CLOSED)
    # Two gaps of 10h: 1h credited each, 9h blind each.
    assert seen[1]["covered_hours"] == pytest.approx(2.0)
    assert seen[1]["blind_hours"] == pytest.approx(18.0)
    assert seen[1]["worst_gap_hours"] == pytest.approx(10.0)


def test_a_single_reading_has_no_coverage_to_report(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(2), ["none"])
    seen = coverage.observation(conn, days=7, closed=NO_CLOSED)
    assert seen[1]["coverage"] is None


# ===== episodes ============================================================


def test_a_run_of_slot_readings_is_one_episode(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(10), ["none", "slot", "slot", "slot", "none"])
    eps = coverage.episodes(conn, days=7)[1]
    assert len(eps) == 1
    assert eps[0]["readings"] == 3
    assert eps[0]["observed_span_hours"] == pytest.approx(1.0)
    assert eps[0]["censored"] is False


def test_an_episode_seen_once_is_bracketed_not_measured(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(10), ["none", "slot", "none"])
    episode = coverage.episodes(conn, days=7)[1][0]
    assert episode["readings"] == 1
    assert episode["observed_span_hours"] == 0.0        # no lower bound at all
    assert episode["duration_max_hours"] == pytest.approx(1.0)   # the gap around it


def test_an_episode_still_open_at_the_end_is_censored(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(3), ["none", "slot", "slot"])
    assert coverage.episodes(conn, days=7)[1][0]["censored"] is True


def test_separate_episodes_are_not_merged(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(10),
              ["none", "slot", "none", "slot", "none"])
    assert len(coverage.episodes(conn, days=7)[1]) == 2


def test_resolution_needs_one_episode_seen_more_than_once(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(10), ["none", "slot", "none", "slot", "none"])
    assert coverage.resolved(coverage.episodes(conn, days=7)[1]) is False
    _readings(conn, 1, _recent(4), ["none", "slot", "slot", "none"])
    assert coverage.resolved(coverage.episodes(conn, days=7)[1]) is True


# ===== regime ==============================================================


def test_too_few_readings_is_sparse_not_a_verdict():
    assert coverage.regime(1, 1, 0, [{"readings": 1}]) == coverage.SPARSE


def test_nothing_read_is_unseen():
    assert coverage.regime(0, 0, 0, []) == coverage.UNSEEN


def test_always_open_is_persistent():
    assert coverage.regime(100, 100, 0, [{"readings": 100}]) == coverage.PERSISTENT


def test_waitlist_without_slots_is_waitlist_only():
    assert coverage.regime(100, 0, 100, []) == coverage.WAITLIST_ONLY


def test_neither_slots_nor_waitlist_is_dormant():
    assert coverage.regime(100, 0, 0, []) == coverage.DORMANT


def test_episodes_that_vanish_are_flash():
    brief = [{"readings": 1}, {"readings": 1}, {"readings": 2}]
    assert coverage.regime(100, 4, 0, brief) == coverage.FLASH


def test_episodes_that_persist_a_while_are_intermittent():
    lasting = [{"readings": 12}, {"readings": 9}, {"readings": 20}]
    assert coverage.regime(100, 41, 0, lasting) == coverage.INTERMITTENT


def test_a_regime_is_not_pinned_to_a_route(tmp_path):
    """The same route reclassifies when its behaviour changes."""
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(40), ["waitlist"] * 30, minutes=30)
    assert coverage.profile(conn, days=7)[0]["regime"] == coverage.WAITLIST_ONLY
    _readings(conn, 1, _recent(8), ["slot"] * 30, minutes=15)
    assert coverage.profile(conn, days=7)[0]["regime"] != coverage.WAITLIST_ONLY


# ===== detection arithmetic ================================================


def test_a_release_as_long_as_the_gap_is_always_caught():
    row = coverage.detection_sensitivity(0.5, [0.5])[0]
    assert row["catch_rate"] == 1.0
    assert row["implied_true_per_100_seen"] == 100.0


def test_a_release_a_quarter_of_the_gap_is_mostly_missed():
    row = coverage.detection_sensitivity(1.0, [0.25])[0]
    assert row["catch_rate"] == 0.25
    assert row["implied_true_per_100_seen"] == 400.0


def test_no_cadence_yields_no_curve():
    assert coverage.detection_sensitivity(0) == []


# ===== the budget ==========================================================


def test_water_fill_spends_the_whole_budget():
    rates = schedule.water_fill([3.0, 1.0], budget=8.0, floor=0.5, ceiling=10.0)
    assert sum(rates) == pytest.approx(8.0)
    assert rates[0] > rates[1]


def test_water_fill_respects_the_ceiling_and_redistributes():
    rates = schedule.water_fill([100.0, 1.0], budget=10.0, floor=0.1, ceiling=6.0)
    assert rates[0] == pytest.approx(6.0)
    assert sum(rates) == pytest.approx(10.0)


def test_water_fill_never_starves_a_combination():
    rates = schedule.water_fill([1.0, 0.0, 0.0], budget=4.0, floor=0.5, ceiling=9.0)
    assert min(rates) >= 0.5


def test_a_budget_too_small_for_the_floor_reports_rather_than_starves():
    rates = schedule.water_fill([1.0] * 10, budget=1.0, floor=0.5, ceiling=5.0)
    assert min(rates) == pytest.approx(0.5)
    assert sum(rates) > 1.0          # the caller is told it does not fit


def test_zero_budget_falls_back_to_the_floor():
    assert schedule.water_fill([1.0, 2.0], 0.0, floor=0.25, ceiling=4.0) == [0.25, 0.25]


def test_no_candidates_is_an_empty_allocation():
    assert schedule.water_fill([], 5.0, 0.1, 1.0) == []


# ===== priority and plan ===================================================


def test_a_fragile_combination_outranks_a_persistent_one(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO combos (combo_key, route, source_code, dest_code, country_name,"
        " centre, city, category, sub_category, visa_type, purpose, config_label,"
        " enabled, config_order, in_config)"
        " VALUES ('k2','AE-XXX','AE','XXX','Example','Abu Dhabi','Abu Dhabi',"
        " 'Short Stay','','Short Stay','any','Abu Dhabi',1,1,1)")
    conn.commit()
    # Combination 1 flashes: every episode is a single reading.
    _readings(conn, 1, _recent(40),
              (["none", "slot"] * 15) + ["none"] * 10, minutes=30)
    # Combination 2 is open throughout.
    _readings(conn, 2, _recent(40), ["slot"] * 40, minutes=30)

    scored = {r["combo_id"]: r for r in schedule.priorities(conn, days=7)}
    assert scored[1]["fragility"] > 0.8
    assert scored[1]["score"] > scored[2]["score"]


def test_an_unknown_combination_is_worth_exploring(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(2), ["none"])
    row = schedule.priorities(conn, days=7)[0]
    assert row["regime"] == coverage.SPARSE
    assert row["exploration"] == 1.0


def test_a_disabled_combination_gets_no_budget(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("UPDATE combos SET enabled = 0")
    conn.commit()
    _readings(conn, 1, _recent(10), ["none"] * 10)
    assert schedule.priorities(conn, days=7) == []


def test_every_planned_interval_is_inside_the_bounds(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(40), ["none", "slot"] * 20, minutes=30)
    result = schedule.plan(conn, days=7, readings_per_hour=2.0,
                           min_interval_min=5.0, max_interval_min=120.0)
    for row in result["combinations"]:
        assert 5.0 <= row["interval_minutes"] <= 120.0


def test_the_plan_reports_when_it_cannot_fit(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(10), ["none"] * 10)
    result = schedule.plan(conn, days=7, readings_per_hour=0.01,
                           max_interval_min=60.0)
    assert result["unmet"] is True


# ===== the measurement that corrected the plan =============================


def test_achieved_cadence_reflects_outages_not_the_good_stretches(tmp_path):
    """Half a day of 30-minute readings, then a day of silence.

    The median gap stays at 30 minutes. The cadence actually achieved is far
    worse, and that is the number a before-and-after comparison has to use.
    """
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(48), ["none"] * 24, minutes=30)   # 12h of work
    _readings(conn, 1, _recent(6), ["none"] * 2, minutes=30)     # back after a day

    prof = coverage.profile(conn, days=7, closed=NO_CLOSED)[0]
    achieved = schedule.achieved_interval_hours(prof)
    assert prof["median_gap_hours"] == pytest.approx(0.5)
    assert achieved > 1.0


def test_required_budget_names_the_shortfall(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(40), ["none", "slot"] * 20, minutes=30)
    need = schedule.required_budget(conn, days=7, target_interval_min=5.0)
    assert need["required_per_hour"] > need["current_per_hour"]
    assert need["shortfall_multiple"] > 1.0


def test_the_trade_is_reported_per_group_not_netted_off(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(40), ["none", "slot"] * 20, minutes=30)
    gain = schedule.expected_gain(conn, days=7)
    assert set(gain) >= {"detection", "confirmation", "assumed_duration_hours"}


# ===== capacity ============================================================


def _run(conn, started, minutes, *, readings=0, combo_id=1):
    """One finished run, optionally with readings attached to it."""
    finished = started + timedelta(minutes=minutes)
    cur = conn.execute(
        "INSERT INTO runs (route, source_code, dest_code, started_at_utc,"
        " finished_at_utc, status, source) VALUES ('AE-XXX','AE','XXX',?,?,'OK','live')",
        (started.astimezone(timezone.utc).isoformat(timespec="seconds"),
         finished.astimezone(timezone.utc).isoformat(timespec="seconds")),
    )
    run_id = cur.lastrowid
    when = started
    for _ in range(readings):
        utc = when.astimezone(timezone.utc)
        conn.execute(
            "INSERT INTO checks (combo_id, run_id, ts_utc, ts_local, tz_offset_min,"
            " date_local, hour_local, weekday, outcome, raw_message, source)"
            " VALUES (?,?,?,?,?,?,?,?,'none','','live')",
            (combo_id, run_id, utc.isoformat(timespec="seconds"),
             when.isoformat(timespec="seconds"), 240, when.date().isoformat(),
             when.hour, when.weekday(), ),
        )
        when += timedelta(seconds=20)
    conn.commit()
    return run_id


def test_capacity_bills_a_run_once_however_many_readings_it_made(tmp_path):
    """The rate is readings per run-MINUTE, not per reading-weighted minute.

    Joining runs to checks repeats each run's duration once per reading, which
    quietly divided the measured capacity by the average readings per run — and
    that number is what decides whether a target cadence needs a second worker.
    """
    conn = _conn(tmp_path)
    # Two runs, 10 minutes each, 5 readings apiece: 10 readings in 20 minutes.
    _run(conn, _recent(5), 10, readings=5)
    _run(conn, _recent(3), 10, readings=5)
    cap = schedule.capacity(conn, days=7)
    assert cap["readings_per_run_minute"] == pytest.approx(0.5)
    assert cap["sustained_per_hour"] == pytest.approx(30.0)


def test_capacity_reports_idle_time_as_headroom(tmp_path):
    conn = _conn(tmp_path)
    _run(conn, _recent(10), 6, readings=6)
    _run(conn, _recent(2), 6, readings=6)
    cap = schedule.capacity(conn, days=7)
    assert 0.0 < cap["busy_share"] < 1.0
    assert cap["idle_share"] == pytest.approx(1.0 - cap["busy_share"])


def test_a_run_that_produced_nothing_is_wasted_time(tmp_path):
    conn = _conn(tmp_path)
    _run(conn, _recent(8), 10, readings=10)
    _run(conn, _recent(4), 10, readings=0)
    cap = schedule.capacity(conn, days=7)
    assert cap["wasted_run_minutes"] == pytest.approx(10.0)
    assert cap["wasted_share_of_run_time"] == pytest.approx(0.5)
    # The wasted run must not drag the achievable rate down: capacity is what a
    # working run manages, and the empty one is the thing to fix, not a limit.
    assert cap["readings_per_run_minute"] == pytest.approx(1.0)


def test_no_finished_runs_reports_nothing_rather_than_guessing(tmp_path):
    conn = _conn(tmp_path)
    assert schedule.capacity(conn, days=7)["sustained_per_hour"] is None


def test_required_budget_answers_against_capacity_not_just_today(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(40), ["none", "slot"] * 20, minutes=30)
    _run(conn, _recent(6), 10, readings=30)
    need = schedule.required_budget(conn, days=7, target_interval_min=600.0)
    assert need["reachable_without_new_workers"] is True
    assert need["workers_needed"] == 1


# ===== hour-of-day coverage ================================================
#
# The per-combination figures cannot see an hour nobody looks at: a combination
# watched sixteen hours a day reads as well covered. These protect the measure
# that does see it, and the honesty of the projection built on top of it.


def _hourly(conn, combo_id, hours, *, days=5, per_hour=30, opened_in=()):
    """Readings in `hours` of the local clock on each of the last `days` days.

    `opened_in` names hours that also get one 'opened' event, so a per-hour rate
    can be asserted.
    """
    base = datetime.now(GST).replace(minute=0, second=0, microsecond=0)
    for day in range(1, days + 1):
        for hour in hours:
            when = (base - timedelta(days=day)).replace(hour=hour)
            # Spaced to fill exactly this hour: at minute granularity a count
            # above 60 would spill into the next hour and be credited to it.
            step = 3600.0 / per_hour
            for i in range(per_hour):
                ts = when + timedelta(seconds=i * step)
                utc = ts.astimezone(timezone.utc)
                cur = conn.execute(
                    "INSERT INTO checks (combo_id, ts_utc, ts_local, tz_offset_min,"
                    " date_local, hour_local, weekday, outcome, raw_message, source)"
                    " VALUES (?,?,?,?,?,?,?,'none','','live')",
                    (combo_id, utc.isoformat(timespec="seconds"),
                     ts.isoformat(timespec="seconds"), 240, ts.date().isoformat(),
                     ts.hour, ts.weekday()),
                )
                if i == 0 and hour in opened_in:
                    conn.execute(
                        "INSERT INTO events (combo_id, check_id, ts_utc, ts_local,"
                        " hour_local, weekday, kind, new_outcome)"
                        " VALUES (?,?,?,?,?,?,'opened','slots')",
                        (combo_id, cur.lastrowid, utc.isoformat(timespec="seconds"),
                         ts.isoformat(timespec="seconds"), ts.hour, ts.weekday()),
                    )
    conn.commit()


def test_an_hour_never_read_is_reported_blind(tmp_path):
    conn = _conn(tmp_path)
    _hourly(conn, 1, range(9, 24))
    prof = coverage.hour_profile(conn, days=30)
    assert prof["watched_hours"] == 15
    assert prof["blind_hours"] == list(range(0, 9))
    assert prof["clock_coverage"] == pytest.approx(15 / 24.0, abs=1e-3)


def test_the_uplift_is_the_share_of_the_clock_we_do_not_watch(tmp_path):
    """Watching two thirds of the clock means a third of releases are unseen."""
    conn = _conn(tmp_path)
    _hourly(conn, 1, range(0, 16))
    prof = coverage.hour_profile(conn, days=30)
    assert prof["watched_hours"] == 16
    assert prof["uplift_if_uniform"] == pytest.approx(1.5, abs=0.01)


def test_a_fully_watched_clock_claims_no_uplift(tmp_path):
    conn = _conn(tmp_path)
    _hourly(conn, 1, range(0, 24))
    prof = coverage.hour_profile(conn, days=30)
    assert prof["blind_hours"] == []
    assert prof["uplift_if_uniform"] == pytest.approx(1.0)


def test_a_barely_read_hour_counts_as_blind(tmp_path):
    """A handful of readings in an hour is not coverage of it.

    The bar is relative to the median watched hour, so it keeps its meaning when
    the bot's overall rate changes rather than being a count someone must retune.
    """
    conn = _conn(tmp_path)
    _hourly(conn, 1, range(9, 24), per_hour=40)
    _hourly(conn, 1, [3], per_hour=1)          # 5 readings against a median of 200
    prof = coverage.hour_profile(conn, days=30)
    assert 3 in prof["blind_hours"]
    hour3 = next(h for h in prof["hours"] if h["hour"] == 3)
    assert hour3["readings"] == 5 and hour3["blind"] is True


def test_openings_are_reported_per_reading_not_per_hour(tmp_path):
    """An hour read twice as often will see more openings for the same rate.

    Reporting raw counts per hour would make the busiest-watched hour look like
    the busiest-releasing one, which is exactly the bias that would send the
    scheduler to where we already look.
    """
    conn = _conn(tmp_path)
    _hourly(conn, 1, [10], days=4, per_hour=100, opened_in=[10])   # 400 reads, 4 opens
    _hourly(conn, 1, [11], days=4, per_hour=25, opened_in=[11])    # 100 reads, 4 opens
    prof = coverage.hour_profile(conn, days=30)
    hours = {h["hour"]: h for h in prof["hours"]}
    assert hours[10]["openings"] == hours[11]["openings"] == 4
    assert hours[10]["openings_per_1k"] == pytest.approx(10.0)
    assert hours[11]["openings_per_1k"] == pytest.approx(40.0)
    assert prof["busiest_hours"][0] == 11


def test_a_flat_rate_across_watched_hours_is_not_called_variation(tmp_path):
    """No evidence of a quiet hour is the point, not a weak finding.

    If the hours we watch differ no more than chance would produce, there is no
    ground to assume the hours we never watch are quiet — which is the assumption
    that would otherwise justify leaving them unwatched.
    """
    conn = _conn(tmp_path)
    _hourly(conn, 1, range(9, 24), days=6, per_hour=40,
            opened_in=range(9, 24))
    prof = coverage.hour_profile(conn, days=30)
    assert prof["rate_varies"] is False


def test_too_few_openings_refuses_a_verdict_on_variation(tmp_path):
    conn = _conn(tmp_path)
    _hourly(conn, 1, range(9, 24), days=2, per_hour=10, opened_in=[10])
    prof = coverage.hour_profile(conn, days=30)
    assert prof["rate_varies"] is None


def test_hour_coverage_reaches_the_summary(tmp_path):
    conn = _conn(tmp_path)
    _hourly(conn, 1, range(9, 24))
    head = coverage.summary(conn, days=30)
    assert head["clock"]["blind_hours"] == list(range(0, 9))


# ===== provenance of an empty run ==========================================
#
# 'No readings' means two different things. A run the bot performed and came
# back empty from is wasted effort. A run reconstructed from a log file whose
# check lines could not be attributed is a limit on what history can tell us,
# not time thrown away. Pooling them made the waste look nearly twice as bad as
# it is, and pointed the next piece of work at the wrong thing.


def _recovered_run(conn, started, minutes):
    """A run rebuilt from a log, carrying no readings."""
    finished = started + timedelta(minutes=minutes)
    conn.execute(
        "INSERT INTO runs (route, source_code, dest_code, started_at_utc,"
        " finished_at_utc, status, source)"
        " VALUES ('AE-XXX','AE','XXX',?,?,'OK','backfill')",
        (started.astimezone(timezone.utc).isoformat(timespec="seconds"),
         finished.astimezone(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()


def test_a_recovered_run_without_readings_is_not_counted_as_waste(tmp_path):
    conn = _conn(tmp_path)
    _run(conn, _recent(5), 2, readings=10)
    _recovered_run(conn, _recent(4), 2)
    _recovered_run(conn, _recent(3), 2)
    head = schedule.capacity(conn, days=30)
    assert head["wasted_run_minutes"] == 0
    assert head["wasted_share_of_run_time"] == pytest.approx(0.0)
    assert head["unattributed_runs"] == 2


def test_an_observed_run_without_readings_is_counted_as_waste(tmp_path):
    conn = _conn(tmp_path)
    _run(conn, _recent(5), 6, readings=10)
    _run(conn, _recent(4), 2, readings=0)
    head = schedule.capacity(conn, days=30)
    assert head["observed_runs"] == 2
    assert head["observed_empty_runs"] == 1
    assert head["wasted_run_minutes"] == pytest.approx(2.0)
    assert head["wasted_share_of_run_time"] == pytest.approx(0.25)


def test_waste_is_measured_against_observed_run_time_only(tmp_path):
    """The denominator has to match the numerator.

    Dividing observed waste by ALL run time — recovered runs included — would
    shrink the share towards nothing as more history is seeded, which is the
    mirror image of the error it replaced.
    """
    conn = _conn(tmp_path)
    _run(conn, _recent(6), 3, readings=5)
    _run(conn, _recent(5), 1, readings=0)
    for hour in (4, 3, 2):
        _recovered_run(conn, _recent(hour), 30)
    head = schedule.capacity(conn, days=30)
    assert head["wasted_share_of_run_time"] == pytest.approx(0.25)


def test_the_summary_separates_observed_runs_from_recovered_ones(tmp_path):
    conn = _conn(tmp_path)
    _readings(conn, 1, _recent(6), ["none", "slots", "none"])
    _run(conn, _recent(5), 2, readings=0)
    _recovered_run(conn, _recent(4), 2)
    head = coverage.summary(conn, days=30)
    assert head["observed_runs"] == 1
    assert head["observed_runs_without_readings"] == 1
    assert head["runs"] == 2
    assert head["runs_without_readings"] == 2


def test_readings_entirely_on_a_closed_day_still_report_a_throughput(tmp_path):
    """The readings win over the assumption about the day.

    Closed days are removed from the span, so a short window sitting inside one
    used to leave no open hours at all and report a throughput of zero — handing
    `plan` an empty budget on the evidence that the bot was busy. If readings
    exist, the bot ran, whatever the calendar says.
    """
    conn = _conn(tmp_path)
    sunday = datetime(2026, 9, 13, 10, 0, tzinfo=GST)      # a Sunday
    assert sunday.weekday() == 6
    _readings(conn, 1, sunday, ["none"] * 6, minutes=30)
    rate = schedule._current_throughput(conn, days=3650, closed=SUNDAY)
    assert rate > 0


def test_closed_days_are_still_removed_when_the_window_spans_them(tmp_path):
    """The fallback is a last resort, not a licence to ignore closed days."""
    conn = _conn(tmp_path)
    friday = datetime(2026, 9, 11, 12, 0, tzinfo=GST)
    assert friday.weekday() == 4
    # Friday noon to Monday noon: 72 hours of clock, 48 of them open.
    _readings(conn, 1, friday, ["none"] * 5, minutes=1080)
    with_closed = schedule._current_throughput(conn, days=3650, closed=SUNDAY)
    without = schedule._current_throughput(conn, days=3650, closed=NO_CLOSED)
    assert with_closed > without
