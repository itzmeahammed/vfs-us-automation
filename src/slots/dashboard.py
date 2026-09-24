"""Builds the agent-facing dashboard: one self-contained HTML file.

Self-contained on purpose. The page has no server, no build step and no network
calls — an agent opens the file (or a copy of it someone sent them) and it works.
Data is embedded as JSON at the bottom and rendered by a small script, so the
same page can re-render instantly when the party size changes.

What it has to answer, in the order an agent needs it:

    1. Which country should I pitch this client?      -> the ranking table
    2. How soon, and how sure are we?                 -> date, typical wait, week strip
    3. What do I actually say?                        -> the pitch line, copyable
    4. Which centre and visa type?                    -> the expandable detail rows
    5. When do slots appear?                          -> openings + the hour grid
    6. When will it open NEXT?                        -> the forecast range

The forecast is arithmetic on the gaps between past releases, not a trained
model, and it declines to answer more often than it answers. A row showing
"open now" or a reason instead of dates is the forecast working, not failing: a
date range offered for a combination that is open this minute would send an
agent away from a slot on the screen.

Party size is deliberately absent: each figure is the earliest date the check
offered, whichever size VFS quoted it for (see query._combo_stats).

Colour follows the data's job: availability is a magnitude, so it's one blue
ramp light->dark; state (always open / waitlist only / nothing) is a status
colour that always ships with its label, never colour alone.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from src.slots import db, forecast, query, registry

TEMPLATE = os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")
DEFAULT_OUTPUT = os.path.join("reports", "slot_dashboard.html")


def _seen_date(ts_utc: Optional[str]) -> Optional[str]:
    """The LOCAL calendar date a reading happened, as 'YYYY-MM-DD'.

    The board shows when a slot was last detected, not just how long ago — "17
    Sep" survives being read at a glance, screenshotted, or pasted into a chat
    with a client, and "26m ago" does not.
    """
    if not ts_utc:
        return None
    try:
        when = datetime.fromisoformat(ts_utc)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().date().isoformat()


# Both boards show the same forecast, so the shaping lives with the forecast
# and not with either page.
_forecast_cell = forecast.cell
_country_forecast = forecast.roll_up


def _slim_combo(combo: dict, forecasts: Optional[dict] = None) -> dict:
    """A combination as the page needs it — without the per-day working set."""
    return {
        "forecast": _forecast_cell((forecasts or {}).get(combo["combo_id"])),
        "city": combo["city"], "centre": combo["centre"],
        "visa_type": combo["visa_type"], "verdict": combo["verdict"],
        "purpose": combo["purpose"],
        "enabled": combo["enabled"], "in_config": combo["in_config"],
        "availability": round(combo["availability"], 3),
        "waitlist_rate": round(combo["waitlist_rate"], 3),
        "median_lead": combo["median_lead"], "best_lead": combo["best_lead"],
        "current_outcome": combo["current_outcome"],
        "current_bookable": combo["current_bookable"],
        "current_date": combo["current_date"],
        "current_age": query.humanise_age(combo["current_seen"]),
        "last_slot_date": combo["last_slot_date"],
        "last_slot_seen": combo["last_slot_seen"],
        "last_slot_seen_date": _seen_date(combo["last_slot_seen"]),
        "last_slot_age": query.humanise_age(combo["last_slot_seen"]),
        "openings": combo["openings"], "drift_per_day": combo["drift_per_day"],
    }


def _slim_country(country: dict, forecasts: Optional[dict] = None) -> dict:
    combos = [_slim_combo(c, forecasts) for c in country["combos"]]
    return {
        "forecast": _country_forecast([c["forecast"] for c in combos]),
        "country": country["country"], "route": country["route"],
        "dest_code": country["dest_code"], "cities": country["cities"],
        "score": country["score"],
        "score_parts": {
            "availability": country["availability_part"],
            "speed": country["speed_part"],
            "freshness": country["freshness_part"],
        },
        "verdict": country["verdict"],
        "availability": round(country["availability"], 3),
        "waitlist_rate": round(country["waitlist_rate"], 3),
        "median_lead": country["median_lead"], "best_lead": country["best_lead"],
        "current_outcome": country["current_outcome"],
        "current_bookable": country["current_bookable"],
        "current_date": country["current_date"],
        "current_city": country["current_city"],
        "current_age": query.humanise_age(country["current_seen"]),
        "last_slot_date": country["last_slot_date"],
        "last_slot_seen": country["last_slot_seen"],
        "last_slot_seen_date": _seen_date(country["last_slot_seen"]),
        "last_slot_age": query.humanise_age(country["last_slot_seen"]),
        "openings": country["openings"], "drift_per_day": country["drift_per_day"],
        "observed": country["observed"],
        "combos": combos,
    }


def _cells(grid: dict) -> list:
    return [{"weekday": wd, "hour": hr, "observed": cell["observed"],
             "rate": round(cell["rate"], 3),
             "openings": grid["openings"].get((wd, hr), 0)}
            for (wd, hr), cell in sorted(grid["grid"].items())]


def _view(conn, days: int, purpose: Optional[str],
          forecasts: Optional[dict] = None) -> dict:
    """The whole board for one visa type: ranking, grid and activity.

    Built per purpose rather than filtered in the browser because the numbers
    are aggregates, not rows — a country's availability over tourist
    combinations is not something you can recover by hiding table rows.
    """
    countries = [_slim_country(c, forecasts) for c in
                 query.rank(conn, days=days, purpose=purpose)]
    routes = [{"route": c["route"], "country": c["country"]} for c in countries]
    per_route = {
        entry["route"]: _cells(query.heatmap(conn, days=days, route=entry["route"],
                                             purpose=purpose))
        for entry in routes
    }

    return {
        "countries": countries,
        "routes": routes,
        "heatmap": {"all": _cells(query.heatmap(conn, days=days, purpose=purpose)),
                    "routes": per_route},
        "activity": query.recent_activity(conn, days=days, limit=40, purpose=purpose),
    }


def _waitlist_view(conn, days: int) -> dict:
    """The waitlist board — a different question, so different columns.

    Its activity feed carries only waitlist changes: on this tab, a slot opening
    somewhere else is noise.
    """
    countries = []
    for country in query.waitlist_board(conn, days=days):
        combos = [dict(k, last_seen_date=_seen_date(k["last_seen"]),
                       last_seen_age=query.humanise_age(k["last_seen"]))
                  for k in country["combos"]]
        countries.append(dict(
            country, combos=combos,
            offered_rate=round(country["offered_rate"], 3),
            slot_availability=round(country["slot_availability"], 3),
            last_seen_date=_seen_date(country["last_seen"]),
            last_seen_age=query.humanise_age(country["last_seen"]),
            last_slot_age=query.humanise_age(country["last_slot_seen"]),
        ))
    activity = [a for a in query.recent_activity(conn, days=days, limit=40)
                if a["kind"] == "waitlist_opened"]
    return {"countries": countries, "activity": activity}


def build_payload(conn, days: int = query.DEFAULT_DAYS) -> dict:
    """Everything the page renders: one view per visa type, each per party size."""
    # Built once for the whole board: a combination's next opening does not
    # depend on which tab it is being shown under.
    forecasts = {row["combo_id"]: row for row in forecast.forecast(conn, days)}
    return {
        "generated": datetime.now().astimezone().strftime("%d %b %Y, %H:%M"),
        "window_days": days,
        "coverage": query.coverage(conn, days),
        # One view per visa type an agent actually sells. There is no combined
        # view: a country's numbers are aggregates over ONE kind of appointment,
        # and mixing tourist with business produced a figure nobody could act on
        # (France reads 'always open' on business and 'occasional' on tourist).
        "views": {
            "tourist": _view(conn, days, registry.TOURIST, forecasts),
            "business": _view(conn, days, registry.BUSINESS, forecasts),
            "waitlist": _waitlist_view(conn, days),
        },
    }


def render(payload: dict, template_path: str = TEMPLATE) -> str:
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()
    # '</' inside a <script> block would end it early — escape it, the standard
    # fix for embedding JSON in HTML.
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return template.replace("__PAYLOAD__", data)


def build(db_path: Optional[str] = None, output: Optional[str] = None,
          days: int = query.DEFAULT_DAYS) -> str:
    """Writes the dashboard and returns the path it wrote."""
    from src.slots import store as store_mod

    path = db_path or store_mod.db_path()
    out = output or dashboard_path()
    conn = db.connect(path, read_only=True)
    try:
        html = render(build_payload(conn, days=days))
    finally:
        conn.close()

    parent = os.path.dirname(os.path.abspath(out))
    if parent:
        os.makedirs(parent, exist_ok=True)
    # Write-then-replace: an agent refreshing mid-build never sees half a page.
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, out)
    return out


def dashboard_path() -> str:
    try:
        from src.utils.config_reader import get_config_value
        return get_config_value("slots", "dashboard_path", DEFAULT_OUTPUT) or DEFAULT_OUTPUT
    except Exception:
        return DEFAULT_OUTPUT


def auto_build_enabled() -> bool:
    try:
        from src.utils.config_reader import get_config_value
        value = get_config_value("slots", "auto_build_dashboard", "true")
        return str(value).strip().lower() not in ("false", "0", "no", "off")
    except Exception:
        return False


def build_quietly() -> Optional[str]:
    """Rebuild after a run. Never raises — a failed page must not fail the bot."""
    if not auto_build_enabled():
        return None
    try:
        path = build()
        logging.info(f"Slot dashboard rebuilt: {path}")
        return path
    except Exception as e:
        logging.warning(f"Slot dashboard not rebuilt (non-fatal): {e}")
        return None
