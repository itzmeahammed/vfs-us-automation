"""Matching is pure, so it is tested against data — no IMAP, no network.

The cases below are the ones that actually decide whether this works:

  * the two REAL Italian emails, verbatim, as fixtures
  * the wording traps in them ('Tourist visa visa category', 'Reference Number
    is ITD...') which a plausible-looking regex gets wrong
  * the three different reference formats seen in production, since a pattern
    tuned to one silently misses the others
  * extraction FAILING, which must never break a match
  * matcher order, since first-match-wins makes it load-bearing
"""

import os

import pytest

from src.inbox.matcher import (
    CONFIRMATION,
    INVITATION,
    OTHER,
    UNMATCHED,
    Email,
    MatcherConfigError,
    classify,
    classify_all,
    extract_fields,
    matches,
    validate_matcher,
    validate_matchers,
)

FIXTURE_DIR = os.path.join("tests", "fixtures", "emails")


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

def load_fixture(name: str) -> Email:
    """Parses a saved .eml through the real watcher parser."""
    from src.inbox.watcher import _parse_message

    with open(os.path.join(FIXTURE_DIR, name), "rb") as f:
        raw = f.read()
    return _parse_message(raw, uid="1", mailbox="test", received=1756000000.0)


@pytest.fixture
def invitation() -> Email:
    return load_fixture("ae-ita-invitation.eml")


@pytest.fixture
def confirmation() -> Email:
    return load_fixture("ae-ita-confirmation.eml")


@pytest.fixture
def italy_matchers():
    from src.inbox import config as inbox_config

    inbox_config.clear_cache()
    return inbox_config.matchers_for("AE-ITA")


# --------------------------------------------------------------------------- #
# The real emails                                                              #
# --------------------------------------------------------------------------- #

def test_real_invitation_classifies(invitation, italy_matchers):
    found = classify(invitation, italy_matchers, route="AE-ITA")
    assert found.classification == INVITATION
    assert found.matcher_name == "waitlist_invitation"
    assert found.validity_hours == 48


def test_real_invitation_extracts_name_and_category(invitation, italy_matchers):
    found = classify(invitation, italy_matchers, route="AE-ITA")
    assert found.get("applicant_name") == "IRINA KONOVALOVA"
    # 'for the Tourist visa visa category' — the doubled word is real. Stopping
    # at the first 'visa' is what keeps this from capturing 'Tourist visa visa'.
    assert found.get("category") == "Tourist"


def test_real_confirmation_classifies_and_yields_the_reference(
    confirmation, italy_matchers
):
    found = classify(confirmation, italy_matchers, route="AE-ITA")
    assert found.classification == CONFIRMATION
    assert found.get("reference") == "ITD125298020335"
    assert found.get("applicant_name") == "IRINA KONOVALOVA"


def test_the_two_emails_do_not_match_each_others_matcher(
    invitation, confirmation, italy_matchers
):
    """Both mention waitlists and both greet by name; only the subject separates
    them. A matcher keyed too loosely would classify both the same way."""
    assert classify(invitation, italy_matchers).classification == INVITATION
    assert classify(confirmation, italy_matchers).classification == CONFIRMATION


# --------------------------------------------------------------------------- #
# Reference formats — three real shapes, one pattern                           #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Your Unique Reference Number is ITD125298020335.", "ITD125298020335"),
        ("Your Unique Reference Number is SWDB79923880977.", "SWDB79923880977"),
        ("Your Unique Reference Number is WL-77231.", "WL-77231"),
    ],
)
def test_reference_pattern_covers_every_observed_format(text, expected):
    """ITD… (3-letter), SWDB… (4-letter) and the hand-verified WL-77231 all
    exist in production. A tightened \\b[A-Z]{4}\\d{9,}\\b — which the waitlist
    configs use — would miss two of these three."""
    email = Email(subject="Successfully Added to Waitlist", body=text)
    found = extract_fields(email, {"reference": r"Reference Number is\s+([A-Z0-9-]+)"})
    assert found["reference"] == expected


# --------------------------------------------------------------------------- #
# Name extraction                                                              #
# --------------------------------------------------------------------------- #

NAME_PATTERN = r"Dear\s+([A-Za-z][A-Za-z\s'.-]+?)\s*,"


@pytest.mark.parametrize(
    "greeting,expected",
    [
        ("Dear IRINA KONOVALOVA,", "IRINA KONOVALOVA"),
        ("Dear Ahmed Khan,", "Ahmed Khan"),
        ("Dear MARY O'BRIEN,", "MARY O'BRIEN"),
        ("Dear AHMED AL-FARSI,", "AHMED AL-FARSI"),
        # Trailing punctuation is stripped, so a suffixed name loses its stop.
        # Harmless: the value is a matching HINT, and normalisation drops
        # punctuation on both sides before any comparison anyway.
        ("Dear John Smith Jr.,", "John Smith Jr"),
        ("Dear  SPACED   NAME ,", "SPACED NAME"),
    ],
)
def test_name_extraction_handles_real_name_shapes(greeting, expected):
    email = Email(body=f"{greeting}\n\nSome body text.")
    found = extract_fields(email, {"applicant_name": NAME_PATTERN})
    assert found["applicant_name"] == expected


def test_name_extraction_survives_a_hard_wrapped_body():
    """Mail is wrapped at ~72 chars, so a phrase can straddle a newline in the
    body but not in the config. Matching happens on whitespace-normalised text."""
    email = Email(body="Dear IRINA\nKONOVALOVA,\n\nBody.")
    found = extract_fields(email, {"applicant_name": NAME_PATTERN})
    assert found["applicant_name"] == "IRINA KONOVALOVA"


def test_a_failed_extraction_is_none_not_an_error():
    """'Dear Applicant' carries no name. That must not break the match — identity
    is settled against the dashboard, never against the greeting."""
    email = Email(body="Dear Applicant, your slots are ready.")
    found = extract_fields(email, {"applicant_name": r"Dear\s+([A-Z]{2,}\s+[A-Z]{2,}),"})
    assert found["applicant_name"] is None


def test_extraction_failure_does_not_prevent_classification():
    matchers = [{
        "name": "invite",
        "classify": INVITATION,
        "subject_contains": ["Slots available"],
        "extract": {"applicant_name": r"Dear\s+([A-Z]+\s+[A-Z]+),"},
    }]
    email = Email(subject="Slots available for booking", body="Dear Applicant,")
    found = classify(email, matchers)
    assert found.classification == INVITATION      # still matched
    assert found.get("applicant_name") is None     # just no name


# --------------------------------------------------------------------------- #
# Conditions                                                                   #
# --------------------------------------------------------------------------- #

def test_conditions_are_anded_together():
    matcher = {
        "name": "m", "classify": INVITATION,
        "subject_contains": ["Slots available"],
        "body_contains": ["now available for booking"],
    }
    assert matches(Email(subject="Slots available", body="now available for booking"), matcher)
    assert not matches(Email(subject="Slots available", body="unrelated"), matcher)
    assert not matches(Email(subject="Other", body="now available for booking"), matcher)


def test_alternatives_within_one_condition_are_ored():
    matcher = {"name": "m", "classify": OTHER, "from_contains": ["a.com", "b.com"]}
    assert matches(Email(sender="x@a.com"), matcher)
    assert matches(Email(sender="x@b.com"), matcher)
    assert not matches(Email(sender="x@c.com"), matcher)


def test_matching_is_case_insensitive():
    matcher = {"name": "m", "classify": INVITATION, "subject_contains": ["SLOTS AVAILABLE"]}
    assert matches(Email(subject="slots available for booking"), matcher)


def test_a_scalar_condition_is_accepted_as_well_as_a_list():
    """Hand-written config uses both forms; rejecting the scalar is a papercut."""
    matcher = {"name": "m", "classify": OTHER, "subject_contains": "Slots"}
    assert matches(Email(subject="Slots available"), matcher)


# --------------------------------------------------------------------------- #
# Ordering                                                                     #
# --------------------------------------------------------------------------- #

def test_first_match_wins_so_config_order_matters():
    email = Email(subject="Slots available for booking", sender="x@vfsglobal.com")
    specific = {"name": "invite", "classify": INVITATION, "subject_contains": ["Slots available"]}
    catchall = {"name": "other", "classify": OTHER, "from_contains": ["vfsglobal.com"]}

    assert classify(email, [specific, catchall]).classification == INVITATION
    # Reversed, the catch-all swallows it — which is why _default puts it last.
    assert classify(email, [catchall, specific]).classification == OTHER


def test_unmatched_returns_a_match_object_not_none():
    """Callers must never have to branch on None."""
    found = classify(Email(subject="Lunch?"), [
        {"name": "m", "classify": INVITATION, "subject_contains": ["Slots"]}
    ])
    assert found.classification == UNMATCHED
    assert not found.matched
    assert found.get("anything") is None


def test_classify_all_tries_every_route_deterministically():
    matchers_by_route = {
        "AE-ITA": [{"name": "ita", "classify": INVITATION, "from_contains": ["italyuae"]}],
        "AE-CHE": [{"name": "che", "classify": INVITATION, "from_contains": ["cheuae"]}],
    }
    found = classify_all(Email(sender="info.cheuae@vfshelpline.com"), matchers_by_route)
    assert found.route == "AE-CHE"
    assert classify_all(Email(sender="nobody@example.com"), matchers_by_route).matched is False


# --------------------------------------------------------------------------- #
# Validation — errors belong at load time, with a filename                     #
# --------------------------------------------------------------------------- #

def test_a_matcher_with_no_conditions_is_rejected():
    """It would match every email in the mailbox. Always a mistake."""
    with pytest.raises(MatcherConfigError, match="no conditions"):
        validate_matcher({"name": "m", "classify": INVITATION})


def test_an_extract_pattern_without_a_capture_group_is_rejected():
    """Group 1 is the value, so a group-less pattern can only ever record None —
    a silent no-op that looks like it works."""
    with pytest.raises(MatcherConfigError, match="capture group"):
        validate_matcher({
            "name": "m", "classify": INVITATION,
            "subject_contains": ["x"], "extract": {"ref": r"Reference \d+"},
        })


def test_an_invalid_regex_is_rejected():
    with pytest.raises(MatcherConfigError, match="invalid regex"):
        validate_matcher({
            "name": "m", "classify": INVITATION,
            "subject_contains": ["x"], "extract": {"ref": r"([A-Z"},
        })


def test_an_unknown_classification_is_rejected():
    with pytest.raises(MatcherConfigError, match="Valid:"):
        validate_matcher({"name": "m", "classify": "booked", "subject_contains": ["x"]})


def test_a_missing_name_or_classify_is_rejected():
    with pytest.raises(MatcherConfigError, match="name"):
        validate_matcher({"classify": INVITATION, "subject_contains": ["x"]})
    with pytest.raises(MatcherConfigError, match="classify"):
        validate_matcher({"name": "m", "subject_contains": ["x"]})


def test_duplicate_matcher_names_are_rejected():
    with pytest.raises(MatcherConfigError, match="duplicate"):
        validate_matchers([
            {"name": "m", "classify": INVITATION, "subject_contains": ["a"]},
            {"name": "m", "classify": CONFIRMATION, "subject_contains": ["b"]},
        ])


def test_validity_hours_must_be_a_positive_integer():
    with pytest.raises(MatcherConfigError, match="validity_hours"):
        validate_matcher({
            "name": "m", "classify": INVITATION,
            "subject_contains": ["x"], "validity_hours": 0,
        })


# --------------------------------------------------------------------------- #
# Formats found by the first LIVE pass (2026-09-02)                            #
# --------------------------------------------------------------------------- #
# These are not designed cases — they are what three real mailboxes actually
# contained. They exist so a future config edit cannot silently stop matching
# mail that is known to arrive.

def test_the_real_sender_is_donotreply_not_the_signature_address(italy_matchers):
    """MEASURED: every VFS message came from donotreply@vfshelpline.com, NOT the
    info.italyuae@ address printed in the body's signature. Pinning the
    signature address would match nothing at all."""
    email = Email(
        subject="Slots available for booking an appointment",
        sender="VFS <donotreply@vfshelpline.com>",
        # The portal URL is required: it is the country discriminator that keeps
        # one country's mail from matching another's matcher and inheriting the
        # wrong validity window.
        body=("Dear IRINA KONOVALOVA, ... are now available for booking. "
              "https://services.vfsglobal.com/are/en/ita/login"),
    )
    assert classify(email, italy_matchers).classification == INVITATION


def test_a_real_waitlist_cancellation_is_recognised(italy_matchers):
    """Cancellation means a 'success' journal row is NO LONGER TRUE. Nothing acts
    on that yet, but the mail must at least be recognised and its reference
    captured."""
    email = Email(
        subject="Waitlist cancellation",
        sender="donotreply@vfshelpline.com",
        body=("VFS Appointment System Dear AHMED KHAN Your appointment for "
              "Application Number SWDB79923880977 has been cancelled on "
              "11-08-2026 and 04:22:54 PM ."),
    )
    found = classify(email, italy_matchers)
    assert found.matcher_name == "waitlist_cancellation"
    assert found.get("reference") == "SWDB79923880977"
    assert found.get("applicant_name") == "AHMED KHAN"


def test_the_cancellation_greeting_has_no_comma():
    """'Dear TRAV NOOK Your appointment for...' — unlike the invitation and the
    confirmation, which both end the greeting with a comma. A pattern anchored
    on the comma captures nothing here, so the two shapes need separate
    patterns."""
    body = "Dear TRAV NOOK Your appointment for Application Number SWDB79819570830"
    with_comma = extract_fields(Email(body=body), {"n": NAME_PATTERN})
    assert with_comma["n"] is None

    without = extract_fields(
        Email(body=body), {"n": r"Dear\s+([A-Za-z][A-Za-z\s'.-]+?)\s+Your"}
    )
    assert without["n"] == "TRAV NOOK"


def test_otp_mail_is_recognised_rather_than_left_unrecognised(italy_matchers):
    """111 of 123 messages in the first live pass. Left in the catch-all they
    bury everything worth reading in the digest."""
    email = Email(subject="One Time Password", sender="donotreply@vfshelpline.com",
                  body="Your OTP is attached.")
    assert classify(email, italy_matchers).matcher_name == "vfs_otp"


def test_ordinary_vfs_mail_still_reaches_the_catch_all(italy_matchers):
    """Welcome / refund / cancellation-confirmation mail has no specific matcher.
    It must stay visible as 'unrecognised' — that is how a new email type gets
    noticed."""
    for subject in ("Welcome", "Refund Processed",
                    "Appointment Cancellation Confirmation"):
        email = Email(subject=subject, sender="donotreply@vfsglobal.com", body="...")
        assert classify(email, italy_matchers).matcher_name == "vfs_other", subject


# --------------------------------------------------------------------------- #
# Multi-country routing (bugs found 2026-09-02 by adding a 2nd and 3rd country) #
# --------------------------------------------------------------------------- #

def _fixture_matchers():
    from src.inbox import config as inbox_config

    inbox_config.clear_cache()
    return inbox_config.all_matchers()


@pytest.mark.parametrize(
    "filename,route,hours,category",
    [
        ("ae-ita-invitation.eml", "AE-ITA", 48, "Tourist"),
        ("ae-grc-invitation.eml", "AE-GRC", 36, "General"),
        ("ae-nld-invitation.eml", "AE-NLD", 36, "Tourist Purpose"),
    ],
)
def test_each_countrys_invitation_routes_to_its_own_config(
    filename, route, hours, category
):
    """THE 36-vs-48 BUG. Every country's invitation shares a subject and nearly
    identical wording, so without a discriminator one country's mail matches
    another's matcher and inherits the wrong validity window — silently
    believing a window is open 12 hours after it closed.

    The portal URL in the body (/are/en/grc/) is the discriminator.
    """
    found = classify_all(load_fixture(filename), _fixture_matchers())
    assert found.route == route
    assert found.classification == INVITATION
    assert found.validity_hours == hours
    assert found.get("category") == category


def test_a_specific_matcher_beats_another_routes_catch_all():
    """THE SHADOWING BUG. Every route inherits the generic vfs_other from
    _default. Tried route-by-route, the alphabetically-first route's catch-all
    swallowed an Italian invitation before AE-ITA's own matcher was reached —
    reporting it as unrecognised Greek mail.

    classify_all therefore runs specific matchers across ALL routes first, and
    only then the catch-alls.
    """
    found = classify_all(load_fixture("ae-ita-invitation.eml"), _fixture_matchers())
    assert found.matcher_name == "waitlist_invitation"
    assert found.route == "AE-ITA", "a catch-all must not shadow a specific match"


def test_ordinary_vfs_mail_still_lands_in_a_catch_all():
    """The second pass must still work — generic mail has nowhere else to go."""
    email = Email(subject="Welcome", sender="donotreply@vfsglobal.com",
                  body="Your account has been successfully created.")
    found = classify_all(email, _fixture_matchers())
    assert found.matcher_name == "vfs_other"


def test_alternatives_within_a_condition_loosen_rather_than_tighten():
    """A trap worth pinning: adding a second entry to body_contains makes the
    matcher match MORE, not less. Conditions AND across kinds (subject AND from
    AND body); alternatives OR within one kind."""
    matcher = {
        "name": "m", "classify": CONFIRMATION,
        "body_contains": ["/are/en/ita/", "has been successfully registered"],
    }
    assert matches(Email(body="see /are/en/ita/ for details"), matcher)
    assert matches(Email(body="has been successfully registered"), matcher)


# --------------------------------------------------------------------------- #
# Appointment confirmations (found live, 2026-09-03)                           #
# --------------------------------------------------------------------------- #
# The END of the journey — VFS confirming a booked appointment. Not designed up
# front; found by reading a real mailbox. Two wordings, three reference labels,
# and a THIRD greeting shape.

def test_a_greece_appointment_confirmation_is_parsed(italy_matchers):
    """The evidence that ties everything together: this reference and name are
    EXACTLY row [0] of the real dashboard screenshot, which independently
    confirms VFS's mail and the dashboard carry the same reference."""
    email = Email(
        subject="Your Greece Visa Appointment is Confirmed!",
        sender="donotreply@vfsglobal.com",
        body=("Appointment Reference Group URN - GRC127086415238 "
              "Dear MICHEL EL KHOURY Greetings from VFS Global. "
              "Please find your Appointment Letter attached."),
    )
    found = classify(email, italy_matchers)
    assert found.matcher_name == "appointment_confirmed"
    assert found.get("reference") == "GRC127086415238"
    assert found.get("applicant_name") == "MICHEL EL KHOURY"


def test_the_plainer_confirmation_wording_is_also_parsed(italy_matchers):
    """Same mailbox, different template: 'Appointment Confirmation' with
    'Unique Reference Number' instead of 'Group URN'. One matcher serves both."""
    email = Email(
        subject="Appointment Confirmation",
        sender="donotreply@vfshelpline.com",
        body=("VFS Appointment System Dear Applicant, Please note that your "
              "appointment for Unique Reference Number NOR126162488430 on "
              "08-09-2026 at 11:45 at Wafi Mall"),
    )
    found = classify(email, italy_matchers)
    assert found.matcher_name == "appointment_confirmed"
    assert found.get("reference") == "NOR126162488430"
    assert found.get("applicant_name") is None      # 'Dear Applicant' — tolerated


@pytest.mark.parametrize(
    "reference",
    ["GRC127086415238", "DEU72044101933", "CZH124459430502", "NOR126162488430"],
)
def test_every_live_confirmation_reference_format_is_matched(reference, italy_matchers):
    """Four countries, four prefixes, all seen in one real mailbox."""
    email = Email(
        subject="Your Visa Appointment is Confirmed!",
        body=f"Appointment Reference Group URN - {reference} Dear X Y Greetings",
    )
    assert classify(email, italy_matchers).get("reference") == reference


def test_the_confirmation_greeting_ends_at_Greetings_not_a_comma():
    """A THIRD greeting shape: 'Dear MICHEL EL KHOURY Greetings from VFS Global'
    — no comma, unlike the invitation. The cancellation email has yet another.
    Assume nothing about greetings across email types."""
    body = "Dear MICHEL EL KHOURY Greetings from VFS Global."
    assert extract_fields(Email(body=body), {"n": NAME_PATTERN})["n"] is None

    pattern = r"Dear\s+([A-Za-z][A-Za-z\s'.-]+?)\s+Greetings"
    assert extract_fields(Email(body=body), {"n": pattern})["n"] == "MICHEL EL KHOURY"


def test_an_appointment_confirmation_is_not_a_waitlist_confirmation(italy_matchers):
    """Both say 'confirmed' and carry a reference, but they mean opposite things:
    one is 'on the waitlist', the other is 'appointment booked'. Confusing them
    would mark a waiting client as booked."""
    email = Email(
        subject="Your Greece Visa Appointment is Confirmed!",
        body="Appointment Reference Group URN - GRC1 Dear X Y Greetings",
    )
    assert classify(email, italy_matchers).classification != CONFIRMATION
