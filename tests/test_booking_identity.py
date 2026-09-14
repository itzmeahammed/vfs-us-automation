"""Identity resolution — the decision that must not be wrong.

Getting this wrong books one client's appointment under another's passport:
unrecoverable, costs a real person a real slot, may burn the account. Against
that, missing an invitation is a bad day.

So most of this file asserts REFUSALS. The adversarial cases — two clients
sharing a surname on one account — are the ones that matter, and they are cheap
to test here and unaffordable to get wrong in production.
"""

import pytest

from src.booking.identity import (
    Candidate,
    Confidence,
    normalise_name,
    normalise_reference,
    references_match,
    resolve,
    score_names,
    verify,
)


def client(key, name="", reference="", **extra):
    return Candidate(key=key, name=name, reference=reference, extra=extra)


# --------------------------------------------------------------------------- #
# Name normalisation                                                           #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "left,right,why",
    [
        ("IRINA KONOVALOVA", "Irina Konovalova", "case"),
        ("IRINA KONOVALOVA", "KONOVALOVA IRINA", "token order — portals differ"),
        ("Ahmed  Khan", "Ahmed Khan", "whitespace"),
        ("KONOVÁLOVÁ IRINA", "Irina Konovalova", "diacritics"),
        ("Mr Ahmed Khan", "Ahmed Khan", "honorific"),
        ("MARY O'BRIEN", "Mary OBrien", "apostrophe deleted, not spaced"),
        ("AHMED AL-FARSI", "Ahmed Al Farsi", "hyphen splits"),
    ],
)
def test_the_same_person_normalises_identically(left, right, why):
    assert normalise_name(left) == normalise_name(right), why


def test_an_apostrophe_is_deleted_but_a_hyphen_splits():
    """A REAL BUG the first version had: both were spaced, so O'BRIEN became two
    tokens and never matched OBrien. An apostrophe sits inside one name; a
    hyphen joins two parts that portals render inconsistently."""
    assert normalise_name("O'BRIEN") == "obrien"
    assert normalise_name("AL-FARSI") == "al farsi"


@pytest.mark.parametrize("value", ["", "   ", "!!!", "Mr", "Dr Mrs"])
def test_an_unusable_name_normalises_to_empty(value):
    """Callers treat '' as 'cannot compare' — never 'matches everything'."""
    assert normalise_name(value) == ""


# --------------------------------------------------------------------------- #
# Reference normalisation — three real formats                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "left,right",
    [
        ("GRC127086415238", "grc127086415238"),
        ("GRC127086415238", "GRC 127086415238"),   # dashboard may add spacing
        ("WL-77231", "WL77231"),
        ("SWDB79923880977", "SWDB79923880977"),
    ],
)
def test_references_compare_equal_across_formatting(left, right):
    assert references_match(left, right)


def test_different_references_do_not_match():
    assert not references_match("GRC127086415238", "GRC127086482266")


def test_an_empty_reference_never_matches():
    """Otherwise a client with no stored reference would match every row."""
    assert not references_match("", "")
    assert not references_match("", "GRC127086415238")


def test_the_dashboard_and_email_reference_are_the_same_value():
    """CONFIRMED for Greece, 2026-09-02: the email's 'Unique Reference Number'
    and the dashboard's 'Group Reference Number' are one value. That equality is
    what makes the whole join exact rather than fuzzy."""
    assert references_match("GRC127086415238", "GRC127086415238")


# --------------------------------------------------------------------------- #
# Scoring                                                                      #
# --------------------------------------------------------------------------- #

def test_identical_names_score_exact():
    assert score_names("Ahmed Khan", "AHMED KHAN") == Confidence.EXACT


def test_a_missing_middle_name_scores_below_exact():
    """Suggestive, not conclusive: a middle name we have never seen might belong
    to a different person on the same account."""
    level = score_names("Ahmed Khan", "Ahmed Ali Khan")
    assert level == Confidence.STRONG
    assert level != Confidence.EXACT


def test_a_shared_surname_alone_is_weak():
    """THE danger case, scored deliberately low."""
    assert score_names("Ahmed Khan", "Fatima Khan") == Confidence.WEAK


def test_unrelated_names_score_none():
    assert score_names("Ahmed Khan", "Irina Konovalova") == Confidence.NONE


def test_an_empty_name_scores_none():
    assert score_names("", "Ahmed Khan") == Confidence.NONE


def test_confidence_ordering():
    assert Confidence.at_least(Confidence.EXACT, Confidence.STRONG)
    assert not Confidence.at_least(Confidence.WEAK, Confidence.STRONG)


# --------------------------------------------------------------------------- #
# Resolution — the reference path                                              #
# --------------------------------------------------------------------------- #

ROWS = [
    client("row-0", "MICHEL EL KHOURY", "GRC127086415238"),
    client("row-1", "MUHJA MOHAMED ELHASSAN GADOUR", "GRC127086482266"),
    client("row-2", "KHADIJA AL JACHI", "GRC127086557919"),
]


def test_a_reference_resolves_exactly():
    """The real dashboard, from the screenshot."""
    found = resolve(ROWS, reference="GRC127086482266")
    assert found.resolved
    assert found.candidate.key == "row-1"
    assert found.confidence == Confidence.EXACT
    assert found.matched_on == "reference"


def test_a_reference_match_ignores_a_conflicting_name():
    """Reference is exact and short-circuits: no name can override it."""
    found = resolve(ROWS, reference="GRC127086415238", name="SOMEBODY ELSE")
    assert found.candidate.key == "row-0"
    assert found.matched_on == "reference"


def test_a_reference_matching_two_candidates_refuses():
    """A data fault, not an ordinary tie. Refuse rather than pick."""
    duplicated = [client("a", "X", "GRC1"), client("b", "Y", "GRC1")]
    found = resolve(duplicated, reference="GRC1")
    assert not found.resolved
    assert "inconsistent" in found.reason


def test_an_unmatched_reference_falls_back_to_the_name():
    """A client registered before references were captured still resolves."""
    found = resolve(ROWS, reference="NOTHING", name="KHADIJA AL JACHI")
    assert found.resolved
    assert found.candidate.key == "row-2"
    assert found.matched_on == "name"


# --------------------------------------------------------------------------- #
# Resolution — refusals                                                        #
# --------------------------------------------------------------------------- #

def test_two_clients_with_the_same_name_resolve_NEITHER():
    """THE case this module exists for. Booking neither is a bad day; booking
    the wrong one is unrecoverable."""
    twins = [client("ahmed-1", "AHMED KHAN"), client("ahmed-2", "AHMED KHAN")]
    found = resolve(twins, name="AHMED KHAN")

    assert not found.resolved
    assert "refusing to guess" in found.reason
    assert len(found.rejected) == 2


def test_a_tie_can_be_permitted_explicitly_but_never_by_default():
    twins = [client("a", "AHMED KHAN"), client("b", "AHMED KHAN")]
    assert not resolve(twins, name="AHMED KHAN").resolved
    assert resolve(twins, name="AHMED KHAN", require_unique=False).resolved


def test_a_weak_match_is_refused_at_the_default_confidence():
    """Sharing a surname must not be enough to book someone."""
    found = resolve([client("f", "FATIMA KHAN")], name="AHMED KHAN")
    assert not found.resolved
    assert "confidence" in found.reason


def test_lowering_the_bar_admits_a_weaker_match():
    """Legitimate for NARROWING a shortlist; never where a booking follows."""
    found = resolve([client("f", "Ahmed Ali Khan")], name="Ahmed Khan",
                    min_confidence=Confidence.STRONG)
    assert found.resolved


def test_no_candidates_resolves_to_nothing():
    found = resolve([], reference="GRC1", name="X")
    assert not found.resolved
    assert "no candidates" in found.reason


def test_nothing_to_match_on_resolves_to_nothing():
    found = resolve(ROWS)
    assert not found.resolved


def test_a_uniquely_best_match_wins_over_weaker_ones():
    """A tie is only a tie at the SAME confidence — an exact match beats a weak
    one outright."""
    candidates = [client("exact", "AHMED KHAN"), client("weak", "AHMED ALI")]
    found = resolve(candidates, name="AHMED KHAN")
    assert found.resolved and found.candidate.key == "exact"


def test_rejected_candidates_are_reported_even_on_success():
    """A resolution nobody can explain afterwards is not worth trusting."""
    found = resolve(ROWS, reference="GRC127086415238")
    assert len(found.rejected) == 2
    assert "rejected" in found.describe()


# --------------------------------------------------------------------------- #
# verify — the click-then-check half                                           #
# --------------------------------------------------------------------------- #

def test_verification_passes_on_a_matching_reference():
    row = client("row", "MICHEL EL KHOURY", "GRC127086415238")
    assert verify(row, reference="GRC127086415238").resolved


def test_verification_FAILS_on_a_mismatched_reference():
    """The whole point of click-then-check: opening a row commits nothing, so a
    wrong click is free — provided it is DETECTED."""
    row = client("row", "MICHEL EL KHOURY", "GRC127086415238")
    found = verify(row, reference="GRC999999999999")

    assert not found.resolved
    assert "MISMATCH" in found.reason


def test_verification_fails_on_a_mismatched_name_even_if_nothing_else_is_given():
    row = client("row", "MICHEL EL KHOURY")
    assert not verify(row, name="AHMED KHAN").resolved


def test_one_agreeing_field_is_enough():
    """Portals show different things; requiring all of them would fail on a page
    that simply omits one."""
    row = client("row", "MICHEL EL KHOURY")
    found = verify(row, name="MICHEL EL KHOURY", reference="GRC1")
    assert found.resolved
    assert found.matched_on == "name"      # reference absent on the row, skipped


def test_a_contradiction_beats_an_agreement():
    """Silence is tolerated; disagreement is fatal."""
    row = client("row", "MICHEL EL KHOURY", "GRC127086415238")
    found = verify(row, name="MICHEL EL KHOURY", reference="GRC999")
    assert not found.resolved


def test_nothing_comparable_is_not_a_pass():
    """A verification with no shared field proves nothing and must not read as
    success."""
    found = verify(client("row"), reference="GRC1", name="X")
    assert not found.resolved
    assert "nothing comparable" in found.reason


def test_a_passport_can_verify_when_the_page_shows_one():
    row = client("row", "MICHEL EL KHOURY", passport="A1234567")
    assert verify(row, passport="A1234567").resolved
    assert not verify(row, passport="B7654321").resolved
