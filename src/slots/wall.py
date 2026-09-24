"""The wall board — the card-wall design, fed by the live slot history.

A second, separate page from `dashboard.py`. The dashboard is a working tool an
agent reads at their desk; this is a **screen in the room**: country cards, a
change feed and a ticker, sized 1920x1080 and scaled to whatever it is shown on.

It deliberately mirrors the dashboard's CONTENT, so an agent who glances at the
wall and then opens the dashboard sees the same thing named the same way:

  * the same three views — Tourist, Business, Waitlist. A screen nobody touches
    cannot be clicked, so it rotates through them instead of offering tabs;
  * the same numbers per country — next appointment, typical wait, soonest
    seen, last slot seen (as a date), date drift;
  * none of what the dashboard dropped — no week strip, no pitch sentence.

The layout, colours, type and spacing come from the design mockup in
`Card wall (your concept)-html/Main.dc.html`; that file stays untouched as the
reference. Data comes from the same `query` layer as the dashboard, so the two
can never disagree about a number.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

from src.slots import db, forecast, query, registry

TEMPLATE = os.path.join(os.path.dirname(__file__), "templates", "wall.html")
DEFAULT_OUTPUT = os.path.join("reports", "slot_wall.html")

# Six cards: the design's 3x2 grid. More would shrink the type past reading
# distance, which is the one thing this page cannot trade away.
CARD_COUNT = 6

# How long each view stays on screen before the wall moves to the next.
ROTATE_SECONDS = 20
# ===== formatting ==========================================================


def _fmt_date(iso: Optional[str]) -> str:
    """'2026-09-18' -> 'Sep 18' — the mockup's format, and the dashboard's."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%b %d")
    except ValueError:
        return ""


def _fmt_lead(days) -> str:
    return "—" if days is None else f"{int(round(days))}d"


def _fmt_drift(per_day) -> str:
    if per_day is None:
        return "—"
    return f"{per_day:+g}d"


def _seen_date(ts_utc: Optional[str]) -> str:
    """When a reading happened: an age today, a date before that.

    "Sep 22" is useless on the 22nd — it reads as old news when it may be
    minutes fresh. Within today the board shows how long ago instead, which is
    the question actually being asked of this figure.
    """
    if not ts_utc:
        return "never"
    try:
        when = datetime.fromisoformat(ts_utc)
    except ValueError:
        return "never"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone()
    if local.date() == datetime.now(local.tzinfo).date():
        return query.humanise_age(ts_utc).replace(" ago", "")
    return local.strftime("%b %d")


def _relative(ts_local: Optional[str]) -> str:
    """'2m' / '4h' / '3d' — the feed's right-hand column, kept very short."""
    if not ts_local:
        return ""
    try:
        then = datetime.fromisoformat(ts_local)
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.astimezone()
    minutes = (datetime.now(then.tzinfo) - then).total_seconds() / 60
    if minutes < 60:
        return f"{max(0, int(minutes))}m"
    if minutes < 60 * 24:
        return f"{int(minutes / 60)}h"
    return f"{int(minutes / 1440)}d"


# ===== slot views (Tourist / Business) =====================================


# What a card says about its next opening, per refusal reason. Kept short: this
# is read from across a room. `open_now` is absent on purpose -- the card's
# headline already carries the date, and repeating it would waste the line.
# Only the reasons the card does not already show. A card whose headline reads
# "Waitlist only" or "No slots" says that plainly enough; repeating it on this
# line would spend the space and tell an agent nothing new.
#
# Each carries its own label, because "Next opening" is the wrong heading for a
# combination that may be open at this moment and simply has not been looked at.
_FORECAST_WORDS = {
    "unconfirmed": ("Right now", "was open, not checked since", "due"),
    "overdue": ("Next opening", "quiet longer than ever before", "quiet"),
    "stale": ("Next opening", "not checked recently", "quiet"),
    "insufficient": ("Next opening", "too little history to say", "quiet"),
}


def _forecast_line(cell: dict) -> Optional[dict]:
    """One line for the card, or None when there is nothing worth the space."""
    if not cell or cell.get("reason") == "open_now":
        return None
    if cell.get("from"):
        value = (_fmt_date(cell["from"]) if cell.get("same_day")
                 else _fmt_date(cell["from"]) + " – " + _fmt_date(cell["to"]))
        return {"label": "Next opening", "value": value,
                "note": cell.get("confidence", ""), "kind": "due"}
    words = _FORECAST_WORDS.get(cell.get("reason", ""))
    if not words:
        return None
    label, value, kind = words
    note = ""
    if cell.get("reason") == "unconfirmed" and cell.get("since"):
        note = _relative(cell["since"])
    return {"label": label, "value": value, "note": note, "kind": kind}


def _slot_card(country: dict, rank: int, forecasts: Optional[dict] = None) -> dict:
    # Bookable, not merely "the last reading saw a slot". A date in 88px type is
    # a promise that an agent can act on it now; a stale reading or a date that
    # has already passed cannot back that, and the empty state says more.
    has_date = bool(country["current_bookable"])

    cells = [forecast.cell((forecasts or {}).get(k["combo_id"]))
             for k in country["combos"]]
    rolled = forecast.roll_up(cells) if cells else {}
    line = _forecast_line(rolled)

    centre = ""
    if has_date:
        best = next((k for k in country["combos"]
                     if k["current_date"] == country["current_date"]), None)
        parts = [country["current_city"] or (best or {}).get("city", "")]
        if best and best.get("visa_type"):
            parts.append(best["visa_type"])
        centre = ", ".join(p for p in parts if p)

    # The chip says what is true NOW, not what was true across the window. A
    # country with a slot on the screen read "Occasional" because that is its
    # history — accurate, and the opposite of useful to someone deciding whether
    # to call a client. History still shows, in the score and the footer.
    if has_date:
        live, live_kind = "Open now", "open"
    elif rolled.get("reason") == "unconfirmed" and rolled.get("since"):
        live, live_kind = f"Was open {_relative(rolled['since'])} ago", "occ"
    elif country["current_outcome"] == "waitlist":
        live, live_kind = "Waitlist only", "wait"
    elif country["current_outcome"] == "none":
        live, live_kind = "No slots now", "wait"
    else:
        live, live_kind = "Not checked", "none"

    # The empty state has to say what to DO, not just what is missing.
    if country["verdict"] == "Waitlist only":
        empty_title, empty_sub = "Waitlist only", "Register the client, wait for a drop"
    elif country["verdict"] == "Not checked":
        empty_title, empty_sub = "Not checked", "No data for this country yet"
    elif country["last_slot_date"]:
        # No heading: "Worth watching" was a label for the fact below it, and the
        # fact says more. The sub carries the card on its own.
        empty_title = ""
        empty_sub = (f"Last slot {_fmt_date(country['last_slot_date'])}, "
                     f"seen {query.humanise_age(country['last_slot_seen'])}")
    else:
        empty_title, empty_sub = "No slots", "Nothing seen in the window"

    # What puts a card near the top. Availability first and score second: a
    # board is read left to right, so what can be booked now has to be in the
    # first position, whatever its history score says. Within the bookable tier
    # the soonest appointment leads.
    if has_date:
        tier, within = 0, country["current_date"] or ""
    elif rolled.get("reason") == "unconfirmed":
        tier, within = 1, ""
    else:
        tier, within = 2, ""

    return {
        "rank": rank,
        "order": [tier, within, -country["score"]],
        "country": country["country"],
        "forecast": line,
        "meter_value": str(country["score"]),
        "meter": country["score"],
        "kind": live_kind,
        "status": live,
        "badge": "",          # filled in from the activity feed
        "has_date": bool(has_date),
        "headline_label": "Next appointment",
        "headline": _fmt_date(country["current_date"]) if has_date else "",
        "centre": centre,
        "empty_title": empty_title,
        "empty_sub": empty_sub,
        # The dashboard's four numbers, laid out in the mockup's footer style:
        # a group bottom-left (where the week bars used to be) and one bottom-right.
        "stats_left": [
            {"label": "Wait", "value": _fmt_lead(country["median_lead"])},
            {"label": "Soonest", "value": _fmt_lead(country["best_lead"])},
        ],
        "stats_right": [
            {"label": "Seen", "value": _seen_date(country["last_slot_seen"])},
            {"label": "Drift", "value": _fmt_drift(country["drift_per_day"])},
        ],
    }


def _feed(activity: List[dict], limit: int = 5) -> List[dict]:
    """The change feed, phrased for someone glancing at it from a desk away."""
    out = []
    for item in activity[:limit]:
        kind, city = item["kind"], item.get("city") or ""
        country = item["country_name"]
        visa = item.get("visa_type") or ""
        if kind == "opened":
            title = f"{country}: new slot {_fmt_date(item['new_date'])}"
            sub, dot = ", ".join(p for p in (city, visa) if p), "open"
        elif kind == "moved_earlier":
            days = item.get("days_earlier") or 0
            title = (f"{country}: {_fmt_date(item['new_date'])}, "
                     f"{days} day{'' if days == 1 else 's'} sooner")
            sub, dot = f"{city}, was {_fmt_date(item['prev_date'])}", "open"
        elif kind == "closed":
            title = f"{country}: slot gone"
            sub, dot = f"{city}, was {_fmt_date(item['prev_date'])}", "occ"
        else:
            title = f"{country}: waitlist opened"
            sub, dot = ", ".join(p for p in (city, visa) if p), "wait"
        out.append({"title": title, "sub": sub, "kind": dot,
                    "when": _relative(item["ts_local"])})
    return out


def _slot_view(conn, days: int, purpose: str) -> dict:
    ranked = query.rank(conn, days=days, purpose=purpose)
    activity = query.recent_activity(conn, days=days, limit=12, purpose=purpose)
    forecasts = {row["combo_id"]: row for row in forecast.forecast(conn, days)}
    # Built for EVERY country before the board is cut to its top cards: sorting
    # after the slice would drop a bookable country that scored ninth on history
    # and show a waitlist-only one that scored third.
    cards = [_slot_card(c, 0, forecasts) for c in ranked]
    cards.sort(key=lambda c: c["order"])
    # Ranked across the WHOLE board, then cut. The table behind the button shows
    # every country, and a country has to keep one rank in both places or the
    # two views disagree about who is third.
    for i, card in enumerate(cards):
        card["rank"] = i + 1
    every = cards
    cards = cards[:CARD_COUNT]

    # A country that opened or jumped closer in the last hour wears a badge —
    # the whole point of a wall board is catching that from across the room.
    recent = {}
    for a in activity:                                   # newest first
        if (a["kind"] in ("opened", "moved_earlier")
                and _relative(a["ts_local"]).endswith("m")):
            recent.setdefault(a["country_name"], a)
    for card in every:
        hit = recent.get(card["country"])
        if hit:
            card["badge"] = ("New slot" if hit["kind"] == "opened"
                             else f"{hit.get('days_earlier', 0)}d sooner")

    open_now = [c for c in ranked if c["current_bookable"]]
    soonest = min((c["current_date"] for c in open_now if c["current_date"]),
                  default=None)
    waitlist_only = [c for c in ranked if c["verdict"] == "Waitlist only"]
    label = "Tourist" if purpose == registry.TOURIST else "Business"

    return {
        "name": label,
        "subtitle": f"{label} appointments in the UAE, ranked live",
        "feed_title": "What just changed",
        "feed_empty": "Nothing has changed in the window.",
        "kpis": [
            {"label": "Top pick", "value": ranked[0]["country"] if ranked else "—"},
            {"label": "Soonest", "value": _fmt_date(soonest) or "—"},
            {"label": "Open now", "value": f"{len(open_now)} of {len(ranked)}"},
            {"label": "Waitlist only", "value": str(len(waitlist_only))},
        ],
        "cards": cards,
        "table": every,
        "feed": _feed(activity),
        # The mockup's bottom box, kept for the look — but as a plain statement
        # of the top card, not a sales line (the dashboard dropped the pitch).
        "note_label": "Best right now",
        "note": _best_note(cards),
    }


def _best_note(cards: List[dict]) -> str:
    lead = next((c for c in cards if c["has_date"]), None)
    if not lead:
        return "No country has an appointment on offer right now."
    wait = next((s["value"] for s in lead["stats_left"] if s["label"] == "Wait"), "—")
    return f"{lead['country']}, {lead['headline']} at {lead['centre']}. Typical wait {wait}."


# ===== waitlist view =======================================================


def _waitlist_card(country: dict, rank: int) -> dict:
    if country["slot_availability"] >= 0.85:
        slots_too = "Always"
    elif country["slot_availability"] > 0:
        slots_too = f"{round(country['slot_availability'] * 100)}%"
    else:
        slots_too = "Never"
    offered = round(country["offered_rate"] * 100)
    return {
        "rank": rank,
        "country": country["country"],
        "meter_value": f"{offered}%",
        "meter": offered,
        "kind": "wait" if country["open_now"] else "occ",
        "status": country["verdict"],
        "badge": "",
        "has_date": False,
        "empty_title": "Waitlist open" if country["open_now"] else "Waitlist closed",
        "empty_sub": ("Register the client, wait for a drop"
                      if country["open_now"]
                      else f"Last open {_seen_date(country['last_seen'])}"),
        "stats_left": [
            {"label": "Offered", "value": f"{offered}%"},
            {"label": "Real slots", "value": slots_too},
        ],
        "stats_right": [
            {"label": "Seen", "value": _seen_date(country["last_seen"])},
        ],
    }


def _waitlist_view(conn, days: int) -> dict:
    board = query.waitlist_board(conn, days=days)
    open_now = [c for c in board if c["open_now"]]
    dependable = [c for c in open_now if c["offered_rate"] >= 0.95]
    also_slots = [c for c in open_now if c["slot_availability"] > 0]
    activity = [a for a in query.recent_activity(conn, days=days, limit=40)
                if a["kind"] == "waitlist_opened"]
    waitlist_cards = [_waitlist_card(c, i + 1) for i, c in enumerate(board)]
    return {
        "name": "Waitlist",
        "subtitle": "Waitlists open in the UAE — register now, take the drop",
        "feed_title": "Waitlist changes",
        "feed_empty": "No waitlist opened or closed in the window — the open ones stayed open.",
        "kpis": [
            {"label": "Open now", "value": str(len(open_now))},
            {"label": "Every check", "value": str(len(dependable))},
            {"label": "Real slots too", "value": str(len(also_slots))},
            {"label": "Countries", "value": str(len(board))},
        ],
        "cards": waitlist_cards[:CARD_COUNT],
        "table": waitlist_cards,
        "feed": _feed(activity),
        "note_label": "Waitlists open now",
        "note": (", ".join(c["country"] for c in open_now) + "."
                 if open_now else "No waitlist is open right now."),
    }


# ===== assembly ============================================================


def build_payload(conn, days: int = query.DEFAULT_DAYS) -> dict:
    """Everything the wall shows, already formatted — the page does no maths.

    Three views in the dashboard's tab order. The wall starts on Tourist and
    rotates; each view is complete on its own, so any single frame is correct.
    """
    return {
        "generated_iso": datetime.now().astimezone().isoformat(timespec="seconds"),
        "generated": datetime.now().astimezone().strftime("%a %d %b, %H:%M"),
        "rotate_seconds": ROTATE_SECONDS,
        "views": [
            _slot_view(conn, days, registry.TOURIST),
            _slot_view(conn, days, registry.BUSINESS),
            _waitlist_view(conn, days),
        ],
    }


def render(payload: dict, template_path: str = TEMPLATE) -> str:
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return template.replace("__PAYLOAD__", data)


def build(db_path: Optional[str] = None, output: Optional[str] = None,
          days: int = query.DEFAULT_DAYS) -> str:
    from src.slots import store as store_mod

    path = db_path or store_mod.db_path()
    out = output or wall_path()
    conn = db.connect(path, read_only=True)
    try:
        html = render(build_payload(conn, days=days))
    finally:
        conn.close()

    parent = os.path.dirname(os.path.abspath(out))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, out)
    return out


def wall_path() -> str:
    try:
        from src.utils.config_reader import get_config_value
        return get_config_value("slots", "wall_path", DEFAULT_OUTPUT) or DEFAULT_OUTPUT
    except Exception:
        return DEFAULT_OUTPUT


def build_quietly() -> Optional[str]:
    """Rebuild after a run. Never raises — a screen must not fail a slot check."""
    from src.slots import dashboard
    if not dashboard.auto_build_enabled():
        return None
    try:
        path = build()
        logging.info(f"Slot wall rebuilt: {path}")
        return path
    except Exception as e:
        logging.warning(f"Slot wall not rebuilt (non-fatal): {e}")
        return None
