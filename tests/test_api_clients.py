"""Tests for the client-management API (Phase 1).

Covers the two rules that matter most:

  * secrets go IN but never OUT — a VFS account password must not appear in any
    response body, at any endpoint;
  * created is not the same as armed — a new client is parked by default.

Client files are written into a temp directory so no real client under
config/registrants/ is touched.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

TEST_TOKEN = "c" * 64
os.environ.setdefault("VFSAPI_SECRET_TOKEN", TEST_TOKEN)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

HEADERS = {"X-Webhook-Secret-Token": os.environ["VFSAPI_SECRET_TOKEN"]}

# A route that is genuinely registration-ready in this repo.
READY_ROUTE = "AE-CHE"
READY_COMBO = "Dubai - SCHENGEN"

SECRET_PASSWORD = "sup3r-secret-vfs-password"


def _client_payload(client_id: str = "test-client-che", **overrides):
    """A complete, valid client payload for AE-CHE."""
    payload = {
        "client_id": client_id,
        "route": READY_ROUTE,
        "combos": [READY_COMBO],
        "account": "waitlist-test@example.com",
        "account_password": SECRET_PASSWORD,
        "first_name": "TEST",
        "last_name": "CLIENT",
        "nationality": "India",
        "passport_number": "X1234567",
        "date_of_birth": "1990-04-12",
        "phone_country_code": "971",
        "phone_number": "501234567",
        "email": "test.client@example.com",
        "address_line_1": "FLAT 1, TEST TOWER",
        "address_line_2": "TEST ROAD, DUBAI",
        "gender": "Male",
        "passport_expiry": "2030-06-30",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """A TestClient whose client files live in a temp dir."""
    from fastapi.testclient import TestClient

    registrant_dir = tmp_path / "registrants"
    registrant_dir.mkdir()

    # Point BOTH the reader and the writer at the temp directory.
    monkeypatch.setattr("src.waitlist.registrant.REGISTRANT_DIR",
                        str(registrant_dir), raising=False)
    monkeypatch.setattr("src.waitlist.store.REGISTRANT_DIR",
                        str(registrant_dir), raising=False)
    monkeypatch.setattr("src.waitlist.store.path_for",
                        lambda rid: str(registrant_dir / f"{rid}.json"),
                        raising=False)

    # The rate limiter is a per-process singleton — correct in production, but
    # across a whole test file the suite's requests look like one client
    # flooding the endpoint. Reset it per test rather than weakening the limit.
    from src.api.security import _reset_rate_limiter
    _reset_rate_limiter()

    from src.api.main import app
    with TestClient(app) as client:
        client.registrant_dir = registrant_dir      # type: ignore[attr-defined]
        yield client


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/clients"),
        ("post", "/clients"),
        ("get", "/clients/anything"),
        ("delete", "/clients/anything"),
        ("post", "/clients/anything/enable"),
        ("get", f"/routes/{READY_ROUTE}/readiness"),
    ],
)
def test_every_client_endpoint_requires_the_token(api, method, path):
    """No endpoint may be reachable without the shared secret."""
    response = getattr(api, method)(path)
    assert response.status_code == 401


# --------------------------------------------------------------------------
# Route readiness
# --------------------------------------------------------------------------


def test_ready_route_reports_its_combos(api):
    """A live route returns ready=true and the labels a form should offer."""
    r = api.get(f"/routes/{READY_ROUTE}/readiness", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert READY_COMBO in body["combos"]


def test_disabled_route_is_not_ready_and_says_why(api):
    """AE-DEU is disabled pending the known centre bug — say so, don't hide it."""
    r = api.get("/routes/AE-DEU/readiness", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False
    assert body["problems"], "a not-ready route must explain why"
    assert any("enabled" in p["message"].lower() for p in body["problems"])


def test_unknown_route_is_not_ready(api):
    r = api.get("/routes/ZZ-ZZZ/readiness", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["ready"] is False


# --------------------------------------------------------------------------
# Creating clients
# --------------------------------------------------------------------------


def test_create_writes_a_parked_client(api):
    """Created is not armed: enabled defaults to false."""
    r = api.post("/clients", json=_client_payload(), headers=HEADERS)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True
    assert body["enabled"] is False, "a new client must be PARKED by default"

    path = api.registrant_dir / "test-client-che.json"
    assert path.exists()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["route"] == READY_ROUTE
    assert stored["enabled"] is False
    # client_id is the filename, not file content.
    assert "client_id" not in stored


def test_create_can_arm_explicitly(api):
    """enabled=true is honoured when the caller asks for it deliberately."""
    r = api.post("/clients", json=_client_payload(enabled=True), headers=HEADERS)
    assert r.status_code == 201
    assert r.json()["enabled"] is True


def test_duplicate_create_is_rejected(api):
    """A double-submit must not clobber live client data."""
    assert api.post("/clients", json=_client_payload(), headers=HEADERS).status_code == 201
    r = api.post("/clients", json=_client_payload(), headers=HEADERS)
    assert r.status_code == 409


def test_unknown_combo_is_rejected_with_the_valid_list(api):
    """A client must not be able to queue for a combination that does not exist."""
    r = api.post(
        "/clients",
        json=_client_payload(combos=["Atlantis - Moon Visa"]),
        headers=HEADERS,
    )
    assert r.status_code == 422
    problems = r.json()["problems"]
    assert any("not a combination" in p["message"] for p in problems)
    # The error should tell the caller what IS valid.
    assert any(READY_COMBO in (p.get("hint") or "") for p in problems)


def test_not_ready_route_is_rejected(api):
    """Creating against a disabled route must fail, not silently park a client."""
    r = api.post(
        "/clients",
        json=_client_payload(route="AE-DEU", combos=["Dubai - Tourism"]),
        headers=HEADERS,
    )
    assert r.status_code == 422


def test_all_problems_are_returned_at_once(api):
    """A web form needs every error in one pass, not one at a time."""
    r = api.post(
        "/clients",
        json={
            "client_id": "broken-client",
            "route": READY_ROUTE,
            "combos": ["Nope - Not Real"],
            "account": "not-an-email",          # missing '@'
            "account_password": "x",
            "first_name": "TEST",
        },
        headers=HEADERS,
    )
    assert r.status_code == 422
    problems = r.json()["problems"]
    fields = {p["field"] for p in problems}
    # At minimum the bad account AND the bad combo must both be reported.
    assert "account" in fields
    assert "combos" in fields


def test_account_without_password_is_rejected(api):
    """All-or-nothing: a pinned account needs its own password."""
    payload = _client_payload()
    payload.pop("account_password")
    r = api.post("/clients", json=payload, headers=HEADERS)
    assert r.status_code == 422
    assert any(p["field"] == "account_password"
               for p in r.json()["problems"])


def test_selector_in_a_field_is_rejected(api):
    """Client files hold data, never page structure."""
    r = api.post(
        "/clients",
        json=_client_payload(first_name="mat-select[formcontrolname='x']"),
        headers=HEADERS,
    )
    assert r.status_code == 422


def test_bad_client_id_is_rejected(api):
    """The id becomes a filename — no traversal, no spaces, no dots."""
    for bad in ["../escape", "Has Spaces", "", "a/b", "with.dot", "-leading"]:
        r = api.post("/clients", json=_client_payload(client_id=bad), headers=HEADERS)
        assert r.status_code == 422, f"accepted bad id {bad!r}"


def test_client_id_and_route_are_normalised(api):
    """Case is normalised rather than rejected — 'UPPER' is a typo, not an attack.

    Mirrors how `route` accepts 'ae-che'. Rejecting it would be user-hostile for
    no security gain: the slug charset, not the case, is what keeps the id safe
    as a filename.
    """
    r = api.post(
        "/clients",
        json=_client_payload(client_id="UPPER-Client", route="ae-che"),
        headers=HEADERS,
    )
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == "upper-client"
    assert (api.registrant_dir / "upper-client.json").exists()


# --------------------------------------------------------------------------
# Secrets must never come back out
# --------------------------------------------------------------------------


def test_password_never_appears_in_any_response(api):
    """The single most important assertion in this file.

    The web app supplies per-client VFS credentials, so a leak here would
    expose a real account through the tunnel.
    """
    create = api.post("/clients", json=_client_payload(), headers=HEADERS)
    assert create.status_code == 201
    assert SECRET_PASSWORD not in create.text

    detail = api.get("/clients/test-client-che", headers=HEADERS)
    assert SECRET_PASSWORD not in detail.text

    listing = api.get("/clients", headers=HEADERS)
    assert SECRET_PASSWORD not in listing.text

    enable = api.post("/clients/test-client-che/enable", headers=HEADERS)
    assert SECRET_PASSWORD not in enable.text

    # ...but it IS persisted, or the run could not log in.
    stored = json.loads(
        (api.registrant_dir / "test-client-che.json").read_text(encoding="utf-8"))
    assert stored["account_password"] == SECRET_PASSWORD


def test_response_reports_password_presence_without_revealing_it(api):
    api.post("/clients", json=_client_payload(), headers=HEADERS)
    body = api.get("/clients/test-client-che", headers=HEADERS).json()
    assert body["client"]["has_account_password"] is True
    assert "account_password" not in body["client"]


def test_pii_is_masked_in_responses(api):
    """Passport numbers are recognisable but not usable in a response."""
    api.post("/clients", json=_client_payload(), headers=HEADERS)
    body = api.get("/clients/test-client-che", headers=HEADERS).json()
    assert body["client"]["passport_number"] != "X1234567"
    assert "*" in body["client"]["passport_number"]


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_enable_then_disable(api):
    api.post("/clients", json=_client_payload(), headers=HEADERS)

    enabled = api.post("/clients/test-client-che/enable", headers=HEADERS)
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True

    disabled = api.post("/clients/test-client-che/disable", headers=HEADERS)
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False


def test_get_reports_runnable(api):
    api.post("/clients", json=_client_payload(), headers=HEADERS)
    body = api.get("/clients/test-client-che", headers=HEADERS).json()
    assert body["runnable"] is True
    assert body["problems"] == []


def test_list_filters_by_route(api):
    api.post("/clients", json=_client_payload("client-a"), headers=HEADERS)
    api.post("/clients", json=_client_payload("client-b"), headers=HEADERS)

    listed = api.get("/clients", headers=HEADERS).json()
    assert listed["count"] == 2

    filtered = api.get(f"/clients?route={READY_ROUTE}", headers=HEADERS).json()
    assert filtered["count"] == 2

    none = api.get("/clients?route=AE-ITA", headers=HEADERS).json()
    assert none["count"] == 0


def test_update_changes_data(api):
    api.post("/clients", json=_client_payload(), headers=HEADERS)
    updated = api.put(
        "/clients/test-client-che",
        json=_client_payload(first_name="CHANGED"),
        headers=HEADERS,
    )
    assert updated.status_code == 200
    stored = json.loads(
        (api.registrant_dir / "test-client-che.json").read_text(encoding="utf-8"))
    assert stored["first_name"] == "CHANGED"


def test_delete_removes_the_file(api):
    api.post("/clients", json=_client_payload(), headers=HEADERS)
    assert api.delete("/clients/test-client-che", headers=HEADERS).status_code == 200
    assert not (api.registrant_dir / "test-client-che.json").exists()
    assert api.get("/clients/test-client-che", headers=HEADERS).status_code == 404


def test_unknown_client_is_404(api):
    assert api.get("/clients/nope", headers=HEADERS).status_code == 404
    assert api.delete("/clients/nope", headers=HEADERS).status_code == 404
    assert api.post("/clients/nope/enable", headers=HEADERS).status_code == 404


def test_update_without_enabled_preserves_the_armed_state(api):
    """Regression: PUT silently DISARMED an enabled client.

    `enabled` defaults to False on the request model, so an update that does not
    mention it looked identical to one asking to park the client. A client you
    had armed went quiet, with nothing to tell you — the run then failed with
    'Client is disabled', long after the PUT that caused it.
    """
    api.post("/clients", json=_client_payload(), headers=HEADERS)
    assert api.post("/clients/test-client-che/enable",
                    headers=HEADERS).json()["enabled"] is True

    body = _client_payload(first_name="UPDATED")
    body.pop("enabled", None)          # caller simply does not mention it
    updated = api.put("/clients/test-client-che", json=body, headers=HEADERS)

    assert updated.status_code == 200
    assert updated.json()["enabled"] is True, "PUT silently disarmed the client"
    stored = json.loads(
        (api.registrant_dir / "test-client-che.json").read_text(encoding="utf-8"))
    assert stored["enabled"] is True
    assert stored["first_name"] == "UPDATED"


def test_update_can_still_park_explicitly(api):
    """Sending enabled=false deliberately must still work."""
    api.post("/clients", json=_client_payload(), headers=HEADERS)
    api.post("/clients/test-client-che/enable", headers=HEADERS)

    updated = api.put("/clients/test-client-che",
                      json=_client_payload(enabled=False), headers=HEADERS)
    assert updated.json()["enabled"] is False


def test_update_can_arm_explicitly(api):
    """...and enabled=true through PUT arms it."""
    api.post("/clients", json=_client_payload(), headers=HEADERS)
    updated = api.put("/clients/test-client-che",
                      json=_client_payload(enabled=True), headers=HEADERS)
    assert updated.json()["enabled"] is True
