"""Flow 2 (invitation email -> booking) and the four master switches.

The rules that must hold:
  * an invitation books ONLY a client it can identify without guessing —
    the account it arrived at, registered on the waitlist, named in the email
    when several share the account;
  * a country whose booking map cannot resume a waitlisted application sends
    the invitation to a human with its deadline, instead of dropping it;
  * every master switch stops its flow; off is always safe;
  * the switches API changes values without erasing config.local.ini comments.
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

from src.booking import autobook, invites  # noqa: E402
from src.settings import settings  # noqa: E402
from src.waitlist.registrant import Registrant  # noqa: E402

ROUTE = "AE-NLD"
ACCOUNT = "holder@example.com"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(invites, "INVITE_DIR", str(tmp_path / "invites"))
    sent: List[str] = []
    monkeypatch.setattr(autobook, "_telegram", sent.append)
    monkeypatch.setattr(autobook, "_alert_once", lambda key, text: sent.append(text))
    return sent


def _person(cid: str, first: str, last: str, **extra) -> Registrant:
    return Registrant(cid, {"route": ROUTE, "combos": ["Dubai - Tourist Visa"],
                            "first_name": first, "last_name": last,
                            "account": ACCOUNT, "account_password": "pw", **extra})


@pytest.fixture()
def roster(monkeypatch):
    people: List[Registrant] = []
    from src.waitlist import registrant as registrant_mod
    monkeypatch.setattr(registrant_mod, "for_route", lambda route: list(people))
    monkeypatch.setattr(invites, "registered_on", lambda route, p: True)
    return people


def _item(name: str = "", account: str = ACCOUNT, expires_in: float = 3600) -> Dict[str, Any]:
    item = {"key": "abc123", "route": ROUTE, "account": account,
            "applicant_name": name, "category": "Tourist Purpose",
            "received_epoch": time.time(), "expires_epoch": time.time() + expires_in,
            "validity_hours": 36, "uid": "7", "status": invites.PENDING,
            "clients": {}, "attempts": 0, "history": []}
    os.makedirs(invites.INVITE_DIR, exist_ok=True)
    invites.save(item)
    return item


# --------------------------------------------------------------------------- #
# Who is this invitation for?                                                  #
# --------------------------------------------------------------------------- #


def test_name_match_tolerates_a_middle_name_and_case():
    p = _person("c1", "Aissar", "Abbaas")
    assert invites.name_matches("AISSAR AHMED ALI ABBAAS", p)
    assert not invites.name_matches("ZAID KHAN", p)


def test_the_named_client_is_chosen(roster):
    roster += [_person("c1", "AISSAR", "ABBAAS"), _person("c2", "ZAID", "KHAN")]
    people, why = invites.candidates(_item("AISSAR AHMED ALI ABBAAS"))
    assert [p.id for p in people] == ["c1"] and "name" in why


def test_a_lone_client_on_the_account_is_chosen_without_a_name(roster):
    roster.append(_person("c1", "AISSAR", "ABBAAS"))
    assert [p.id for p in invites.candidates(_item(""))[0]] == ["c1"]


def test_several_clients_and_no_name_is_never_guessed(roster):
    roster += [_person("c1", "A", "B"), _person("c2", "C", "D")]
    people, why = invites.candidates(_item(""))
    assert people == [] and "names none" in why


def test_a_client_on_another_account_is_never_booked(roster):
    roster.append(_person("c1", "AISSAR", "ABBAAS", account="other@example.com"))
    assert invites.candidates(_item("AISSAR ABBAAS"))[0] == []


def test_a_client_not_registered_on_the_waitlist_is_skipped(roster, monkeypatch):
    roster.append(_person("c1", "AISSAR", "ABBAAS"))
    monkeypatch.setattr(invites, "registered_on", lambda route, p: False)
    assert invites.candidates(_item("AISSAR ABBAAS"))[0] == []


# --------------------------------------------------------------------------- #
# What happens to an invitation                                                #
# --------------------------------------------------------------------------- #


def test_a_country_without_a_booking_map_goes_to_a_human(roster, monkeypatch, isolated):
    roster.append(_person("c1", "AISSAR", "ABBAAS"))
    _item("AISSAR ABBAAS")
    monkeypatch.setattr(invites, "route_problem", lambda r: "no booking map for AE-NLD")
    spawned = []
    monkeypatch.setattr(invites, "spawn", lambda key, ids: spawned.append(ids))

    invites.process_pending()
    assert spawned == []
    assert invites.get("abc123")["status"] == invites.MANUAL
    assert "BOOK MANUALLY" in isolated[-1] and "Deadline" in isolated[-1]
    assert "c1 (AISSAR ABBAAS)" in isolated[-1]


def test_a_bookable_invitation_spawns_one_booking(roster, monkeypatch, isolated):
    roster.append(_person("c1", "AISSAR", "ABBAAS"))
    _item("AISSAR ABBAAS")
    monkeypatch.setattr(invites, "route_problem", lambda r: "")
    monkeypatch.setattr(invites, "gate_problem", lambda test=False: "")
    spawned = []
    monkeypatch.setattr(invites, "spawn",
                        lambda key, ids: spawned.append(ids) or "logs/x.log")

    assert invites.process_pending() == ["abc123"]
    assert spawned == [["c1"]]
    assert "BOOKING TRIGGERED" in isolated[-1]
    # Still PENDING: the child moves it to booking. A second pass before the
    # child starts is prevented by the booking lock, not by this status.
    assert invites.get("abc123")["status"] == invites.PENDING


def test_a_closed_switch_blocks_and_says_so(roster, monkeypatch, isolated):
    roster.append(_person("c1", "AISSAR", "ABBAAS"))
    _item("AISSAR ABBAAS")
    monkeypatch.setattr(invites, "route_problem", lambda r: "")
    monkeypatch.setattr(settings().switches, "invite_booking", False)
    spawned = []
    monkeypatch.setattr(invites, "spawn", lambda key, ids: spawned.append(ids))

    invites.process_pending()
    assert spawned == [] and "switched off" in isolated[-1]


def test_an_expired_invitation_is_closed(isolated):
    _item("X Y", expires_in=-10)
    invites.process_pending()
    assert invites.get("abc123")["status"] == invites.EXPIRED
    assert "EXPIRED" in isolated[-1]


def test_a_silent_booking_run_is_flagged_not_retried(isolated):
    item = _item("X Y")
    item["status"] = invites.BOOKING
    invites.save(item)
    invites.process_pending(now=time.time() + invites.STALE_SECONDS + 60)
    assert invites.get("abc123")["status"] == invites.NEEDS_ATTENTION


@dataclass
class _Email:
    uid: str = "9"
    mailbox: str = ACCOUNT
    received_epoch: float = field(default_factory=time.time)


@dataclass
class _Match:
    route: str = ROUTE
    fields: Dict[str, Any] = field(default_factory=lambda: {"applicant_name": "A B"})
    validity_hours: int = 36
    is_invitation: bool = True


@dataclass
class _Obs:
    email: _Email = field(default_factory=_Email)
    match: _Match = field(default_factory=_Match)
    account: str = ACCOUNT

    def expires_at(self):
        return self.email.received_epoch + 36 * 3600


def test_recording_is_idempotent_per_email():
    first = invites.record([_Obs()])
    again = invites.record([_Obs()])
    assert len(first) == 1 and again == []
    assert invites.get(first[0])["applicant_name"] == "A B"


# --------------------------------------------------------------------------- #
# Master switches                                                              #
# --------------------------------------------------------------------------- #


def test_each_master_switch_stops_its_flow(monkeypatch):
    from src.waitlist import guards

    sw = settings().switches
    monkeypatch.setattr(sw, "waitlist", False)
    verdict = guards.check("AE-CHE", "x", _person("c1", "A", "B"))
    assert not verdict.allowed and "[switches] waitlist" in verdict.reason

    monkeypatch.setattr(sw, "live_booking", False)
    assert "live_booking" in autobook.gate_problem("AE-NOR")
    monkeypatch.setattr(sw, "test_booking", False)
    assert "test_booking" in autobook.gate_problem("AE-NOR", test=True)
    monkeypatch.setattr(sw, "invite_booking", False)
    assert "invite_booking" in invites.gate_problem()


def test_ini_writer_keeps_comments_and_other_keys():
    from src.api.modules.waitlist.status import set_ini_value

    text = ("; operator notes\n[waitlist]\n; why: careful\ncooldown_hours = 2\n"
            "; register_enabled = old idea\n\n[otp]\nimap_host = x\n")
    out = set_ini_value(text, "waitlist", "register_enabled", "true")
    out = set_ini_value(out, "switches", "waitlist", "true")
    out = set_ini_value(out, "waitlist", "cooldown_hours", "3")
    assert "; operator notes" in out and "; why: careful" in out
    assert "; register_enabled = old idea" in out          # comment untouched
    assert "register_enabled = true" in out and "cooldown_hours = 3" in out
    assert out.index("register_enabled = true") < out.index("[otp]")
    assert "[switches]\nwaitlist = true" in out and "imap_host = x" in out


def test_status_reports_the_master_switches():
    from src.api.modules.waitlist.status import _switches

    state = _switches()
    for name in ("waitlist", "invite_booking", "live_booking", "test_booking"):
        assert hasattr(state, name)
