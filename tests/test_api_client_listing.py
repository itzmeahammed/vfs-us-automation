"""Client timestamps, and what `GET /clients` can tell you in one call.

Two things are pinned here.

**Timestamps are the store's, not the caller's.** A web app that GETs a client
and PUTs it back would otherwise write its own stale created_at straight over
ours, and "when was this created?" would mean "whatever was last claimed". The
store therefore strips them from input and writes them from its own clock. The
subtle half is `merge=False` (a true PUT): the caller cannot send created_at
and nothing else carries it forward, so without care a replace silently loses
the creation date of every client it touches.

**The listing answers fleet questions without N+1.** Before `?include=`, a
dashboard had to call GET /clients and then GET /clients/{id} once per client
to learn whether any of them were broken. The extras are opt-in rather than
always-on because each costs real work per client — the pre-flight runs route
readiness and placeholder checks — so the cheap default stays cheap for the
callers that only need a picker.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# setdefault, NOT assignment: the token is process-wide, and overwriting one
# another test file already captured into its own HEADERS would make every
# later request in the run fail auth.
os.environ.setdefault("VFSAPI_SECRET_TOKEN", "c" * 64)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

from src.waitlist import store  # noqa: E402

HEADERS = {"X-Webhook-Secret-Token": os.environ["VFSAPI_SECRET_TOKEN"]}

CLIENT = {
    "route": "AE-CZE",
    "combos": ["Dubai - Tourism"],
    "enabled": False,
    "first_name": "TRAV",
    "last_name": "NOOK",
    "gender": "Male",
    "nationality": "India",
    "passport_number": "A12945678",
    "passport_expiry": "2030-07-05",
    "phone_country_code": "971",
    "phone_number": "556024553",
    "email": "a@b.com",
    "account": "x@y.com",
    "account_password": "p",
}


@pytest.fixture
def client_id(tmp_path, monkeypatch):
    """A throwaway client, written to a temp dir so real files are untouched.

    Patched by dotted path (not on the imported module object) so the reader
    and the writer both follow, matching tests/test_api_clients.py.
    """
    monkeypatch.setattr("src.waitlist.registrant.REGISTRANT_DIR",
                        str(tmp_path), raising=False)
    monkeypatch.setattr("src.waitlist.store.REGISTRANT_DIR",
                        str(tmp_path), raising=False)
    monkeypatch.setattr("src.waitlist.store.path_for",
                        lambda rid: str(tmp_path / f"{rid}.json"),
                        raising=False)
    return "zz-stamp"


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------


def test_create_stamps_both_dates(client_id):
    store.create(client_id, dict(CLIENT))
    data = store.get_raw(client_id)
    assert data["created_at"] == data["updated_at"]
    assert data["created_at"].endswith("Z"), "timestamps are UTC ISO 8601"


def test_a_parked_client_has_no_enabled_at(client_id):
    store.create(client_id, dict(CLIENT))
    assert "enabled_at" not in store.get_raw(client_id), (
        "a client that has never been armed must not carry an arming date"
    )


def test_creating_an_armed_client_stamps_enabled_at(client_id):
    store.create(client_id, dict(CLIENT, enabled=True))
    data = store.get_raw(client_id)
    assert data["enabled_at"] == data["created_at"]


def test_update_moves_updated_at_but_never_created_at(client_id):
    store.create(client_id, dict(CLIENT))
    created = store.get_raw(client_id)["created_at"]
    time.sleep(1.1)                      # second-precision stamps
    store.update(client_id, {"first_name": "CHANGED"}, merge=True)
    data = store.get_raw(client_id)
    assert data["created_at"] == created
    assert data["updated_at"] > created


def test_a_replace_does_not_lose_created_at(client_id):
    """merge=False is where creation time would silently disappear."""
    store.create(client_id, dict(CLIENT))
    created = store.get_raw(client_id)["created_at"]
    time.sleep(1.1)
    store.update(client_id, dict(CLIENT), merge=False)
    assert store.get_raw(client_id)["created_at"] == created


def test_caller_supplied_timestamps_are_ignored(client_id):
    """Otherwise the audit trail is whatever the last caller claimed."""
    store.create(client_id, dict(CLIENT))
    created = store.get_raw(client_id)["created_at"]
    store.update(client_id, {
        "created_at": "1999-01-01T00:00:00Z",
        "updated_at": "1999-01-01T00:00:00Z",
    }, merge=True)
    data = store.get_raw(client_id)
    assert data["created_at"] == created
    assert not data["updated_at"].startswith("1999")


def test_enabled_at_marks_arming_not_every_edit(client_id):
    store.create(client_id, dict(CLIENT))
    store.set_enabled(client_id, True)
    armed = store.get_raw(client_id)["enabled_at"]
    time.sleep(1.1)
    store.update(client_id, {"first_name": "CHANGED"}, merge=True)
    assert store.get_raw(client_id)["enabled_at"] == armed, (
        "editing an armed client is not a re-arming"
    )


def test_disabling_clears_enabled_at(client_id):
    """A stale arming date on a parked client reads as though it were live."""
    store.create(client_id, dict(CLIENT, enabled=True))
    store.set_enabled(client_id, False)
    assert "enabled_at" not in store.get_raw(client_id)


def test_backfill_only_touches_untimestamped_clients(client_id, tmp_path):
    (tmp_path / "zz-old.json").write_text(
        json.dumps({"route": "AE-CZE", "combos": ["Dubai - Tourism"]}),
        encoding="utf-8")
    store.create(client_id, dict(CLIENT))

    changed = store.backfill_timestamps()
    assert "zz-old" in changed
    assert client_id not in changed, "an already-stamped client is left alone"
    assert store.get_raw("zz-old")["created_at"]

    assert store.backfill_timestamps() == [], "backfill must be idempotent"


def test_backfill_gives_an_armed_client_an_arming_date(client_id, tmp_path):
    """enabled=true with a blank enabled_at renders as a contradiction."""
    (tmp_path / "zz-armed.json").write_text(
        json.dumps({"route": "AE-CZE", "combos": ["Dubai - Tourism"],
                    "enabled": True}),
        encoding="utf-8")
    store.backfill_timestamps()
    assert store.get_raw("zz-armed")["enabled_at"]


# --------------------------------------------------------------------------
# The listing
# --------------------------------------------------------------------------


@pytest.fixture
def api():
    """A TestClient reading the REAL client files — this listing is about them.

    Unlike the store fixtures above, these tests assert on shape rather than on
    specific clients, so pointing at a temp dir would test nothing useful.
    """
    import src.api.config as config_mod
    config_mod.get_settings.cache_clear()

    # The rate limiter is a per-process singleton — correct in production, but
    # it makes a whole test file look like one client flooding the endpoint,
    # and it leaks into every test that runs after this one. Reset it per test
    # rather than weakening the limit.
    from src.api.security import _reset_rate_limiter
    _reset_rate_limiter()

    from fastapi.testclient import TestClient
    import src.api.main as main_mod
    return TestClient(main_mod.app, raise_server_exceptions=False)



def test_the_default_listing_stays_cheap(api):
    """No pre-flight, no journal — a picker should not pay for a dashboard."""
    body = api.get("/clients", headers=HEADERS).json()
    assert body["included"] == []
    for row in body["clients"]:
        assert row["runnable"] is None
        assert row["last_status"] is None
        # ...but timestamps are free, so they are always there.
        assert row["created_at"]


def test_include_status_reports_runnability(api):
    body = api.get("/clients?include=status", headers=HEADERS).json()
    assert body["included"] == ["status"]
    for row in body["clients"]:
        assert isinstance(row["runnable"], bool)
        assert isinstance(row["problem_count"], int)
        assert row["last_status"] is None, "journal was not requested"


def test_include_journal_reports_the_last_run(api):
    body = api.get("/clients?include=journal", headers=HEADERS).json()
    assert body["included"] == ["journal"]
    for row in body["clients"]:
        assert isinstance(row["run_count"], int)
        assert row["runnable"] is None, "status was not requested"


def test_include_all_is_both(api):
    body = api.get("/clients?include=all", headers=HEADERS).json()
    assert body["included"] == ["journal", "status"]


def test_enabled_filter(api):
    body = api.get("/clients?enabled=true", headers=HEADERS).json()
    assert all(r["enabled"] for r in body["clients"])
    assert body["count"] == len(body["clients"])


def test_runnable_filter_implies_the_status_work(api):
    """Filtering on a field the caller did not ask to compute must still work."""
    body = api.get("/clients?runnable=true", headers=HEADERS).json()
    assert "status" in body["included"]
    assert all(r["runnable"] for r in body["clients"])


def test_listing_still_needs_a_token(api):
    assert api.get("/clients?include=all").status_code == 401
