"""A live client's detail view must run the real pre-flight.

WHY THIS FILE EXISTS
--------------------
`GET /v1/clients/{id}` used to hardcode the live branch as

    details, problems, runnable = resp.request, [], resp.enabled

so `problems` was always empty and `runnable` was merely "is it armed". The
browser builds the gate in front of the irreversible "Book and pay" button out
of exactly those two values (modules/clients/PreflightChecks.jsx renders
`ok: blocking.length === 0` under the label "Details pass the bot's own
pre-flight"). An always-empty problem list makes that check unfalsifiable: a
commit run against a record missing its billing address passed the gate and
left a booked-but-unpaid appointment.

It also made the API contradict itself — the listing ran the real pre-flight,
so the same client could read "2 problems - cannot run" in the table and
"no problems" in the drawer.

These tests assert the wiring rather than any particular rule: that the detail
view calls the booking store's pre-flight and reports what it says. The rules
themselves belong to src/booking/requests.py and are tested there.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

TEST_TOKEN = "e" * 64
os.environ.setdefault("VFSAPI_SECRET_TOKEN", TEST_TOKEN)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

HEADERS = {"X-Webhook-Secret-Token": os.environ["VFSAPI_SECRET_TOKEN"]}

#: A complete, valid live client. conftest.py's autouse `_isolate_booking_state`
#: points the request store at a tmp_path, so creating one touches no real
#: state and the auto-booker never sees it. It is created parked (`enabled:
#: False`) regardless, so nothing could act on it even if it did.
_APPLICANT = {
    "first_name": "AHMED", "last_name": "KHAN", "passport_number": "A1234567",
    "date_of_birth": "1990-04-12", "nationality": "India", "gender": "Male",
    "phone_country_code": "971", "phone_number": "501234567",
    "email": "ahmed@example.com", "address_line_1": "FLAT 101",
    "address_line_2": "AL BARSHA", "city": "Dubai", "postcode": "00000",
    "account": "booker@example.com", "account_password": "pw-never-returned",
}
LIVE_CLIENT = {
    "client_id": "preflight-live-1", "flow": "live", "route": "AE-NOR",
    "combo": "Norway Visa Application Center - Dubai - Tourist",
    "date_from": (date.today() + timedelta(days=10)).isoformat(),
    "date_to": (date.today() + timedelta(days=20)).isoformat(),
    "enabled": False, **_APPLICANT,
}


class _Problem:
    """Stands in for a booking-store pre-flight problem.

    The real ones come from src/booking/requests.py and carry no severity —
    every one is blocking — so only `to_dict()` matters here.
    """

    def __init__(self, field: str, message: str):
        self._field, self._message = field, message

    def to_dict(self):
        return {"field": self._field, "message": self._message}


@pytest.fixture()
def api():
    from fastapi.testclient import TestClient

    from src.api.core.security import _reset_rate_limiter

    _reset_rate_limiter()

    from src.api.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture()
def live_client(api) -> str:
    """A freshly created, parked live client in the isolated store.

    Created rather than discovered: a test that hunts for whatever the repo
    happens to contain skips silently when the store is empty, and a silent
    skip on a safety guard is indistinguishable from a pass — the same failure
    mode that let `test_every_route_requires_the_token` assert nothing for
    however long it did.
    """
    created = api.post("/v1/clients", headers=HEADERS, json=LIVE_CLIENT)
    assert created.status_code == 201, created.text
    return LIVE_CLIENT["client_id"]


def test_a_healthy_live_client_reports_no_problems(api, live_client):
    """The baseline: a complete record is runnable with an empty problem list.

    Without this, the test below could pass against a client that was broken
    for some unrelated reason.
    """
    body = api.get(f"/v1/clients/{live_client}", headers=HEADERS).json()

    assert body["problems"] == []
    assert body["problem_count"] == 0
    assert body["runnable"] is True


def test_detail_reports_the_stores_problems_and_is_not_runnable(api, live_client,
                                                                monkeypatch):
    """A blocking pre-flight problem must reach the detail response."""
    monkeypatch.setattr(
        "src.booking.requests.precheck",
        lambda request_id, fields: [_Problem("postcode",
                                             "Required for payment: postcode.")])

    body = api.get(f"/v1/clients/{live_client}", headers=HEADERS).json()

    assert body["runnable"] is False, (
        "a client with a blocking pre-flight problem must not report runnable — "
        "this is the gate in front of 'Book and pay'")
    assert body["problem_count"] == 1
    assert [p["field"] for p in body["problems"]] == ["postcode"], (
        "the problem must arrive with its field name, or the form cannot "
        "highlight the input that is wrong")


def test_detail_agrees_with_the_listing(api, live_client, monkeypatch):
    """The same client must not read differently in the table and the drawer.

    Asserted with a problem present: before the fix the listing ran the real
    pre-flight and the detail view hardcoded an empty one, so the two agreed
    only while there was nothing to disagree about.
    """
    monkeypatch.setattr(
        "src.booking.requests.precheck",
        lambda request_id, fields: [_Problem("city", "Required for payment: city.")])

    listed = next(
        row for row in api.get("/v1/clients",
                               params={"flow": "live", "include": "problems"},
                               headers=HEADERS).json()["items"]
        if row["client_id"] == live_client)
    detail = api.get(f"/v1/clients/{live_client}", headers=HEADERS).json()

    assert detail["runnable"] == listed["runnable"] is False
    assert detail["problem_count"] == listed["problem_count"] == 1


def test_problem_count_is_computed_not_null(api, live_client):
    """`None` means "not computed" and must not survive into a detail response.

    The listing builds rows with `problem_count: None` and fills it in only
    when asked for problems; the detail view reused those rows and overrode
    `runnable` alone, so it shipped a null count forever. A caller then cannot
    tell "no problems" from "nobody looked".
    """
    body = api.get(f"/v1/clients/{live_client}", headers=HEADERS).json()

    assert body["problem_count"] is not None
    assert body["problem_count"] == len(
        [p for p in body["problems"] if (p.get("severity") or "error") != "warning"])
