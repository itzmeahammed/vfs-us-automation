"""The wall board — the big-screen view.

It mirrors the dashboard's content in the card-wall mockup's look, so the tests
check both halves of that: the same three views and the same numbers as the
dashboard, and none of what the dashboard dropped (the week strip).
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.slots import query, wall  # noqa: E402
from src.slots.store import SlotStore  # noqa: E402

GST = timezone(timedelta(hours=4))
# 'Sep 17' / 'Oct 08' — the mockup's and the dashboard's date format.
MON_DAY = re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{2}$")

NOR = {"centre": "Norway Visa Application Center - Abu Dhabi",
       "category": "Short Stay", "sub_category": "Tourist"}
HUN = {"centre": "Dubai", "category": "Short Stay", "sub_category": "Tourist"}
ITA = {"centre": "Dubai", "category": "Schengen Visa", "sub_category": ""}
FRA = {"centre": "Abu Dhabi", "category": "Short Stay - Business", "sub_category": ""}
ROUTES = {"AE-NOR": [NOR], "AE-HUN": [HUN], "AE-ITA": [ITA], "AE-FRA": [FRA]}
WAITLIST = "WAITLIST — no slots; waitlist sign-up available"


def slot_msg(days_ahead: int, checked_on: datetime) -> str:
    when = (checked_on.date() + timedelta(days=days_ahead)).strftime("%d-%m-%Y")
    return f"Earliest available slot for 1 Applicants is : {when}"


@pytest.fixture
def store(tmp_path):
    routes = tmp_path / "routes"
    routes.mkdir()
    for route, combos in ROUTES.items():
        (routes / f"{route}.json").write_text(
            json.dumps({"slot_check": {"combinations": combos}}), encoding="utf-8")
    s = SlotStore.open(str(tmp_path / "slots.db"))
    s.sync_combos(str(routes))
    yield s
    s.close()


def _fill(store):
    now = datetime.now(GST)
    for hours_ago in range(1, 48, 6):
        ts = now - timedelta(hours=hours_ago)
        store.record_check("AE-NOR", slot_msg(3, ts), combo=NOR, ts=ts)
        store.record_check("AE-FRA", slot_msg(20, ts), combo=FRA, ts=ts)
        store.record_check("AE-HUN", "No slot message shown (no availability?).",
                           combo=HUN, ts=ts)
        store.record_check("AE-ITA", WAITLIST, combo=ITA, ts=ts)
    return now


def view(payload, name):
    return next(v for v in payload["views"] if v["name"] == name)


# ===== parity with the dashboard ============================================


def test_the_wall_has_the_dashboards_three_tabs_in_order(store):
    _fill(store)
    payload = wall.build_payload(store.conn, days=7)
    assert [v["name"] for v in payload["views"]] == ["Tourist", "Business", "Waitlist"]


def test_each_slot_card_carries_the_dashboards_four_numbers(store):
    _fill(store)
    lead = view(wall.build_payload(store.conn, days=7), "Tourist")["cards"][0]
    labels = [s["label"] for s in lead["stats_left"] + lead["stats_right"]]
    # Typical wait, soonest seen, last slot seen, date drift — footer-sized.
    assert labels == ["Wait", "Soonest", "Seen", "Drift"]


def _seen_of(card):
    return next(s["value"] for s in card["stats_right"] if s["label"] == "Seen")


def test_last_slot_seen_shows_an_age_when_it_happened_today(store):
    """"Sep 22" on the 22nd reads as old news when it may be minutes fresh.

    The figure is there to answer "is this still live?", and within today an age
    answers that where a date cannot.
    """
    _fill(store)
    lead = view(wall.build_payload(store.conn, days=7), "Tourist")["cards"][0]
    seen = _seen_of(lead)
    assert not MON_DAY.match(seen), seen
    assert re.match(r"^(just now|\d+[mhd])$", seen), seen


def test_last_slot_seen_is_still_a_date_once_the_day_has_turned(store):
    """Before today, the day itself is what an agent needs — not "31h"."""
    old = datetime.now(GST) - timedelta(days=3)
    store.record_check("AE-NOR", slot_msg(9, old), combo=NOR, ts=old)
    cards = [c for v in wall.build_payload(store.conn, days=30)["views"]
             for c in v.get("cards", []) if c["country"] == "Norway"]
    assert cards
    assert MON_DAY.match(_seen_of(cards[0])), _seen_of(cards[0])


def test_the_week_strip_is_gone_as_on_the_dashboard(store):
    _fill(store)
    payload = wall.build_payload(store.conn, days=7)
    for v in payload["views"]:
        for card in v["cards"]:
            assert "week" not in card


def test_dates_use_the_mockups_format(store):
    _fill(store)
    lead = view(wall.build_payload(store.conn, days=7), "Tourist")["cards"][0]
    assert lead["has_date"] is True
    assert MON_DAY.match(lead["headline"]), lead["headline"]


# ===== the views are genuinely different ===================================


def test_tourist_and_business_rank_different_countries(store):
    """France's slot is business-only; Norway's is tourist-only."""
    _fill(store)
    payload = wall.build_payload(store.conn, days=7)
    tourist = [c["country"] for c in view(payload, "Tourist")["cards"] if c["has_date"]]
    business = [c["country"] for c in view(payload, "Business")["cards"] if c["has_date"]]
    assert "Norway" in tourist and "France" not in tourist
    assert "France" in business and "Norway" not in business


def test_the_waitlist_view_lists_open_waitlists(store):
    _fill(store)
    waitlist = view(wall.build_payload(store.conn, days=7), "Waitlist")
    italy = waitlist["cards"][0]
    assert italy["country"] == "Italy"
    assert italy["status"] == "Open now"
    assert italy["meter_value"] == "100%"
    assert italy["empty_title"] == "Waitlist open"
    kpis = {k["label"]: k["value"] for k in waitlist["kpis"]}
    assert kpis["Open now"] == "1"


# ===== what a glance must convey ============================================


def test_the_wall_leads_with_the_best_country_and_its_date(store):
    _fill(store)
    lead = view(wall.build_payload(store.conn, days=7), "Tourist")["cards"][0]
    assert lead["rank"] == 1
    assert lead["country"] == "Norway"
    assert lead["kind"] == "open"


def test_a_country_with_no_slots_says_what_to_do_instead(store):
    _fill(store)
    cards = {c["country"]: c for c in
             view(wall.build_payload(store.conn, days=7), "Tourist")["cards"]}
    assert cards["Italy"]["has_date"] is False
    assert cards["Italy"]["empty_title"] == "Waitlist only"
    assert "Register the client" in cards["Italy"]["empty_sub"]


def test_the_wall_never_shows_more_cards_than_the_grid_holds(store):
    _fill(store)
    for v in wall.build_payload(store.conn, days=7)["views"]:
        assert len(v["cards"]) <= wall.CARD_COUNT


def test_a_fresh_opening_earns_a_badge(store):
    now = datetime.now(GST)
    store.record_check("AE-HUN", "No slot message shown (no availability?).",
                       combo=HUN, ts=now - timedelta(minutes=40))
    store.record_check("AE-HUN", slot_msg(4, now), combo=HUN,
                       ts=now - timedelta(minutes=5))
    hungary = next(c for c in view(wall.build_payload(store.conn, days=7), "Tourist")["cards"]
                   if c["country"] == "Hungary")
    assert hungary["badge"] == "New slot"


def test_an_older_opening_does_not_keep_the_badge(store):
    now = datetime.now(GST)
    store.record_check("AE-HUN", "No slot message shown (no availability?).",
                       combo=HUN, ts=now - timedelta(hours=9))
    store.record_check("AE-HUN", slot_msg(4, now), combo=HUN,
                       ts=now - timedelta(hours=8))
    hungary = next(c for c in view(wall.build_payload(store.conn, days=7), "Tourist")["cards"]
                   if c["country"] == "Hungary")
    assert hungary["badge"] == ""


def test_the_headline_numbers_match_the_cards(store):
    _fill(store)
    tourist = view(wall.build_payload(store.conn, days=7), "Tourist")
    kpis = {k["label"]: k["value"] for k in tourist["kpis"]}
    assert kpis["Top pick"] == tourist["cards"][0]["country"]
    assert kpis["Soonest"] == tourist["cards"][0]["headline"]


def test_the_feed_holds_no_more_rows_than_the_panel_shows(store):
    _fill(store)
    for v in wall.build_payload(store.conn, days=7)["views"]:
        assert len(v["feed"]) <= 5


# ===== the page ============================================================


def test_the_page_is_self_contained_and_states_when_it_was_built(store, tmp_path):
    _fill(store)
    out = tmp_path / "wall.html"
    wall.build(db_path=store.db_path, output=str(out), days=7)
    html = out.read_text(encoding="utf-8")
    assert "__PAYLOAD__" not in html
    assert "Norway" in html
    assert "generated_iso" in html
    assert 'http-equiv="refresh"' in html
    assert html.count("https://") == html.count("https://fonts.g")


def test_an_empty_database_still_produces_a_wall(store, tmp_path):
    out = tmp_path / "wall.html"
    wall.build(db_path=store.db_path, output=str(out), days=7)
    assert "Schengen Slot Board" in out.read_text(encoding="utf-8")


def test_a_failed_wall_build_never_raises(monkeypatch):
    from src.slots import dashboard
    monkeypatch.setattr(dashboard, "auto_build_enabled", lambda: True)
    monkeypatch.setattr(wall, "build",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
    assert wall.build_quietly() is None


# ===== the forecast line on a card =========================================
#
# The wall is read from across a room, so a card earns its line only when the
# line says something the card does not already say.


def test_a_card_with_a_date_range_shows_it():
    line = wall._forecast_line({"from": "2026-09-26", "to": "2026-10-24",
                                "confidence": "pooled", "same_day": False})
    assert line["kind"] == "due"
    assert line["value"] == "Sep 26 – Oct 24"
    assert line["note"] == "pooled"


def test_a_single_day_range_is_not_printed_twice():
    line = wall._forecast_line({"from": "2026-10-16", "to": "2026-10-16",
                                "confidence": "pooled", "same_day": True})
    assert line["value"] == "Oct 16"


def test_a_card_that_is_open_now_spends_no_line_on_a_forecast():
    """Its headline already carries the date. Saying it twice wastes the card."""
    assert wall._forecast_line({"reason": "open_now", "since": "x"}) is None


def test_a_card_says_nothing_it_already_says_in_its_headline():
    """'Waitlist only' and 'No slots' are the card's own empty state."""
    for reason in ("waitlist_only", "dormant", "never_opened"):
        assert wall._forecast_line({"reason": reason}) is None


def test_a_reason_the_card_does_not_already_show_earns_its_line():
    for reason in ("overdue", "stale", "insufficient"):
        line = wall._forecast_line({"reason": reason})
        assert line is not None and line["kind"] == "quiet"
        assert line["value"]


def test_a_missing_forecast_is_simply_no_line():
    assert wall._forecast_line(None) is None
    assert wall._forecast_line({}) is None


# ===== a big date is a promise =============================================
#
# The wall shows the appointment date in 88px type. That is a promise an agent
# acts on, so it may only appear when the slot can actually be booked now:
# a fresh reading, and a date that has not already passed. The board was showing
# "Next appointment Sep 03" off a reading 56 days old.


def test_a_fresh_slot_with_a_future_date_is_bookable():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    assert query._bookable("slot", "2099-01-01", now) is True


def test_a_stale_reading_is_not_bookable_however_good_the_date():
    old = (datetime.now(timezone.utc) - timedelta(days=56)).isoformat(timespec="seconds")
    assert query._bookable("slot", "2099-01-01", old) is False


def test_an_appointment_date_in_the_past_is_not_a_next_appointment():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    assert query._bookable("slot", "2020-01-01", now) is False


def test_no_slot_is_never_bookable():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    assert query._bookable("none", None, now) is False
    assert query._bookable("waitlist", None, now) is False


def test_a_card_that_is_not_bookable_shows_no_headline_date(store):
    """It falls back to the empty state, which says when a slot was last seen."""
    old = datetime.now(GST) - timedelta(days=40)
    store.record_check("AE-NOR", slot_msg(3, old), combo=NOR, ts=old)
    payload = wall.build_payload(store.conn, days=60)
    cards = [c for v in payload["views"] for c in v.get("cards", [])
             if c["country"] == "Norway"]
    assert cards, "Norway should still be on the board"
    assert all(not c["has_date"] for c in cards)
    # No heading on this one by design: the fact below it says more than
    # "Worth watching" did, so it carries the card alone.
    assert all(not c["empty_title"] for c in cards)
    assert all("Last slot" in c["empty_sub"] for c in cards)


# ===== a slot nobody has confirmed =========================================


def test_a_slot_seen_but_not_checked_since_is_its_own_state():
    """Not 'open now' and not a future prediction — 'go and look'.

    This is the gap that opened when the headline got stricter than the
    forecast: the card showed neither a date nor a line.
    """
    line = wall._forecast_line({"reason": "unconfirmed",
                                "since": "2026-09-22T11:39+04:00"})
    assert line is not None
    assert line["label"] == "Right now"
    assert line["kind"] == "due"


def test_the_forecast_and_the_headline_agree_on_what_open_means(store):
    """Every card shows a date or a line — never neither, never both."""
    _fill(store)
    payload = wall.build_payload(store.conn, days=30)
    for view in payload["views"]:
        for card in view.get("cards", []):
            if card["country"] in ("Norway", "Hungary"):
                assert card["has_date"] or card["forecast"] or card["empty_title"]


# ===== the chip says NOW, and availability leads ===========================


def test_the_chip_says_open_now_when_a_slot_is_there(store):
    """It used to read "Occasional" with a slot on the screen.

    That was its history over the window — accurate, and the opposite of useful
    to someone deciding whether to call a client. The history is still on the
    card, in the score and the footer.
    """
    now = datetime.now(GST)
    store.record_check("AE-HUN", slot_msg(20, now), combo=HUN, ts=now)
    cards = [c for v in wall.build_payload(store.conn, days=30)["views"]
             for c in v.get("cards", []) if c["country"] == "Hungary"]
    assert cards and cards[0]["status"] == "Open now"
    assert cards[0]["kind"] == "open"


def test_the_chip_says_a_slot_was_there_when_nobody_has_checked_since(store):
    now = datetime.now(GST)
    store.record_check("AE-HUN", slot_msg(20, now), combo=HUN,
                       ts=now - timedelta(hours=6))
    cards = [c for v in wall.build_payload(store.conn, days=30)["views"]
             for c in v.get("cards", []) if c["country"] == "Hungary"]
    assert cards and cards[0]["status"].startswith("Was open")
    assert not cards[0]["has_date"]


def test_a_country_with_a_slot_comes_before_one_without(store):
    """Availability leads the board, whatever the history score says."""
    now = datetime.now(GST)
    # Norway earns a high score over the window but has nothing right now.
    for hours in range(2, 60, 2):
        past = now - timedelta(hours=hours)
        store.record_check("AE-NOR", slot_msg(3, past), combo=NOR, ts=past)
    store.record_check("AE-NOR", "No slot message shown (no availability?).",
                       combo=NOR, ts=now)
    # Hungary has almost no history, but a slot on the screen.
    store.record_check("AE-HUN", slot_msg(40, now), combo=HUN, ts=now)

    cards = view(wall.build_payload(store.conn, days=7), "Tourist")["cards"]
    order = [c["country"] for c in cards]
    assert order.index("Hungary") < order.index("Norway"), order
    assert cards[0]["rank"] == 1


def test_the_soonest_appointment_leads_among_the_bookable(store):
    now = datetime.now(GST)
    store.record_check("AE-NOR", slot_msg(30, now), combo=NOR, ts=now)
    store.record_check("AE-HUN", slot_msg(2, now), combo=HUN, ts=now)
    cards = view(wall.build_payload(store.conn, days=7), "Tourist")["cards"]
    bookable = [c["country"] for c in cards if c["has_date"]]
    assert bookable[:2] == ["Hungary", "Norway"], bookable


def test_sorting_happens_before_the_board_is_cut_to_its_top_cards(store):
    """A bookable country must not be dropped for a waitlist one that scored higher.

    Slicing the ranking first and sorting the survivors would do exactly that.
    """
    now = datetime.now(GST)
    for hours in range(1, 40):
        past = now - timedelta(hours=hours)
        for route, combo in (("AE-NOR", NOR), ("AE-FRA", FRA)):
            store.record_check(route, WAITLIST, combo=combo, ts=past)
        store.record_check("AE-ITA", WAITLIST, combo=ITA, ts=past)
    store.record_check("AE-HUN", slot_msg(5, now), combo=HUN, ts=now)

    cards = view(wall.build_payload(store.conn, days=7), "Tourist")["cards"]
    assert cards[0]["country"] == "Hungary"
    assert cards[0]["has_date"]


# ===== the table behind "All countries" ====================================
#
# The board shows six because six reads from across the room. The button opens
# the rest for someone standing at the screen, so the table has to hold EVERY
# country — and rank them the same way, or the two views disagree about who is
# third.


def test_the_table_holds_every_country_not_just_the_cards(store):
    _fill(store)
    for view_ in wall.build_payload(store.conn, days=7)["views"]:
        assert len(view_["table"]) >= len(view_["cards"])
        shown = {c["country"] for c in view_["cards"]}
        assert shown <= {c["country"] for c in view_["table"]}


def test_a_country_keeps_one_rank_in_the_table_and_on_the_board(store):
    """Numbering the six after the cut would restart at 1 and contradict it."""
    _fill(store)
    for view_ in wall.build_payload(store.conn, days=7)["views"]:
        by_country = {c["country"]: c["rank"] for c in view_["table"]}
        for card in view_["cards"]:
            assert card["rank"] == by_country[card["country"]]


def test_the_table_is_in_the_same_order_as_the_board(store):
    _fill(store)
    for view_ in wall.build_payload(store.conn, days=7)["views"]:
        ranks = [c["rank"] for c in view_["table"]]
        assert ranks == sorted(ranks) == list(range(1, len(ranks) + 1))


def test_every_table_row_carries_what_the_card_carries(store):
    """The table renders from the card, so a missing field is a blank column."""
    _fill(store)
    for view_ in wall.build_payload(store.conn, days=7)["views"]:
        for row in view_["table"]:
            assert row["country"] and row["status"] and row["kind"]
            assert row["meter_value"] is not None
            assert isinstance(row["stats_left"], list)
            assert isinstance(row["stats_right"], list)


def test_a_country_off_the_board_still_appears_in_the_table(store, monkeypatch):
    """More countries than cards — the ones that do not fit have to be somewhere.

    The board's own limit is six; this squeezes it so the overflow can be seen
    with the handful of routes the fixture defines.
    """
    monkeypatch.setattr(wall, "CARD_COUNT", 1)
    _fill(store)
    tourist = view(wall.build_payload(store.conn, days=7), "Tourist")
    assert len(tourist["cards"]) == 1
    assert len(tourist["table"]) > 1
    missing = ({c["country"] for c in tourist["table"]}
               - {c["country"] for c in tourist["cards"]})
    assert missing, "a country was dropped from the board and from the table"


# ===== every centre, not just the one that won =============================
#
# `query.rank` collapses a country to its best centre on purpose, and the card's
# `centre` names that winner. Before `centres`, every other centre was gone from
# the payload: a city appeared in exactly two fields on the whole board, so a
# renderer could not have drawn Dubai even if it wanted to. Abu Dhabi won
# `centre` almost every cycle, which read as "Dubai is not checked" while Dubai
# was being checked all day and routinely differed.

SWE_AUH = {"centre": "Abu Dhabi", "category": "Short Stay",
           "sub_category": "Tourist"}
SWE_DXB = {"centre": "Dubai", "category": "Short Stay",
           "sub_category": "Tourist"}


@pytest.fixture
def pair(tmp_path):
    """One country, two centres - the shape the whole feature exists for."""
    routes = tmp_path / "routes"
    routes.mkdir()
    (routes / "AE-SWE.json").write_text(
        json.dumps({"slot_check": {"combinations": [SWE_AUH, SWE_DXB]}}),
        encoding="utf-8")
    s = SlotStore.open(str(tmp_path / "slots.db"))
    s.sync_combos(str(routes))
    yield s
    s.close()


def _sweden(store, view_name="Tourist"):
    cards = view(wall.build_payload(store.conn, days=30), view_name)["cards"]
    return next(c for c in cards if c["country"] == "Sweden")


def _row(card, city):
    return next(r for r in card["centres"] if r["city"] == city)


def test_both_centres_are_sent_when_only_one_has_a_slot(pair):
    """*** THE WHOLE POINT. ***

    Abu Dhabi wins the card's single `centre`; Dubai still has to arrive, with
    its own state, or the board has said Sweden has one centre.
    """
    now = datetime.now(GST)
    pair.record_check("AE-SWE", slot_msg(10, now), combo=SWE_AUH, ts=now)
    pair.record_check("AE-SWE", "No slot message shown (no availability?).",
                      combo=SWE_DXB, ts=now)

    card = _sweden(pair)

    assert [r["city"] for r in card["centres"]] == ["Abu Dhabi", "Dubai"]
    assert _row(card, "Abu Dhabi")["status"] == "Open now"
    assert _row(card, "Dubai")["status"] == "No slots now"


def test_a_centre_with_nothing_to_report_still_gets_a_row(pair):
    """A missing row reads as "not checked" - the same lie in a new place."""
    now = datetime.now(GST)
    pair.record_check("AE-SWE", slot_msg(10, now), combo=SWE_AUH, ts=now)

    card = _sweden(pair)

    assert len(card["centres"]) == 2
    assert _row(card, "Dubai")["status"] == "Not checked"
    assert _row(card, "Dubai")["note"]


def test_each_centre_carries_its_own_date(pair):
    now = datetime.now(GST)
    pair.record_check("AE-SWE", slot_msg(10, now), combo=SWE_AUH, ts=now)
    pair.record_check("AE-SWE", slot_msg(40, now), combo=SWE_DXB, ts=now)

    card = _sweden(pair)

    assert _row(card, "Abu Dhabi")["date_iso"] != _row(card, "Dubai")["date_iso"]
    assert MON_DAY.match(_row(card, "Dubai")["headline"])
    assert all(r["has_date"] for r in card["centres"])


def test_the_card_headline_is_the_soonest_of_its_centres(pair):
    """The rows must not disagree with the number in 88px type above them."""
    now = datetime.now(GST)
    pair.record_check("AE-SWE", slot_msg(40, now), combo=SWE_AUH, ts=now)
    pair.record_check("AE-SWE", slot_msg(10, now), combo=SWE_DXB, ts=now)

    card = _sweden(pair)
    dated = [r["date_iso"] for r in card["centres"] if r["date_iso"]]

    assert card["headline"] == wall._fmt_date(min(dated))
    assert "Dubai" in card["centre"], "the winning centre is Dubai here"


def test_a_centre_wins_the_card_even_when_it_is_not_abu_dhabi(pair):
    """Abu Dhabi won `centre` almost every cycle because it usually held the
    earlier date, not because anything preferred it. Nothing may."""
    now = datetime.now(GST)
    pair.record_check("AE-SWE", "No slot message shown (no availability?).",
                      combo=SWE_AUH, ts=now)
    pair.record_check("AE-SWE", slot_msg(10, now), combo=SWE_DXB, ts=now)

    card = _sweden(pair)

    assert card["centre"].startswith("Dubai")
    assert _row(card, "Dubai")["has_date"]
    assert not _row(card, "Abu Dhabi")["has_date"]


def test_the_bookable_centre_is_listed_first(pair):
    """Availability leads a row list for the same reason it leads the board."""
    now = datetime.now(GST)
    pair.record_check("AE-SWE", "No slot message shown (no availability?).",
                      combo=SWE_AUH, ts=now)
    pair.record_check("AE-SWE", slot_msg(10, now), combo=SWE_DXB, ts=now)

    assert [r["city"] for r in _sweden(pair)["centres"]] == ["Dubai", "Abu Dhabi"]


def test_two_centres_with_nothing_to_separate_them_keep_one_order(pair):
    """A row that moves on a screen nobody touches looks like news."""
    now = datetime.now(GST)
    for hours in range(1, 12):
        past = now - timedelta(hours=hours)
        pair.record_check("AE-SWE", slot_msg(10, past), combo=SWE_AUH, ts=past)
        pair.record_check("AE-SWE", slot_msg(10, past), combo=SWE_DXB, ts=past)

    seen = {tuple(r["city"] for r in _sweden(pair)["centres"]) for _ in range(3)}

    assert seen == {("Abu Dhabi", "Dubai")}


def test_a_stale_centre_reading_is_no_more_bookable_than_a_stale_card(pair):
    """The row applies the card's own test, not a looser one."""
    now = datetime.now(GST)
    pair.record_check("AE-SWE", slot_msg(10, now - timedelta(hours=6)),
                      combo=SWE_AUH, ts=now - timedelta(hours=6))

    row = _row(_sweden(pair), "Abu Dhabi")

    assert not row["has_date"]
    assert row["status"].startswith("Was open")
    assert "Last slot" in row["note"]


def test_a_centre_row_carries_the_cards_four_numbers(pair):
    """It renders beside the card, so a missing figure is a blank column."""
    now = datetime.now(GST)
    pair.record_check("AE-SWE", slot_msg(10, now), combo=SWE_AUH, ts=now)
    pair.record_check("AE-SWE", slot_msg(10, now), combo=SWE_DXB, ts=now)

    for row in _sweden(pair)["centres"]:
        assert [s["label"] for s in row["stats_left"]] == ["Wait", "Soonest"]
        assert [s["label"] for s in row["stats_right"]] == ["Seen", "Drift"]
        assert all(s["value"] for s in row["stats_left"] + row["stats_right"])
        assert row["kind"] and row["status"]


def test_the_business_view_gets_centres_too(pair, tmp_path):
    """Asked for on both: an agent sells business appointments off this screen
    as often as tourist ones."""
    biz_auh = {"centre": "Abu Dhabi", "category": "Short Stay - Business",
               "sub_category": ""}
    biz_dxb = {"centre": "Dubai", "category": "Short Stay - Business",
               "sub_category": ""}
    routes = tmp_path / "biz"
    routes.mkdir()
    (routes / "AE-SWE.json").write_text(
        json.dumps({"slot_check": {"combinations": [biz_auh, biz_dxb]}}),
        encoding="utf-8")
    pair.sync_combos(str(routes))
    now = datetime.now(GST)
    pair.record_check("AE-SWE", slot_msg(10, now), combo=biz_dxb, ts=now)

    card = _sweden(pair, "Business")

    assert {r["city"] for r in card["centres"]} == {"Abu Dhabi", "Dubai"}
    assert _row(card, "Dubai")["has_date"]


def test_every_table_row_carries_its_centres_as_well(pair):
    """The table renders from the same card, so it must not be the one place a
    centre is missing."""
    now = datetime.now(GST)
    pair.record_check("AE-SWE", slot_msg(10, now), combo=SWE_AUH, ts=now)

    for view_ in wall.build_payload(pair.conn, days=30)["views"]:
        for row in view_["table"]:
            assert isinstance(row.get("centres"), list)


def test_the_waitlist_view_is_left_alone(store):
    """It has no appointment date to break down by centre, and nothing was asked
    of it. Its cards come from a different query."""
    _fill(store)
    for card in view(wall.build_payload(store.conn, days=7), "Waitlist")["cards"]:
        assert "centres" not in card
