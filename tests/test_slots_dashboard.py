"""Phases 4 and 5: the sales numbers, and the page built from them.

The numbers here are the ones an agent quotes to a client, so the tests are
written as the claims themselves: "a country nobody checked is not reported as
having no slots", "the country's figure is its best centre", "an error doesn't
count against availability".
"""

import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.slots import dashboard, query, registry  # noqa: E402
from src.slots.store import SlotStore  # noqa: E402

GST = timezone(timedelta(hours=4))

NOR_AD = {"centre": "Norway Visa Application Center - Abu Dhabi",
          "category": "Short Stay", "sub_category": "Tourist"}
NOR_DXB = {"centre": "Norway Visa Application Center - Dubai",
           "category": "Short Stay", "sub_category": "Tourist"}
HUN_DXB = {"centre": "Dubai", "category": "Short Stay", "sub_category": "Tourist"}
ITA_DXB = {"centre": "Dubai", "category": "Schengen Visa", "sub_category": ""}
# Sweden's real shape: its ONLY live combination is an any-purpose 'ShortStay'.
SWE_DXB = {"centre": "Sweden Visa Application Centre, Dubai",
           "category": "Short Stay", "sub_category": "ShortStay"}
# France's real shape: a business-only combination alongside an any-purpose one.
FRA_AD_BIZ = {"centre": "Abu Dhabi", "category": "Short Stay - Business",
              "sub_category": ""}

ROUTES = {"AE-NOR": [NOR_AD, NOR_DXB], "AE-HUN": [HUN_DXB], "AE-ITA": [ITA_DXB],
          "AE-SWE": [SWE_DXB], "AE-FRA": [FRA_AD_BIZ]}


def slot_msg(days_ahead: int, checked_on: datetime, second: int = None) -> str:
    first = (checked_on.date() + timedelta(days=days_ahead)).strftime("%d-%m-%Y")
    text = f"Earliest available slot for 1 Applicants is : {first}"
    if second is not None:
        later = (checked_on.date() + timedelta(days=second)).strftime("%d-%m-%Y")
        text += f"\nEarliest available slot for 2 Applicants is : {later}"
    return text


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
    """Three days of history, built to be unambiguous:

        Norway Abu Dhabi — a slot every check, 3 days out   (the easy one)
        Norway Dubai     — a slot every check, 30 days out  (same country, slower)
        Hungary          — nothing, ever                    (checked, empty)
        Italy            — never checked at all             (no data)
    """
    now = datetime.now(GST).replace(minute=0, second=0, microsecond=0)
    for hours_ago in range(1, 72, 6):
        ts = now - timedelta(hours=hours_ago)
        store.record_check("AE-NOR", slot_msg(3, ts, second=5), combo=NOR_AD, ts=ts)
        store.record_check("AE-NOR", slot_msg(30, ts), combo=NOR_DXB, ts=ts)
        store.record_check("AE-HUN", "No slot message shown (no availability?).",
                           combo=HUN_DXB, ts=ts)
    return now


# ===== query ===============================================================


def test_ranks_the_easiest_country_first(store):
    _fill(store)
    ranked = query.rank(store.conn, days=7)
    assert ranked[0]["country"] == "Norway"
    assert ranked[0]["verdict"] == "Always open"
    assert ranked[0]["score"] > ranked[1]["score"]


def test_a_country_is_judged_by_its_best_centre(store):
    """Abu Dhabi at 3 days is the offer; averaging in Dubai's 30 would mislead."""
    _fill(store)
    norway = next(c for c in query.rank(store.conn, days=7) if c["country"] == "Norway")
    assert norway["median_lead"] == 3
    assert norway["current_city"] == "Abu Dhabi"
    assert {c["city"]: c["median_lead"] for c in norway["combos"]} == \
           {"Abu Dhabi": 3, "Dubai": 30}


def test_the_earliest_date_of_any_party_size_is_the_one_reported(store):
    """Norway Abu Dhabi quotes 3 days for one applicant and 5 for two.

    Party size is not a filter on this board, so the figure is the soonest
    appointment the check offered — a client asks when they can go, not when one
    of them could. Both sizes stay in `slot_dates` for later.
    """
    _fill(store)
    norway = next(c for c in query.rank(store.conn, days=7) if c["country"] == "Norway")
    assert norway["median_lead"] == 3

    both = store.conn.execute(
        "SELECT applicants, slot_date FROM slot_dates sd"
        " JOIN checks c ON c.id = sd.check_id"
        " WHERE c.combo_id = (SELECT id FROM combos WHERE city = 'Abu Dhabi'"
        "                     AND route = 'AE-NOR')"
        " ORDER BY c.ts_utc DESC, applicants LIMIT 2").fetchall()
    assert [r["applicants"] for r in both] == [1, 2]
    assert both[0]["slot_date"] < both[1]["slot_date"]


def test_a_check_offering_only_a_larger_party_still_counts(store):
    """A banner quoting only 2 applicants is still availability."""
    ts = datetime.now(GST) - timedelta(hours=1)
    later = (ts.date() + timedelta(days=6)).strftime("%d-%m-%Y")
    store.record_check("AE-HUN",
                       f"Earliest available slot for 2 Applicants is : {later}",
                       combo=HUN_DXB, ts=ts)
    hungary = next(c for c in query.rank(store.conn, days=7) if c["country"] == "Hungary")
    assert hungary["availability"] == 1.0
    assert hungary["median_lead"] == 6


def test_a_checked_but_empty_country_reads_as_no_slots(store):
    _fill(store)
    hungary = next(c for c in query.rank(store.conn, days=7) if c["country"] == "Hungary")
    assert hungary["verdict"] == "No slots"
    assert hungary["availability"] == 0


def test_an_unchecked_country_is_never_called_empty(store):
    """The distinction the whole design protects: nobody looked != nothing there."""
    _fill(store)
    italy = next(c for c in query.rank(store.conn, days=7) if c["country"] == "Italy")
    assert italy["verdict"] == "Not checked"
    assert italy["observed"] == 0


def test_errors_do_not_count_against_availability(store):
    ts = datetime.now(GST) - timedelta(hours=2)
    store.record_check("AE-HUN", slot_msg(4, ts), combo=HUN_DXB, ts=ts)
    store.record_check("AE-HUN", "ERROR: could not select centre 'Dubai'",
                       combo=HUN_DXB, ts=ts + timedelta(minutes=30))
    hungary = next(c for c in query.rank(store.conn, days=7) if c["country"] == "Hungary")
    assert hungary["availability"] == 1.0      # one check, one slot — not 0.5


def test_days_without_checks_are_marked_unobserved(store):
    ts = datetime.now(GST) - timedelta(hours=2)
    store.record_check("AE-HUN", slot_msg(4, ts), combo=HUN_DXB, ts=ts)
    hungary = next(c for c in query.rank(store.conn, days=7) if c["country"] == "Hungary")
    observed = [d for d in hungary["sparkline"] if d["observed"]]
    assert len(hungary["sparkline"]) == 7
    assert len(observed) == 1
    assert observed[0]["rate"] == 1.0


def test_waitlist_only_country_gets_its_own_verdict(store):
    ts = datetime.now(GST) - timedelta(hours=1)
    store.record_check("AE-ITA", "WAITLIST — no slots; waitlist sign-up available",
                       combo=ITA_DXB, ts=ts)
    italy = next(c for c in query.rank(store.conn, days=7) if c["country"] == "Italy")
    assert italy["verdict"] == "Waitlist only"
    assert italy["waitlist_rate"] == 1.0


def test_the_next_appointment_is_the_soonest_centre(store):
    """The headline date must be the best one an agent could actually book."""
    _fill(store)
    norway = next(c for c in query.rank(store.conn, days=7) if c["country"] == "Norway")
    soonest = min(k["current_date"] for k in norway["combos"] if k["current_date"])
    assert norway["current_date"] == soonest
    assert norway["current_city"] == "Abu Dhabi"


def test_score_parts_add_up_to_the_score(store):
    _fill(store)
    for country in query.rank(store.conn, days=7):
        parts = (country["availability_part"] + country["speed_part"]
                 + country["freshness_part"])
        assert abs(parts - country["score"]) <= 2      # rounding only


def test_heatmap_cells_carry_their_observation_count(store):
    _fill(store)
    grid = query.heatmap(store.conn, days=7)
    assert grid["grid"]
    for cell in grid["grid"].values():
        assert cell["observed"] > 0
        assert 0.0 <= cell["rate"] <= 1.0


def test_a_date_jumping_closer_is_reported_as_activity(store):
    """The most valuable event of all: VFS releasing an earlier appointment.

    It is not an 'opening' (the combination never stopped having slots), so a
    feed of openings alone would hide it — which is exactly what it used to do.
    """
    now = datetime.now(GST)
    store.record_check("AE-HUN", slot_msg(40, now), combo=HUN_DXB,
                       ts=now - timedelta(hours=2))
    store.record_check("AE-HUN", slot_msg(8, now), combo=HUN_DXB,
                       ts=now - timedelta(hours=1))

    assert query.recent_openings(store.conn, days=7) == []
    activity = query.recent_activity(store.conn, days=7)
    assert [a["kind"] for a in activity] == ["moved_earlier"]
    assert activity[0]["days_earlier"] == 32
    assert activity[0]["country_name"] == "Hungary"


def test_a_date_drifting_later_is_not_activity(store):
    """Normal decay as slots get taken — it would bury everything else."""
    now = datetime.now(GST)
    store.record_check("AE-HUN", slot_msg(5, now), combo=HUN_DXB,
                       ts=now - timedelta(hours=2))
    store.record_check("AE-HUN", slot_msg(9, now), combo=HUN_DXB,
                       ts=now - timedelta(hours=1))
    assert query.recent_activity(store.conn, days=7) == []


def test_activity_covers_closures_and_waitlists(store):
    now = datetime.now(GST)
    store.record_check("AE-HUN", slot_msg(5, now), combo=HUN_DXB,
                       ts=now - timedelta(hours=3))
    store.record_check("AE-HUN", "No slot message shown (no availability?).",
                       combo=HUN_DXB, ts=now - timedelta(hours=2))
    store.record_check("AE-HUN", "WAITLIST — no slots; waitlist sign-up available",
                       combo=HUN_DXB, ts=now - timedelta(hours=1))
    kinds = [a["kind"] for a in query.recent_activity(store.conn, days=7)]
    assert kinds == ["waitlist_opened", "closed"]     # newest first


def test_openings_are_listed_newest_first(store):
    now = datetime.now(GST)
    store.record_check("AE-HUN", "No slot message shown (no availability?).",
                       combo=HUN_DXB, ts=now - timedelta(hours=3))
    store.record_check("AE-HUN", slot_msg(5, now), combo=HUN_DXB,
                       ts=now - timedelta(hours=2))
    store.record_check("AE-HUN", "No slot message shown (no availability?).",
                       combo=HUN_DXB, ts=now - timedelta(hours=1))
    openings = query.recent_openings(store.conn, days=7)
    assert len(openings) == 1
    assert openings[0]["country_name"] == "Hungary"


# ===== the page ============================================================


def test_page_is_self_contained_and_has_the_data(store, tmp_path):
    _fill(store)
    out = tmp_path / "board.html"
    dashboard.build(db_path=store.db_path, output=str(out), days=7)
    html = out.read_text(encoding="utf-8")

    assert "__PAYLOAD__" not in html          # the placeholder was replaced
    assert "Norway" in html
    assert "<script src" not in html          # no external JS
    assert "http://" not in html and "https://" not in html   # no network calls
    assert "</" not in html.split("const DATA = ")[1].split(";\n")[0]  # JSON escaped


def test_page_carries_one_ranking_per_visa_type(store, tmp_path):
    _fill(store)
    payload = dashboard.build_payload(store.conn, days=7)
    everything = payload["views"]["tourist"]
    assert everything["countries"][0]["country"] == "Norway"
    assert payload["coverage"]["checks"] > 0
    assert everything["heatmap"]["all"]


def test_page_survives_an_empty_database(store, tmp_path):
    """A fresh install must still produce a page, not a stack trace."""
    out = tmp_path / "board.html"
    dashboard.build(db_path=store.db_path, output=str(out), days=7)
    assert out.exists()
    assert "Schengen Slot Board" in out.read_text(encoding="utf-8")


def test_rebuild_replaces_the_page_atomically(store, tmp_path):
    _fill(store)
    out = tmp_path / "board.html"
    dashboard.build(db_path=store.db_path, output=str(out), days=7)
    first = out.stat().st_size
    dashboard.build(db_path=store.db_path, output=str(out), days=7)
    assert out.stat().st_size == pytest.approx(first, rel=0.05)
    assert not (tmp_path / "board.html.tmp").exists()


def test_auto_build_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(dashboard, "auto_build_enabled", lambda: True)
    monkeypatch.setattr(dashboard, "build",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    assert dashboard.build_quietly() is None       # logged, not raised


# ===== visa type (tourist / business) ======================================


@pytest.mark.parametrize("category,sub,expected", [
    ("Short Stay", "Tourist", registry.TOURIST),
    ("Tourism", "Tourism", registry.TOURIST),
    ("Tourist Visa", "Tourist Purpose", registry.TOURIST),
    ("Short Stay", "Tourist visa", registry.TOURIST),
    ("Short Stay", "Business", registry.BUSINESS),
    ("Business Visa", "Business Purpose", registry.BUSINESS),
    ("Short Stay - Business", "", registry.BUSINESS),
    # The any-purpose family — one appointment type serving both kinds of client.
    ("SCHENGEN", "", registry.ANY_PURPOSE),
    ("Short Stay", "ShortStay", registry.ANY_PURPOSE),
    ("Schengen Visa", "Schengen Short Stay", registry.ANY_PURPOSE),
    ("Short Term Visa", "Short Stay", registry.ANY_PURPOSE),
    ("Short Stay (any purpose)", "", registry.ANY_PURPOSE),
    ("Short Stay", "General Appointment", registry.ANY_PURPOSE),
    ("Prime Time", "Primetime", registry.ANY_PURPOSE),
])
def test_visa_purpose_is_derived_from_the_portal_wording(category, sub, expected):
    assert registry.purpose(category, sub) == expected


def test_an_any_purpose_country_appears_under_both_tabs(store):
    """Sweden's only live combination is 'ShortStay' — an any-purpose type.

    If the Tourist tab dropped it, the second-best country on the board would
    vanish from the tab an agent uses most. This is the test that protects that.
    """
    now = datetime.now(GST)
    for hours_ago in (1, 7, 13):
        ts = now - timedelta(hours=hours_ago)
        store.record_check("AE-SWE", slot_msg(2, ts), combo=SWE_DXB, ts=ts)

    for purpose in (None, "tourist", "business"):
        ranked = query.rank(store.conn, days=7, purpose=purpose)
        sweden = next(c for c in ranked if c["country"] == "Sweden")
        assert sweden["availability"] == 1.0, f"Sweden missing from {purpose} tab"
        assert sweden["median_lead"] == 2


def test_a_business_only_combination_is_hidden_from_the_tourist_tab(store):
    """France's business slots must not be sold to a tourist client.

    France here has ONLY a business combination, so it drops off the Tourist tab
    entirely rather than appearing with a tempting availability figure that
    belongs to an appointment type this client cannot use.
    """
    now = datetime.now(GST)
    for hours_ago in (1, 7, 13):
        ts = now - timedelta(hours=hours_ago)
        store.record_check("AE-FRA", slot_msg(20, ts), combo=FRA_AD_BIZ, ts=ts)

    business = next(c for c in query.rank(store.conn, days=7, purpose="business")
                    if c["country"] == "France")
    assert business["availability"] == 1.0
    assert business["current_outcome"] == "slot"

    tourist = [c["country"] for c in query.rank(store.conn, days=7, purpose="tourist")]
    assert "France" not in tourist


def test_tourist_only_combination_is_hidden_from_the_business_tab(store):
    _fill(store)
    tourist = next(c for c in query.rank(store.conn, days=7, purpose="tourist")
                   if c["country"] == "Norway")
    assert tourist["availability"] == 1.0
    assert "Norway" not in [c["country"] for c in
                            query.rank(store.conn, days=7, purpose="business")]


def test_a_country_whose_business_combo_is_merely_disabled_still_shows(store):
    """'We don't monitor this' must not look like 'this doesn't exist'.

    A combination switched off in config keeps its row, so the country stays on
    the tab marked 'Not checked' — the honest answer. Only a country with no
    such appointment type AT ALL disappears.
    """
    routes = pathlib.Path(store.db_path).parent / "routes"
    routes.joinpath("AE-NOR.json").write_text(json.dumps({"slot_check": {"combinations": [
        NOR_AD, NOR_DXB,
        dict(NOR_AD, sub_category="Business", disabled=True),
    ]}}), encoding="utf-8")
    store.sync_combos(str(routes))
    _fill(store)

    norway = next(c for c in query.rank(store.conn, days=7, purpose="business")
                  if c["country"] == "Norway")
    assert norway["verdict"] == "Not checked"
    assert norway["observed"] == 0


def test_the_hour_grid_follows_the_selected_visa_type(store):
    now = datetime.now(GST)
    ts = now - timedelta(hours=2)
    store.record_check("AE-FRA", slot_msg(20, ts), combo=FRA_AD_BIZ, ts=ts)
    assert query.heatmap(store.conn, days=7, purpose="business")["grid"]
    assert query.heatmap(store.conn, days=7, purpose="tourist")["grid"] == {}


def test_the_page_carries_a_view_per_visa_type(store, tmp_path):
    _fill(store)
    payload = dashboard.build_payload(store.conn, days=7)
    assert set(payload["views"]) == {"tourist", "business", "waitlist"}
    for name in ("tourist", "business"):
        view = payload["views"][name]
        assert "countries" in view
        assert "heatmap" in view and "activity" in view and "routes" in view


# ===== the waitlist board ==================================================


def test_waitlist_board_lists_only_countries_that_offered_one(store):
    now = datetime.now(GST)
    store.record_check("AE-ITA", "WAITLIST \u2014 no slots; waitlist sign-up available",
                       combo=ITA_DXB, ts=now - timedelta(hours=1))
    store.record_check("AE-HUN", "No slot message shown (no availability?).",
                       combo=HUN_DXB, ts=now - timedelta(hours=1))

    board = query.waitlist_board(store.conn, days=7)
    assert [c["country"] for c in board] == ["Italy"]
    assert board[0]["open_now"] is True
    assert board[0]["offered_rate"] == 1.0
    assert board[0]["verdict"] == "Open now"


def test_a_closed_waitlist_still_shows_but_not_as_open(store):
    """It was there yesterday and isn't now — an agent needs to see the difference."""
    now = datetime.now(GST)
    store.record_check("AE-ITA", "WAITLIST \u2014 no slots; waitlist sign-up available",
                       combo=ITA_DXB, ts=now - timedelta(hours=5))
    store.record_check("AE-ITA", "No slot message shown (no availability?).",
                       combo=ITA_DXB, ts=now - timedelta(hours=1))

    italy = query.waitlist_board(store.conn, days=7)[0]
    assert italy["open_now"] is False
    assert italy["verdict"] == "Was open"
    assert italy["offered_rate"] == 0.5          # one of two checks
    assert italy["last_seen"]                     # when we last saw it open


def test_open_waitlists_sort_above_closed_ones(store):
    now = datetime.now(GST)
    waitlist = "WAITLIST \u2014 no slots; waitlist sign-up available"
    # Italy: open now. Sweden: was open, then gone.
    store.record_check("AE-ITA", waitlist, combo=ITA_DXB, ts=now - timedelta(hours=1))
    store.record_check("AE-SWE", waitlist, combo=SWE_DXB, ts=now - timedelta(hours=5))
    store.record_check("AE-SWE", "No slot message shown (no availability?).",
                       combo=SWE_DXB, ts=now - timedelta(hours=1))

    assert [c["country"] for c in query.waitlist_board(store.conn, days=7)] == \
           ["Italy", "Sweden"]


def test_the_board_says_whether_real_slots_turn_up_too(store):
    """A waitlist where slots also appear is a much better sell."""
    now = datetime.now(GST)
    waitlist = "WAITLIST \u2014 no slots; waitlist sign-up available"
    store.record_check("AE-HUN", waitlist, combo=HUN_DXB, ts=now - timedelta(hours=3))
    store.record_check("AE-HUN", slot_msg(5, now), combo=HUN_DXB,
                       ts=now - timedelta(hours=2))
    store.record_check("AE-ITA", waitlist, combo=ITA_DXB, ts=now - timedelta(hours=1))

    board = {c["country"]: c for c in query.waitlist_board(store.conn, days=7)}
    assert board["Hungary"]["slot_availability"] == 0.5    # one of its two checks
    assert board["Italy"]["slot_availability"] == 0.0


def test_the_waitlist_view_ignores_the_visa_type_split(store):
    """A waitlist covers the country, not one appointment type."""
    now = datetime.now(GST)
    waitlist = "WAITLIST \u2014 no slots; waitlist sign-up available"
    store.record_check("AE-HUN", waitlist, combo=HUN_DXB, ts=now - timedelta(hours=2))
    store.record_check("AE-FRA", waitlist, combo=FRA_AD_BIZ, ts=now - timedelta(hours=2))

    countries = [c["country"] for c in query.waitlist_board(store.conn, days=7)]
    assert set(countries) == {"Hungary", "France"}        # tourist AND business


def test_the_page_carries_the_waitlist_view(store, tmp_path):
    now = datetime.now(GST)
    store.record_check("AE-ITA", "WAITLIST \u2014 no slots; waitlist sign-up available",
                       combo=ITA_DXB, ts=now - timedelta(hours=1))
    payload = dashboard.build_payload(store.conn, days=7)

    assert set(payload["views"]) == {"tourist", "business", "waitlist"}
    waitlist = payload["views"]["waitlist"]
    assert waitlist["countries"][0]["country"] == "Italy"
    assert waitlist["countries"][0]["last_seen_date"]     # a real date, for the page
    # Its feed carries waitlist changes only — a slot opening elsewhere is noise here.
    assert all(a["kind"] == "waitlist_opened" for a in waitlist["activity"])


# ===== the forecast on the board ===========================================
#
# The forecast itself is tested in test_slots_forecast.py. These are about how
# it reaches the page: a country shows ONE answer rolled up from its
# combinations, and every cell carries either dates or a reason, never both and
# never neither — the page has a single branch to render and no blank cells.


def test_a_forecast_cell_carries_dates_or_a_reason_but_never_both():
    dated = dashboard._forecast_cell({
        "from": "2026-09-24", "to": "2026-09-28", "confidence": "likely",
        "gaps": 9, "pooled_gaps": 0, "last_opened": "2026-09-20"})
    assert dated["from"] and "reason" not in dated
    assert dated["same_day"] is False

    refused = dashboard._forecast_cell({"reason": "overdue", "elapsed_days": 47.2,
                                        "longest_gap_days": 13.8})
    assert refused["reason"] == "overdue" and "from" not in refused
    assert refused["elapsed_days"] == 47.2


def test_a_missing_forecast_row_becomes_a_reason_not_a_blank():
    assert dashboard._forecast_cell(None) == {"reason": "no_data"}


def test_a_single_day_range_is_flagged_so_the_page_does_not_print_it_twice():
    cell = dashboard._forecast_cell({
        "from": "2026-10-16", "to": "2026-10-16", "confidence": "pooled",
        "gaps": 0, "pooled_gaps": 8})
    assert cell["same_day"] is True


def test_a_country_open_right_now_shows_that_and_not_a_future_date():
    """Open now outranks every date. An agent must not be sent away from a slot."""
    rolled = dashboard._country_forecast([
        {"from": "2026-09-24", "to": "2026-09-28", "confidence": "likely"},
        {"reason": "open_now", "since": "2026-09-21T18:22+04:00"},
        {"reason": "waitlist_only"},
    ])
    assert rolled["reason"] == "open_now"


def test_a_country_shows_its_soonest_forecast_when_nothing_is_open():
    rolled = dashboard._country_forecast([
        {"from": "2026-10-11", "to": "2026-10-20", "confidence": "pooled"},
        {"from": "2026-09-24", "to": "2026-09-28", "confidence": "likely"},
        {"reason": "dormant"},
    ])
    assert rolled["from"] == "2026-09-24"


def test_a_country_with_no_forecastable_combination_shows_the_most_telling_reason():
    """'Overdue' says more than 'never opened' — it means this one usually does."""
    rolled = dashboard._country_forecast([
        {"reason": "never_opened"},
        {"reason": "overdue", "elapsed_days": 47.2, "longest_gap_days": 13.8},
        {"reason": "waitlist_only"},
    ])
    assert rolled["reason"] == "overdue"


def test_every_country_and_combination_on_the_page_has_a_forecast(store):
    _fill(store)
    payload = dashboard.build_payload(store.conn, days=30)
    for name, view in payload["views"].items():
        if name == "waitlist":
            continue                      # a different board, different columns
        for country in view["countries"]:
            assert "forecast" in country, country["country"]
            cell = country["forecast"]
            assert bool(cell.get("from")) != bool(cell.get("reason"))
            for combo in country["combos"]:
                assert "forecast" in combo
                own = combo["forecast"]
                assert bool(own.get("from")) != bool(own.get("reason"))
