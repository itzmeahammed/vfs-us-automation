"""The inbox watcher as a service: heartbeat, one cycle, and its API.

The rules that must hold:
  * every cycle writes a heartbeat a panel can read; a stale or stopped
    heartbeat is not 'live', so the supervisor falls back to its own pass;
  * a cycle acts on invitations only while [switches] invite_booking is on,
    but always READS (invitations and confirmations are recorded either way);
  * two callers never run a pass at once — the second skips;
  * GET /v1/inbox/status and /confirmations answer with the token and only
    with the token; POST /v1/inbox/pass reports 409 when a pass is running.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "d" * 64)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

from src.inbox import heartbeat, service  # noqa: E402
from src.inbox.watcher import PassResult  # noqa: E402

HEADERS = {"X-Webhook-Secret-Token": os.environ["VFSAPI_SECRET_TOKEN"]}


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(heartbeat, "FILE", str(tmp_path / "inbox_watcher.json"))
    monkeypatch.setattr(service, "LOCK_PATH", str(tmp_path / "inbox_pass"))
    return tmp_path


@pytest.fixture()
def fake_pass(monkeypatch):
    """run_pass -> a canned PassResult; process_pending -> records the call."""
    import src.inbox.watcher as watcher_mod
    from src.booking import invites
    from src.inbox import report as report_mod

    calls: Dict[str, Any] = {"process_pending": 0}
    result = PassResult(mailboxes_checked=3, messages_seen=12)
    monkeypatch.setattr(watcher_mod, "run_pass", lambda *a, **k: result)
    monkeypatch.setattr(report_mod, "report", lambda r: None)

    def fake_process():
        calls["process_pending"] += 1
        return ["inv-1"]

    monkeypatch.setattr(invites, "process_pending", fake_process)
    return calls


# --------------------------------------------------------------------------- #
# Heartbeat                                                                    #
# --------------------------------------------------------------------------- #

def test_a_fresh_heartbeat_is_live_and_a_stale_one_is_not(monkeypatch):
    assert heartbeat.read() == {}
    assert not heartbeat.is_live()

    heartbeat.write(state=heartbeat.IDLE, passes=1)
    assert heartbeat.is_live(max_age_seconds=60)
    assert heartbeat.age_seconds() < 5

    # Age the file by hand rather than faking time.time(): undoing a
    # monkeypatch inside the test would also undo the fixture that points the
    # heartbeat at tmp_path, and the next write would hit the real state/.
    import json
    data = heartbeat.read()
    data["updated_epoch"] = time.time() - 10_000
    with open(heartbeat.FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    assert not heartbeat.is_live(max_age_seconds=60)


def test_a_stopped_or_disabled_heartbeat_is_never_live():
    heartbeat.write(state=heartbeat.STOPPED)
    assert not heartbeat.is_live(max_age_seconds=10_000)
    heartbeat.write(state=heartbeat.DISABLED)
    assert not heartbeat.is_live(max_age_seconds=10_000)


def test_write_merges_and_never_raises_on_a_bad_dir(monkeypatch):
    heartbeat.write(a=1)
    heartbeat.write(b=2)
    assert heartbeat.read()["a"] == 1 and heartbeat.read()["b"] == 2
    monkeypatch.setattr(heartbeat, "STATE_DIR", "Z:\\nope\\nowhere")
    monkeypatch.setattr(heartbeat, "FILE", "Z:\\nope\\nowhere\\x.json")
    heartbeat.write(c=3)          # logs, does not raise


# --------------------------------------------------------------------------- #
# One cycle                                                                    #
# --------------------------------------------------------------------------- #

def test_a_cycle_reads_then_acts_when_the_switch_is_on(fake_pass, monkeypatch):
    from src.settings import settings
    monkeypatch.setattr(settings().switches, "invite_booking", True)

    summary = service.run_cycle("api")

    assert summary["mailboxes_checked"] == 3
    assert summary["acting"] is True
    assert summary["bookings_started"] == ["inv-1"]
    assert fake_pass["process_pending"] == 1
    hb = heartbeat.read()
    assert hb["state"] == heartbeat.IDLE and hb["passes"] == 1
    assert hb["last_pass"]["messages_seen"] == 12
    assert hb["next_pass_at"]                      # the API loop announces its next pass
    assert heartbeat.is_live()


def test_a_cycle_only_reads_when_the_switch_is_off(fake_pass, monkeypatch):
    from src.settings import settings
    monkeypatch.setattr(settings().switches, "invite_booking", False)

    summary = service.run_cycle("supervisor")

    assert summary["acting"] is False
    assert fake_pass["process_pending"] == 0
    assert heartbeat.read()["state"] == heartbeat.IDLE


def test_an_explicit_act_flag_overrides_the_switch(fake_pass, monkeypatch):
    from src.settings import settings
    monkeypatch.setattr(settings().switches, "invite_booking", True)
    service.run_cycle("manual", act=False)
    assert fake_pass["process_pending"] == 0


def test_a_failing_pass_is_recorded_not_raised(monkeypatch):
    import src.inbox.watcher as watcher_mod

    def boom():
        raise RuntimeError("imap down")

    monkeypatch.setattr(watcher_mod, "run_pass", boom)
    summary = service.run_cycle("api")
    assert "imap down" in summary["error"]
    hb = heartbeat.read()
    assert hb["state"] == heartbeat.ERROR and "imap down" in hb["last_error"]
    assert heartbeat.is_live()                    # alive, just unhappy


def test_a_second_caller_skips_while_a_pass_is_running(fake_pass, monkeypatch):
    from contextlib import contextmanager
    from src.utils import filelock

    @contextmanager
    def busy(path, timeout=0):
        raise filelock.LockTimeout("busy")
        yield  # pragma: no cover

    monkeypatch.setattr(filelock, "locked", busy)
    summary = service.run_cycle("supervisor")
    assert summary["skipped"]
    assert fake_pass["process_pending"] == 0
    assert heartbeat.read() == {}                 # touched nothing


# --------------------------------------------------------------------------- #
# The background loop                                                          #
# --------------------------------------------------------------------------- #

def test_the_background_loop_idles_when_disabled_and_runs_when_enabled(monkeypatch):
    from src.inbox import background
    from src.settings import settings

    ran: List[str] = []
    monkeypatch.setattr(service, "run_cycle", lambda trigger: ran.append(trigger))
    monkeypatch.setattr(background, "FIRST_DELAY_SECONDS", 0)
    monkeypatch.setattr(background, "IDLE_SECONDS", 0.01)
    monkeypatch.setattr(background, "MIN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(settings().inbox, "poll_seconds", 1)
    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "reload_from_disk", lambda: None)

    monkeypatch.setattr(settings().inbox, "background_watch", False)
    background._stop.clear()
    import threading
    t = threading.Thread(target=background._loop, daemon=True)
    t.start()
    time.sleep(0.1)
    assert heartbeat.read().get("state") == heartbeat.DISABLED
    assert ran == []

    monkeypatch.setattr(settings().inbox, "background_watch", True)
    time.sleep(0.2)
    background._stop.set()
    t.join(timeout=2)
    assert ran and ran[0] == "api"
    assert heartbeat.read().get("state") == heartbeat.STOPPED


# --------------------------------------------------------------------------- #
# API                                                                          #
# --------------------------------------------------------------------------- #

@pytest.fixture()
def api(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from src.api.main import app
    from src.booking import confirmations, invites

    monkeypatch.setattr(confirmations, "CONFIRM_DIR", str(tmp_path / "conf"))
    monkeypatch.setattr(invites, "INVITE_DIR", str(tmp_path / "inv"))
    import src.inbox.watcher as watcher_mod
    monkeypatch.setattr(watcher_mod, "mailbox_accounts",
                        lambda: [("one@example.com", "pw"), ("two@example.com", "pw")])
    # No `with`: the lifespan (and so the real watcher thread) never starts.
    return TestClient(app)


def test_status_needs_the_token(api):
    assert api.get("/v1/inbox/status").status_code in (401, 403)


def test_status_describes_a_watcher_that_never_ran(api):
    r = api.get("/v1/inbox/status", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["watcher"]["verdict"] == "never_ran"
    assert body["watcher"]["live"] is False
    assert body["mailboxes"]["count"] == 2
    assert body["mailboxes"]["watched"] == ["on***@example.com", "tw***@example.com"] \
        or all("example.com" in m for m in body["mailboxes"]["watched"])
    assert set(body["switches"]) == {"invite_booking", "test_booking",
                                     "background_watch", "poll_seconds"}
    assert body["invitations"]["pending"] == 0
    assert body["confirmations"]["matched"] == 0


def test_status_reflects_the_last_pass(api, fake_pass):
    service.run_cycle("api", act=False)
    body = api.get("/v1/inbox/status", headers=HEADERS).json()
    assert body["watcher"]["verdict"] == "live"
    assert body["last_pass"]["mailboxes_checked"] == 3
    assert body["watcher"]["passes"] == 1


def test_confirmations_list_is_paginated_and_masked(api, tmp_path):
    from src.booking import confirmations
    confirmations.save({"key": "k1", "route": "AE-NOR", "account": "holder@example.com",
                        "applicant_name": "A B", "reference": "NOR1", "received_epoch": 2,
                        "status": "matched", "settled": [], "notes": []})
    confirmations.save({"key": "k2", "route": "AE-NOR", "account": "holder@example.com",
                        "applicant_name": "C D", "reference": "", "received_epoch": 1,
                        "status": "unmatched", "settled": [], "notes": []})
    r = api.get("/v1/inbox/confirmations", headers=HEADERS)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["key"] for i in items] == ["k1", "k2"]          # newest first
    assert "holder@example.com" not in str(items)              # masked
    r = api.get("/v1/inbox/confirmations?status=unmatched", headers=HEADERS)
    assert [i["key"] for i in r.json()["items"]] == ["k2"]


def test_pass_now_runs_a_cycle_and_409s_when_busy(api, monkeypatch):
    monkeypatch.setattr(service, "run_cycle",
                        lambda trigger: {"trigger": trigger, "mailboxes_checked": 1})
    r = api.post("/v1/inbox/pass", headers=HEADERS)
    assert r.status_code == 200 and r.json()["trigger"] == "api-manual"

    monkeypatch.setattr(service, "run_cycle",
                        lambda trigger: {"skipped": "another inbox pass is running"})
    assert api.post("/v1/inbox/pass", headers=HEADERS).status_code == 409


def test_the_stream_sends_a_status_event_then_ends_at_its_ceiling(api, monkeypatch):
    """With the ceiling at zero the generator emits one status event and an
    `end`, which keeps the test finite — the shape is what matters here."""
    from src.api.modules.inbox import router as inbox_router
    monkeypatch.setattr(inbox_router, "STREAM_MAX_SECONDS", 0)

    r = api.get("/v1/inbox/stream?interval=3", headers=HEADERS)

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = [f for f in r.text.split("\n\n") if f.strip()]
    assert frames[0].startswith("event: status\ndata: {")
    assert frames[-1].startswith("event: end")
