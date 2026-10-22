"""VFS appointment-confirmation emails settle what the browser could not.

The rules that must hold:
  * an invitation whose matched client is the applicant named in the email
    becomes `booked`, with the VFS reference, from the account's own mail;
  * a booking request for that applicant becomes `booked` and any unanswered
    payment it submitted is answered with a `payment_confirmed` row carrying
    the SAME run_id (so GET /payments/unanswered goes quiet);
  * matching is exact and never guesses: nothing on file -> `unmatched`,
    two candidates -> `ambiguous`, another account -> ignored;
  * recording is idempotent per email.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "d" * 64)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

from src.booking import autobook, confirmations, invites  # noqa: E402
from src.booking import requests as req_store  # noqa: E402
from src.payment import journal as payment_journal  # noqa: E402
from src.waitlist.registrant import Registrant  # noqa: E402

ROUTE = "AE-NOR"
ACCOUNT = "holder@example.com"


@dataclass
class _Email:
    uid: str = "41"
    mailbox: str = ACCOUNT
    received_epoch: float = field(default_factory=time.time)


@dataclass
class _Match:
    matcher_name: str = "appointment_confirmed"
    route: str = ROUTE
    classification: str = "other"
    fields: Dict[str, Any] = field(default_factory=lambda: {
        "applicant_name": "Ahmed Khan", "reference": "NOR126162488430"})


@dataclass
class _Obs:
    email: _Email = field(default_factory=_Email)
    match: _Match = field(default_factory=_Match)
    account: str = ACCOUNT


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(confirmations, "CONFIRM_DIR", str(tmp_path / "confirmations"))
    monkeypatch.setattr(invites, "INVITE_DIR", str(tmp_path / "invites"))
    monkeypatch.setattr(req_store, "REQUEST_DIR", str(tmp_path / "requests"))
    monkeypatch.setattr(payment_journal, "JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(payment_journal, "JOURNAL_FILE", str(tmp_path / "payments.jsonl"))
    sent: List[str] = []
    monkeypatch.setattr(autobook, "_telegram", sent.append)
    people: Dict[str, Registrant] = {}
    monkeypatch.setattr(confirmations, "_load_person", lambda cid: people.get(cid))
    return {"sent": sent, "people": people}


def _invite(status=invites.BOOKING, clients=("ahmed",), account=ACCOUNT,
            key="inv1") -> Dict[str, Any]:
    item = {"key": key, "route": ROUTE, "account": account, "applicant_name": "",
            "received_epoch": time.time(), "expires_epoch": time.time() + 3600,
            "uid": "7", "status": status, "attempts": 1, "history": [],
            "clients": {c: {"status": status} for c in clients}}
    os.makedirs(invites.INVITE_DIR, exist_ok=True)
    invites.save(item)
    return item


def _person(cid, first="Ahmed", last="Khan") -> Registrant:
    return Registrant(cid, {"route": ROUTE, "combos": ["x"], "first_name": first,
                            "last_name": last, "account": ACCOUNT,
                            "account_password": "pw"})


def _request(rid="r1", status=req_store.NEEDS_ATTENTION, account="", attempts=1,
             first="AHMED", last="KHAN"):
    data = {"route": ROUTE, "combo": "Norway Visa Application Center - Dubai - Tourist",
            "first_name": first, "last_name": last, "passport_number": "A1",
            "date_of_birth": "1990-01-01", "nationality": "India", "gender": "Male",
            "phone_country_code": "971", "phone_number": "5", "email": "a@x.com",
            "address_line_1": "x", "city": "Dubai", "postcode": "0",
            "country_code": "AE", "date_from": "2026-10-10", "date_to": "2026-10-20",
            "enabled": True, "status": status, "attempts": attempts,
            "created_at": "2026-10-01T00:00:00Z", "history": []}
    if account:
        data["account"] = account
        data["account_password"] = "pw"
    os.makedirs(req_store.REQUEST_DIR, exist_ok=True)
    req_store._write(rid, data)
    return rid


# --------------------------------------------------------------------------- #
# Invitations (Flow 2)                                                         #
# --------------------------------------------------------------------------- #

def test_the_named_clients_invitation_is_booked_with_the_reference(isolated):
    isolated["people"]["ahmed"] = _person("ahmed")
    _invite(status=invites.BOOKING)

    keys = confirmations.record([_Obs()])

    assert len(keys) == 1
    item = confirmations.get(keys[0])
    assert item["status"] == confirmations.MATCHED
    inv = invites.get("inv1")
    assert inv["status"] == invites.BOOKED
    assert inv["clients"]["ahmed"]["status"] == invites.BOOKED
    assert inv["clients"]["ahmed"]["vfs_reference"] == "NOR126162488430"
    assert inv["history"][-1]["event"] == "confirmed_by_email"
    assert any("CONFIRMED" in s for s in isolated["sent"])


def test_a_needs_attention_invitation_is_settled_too(isolated):
    isolated["people"]["ahmed"] = _person("ahmed")
    _invite(status=invites.NEEDS_ATTENTION)
    confirmations.record([_Obs()])
    assert invites.get("inv1")["status"] == invites.BOOKED


def test_another_accounts_invitation_is_never_touched(isolated):
    isolated["people"]["ahmed"] = _person("ahmed")
    _invite(account="someone-else@example.com")
    keys = confirmations.record([_Obs()])
    assert confirmations.get(keys[0])["status"] == confirmations.UNMATCHED
    assert invites.get("inv1")["status"] == invites.BOOKING


def test_a_different_name_is_never_matched(isolated):
    isolated["people"]["osama"] = _person("osama", first="Osama", last="Ali")
    _invite(clients=("osama",))
    keys = confirmations.record([_Obs()])
    assert confirmations.get(keys[0])["status"] == confirmations.UNMATCHED


def test_two_same_named_clients_on_one_invitation_is_ambiguous(isolated):
    isolated["people"]["a1"] = _person("a1")
    isolated["people"]["a2"] = _person("a2")
    _invite(clients=("a1", "a2"))
    keys = confirmations.record([_Obs()])
    item = confirmations.get(keys[0])
    assert item["status"] == confirmations.AMBIGUOUS
    assert invites.get("inv1")["status"] == invites.BOOKING


def test_an_already_booked_invitation_only_gains_the_reference(isolated):
    isolated["people"]["ahmed"] = _person("ahmed")
    _invite(status=invites.BOOKED)
    confirmations.record([_Obs()])
    inv = invites.get("inv1")
    assert inv["status"] == invites.BOOKED
    assert inv["history"][-1]["vfs_reference"] == "NOR126162488430"


# --------------------------------------------------------------------------- #
# Booking requests (Flow 3) and the payment journal                            #
# --------------------------------------------------------------------------- #

def test_a_request_is_booked_and_its_unanswered_payment_answered(isolated):
    rid = _request(status=req_store.NEEDS_ATTENTION)
    payment_journal.append({"event": "payment_submitting", "route": ROUTE,
                            "registrant_id": rid, "run_id": "run-77",
                            "booking_ref": "2026-10-16 10:15"})
    assert len(payment_journal.unanswered()) == 1

    keys = confirmations.record([_Obs()])

    item = confirmations.get(keys[0])
    assert item["status"] == confirmations.MATCHED
    kinds = sorted(s["kind"] for s in item["settled"])
    assert kinds == ["booking_request", "payment"]
    req = req_store.get(rid)
    assert req.status == req_store.BOOKED
    assert req.data["booked"]["vfs_reference"] == "NOR126162488430"
    assert payment_journal.unanswered() == []
    answer = payment_journal.read_all()[-1]
    assert answer["event"] == "payment_confirmed" and answer["run_id"] == "run-77"


def test_a_waiting_request_that_never_ran_needs_the_account_to_match(isolated):
    cold = _request("cold", status=req_store.WAITING, attempts=0)
    keys = confirmations.record([_Obs()])
    assert confirmations.get(keys[0])["status"] == confirmations.UNMATCHED
    assert req_store.get(cold).status == req_store.WAITING

    warm = _request("warm", status=req_store.WAITING, attempts=0, account=ACCOUNT)
    keys = confirmations.record([_Obs(email=_Email(uid="42"))])
    assert confirmations.get(keys[0])["status"] == confirmations.MATCHED
    assert req_store.get(warm).status == req_store.BOOKED


def test_two_open_requests_for_the_same_person_is_ambiguous(isolated):
    _request("r1", status=req_store.NEEDS_ATTENTION)
    _request("r2", status=req_store.BOOKING)
    keys = confirmations.record([_Obs()])
    assert confirmations.get(keys[0])["status"] == confirmations.AMBIGUOUS
    assert req_store.get("r1").status == req_store.NEEDS_ATTENTION


# --------------------------------------------------------------------------- #
# Hygiene                                                                      #
# --------------------------------------------------------------------------- #

def test_recording_is_idempotent_per_email(isolated):
    isolated["people"]["ahmed"] = _person("ahmed")
    _invite()
    first = confirmations.record([_Obs()])
    again = confirmations.record([_Obs()])
    assert len(first) == 1 and again == []
    events = [h["event"] for h in invites.get("inv1")["history"]]
    assert events.count("confirmed_by_email") == 1


def test_only_appointment_confirmations_are_consumed(isolated):
    other = _Obs(match=_Match(matcher_name="waitlist_confirmation",
                              classification="confirmation"))
    assert confirmations.record([other]) == []


def test_an_email_naming_nobody_is_filed_unmatched_not_guessed(isolated):
    isolated["people"]["ahmed"] = _person("ahmed")
    _invite()
    obs = _Obs(match=_Match(fields={"applicant_name": "", "reference": "X1"}))
    keys = confirmations.record([obs])
    item = confirmations.get(keys[0])
    assert item["status"] == confirmations.UNMATCHED
    assert invites.get("inv1")["status"] == invites.BOOKING


def test_pass_result_partitions_appointment_confirmations():
    from src.inbox.watcher import Observation, PassResult
    from src.inbox.matcher import Email, Match

    appt = Observation(Email(uid="1"), Match(classification="other",
                                            matcher_name="appointment_confirmed",
                                            route=ROUTE))
    other = Observation(Email(uid="2"), Match(classification="other",
                                             matcher_name="vfs_other", route=ROUTE))
    result = PassResult(observations=[appt, other])
    assert result.appointments() == [appt]
