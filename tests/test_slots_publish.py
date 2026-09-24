"""Pushing the slot board to the web app's ingest API.

The rules worth protecting:

  * A push that fails must never fail the run. The board is a nicety; checking
    slots is the job.
  * Off by default. A feature that posts data to a third party does not turn
    itself on because a default said so.
  * The envelope carries a schema, so the web app can refuse a shape it does not
    know instead of rendering blanks.
  * The key falls back to the announcement key, because both go to the same host
    and two copies only create a way to drift.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.slots import db, publish  # noqa: E402

GST = timezone(timedelta(hours=4))


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "p.db"))
    c.execute(
        "INSERT INTO combos (combo_key, route, source_code, dest_code, country_name,"
        " centre, city, category, sub_category, visa_type, purpose, config_label,"
        " enabled, config_order, in_config)"
        " VALUES ('k1','AE-XXX','AE','XXX','Example','Dubai','Dubai','Short Stay',"
        " '','Short Stay','any','Dubai',1,0,1)")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def settings(monkeypatch):
    """Config values this module reads, without touching the real config."""
    values = {}

    def fake(section, key, default=None):
        return values.get((section, key), default)

    monkeypatch.setattr(publish, "get_config_value", fake)
    return values


# ===== the envelope ========================================================


def test_the_body_carries_the_board_and_a_schema(conn, settings):
    body = publish.build_body(conn, days=7)
    assert body["kind"] == "slot_board"
    assert body["schema"] == publish.SCHEMA
    assert body["board"]["schema"] == publish.SCHEMA
    assert isinstance(body["board"]["views"], list)
    assert body["generated_iso"] == body["board"]["generated_iso"]


def test_the_body_is_json_serialisable(conn, settings):
    """It goes out over HTTP; a stray non-serialisable value would fail at send."""
    body = publish.build_body(conn, days=7)
    assert json.loads(json.dumps(body))["kind"] == "slot_board"


def test_every_view_reaches_the_body(conn, settings):
    body = publish.build_body(conn, days=7)
    names = [v["name"] for v in body["board"]["views"]]
    assert names == ["Tourist", "Business", "Waitlist"]


# ===== configuration =======================================================


def test_the_push_is_off_unless_it_is_turned_on(settings):
    assert publish._enabled() is False
    assert publish.is_configured() is False


def test_the_key_falls_back_to_the_announcement_key(settings):
    settings[("tv_board", "enabled")] = "true"
    settings[("tv_announce", "api_key")] = "shared-key"
    assert publish._api_key() == "shared-key"
    assert publish.is_configured() is True


def test_its_own_key_wins_when_both_are_set(settings):
    settings[("tv_board", "api_key")] = "board-key"
    settings[("tv_announce", "api_key")] = "announce-key"
    assert publish._api_key() == "board-key"


def test_enabled_without_a_key_is_not_configured(settings):
    settings[("tv_board", "enabled")] = "true"
    assert publish._enabled() is True
    assert publish.is_configured() is False


def test_a_nonsense_number_falls_back_rather_than_crashing(settings):
    settings[("tv_board", "timeout_seconds")] = "soon"
    settings[("tv_board", "days")] = "lots"
    assert publish._timeout() == 15.0
    assert publish._days() > 0


# ===== sending =============================================================


class _Resp:
    def __init__(self, body):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_plain_200_counts_as_accepted(settings, monkeypatch):
    """The receiver's job is to store a blob.

    Insisting on an {"ok": true} body would make this report failure against a
    perfectly good endpoint that answers 200 with nothing.
    """
    settings[("tv_board", "api_key")] = "k"
    monkeypatch.setattr(publish.urllib.request, "urlopen", lambda *a, **k: _Resp(""))
    assert publish.send({"board": {"views": []}}) is True


def test_an_explicit_not_ok_is_a_failure(settings, monkeypatch):
    settings[("tv_board", "api_key")] = "k"
    monkeypatch.setattr(publish.urllib.request, "urlopen",
                        lambda *a, **k: _Resp('{"ok": false, "error": "nope"}'))
    assert publish.send({"board": {"views": []}}) is False


def test_a_network_failure_is_swallowed(settings, monkeypatch):
    """Never raises. The caller is the end of a run that otherwise succeeded."""
    settings[("tv_board", "api_key")] = "k"

    def boom(*a, **k):
        raise OSError("no route to host")

    monkeypatch.setattr(publish.urllib.request, "urlopen", boom)
    assert publish.send({"board": {"views": []}}) is False


def test_an_http_error_is_swallowed(settings, monkeypatch):
    settings[("tv_board", "api_key")] = "k"

    def boom(*a, **k):
        raise publish.urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(publish.urllib.request, "urlopen", boom)
    assert publish.send({"board": {"views": []}}) is False


def test_the_request_carries_the_key_and_is_a_post(settings, monkeypatch):
    settings[("tv_board", "api_key")] = "secret"
    settings[("tv_board", "url")] = "https://example.test/api/tv/ingest/board"
    seen = {}

    def capture(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp('{"ok": true}')

    monkeypatch.setattr(publish.urllib.request, "urlopen", capture)
    assert publish.send({"board": {"views": []}, "kind": "slot_board"}) is True
    assert seen["url"] == "https://example.test/api/tv/ingest/board"
    assert seen["method"] == "POST"
    assert seen["headers"]["x-api-key"] == "secret"
    assert seen["headers"]["content-type"] == "application/json"
    assert seen["body"]["kind"] == "slot_board"


# ===== the run must survive it =============================================


def test_push_quietly_does_nothing_when_it_is_off(settings, monkeypatch):
    def never(*a, **k):
        raise AssertionError("must not send while disabled")

    monkeypatch.setattr(publish.urllib.request, "urlopen", never)
    assert publish.push_quietly() is False


def test_push_quietly_survives_a_missing_database(settings, monkeypatch):
    settings[("tv_board", "enabled")] = "true"
    settings[("tv_board", "api_key")] = "k"
    monkeypatch.setattr(publish.db, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no db")))
    assert publish.push_quietly() is False


def test_push_quietly_sends_the_board_when_configured(tmp_path, conn, settings,
                                                      monkeypatch):
    settings[("tv_board", "enabled")] = "true"
    settings[("tv_board", "api_key")] = "k"
    sent = {}

    def capture(req, timeout=None):
        sent["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp('{"ok": true}')

    monkeypatch.setattr(publish.urllib.request, "urlopen", capture)
    monkeypatch.setattr(publish.db, "connect", lambda *a, **k: conn)
    assert publish.push_quietly(db_path=str(tmp_path / "p.db")) is True
    assert sent["body"]["kind"] == "slot_board"
    assert sent["body"]["schema"] == publish.SCHEMA
