"""The /v1 API: one client resource, one error shape, audit, lanes, legacy aliases.

What must hold for a web app built against /v1:
  * every error is the same envelope and carries the request id;
  * a client is created with a flow and then behaves the same whichever flow;
  * ids are unique across flows;
  * the old paths still work and say they are deprecated;
  * every mutating call is audited with its X-Actor, never with its body;
  * a manual run of a live client runs THAT request (the bug behind
    "No client files target AE-NOR").
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "d" * 64)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

H = {"X-Webhook-Secret-Token": os.environ["VFSAPI_SECRET_TOKEN"],
     "X-Actor": "agent.test@example.com"}

START = (date.today() + timedelta(days=10)).isoformat()
END = (date.today() + timedelta(days=20)).isoformat()

APPLICANT = {
    "first_name": "AHMED", "last_name": "KHAN", "passport_number": "A1234567",
    "date_of_birth": "1990-04-12", "nationality": "India", "gender": "Male",
    "phone_country_code": "971", "phone_number": "501234567",
    "email": "ahmed@example.com", "address_line_1": "FLAT 101",
    "address_line_2": "AL BARSHA", "city": "Dubai", "postcode": "00000",
    "account": "booker@example.com", "account_password": "pw-never-returned",
}
LIVE = {"client_id": "live-1", "flow": "live", "route": "AE-NOR",
        "combo": "Norway Visa Application Center - Dubai - Tourist",
        "date_from": START, "date_to": END, "enabled": False, **APPLICANT}
WAITLIST = {"client_id": "wl-1", "flow": "waitlist", "route": "AE-CHE",
            "combos": ["Dubai - SCHENGEN"], "nationality": "INDIA",
            "passport_expiry": "2030-06-30",
            **{k: v for k, v in APPLICANT.items() if k != "nationality"}}


@pytest.fixture()
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    registrants = tmp_path / "registrants"
    registrants.mkdir()
    monkeypatch.setattr("src.waitlist.registrant.REGISTRANT_DIR", str(registrants))
    monkeypatch.setattr("src.waitlist.store.REGISTRANT_DIR", str(registrants))
    monkeypatch.setattr("src.waitlist.store.path_for",
                        lambda rid: str(registrants / f"{rid}.json"))
    monkeypatch.setattr("src.api.core.context.AUDIT_FILE", str(tmp_path / "audit.jsonl"))

    from src.api.core.security import _reset_rate_limiter
    _reset_rate_limiter()
    from src.api.main import app
    with TestClient(app) as client:
        client.tmp = tmp_path                                # type: ignore[attr-defined]
        yield client


# --------------------------------------------------------------------------- #
# Envelope, request id, deprecation                                            #
# --------------------------------------------------------------------------- #


def test_every_v1_error_is_the_same_envelope(api):
    r = api.get("/v1/clients/nobody", headers=H)
    body = r.json()["error"]
    assert r.status_code == 404 and body["code"] == "not_found"
    assert body["request_id"] == r.headers["X-Request-ID"]

    r = api.post("/v1/clients", headers=H, json={"client_id": "x", "route": "AE-NOR"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert any(p["field"].endswith("flow") for p in r.json()["error"]["problems"])


def test_legacy_rule_errors_are_normalised_too(api):
    """The legacy handler raises booking_request_invalid; /v1 says validation_error."""
    r = api.post("/v1/clients", headers=H, json={**LIVE, "postcode": ""})
    err = r.json()["error"]
    assert r.status_code == 422 and err["code"] == "validation_error"
    assert {p["field"] for p in err["problems"]} >= {"postcode"}


def test_a_callers_request_id_is_echoed(api):
    r = api.get("/v1/switches", headers={**H, "X-Request-ID": "web-abc-123"})
    assert r.headers["X-Request-ID"] == "web-abc-123"


def test_the_pre_v1_paths_are_gone(api):
    """Every endpoint is under /v1; the old paths were deleted, not hidden."""
    for path in ("/clients", "/booking-requests", "/status", "/jobs", "/health"):
        r = api.get(path, headers=H)
        assert r.status_code == 404, path
        assert r.json()["error"]["code"] == "not_found"
    assert api.get("/v1/clients", headers=H).status_code == 200
    assert api.get("/v1/health").status_code == 200        # no token needed


# --------------------------------------------------------------------------- #
# One client resource                                                          #
# --------------------------------------------------------------------------- #


def test_create_and_read_both_flows_in_one_shape(api):
    live = api.post("/v1/clients", headers=H, json=LIVE)
    assert live.status_code == 201, live.text
    wl = api.post("/v1/clients", headers=H, json=WAITLIST)
    assert wl.status_code == 201, wl.text

    for body, flow in ((live.json(), "live"), (wl.json(), "waitlist")):
        assert body["flow"] == flow and body["status"] == "waiting"
        assert body["enabled"] is False
        assert "account_password" not in body["details"]

    page = api.get("/v1/clients", headers=H).json()
    assert page["total"] == 2 and page["next_offset"] is None
    assert [c["client_id"] for c in
            api.get("/v1/clients?flow=live", headers=H).json()["items"]] == ["live-1"]
    assert api.get("/v1/clients?limit=1", headers=H).json()["next_offset"] == 1


def test_ids_are_unique_across_flows(api):
    assert api.post("/v1/clients", headers=H, json=LIVE).status_code == 201
    clash = api.post("/v1/clients", headers=H, json={**WAITLIST, "client_id": "live-1"})
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "conflict"


def test_edit_enable_and_timeline_a_live_client(api):
    api.post("/v1/clients", headers=H, json=LIVE)
    later = (date.today() + timedelta(days=25)).isoformat()
    r = api.patch("/v1/clients/live-1", headers=H, json={"date_to": later})
    assert r.status_code == 200 and r.json()["date_to"] == later

    assert api.post("/v1/clients/live-1/enable", headers=H).json()["enabled"] is True
    events = api.get("/v1/clients/live-1/timeline", headers=H).json()["items"]
    assert [e["event"] for e in events][:2] == ["enabled", "edited"]

    flow_change = api.patch("/v1/clients/live-1", headers=H, json={"flow": "waitlist"})
    assert flow_change.status_code == 422


def test_waitlist_only_endpoints_refuse_a_live_client(api):
    api.post("/v1/clients", headers=H, json=LIVE)
    assert api.get("/v1/clients/live-1/journal", headers=H).status_code == 422


# --------------------------------------------------------------------------- #
# Manual booking run                                                           #
# --------------------------------------------------------------------------- #


def test_a_manual_run_of_a_live_client_runs_that_request(api, monkeypatch):
    api.post("/v1/clients", headers=H, json=LIVE)
    captured = {}

    async def fake_trigger(payload, idempotency_key=None):
        captured["args"] = payload.to_cli_args()
        return {"accepted": True}
    monkeypatch.setattr("src.api.modules.booking.handlers.trigger_booking", fake_trigger)

    r = api.post("/v1/booking/runs", headers=H,
                 json={"client_id": "live-1", "mode": "walk"})
    assert r.status_code == 202, r.text
    args = captured["args"]
    assert args[args.index("--request") + 1] == "live-1"
    assert "--walk" in args and "--commit" not in args
    assert args[args.index("-dc") + 1] == "NOR"


def test_a_commit_run_still_needs_confirm(api):
    api.post("/v1/clients", headers=H, json=LIVE)
    r = api.post("/v1/booking/runs", headers=H,
                 json={"client_id": "live-1", "mode": "commit"})
    assert r.status_code == 422 and "confirm" in r.json()["error"]["message"]


# --------------------------------------------------------------------------- #
# Switches, readiness, audit                                                   #
# --------------------------------------------------------------------------- #


def test_switches_patch_writes_only_what_was_sent(api, monkeypatch):
    written = {}
    monkeypatch.setattr("src.api.modules.waitlist.status._write_ini_switches", written.update)
    r = api.patch("/v1/switches", headers=H, json={"test_booking": True})
    assert r.status_code == 200 and written == {"test_booking": True}
    assert "NEXT run" in r.json()["note"]
    assert api.patch("/v1/switches", headers=H,
                     json={"nonsense": True}).status_code == 422


def test_readiness_lists_named_checks(api):
    r = api.get("/v1/health/ready", headers=H)
    assert r.status_code in (200, 503)
    names = {c["name"] for c in r.json()["checks"]}
    assert {"switches", "company_card", "imap", "telegram", "slot_checker"} <= names


def test_mutations_are_audited_with_the_actor_and_never_the_body(api):
    api.post("/v1/clients", headers=H, json=LIVE)
    rows = api.get("/v1/audit", headers=H).json()["items"]
    row = rows[0]
    assert row["actor"] == "agent.test@example.com"
    assert row["method"] == "POST" and row["path"] == "/v1/clients"
    raw = (api.tmp / "audit.jsonl").read_text(encoding="utf-8")
    assert "A1234567" not in raw and "pw-never-returned" not in raw


# --------------------------------------------------------------------------- #
# Job lanes                                                                    #
# --------------------------------------------------------------------------- #


def test_waitlist_and_booking_jobs_do_not_block_each_other(tmp_path, monkeypatch):
    import asyncio

    from src.api.core.config import ApiSettings
    from src.api.modules.jobs.manager import JobAlreadyRunningError, JobManager

    settings = ApiSettings(
        secret_token="d" * 64,
        job_command=[sys.executable, "-c", "import time; time.sleep(5)"],
        job_log_dir=tmp_path / "logs",
        job_history_file=tmp_path / "logs" / "jobs.jsonl")
    monkeypatch.setattr("src.api.modules.jobs.manager._machine_lock_busy", lambda lane="waitlist": "")
    manager = JobManager(settings)

    async def scenario():
        await manager.trigger(lane="waitlist")
        await manager.trigger(command_override=[sys.executable, "-c",
                                                "import time; time.sleep(5)"])
        assert set(manager.active_jobs()) == {"waitlist", "booking"}
        with pytest.raises(JobAlreadyRunningError):
            await manager.trigger(lane="waitlist")
        await manager.shutdown()

    asyncio.run(scenario())


def test_the_api_finds_the_repo_root():
    """config.py locates .env.api, logs/ and the job history from the repo
    root by counting parent folders. Moving the file one level deeper once made
    it look in src/ — the server then could not find its token and would not
    start, while every test (which sets the token in the environment) passed."""
    from src.api.core.config import REPO_ROOT

    assert (REPO_ROOT / "src" / "api" / "main.py").is_file()
    assert (REPO_ROOT / "config").is_dir()
