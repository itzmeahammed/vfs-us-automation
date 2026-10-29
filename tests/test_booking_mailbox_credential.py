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
