"""The contact number must be the SUBSCRIBER number, with no country code in it.

*** WHY THIS IS AN ERROR AND NOT A WARNING. ***

Every route's form splits "Contact number" into two controls - the dialling code,
then the number - and the bot fills them from `phone_country_code` and
`phone_number`. A `+971` carried inside the number is therefore typed ON TOP of
the code already chosen: the portal receives 971, then +9715445792.

Nothing checked this. On 2026-10-30 booking request
osama-ahammed-ae-fra-b71d1c80 held `phone_country_code: "971"` with
`phone_number: "+9715445792"`, and the only reason it was not booked against an
unreachable contact number is that the run was cancelled for an unrelated
reason. Stripping the prefix leaves 7 digits, so that number was wrong twice
over and nothing said so.

Refusing costs an agent one retyped field while they are still on the form.
Allowing it costs an appointment booked and PAID against a number VFS cannot
reach, which no later correction undoes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "f" * 64)

from src.utils.config_reader import initialize_config                # noqa: E402

initialize_config()

from src.waitlist import validate                                    # noqa: E402


def _check(number, code="971"):
    return validate.check_phone({"phone_country_code": code,
                                 "phone_number": number})


def _fields(number, code="971"):
    return [p.field for p in _check(number, code)]


# ── the shape the portal actually wants ──────────────────────────────────────

def test_a_plain_subscriber_number_passes():
    assert _check("556023557") == []


def test_human_spacing_is_not_an_error():
    """An agent types what is on the client's passport copy. Spaces, brackets
    and dashes are presentation, and the bot's fill strips them anyway."""
    for written in ("55 602 3557", "(55) 602-3557", "55.602.3557"):
        assert _check(written) == [], written


def test_every_severity_is_error():
    """The whole point. A warning here is a booking made against a number
    nobody can call."""
    for p in _check("+9715445792"):
        assert p.severity == validate.ERROR


# ── the bug ──────────────────────────────────────────────────────────────────

def test_a_leading_plus_is_refused():
    """*** THE ONE THAT WAS LIVE. ***"""
    problems = _check("+9715445792")

    assert [p.field for p in problems] == ["phone_number"]
    assert "country code is chosen in the box beside it" in problems[0].message


def test_the_refusal_shows_how_to_split_it():
    """The reader is a sales agent at a form, not an operator at a terminal."""
    said = _check("+9715445792")[0]

    assert "phone_country_code" in said.hint
    assert "556024553" in said.hint, "it should show a worked example"


def test_a_repeated_country_code_is_refused():
    """No '+', but the code typed again - the same value reaches the portal."""
    problems = _check("9715445792")

    assert "phone_number" in [p.field for p in problems]
    assert any("repeats the country code 971" in p.message for p in problems)


def test_the_refusal_says_what_the_value_should_be():
    """It can compute the answer, so it should give it rather than describe it."""
    said = next(p for p in _check("971551234567")
                if "repeats" in p.message)

    assert '"551234567"' in said.hint


def test_letters_are_refused():
    problems = _check("55abc1234")

    assert [p.field for p in problems] == ["phone_number"]
    assert "Digits only" in problems[0].message


# ── length, per country ──────────────────────────────────────────────────────

def test_the_example_number_passes():
    """556024553 - the shape an agent is told to enter: +971 chosen beside it,
    nine digits in the box. If this ever fails, the rule is wrong, not the data.
    """
    assert _check("556024553") == []


def test_a_uae_number_must_be_nine_digits():
    """`ahmed-travnook-ae-nld-7b4d4f` carried 11 and read as fine, because the
    only check was for a '+'."""
    problems = _check("55623578765")

    assert [p.field for p in problems] == ["phone_number"]
    assert "11 digits" in problems[0].message
    assert "is 9" in problems[0].message


def test_too_short_is_caught_too():
    assert _fields("55512") == ["phone_number"]


def test_the_length_is_judged_after_the_code_is_stripped():
    """osama's number was wrong TWICE: it carried +971, and what was left was 7
    digits. Reporting only the prefix would have sent an agent back with a
    number still too short to work."""
    problems = _check("9715445792")

    assert len(problems) == 2, [p.message for p in problems]
    assert any("repeats the country code" in p.message for p in problems)
    assert any("7 digits" in p.message for p in problems)


def test_only_uae_numbers_are_accepted():
    """Every route is AE-*, every centre is in the UAE, and VFS calls a UAE
    number - so a code that is not 971 is a typo, not a foreign client."""
    problems = _check("9876543210", code="91")

    assert "phone_country_code" in [p.field for p in problems]
    assert "not a UAE dialling code" in problems[0].message


def test_the_code_is_judged_even_when_the_number_is_missing():
    """A record can carry a code with the number still to come, and the code is
    wrong on its own terms."""
    problems = validate.check_phone({"phone_country_code": "44",
                                     "phone_number": ""})

    assert [p.field for p in problems] == ["phone_country_code"]


def test_a_missing_code_is_not_this_rules_business():
    """Whether the code is REQUIRED is asked by the form definition. Refusing an
    empty one here would report a phone problem on every route that has no phone
    field at all."""
    assert validate.check_phone({"phone_country_code": "",
                                 "phone_number": "556024553"}) == []


# ── where it applies ─────────────────────────────────────────────────────────

def test_an_empty_number_is_somebody_elses_rule():
    """Whether the field is REQUIRED is asked by the form definition. This one
    only judges a value that is there - otherwise every route without a phone
    field reports a phone problem."""
    assert _check("") == []
    assert _check(None) == []


def test_the_booking_precheck_applies_it():
    """The auto-booker acts with nobody watching, so this has to be refused on
    the booking path too, not only on the waitlist form."""
    from src.booking import requests as store

    data = {"route": "AE-FRA", "combo": "x", "date_from": "2026-12-01",
            "date_to": "2026-12-31", "phone_country_code": "971",
            "phone_number": "+9715445792"}

    assert "phone_number" in {p.field for p in store.precheck("c-ae-fra-1", data)}


def test_the_waitlist_precheck_applies_it():
    data = {"route": "AE-CHE", "combos": ["Dubai - SCHENGEN"],
            "phone_country_code": "971", "phone_number": "+9715445792"}

    assert "phone_number" in {p.field
                              for p in validate.precheck_client("c1", data)}


#: Records that were ALREADY wrong when this rule was written, 2026-10-30:
#:
#:   osama-ahammed-ae-fra-b71d1c80   phone_number "+9715445792" - carries +971,
#:                                   and only 7 digits remain once it is removed,
#:                                   so the real number is not recoverable from
#:                                   the record and an operator has to supply it.
#:   ahmed-travnook-ae-nld-7b4d4f    phone_number "55623578765" - 11 digits,
#:                                   two too many for a UAE mobile. Which two is
#:                                   not guessable either.
#:
#: Listed rather than silently tolerated, and listed rather than "fixed" by
#: deleting digits: a wrong contact number that LOOKS right is worse than one
#: that fails a check. Delete a line here as each is corrected; a NEW bad record
#: still fails this test.
KNOWN_BAD = {
    "osama-ahammed-ae-fra-b71d1c80.json",
    "ahmed-travnook-ae-nld-7b4d4f.json",
}


def test_no_new_record_has_an_unusable_phone_number():
    """A rule nothing in config/ satisfies is a rule that gets turned off.

    So this pins the two records that were already broken when the rule landed
    (see KNOWN_BAD) and fails on any others - the next bad number is caught by a
    suite that was green before it.
    """
    import json

    offenders = []
    for path in sorted((REPO_ROOT / "config").glob("*/*.json")):
        if path.parent.name not in ("booking_requests", "registrants"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if validate.check_phone(data) and path.name not in KNOWN_BAD:
            offenders.append(path.name)

    assert not offenders, f"records with an unusable phone number: {offenders}"


def test_the_known_bad_records_are_still_actually_bad():
    """The allowlist must not outlive the problem it records.

    Once a number is corrected its entry has to go, or the list quietly becomes
    a hole in the check for a record that is fine again.
    """
    import json

    for name in sorted(KNOWN_BAD):
        for folder in ("booking_requests", "registrants"):
            path = REPO_ROOT / "config" / folder / name
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            assert validate.check_phone(data), (
                f"{name} is fixed - remove it from KNOWN_BAD")
