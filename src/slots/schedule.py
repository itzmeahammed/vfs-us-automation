"""How often each combination deserves to be read.

The bot currently gives every combination roughly the same cadence. That spends
the same effort on one that has not changed state in seven weeks as on one whose
releases vanish inside a single gap, and the second is the only kind we can
actually lose. This module works out a per-combination interval from measured
behaviour, under a fixed total budget — so nothing here asks VFS for more
traffic, it only moves the traffic to where a reading buys something.

Four measured quantities set a combination's priority. None of them is a country,
a route or a hand-kept list; a combination that changes character is re-scored on
its next run, and one added tomorrow starts at `exploration` and earns its place.

  * **opportunity** — state changes per observed day. How often anything happens.
  * **fragility** — the share of its releases that were seen exactly once. This
    is the signal that our cadence is too slow for it: an episode caught by a
    single reading was very likely shorter than the gap, so others like it were
    missed entirely.
  * **staleness** — how long since we last looked, against what we intended.
    Keeps a starved combination from being ignored forever by a low score.
  * **exploration** — a combination we cannot yet describe is worth sampling
    precisely because we cannot describe it. Decays as readings accumulate.

The allocation is water-filling: rates proportional to priority, clipped to a
floor and ceiling, with the surplus redistributed until the budget is met. The
floor matters as much as the ceiling — a combination must never be starved to
zero, or it stops producing the evidence that would raise its own score.
"""

from typing import Dict, Iterable, List, Optional

from src.slots import coverage

# Bounds on a single combination's cadence. The floor stops a quiet combination
# from being dropped entirely (and going stale in the dashboard); the ceiling
# stops one busy combination from eating the whole budget.
DEFAULT_MIN_INTERVAL_MIN = 2.0
DEFAULT_MAX_INTERVAL_MIN = 240.0

# What the four measurements are worth relative to each other. Fragility leads
# because it is the only one that points at readings we are actually losing.
WEIGHTS = {
    "fragility": 3.0,
    "opportunity": 2.0,
    "staleness": 1.0,
    "exploration": 1.5,
}

# An opportunity rate at which the opportunity term is worth half marks. Set from
# the data's own scale: a combination changing state about twice a day is busy.
_OPPORTUNITY_HALF = 2.0

# Readings an unfamiliar combination needs before exploration stops paying.
_EXPLORATION_READINGS = 200.0

# Regimes whose readings tell us little: the state is known and stable, so a
# reading mostly confirms it. Derived labels, not names of places.
_LOW_INFORMATION = frozenset({coverage.PERSISTENT, coverage.WAITLIST_ONLY,
                              coverage.DORMANT})
_DAMPEN_LOW_INFORMATION = 0.4

# Runs the bot performed itself, where a missing reading means wasted effort
# rather than a log we could not parse. Matches store.start_run's default.
LIVE_SOURCE = "live"


def _event_rate(conn, days: int, observed_days: Dict[int, float]) -> Dict[int, float]:
    """State changes per OBSERVED day, per combination.

    Per observed day rather than per calendar day: a combination watched a third
    of the time would otherwise look a third as eventful as it is, and be given
    an even smaller share of the budget for it.
    """
    rows = conn.execute(
        "SELECT combo_id, COUNT(*) n FROM events"
        " WHERE ts_utc >= ? AND kind != 'date_moved' GROUP BY combo_id",
        (coverage._window_start(days),),
    ).fetchall()
    out: Dict[int, float] = {}
    for row in rows:
        seen = observed_days.get(row["combo_id"], 0.0)
        if seen > 0:
            out[row["combo_id"]] = row["n"] / seen
    return out


def priorities(conn, days: int = coverage.DEFAULT_DAYS, *,
               profile_rows: Optional[List[dict]] = None,
               target_interval_min: float = 30.0) -> List[dict]:
    """One row per candidate combination, with its priority and the parts of it.

    The parts are returned alongside the score on purpose: a scheduler nobody can
    interrogate is a scheduler nobody will trust with the bot's time.
    """
    rows = profile_rows if profile_rows is not None else coverage.profile(conn, days)
    observed_days = {
        r["combo_id"]: (r["covered_hours"] + r["blind_hours"]) / 24.0
        for r in rows
    }
    rates = _event_rate(conn, days, observed_days)

    out: List[dict] = []
    for row in rows:
        if not row["enabled"] or not row["in_config"]:
            continue

        rate = rates.get(row["combo_id"], 0.0)
        opportunity = rate / (rate + _OPPORTUNITY_HALF)

        episodes = row["episodes"] or 0
        fragility = (row["episodes_seen_once"] / episodes) if episodes else 0.0

        gap = row["median_gap_hours"]
        staleness = min(1.0, (gap * 60.0) / target_interval_min / 4.0) if gap else 1.0

        readings = row["readings"] or 0
        exploration = max(0.0, 1.0 - readings / _EXPLORATION_READINGS)
        if row["regime"] in (coverage.UNSEEN, coverage.SPARSE):
            exploration = 1.0

        score = (WEIGHTS["fragility"] * fragility
                 + WEIGHTS["opportunity"] * opportunity
                 + WEIGHTS["staleness"] * staleness
                 + WEIGHTS["exploration"] * exploration)

        if row["regime"] in _LOW_INFORMATION:
            # Known and stable. Still read — the floor guarantees that — but a
            # reading here is a confirmation, not a discovery.
            score *= _DAMPEN_LOW_INFORMATION

        out.append({
            "combo_id": row["combo_id"],
            "route": row["route"],
            "country_name": row["country_name"],
            "city": row["city"],
            "visa_type": row["visa_type"],
            "regime": row["regime"],
            "events_per_observed_day": round(rate, 3),
            "opportunity": round(opportunity, 4),
            "fragility": round(fragility, 4),
            "staleness": round(staleness, 4),
            "exploration": round(exploration, 4),
            "score": round(score, 4),
        })

    out.sort(key=lambda r: -r["score"])
    return out


def water_fill(weights: List[float], budget: float,
               floor: float, ceiling: float) -> List[float]:
    """Rates proportional to `weights`, each within [floor, ceiling], summing to `budget`.

    Clip, then redistribute what the clipping freed among whoever is still free to
    move, and repeat. Converges in a handful of passes because each pass either
    fixes a rate for good or finishes.

    A budget below `floor * n` cannot be met without starving something, so the
    floor wins and the caller is handed a total above budget — a scheduler that
    silently stopped reading a combination would be worse than one that reports
    it cannot fit.
    """
    n = len(weights)
    if not n:
        return []
    if budget <= 0:
        return [floor] * n

    rates = [0.0] * n
    fixed = [False] * n

    # Ceilings are settled before floors. Doing both in one pass strands the
    # budget a ceiling just freed: with weights 100 and 1 the second share falls
    # under the floor, both get pinned, and a feasible allocation is abandoned
    # part-spent. Capping the greedy one first and re-solving hands that budget
    # to whoever is still free to take it.
    for _ in range(2 * n + 2):
        free = [i for i in range(n) if not fixed[i]]
        if not free:
            break
        remaining = budget - sum(rates[i] for i in range(n) if fixed[i])
        if remaining <= 0:
            for i in free:
                rates[i], fixed[i] = floor, True
            continue

        total = sum(max(0.0, weights[i]) for i in free)
        if total <= 0:
            share = remaining / len(free)
            for i in free:
                rates[i] = min(ceiling, max(floor, share))
            break

        want = {i: remaining * max(0.0, weights[i]) / total for i in free}

        over = [i for i in free if want[i] > ceiling]
        if over:
            for i in over:
                rates[i], fixed[i] = ceiling, True
            continue

        under = [i for i in free if want[i] < floor]
        if under:
            for i in under:
                rates[i], fixed[i] = floor, True
            continue

        for i in free:
            rates[i], fixed[i] = want[i], True

    return rates


def plan(conn, days: int = coverage.DEFAULT_DAYS, *,
         readings_per_hour: Optional[float] = None,
         min_interval_min: float = DEFAULT_MIN_INTERVAL_MIN,
         max_interval_min: float = DEFAULT_MAX_INTERVAL_MIN,
         profile_rows: Optional[List[dict]] = None) -> dict:
    """A cadence for every active combination, inside the budget we already spend.

    `readings_per_hour` defaults to what the bot is achieving now, measured from
    the window — so the default plan is a reallocation, not an increase, and can
    be compared against today like for like.
    """
    scored = priorities(conn, days, profile_rows=profile_rows)
    if not scored:
        return {"budget_per_hour": 0.0, "combinations": [], "unmet": False}

    if readings_per_hour is None:
        readings_per_hour = _current_throughput(conn, days)

    floor = 60.0 / max_interval_min
    ceiling = 60.0 / min_interval_min
    rates = water_fill([r["score"] for r in scored], readings_per_hour,
                       floor, ceiling)

    for row, rate in zip(scored, rates):
        row["readings_per_hour"] = round(rate, 3)
        row["interval_minutes"] = round(60.0 / rate, 1) if rate > 0 else None

    allocated = sum(rates)
    return {
        "days": days,
        "budget_per_hour": round(readings_per_hour, 2),
        "allocated_per_hour": round(allocated, 2),
        "unmet": allocated > readings_per_hour + 1e-6,
        "min_interval_minutes": min_interval_min,
        "max_interval_minutes": max_interval_min,
        "combinations": scored,
    }


def _current_throughput(conn, days: int, *,
                        closed: Optional[frozenset] = None) -> float:
    """Readings per hour the bot is achieving now, over open days only.

    Measured rather than configured: the budget to reallocate is whatever we are
    already managing, including the effect of every failed and paused run.
    """
    closed = coverage.closed_days() if closed is None else closed
    row = conn.execute(
        "SELECT COUNT(*) n, MIN(ts_local) a, MAX(ts_local) b FROM checks"
        " WHERE ts_utc >= ?",
        (coverage._window_start(days),),
    ).fetchone()
    if not row or not row["n"] or not row["a"] or not row["b"]:
        return 0.0
    start, end = coverage._parse(row["a"]), coverage._parse(row["b"])
    if not start or not end or end <= start:
        return 0.0
    raw = (end - start).total_seconds() / 3600.0
    span = raw - coverage.closed_hours_between(start, end, closed)
    # Readings that fall entirely on a day we call closed contradict the
    # assumption, so the readings win: the bot demonstrably ran, and reporting
    # its throughput as zero would hand `plan` an empty budget on the evidence
    # that it was busy. Short windows sitting inside a closed day hit this.
    if span <= 0:
        span = raw
    return row["n"] / span if span > 0 else 0.0


# Where a missed episode actually costs something. A `persistent` combination is
# open anyway and a `waitlist-only` one has nothing to miss, so a reading there
# confirms a known state; on a `flash` or `intermittent` one an episode is a
# perishable opportunity. Derived labels again, not a list of places.
DETECTION_REGIMES = frozenset({coverage.FLASH, coverage.INTERMITTENT})
CONFIRMATION_REGIMES = frozenset({coverage.PERSISTENT, coverage.WAITLIST_ONLY,
                                  coverage.DORMANT})


def achieved_interval_hours(prof: dict) -> Optional[float]:
    """The cadence a combination actually got, not the one it got on a good day.

    Readings divided into the hours they span. The median gap flatters us badly
    here: it describes the healthy stretches and says nothing about the outages
    between them, and comparing a median against a planned average would credit
    the plan with a gain that is really just the two numbers meaning different
    things.
    """
    observed = (prof["covered_hours"] or 0.0) + (prof["blind_hours"] or 0.0)
    readings = prof["readings"] or 0
    if observed <= 0 or readings < 2:
        return None
    return observed / readings


def expected_gain(conn, days: int = coverage.DEFAULT_DAYS, *,
                  plan_rows: Optional[List[dict]] = None,
                  assumed_duration_hours: float = 0.25,
                  profile_rows: Optional[List[dict]] = None) -> dict:
    """What the plan trades, split by whether a missed episode costs anything.

    An episode lasting `assumed_duration_hours` is caught with probability
    `min(1, duration / interval)`. Summed before and after, that is the change in
    expected catches — conditional on an assumption this data cannot settle
    (`coverage.detection_sensitivity` is the same arithmetic from the other side),
    so it is quoted as a ratio.

    Reported in two groups because one total would hide the whole point. The plan
    deliberately gives up confirmation readings on combinations that are always
    open or never open, to buy detection readings on the ones whose releases
    vanish. A single number would net those off and call a good trade a bad one.
    """
    rows = plan_rows if plan_rows is not None else plan(conn, days)["combinations"]
    profile_by_id = {
        r["combo_id"]: r
        for r in (profile_rows if profile_rows is not None
                  else coverage.profile(conn, days))
    }

    groups = {
        "detection": {"before": 0.0, "after": 0.0, "combinations": 0, "episodes": 0},
        "confirmation": {"before": 0.0, "after": 0.0, "combinations": 0, "episodes": 0},
    }

    for row in rows:
        prof = profile_by_id.get(row["combo_id"])
        if not prof or not prof["episodes"]:
            continue
        current = achieved_interval_hours(prof)
        planned = (row.get("interval_minutes") or 0) / 60.0
        if not current or not planned:
            continue

        if row["regime"] in DETECTION_REGIMES:
            bucket = groups["detection"]
        elif row["regime"] in CONFIRMATION_REGIMES:
            bucket = groups["confirmation"]
        else:
            continue

        weight = prof["episodes"]
        bucket["combinations"] += 1
        bucket["episodes"] += weight
        bucket["before"] += weight * min(1.0, assumed_duration_hours / current)
        bucket["after"] += weight * min(1.0, assumed_duration_hours / planned)

    for bucket in groups.values():
        bucket["before"] = round(bucket["before"], 2)
        bucket["after"] = round(bucket["after"], 2)
        bucket["ratio"] = (round(bucket["after"] / bucket["before"], 2)
                           if bucket["before"] else None)

    return {"assumed_duration_hours": assumed_duration_hours, **groups}


def capacity(conn, days: int = coverage.DEFAULT_DAYS) -> dict:
    """What the bot could read per hour if it never sat idle.

    Measured from the runs themselves: a productive run's duration and how many
    readings it produced give a readings-per-run-minute rate, and the share of
    wall-clock actually spent inside runs says how much room is left. The gap
    between `sustained_per_hour` and the throughput we achieve is idle time, not
    a capability limit — which is why a scheduler that only reshuffles a fixed
    readings-per-hour budget finds almost nothing to win.

    `idle_share` is the headroom and also the warning. Some of that pause is
    deliberate: the portals answer back with restricted, geo-blocked and blocked
    runs, so spending all of it is not free. The number to take from here is what
    a tighter cycle could buy, weighed against that.
    """
    row = conn.execute(
        "SELECT COUNT(*) runs,"
        " SUM((julianday(finished_at_utc) - julianday(started_at_utc)) * 1440) run_minutes,"
        " MIN(started_at_utc) first_start, MAX(finished_at_utc) last_finish"
        " FROM runs WHERE finished_at_utc IS NOT NULL AND started_at_utc >= ?",
        (coverage._window_start(days),),
    ).fetchone()
    if not row or not row["run_minutes"]:
        return {"sustained_per_hour": None, "idle_share": None}

    # Readings and run-minutes are counted separately on purpose. Joining runs to
    # checks repeats a run's duration once per reading it produced, so a run with
    # three readings would be billed three times over and the rate would come out
    # at a third of the truth.
    since = coverage._window_start(days)
    productive = conn.execute(
        "SELECT"
        " (SELECT COUNT(*) FROM checks"
        "   WHERE run_id IS NOT NULL AND ts_utc >= ?) readings,"
        " (SELECT SUM((julianday(finished_at_utc) - julianday(started_at_utc)) * 1440)"
        "   FROM runs WHERE finished_at_utc IS NOT NULL AND started_at_utc >= ?"
        "   AND EXISTS (SELECT 1 FROM checks ch WHERE ch.run_id = runs.id)) minutes",
        (since, since),
    ).fetchone()
    if not productive or not productive["minutes"] or not productive["readings"]:
        return {"sustained_per_hour": None, "idle_share": None}

    per_minute = productive["readings"] / productive["minutes"]

    # Wall-clock from the first run's start to the last one's finish, so a window
    # holding a single run still has a span. Closed days come out of it the same
    # way they do everywhere else rather than by a flat allowance per week, which
    # went negative on any window shorter than the allowance.
    begin = coverage._parse(row["first_start"])
    end = coverage._parse(row["last_finish"])
    busy_share = None
    if begin and end and end > begin:
        elapsed = (end - begin).total_seconds() / 3600.0
        elapsed -= coverage.closed_hours_between(begin, end, coverage.closed_days())
        if elapsed > 0:
            busy_share = min(1.0, (row["run_minutes"] / 60.0) / elapsed)

    # A run with no readings means two different things depending on where the
    # run came from, and pooling them overstates the loss badly. For a run the
    # bot performed, no readings is wasted effort. For one reconstructed from a
    # log file, it usually means the log held no attributable check lines --
    # a gap in what we can recover, not time the bot threw away. So the wasted
    # share is measured only over runs whose readings would have been recorded
    # as they happened; the rest are counted and reported separately.
    live = conn.execute(
        "SELECT"
        " SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM checks ch WHERE ch.run_id = runs.id)"
        "   THEN (julianday(finished_at_utc) - julianday(started_at_utc)) * 1440"
        "   ELSE 0 END) empty_minutes,"
        " SUM((julianday(finished_at_utc) - julianday(started_at_utc)) * 1440) minutes,"
        " COUNT(*) runs,"
        " SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM checks ch WHERE ch.run_id = runs.id)"
        "   THEN 1 ELSE 0 END) empty_runs"
        " FROM runs WHERE finished_at_utc IS NOT NULL AND started_at_utc >= ?"
        "   AND source = ?",
        (coverage._window_start(days), LIVE_SOURCE),
    ).fetchone()
    wasted = (live["empty_minutes"] or 0.0) if live else 0.0
    live_minutes = (live["minutes"] or 0.0) if live else 0.0

    unattributed = conn.execute(
        "SELECT COUNT(*) runs FROM runs"
        " WHERE started_at_utc >= ? AND source != ?"
        " AND NOT EXISTS (SELECT 1 FROM checks ch WHERE ch.run_id = runs.id)",
        (coverage._window_start(days), LIVE_SOURCE),
    ).fetchone()

    return {
        "readings_per_run_minute": round(per_minute, 3),
        "sustained_per_hour": round(per_minute * 60.0, 1),
        "achieved_per_hour": round(_current_throughput(conn, days), 2),
        "busy_share": round(busy_share, 4) if busy_share else None,
        "idle_share": round(1.0 - busy_share, 4) if busy_share else None,
        "run_minutes": round(row["run_minutes"], 0),
        "wasted_run_minutes": round(wasted, 0),
        "observed_runs": (live["runs"] or 0) if live else 0,
        "observed_empty_runs": (live["empty_runs"] or 0) if live else 0,
        "wasted_share_of_run_time": (round(wasted / live_minutes, 4)
                                     if live_minutes else None),
        # Runs recovered from logs that carried no attributable readings. Not
        # wasted bot time -- a limit on what the history can tell us.
        "unattributed_runs": (unattributed["runs"] or 0) if unattributed else 0,
    }


def required_budget(conn, days: int = coverage.DEFAULT_DAYS, *,
                    target_interval_min: float = 5.0,
                    floor_interval_min: float = DEFAULT_MAX_INTERVAL_MIN,
                    profile_rows: Optional[List[dict]] = None) -> dict:
    """Throughput needed to poll the detection group at `target_interval_min`.

    The reallocation in `plan` is bounded by what the bot currently achieves, and
    that ceiling is low enough to matter: this says how far short it is. If the
    answer is several times today's throughput, then no amount of reallocation
    catches a release that lasts minutes, and the honest next move is to repair
    the runs that produce nothing rather than to reshuffle the ones that work.
    """
    rows = profile_rows if profile_rows is not None else coverage.profile(conn, days)
    active = [r for r in rows if r["enabled"] and r["in_config"]]
    detection = [r for r in active if r["regime"] in DETECTION_REGIMES]
    rest = [r for r in active if r["regime"] not in DETECTION_REGIMES]

    needed = (len(detection) * 60.0 / target_interval_min
              + len(rest) * 60.0 / floor_interval_min)
    current = _current_throughput(conn, days)
    head = capacity(conn, days)
    sustained = head.get("sustained_per_hour")
    return {
        "target_interval_minutes": target_interval_min,
        "detection_combinations": len(detection),
        "other_combinations": len(rest),
        "required_per_hour": round(needed, 1),
        "current_per_hour": round(current, 2),
        "shortfall_multiple": round(needed / current, 1) if current else None,
        # Against capacity rather than against today: this is the question that
        # decides whether the answer is "stop idling" or "add a second worker".
        "sustained_per_hour": sustained,
        "reachable_without_new_workers": (bool(sustained and needed <= sustained)
                                         if sustained else None),
        "workers_needed": (max(1, -(-needed // sustained)) if sustained else None),
    }
