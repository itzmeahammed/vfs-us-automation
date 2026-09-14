"""The form email must match the account, or the invitation is never seen.

VFS sends the "Slots available for booking" invitation to the email TYPED INTO
THE WAITLIST FORM, not to the account the registration was made under. The inbox
watcher reads ACCOUNT mailboxes. When those differ, the invitation lands
somewhere nothing is watching and the 36-48h booking window closes unnoticed.

The failure is entirely silent — no error, no missing row, nothing to notice.
That is what makes it worth a validation rule rather than a docs note.
"""

import pytest

from src.waitlist.validate import ERROR, WARNING, check_invitation_email


def check(email=None, account=None, **extra):
    data = dict(extra)
    if email is not None:
        data["email"] = email
    if account is not None:
        data["account"] = account
    return check_invitation_email(data)


# --------------------------------------------------------------------------- #
# The case this exists for                                                     #
# --------------------------------------------------------------------------- #

def test_a_differing_email_is_flagged():
    problems = check(email="client@gmail.com", account="acct@travnook.com")
    assert len(problems) == 1
    assert problems[0].field == "email"


def test_the_message_names_BOTH_addresses():
    """An operator has to see which is which to decide what to change."""
    problems = check(email="client@gmail.com", account="acct@travnook.com")
    assert "client@gmail.com" in problems[0].message
    assert "acct@travnook.com" in problems[0].message


def test_the_message_explains_the_consequence_not_just_the_mismatch():
    """'These differ' is not actionable; 'the invitation goes somewhere nothing
    is watching' is."""
    text = check(email="a@x.com", account="b@y.com")[0].message
    assert "invitation" in text.lower()
    assert "watching" in text.lower()


def test_the_hint_gives_the_fix_and_the_deadline():
    hint = check(email="a@x.com", account="b@y.com")[0].hint
    assert "b@y.com" in hint          # what to set it to
    assert "36" in hint or "48" in hint   # why it is urgent


# --------------------------------------------------------------------------- #
# It is a WARNING, deliberately                                                #
# --------------------------------------------------------------------------- #

def test_it_warns_rather_than_erroring():
    """Non-blocking on purpose. 4 of the 5 clients in this repo predate the rule;
    erroring would break working registrations to enforce a preference."""
    problems = check(email="a@x.com", account="b@y.com")
    assert problems[0].severity == WARNING
    assert problems[0].severity != ERROR


def test_the_clients_data_is_never_rewritten():
    """Rejected alternative: silently setting email = account. It changes data
    the client supplied, and the person who typed it deserves to be told rather
    than overruled. The check is pure — it returns findings and mutates nothing."""
    data = {"email": "a@x.com", "account": "b@y.com"}
    check_invitation_email(data)
    assert data == {"email": "a@x.com", "account": "b@y.com"}


# --------------------------------------------------------------------------- #
# When it must stay quiet                                                      #
# --------------------------------------------------------------------------- #

def test_matching_addresses_pass_silently():
    assert check(email="same@travnook.com", account="same@travnook.com") == []


def test_the_comparison_ignores_case_and_padding():
    """Hand-edited client files; a stray space or capital is not a real
    mismatch and a false warning trains people to ignore real ones."""
    assert check(email="  Same@Travnook.com ", account="same@travnook.com") == []


def test_no_account_means_nothing_to_compare():
    """An empty account means the shared [waitlist] account is used, and the
    client file cannot know which that is."""
    assert check(email="a@x.com", account="") == []
    assert check(email="a@x.com") == []


def test_no_email_means_nothing_to_compare():
    """Some routes take applicant details from an uploaded document instead of
    typed fields, so a client may legitimately have no email."""
    assert check(account="b@y.com") == []
    assert check(email="", account="b@y.com") == []


def test_an_empty_payload_is_quiet():
    assert check_invitation_email({}) == []


# --------------------------------------------------------------------------- #
# Wired into the pre-flight                                                    #
# --------------------------------------------------------------------------- #

def test_precheck_includes_the_warning():
    from src.utils.config_reader import initialize_config
    from src.waitlist.validate import precheck_client

    # precheck reaches route readiness, which reads the INI. Idempotent.
    initialize_config()

    problems = precheck_client("someone", {
        "route": "AE-CHE", "combos": ["Dubai - SCHENGEN"],
        "email": "client@gmail.com", "account": "acct@travnook.com",
    })
    assert any(p.field == "email" and p.severity == WARNING for p in problems)


def test_the_warning_survives_an_unusable_route():
    """It is independent of the route, so a client with a bad route still gets
    told their invitation would go somewhere unwatched."""
    from src.waitlist.validate import precheck_client

    problems = precheck_client("someone", {
        "route": "NOT-A-ROUTE",
        "email": "client@gmail.com", "account": "acct@travnook.com",
    })
    assert any(p.field == "email" for p in problems)


# --------------------------------------------------------------------------- #
# The real client files                                                        #
# --------------------------------------------------------------------------- #

def test_the_real_clients_are_checked_without_crashing():
    """Guards against a shape in production data the check does not expect."""
    from src.waitlist import registrant as registrant_mod

    for person in registrant_mod.load_all(skip_invalid=True):
        data = dict(person.as_context())
        data["account"] = person.account
        for problem in check_invitation_email(data):
            assert problem.severity == WARNING
