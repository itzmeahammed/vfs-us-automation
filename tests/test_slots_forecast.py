"""Forecasting the next opening from gaps, with no model and no training.

The rules worth protecting are the ones that stop this being a plausible-looking
lie on a dashboard:

  * What is open RIGHT NOW outranks any prediction about it. Sending an agent
    away from a slot that is on the screen is the worst thing this can do.
  * A stale reading describes nothing. We do not know the current state of a
    combination nobody has read in a month, and must not imply that we do.
  * A release that blinks is one release. Forecasting from raw open/close events
    collapses every answer to "within the hour".
  * Only gaps LONGER than the wait so far say anything about what is left of it.
  * When more time has passed than any gap on record, the honest answer is that
    we do not know. This is exactly where extrapolation is tempting.
  * A closed day is not part of a gap, and a forecast may not land on one.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.slots import coverage, db, forecast  # noqa: E402

GST = timezone(timedelta(hours=4))
SUNDAY = frozenset({6})
NO_CLOSED = frozenset()


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "f.db"))
    conn.execute(
        "INSERT INTO combos (combo_key, route, source_code, dest_code, country_name,"
        " centre, city, category, sub_category, visa_type, purpose, config_label,"
        " enabled, config_order, in_config)"
        " VALUES ('k1','AE-XXX','AE','XXX','Example','Dubai','Dubai','Short Stay',"
        " '','Short Stay','any','Dubai',1,0,1)")
    conn.commit()
    return conn


def _write(conn, combo_id, when, outcome):
    utc = when.astimezone(timezone.utc)
    conn.execute(
        "INSERT OR IGNORE INTO checks (combo_id, ts_utc, ts_local, tz_offset_min,"
        " date_local, hour_local, weekday, outcome, raw_message, source)"
        " VALUES (?,?,?,?,?,?,?,?,'','live')",
        (combo_id, utc.isoformat(timespec="seconds"),
         when.isoformat(timespec="seconds"), 240, when.date().isoformat(),
         when.hour, when.weekday(), outcome))


def _series(conn, combo_id, start, pattern, *, minutes=30):
    """Readings `minutes` apart; pattern is a string of 'n' (none) and 's' (slot)."""
    when = start
    for ch in pattern:
        _write(conn, combo_id, when, "slot" if ch == "s" else "none")
        when += timedelta(minutes=minutes)
    conn.commit()
    return when


# ===== the present beats the forecast ======================================


def test_a_combination_open_right_now_is_not_given_a_future_date():
    now = datetime(2026, 9, 21, 12, 0, tzinfo=GST)
    starts = [now - timedelta(days=d) for d in (9, 6, 3)]
    out = forecast.next_opening(
        starts, regime=coverage.FLASH, now=now, closed=NO_CLOSED,
        pooled=[3.0, 3.0, 3.0, 3.0],
        state=("slot", now - timedelta(minutes=20)), cadence_hours=1.0)
    assert out["reason"] == "open_now"
    assert "from" not in out and "to" not in out


def test_a_closed_combination_read_just_now_is_forecast_normally():
    now = datetime(2026, 9, 21, 12, 0, tzinfo=GST)
    # Gaps of 3 days each, one day into the current wait.
    starts = [now - timedelta(days=d) for d in (10, 7, 4, 1)]
    out = forecast.next_opening(
        starts, regime=coverage.FLASH, now=now, closed=NO_CLOSED,
        pooled=[3.0, 3.0, 3.0, 3.0],
        state=("none", now - timedelta(minutes=20)), cadence_hours=1.0)
    assert "from" in out and out.get("reason") is None


def test_a_stale_reading_does_not_claim_the_combination_is_open():
    """A slot seen two months ago is not a slot now.

    Without this the dashboard would advertise a combination the bot stopped
    reading in July as available today.
    """
    now = datetime(2026, 9, 21, 12, 0, tzinfo=GST)
    starts = [now - timedelta(days=d) for d in (60, 58, 56)]
    out = forecast.next_opening(
        starts, regime=coverage.FLASH, now=now, closed=NO_CLOSED,
        pooled=[2.0] * 4,
        state=("slot", now - timedelta(days=55)), cadence_hours=1.0)
    assert out["reason"] == "stale"
    assert "from" not in out


def test_staleness_is_judged_against_the_combination_s_own_cadence():
    """Two different bars, and both matter.

    Whether we still know the combination's STATE is judged against its own
    cadence: seven hours is nothing to a four-hourly combination and an age to a
    brisk one. Whether we will call it OPEN is a wall-clock bar, shared with the
    board, because a slot's shelf life does not depend on how often we look.

    So a seven-hour-old slot on a slow combination is neither: we still know
    roughly where it stands, but not well enough to promise the slot is there.
    """
    now = datetime(2026, 9, 21, 12, 0, tzinfo=GST)
    starts = [now - timedelta(days=d) for d in (9, 6, 3)]
    seen = now - timedelta(hours=7)
    slow = forecast.next_opening(starts, regime=coverage.FLASH, now=now,
                                 closed=NO_CLOSED, pooled=[3.0] * 4,
                                 state=("slot", seen), cadence_hours=4.0)
    brisk = forecast.next_opening(starts, regime=coverage.FLASH, now=now,
                                  closed=NO_CLOSED, pooled=[3.0] * 4,
                                  state=("slot", seen), cadence_hours=0.5)
    assert slow["reason"] == "unconfirmed"  # 7h is under 3 x 4h, but over the bar
    assert brisk["reason"] == "stale"       # 7h is far past 3 x 0.5h


def test_a_slot_read_minutes_ago_is_open_now_not_unconfirmed():
    now = datetime(2026, 9, 21, 12, 0, tzinfo=GST)
    starts = [now - timedelta(days=d) for d in (9, 6, 3)]
    out = forecast.next_opening(starts, regime=coverage.FLASH, now=now,
                                closed=NO_CLOSED, pooled=[3.0] * 4,
                                state=("slot", now - timedelta(minutes=15)),
                                cadence_hours=4.0)
    assert out["reason"] == "open_now"


def test_an_unconfirmed_slot_is_not_given_a_future_date_either():
    """It may never have closed, so a 'next opening' would be wrong twice over."""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=GST)
    starts = [now - timedelta(days=d) for d in (9, 6, 3)]
    out = forecast.next_opening(starts, regime=coverage.FLASH, now=now,
                                closed=NO_CLOSED, pooled=[3.0] * 4,
                                state=("slot", now - timedelta(hours=5)),
                                cadence_hours=4.0)
    assert out["reason"] == "unconfirmed" and "from" not in out


# ===== a blinking release is one release ===================================


def test_a_release_that_blinks_counts_once(tmp_path):
    """slot, none, slot within one cadence is one opportunity, not two.

    Counting it twice puts a one-hour gap in the distribution and every forecast
    afterwards says 'within the hour'.
    """
    conn = _conn(tmp_path)
    start = datetime.now(GST) - timedelta(days=3)
    _series(conn, 1, start, "nnssnssnn", minutes=30)
    starts = forecast.release_starts(conn, days=30)[1]
    assert len(starts) == 1


def test_releases_a_day_apart_count_separately(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(GST)
    _series(conn, 1, now - timedelta(days=6), "nnssnn", minutes=30)
    _series(conn, 1, now - timedelta(days=3), "nnssnn", minutes=30)
    starts = forecast.release_starts(conn, days=30)[1]
    assert len(starts) == 2


# ===== the conditional gap =================================================


def test_only_gaps_longer_than_the_wait_so_far_are_used():
    """Two days in, the gaps that closed in one say nothing about what is left."""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=GST)
    # Gaps of 1, 1, 1, 10 days; we are 2 days into the current wait.
    starts = [now - timedelta(days=d) for d in (15, 14, 13, 12, 2)]
    out = forecast.next_opening(starts, regime=coverage.FLASH, now=now,
                                closed=NO_CLOSED, state=None)
    # Only the 10-day gap survives, leaving 8 days to come.
    assert out["from"] == (now + timedelta(days=8)).date().isoformat()


def test_a_wait_longer_than_every_gap_is_refused_not_extrapolated():
    now = datetime(2026, 9, 21, 12, 0, tzinfo=GST)
    starts = [now - timedelta(days=d) for d in (70, 68, 66, 64, 62)]
    out = forecast.next_opening(starts, regime=coverage.FLASH, now=now,
                                closed=NO_CLOSED, state=None)
    assert out["reason"] == "overdue"
    assert out["longest_gap_days"] == pytest.approx(2.0)
    assert "from" not in out


def test_the_range_widens_rather_than_narrows_on_thin_evidence():
    """Quartiles of two numbers are a false precision.

    With only a couple of comparable gaps still running, the full observed
    spread is reported instead of a quartile band around nothing.
    """
    now = datetime(2026, 9, 21, 12, 0, tzinfo=GST)
    # Gaps 1,1,1,4,9 days; 2 days elapsed leaves just 4 and 9 -> 2 and 7 to come.
    starts = [now - timedelta(days=d) for d in (18, 17, 16, 15, 11, 2)]
    out = forecast.next_opening(starts, regime=coverage.FLASH, now=now,
                                closed=NO_CLOSED, state=None)
    span = (datetime.fromisoformat(out["to"]) - datetime.fromisoformat(out["from"])).days
    assert span == 5        # the whole of 2..7 days, not a quartile slice


# ===== refusals ============================================================


def test_a_combination_that_never_opened_gets_no_forecast():
    now = datetime(2026, 9, 21, 12, 0, tzinfo=GST)
    out = forecast.next_opening([], regime=coverage.UNSEEN, now=now,
                                closed=NO_CLOSED, state=None)
    assert out == {"reason": "never_opened"}


def test_a_waitlist_only_combination_has_nothing_to_predict():
    now = datetime(2026, 9, 21, 12, 0, tzinfo=GST)
    out = forecast.next_opening([now - timedelta(days=2)],
                                regime=coverage.WAITLIST_ONLY, now=now,
                                closed=NO_CLOSED, state=None)
    assert out == {"reason": "waitlist_only"}


def test_one_gap_alone_is_not_enough_even_with_peers():
    now = datetime(2026, 9, 21, 12, 0, tzinfo=GST)
    out = forecast.next_opening([now - timedelta(days=4), now - timedelta(days=1)],
                                regime=coverage.FLASH, now=now, closed=NO_CLOSED,
                                pooled=[2.0], state=None)
    assert out["reason"] == "insufficient"


# ===== closed days =========================================================


def test_a_closed_day_is_not_part_of_a_gap():
    """Friday to Monday is three days of clock but two of opportunity."""
    friday = datetime(2026, 9, 11, 12, 0, tzinfo=GST)
    monday = datetime(2026, 9, 14, 12, 0, tzinfo=GST)
    assert friday.weekday() == 4 and monday.weekday() == 0
    assert forecast.gaps_between([friday, monday], SUNDAY) == pytest.approx([2.0])
    assert forecast.gaps_between([friday, monday], NO_CLOSED) == pytest.approx([3.0])


def test_a_forecast_steps_over_a_closed_day_rather_than_landing_early():
    """Two open days from a Saturday is Tuesday, not Monday.

    Learning a wait in open time and then adding it as calendar time would put
    every range that crosses a Sunday a day early.
    """
    saturday = datetime(2026, 9, 12, 12, 0, tzinfo=GST)
    assert saturday.weekday() == 5
    landed = forecast._add_open_days(saturday, 2.0, SUNDAY)
    assert landed.date().isoformat() == "2026-09-15"      # Tuesday
    assert forecast._add_open_days(saturday, 2.0, NO_CLOSED).date().isoformat() \
        == "2026-09-14"


# ===== pooling =============================================================


def test_pooling_is_within_a_behaviour_not_across_everything(tmp_path):
    """A quiet combination must not borrow a busy one's rhythm.

    Pooling across all combinations would drag every estimate to the middle and
    make a flash combination look like a persistent one.
    """
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO combos (combo_key, route, source_code, dest_code, country_name,"
        " centre, city, category, sub_category, visa_type, purpose, config_label,"
        " enabled, config_order, in_config)"
        " VALUES ('k2','AE-XXX','AE','XXX','Example','Abu Dhabi','Abu Dhabi',"
        " 'Short Stay','','Short Stay','any','Abu Dhabi',1,1,1)")
    conn.commit()
    now = datetime.now(GST)
    # Combo 1 opens often; combo 2 barely. Different regimes, so no borrowing.
    for day in range(20, 1, -2):
        _series(conn, 1, now - timedelta(days=day), "nnssnn", minutes=30)
    _series(conn, 2, now - timedelta(hours=39), "n" * 40, minutes=60)

    rows = forecast.forecast(conn, days=30)
    quiet = next(r for r in rows if r["combo_id"] == 2)
    assert quiet.get("from") is None
    assert quiet["reason"] in ("dormant", "never_opened", "insufficient")


def test_a_combination_does_not_pool_with_its_own_gaps(tmp_path):
    """Its own gaps must not be counted twice, once as its own and once as a peer's."""
    conn = _conn(tmp_path)
    now = datetime.now(GST)
    for day in (12, 9, 6, 3):
        _series(conn, 1, now - timedelta(days=day), "nnssnn", minutes=30)
    rows = forecast.forecast(conn, days=30)
    only = next(r for r in rows if r["combo_id"] == 1)
    if only.get("from"):
        assert only["pooled_gaps"] == 0


# ===== end to end ==========================================================


def test_every_combination_gets_either_dates_or_a_reason(tmp_path):
    """No row is ever blank, and no row carries both."""
    conn = _conn(tmp_path)
    now = datetime.now(GST)
    for day in (12, 9, 6, 3):
        _series(conn, 1, now - timedelta(days=day), "nnssnn", minutes=30)
    for row in forecast.forecast(conn, days=30):
        has_dates = row.get("from") is not None
        has_reason = row.get("reason") is not None
        assert has_dates != has_reason, row
