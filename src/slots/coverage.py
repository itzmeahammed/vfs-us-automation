"""How much of the window we actually watched, and what that costs us.

Every rate in `query.py` is honest about which checks happened, but none of them
says how much of the clock those checks *cover*. A combination read twice a day
and one read every ten minutes both produce an availability percentage, and the
two numbers do not mean the same thing. This module supplies the missing half:
observed time, blind time, and what a release has to survive to be seen at all.

Nothing here names a country, a route or a centre. Every classification is
derived from the readings themselves, so a combination that starts behaving
differently next month is re-classified on its own evidence, and one added
tomorrow is handled without a code change.

Three ideas do the work:

  * **A closed day is not missing evidence.** The bot does not run on the days
    listed in `[slots] closed_days`, and nothing opens on them. Counting those
    hours as unwatched would invent a blind spot that costs nothing, and would
    make every coverage figure look worse than it is. They are removed from the
    numerator and the denominator alike.
  * **An episode's length is bracketed, not known.** A release seen once sits
    somewhere between an instant and the span from the previous reading to the
    next. That is an interval, and reporting the interval is the honest answer —
    a single number here would be invention.
  * **The miss rate is therefore not identifiable from this data alone.** With
    episodes shorter than the polling gap, what we caught is a sample of unknown
    size. `detection_sensitivity` says what the catch rate would be *given* a
    true duration, which turns the question into one experiment: poll one busy
    combination fast enough to resolve its episodes, then read the rest off the
    curve.
"""

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

DEFAULT_DAYS = 30

# A gap longer than this counts as unwatched. Not a polling target — a bar for
# "close enough to the previous reading that we would have noticed a change".
DEFAULT_RESOLUTION_HOURS = 1.0

# Where a combination's behaviour is called one thing or another. Thresholds,
# not country lists: they are applied to whatever the readings say.
_PERSISTENT_SHARE = 0.85      # slots on this share of readings = always open
_FLASH_MAX_READINGS = 2       # an episode this short vanished inside the cadence
_FLASH_MIN_SHARE = 0.5        # ... and this many of its episodes did
# Below this many readings a combination is not described, only noted. One
# reading that happened to catch a slot would otherwise read as 'always open',
# which is the kind of confident nonsense a scheduler would then act on.
_MIN_READINGS = 20

# An hour of the local clock holding less than this share of the median watched
# hour's readings is called blind. Relative rather than a fixed count, so the
# verdict keeps its meaning if the bot's overall rate changes.
_BLIND_SHARE = 0.05

WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday",
                 "saturday", "sunday")

# Regimes. Strings rather than an enum so they cross into JSON and templates
# unchanged, the way `parse`'s outcomes already do.
UNSEEN = "unseen"             # nothing read in the window
SPARSE = "sparse"             # read too few times to characterise yet
DORMANT = "dormant"           # read, but never a slot and never a waitlist
WAITLIST_ONLY = "waitlist-only"
PERSISTENT = "persistent"     # open on nearly every reading
FLASH = "flash"               # opens, but the openings do not survive one gap
INTERMITTENT = "intermittent"  # opens and closes at a readable pace


def closed_days(default: Iterable[str] = ("sunday",)) -> frozenset:
    """Weekdays the bot does not run, as `checks.weekday` numbers (0 = Monday).

    Configured by name in `[slots] closed_days` so the file stays readable; an
    empty setting means the bot runs every day. An unknown name is ignored
    rather than raising — a typo must not take the dashboard down.
    """
    raw: Optional[str] = None
    try:
        from src.utils.config_reader import get_config_value
        raw = get_config_value("slots", "closed_days", None)
    except Exception:
        raw = None
    if raw is None:
        names = list(default)
    else:
        names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return frozenset(WEEKDAY_NAMES.index(n) for n in names if n in WEEKDAY_NAMES)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def closed_hours_between(start: datetime, end: datetime,
                         closed: frozenset) -> float:
    """Hours between two local timestamps that fall on a closed day.

    Walks whole days because a gap can be long — the worst in the current
    history is about 25 days — and a closed day inside one is time we were never
    going to watch.
    """
    if not closed or end <= start:
        return 0.0
    total = 0.0
    day = start.date()
    last = end.date()
    while day <= last:
        if day.weekday() in closed:
            # The closed day's own bounds, clipped to the gap.
            begin = datetime.combine(day, datetime.min.time(), tzinfo=start.tzinfo)
            finish = begin + timedelta(days=1)
            lo = max(begin, start)
            hi = min(finish, end)
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600.0
        day += timedelta(days=1)
    return total


def _window_start(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _readings(conn: sqlite3.Connection, days: int) -> Dict[int, List[sqlite3.Row]]:
    """Every combination's readings in the window, oldest first."""
    rows = conn.execute(
        "SELECT combo_id, ts_utc, ts_local, outcome FROM checks"
        " WHERE ts_utc >= ? ORDER BY combo_id, ts_utc",
        (_window_start(days),),
    ).fetchall()
    out: Dict[int, List[sqlite3.Row]] = {}
    for row in rows:
        out.setdefault(row["combo_id"], []).append(row)
    return out


# ===== observation =========================================================


def observation(conn: sqlite3.Connection, days: int = DEFAULT_DAYS, *,
                resolution_hours: float = DEFAULT_RESOLUTION_HOURS,
                closed: Optional[frozenset] = None) -> Dict[int, dict]:
    """Watched vs blind time per combination, keyed by combo id.

    `covered_hours` credits each gap with at most `resolution_hours`: a reading
    speaks for the moment it was taken and a little after, not for the eight
    hours until the next one. The rest of the gap is blind. Closed days are
    removed from both, so a combination watched every hour on every open day
    reports full coverage rather than being marked down for a Sunday.
    """
    closed = closed_days() if closed is None else closed
    out: Dict[int, dict] = {}

    for combo_id, rows in _readings(conn, days).items():
        covered = blind = 0.0
        worst = 0.0
        gaps: List[float] = []
        previous: Optional[datetime] = None

        for row in rows:
            local = _parse(row["ts_local"])
            if local is None:
                continue
            if previous is not None:
                span = (local - previous).total_seconds() / 3600.0
                if span > 0:
                    open_span = span - closed_hours_between(previous, local, closed)
                    if open_span > 0:
                        gaps.append(open_span)
                        covered += min(open_span, resolution_hours)
                        blind += max(0.0, open_span - resolution_hours)
                        worst = max(worst, open_span)
            previous = local

        operating = covered + blind
        out[combo_id] = {
            "readings": len(rows),
            "covered_hours": round(covered, 1),
            "blind_hours": round(blind, 1),
            "operating_hours": round(operating, 1),
            "coverage": round(covered / operating, 4) if operating else None,
            "worst_gap_hours": round(worst, 1),
            "median_gap_hours": _median(gaps),
            "first_seen": rows[0]["ts_utc"] if rows else None,
            "last_seen": rows[-1]["ts_utc"] if rows else None,
        }
    return out


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    value = (ordered[mid] if len(ordered) % 2
             else (ordered[mid - 1] + ordered[mid]) / 2.0)
    return round(value, 3)


# ===== episodes ============================================================


def episodes(conn: sqlite3.Connection, days: int = DEFAULT_DAYS, *,
             outcome: str = "slot") -> Dict[int, List[dict]]:
    """Contiguous runs of one outcome per combination, with their bounds.

    Each episode carries what we saw (`readings`, `observed_span_hours`) and what
    the readings on either side allow (`duration_max_hours` — from the last
    reading that did NOT show it to the first that did not again). An episode
    seen once has an observed span of zero and a maximum of the surrounding gap:
    the truth is somewhere in between and this is as far as the data goes.

    An episode still open at the end of the window, or already open at the start,
    is marked `censored` — its length is a lower bound only.
    """
    out: Dict[int, List[dict]] = {}

    for combo_id, rows in _readings(conn, days).items():
        found: List[dict] = []
        current: List[sqlite3.Row] = []
        before: Optional[sqlite3.Row] = None      # last reading that was not `outcome`

        def close(after: Optional[sqlite3.Row]) -> None:
            if not current:
                return
            first = _parse(current[0]["ts_local"])
            last = _parse(current[-1]["ts_local"])
            span = (last - first).total_seconds() / 3600.0 if first and last else 0.0
            lower = _parse(before["ts_local"]) if before else None
            upper = _parse(after["ts_local"]) if after else None
            widest = ((upper - lower).total_seconds() / 3600.0
                      if lower and upper else None)
            found.append({
                "readings": len(current),
                "first_seen": current[0]["ts_utc"],
                "last_seen": current[-1]["ts_utc"],
                "observed_span_hours": round(span, 3),
                "duration_max_hours": round(widest, 3) if widest is not None else None,
                "censored": before is None or after is None,
            })

        for row in rows:
            if row["outcome"] == outcome:
                current.append(row)
            else:
                close(row)
                current = []
                before = row
        close(None)
        if found:
            out[combo_id] = found
    return out


# ===== what a release has to survive to be seen ============================


def detection_sensitivity(cadence_hours: float,
                          durations_hours: Iterable[float] = (
                              0.083, 0.25, 0.5, 1.0, 2.0, 4.0)) -> List[dict]:
    """Catch rate at one cadence, for each assumed true episode duration.

    Sampling at a fixed interval with an arbitrary phase, a release lasting `d`
    is seen with probability `min(1, d / cadence)`. So this is not a prediction:
    it is the arithmetic that turns "we caught 38 releases" into "we caught 38 of
    somewhere between 38 and several hundred", and it names the experiment that
    settles which — poll fast enough that `d / cadence` reaches 1.
    """
    if cadence_hours <= 0:
        return []
    rows = []
    for duration in durations_hours:
        caught = min(1.0, duration / cadence_hours)
        rows.append({
            "duration_hours": duration,
            "duration_minutes": round(duration * 60),
            "catch_rate": round(caught, 4),
            # What 100 observed episodes would imply about the true count.
            "implied_true_per_100_seen": round(100.0 / caught, 1) if caught else None,
        })
    return rows


def resolved(episode_list: Iterable[dict]) -> bool:
    """Whether a combination's episodes are long enough to measure at all.

    True when some episode was seen more than once: only then does the data
    contain a positive lower bound on how long a release lasts. Until that
    happens, every miss-rate figure for this combination is an assumption
    wearing a number, and `detection_sensitivity` is the honest form.
    """
    return any(e["readings"] > 1 for e in episode_list)


# ===== regime, derived from the readings ===================================


def regime(readings: int, slot_readings: int, waitlist_readings: int,
           episode_list: List[dict]) -> str:
    """What this combination behaves like, from its own numbers only.

    Deliberately ignorant of which country it is. A waitlist-only centre that
    starts releasing slots becomes `flash` or `intermittent` on the next run,
    and a persistent one that dries up stops being `persistent`, without anyone
    editing a list.

    A combination with barely any readings is `sparse` rather than whatever its
    handful of readings happen to suggest: the scheduler treats that as "go and
    find out", which is the correct response to not knowing.
    """
    if not readings:
        return UNSEEN
    if readings < _MIN_READINGS:
        return SPARSE
    if not slot_readings:
        return WAITLIST_ONLY if waitlist_readings else DORMANT
    if slot_readings / readings >= _PERSISTENT_SHARE:
        return PERSISTENT
    if episode_list:
        brief = sum(1 for e in episode_list if e["readings"] <= _FLASH_MAX_READINGS)
        if brief / len(episode_list) >= _FLASH_MIN_SHARE:
            return FLASH
    return INTERMITTENT


def profile(conn: sqlite3.Connection, days: int = DEFAULT_DAYS, *,
            resolution_hours: float = DEFAULT_RESOLUTION_HOURS,
            closed: Optional[frozenset] = None) -> List[dict]:
    """One row per combination: coverage, behaviour, and how measurable it is.

    This is what the poll scheduler and any later model both read, so the two
    can never disagree about what a combination is doing.
    """
    closed = closed_days() if closed is None else closed
    seen = observation(conn, days, resolution_hours=resolution_hours, closed=closed)
    slot_episodes = episodes(conn, days)

    counts = {
        row["combo_id"]: row
        for row in conn.execute(
            "SELECT combo_id, COUNT(*) readings,"
            " SUM(outcome = 'slot') slot_readings,"
            " SUM(outcome = 'waitlist') waitlist_readings,"
            " SUM(outcome = 'error') error_readings"
            " FROM checks WHERE ts_utc >= ? GROUP BY combo_id",
            (_window_start(days),),
        )
    }

    rows: List[dict] = []
    for combo in conn.execute(
        "SELECT id, route, country_name, city, visa_type, purpose, enabled, in_config"
        " FROM combos ORDER BY route, config_order, id"
    ):
        cid = combo["id"]
        watch = seen.get(cid, {})
        tally = counts.get(cid)
        eps = slot_episodes.get(cid, [])
        readings = tally["readings"] if tally else 0
        slots = (tally["slot_readings"] or 0) if tally else 0
        waits = (tally["waitlist_readings"] or 0) if tally else 0

        rows.append({
            "combo_id": cid,
            "route": combo["route"],
            "country_name": combo["country_name"],
            "city": combo["city"],
            "visa_type": combo["visa_type"],
            "purpose": combo["purpose"],
            "enabled": bool(combo["enabled"]),
            "in_config": bool(combo["in_config"]),
            "readings": readings,
            "slot_readings": slots,
            "waitlist_readings": waits,
            "error_readings": (tally["error_readings"] or 0) if tally else 0,
            "slot_share": round(slots / readings, 4) if readings else None,
            "regime": regime(readings, slots, waits, eps),
            "episodes": len(eps),
            "episodes_seen_once": sum(1 for e in eps if e["readings"] == 1),
            "duration_resolved": resolved(eps),
            "shortest_bound_hours": min(
                (e["duration_max_hours"] for e in eps
                 if e["duration_max_hours"] is not None), default=None),
            "coverage": watch.get("coverage"),
            "covered_hours": watch.get("covered_hours", 0.0),
            "blind_hours": watch.get("blind_hours", 0.0),
            "median_gap_hours": watch.get("median_gap_hours"),
            "worst_gap_hours": watch.get("worst_gap_hours"),
            "last_seen": watch.get("last_seen"),
        })
    return rows


def summary(conn: sqlite3.Connection, days: int = DEFAULT_DAYS, *,
            closed: Optional[frozenset] = None) -> dict:
    """The headline figures: what we watched, and what we cannot yet measure."""
    closed = closed_days() if closed is None else closed
    rows = profile(conn, days, closed=closed)
    watched = [r for r in rows if r["readings"]]
    covered = sum(r["covered_hours"] for r in watched)
    blind = sum(r["blind_hours"] for r in watched)

    # Split by provenance: a reconstructed run with no readings means the log
    # held nothing attributable, which is not the same failure as the bot doing
    # a run and coming back empty. Pooling them inflates the apparent waste.
    runs = conn.execute(
        "SELECT COUNT(*) total,"
        " SUM(CASE WHEN NOT EXISTS"
        "   (SELECT 1 FROM checks ch WHERE ch.run_id = runs.id) THEN 1 ELSE 0 END) empty,"
        " SUM(CASE WHEN source = 'live' THEN 1 ELSE 0 END) observed,"
        " SUM(CASE WHEN source = 'live' AND NOT EXISTS"
        "   (SELECT 1 FROM checks ch WHERE ch.run_id = runs.id) THEN 1 ELSE 0 END)"
        "   observed_empty"
        " FROM runs WHERE started_at_utc >= ?",
        (_window_start(days),),
    ).fetchone()

    by_regime: Dict[str, int] = {}
    for row in rows:
        by_regime[row["regime"]] = by_regime.get(row["regime"], 0) + 1

    unresolved = [r for r in watched
                  if r["episodes"] and not r["duration_resolved"]]
    clock = hour_profile(conn, days)
    return {
        "days": days,
        # Coverage of the CLOCK, not of the combinations: an hour nobody looks
        # at is invisible to the per-combination figures below it.
        "clock": clock,
        "closed_days": sorted(WEEKDAY_NAMES[d] for d in closed),
        "combinations_watched": len(watched),
        "covered_hours": round(covered, 1),
        "blind_hours": round(blind, 1),
        "coverage": round(covered / (covered + blind), 4) if covered + blind else None,
        "runs": runs["total"] if runs else 0,
        "runs_without_readings": (runs["empty"] or 0) if runs else 0,
        "observed_runs": (runs["observed"] or 0) if runs else 0,
        "observed_runs_without_readings": ((runs["observed_empty"] or 0)
                                          if runs else 0),
        "by_regime": by_regime,
        "combinations_with_unresolved_episodes": len(unresolved),
        "episodes_seen_once": sum(r["episodes_seen_once"] for r in watched),
        "episodes": sum(r["episodes"] for r in watched),
    }


# ===== hour-of-day coverage =================================================
#
# Everything above measures coverage per COMBINATION. This measures it per HOUR
# of the local clock, which catches a different and cheaper failure: an hour we
# never look at all. A combination-level report cannot see that, because a
# combination watched sixteen hours a day looks well covered.


def hour_profile(conn: sqlite3.Connection, days: int = DEFAULT_DAYS, *,
                 blind_share: float = _BLIND_SHARE) -> dict:
    """Readings and openings per local hour, and which hours we never watch.

    An hour counts as blind when it holds less than `blind_share` of the median
    watched hour's readings — relative, not a fixed count, so the verdict does
    not change meaning when the bot's overall rate changes.

    The uplift figure assumes releases are uniform across the clock. That
    assumption cannot be checked for the hours we never watch, which is exactly
    why it is stated rather than buried: `rate_varies` reports whether the hours
    we DO watch show more variation than chance would produce, and if they do
    not, uniform is the only defensible prior for the ones we do not.
    """
    reads = {h: 0 for h in range(24)}
    opens = {h: 0 for h in range(24)}
    start = _window_start(days)
    for row in conn.execute(
            "SELECT hour_local AS h, COUNT(*) AS n FROM checks"
            " WHERE ts_utc >= ? AND hour_local IS NOT NULL GROUP BY h", (start,)):
        reads[row["h"]] = row["n"]
    for row in conn.execute(
            "SELECT hour_local AS h, COUNT(*) AS n FROM events"
            " WHERE ts_utc >= ? AND kind = 'opened' AND hour_local IS NOT NULL"
            " GROUP BY h", (start,)):
        opens[row["h"]] = row["n"]

    nonzero = sorted(n for n in reads.values() if n)
    typical = _median([float(n) for n in nonzero]) or 0.0
    threshold = typical * blind_share

    hours = []
    for h in range(24):
        n, e = reads[h], opens[h]
        hours.append({
            "hour": h,
            "readings": n,
            "openings": e,
            "openings_per_1k": round(1000.0 * e / n, 2) if n else None,
            "blind": n <= threshold,
        })

    watched = [r for r in hours if not r["blind"]]
    total_reads = sum(r["readings"] for r in watched)
    total_opens = sum(r["openings"] for r in watched)
    overall = (1000.0 * total_opens / total_reads) if total_reads else 0.0

    return {
        "days": days,
        "hours": hours,
        "watched_hours": len(watched),
        "blind_hours": [r["hour"] for r in hours if r["blind"]],
        "clock_coverage": round(len(watched) / 24.0, 4),
        # What closing the blind hours would buy, IF releases are uniform in
        # time. 1.0 means the clock is already fully watched.
        "uplift_if_uniform": round(24.0 / len(watched), 2) if watched else None,
        "openings_per_1k": round(overall, 2),
        "openings": total_opens,
        "rate_varies": _rate_varies(watched, overall),
        "busiest_hours": [r["hour"] for r in sorted(
            (r for r in watched if r["openings_per_1k"] is not None),
            key=lambda r: r["openings_per_1k"], reverse=True)[:3]],
    }


def _rate_varies(watched: List[dict], overall_per_1k: float) -> Optional[bool]:
    """Do the watched hours differ by more than chance?

    A Pearson dispersion test against one flat rate. Openings are rare and
    spread thin, so the usual answer is None (too little evidence) or False —
    and False is the useful one: it says we have no grounds to call any hour
    quiet, which is what a scheduler would otherwise wrongly assume about the
    hours nobody has looked at.
    """
    rate = overall_per_1k / 1000.0
    cells = [r for r in watched if r["readings"] * rate >= 1.0]
    if len(cells) < 3 or sum(r["openings"] for r in cells) < 10:
        return None
    chi2 = sum((r["openings"] - r["readings"] * rate) ** 2 / (r["readings"] * rate)
               for r in cells)
    dof = len(cells) - 1
    # Excess over the degrees of freedom, in units of its own sd (sqrt(2*dof)).
    return (chi2 - dof) / math.sqrt(2.0 * dof) > 2.0
