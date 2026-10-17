"""Two passwords, one address: the VFS portal and the mailbox.

    ═════════════════ WHY THIS SPLIT EXISTS ═════════════════

Every VFS account email is ALSO a real mailbox, and the bot signs in to both:
the portal in a browser, and Roundcube over IMAP to read the emailed one-time
password on `otp: true` routes. Those were one password until the webmail
password was rotated on 2026-10-16.

The failure that caused is the reason these tests are worth having: VFS login
kept working, every OTP route broke, and the symptom was "no OTP email
arrived" — which sends you to look at the mail matcher, the clock skew, the
IMAP search, anything but the password. Nothing in the system said the two
credentials had diverged, because nothing knew they were two credentials.

THE RULE, asserted below in every direction: BOTH passwords are REQUIRED and
`email_password` NEVER falls back to `password`. A fallback would make a
forgotten config line indistinguishable from a deliberate one and silently
resume using the wrong credential — reinstating the bug at the moment nobody
is looking for it. Refusing is louder and cheaper.
"""

import configparser

import pytest

from src.utils import credentials
from src.utils.credentials import MissingMailboxPassword
from src.waitlist.accounts import Account


# --------------------------------------------------------------------------- #
# Account — the waitlist pool                                                  #
# --------------------------------------------------------------------------- #

def test_the_mailbox_password_does_not_fall_back_to_the_portal_one():
    """*** THE CENTRAL RULE. *** Unset means unset, not 'use the other one'."""
    acc = Account("a@example.com", "portal-pw", "test")
    assert acc.password == "portal-pw"
    assert acc.email_password == ""
    assert acc.can_read_mailbox is False


def test_a_configured_mailbox_password_is_kept_separate():
    acc = Account("a@example.com", "portal-pw", "test",
                  email_password="mailbox-pw")
    assert acc.password == "portal-pw"
    assert acc.email_password == "mailbox-pw"
    assert acc.can_read_mailbox is True


def test_a_blank_mailbox_password_reads_as_absent():
    """'' and None mean the same thing: this account cannot be read. What must
    never happen is an empty string reaching imap.login(), whose error says
    'authentication failed' and reads as a wrong password."""
    for blank in ("", None):
        acc = Account("a@example.com", "portal-pw", "test",
                      email_password=blank)
        assert acc.email_password == ""
        assert acc.can_read_mailbox is False


def test_the_portal_password_is_never_silently_replaced():
    """The split must not work in reverse: a mailbox password must not end up
    typed into the VFS login form."""
    acc = Account("a@example.com", "portal-pw", "test",
                  email_password="mailbox-pw")
    assert acc.password == "portal-pw"
    assert "mailbox-pw" not in repr(acc)


def test_the_password_is_still_not_in_the_repr():
    acc = Account("a@example.com", "portal-pw", "test",
                  email_password="mailbox-pw")
    assert "portal-pw" not in repr(acc)


# --------------------------------------------------------------------------- #
# credentials.py — the slot-check pool                                         #
# --------------------------------------------------------------------------- #

def _write_pool(tmp_path, monkeypatch, body: str):
    path = tmp_path / "credentials.local.ini"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(credentials, "CREDENTIALS_FILE", str(path))
    return path


SPLIT = """
[cred1]
email = split@example.com
password = portal-pw
email_password = mailbox-pw
routes = AE-ITA
"""

#: Exactly what every account looked like before 2026-10-16.
NO_MAILBOX = """
[cred9]
email = legacy@example.com
password = one-pw
routes = AE-GRC
"""

SPLIT_ONLY = """
[cred1]
email = a@example.com
password = portal-a
email_password = mailbox-a

[cred2]
email = b@example.com
password = portal-b
email_password = mailbox-b
"""


def test_a_split_account_returns_each_password_separately(tmp_path,
                                                          monkeypatch):
    _write_pool(tmp_path, monkeypatch, SPLIT)
    assert credentials.password_for("split@example.com") == "portal-pw"
    assert credentials.email_password_for("split@example.com") == "mailbox-pw"


def test_an_account_without_the_key_raises_rather_than_falling_back(
        tmp_path, monkeypatch):
    """*** THE TEST THAT DEFINES THE DESIGN. ***

    Every account looked like this before 2026-10-16. Returning 'one-pw' here
    is the tempting, wrong answer: it works right up until the two passwords
    diverge, then fails as "no OTP email arrived".
    """
    _write_pool(tmp_path, monkeypatch, NO_MAILBOX)
    with pytest.raises(MissingMailboxPassword, match="email_password"):
        credentials.email_password_for("legacy@example.com")

    # And the knock-on effect, asserted so it is a decision rather than a
    # surprise: an account dropped from the pool has no portal password
    # either, because password_for() reads the same pool. "Half-configured"
    # is not a usable state for an account, so it resolves to nothing at all.
    assert credentials.password_for("legacy@example.com") == ""


def test_the_error_names_the_account_the_section_and_the_fix(tmp_path,
                                                             monkeypatch):
    """An error on the login path is read by someone under time pressure."""
    _write_pool(tmp_path, monkeypatch, NO_MAILBOX)
    with pytest.raises(MissingMailboxPassword) as caught:
        credentials.email_password_for("legacy@example.com")
    message = str(caught.value)
    assert "cred9" in message
    assert "email_password" in message
    assert "credentials check" in message


def test_the_error_never_quotes_a_password(tmp_path, monkeypatch):
    _write_pool(tmp_path, monkeypatch, NO_MAILBOX)
    with pytest.raises(MissingMailboxPassword) as caught:
        credentials.email_password_for("legacy@example.com")
    assert "one-pw" not in str(caught.value)


def test_required_false_reports_instead_of_raising(tmp_path, monkeypatch):
    """For diagnostics that want to list problems, not stop at the first."""
    _write_pool(tmp_path, monkeypatch, NO_MAILBOX)
    assert credentials.email_password_for(
        "legacy@example.com", required=False) == ""


def test_an_incomplete_account_is_dropped_from_the_pool(tmp_path, monkeypatch):
    """Dropped, not fatal — one bad section must not take every route down.
    The account simply stops being rotated into."""
    _write_pool(tmp_path, monkeypatch, SPLIT + NO_MAILBOX)
    emails = [e for e, _pw, _r in credentials._load_pool()]
    assert "split@example.com" in emails
    assert "legacy@example.com" not in emails


def test_check_reports_the_missing_key(tmp_path, monkeypatch):
    _write_pool(tmp_path, monkeypatch, NO_MAILBOX)
    problems = credentials.check()
    assert any("email_password" in p for p in problems)


def test_check_notes_an_account_still_sharing_one_password(tmp_path,
                                                           monkeypatch):
    """Legal, but it is the account that breaks at the next rotation — so it
    is surfaced rather than left to be discovered."""
    _write_pool(tmp_path, monkeypatch, """
[cred1]
email = same@example.com
password = one-pw
email_password = one-pw
""")
    problems = credentials.check()
    assert any("same value" in p for p in problems)
    # An advisory, not a failure: the account still loads.
    assert [e for e, _p, _r in credentials._load_pool()] == ["same@example.com"]


def test_check_is_silent_when_everything_is_split(tmp_path, monkeypatch):
    _write_pool(tmp_path, monkeypatch, SPLIT_ONLY)
    assert credentials.check() == []


def test_lookup_is_case_insensitive(tmp_path, monkeypatch):
    _write_pool(tmp_path, monkeypatch, SPLIT)
    assert credentials.email_password_for("SPLIT@EXAMPLE.COM") == "mailbox-pw"


def test_an_unknown_email_raises_rather_than_guessing(tmp_path, monkeypatch):
    """Returning SOME other account's password would be far worse than
    refusing, and returning '' would be handed to imap.login()."""
    _write_pool(tmp_path, monkeypatch, SPLIT)
    for unknown in ("nobody@example.com", ""):
        with pytest.raises(MissingMailboxPassword):
            credentials.email_password_for(unknown)
    assert credentials.email_password_for(
        "nobody@example.com", required=False) == ""


def test_the_pool_tuple_shape_is_unchanged(tmp_path, monkeypatch):
    """*** DELIBERATE. *** _load_pool() still yields 3-tuples. Widening it
    would break every caller that unpacks (email, password, routes) — e.g.
    analytics.py — for a change none of them care about."""
    _write_pool(tmp_path, monkeypatch, SPLIT)
    pool = credentials._load_pool()
    assert all(len(entry) == 3 for entry in pool)
    emails = [e for e, _pw, _r in pool]
    assert "split@example.com" in emails
    # And the tuple's password is the PORTAL one, not the mailbox one.
    for email, pw, _routes in pool:
        if email == "split@example.com":
            assert pw == "portal-pw"


def test_an_empty_email_password_key_is_treated_as_missing(tmp_path,
                                                           monkeypatch):
    """`email_password =` with nothing after it is a half-finished edit — the
    shape credentials_editor.py writes for a row you have not filled in yet.
    It must refuse, not log in with an empty password."""
    _write_pool(tmp_path, monkeypatch, """
[cred1]
email = blank@example.com
password = portal-pw
email_password =
""")
    with pytest.raises(MissingMailboxPassword):
        credentials.email_password_for("blank@example.com")


def test_a_missing_file_raises_but_only_when_required(tmp_path, monkeypatch):
    """No file at all is still 'this mailbox password is unknown'."""
    monkeypatch.setattr(credentials, "CREDENTIALS_FILE",
                        str(tmp_path / "nope.ini"))
    with pytest.raises(MissingMailboxPassword):
        credentials.email_password_for("a@example.com")
    assert credentials.email_password_for("a@example.com",
                                          required=False) == ""
