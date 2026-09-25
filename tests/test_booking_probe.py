"""Parsing dashboard cards, and matching one to a client.

`parse_card` is pure, so the card text from the real screenshot can be asserted
against directly — no browser, no account. That is the whole point: by the time
the probe runs live, the parsing is already known to work and any failure is
about selectors, not logic.

Card text below is transcribed from the actual Greece dashboard screenshot
(2026-09-02).
"""

import pytest

from src.booking.probe import (
    DEFAULT_REFERENCE_PATTERN,
    DashboardRow,
    match_row,
    parse_card,
)

# Exactly as the screenshot reads, top to bottom.
INVITED_CARD = """Appointment details
General
Group Reference Number - GRC127086557919
Manage Application
Waitlist Status: SLOTS AVAILABLE
Book Now
Applicants:
KHADIJA AL JACHI"""

ACTIVE_CARD = """Appointment details
General
Group Reference Number - GRC127086415238
Manage Appointment
Applicants:
MICHEL EL KHOURY"""


def parse(text, index=0):
    return parse_card(text, index, DEFAULT_REFERENCE_PATTERN)


# --------------------------------------------------------------------------- #
# The real cards                                                               #
# --------------------------------------------------------------------------- #

def test_an_invited_card_is_read_correctly():
    row = parse(INVITED_CARD)
    assert row.reference == "GRC127086557919"
    assert row.applicants == ["KHADIJA AL JACHI"]
    assert row.bookable is True
    assert "Book Now" in row.actions


def test_an_ordinary_active_card_is_not_bookable():
    """'Manage Appointment' without 'SLOTS AVAILABLE' means waiting, not
    invited. Treating it as bookable would send the runner at a row that has
    nothing to book."""
    row = parse(ACTIVE_CARD)
    assert row.reference == "GRC127086415238"
    assert row.applicants == ["MICHEL EL KHOURY"]
    assert row.bookable is False
    assert "Manage Appointment" in row.actions


def test_bookability_comes_from_the_waitlist_status_not_the_button():
    """The status text is the fact; the button is a consequence. Keying off the
    button alone would misread a portal that labels it differently."""
    without_button = INVITED_CARD.replace("Book Now\n", "")
    assert parse(without_button).bookable is True


def test_the_raw_text_is_always_kept():
    """A card that fails to parse is far more useful reported in full than
    reduced to empty fields — the probe's job includes saying what it could not
    read."""
    row = parse("something unexpected")
    assert row.raw_text == "something unexpected"


# --------------------------------------------------------------------------- #
# Shapes the portal might throw                                                #
# --------------------------------------------------------------------------- #

def test_a_card_wrapped_across_lines_still_parses():
    """Real inner_text arrives hard-wrapped; matching happens on a
    whitespace-normalised copy."""
    row = parse("Group Reference Number -\n  GRC127086557919\nApplicants:\n  X Y")
    assert row.reference == "GRC127086557919"


def test_several_applicants_on_one_card_are_all_captured():
    """A group booking. The first is the one the invitation greets."""
    row = parse("Group Reference Number - GRC1\nApplicants:\nAHMED KHAN, FATIMA KHAN")
    assert row.applicants == ["AHMED KHAN", "FATIMA KHAN"]
    assert row.name == "AHMED KHAN"


def test_a_card_with_no_reference_still_yields_a_row():
    """Reported rather than dropped, so a changed label is visible in the output
    instead of silently reducing the card count."""
    row = parse("Appointment details\nApplicants:\nAHMED KHAN")
    assert row.reference == ""
    assert row.applicants == ["AHMED KHAN"]


def test_an_empty_card_parses_without_raising():
    row = parse("")
    assert row.reference == "" and row.applicants == []


@pytest.mark.parametrize(
    "reference",
    ["GRC127086415238", "ITD125298020335", "SWDB79923880977", "WL-77231"],
)
def test_every_observed_reference_format_is_matched(reference):
    """Three real prefixes plus a hand-recorded one. A pattern tuned to a single
    format silently misses the others."""
    row = parse(f"Group Reference Number - {reference}\nApplicants:\nX Y")
    assert row.reference == reference


# --------------------------------------------------------------------------- #
# Matching a client to a row                                                   #
# --------------------------------------------------------------------------- #

ROWS = [
    DashboardRow(index=0, reference="GRC127086415238",
                 applicants=["MICHEL EL KHOURY"]),
    DashboardRow(index=1, reference="GRC127086482266",
                 applicants=["MUHJA MOHAMED ELHASSAN GADOUR"]),
    DashboardRow(index=2, reference="GRC127086557919",
                 applicants=["KHADIJA AL JACHI"], bookable=True),
]


def test_a_stored_reference_selects_the_row_exactly():
    """THE JOIN: the confirmation email's reference is the same value the
    dashboard shows, so a stored reference needs no name matching at all."""
    row, reason = match_row(ROWS, reference="GRC127086557919")
    assert row.index == 2
    assert "exactly" in reason


def test_a_name_selects_the_row_when_no_reference_is_stored():
    """The fallback for clients registered before references were captured."""
    row, reason = match_row(ROWS, name="MICHEL EL KHOURY")
    assert row.index == 0


def test_the_reference_wins_over_a_conflicting_name():
    row, _ = match_row(ROWS, reference="GRC127086415238", name="KHADIJA AL JACHI")
    assert row.index == 0


def test_two_identical_names_resolve_to_NOTHING():
    """The probe obeys the same rule the runner will: booking the wrong client
    is unrecoverable, so a tie resolves to nothing rather than a guess."""
    twins = [
        DashboardRow(index=0, applicants=["AHMED KHAN"]),
        DashboardRow(index=1, applicants=["AHMED KHAN"]),
    ]
    row, reason = match_row(twins, name="AHMED KHAN")
    assert row is None
    assert "refusing to guess" in reason


def test_an_unknown_client_matches_nothing():
    row, reason = match_row(ROWS, name="SOMEBODY ELSE")
    assert row is None


def test_an_empty_dashboard_matches_nothing():
    row, reason = match_row([], reference="GRC1")
    assert row is None
    assert "no rows" in reason


def test_name_order_does_not_prevent_a_match():
    """VFS renders 'KHADIJA AL JACHI'; a client file may hold the parts in
    another order. Normalisation sorts tokens so either compares equal."""
    row, _ = match_row(ROWS, name="AL JACHI KHADIJA")
    assert row is not None and row.index == 2


# --------------------------------------------------------------------------- #
# Diagnosing an empty result                                                   #
# --------------------------------------------------------------------------- #
# "0 cards" has two causes needing OPPOSITE fixes: an account with no
# applications (nothing to change) versus a wrong selector (fix the config).
# The probe must tell them apart, or hours get spent on the wrong one.

import logging


class FakePage:
    def __init__(self, text):
        self._text = text

    def inner_text(self, selector, timeout=None):
        return self._text


def test_an_empty_account_is_reported_as_such(caplog):
    """Confirmed real case: mufaddal@travnook.com has no GREEK applications, so
    a Greek probe correctly finds nothing."""
    from src.booking.probe import _diagnose_empty

    with caplog.at_level(logging.INFO):
        _diagnose_empty(FakePage("Active application(s) You have no active applications"))

    assert any("holds no applications" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records), \
        "an empty account is not a fault and must not warn"


def test_a_page_with_applications_but_no_matches_warns_about_the_selector(caplog):
    from src.booking.probe import _diagnose_empty

    with caplog.at_level(logging.INFO):
        _diagnose_empty(FakePage(
            "Appointment details Group Reference Number - GRC1 Applicants: X Y"))

    assert any("selector is probably WRONG" in r.message for r in caplog.records)


def test_the_page_text_is_reported_for_diagnosis(caplog):
    """Without it, a failed probe is a dead end rather than a lead."""
    from src.booking.probe import _diagnose_empty

    with caplog.at_level(logging.INFO):
        _diagnose_empty(FakePage("Some unexpected page layout"))

    assert any("Some unexpected page layout" in r.message for r in caplog.records)


def test_an_unreadable_page_does_not_raise():
    """Diagnosis is best-effort — it must never turn a quiet result into a crash."""
    from src.booking.probe import _diagnose_empty

    class Broken:
        def inner_text(self, selector, timeout=None):
            raise RuntimeError("page gone")

    _diagnose_empty(Broken())


# --------------------------------------------------------------------------- #
# The real AE-CHE card                                                         #
# --------------------------------------------------------------------------- #
#
# Every test above uses text transcribed from a SCREENSHOT. The text below was
# taken off the running portal on 2026-09-25, from osama@travnook.com holding
# SWDB82433277533 with an invitation open.

REAL_CHE_CARD = (
    "Appointment details General Appointments "
    "Group Reference Number - SWDB82433277533 Manage Application "
    "Waitlist Status: SLOTS AVAILABLE Book Now "
    "Applicants: MUFADDAL MUFADDAL "
    "Visa Application form Status - Not Initiated Edit Form"
)


def _real_row():
    from src.booking.probe import _reference_pattern

    return parse_card(REAL_CHE_CARD, 0, _reference_pattern("AE-CHE"))


def test_the_real_card_yields_its_reference():
    assert _real_row().reference == "SWDB82433277533"


def test_the_applicant_name_stops_at_the_next_label():
    """The name must not absorb the labels that FOLLOW it.

    The dashboard puts "Visa Application form Status - ..." and "Edit Form"
    straight after the applicant, and a terminator list naming only the labels
    that PRECEDE it captured
    "MUFADDAL MUFADDAL Visa Application form Status - Not Initiated Edit Form"
    as the person's name. It still matched by reference, so this would have
    gone unnoticed until a name comparison quietly failed.
    """
    assert _real_row().name == "MUFADDAL MUFADDAL"


def test_the_reference_resolves_where_a_name_alone_refuses():
    """Why the client file exists at all.

    osama@travnook.com is shared by several real people, and an invitation
    carries a name and no reference. Storing the reference at registration is
    what turns a guess into an exact match.
    """
    rows = [_real_row()]

    found, _ = match_row(rows, reference="SWDB82433277533",
                         name="MUFADDAL MUFADDAL")
    assert found is not None

    refused, reason = match_row(rows, reference="", name="SOMEBODY ELSE")
    assert refused is None, f"should have refused, got {reason}"
