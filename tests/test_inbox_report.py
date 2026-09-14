"""The digest: what a human sees, and what must never reach Telegram.

Two things are actually load-bearing here.

REDACTION. Bodies, greetings and extracted fields all carry client names, and
the digest goes to an external service. A leak here is a PII incident, not a
formatting bug — so the redaction path is tested including the case where
redaction itself fails.

QUIET WHEN THERE IS NOTHING. The watcher polls every few minutes. A message per
pass would train whoever reads the channel to ignore it, and the one message
that matters would be ignored with the rest.
"""

import pytest

from src.inbox.matcher import CONFIRMATION, INVITATION, OTHER, Email, Match
from src.inbox.report import build_digest, report
from src.inbox.watcher import Observation, PassResult


def observation(classification=INVITATION, route="AE-ITA", fields=None,
                account="ac***@example.com", subject="s", received=1756000000.0,
                hours=48):
    return Observation(
        email=Email(subject=subject, received_epoch=received),
        match=Match(
            classification=classification, matcher_name="m", route=route,
            fields=fields or {}, validity_hours=hours,
        ),
        account=account,
    )


@pytest.fixture(autouse=True)
def no_real_redaction(monkeypatch):
    """Redaction is exercised explicitly below; elsewhere it passes text through
    so assertions read against the real content."""
    from src.waitlist import redaction

    monkeypatch.setattr(redaction, "scrub", lambda t: t)


# --------------------------------------------------------------------------- #
# Staying quiet                                                                #
# --------------------------------------------------------------------------- #

def test_an_empty_pass_produces_no_digest():
    """'' not 'nothing found' — the watcher polls constantly and a message per
    pass would make the channel worthless."""
    assert build_digest(PassResult(mailboxes_checked=3, messages_seen=40)) == ""


def test_a_pass_with_findings_produces_a_digest():
    result = PassResult(observations=[observation()], mailboxes_checked=1,
                        messages_seen=5)
    digest = build_digest(result)
    assert "INVITATIONS (1)" in digest
    assert "1 mailbox(es)" in digest
    assert "5 message(s) examined" in digest


# --------------------------------------------------------------------------- #
# Sections                                                                     #
# --------------------------------------------------------------------------- #

def test_invitations_confirmations_and_unknowns_are_separated():
    result = PassResult(observations=[
        observation(INVITATION),
        observation(CONFIRMATION),
        observation(OTHER, subject="Your VFS receipt"),
    ])
    digest = build_digest(result)
    assert "INVITATIONS (1)" in digest
    assert "CONFIRMATIONS (1)" in digest
    assert "UNRECOGNISED VFS MAIL (1)" in digest


def test_unrecognised_mail_explains_why_it_is_being_shown():
    """It is the most useful line in the digest during the observational phase:
    it is how a new email type gets discovered."""
    digest = build_digest(PassResult(observations=[observation(OTHER)]))
    assert "config/inbox" in digest


def test_failed_mailboxes_are_reported():
    """Silence about an unread mailbox would read as 'no mail', which is exactly
    the failure this package exists to prevent."""
    digest = build_digest(PassResult(mailboxes_failed=["ac***@example.com"]))
    assert "MAILBOXES THAT FAILED (1)" in digest
    assert "ac***@example.com" in digest


def test_extracted_fields_appear_in_the_line():
    digest = build_digest(PassResult(observations=[
        observation(fields={"applicant_name": "IRINA KONOVALOVA",
                            "reference": "ITD125298020335"})
    ]))
    assert "IRINA KONOVALOVA" in digest
    assert "ITD125298020335" in digest


def test_empty_extractions_are_omitted_rather_than_shown_as_none():
    digest = build_digest(PassResult(observations=[
        observation(fields={"applicant_name": None, "category": "Tourist"})
    ]))
    assert "Tourist" in digest
    assert "None" not in digest


# --------------------------------------------------------------------------- #
# The 48-hour clock                                                            #
# --------------------------------------------------------------------------- #

def test_an_invitation_shows_the_time_remaining(monkeypatch):
    import src.inbox.watcher as watcher_mod

    received = 1756000000.0
    monkeypatch.setattr(watcher_mod.time, "time", lambda: received + 3600)
    digest = build_digest(PassResult(observations=[observation(received=received)]))
    assert "47h left" in digest


def test_a_lapsed_invitation_is_marked_expired(monkeypatch):
    """A window that has closed must be visibly different from one with hours
    left; '-3h left' would be read as a live deadline at a glance."""
    import src.inbox.watcher as watcher_mod

    received = 1756000000.0
    monkeypatch.setattr(watcher_mod.time, "time", lambda: received + 51 * 3600)
    digest = build_digest(PassResult(observations=[observation(received=received)]))
    assert "EXPIRED" in digest


def test_a_confirmation_shows_no_deadline():
    digest = build_digest(PassResult(observations=[observation(CONFIRMATION)]))
    assert "left" not in digest and "EXPIRED" not in digest


# --------------------------------------------------------------------------- #
# Redaction — a leak here is a PII incident                                    #
# --------------------------------------------------------------------------- #

def test_extracted_values_pass_through_redaction(monkeypatch):
    from src.waitlist import redaction

    monkeypatch.setattr(
        redaction, "scrub",
        lambda t: t.replace("IRINA KONOVALOVA", "[REDACTED]"),
    )
    digest = build_digest(PassResult(observations=[
        observation(fields={"applicant_name": "IRINA KONOVALOVA"})
    ]))
    assert "IRINA KONOVALOVA" not in digest
    assert "[REDACTED]" in digest


def test_unmatched_subjects_pass_through_redaction(monkeypatch):
    from src.waitlist import redaction

    monkeypatch.setattr(redaction, "scrub", lambda t: t.replace("SECRET", "[X]"))
    digest = build_digest(PassResult(observations=[
        observation(OTHER, subject="Receipt for SECRET")
    ]))
    assert "SECRET" not in digest


def test_a_redaction_failure_withholds_the_value_rather_than_leaking_it(monkeypatch):
    """Fail closed. If redaction cannot run, the raw value must NOT be printed —
    a broken filter is not a licence to publish a client's name."""
    from src.waitlist import redaction

    def boom(text):
        raise RuntimeError("redaction is broken")

    monkeypatch.setattr(redaction, "scrub", boom)
    digest = build_digest(PassResult(observations=[
        observation(fields={"applicant_name": "IRINA KONOVALOVA"})
    ]))
    assert "IRINA KONOVALOVA" not in digest
    assert "[redacted]" in digest


def test_the_message_body_never_reaches_the_digest():
    """Only subjects and extracted fields are rendered. Bodies are full of PII
    and there is no reason to put one in a chat message."""
    result = PassResult(observations=[Observation(
        email=Email(subject="Slots available",
                    body="Dear IRINA KONOVALOVA, passport A1234567"),
        match=Match(classification=INVITATION, route="AE-ITA"),
    )])
    digest = build_digest(result)
    assert "A1234567" not in digest


# --------------------------------------------------------------------------- #
# Telegram                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def fake_telegram(monkeypatch):
    from src.utils import telegram

    sent = []
    monkeypatch.setattr(telegram, "is_error_configured", lambda: True)
    monkeypatch.setattr(telegram, "send_error", lambda text: sent.append(text) or True)
    return sent


def test_an_invitation_is_pushed_to_telegram(fake_telegram):
    report(PassResult(observations=[observation(INVITATION)], mailboxes_checked=1))
    assert len(fake_telegram) == 1
    assert "INVITATIONS" in fake_telegram[0]


def test_a_failed_mailbox_is_pushed_to_telegram(fake_telegram):
    report(PassResult(mailboxes_failed=["ac***@example.com"], mailboxes_checked=1))
    assert len(fake_telegram) == 1


def test_confirmations_alone_do_not_push(fake_telegram):
    """Only an invitation is time-critical. Everything else sits in the log for
    whoever is reviewing the matchers."""
    report(PassResult(observations=[observation(CONFIRMATION)], mailboxes_checked=1))
    assert fake_telegram == []


def test_a_quiet_pass_does_not_push(fake_telegram):
    report(PassResult(mailboxes_checked=2, messages_seen=10))
    assert fake_telegram == []


def test_a_telegram_failure_does_not_break_the_pass(monkeypatch):
    """Reporting is best-effort; a chat outage must not end the watcher."""
    from src.utils import telegram

    monkeypatch.setattr(telegram, "is_error_configured", lambda: True)

    def boom(text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(telegram, "send_error", boom)
    report(PassResult(observations=[observation(INVITATION)], mailboxes_checked=1))


def test_nothing_is_sent_when_telegram_is_not_configured(monkeypatch):
    from src.utils import telegram

    sent = []
    monkeypatch.setattr(telegram, "is_error_configured", lambda: False)
    monkeypatch.setattr(telegram, "send_error", lambda t: sent.append(t))
    report(PassResult(observations=[observation(INVITATION)], mailboxes_checked=1))
    assert sent == []


def test_the_digest_is_always_logged_even_when_not_pushed(caplog):
    import logging

    with caplog.at_level(logging.INFO):
        report(PassResult(observations=[observation(CONFIRMATION)], mailboxes_checked=1))
    assert any("CONFIRMATIONS" in r.message for r in caplog.records)
