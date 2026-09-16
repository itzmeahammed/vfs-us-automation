"""`GET /routes/{route}/readiness` must describe the form a route needs.

The gap this closes: `POST /clients` accepts arbitrary extra keys
(`extra="allow"`), because each route's VFS page asks for different things. That
is correct for the API but leaves a web app with nowhere to learn WHICH keys —
Swagger just shows `additionalProp1`. The only alternative was hardcoding a
field list, which is wrong per route and fails silently:

  · AE-CHE wants nine fields, including two address lines
  · AE-NLD wants seven — no address
  · AE-ITA wants a passport SCAN (a file), and nothing else

A form hardcoded from AE-CHE would create AE-ITA clients with no scan. They
would pass creation, pass arming, and only fail minutes into a browser run.

So the fields are derived from each route's own step definitions — the same
config the bot fills from — and cannot drift from what the portal really wants.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

from src.waitlist.validate import required_fields  # noqa: E402


def _by_name(route: str):
    return {f["name"]: f for f in required_fields(route)}


# --------------------------------------------------------------------------
# It reports what each route actually asks for
# --------------------------------------------------------------------------


def test_che_asks_for_the_personal_details():
    fields = _by_name("AE-CHE")
    for expected in ("first_name", "last_name", "nationality",
                     "passport_number", "phone_number", "email"):
        assert expected in fields, expected


def test_routes_genuinely_differ():
    """The whole reason this endpoint exists — one form does not fit all."""
    che = set(_by_name("AE-CHE"))
    ita = set(_by_name("AE-ITA"))
    assert che != ita
    assert "passport_scan" in ita, "AE-ITA needs a file upload"
    assert "passport_scan" not in che


def test_a_file_field_is_reported_as_a_file():
    """A web app must render an upload control, not a text box."""
    assert _by_name("AE-ITA")["passport_scan"]["kind"] == "file"


def test_a_dropdown_is_reported_as_a_select():
    assert _by_name("AE-CHE")["nationality"]["kind"] == "select"


# --------------------------------------------------------------------------
# required vs optional
# --------------------------------------------------------------------------


def test_if_present_fields_are_optional():
    """The portal only renders the address lines sometimes.

    Marking them required would block signup on a field the form may never
    show — so they are reported optional even though the spec says
    "required": true for the render where they DO appear.
    """
    fields = _by_name("AE-CHE")
    assert fields["address_line_1"]["required"] is False
    assert "some renders" in fields["address_line_1"]["notes"]


def test_ordinary_fields_stay_required():
    assert _by_name("AE-CHE")["passport_number"]["required"] is True


# --------------------------------------------------------------------------
# Renderability
# --------------------------------------------------------------------------


def test_labels_are_unique_within_a_route():
    """Two boxes captioned "Contact number" would be unusable.

    The portal really does put the country code and the number under one
    caption, split by `index`. A form rendered from this must still tell them
    apart, so an indexed field falls back to its (descriptive) name.
    """
    for route in ("AE-CHE", "AE-NLD", "AE-DEU"):
        labels = [f["label"] for f in required_fields(route)]
        assert len(labels) == len(set(labels)), f"{route}: duplicate labels {labels}"


def test_every_field_has_what_a_form_needs():
    for route in ("AE-CHE", "AE-NLD", "AE-ITA"):
        for f in required_fields(route):
            assert f["name"] and f["label"] and f["kind"]
            assert isinstance(f["required"], bool)


def test_no_duplicate_field_names():
    names = [f["name"] for f in required_fields("AE-CHE")]
    assert len(names) == len(set(names))


def test_value_constraints_are_surfaced():
    """|digits and |upper change what the portal receives — say so."""
    fields = _by_name("AE-CHE")
    assert "igits" in fields["phone_number"]["notes"]
    assert "UPPER" in fields["first_name"]["notes"]


# --------------------------------------------------------------------------
# It never returns secrets or targeting metadata
# --------------------------------------------------------------------------


def test_account_credentials_are_not_form_fields():
    """They select which VFS login to use — not something an applicant types."""
    for route in ("AE-CHE", "AE-NLD", "AE-ITA"):
        names = set(_by_name(route))
        assert not names & {"account", "account_password", "proxy",
                            "route", "combos", "enabled"}


# --------------------------------------------------------------------------
# Failure behaviour
# --------------------------------------------------------------------------


def test_an_unknown_route_returns_no_fields_rather_than_raising():
    """readiness().problems already explains why; this must not blow up."""
    assert required_fields("ZZ-ZZZ") == []


def test_a_not_ready_route_still_describes_its_form():
    """AE-ITA is disabled, but the web app can still build and validate a form."""
    assert required_fields("AE-ITA"), "a disabled route reported no fields"


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


@pytest.fixture
def api():
    import os

    # Saved and restored rather than left assigned. os.environ is process-wide,
    # so leaking this token made later modules — which capture their HEADERS at
    # import time from a cooperative setdefault — send a stale token and 401 in
    # a full run, while passing when run alone.
    saved = os.environ.get("VFSAPI_SECRET_TOKEN")
    os.environ["VFSAPI_SECRET_TOKEN"] = "t" * 64
    import src.api.config as config_mod
    config_mod.get_settings.cache_clear()
    from fastapi.testclient import TestClient
    import src.api.main as main_mod
    try:
        yield TestClient(main_mod.app, raise_server_exceptions=False)
    finally:
        if saved is None:
            os.environ.pop("VFSAPI_SECRET_TOKEN", None)
        else:
            os.environ["VFSAPI_SECRET_TOKEN"] = saved
        # The env alone is not enough — the cache holds settings built from the
        # token above.
        config_mod.get_settings.cache_clear()


def test_readiness_returns_the_fields(api):
    r = api.get("/routes/AE-CHE/readiness",
                headers={"X-Webhook-Secret-Token": "t" * 64})
    assert r.status_code == 200
    body = r.json()
    assert body["fields"], "readiness returned no fields"
    assert {f["name"] for f in body["fields"]} >= {"first_name", "email"}


def test_readiness_still_needs_a_token(api):
    assert api.get("/routes/AE-CHE/readiness").status_code == 401


# --------------------------------------------------------------------------
# Filters with arguments must not hide a field
# --------------------------------------------------------------------------
#
# Regression. required_fields() used to match placeholders with a regex of its
# own that allowed a bare `|filter` only. A filter WITH an argument —
# `{{passport_expiry|date:%d/%m/%Y}}`, which AE-CZE is the first route to use —
# matched nothing, so the field silently vanished from the form spec.
#
# That broke the contract in the worst possible direction: the readiness
# endpoint advertised 8 fields, a web app rendered exactly those 8, and
# POST /clients then rejected the result with "1 problem(s) with this client"
# for a field it had never been told to ask for. The two sides disagreed
# because they parsed placeholders differently, so the fix was to delegate to
# context.py — the module that resolves them for real — and these tests pin
# that the two stay agreed.


def test_a_filter_with_an_argument_still_reports_its_field():
    fields = _by_name("AE-CZE")
    assert "passport_expiry" in fields, (
        "passport_expiry is templated as {{passport_expiry|date:...}}; a filter "
        "argument must not hide the field from the form spec"
    )
    assert fields["passport_expiry"]["kind"] == "date"
    assert fields["passport_expiry"]["required"] is True


def test_placeholder_parsing_matches_the_real_resolver():
    """Every name context.py would demand is a name required_fields() reports.

    These are the two sides that disagreed: check_templates() rejects a client
    using context.py's parser, while a web form is built from required_fields().
    If the first ever demands a name the second omits, a correctly-built client
    is unfillable — which is exactly the bug this guards.
    """
    from src.waitlist import config as waitlist_config
    from src.waitlist import context as ctx
    from src.waitlist.register import _all_templates

    for route in ("AE-CZE", "AE-CHE", "AE-NLD"):
        advertised = set(_by_name(route))
        demanded = set()
        for _, template in _all_templates(waitlist_config.get(route)):
            if isinstance(template, str):
                demanded.update(ctx.placeholders_in(template))

        # Context-supplied names (route, centre, combo...) come from the run,
        # not the client, so they are legitimately not form fields.
        supplied = {"route", "source", "dest", "combo", "centre", "category",
                    "sub_category", "index", "index1", "today"}
        missing = demanded - advertised - supplied
        assert not missing, (
            f"{route}: these placeholders are demanded at fill time but are "
            f"not advertised by required_fields(): {sorted(missing)}"
        )


# --------------------------------------------------------------------------
# Dropdown options: render the real list, reject anything else
# --------------------------------------------------------------------------
#
# `kind: "select"` alone was a lie of omission — it told a web app to render a
# dropdown without saying what goes in it, so the app either rendered an empty
# control or hardcoded a guess. The guess was then only found wrong minutes
# into a browser run, when get_by_role("option", name=...) matched nothing on
# the real portal. Existing client files still disagree about what a country is
# called ("Lebanese" in one, "Belize" in another), which is the symptom.
#
# So a field now also carries the options observed on the portal, and how far
# they can be trusted:
#
#   known        -> render a dropdown; POST /clients rejects any other value
#   unknown      -> a dropdown nobody has harvested; render free text + warn
#   not_a_choice -> ordinary text/date/file field
#
# The three states matter more than the list itself: "we have not looked yet"
# must be distinguishable from "these are the four values", because a guessed
# list is worse than an admitted gap — it makes a wrong value look validated.

from src.waitlist.validate import (  # noqa: E402
    OPTIONS_KNOWN,
    OPTIONS_NOT_A_CHOICE,
    OPTIONS_UNKNOWN,
    check_choices,
)


def test_a_harvested_dropdown_reports_its_options():
    gender = _by_name("AE-CZE")["gender"]
    assert gender["kind"] == "select"
    assert gender["options_status"] == OPTIONS_KNOWN
    assert gender["options"] == [
        "Female", "Male", "Not Specified", "Others / Transgender",
    ]
    assert gender["options_captured_at"], (
        "an observed list must record WHEN it was observed, or it cannot be "
        "told apart from a guess"
    )


def test_an_unharvested_dropdown_admits_it_rather_than_guessing():
    nationality = _by_name("AE-CZE")["nationality"]
    assert nationality["kind"] == "select"
    assert nationality["options_status"] == OPTIONS_UNKNOWN
    assert nationality["options"] == [], (
        "an unharvested dropdown must report NO options — a partial or "
        "invented list would be validated against and silently reject "
        "legitimate countries"
    )


def test_plain_fields_are_not_treated_as_choices():
    fields = _by_name("AE-CZE")
    for name in ("first_name", "email", "passport_expiry"):
        assert fields[name]["options_status"] == OPTIONS_NOT_A_CHOICE
        assert fields[name]["options"] == []


def _client(**overrides):
    data = {
        "route": "AE-CZE", "combos": ["Dubai - Tourism"],
        "first_name": "TRAV", "last_name": "NOOK", "gender": "Male",
        "nationality": "India", "passport_number": "A1",
        "passport_expiry": "2030-07-05", "phone_country_code": "971",
        "phone_number": "556024553", "email": "a@b.com",
    }
    data.update(overrides)
    return data


def test_a_valid_option_is_accepted():
    assert check_choices("AE-CZE", _client(gender="Male")) == []
    assert check_choices("AE-CZE",
                         _client(gender="Others / Transgender")) == []


def test_a_value_outside_the_list_is_rejected_with_the_list():
    problems = check_choices("AE-CZE", _client(gender="M"))
    assert len(problems) == 1
    assert problems[0].field == "gender"
    # The whole point of the hint: the caller can fix it without reading config.
    assert "Female; Male" in problems[0].hint


def test_a_case_mismatch_is_rejected_and_names_the_exact_spelling():
    """The portal matches the option text exactly, so 'male' would not click.

    Worth its own case because it is the likeliest near-miss from a web form,
    and a bare "not an option" would read as though Male were unavailable.
    """
    problems = check_choices("AE-CZE", _client(gender="male"))
    assert len(problems) == 1
    assert "'Male'" in problems[0].message


def test_an_unharvested_dropdown_does_not_reject_anything():
    """Rejection is earned per field by observing the portal, never assumed.

    nationality has no harvested list, so even a value that is certainly wrong
    passes here — blocking on a list we do not have would make every client
    unsavable on every route until someone ran the harvester.
    """
    assert check_choices("AE-CZE", _client(nationality="Notacountry")) == []


def test_choice_rejection_reaches_the_api(api):
    body = _client(client_id="zz-choice-api", gender="male", enabled=False,
                   account="x@y.com", account_password="p")
    r = api.post("/clients", headers={"X-Webhook-Secret-Token": "t" * 64},
                 json=body)
    assert r.status_code == 422
    problems = r.json()["detail"]["problems"] if isinstance(
        r.json().get("detail"), dict) else r.json()["problems"]
    assert any(p["field"] == "gender" for p in problems)
