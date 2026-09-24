"""The numbers behind the dashboard — one function per question an agent asks.

Read-only. Everything here answers a sales question, not a technical one:

    "Which country can I get this client into soonest?"      -> rank()
    "How long is the wait for Norway, realistically?"        -> typical_wait
    "Is it worth telling them to wait, or book elsewhere?"   -> availability
    "When do slots actually appear?"                         -> heatmap()

Two rules run through all of it:

  * **Only observed checks count.** A route that was paused or blocked produces
    no checks, and a country is never scored as 'no availability' for a window
    nobody looked at. Rates are always over checks that really happened.
  * **`error` readings are excluded from availability.** A dropdown that
    wouldn't open tells us nothing; counting it as 'no slot' would quietly
    punish exactly the countries whose portals are flakiest.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from src.slots.parse import WAITLIST
from src.slots.registry import ANY_PURPOSE as ANY

DEFAULT_DAYS = 7

# How recent the latest reading must be before we will put its appointment date
# on a board as something bookable. A wall-clock bar and not a multiple of the
# combination's cadence, because a VFS slot's shelf life is wall-clock: it is
# gone in minutes whether or not we happened to look. A reading older than this
# tells us what WAS there, which the "last slot seen" figures already say.
BOOKABLE_WITHIN_HOURS = 3.0

# An availability rate this high reads as 'basically always open' to an agent.
_ALWAYS = 0.85
# Lead time (days) at which 'speed' scores half marks. Two weeks is the point
# where a UAE client starts asking whether another country would be faster.
_HALF_LIFE_DAYS = 14.0


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _since(days: int) -> str:
    """UTC cut-off for a window of `days` days, as an ISO string for SQL."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _days_ago(ts_utc: Optional[str]) -> Optional[float]:
    if not ts_utc:
        return None
    try:
        then = datetime.fromisoformat(ts_utc)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 86400.0


def _bookable(outcome: Optional[str], slot_date: Optional[str],
              seen_utc: Optional[str]) -> bool:
    """Could an agent actually book this right now?

    Three things have to hold, and only the first was ever checked. A board that
    shows an appointment date in 88px type is making a promise, so it may only
    do so when all three do:

      * the latest reading found a slot;
      * that reading is recent enough to still mean something;
      * the date it offered has not already passed. An appointment date in the
        past is not a next appointment, and we were showing several.
    """
    if outcome != "slot" or not slot_date:
        return False
    days = _days_ago(seen_utc)
    if days is None or days * 24.0 > BOOKABLE_WITHIN_HOURS:
        return False
    return slot_date >= datetime.now().astimezone().date().isoformat()


def humanise_age(ts_utc: Optional[str]) -> str:
    """'3h ago' / '2d ago' — how an agent reads freshness at a glance."""
    days = _days_ago(ts_utc)
    if days is None:
        return "never"
    hours = days * 24
    if hours * 60 < 1:
        return "just now"
    if hours < 1:
        return f"{int(hours * 60)}m ago"
    if hours < 48:
        return f"{int(hours)}h ago"
    return f"{int(days)}d ago"


# ===== the per-combination core ===========================================


def purpose_filter(purpose: Optional[str]) -> List[str]:
    """Which combination purposes count when an agent picks a visa type.

    An `any` combination (Switzerland's 'SCHENGEN', Sweden's 'ShortStay',
    Germany's 'Short Stay') is one appointment type that serves a tourist and a
    business applicant alike, so it belongs in BOTH tabs. Excluding it would
    empty the Tourist tab of Sweden — currently the second-best country on the
    board — which would be worse than useless: it would be wrong.
    """
    if purpose in (None, "", "all"):
        return []
    return [purpose, ANY]


def _combo_stats(conn: sqlite3.Connection, days: int,
                 purpose: Optional[str] = None) -> Dict[int, dict]:
    """Every combination's window statistics, keyed by combo id.

    Party size is not a dimension here. VFS quotes a separate (later) date for
    two and three applicants, but an agent asks 'when can this client go', not
    'when can one of them go' — so each check contributes the EARLIEST date it
    offered, whatever party size that was. Every size stays on `slot_dates`, so
    a per-size view can be added later without re-reading a single log.
    """
    since = _since(days)
    stats: Dict[int, dict] = {}

    wanted = purpose_filter(purpose)
    sql = ("SELECT co.id, co.route, co.country_name, co.dest_code, co.city,"
           " co.centre, co.visa_type, co.category, co.sub_category, co.enabled,"
           " co.in_config, co.purpose FROM combos co")
    params: list = []
    if wanted:
        sql += " WHERE co.purpose IN (%s)" % ",".join("?" * len(wanted))
        params = wanted

    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        stats[row["id"]] = {
            "combo_id": row["id"], "route": row["route"],
            "country": row["country_name"], "dest_code": row["dest_code"],
            "city": row["city"], "centre": row["centre"],
            "visa_type": row["visa_type"] or row["category"],
            "purpose": row["purpose"],
            "enabled": bool(row["enabled"]), "in_config": bool(row["in_config"]),
            "observed": 0, "slot_checks": 0, "waitlist_checks": 0,
            "availability": 0.0, "waitlist_rate": 0.0,
            "leads": [], "median_lead": None, "best_lead": None,
            "current_outcome": None, "current_date": None, "current_seen": None,
            "current_bookable": False,
            "last_slot_date": None, "last_slot_seen": None,
            "last_waitlist_seen": None, "waitlist_openings": 0,
            "openings": 0, "last_opening": None, "drift_per_day": None,
            "by_day": {},
        }

    # Outcome mix. `error`/`disabled` are dropped here, so they can neither
    # count as availability nor dilute it.
    for row in conn.execute(
            "SELECT combo_id, outcome, date_local, COUNT(*) AS n FROM checks"
            " WHERE ts_utc >= ? AND outcome IN ('slot','waitlist','none')"
            " GROUP BY combo_id, outcome, date_local", (since,)):
        s = stats.get(row["combo_id"])
        if not s:
            continue
        s["observed"] += row["n"]
        day = s["by_day"].setdefault(row["date_local"], {"observed": 0, "slot": 0})
        day["observed"] += row["n"]
        if row["outcome"] == "slot":
            s["slot_checks"] += row["n"]
            day["slot"] += row["n"]
        elif row["outcome"] == "waitlist":
            s["waitlist_checks"] += row["n"]

    for row in conn.execute(
            "SELECT c.combo_id, MIN(sd.lead_days) AS lead_days FROM slot_dates sd"
            " JOIN checks c ON c.id = sd.check_id"
            " WHERE c.ts_utc >= ? GROUP BY sd.check_id", (since,)):
        s = stats.get(row["combo_id"])
        if s and row["lead_days"] is not None:
            s["leads"].append(row["lead_days"])

    # Latest reading per combination — what an agent would see if they logged in
    # right now. Not restricted to the window: a combination checked 10 days ago
    # should still show its last known state, clearly aged.
    for row in conn.execute(
            "SELECT c.combo_id, c.outcome, c.ts_utc,"
            " (SELECT MIN(slot_date) FROM slot_dates WHERE check_id = c.id) AS slot_date"
            " FROM checks c"
            " JOIN (SELECT combo_id, MAX(ts_utc) AS m FROM checks"
            "       WHERE outcome IN ('slot','waitlist','none') GROUP BY combo_id) last"
            "   ON last.combo_id = c.combo_id AND last.m = c.ts_utc"):
        s = stats.get(row["combo_id"])
        if s:
            s["current_outcome"] = row["outcome"]
            s["current_date"] = row["slot_date"]
            s["current_seen"] = row["ts_utc"]
            s["current_bookable"] = _bookable(row["outcome"], row["slot_date"],
                                              row["ts_utc"])

    for row in conn.execute(
            "SELECT c.combo_id, MAX(c.ts_utc) AS ts,"
            " (SELECT MIN(slot_date) FROM slot_dates WHERE check_id = c.id) AS slot_date"
            " FROM checks c WHERE c.outcome = 'slot' GROUP BY c.combo_id"):
        s = stats.get(row["combo_id"])
        if s:
            s["last_slot_seen"] = row["ts"]
            s["last_slot_date"] = row["slot_date"]

    for row in conn.execute(
            "SELECT combo_id, MAX(ts_utc) AS ts FROM checks"
            " WHERE outcome = 'waitlist' GROUP BY combo_id"):
        s = stats.get(row["combo_id"])
        if s:
            s["last_waitlist_seen"] = row["ts"]

    for row in conn.execute(
            "SELECT combo_id, COUNT(*) AS n FROM events"
            " WHERE kind = 'waitlist_opened' AND ts_utc >= ? GROUP BY combo_id", (since,)):
        s = stats.get(row["combo_id"])
        if s:
            s["waitlist_openings"] = row["n"]

    for row in conn.execute(
            "SELECT combo_id, COUNT(*) AS n, MAX(ts_utc) AS last FROM events"
            " WHERE kind = 'opened' AND ts_utc >= ? GROUP BY combo_id", (since,)):
        s = stats.get(row["combo_id"])
        if s:
            s["openings"] = row["n"]
            s["last_opening"] = row["last"]

    # How fast the earliest date slips. A positive drift is the urgency line:
    # 'the date moves about two days further out every day you wait'.
    for row in conn.execute(
            "SELECT combo_id, SUM(delta_days) AS drift, COUNT(*) AS n FROM events"
            " WHERE kind = 'date_moved' AND ts_utc >= ? GROUP BY combo_id", (since,)):
        s = stats.get(row["combo_id"])
        if s and row["drift"] is not None and days:
            s["drift_per_day"] = round(row["drift"] / float(days), 1)

    for s in stats.values():
        if s["observed"]:
            s["availability"] = s["slot_checks"] / s["observed"]
            s["waitlist_rate"] = s["waitlist_checks"] / s["observed"]
        s["median_lead"] = _median(s["leads"])
        s["best_lead"] = min(s["leads"]) if s["leads"] else None
        s.pop("leads")
    return stats


# ===== scoring =============================================================


def score(availability: float, median_lead: Optional[float],
          last_slot_seen: Optional[str], waitlist_rate: float = 0.0) -> dict:
    """The best-bet score (0-100) and the three parts it's made of.

    Deliberately simple and explainable — an agent has to be able to say *why*
    a country is top of the list, and a model they can't explain is a model they
    won't trust:

        availability  how often a slot is actually there      (45%)
        speed         how soon that slot is                   (35%)
        freshness     how recently we saw one                 (20%)

    A country with no slots all week still scores a little if it offers a
    waitlist, because that is a real (if weaker) thing to sell.
    """
    speed = 0.0
    if median_lead is not None:
        speed = 1.0 / (1.0 + max(median_lead, 0) / _HALF_LIFE_DAYS)

    age_days = _days_ago(last_slot_seen)
    if age_days is None:
        freshness = 0.0
    else:
        freshness = max(0.0, 1.0 - age_days / 7.0)

    total = 45.0 * availability + 35.0 * speed + 20.0 * freshness
    if total == 0.0 and waitlist_rate:
        total = 5.0 * waitlist_rate
    return {
        "score": round(total),
        "availability_part": round(45.0 * availability),
        "speed_part": round(35.0 * speed),
        "freshness_part": round(20.0 * freshness),
    }


def verdict(row: dict) -> str:
    """The one-word status an agent scans the table for."""
    if row["availability"] >= _ALWAYS:
        return "Always open"
    if row["availability"] >= 0.4:
        return "Usually open"
    if row["availability"] > 0:
        return "Occasional"
    if row["waitlist_rate"] > 0:
        return "Waitlist only"
    if row["observed"]:
        return "No slots"
    return "Not checked"


# ===== public API ==========================================================


def rank(conn: sqlite3.Connection, days: int = DEFAULT_DAYS,
         purpose: Optional[str] = None) -> List[dict]:
    """One row per COUNTRY, best bet first.

    A country's figure is its best centre, not its average: an agent books the
    client wherever is soonest, so averaging Abu Dhabi's drought into Dubai's
    availability would describe a country nobody actually applies to.
    """
    combos = _combo_stats(conn, days, purpose)
    by_country: Dict[str, dict] = {}

    for s in combos.values():
        country = by_country.setdefault(s["country"], {
            "country": s["country"], "route": s["route"],
            "dest_code": s["dest_code"], "cities": [],
            "observed": 0, "slot_checks": 0, "waitlist_checks": 0,
            "availability": 0.0, "waitlist_rate": 0.0,
            "median_lead": None, "best_lead": None,
            "current_outcome": None, "current_date": None, "current_seen": None,
            "current_bookable": False, "current_city": "", "last_slot_date": None, "last_slot_seen": None,
            "openings": 0, "drift_per_day": None, "combos": [], "by_day": {},
        })
        country["combos"].append(s)
        country["observed"] += s["observed"]
        country["slot_checks"] += s["slot_checks"]
        country["waitlist_checks"] += s["waitlist_checks"]
        country["openings"] += s["openings"]
        if s["city"] and s["city"] not in country["cities"]:
            country["cities"].append(s["city"])

        for day, counts in s["by_day"].items():
            agg = country["by_day"].setdefault(day, {"observed": 0, "slot": 0})
            agg["observed"] += counts["observed"]
            agg["slot"] += counts["slot"]

        # Best (soonest) current offer across this country's centres. Only a
        # BOOKABLE one counts: a country must not inherit a headline date from a
        # centre whose reading went stale weeks ago.
        if s["current_bookable"]:
            if (country["current_date"] is None
                    or s["current_date"] < country["current_date"]):
                country["current_date"] = s["current_date"]
                country["current_outcome"] = "slot"
                country["current_bookable"] = True
                country["current_seen"] = s["current_seen"]
                country["current_city"] = s["city"]
        if s["median_lead"] is not None:
            if country["median_lead"] is None or s["median_lead"] < country["median_lead"]:
                country["median_lead"] = s["median_lead"]
        if s["best_lead"] is not None:
            if country["best_lead"] is None or s["best_lead"] < country["best_lead"]:
                country["best_lead"] = s["best_lead"]
        if s["last_slot_seen"] and (country["last_slot_seen"] is None
                                    or s["last_slot_seen"] > country["last_slot_seen"]):
            country["last_slot_seen"] = s["last_slot_seen"]
            country["last_slot_date"] = s["last_slot_date"]
        if s["drift_per_day"] is not None:
            country["drift_per_day"] = max(country["drift_per_day"] or 0,
                                           s["drift_per_day"])

    out = []
    for country in by_country.values():
        # Availability is the country's BEST centre, for the same reason.
        country["availability"] = max(
            (c["availability"] for c in country["combos"]), default=0.0)
        country["waitlist_rate"] = max(
            (c["waitlist_rate"] for c in country["combos"]), default=0.0)
        if country["current_outcome"] is None:
            has_waitlist = any(c["current_outcome"] == "waitlist"
                               for c in country["combos"])
            country["current_outcome"] = "waitlist" if has_waitlist else (
                "none" if country["observed"] else None)
            if not country["current_seen"]:
                seen = [c["current_seen"] for c in country["combos"] if c["current_seen"]]
                country["current_seen"] = max(seen) if seen else None

        country.update(score(country["availability"], country["median_lead"],
                             country["last_slot_seen"], country["waitlist_rate"]))
        country["verdict"] = verdict(country)
        country["sparkline"] = sparkline(country["by_day"], days)
        country["combos"].sort(key=lambda c: (-c["availability"],
                                              c["median_lead"] if c["median_lead"]
                                              is not None else 9999))
        for combo in country["combos"]:
            combo["verdict"] = verdict(combo)
            combo["sparkline"] = sparkline(combo["by_day"], days)
        out.append(country)

    out.sort(key=lambda c: (-c["score"], c["median_lead"] if c["median_lead"]
                            is not None else 9999))
    return out


def waitlist_board(conn: sqlite3.Connection, days: int = DEFAULT_DAYS) -> List[dict]:
    """Countries offering a waitlist, the ones open right now first.

    A separate board rather than a filter on the ranking, because none of the
    slot columns mean anything here: there is no appointment date to quote, no
    wait to predict, no drift. What an agent needs is whether sign-up is open
    NOW, how dependable it has been, and whether real slots ever turn up at that
    country anyway — a waitlist at a country that also opens is a much better
    sell than one that never does.

    Purpose is ignored: a waitlist covers the country, not one appointment type.
    """
    combos = _combo_stats(conn, days)
    by_country: Dict[str, dict] = {}

    for c in combos.values():
        if not c["waitlist_checks"] and c["current_outcome"] != WAITLIST:
            continue
        country = by_country.setdefault(c["country"], {
            "country": c["country"], "route": c["route"], "cities": [],
            "open_now": False, "offered_rate": 0.0, "last_seen": None,
            "openings": 0, "slot_availability": 0.0, "last_slot_seen": None,
            "combos": [],
        })
        if c["city"] and c["city"] not in country["cities"]:
            country["cities"].append(c["city"])
        open_now = c["current_outcome"] == WAITLIST
        country["open_now"] = country["open_now"] or open_now
        country["offered_rate"] = max(country["offered_rate"], c["waitlist_rate"])
        country["openings"] += c["waitlist_openings"]
        country["slot_availability"] = max(country["slot_availability"],
                                           c["availability"])
        for field in ("last_seen", "last_slot_seen"):
            source = c["last_waitlist_seen"] if field == "last_seen" else c["last_slot_seen"]
            if source and (country[field] is None or source > country[field]):
                country[field] = source
        country["combos"].append({
            "city": c["city"], "visa_type": c["visa_type"], "purpose": c["purpose"],
            "open_now": open_now, "offered_rate": c["waitlist_rate"],
            "last_seen": c["last_waitlist_seen"], "observed": c["observed"],
        })

    out = list(by_country.values())
    for country in out:
        country["combos"].sort(key=lambda k: (not k["open_now"], -k["offered_rate"]))
        country["verdict"] = ("Open now" if country["open_now"]
                              else "Was open" if country["offered_rate"]
                              else "Closed")
    # Open now first, then the most consistently offered.
    out.sort(key=lambda c: (not c["open_now"], -c["offered_rate"], c["country"]))
    return out


def sparkline(by_day: Dict[str, dict], days: int) -> List[dict]:
    """Per-day availability for the window, oldest day first.

    Days with no checks are marked `observed = False` so the page can draw them
    as 'not checked' rather than as a bad day — the difference between "there
    was nothing" and "nobody looked" has to survive all the way to the pixel.
    """
    today = datetime.now().date()
    out = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        counts = by_day.get(day)
        out.append({
            "date": day,
            "label": (today - timedelta(days=offset)).strftime("%a"),
            "observed": bool(counts and counts["observed"]),
            "rate": (counts["slot"] / counts["observed"]) if counts and counts["observed"] else 0.0,
        })
    return out


def heatmap(conn: sqlite3.Connection, days: int = DEFAULT_DAYS,
            route: Optional[str] = None, purpose: Optional[str] = None) -> dict:
    """Weekday x hour availability, plus where openings actually landed.

    Cells carry their own observation count so the page can grey out hours
    nobody checked — with routes rotating, some hours genuinely have no data,
    and an empty cell drawn as 'no slots' would send agents looking at the wrong
    time of day.
    """
    since = _since(days)
    # One filter, built once, used by BOTH queries below — they must always
    # describe the same slice, or the opening rings would land on cells from a
    # different set of countries than the shading.
    clauses = ["{alias}.ts_utc >= ?"]
    params = [since]
    if route:
        clauses.append("co.route = ?")
        params.append(route.upper())
    wanted = purpose_filter(purpose)
    if wanted:
        clauses.append("co.purpose IN (%s)" % ",".join("?" * len(wanted)))
        params.extend(wanted)
    where = " AND ".join(clauses)

    grid = {}
    for row in conn.execute(
            "SELECT c.weekday, c.hour_local, COUNT(*) AS n,"
            " SUM(c.outcome = 'slot') AS slots FROM checks c"
            " JOIN combos co ON co.id = c.combo_id"
            f" WHERE {where.format(alias='c')}"
            " AND c.outcome IN ('slot','waitlist','none')"
            " GROUP BY c.weekday, c.hour_local", params):
        grid[(row["weekday"], row["hour_local"])] = {
            "observed": row["n"],
            "rate": (row["slots"] or 0) / row["n"] if row["n"] else 0.0,
        }

    openings = {}
    for row in conn.execute(
            "SELECT e.weekday, e.hour_local, COUNT(*) AS n FROM events e"
            " JOIN combos co ON co.id = e.combo_id"
            f" WHERE {where.format(alias='e')} AND e.kind = 'opened'"
            " GROUP BY e.weekday, e.hour_local", params):
        openings[(row["weekday"], row["hour_local"])] = row["n"]

    return {"grid": grid, "openings": openings}


def recent_openings(conn: sqlite3.Connection, days: int = DEFAULT_DAYS,
                    limit: int = 25) -> List[dict]:
    """The latest moments a combination went from nothing to bookable."""
    since = _since(days)
    return [dict(row) for row in conn.execute(
        "SELECT e.ts_local, e.new_date, e.gap_hours, co.country_name, co.city,"
        " co.visa_type FROM events e JOIN combos co ON co.id = e.combo_id"
        " WHERE e.kind = 'opened' AND e.ts_utc >= ?"
        " ORDER BY e.ts_utc DESC LIMIT ?", (since, limit))]


def recent_activity(conn: sqlite3.Connection, days: int = DEFAULT_DAYS,
                    limit: int = 40, purpose: Optional[str] = None) -> List[dict]:
    """Everything an agent could act on, newest first.

    Openings are not the only actionable event, and on this data they are the
    RARER one: the countries worth selling (Norway, Sweden, France) are open
    continuously, so they never transition from nothing — they only move. A feed
    of openings alone shows days of silence while the board is in fact busy, and
    hides the single most valuable moment of all: an earliest date jumping
    *closer*, which is VFS releasing slots someone can be booked into today.

    So this carries four kinds:
        opened          nothing -> bookable
        moved_earlier   the date jumped closer (a release)
        closed          the slot is gone
        waitlist_opened the fallback became available

    A date drifting further out is deliberately NOT here. It is normal decay as
    slots get taken, it happens constantly, and it would bury the rest.
    """
    since = _since(days)
    wanted = purpose_filter(purpose)
    sql = ("SELECT e.kind, e.ts_local, e.prev_date, e.new_date, e.delta_days,"
           " co.country_name, co.city, co.visa_type FROM events e"
           " JOIN combos co ON co.id = e.combo_id"
           " WHERE e.ts_utc >= ? AND ("
           "   e.kind IN ('opened', 'closed', 'waitlist_opened')"
           "   OR (e.kind = 'date_moved' AND e.delta_days < 0))")
    params: list = [since]
    if wanted:
        sql += " AND co.purpose IN (%s)" % ",".join("?" * len(wanted))
        params.extend(wanted)
    sql += " ORDER BY e.ts_utc DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()

    out = []
    for row in rows:
        item = dict(row)
        if item["kind"] == "date_moved":
            item["kind"] = "moved_earlier"
            item["days_earlier"] = abs(item["delta_days"] or 0)
        out.append(item)
    return out


def coverage(conn: sqlite3.Connection, days: int = DEFAULT_DAYS) -> dict:
    """What the page is built on: window, first/last check, totals."""
    since = _since(days)
    row = conn.execute(
        "SELECT COUNT(*) AS checks, MIN(ts_local) AS first, MAX(ts_local) AS last"
        " FROM checks WHERE ts_utc >= ?", (since,)).fetchone()
    countries = conn.execute(
        "SELECT COUNT(DISTINCT co.country_name) AS n FROM checks c"
        " JOIN combos co ON co.id = c.combo_id WHERE c.ts_utc >= ?", (since,)).fetchone()
    return {"days": days, "checks": row["checks"], "first": row["first"],
            "last": row["last"], "countries": countries["n"]}
