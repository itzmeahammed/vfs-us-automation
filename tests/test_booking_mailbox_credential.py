"""The MAILBOX password an agent supplies so the bot can read a mid-booking OTP.

France stops on an emailed one-time password between Your Details and the slot
picker, and the bot reads that code from the booking account's own inbox. So an
agent must send `account_email_password` — the Roundcube/IMAP password — which
is NOT the VFS portal password in `account_password`. The two were one value
until 2026-10-16 and the split exists because assuming they still match is a
bug; nothing here may reinstate a fallback between them.

The guards come first because they are the part that fails silently. The field
is read by accounts.resolve and has been for some time, but on 2026-10-29 it was
in NONE of the three places a credential has to be excluded from:

  * Registrant._META_KEYS — so {{account_email_password}} resolved, and a route
    config could have typed a mailbox password into a VFS form field;
  * BookingRequest.public_view() — which stripped only account_password;
  * the API's _SECRET_KEYS — so any endpoint would have returned it.

Nothing had leaked, because nothing set the field. All three would have gone
live together the moment agents began filling it in through the web app, which
is what this change is for.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "a" * 64)

from src.booking import requests as store                      # noqa: E402
from src.waitlist import validate                               # noqa: E402
from src.waitlist.registrant import Registrant                  # noqa: E402

#: Any value that would be obvious in a leak.
MAILBOX_PW = "mailbox-secret-value"
PORTAL_PW = "portal-secret-value"

OTP_ROUTE = "AE-FRA"        # has an "email_otp" step
PLAIN_ROUTE = "AE-NOR"      # does not


def _data(**over):
    data = {
        "route": OTP_ROUTE, "combo": "Abu Dhabi - Short Stay - Business",
        "date_from": "2026-11-01", "date_to": "2026-11-30",
        "first_name": "A", "last_name": "B",
        "account": "agent@travnook.com",
        "account_password": PORTAL_PW,
        "account_email_password": MAILBOX_PW,
    }
    data.update(over)
    return data


# ── the three guards ─────────────────────────────────────────────────────────

def test_it_can_never_reach_a_form_field():
    """*** THE ONE THAT WOULD TYPE A PASSWORD INTO VFS. ***

    Every field value in a route config is a {{placeholder}} resolved against
    this mapping. A credential in it is one typo away from being filled into
    whatever input the config names.
    """
    person = Registrant("x", {"route": OTP_ROUTE, "combos": ["c"],
                              "first_name": "A",
                              "account_email_password": MAILBOX_PW})

    context = person.as_context()

    assert "account_email_password" not in context
    assert MAILBOX_PW not in str(context.values())


def test_it_is_never_returned_by_the_client_record(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "REQUEST_DIR", str(tmp_path))
    store.create("c1", _data())

    view = store.get("c1").public_view()

    assert "account_email_password" not in view
    assert MAILBOX_PW not in str(view)
    assert view["has_account_email_password"] is True, (
        "a form still has to show whether one is on file")


def test_the_portal_password_is_still_guarded_too(tmp_path, monkeypatch):
    """Widening the exclusion to a tuple must not drop the original."""
    monkeypatch.setattr(store, "REQUEST_DIR", str(tmp_path))
    store.create("c1", _data())

    view = store.get("c1").public_view()

    assert "account_password" not in view
    assert PORTAL_PW not in str(view)
    assert view["has_account_password"] is True


def test_it_is_never_returned_by_the_api():
    from src.api.modules.clients import waitlist as api_clients

    assert "account_email_password" in api_clients._SECRET_KEYS


def test_an_absent_password_reports_false(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "REQUEST_DIR", str(tmp_path))
    store.create("c1", _data(account_email_password=None))

    assert store.get("c1").public_view()["has_account_email_password"] is False


# ── asked for only where it is needed ────────────────────────────────────────

def test_the_otp_route_asks_for_all_three_account_fields():
    names = [f["name"] for f in validate.required_fields_live(OTP_ROUTE)]

    assert "account" in names
    assert "account_password" in names
    assert "account_email_password" in names


def test_a_route_without_an_otp_step_asks_for_none_of_them():
    """Two password boxes on a form with no use for them is how agents learn to
    put the wrong value in one."""
    names = [f["name"] for f in validate.required_fields_live(PLAIN_ROUTE)]

    assert not [n for n in names if "account" in n]


def test_the_form_says_which_password_is_which():
    """An agent cannot be expected to know Roundcube from the portal unless the
    form tells them."""
    fields = {f["name"]: f for f in validate.required_fields_live(OTP_ROUTE)}

    mailbox = fields["account_email_password"]
    assert mailbox["kind"] == "password"
    assert "NOT the portal password" in mailbox["notes"]
    assert "Roundcube" in mailbox["label"] or "Roundcube" in mailbox["notes"]


def test_route_needs_mailbox_is_read_from_the_steps():
    """Derived from the step list, not a separate flag that adding the step
    could forget to set."""
    assert store.route_needs_mailbox(OTP_ROUTE) is True
    assert store.route_needs_mailbox(PLAIN_ROUTE) is False
    assert store.route_needs_mailbox("NOPE-XXX") is False


# ── the rules, enforced at the form ──────────────────────────────────────────

def _fields_with_problems(data):
    return {p.field for p in store.precheck("some-client-ae-fra-1", data)}


def test_an_account_off_the_domain_is_refused():
    problems = _fields_with_problems(_data(account="agent@gmail.com"))

    assert "account" in problems


def test_an_agents_own_account_on_the_domain_is_accepted():
    """They will use their own accounts, and a coincidence with a configured
    one is harmless — the pinned branch never consults the pool."""
    assert "account" not in _fields_with_problems(
        _data(account="athul@travnook.com"))


def test_the_domain_check_ignores_case():
    assert "account" not in _fields_with_problems(
        _data(account="AGENT@TRAVNOOK.COM"))


def test_a_lookalike_domain_does_not_pass():
    """Endswith on the bare domain would accept 'nottravnook.com'."""
    problems = _fields_with_problems(_data(account="agent@nottravnook.com"))

    assert "account" in problems


def test_a_missing_mailbox_password_is_refused_on_an_otp_route():
    problems = _fields_with_problems(_data(account_email_password=None))

    assert "account_email_password" in problems


def test_the_mailbox_password_is_not_demanded_off_an_otp_route():
    """AE-NOR never reads a mailbox, so requiring one would block every
    booking on it."""
    data = _data(route=PLAIN_ROUTE,
                 combo="Norway Visa Application Center - Dubai",
                 account="agent@gmail.com", account_email_password=None)

    problems = _fields_with_problems(data)

    assert "account_email_password" not in problems
    assert "account" not in problems, "the domain rule is for OTP routes only"


def test_the_refusal_names_which_password_it_wants():
    """The reader is a sales agent at a form, not an operator at a terminal."""
    problems = store.precheck("some-client-ae-fra-1",
                              _data(account_email_password=None))
    said = next(p for p in problems if p.field == "account_email_password")

    assert "MAILBOX" in said.message or "mailbox" in said.message
    assert "NOT the VFS portal password" in said.hint


def test_the_domain_rule_can_be_turned_off(monkeypatch):
    """A deployment that reads its OTP mail somewhere else is not ours to
    break."""
    monkeypatch.setattr(store, "booking_account_domain", lambda: "")

    assert "account" not in _fields_with_problems(
        _data(account="agent@elsewhere.com"))


# ── it actually reaches the thing that uses it ───────────────────────────────

def test_the_resolver_receives_it(tmp_path, monkeypatch):
    """*** THE PATH THAT WAS DEAD. ***

    accounts.resolve() reads this with getattr(registrant, ...), and nothing
    defined the attribute, so it read '' for every client on every route. The
    guards above would then have been perfectly correct about a value the bot
    could never use, and otp_step would have reported a missing mailbox
    password for a client who had supplied one.
    """
    from src.waitlist import accounts

    monkeypatch.setattr(store, "REQUEST_DIR", str(tmp_path))
    store.create("c1", _data())

    resolved = accounts.resolve(store.get("c1").as_registrant())

    assert resolved.email_password == MAILBOX_PW
    assert resolved.can_read_mailbox is True


def test_the_mailbox_password_never_substitutes_for_the_portal_one(
        tmp_path, monkeypatch):
    """The 2026-10-16 split. A client with a portal password and NO mailbox
    password must resolve to '' — never to the portal password."""
    from src.waitlist import accounts

    monkeypatch.setattr(store, "REQUEST_DIR", str(tmp_path))
    store.create("c1", _data(account_email_password=None))

    resolved = accounts.resolve(store.get("c1").as_registrant())

    assert resolved.password == PORTAL_PW
    assert resolved.email_password == ""
    assert resolved.can_read_mailbox is False


# ── the mailbox password, on every route ─────────────────────────────────────
#
# Registering is half the job. VFS announces a bookable slot by EMAIL, the
# inbox watcher reads it over IMAP from the account the client registered
# under, and invite_booking turns it into a booking. So the field was scoped to
# routes with an `email_otp` step - France - because that is where a MID-booking
# code is read. Too narrow: the INVITATION arrives on every route. On
# 2026-10-29 an AE-NLD client was registered under a mailbox nothing could
# open, and no form had ever asked for its password.


def test_every_waitlist_route_offers_the_field():
    """*** THE WHOLE POINT: NOT JUST THE OTP ROUTE. ***"""
    for route in (OTP_ROUTE, PLAIN_ROUTE, "AE-CHE", "AE-NLD", "AE-CZE"):
        names = [f["name"] for f in validate.required_fields(route)]
        assert "account_email_password" in names, route


def test_the_form_field_says_which_password_it_wants():
    """An agent cannot be expected to know Roundcube from the portal unless the
    form says so."""
    field = next(f for f in validate.required_fields(PLAIN_ROUTE)
                 if f["name"] == "account_email_password")

    assert field["kind"] == "password"
    assert "NOT the portal password" in field["notes"]
    assert "Roundcube" in field["label"] or "Roundcube" in field["notes"]
    assert "invitation" in field["notes"]


def test_the_form_does_not_demand_it_before_an_account_is_chosen():
    """The form renders before anyone has picked an account, and a company
    account needs nothing typed. The demand belongs at submit, not at render -
    marking it required here would make an agent retype a value the bot already
    holds, which is how the wrong value gets typed."""
    field = next(f for f in validate.required_fields(PLAIN_ROUTE)
                 if f["name"] == "account_email_password")

    assert field["required"] is False


def test_the_box_is_offered_even_when_the_pool_holds_a_password(monkeypatch):
    """It briefly skipped the field for an address the pool knows.

    That hid the one field the whole invitation chain hangs on, and made the
    form depend on server-side credential state an agent cannot see - the same
    route rendering differently per account, for a reason the page cannot
    explain. Always offered; whether it HAD to be filled is settled at submit.
    """
    monkeypatch.setattr("src.utils.credentials.email_password_for",
                        lambda email, required=False: "pool-secret")

    field = validate.mailbox_password_field(PLAIN_ROUTE)

    assert field and field["name"] == "account_email_password"


def test_the_box_is_offered_when_the_pool_knows_nothing(monkeypatch):
    monkeypatch.setattr("src.utils.credentials.email_password_for",
                        lambda email, required=False: "")

    field = validate.mailbox_password_field(PLAIN_ROUTE)

    assert field and field["name"] == "account_email_password"


def test_the_form_never_varies_by_account(monkeypatch):
    """One route, one form. A field that appears for some agents and not others
    is a support call nobody can answer from the page."""
    monkeypatch.setattr("src.utils.credentials.email_password_for",
                        lambda email, required=False: "pool-secret")
    known = [f["name"] for f in validate.required_fields(PLAIN_ROUTE)]
    monkeypatch.setattr("src.utils.credentials.email_password_for",
                        lambda email, required=False: "")
    unknown = [f["name"] for f in validate.required_fields(PLAIN_ROUTE)]

    assert known == unknown
    assert "account_email_password" in known


# ── enforced at submit, where the account is known ───────────────────────────

def _client(**over):
    data = {"route": PLAIN_ROUTE, "account": "agent@travnook.com",
            "account_email_password": "", "first_name": "A"}
    data.update(over)
    return data


def _mailbox_errors(data):
    return [p.field for p in validate.check_mailbox_password(data)]


def test_an_agents_own_account_must_supply_it(monkeypatch):
    monkeypatch.setattr("src.utils.credentials.email_password_for",
                        lambda email, required=False: "")

    assert _mailbox_errors(_client()) == ["account_email_password"]


def test_supplying_it_settles_the_matter(monkeypatch):
    monkeypatch.setattr("src.utils.credentials.email_password_for",
                        lambda email, required=False: "")

    assert _mailbox_errors(_client(account_email_password="mailbox-pw")) == []


def test_a_pool_account_needs_nothing_typed(monkeypatch):
    """The bot already holds it. Demanding it again breaks every existing
    client on a company account for no gain."""
    monkeypatch.setattr("src.utils.credentials.email_password_for",
                        lambda email, required=False: "pool-secret")

    assert _mailbox_errors(_client()) == []


def test_no_pinned_account_is_not_a_problem(monkeypatch):
    """With no account the shared pool supplies the login AND its mailbox
    password, so there is nothing for an agent to get wrong."""
    monkeypatch.setattr("src.utils.credentials.email_password_for",
                        lambda email, required=False: "")

    assert _mailbox_errors(_client(account="")) == []


def test_an_unreadable_pool_asks_rather_than_assumes(monkeypatch):
    """Demanding the field is recoverable; assuming a password exists is not."""
    def boom(email, required=False):
        raise RuntimeError("credentials file is unreadable")
    monkeypatch.setattr("src.utils.credentials.email_password_for", boom)

    assert _mailbox_errors(_client()) == ["account_email_password"]


def test_the_refusal_never_points_at_the_portal_password(monkeypatch):
    """The two were one value until 2026-10-16. An agent who reads this must
    not come away thinking the portal password will do."""
    monkeypatch.setattr("src.utils.credentials.email_password_for",
                        lambda email, required=False: "")

    said = validate.check_mailbox_password(_client())[0]

    assert "NOT the VFS portal password" in said.hint
    assert "invitation" in said.hint


def test_the_portal_password_does_not_satisfy_it(monkeypatch):
    """The 2026-10-16 split, as a rule rather than a comment."""
    monkeypatch.setattr("src.utils.credentials.email_password_for",
                        lambda email, required=False: "")

    data = _client(account_password="portal-pw")

    assert _mailbox_errors(data) == ["account_email_password"]
