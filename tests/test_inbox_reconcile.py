"""Reconciliation writes 'this client IS registered' into the journal.

A wrong one is worse than doing nothing: the client is then never retried and
silently misses their appointment. So most of these tests are about REFUSING —
ambiguity, missing names, wrong route — rather than about the happy path.
"""

import pytest

from src.inbox.matcher import CONFIRMATION, INVITATION, Email, Match
from src.inbox.reconcile import (
    Proposal,
    apply,
    client_name,
    names_match,
    normalise_name,
    plan,
)
from src.inbox.watcher import Observation
from src.waitlist.result import Status


# --------------------------------------------------------------------------- #
# Name normalisation                                                           #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "left,right",
    [
        ("IRINA KONOVALOVA", "Irina Konovalova"),        # case
        ("IRINA KONOVALOVA", "KONOVALOVA IRINA"),        # order — portals differ
        ("Ahmed  Khan", "Ahmed Khan"),                   # whitespace
        ("KONOVÁLOVÁ IRINA", "Irina Konovalova"),        # diacritics
        ("Mr Ahmed Khan", "Ahmed Khan"),                 # honorific
        ("MARY O'BRIEN", "Mary OBrien"),                 # punctuation
    ],
)
def test_names_that_are_the_same_person_match(left, right):
    assert names_match(left, right)


@pytest.mark.parametrize(
    "left,right",
    [
        ("Ahmed Khan", "Fatima Khan"),          # the shared-account danger case
        ("Ahmed Khan", "Ahmed"),                # a missing token is NOT a match
        ("Ahmed Khan", "Ahmed Khan Ali"),       # an extra token is NOT a match
        ("Ahmed Khan", ""),
        ("", ""),
    ],
)
def test_different_people_do_not_match(left, right):
    """Deliberately strict: no initials, no substrings, no edit distance."""
    assert not names_match(left, right)


def test_an_apostrophe_is_deleted_but_a_hyphen_splits():
    """Different punctuation, different meaning, so different handling.

    An apostrophe sits INSIDE one name (O'BRIEN), so spacing it would split the
    name in two and stop it matching the same name typed plainly. A hyphen
    genuinely joins two parts (AL-FARSI), and portals render those inconsistently
    — as one token, two, or with the hyphen dropped — so splitting is the form
    most likely to compare equal.
    """
    assert normalise_name("O'BRIEN") == "obrien"
    assert normalise_name("AL-FARSI") == "al farsi"
    assert names_match("AHMED AL-FARSI", "Ahmed Al Farsi")


def test_normalisation_sorts_tokens_so_order_cannot_cause_a_miss():
    assert normalise_name("IRINA KONOVALOVA") == normalise_name("KONOVALOVA IRINA")
    assert normalise_name("Dear") == "dear"


def test_an_unusable_name_normalises_to_empty_not_to_a_wildcard():
    assert normalise_name("") == ""
    assert normalise_name("   ") == ""
    assert normalise_name("!!!") == ""
    # And empty must never match anything, or every client would match.
    assert not names_match("", "Ahmed Khan")


# --------------------------------------------------------------------------- #
# Doubles                                                                      #
# --------------------------------------------------------------------------- #

class FakeRegistrant:
    """Minimal stand-in for waitlist.registrant.Registrant."""

    def __init__(self, client_id, first, last, route="AE-ITA"):
        self.id = client_id
        self._data = {"first_name": first, "last_name": last, "route": route}

    def get(self, key, default=None):
        return self._data.get(key, default)


def confirmation_email(name, reference="ITD125298020335", route="AE-ITA"):
    return Observation(
        email=Email(subject="Successfully Added to Waitlist", received_epoch=1756000000.0),
        match=Match(
            classification=CONFIRMATION,
            matcher_name="waitlist_confirmation",
            route=route,
            fields={"applicant_name": name, "reference": reference},
        ),
        account="ac***@example.com",
    )


@pytest.fixture
def journal_with(monkeypatch):
    """Points the reconciler at a fabricated journal and captures its writes."""
    written = []

    def _install(rows):
        from src.waitlist import journal

        monkeypatch.setattr(journal, "entries", lambda: rows)
        monkeypatch.setattr(journal, "append", lambda result: written.append(result))
        return written

    return _install


def row(client_id, status, reference=None, route="AE-ITA", combo="Dubai - Tourist"):
    return {
        "route": route, "combo": combo, "registrant_id": client_id,
        "status": status, "vfs_reference": reference, "account": "ac***@example.com",
        "started_at": "2026-08-06T11:00:00",
    }


# --------------------------------------------------------------------------- #
# The happy paths                                                              #
# --------------------------------------------------------------------------- #

def test_an_unknown_row_is_settled_by_a_confirmation_email(journal_with):
    """The whole point: a submit that lost the page, answered by VFS's own mail."""
    journal_with([row("irina", Status.UNKNOWN)])
    client = FakeRegistrant("irina", "IRINA", "KONOVALOVA")

    proposals = plan([confirmation_email("IRINA KONOVALOVA")], registrants=[client])

    assert len(proposals) == 1
    assert proposals[0].will_apply
    assert proposals[0].new_status == Status.SUCCESS
    assert proposals[0].reference == "ITD125298020335"


def test_a_pending_row_is_settled_too(journal_with):
    """'pending' means the submit was in flight — equally answerable by the email."""
    journal_with([row("irina", Status.PENDING)])
    client = FakeRegistrant("irina", "IRINA", "KONOVALOVA")

    proposals = plan([confirmation_email("IRINA KONOVALOVA")], registrants=[client])
    assert proposals[0].will_apply
    assert proposals[0].new_status == Status.SUCCESS


def test_a_successful_row_missing_its_reference_gets_it_backfilled(journal_with):
    journal_with([row("irina", Status.SUCCESS, reference=None)])
    client = FakeRegistrant("irina", "IRINA", "KONOVALOVA")

    proposals = plan([confirmation_email("IRINA KONOVALOVA")], registrants=[client])
    assert len(proposals) == 1
    assert proposals[0].reference == "ITD125298020335"
    assert "backfill" in proposals[0].action


def test_a_complete_successful_row_is_left_alone(journal_with):
    journal_with([row("irina", Status.SUCCESS, reference="ITD125298020335")])
    client = FakeRegistrant("irina", "IRINA", "KONOVALOVA")

    assert plan([confirmation_email("IRINA KONOVALOVA")], registrants=[client]) == []


def test_a_failed_row_is_not_resurrected(journal_with):
    """'failed' means nothing was submitted. A confirmation cannot apply to it."""
    journal_with([row("irina", Status.FAILED)])
    client = FakeRegistrant("irina", "IRINA", "KONOVALOVA")

    assert plan([confirmation_email("IRINA KONOVALOVA")], registrants=[client]) == []


# --------------------------------------------------------------------------- #
# The refusals — the important half                                            #
# --------------------------------------------------------------------------- #

def test_two_clients_with_the_same_name_resolve_NEITHER(journal_with):
    """The shared-account danger case. Guessing here would mark the wrong client
    registered, and they would then never be retried."""
    journal_with([row("ahmed-1", Status.UNKNOWN), row("ahmed-2", Status.UNKNOWN)])
    clients = [
        FakeRegistrant("ahmed-1", "AHMED", "KHAN"),
        FakeRegistrant("ahmed-2", "AHMED", "KHAN"),
    ]

    proposals = plan([confirmation_email("AHMED KHAN")], registrants=clients)

    assert proposals, "an ambiguous match must be reported, not silently dropped"
    assert not any(p.will_apply for p in proposals)
    assert all("ambiguous" in p.blocked_reason for p in proposals)


def test_a_confirmation_with_no_name_settles_nothing(journal_with):
    """'Dear Applicant' — nothing to identify the client by."""
    journal_with([row("irina", Status.UNKNOWN)])
    client = FakeRegistrant("irina", "IRINA", "KONOVALOVA")

    observation = confirmation_email(None)
    assert plan([observation], registrants=[client]) == []


def test_a_confirmation_for_a_different_route_is_ignored(journal_with):
    journal_with([row("irina", Status.UNKNOWN, route="AE-ITA")])
    client = FakeRegistrant("irina", "IRINA", "KONOVALOVA", route="AE-ITA")

    observation = confirmation_email("IRINA KONOVALOVA", route="AE-CHE")
    assert plan([observation], registrants=[client]) == []


def test_a_name_that_matches_nobody_settles_nothing(journal_with):
    journal_with([row("irina", Status.UNKNOWN)])
    client = FakeRegistrant("irina", "IRINA", "KONOVALOVA")

    assert plan([confirmation_email("SOMEONE ELSE")], registrants=[client]) == []


def test_an_invitation_email_never_reconciles_anything(journal_with):
    """Only confirmations carry proof of registration. An invitation says the
    opposite — that a registration already exists and is now bookable."""
    journal_with([row("irina", Status.UNKNOWN)])
    client = FakeRegistrant("irina", "IRINA", "KONOVALOVA")

    invitation = Observation(
        email=Email(subject="Slots available for booking"),
        match=Match(classification=INVITATION, route="AE-ITA",
                    fields={"applicant_name": "IRINA KONOVALOVA"}),
    )
    assert plan([invitation], registrants=[client]) == []


def test_a_confirmation_without_a_reference_still_settles_the_row(journal_with):
    """The reference is a bonus. The email's EXISTENCE is the proof."""
    journal_with([row("irina", Status.UNKNOWN)])
    client = FakeRegistrant("irina", "IRINA", "KONOVALOVA")

    proposals = plan(
        [confirmation_email("IRINA KONOVALOVA", reference=None)], registrants=[client]
    )
    assert proposals[0].will_apply
    assert proposals[0].reference is None
    assert "no reference" in proposals[0].action


# --------------------------------------------------------------------------- #
# Applying                                                                     #
# --------------------------------------------------------------------------- #

def test_apply_appends_rather_than_rewriting(journal_with):
    """Append-only: the original 'unknown' stays, so the audit trail shows that
    a later email settled it."""
    written = journal_with([row("irina", Status.UNKNOWN)])
    client = FakeRegistrant("irina", "IRINA", "KONOVALOVA")

    applied = apply(plan([confirmation_email("IRINA KONOVALOVA")], registrants=[client]))

    assert applied == 1
    assert len(written) == 1
    assert written[0].status == Status.SUCCESS
    assert written[0].vfs_reference == "ITD125298020335"
    assert "reconciled" in written[0].reason


def test_apply_writes_nothing_for_a_blocked_proposal(journal_with):
    written = journal_with([])
    blocked = Proposal(
        row={}, registrant_id="x", route="AE-ITA", combo="c",
        blocked_reason="ambiguous",
    )
    assert apply([blocked]) == 0
    assert written == []


def test_client_name_prefers_an_explicit_full_name():
    class WithFull(FakeRegistrant):
        def __init__(self):
            super().__init__("x", "IRINA", "KONOVALOVA")
            self._data["full_name"] = "IRINA M KONOVALOVA"

    assert client_name(WithFull()) == "IRINA M KONOVALOVA"
    assert client_name(FakeRegistrant("x", "IRINA", "KONOVALOVA")) == "IRINA KONOVALOVA"
