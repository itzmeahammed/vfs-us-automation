"""Automatic live-slot booking from stored booking requests.

What must never be wrong, in order of cost:

  * a run that reached PAYMENT without the gateway's own "success" is never
    retried — a retry can book and charge twice;
  * a request fires ONLY when the earliest seen date is inside its window —
    an earliest date before the window does not fire (product decision);
  * requests are stored, validated and served back without the password.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "d" * 64)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

from src.booking import autobook  # noqa: E402
from src.booking import requests as store  # noqa: E402

ROUTE = "AE-NOR"
COMBO = "Norway Visa Application Center - Dubai - Tourist"
TODAY = date(2026, 10, 1)

APPLICANT = {
    "first_name": "AHMED", "last_name": "KHAN", "passport_number": "A1234567",
    "date_of_birth": "1990-04-12", "nationality": "India", "gender": "Male",
    "phone_country_code": "971", "phone_number": "501234567",
    "email": "ahmed@example.com", "address_line_1": "FLAT 101",
    "address_line_2": "AL BARSHA", "city": "Dubai", "postcode": "00000",
    "account": "booker@example.com", "account_password": "pw-not-returned",
}


def _request(**over) -> Dict[str, Any]:
    data = {"route": ROUTE, "combo": COMBO, "date_from": "2026-10-10",
            "date_to": "2026-10-20", "enabled": True, **APPLICANT}
    data.update(over)
    return data


@pytest.fixture()
def request_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "REQUEST_DIR", str(tmp_path / "booking_requests"))
    return tmp_path


def _mk(rid: str, created: str = "2026-09-30T10:00:00Z", **over) -> store.BookingRequest:
    data = _request(**over)
    data.update({"status": over.get("status", store.WAITING),
                 "created_at": created})
    return store.BookingRequest(rid, data)


# --------------------------------------------------------------------------- #
# Matching                                                                     #
# --------------------------------------------------------------------------- #


def _identity(route, label):
    return label


def test_earliest_uses_the_one_applicant_date():
    results = [
        (COMBO, "Earliest available slot for 1 Applicants is : 12-10-2026\n"
                "Earliest available slot for 2 Applicants is : 09-11-2026"),
        ("Other combo", "No slot message shown (no availability?)."),
        ("Waitlisted", "WAITLIST - no slots; waitlist sign-up available"),
    ]
    earliest = autobook.earliest_by_combo(ROUTE, results, resolve_label=_identity)
    assert earliest == {autobook._norm(COMBO): (COMBO, "2026-10-12")}


def test_an_unmappable_label_matches_nothing():
    results = [("unknown label", "Earliest available slot for 1 Applicants is : 12-10-2026")]
    assert autobook.earliest_by_combo(ROUTE, results,
                                      resolve_label=lambda r, l: None) == {}


@pytest.mark.parametrize("seen, fires", [
    ("2026-10-10", True),     # first day of the window
    ("2026-10-15", True),
    ("2026-10-20", True),     # last day
    ("2026-10-09", False),    # BEFORE the window: deliberately does not fire
    ("2026-10-21", False),    # after: cannot contain an in-window date
])
def test_fires_only_when_earliest_is_inside_the_window(seen, fires):
    earliest = {autobook._norm(COMBO): (COMBO, seen)}
    matches = autobook.find_matches(ROUTE, earliest, [_mk("r1")], today=TODAY)
    assert bool(matches) is fires


def test_only_armed_requests_on_this_route_and_combo_match():
    earliest = {autobook._norm(COMBO): (COMBO, "2026-10-12")}
    reqs = [
        _mk("parked", enabled=False),
        _mk("booked", status=store.BOOKED),
        _mk("attention", status=store.NEEDS_ATTENTION),
        _mk("other-combo", combo="Norway Visa Application Center - Dubai - Business"),
        _mk("other-route", route="AE-CHE"),
        _mk("armed"),
    ]
    matches = autobook.find_matches(ROUTE, earliest, reqs, today=TODAY)
    assert [m.request_id for m in matches] == ["armed"]


def test_oldest_request_gets_the_first_chance():
    earliest = {autobook._norm(COMBO): (COMBO, "2026-10-12")}
    reqs = [_mk("newer", created="2026-09-30T12:00:00Z"),
            _mk("older", created="2026-09-29T08:00:00Z")]
    assert [m.request_id for m in autobook.find_matches(
        ROUTE, earliest, reqs, today=TODAY)] == ["older", "newer"]


# --------------------------------------------------------------------------- #
# Outcome classification                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class _Step:
    name: str
    ok: bool = True
    found: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _Walk:
    steps: List[_Step]
    stopped_at: str = ""
    reason: str = ""
    payment_declined: bool = False
    blocked: bool = False

    @property
    def ok(self):
        return bool(self.steps) and all(s.ok for s in self.steps)


@dataclass
class _Result:
    walk: Any = None
    errors: List[str] = field(default_factory=list)
    interrupted: bool = False


PRE = [_Step("start_booking"), _Step("appointment_details"),
       _Step("select_slot", found={"chosen_date": "2026-10-12",
                                   "chosen_time": "09:30"})]
SUBMITTING = {"event": "payment_submitting", "booking_ref": "B1"}


def test_no_walk_goes_back_to_waiting():
    out = autobook.classify(_Result(errors=["login failed"]), "payment", [])
    assert out.status == store.WAITING and "login failed" in out.detail


def test_stopping_before_payment_goes_back_to_waiting():
    walk = _Walk(PRE[:2] + [_Step("select_slot", ok=False)],
                 stopped_at="select_slot", reason="no date in range")
    out = autobook.classify(_Result(walk=walk), "payment", [])
    assert out.status == store.WAITING


def test_gateway_success_is_booked_with_the_appointment():
    walk = _Walk(PRE + [_Step("payment")], stopped_at="payment")
    rows = [SUBMITTING, {"event": "payment_result", "outcome": "success",
                         "requestrefno": "R-77", "transactionid": "T-9"}]
    out = autobook.classify(_Result(walk=walk), "payment", rows)
    assert out.status == store.BOOKED
    assert out.details["appointment_date"] == "2026-10-12"
    assert out.details["appointment_time"] == "09:30"
    assert out.details["requestrefno"] == "R-77"


@pytest.mark.parametrize("rows", [
    [SUBMITTING],                                                   # never answered
    [SUBMITTING, {"event": "payment_result", "outcome": "unknown"}],
    [],                                                             # step reached, no row
])
def test_anything_short_of_gateway_success_after_payment_needs_a_human(rows):
    walk = _Walk(PRE + [_Step("payment")], stopped_at="payment")
    out = autobook.classify(_Result(walk=walk), "payment", rows)
    assert out.status == store.NEEDS_ATTENTION


def test_a_failed_payment_step_needs_a_human_not_a_retry():
    walk = _Walk(PRE + [_Step("payment", ok=False)], stopped_at="payment",
                 reason="billing city missing")
    out = autobook.classify(_Result(walk=walk), "payment", [])
    assert out.status == store.NEEDS_ATTENTION


def test_declined_and_blocked_need_a_human():
    declined = _Walk(PRE + [_Step("payment", ok=False)], payment_declined=True)
    blocked = _Walk(PRE[:1], blocked=True, reason="booking in progress")
    for walk in (declined, blocked):
        assert autobook.classify(_Result(walk=walk), "payment",
                                 []).status == store.NEEDS_ATTENTION


def test_interruption_is_judged_by_whether_payment_was_reached():
    before = _Result(walk=_Walk(PRE[:2]), interrupted=True)
    after = _Result(walk=_Walk(PRE + [_Step("payment", ok=False)]), interrupted=True)
    assert autobook.classify(before, "payment", []).status == store.WAITING
    assert autobook.classify(after, "payment", []).status == store.NEEDS_ATTENTION


# --------------------------------------------------------------------------- #
# Store lifecycle                                                              #
# --------------------------------------------------------------------------- #


def test_create_stamps_state_and_ignores_caller_state(request_dir):
    req = store.create("r1", _request(status="booked", attempts=99))
    assert req.status == store.WAITING and req.data["attempts"] == 0
    assert store.get("r1").data["country_code"] == "AE"      # defaulted
    with pytest.raises(store.RequestExistsError):
        store.create("r1", _request())


def test_a_request_mid_booking_cannot_be_edited_or_deleted(request_dir):
    store.create("r1", _request())
    store.record_event("r1", "attempt_started", status=store.BOOKING)
    with pytest.raises(store.RequestLockedError):
        store.replace_fields("r1", _request(date_to="2026-10-25"))
    with pytest.raises(store.RequestLockedError):
        store.delete("r1")
    with pytest.raises(store.RequestLockedError):
        store.set_enabled("r1", False)


def test_past_windows_expire(request_dir):
    store.create("old", _request(date_from="2026-09-01", date_to="2026-09-05"))
    store.create("live", _request())
    assert store.expire_past(today=TODAY) == ["old"]
    assert store.get("old").status == store.EXPIRED
    assert store.get("live").status == store.WAITING


def test_resolve_only_from_needs_attention(request_dir):
    store.create("r1", _request())
    with pytest.raises(store.RequestLockedError):
        store.resolve("r1", "booked")
    store.record_event("r1", "needs_attention", "x", status=store.NEEDS_ATTENTION)
    assert store.resolve("r1", "not_booked").status == store.WAITING


def test_the_walk_never_sees_state_keys(request_dir):
    store.create("r1", _request())
    store.record_event("r1", "attempt_started", status=store.BOOKING)
    person = store.get("r1").as_registrant()
    context = person.as_context()
    assert not any(k.startswith(("history", "status", "attempts")) for k in context)
    assert person.combos == [COMBO] and person.account == "booker@example.com"


def test_human_resolutions_do_not_count_toward_the_daily_cap(request_dir):
    store.create("r1", _request())
    store.record_event("r1", "needs_attention", "x", status=store.NEEDS_ATTENTION)
    store.resolve("r1", "booked")
    today = date.today()
    assert autobook.committed_today(store.list_all(), today=today) == 1


# --------------------------------------------------------------------------- #
# Supervisor hook and the child                                                #
# --------------------------------------------------------------------------- #


SLOT_RESULTS = [[COMBO, "Earliest available slot for 1 Applicants is : 12-10-2026"]]


def test_hook_spawns_only_when_gates_pass(request_dir, monkeypatch):
    store.create("r1", _request(date_from="2026-10-10", date_to="2026-10-31"))
    monkeypatch.setattr(autobook, "earliest_by_combo",
                        lambda route, results: {autobook._norm(COMBO): (COMBO, "2099-10-12")})
    monkeypatch.setattr(store, "expire_past", lambda: [])
    spawned = []
    monkeypatch.setattr(autobook, "spawn", lambda route, m: spawned.append(m))
    monkeypatch.setattr(autobook, "_alert_once", lambda *a: None)

    # Window does not contain 2099 -> nothing.
    assert autobook.handle_route_checked(ROUTE, SLOT_RESULTS) == []

    monkeypatch.setattr(autobook, "earliest_by_combo",
                        lambda route, results: {autobook._norm(COMBO): (COMBO, "2026-10-12")})
    monkeypatch.setattr(autobook, "find_matches",
                        lambda route, e, reqs: [autobook.Match("r1", ROUTE, COMBO, "2026-10-12")])

    monkeypatch.setattr(autobook, "gate_problem", lambda route: "no company card")
    autobook.handle_route_checked(ROUTE, SLOT_RESULTS)
    assert spawned == []                               # gate held

    monkeypatch.setattr(autobook, "gate_problem", lambda route: "")
    autobook.handle_route_checked(ROUTE, SLOT_RESULTS)
    assert [m.request_id for m in spawned[0]] == ["r1"]


def test_hook_never_raises(monkeypatch):
    def boom(**_):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(store, "expire_past", lambda: boom())
    assert autobook.handle_route_checked(ROUTE, SLOT_RESULTS) == []


def _run_child(monkeypatch, result, rows):
    from src.booking import probe
    from src.payment import journal as payment_journal

    journal = [{"event": "old"}]
    monkeypatch.setattr(payment_journal, "read_all", lambda: list(journal))

    def fake_probe(**kwargs):
        assert kwargs["commit"] is True and kwargs["entry"] == "new"
        assert kwargs["person"].id == "r1"
        journal.extend(rows)
        return result
    monkeypatch.setattr(probe, "run_probe", fake_probe)
    monkeypatch.setattr(autobook, "gate_problem", lambda route: "")
    monkeypatch.setattr(autobook, "_notify", lambda req, out: None)
    monkeypatch.setattr(store, "precheck", lambda rid, data, today=None: [])
    return autobook.run_queue(ROUTE, ["r1"], {"r1": "2026-10-12"})


def test_child_books_and_records_the_appointment(request_dir, monkeypatch):
    store.create("r1", _request())
    walk = _Walk(PRE + [_Step("payment")], stopped_at="payment")
    _run_child(monkeypatch, _Result(walk=walk),
               [SUBMITTING, {"event": "payment_result", "outcome": "success"}])
    req = store.get("r1")
    assert req.status == store.BOOKED
    assert req.data["booked"]["appointment_date"] == "2026-10-12"
    assert req.data["attempts"] == 1


def test_child_returns_a_pre_payment_failure_to_waiting(request_dir, monkeypatch):
    store.create("r1", _request())
    walk = _Walk(PRE[:2] + [_Step("select_slot", ok=False)], stopped_at="select_slot",
                 reason="no date inside the window")
    _run_child(monkeypatch, _Result(walk=walk), [])
    req = store.get("r1")
    assert req.status == store.WAITING and "no date" in req.data["last_error"]


def test_child_gives_up_after_max_attempts(request_dir, monkeypatch):
    store.create("r1", _request())
    from src.settings import settings
    monkeypatch.setattr(settings().booking, "max_attempts", 1)
    _run_child(monkeypatch, _Result(errors=["login failed"]), [])
    assert store.get("r1").status == store.NEEDS_ATTENTION


def test_child_skips_a_request_disabled_since_it_was_matched(request_dir, monkeypatch):
    store.create("r1", _request(enabled=False))
    _run_child(monkeypatch, _Result(errors=["should not run"]), [])
    assert store.get("r1").data["attempts"] == 0


# --------------------------------------------------------------------------- #
# API                                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def api(request_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.security import _reset_rate_limiter
    from src.api.main import app

    _reset_rate_limiter()
    with TestClient(app) as client:
        yield client


HEADERS = {"X-Webhook-Secret-Token": os.environ["VFSAPI_SECRET_TOKEN"]}


def test_api_names_every_missing_field(api):
    r = api.post("/booking-requests", headers=HEADERS, json={
        "request_id": "r1", "route": ROUTE, "combo": COMBO,
        "date_from": "2026-10-10", "date_to": "2026-10-20"})
    assert r.status_code == 422
    fields = {p["field"] for p in r.json()["problems"]}
    assert {"passport_number", "city", "postcode", "email"} <= fields


def test_api_refuses_a_waitlist_only_route_and_a_bad_window(api):
    r = api.post("/booking-requests", headers=HEADERS, json={
        "request_id": "r1", **_request(route="AE-GRC", date_from="2026-10-20",
                                       date_to="2026-10-10")})
    assert r.status_code == 422


def test_api_full_lifecycle(api):
    body = {"request_id": "r1", **_request(date_from="2099-01-10",
                                           date_to="2099-01-20")}
    body["date_from"], body["date_to"] = _future_window()
    r = api.post("/booking-requests", headers=HEADERS, json=body)
    assert r.status_code == 201, r.text
    got = r.json()
    assert got["status"] == "waiting" and got["enabled"] is True
    assert "account_password" not in got["request"]
    assert got["request"]["passport_number"] != "A1234567"      # masked

    assert api.post("/booking-requests", headers=HEADERS, json=body).status_code == 409
    assert api.get("/booking-requests", headers=HEADERS).json()["count"] == 1

    r = api.patch("/booking-requests/r1", headers=HEADERS, json={"city": "Abu Dhabi"})
    assert r.status_code == 200 and r.json()["request"]["city"] == "Abu Dhabi"
    assert store.get("r1").data["account_password"] == "pw-not-returned"

    assert api.post("/booking-requests/r1/disable", headers=HEADERS).json()["enabled"] is False
    assert api.post("/booking-requests/r1/enable", headers=HEADERS).json()["enabled"] is True

    assert api.post("/booking-requests/r1/resolve", headers=HEADERS,
                    json={"outcome": "booked"}).status_code == 409

    store.record_event("r1", "attempt_started", status=store.BOOKING)
    assert api.delete("/booking-requests/r1", headers=HEADERS).status_code == 409
    store.record_event("r1", "needs_attention", "x", status=store.NEEDS_ATTENTION)
    r = api.post("/booking-requests/r1/resolve", headers=HEADERS,
                 json={"outcome": "booked", "appointment_date": "2026-10-12"})
    assert r.json()["status"] == "booked"

    assert api.get("/booking-requests/nope", headers=HEADERS).status_code == 404


def _future_window():
    from datetime import timedelta
    start = date.today() + timedelta(days=10)
    return start.isoformat(), (start + timedelta(days=10)).isoformat()
