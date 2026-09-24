"""When a combination is likely to open next.

No model is fitted and nothing is trained. There are 103 openings in fifty days
spread over thirteen combinations, and the honest thing to do with that is
arithmetic on the gaps between releases, not a learned function. What this
module produces is a date range and a word saying how much to trust it.

The method, in full:

  1. A combination's RELEASE STARTS come from `coverage.episodes`, not from raw
     `opened` events. A release that briefly closes and reopens logs two
     openings, and forecasting from those collapses every prediction to "within
     the hour". Starts closer together than the combination's own reading
     cadence are one release blinking, so they are merged.
  2. The GAPS between consecutive starts are measured in OPEN time — closed days
     come out, because nothing can open on them and leaving them in would stretch
     every gap that happened to span one.
  3. Given `elapsed` open-time since the last release, the wait still to come is
     read off the gaps that were LONGER than `elapsed`. A combination that last
     opened six days ago is not described by the gaps that closed in two.
  4. The range is the quartiles of that conditional wait, walked forward over the
     calendar with closed days skipped.

Where that chain breaks, the answer is a reason and no dates:

  * `never_opened`   nothing has ever opened; there is no gap to reason from.
  * `waitlist_only`  a waitlist and no slot in the window. Nothing to predict.
  * `dormant`        read often, never opened.
  * `overdue`        more time has passed than ANY gap we have on record. The
                     conditional set is empty, so the only truthful answer is
                     that this is outside our experience. A model would happily
                     extrapolate here; we decline.
  * `insufficient`   too few gaps, its own and its peers' together.
  * `open_now`       it is open as of the latest reading. Predicting a future
                     date here would send an agent away from a slot that is on
                     the screen right now, which is the worst thing this could do.
  * `unconfirmed`    a slot was there when we last looked, but not recently
                     enough to promise it is still there. The most actionable
                     state on the board: it may well still be open, and nobody
                     has checked. Predicting a NEXT opening here would be wrong
                     twice over, because it may not have closed at all.
  * `stale`          the newest reading is too old to describe the current state.

Nothing here names a country, a route or a centre. A combination that starts
releasing next month is forecast from its own new gaps on the next run.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from src.slots import coverage, query

# Own gaps needed before a combination is forecast from itself alone. Below it,
# gaps from combinations behaving the same way are pooled in and the answer is
# labelled as such.
MIN_OWN_GAPS = 8

# Own gaps below which we will not speak at all, even with peers pooled in. One
# gap is a coincidence, not a pattern; two with peers behind them is a hint.
MIN_ANY_GAPS = 4

# Two release starts closer than this many times the combination's own median
# reading gap are the same release flickering. Two, because one reading showing
# nothing between two that show slots is the cheapest way for a release to blink
# and the commonest thing in the data.
FLICKER_CADENCES = 2.0

# Floor for that merge window, for a combination whose cadence we cannot measure.
FLICKER_FLOOR_HOURS = 2.0

# The quartiles reported as the range. Deliberately not the full spread: the
# 0-100% range of a skewed distribution is always "some time in the next month",
# which is true and useless.
LOW_QUANTILE = 0.25
HIGH_QUANTILE = 0.75

# Below this many comparable gaps still running, the quartiles of what is left
# are two numbers pretending to be a distribution. The full observed spread is
# reported instead: wider, and true.
MIN_FOR_QUARTILES = 4

# How stale the newest reading may be before we stop describing the combination's
# CURRENT state, as a multiple of its own reading cadence. A combination last
# read two months ago may be open or shut; we do not know and will not imply it.
STALE_CADENCES = 3.0
STALE_FLOOR_HOURS = 6.0

# How fresh a slot reading must be before we will call a combination open. The
# board's bar, shared deliberately: if the dashboard will not put a date in
# large type off a reading this old, the forecast must not call it open either,
# or a card ends up showing neither a date nor a line.
OPEN_WITHIN_HOURS = query.BOOKABLE_WITHIN_HOURS

CONFIDENCE_LIKELY = "likely"        # its own gaps, enough of them
CONFIDENCE_UNCERTAIN = "uncertain"  # its own gaps, few
CONFIDENCE_POOLED = "pooled"        # leaning on combinations that behave alike

# Regimes that have nothing to forecast, and the reason to show instead.
_NO_FORECAST = {
    coverage.WAITLIST_ONLY: "waitlist_only",
    coverage.DORMANT: "dormant",
    coverage.UNSEEN: "never_opened",
}


def _parse(ts: Optional[str]) -> Optional[datetime]:
    return coverage._parse(ts)


def _quantile(values: List[float], q: float) -> float:
    """Linear-interpolated quantile. `values` must be sorted and non-empty."""
    if len(values) == 1:
        return values[0]
    pos = q * (len(values) - 1)
    low = int(pos)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (pos - low)


def _open_days_between(start: datetime, end: datetime,
                       closed: frozenset) -> float:
    """Elapsed days with closed days removed."""
    if end <= start:
        return 0.0
    hours = (end - start).total_seconds() / 3600.0
    return max(0.0, hours - coverage.closed_hours_between(start, end, closed)) / 24.0


def _add_open_days(start: datetime, open_days: float,
                   closed: frozenset) -> datetime:
    """Walk `open_days` forward from `start`, skipping closed days.

    The inverse of `_open_days_between`: a wait learned in open time has to come
    back out as a calendar date, or a range that crosses a closed day would be
    reported a day early.
    """
    remaining = open_days
    when = start
    # Day at a time, so a closed day costs nothing and is stepped over. Bounded
    # generously: a forecast further out than this is not worth showing anyway.
    for _ in range(400):
        if remaining <= 0:
            return when
        if when.weekday() in closed:
            when = (when + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                      microsecond=0)
            continue
        # How much open time is left in this calendar day.
        end_of_day = (when + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                        microsecond=0)
        available = (end_of_day - when).total_seconds() / 86400.0
        if remaining <= available:
            return when + timedelta(days=remaining)
        remaining -= available
        when = end_of_day
    return when


def current_state(conn: sqlite3.Connection, days: int = coverage.DEFAULT_DAYS
                  ) -> Dict[int, Tuple[str, datetime]]:
    """Each combination's newest reading: what it showed and when.

    A forecast is about the future, so it must not be reported for something that
    is open right now. That is the one case where being wrong costs a booking.
    """
    out: Dict[int, Tuple[str, datetime]] = {}
    for row in conn.execute(
            "SELECT combo_id, outcome, ts_local FROM checks WHERE id IN"
            " (SELECT MAX(id) FROM checks WHERE ts_utc >= ? GROUP BY combo_id)",
            (coverage._window_start(days),)):
        when = _parse(row["ts_local"])
        if when is not None:
            out[row["combo_id"]] = (row["outcome"], when)
    return out


def release_starts(conn: sqlite3.Connection, days: int = coverage.DEFAULT_DAYS, *,
                   profile_rows: Optional[List[dict]] = None
                   ) -> Dict[int, List[datetime]]:
    """Distinct release starts per combination, with flicker merged.

    The merge window is the combination's own median reading gap times
    `FLICKER_CADENCES`, so a combination read every four hours and one read every
    twenty minutes are each judged on their own terms rather than against a
    constant someone picked.
    """
    cadence: Dict[int, float] = {}
    rows = (profile_rows if profile_rows is not None
            else coverage.profile(conn, days))
    for row in rows:
        gap = row.get("median_gap_hours")
        if gap:
            cadence[row["combo_id"]] = float(gap)

    out: Dict[int, List[datetime]] = {}
    for combo_id, eps in coverage.episodes(conn, days).items():
        window = timedelta(hours=max(
            FLICKER_FLOOR_HOURS,
            cadence.get(combo_id, FLICKER_FLOOR_HOURS) * FLICKER_CADENCES))
        starts: List[datetime] = []
        for episode in eps:
            when = _parse(episode["first_seen"])
            if when is None:
                continue
            if starts and when - starts[-1] <= window:
                continue          # the same release, blinking
            starts.append(when)
        if starts:
            out[combo_id] = starts
    return out


def gaps_between(starts: List[datetime], closed: frozenset) -> List[float]:
    """Open-time days between consecutive release starts."""
    return [_open_days_between(a, b, closed) for a, b in zip(starts, starts[1:])]


def _pool_for(regime: str, per_regime: Dict[str, List[float]]) -> List[float]:
    """Gaps from combinations behaving the same way.

    Pooling across a regime and not across everything is the point: a `flash`
    combination's silence means something different from a `persistent` one's,
    and mixing them would drag every estimate towards the middle.
    """
    return sorted(per_regime.get(regime, []))


def next_opening(starts: List[datetime], *, regime: str, now: datetime,
                 closed: frozenset, pooled: Optional[List[float]] = None,
                 state: Optional[Tuple[str, datetime]] = None,
                 cadence_hours: Optional[float] = None) -> dict:
    """The date range in which this combination is likely to open next.

    Returns dates and a confidence, or a reason and no dates. It never returns
    both, and it never guesses past the end of what has been observed.
    """
    # The present beats any prediction about it.
    if state:
        outcome, seen = state
        stale_after = timedelta(hours=max(
            STALE_FLOOR_HOURS, (cadence_hours or STALE_FLOOR_HOURS) * STALE_CADENCES))
        if now - seen > stale_after:
            return {"reason": "stale", "last_reading": seen.date().isoformat()}
        if outcome == "slot":
            age = (now - seen).total_seconds() / 3600.0
            reason = "open_now" if age <= OPEN_WITHIN_HOURS else "unconfirmed"
            return {"reason": reason, "since": seen.isoformat(timespec="minutes")}

    reason = _NO_FORECAST.get(regime)
    if reason and regime != coverage.UNSEEN:
        return {"reason": reason}
    if not starts:
        return {"reason": "never_opened"}

    own = sorted(gaps_between(starts, closed))
    using = own
    confidence = CONFIDENCE_LIKELY if len(own) >= MIN_OWN_GAPS else CONFIDENCE_UNCERTAIN
    if len(own) < MIN_OWN_GAPS and pooled:
        using = sorted(own + list(pooled))
        confidence = CONFIDENCE_POOLED
    if len(using) < MIN_ANY_GAPS:
        return {"reason": "insufficient", "gaps": len(own)}

    last = starts[-1]
    elapsed = _open_days_between(last, now, closed)

    # Only the gaps that outlasted the wait so far can say anything about what is
    # left of it. This is the whole method, and the next line is where a model
    # would quietly invent an answer instead.
    remaining = sorted(g - elapsed for g in using if g > elapsed)
    if not remaining:
        return {"reason": "overdue", "gaps": len(own),
                "elapsed_days": round(elapsed, 2),
                "longest_gap_days": round(using[-1], 2)}

    if len(remaining) >= MIN_FOR_QUARTILES:
        low = _quantile(remaining, LOW_QUANTILE)
        high = _quantile(remaining, HIGH_QUANTILE)
    else:
        # Too few left to carve quartiles out of. Show the whole of what remains.
        low, high = remaining[0], remaining[-1]
    return {
        "from": _add_open_days(now, low, closed).date().isoformat(),
        "to": _add_open_days(now, high, closed).date().isoformat(),
        "confidence": confidence,
        "gaps": len(own),
        "pooled_gaps": len(using) - len(own),
        "elapsed_days": round(elapsed, 2),
        "last_opened": last.date().isoformat(),
        # How many of the comparable gaps had already ended by now. A high share
        # means we are late in the distribution and the range is thin evidence.
        "share_of_gaps_already_shorter": round(
            1.0 - len(remaining) / len(using), 3),
    }


def forecast(conn: sqlite3.Connection, days: int = coverage.DEFAULT_DAYS, *,
             now: Optional[datetime] = None,
             closed: Optional[frozenset] = None,
             profile_rows: Optional[List[dict]] = None) -> List[dict]:
    """A row per combination: either a date range, or a reason there is none."""
    closed = coverage.closed_days() if closed is None else closed
    now = now or datetime.now(timezone.utc)
    rows = (profile_rows if profile_rows is not None
            else coverage.profile(conn, days))
    starts = release_starts(conn, days, profile_rows=rows)
    state = current_state(conn, days)
    cadence = {r["combo_id"]: r.get("median_gap_hours") for r in rows}

    # Gaps by regime, for pooling. Built from every combination that has any, so
    # a combination with one release can still borrow the shape of its peers'.
    per_regime: Dict[str, List[float]] = {}
    for row in rows:
        mine = starts.get(row["combo_id"])
        if mine:
            per_regime.setdefault(row["regime"], []).extend(
                gaps_between(mine, closed))

    out: List[dict] = []
    for row in rows:
        mine = starts.get(row["combo_id"], [])
        pool = [g for g in _pool_for(row["regime"], per_regime)]
        # A combination does not pool with itself.
        for own_gap in gaps_between(mine, closed):
            if own_gap in pool:
                pool.remove(own_gap)
        result = next_opening(mine, regime=row["regime"], now=now,
                              closed=closed, pooled=pool,
                              state=state.get(row["combo_id"]),
                              cadence_hours=cadence.get(row["combo_id"]))
        out.append({
            "combo_id": row["combo_id"],
            "route": row.get("route"),
            "country": row.get("country_name") or row.get("route"),
            "city": row.get("city"),
            "visa_type": row.get("visa_type"),
            "regime": row["regime"],
            "releases": len(mine),
            **result,
        })
    # Open now first: those are actionable this minute and outrank any date.
    # Then soonest forecast, then the rows carrying only a reason.
    out.sort(key=lambda r: (0 if r.get("reason") == "open_now" else 1,
                            r.get("from") is None, r.get("from") or "",
                            r.get("country") or ""))
    return out


# ===== shaping for a page ==================================================
#
# Both boards render the same forecast, so the flattening and the per-country
# roll-up live here rather than in either page's builder.


def cell(row: Optional[dict]) -> dict:
    """One combination's forecast, flattened for the page.

    Always carries `reason` XOR `from`/`to`, never both and never neither, so
    the template has exactly one branch to render.
    """
    if not row:
        return {"reason": "no_data"}
    if row.get("from"):
        return {"from": row["from"], "to": row["to"],
                "confidence": row["confidence"],
                "same_day": row["from"] == row["to"],
                "gaps": row.get("gaps", 0),
                "pooled_gaps": row.get("pooled_gaps", 0),
                "last_opened": row.get("last_opened")}
    cell = {"reason": row.get("reason", "no_data")}
    for key in ("since", "last_reading", "elapsed_days", "longest_gap_days"):
        if row.get(key) is not None:
            cell[key] = row[key]
    return cell


# How a country's single forecast is chosen from its combinations': what is
# actionable right now beats a date, a date beats a reason, and among reasons the
# most informative comes first. A country is only as good as its best row.
REASON_ORDER = ("unconfirmed", "overdue", "stale", "insufficient", "dormant",
                "waitlist_only", "never_opened", "no_data")


def roll_up(cells: list) -> dict:
    """The most actionable forecast among a country's combinations."""
    if not cells:
        return {"reason": "no_data"}
    for state in ("open_now", "unconfirmed"):
        hits = [c for c in cells if c.get("reason") == state]
        if hits:
            # Newest first: the freshest sighting is the one worth acting on.
            return max(hits, key=lambda c: c.get("since") or "")
    dated = [c for c in cells if c.get("from")]
    if dated:
        return min(dated, key=lambda c: c["from"])
    return min(cells, key=lambda c: (
        REASON_ORDER.index(c["reason"]) if c.get("reason") in REASON_ORDER
        else len(REASON_ORDER)))
