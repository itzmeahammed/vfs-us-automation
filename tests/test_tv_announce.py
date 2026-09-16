"""Tests for the TV announce sender (France/Italy slot finds -> web app TV)."""

import json
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.utils import tv_announce  # noqa: E402

CONFIG = {
    "enabled": "true",
    "url": "https://example.test/announce",
    "api_key": "test-key",
    "destinations": "FRA, ITA",
}


def _fake_config(section, key, default=None):
    return CONFIG.get(key, default) if section == "tv_announce" else default


def _response(body: dict):
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.__enter__.return_value = resp
    return resp


REPORT = [
    ({"centre": "Abu Dhabi", "category": "Short Stay", "sub_category": "Tourist"},
     "Earliest available slot for 1 Applicants is : 11-08-2026"),
    ({"centre": "Dubai", "category": "Short Stay", "sub_category": "Business"},
     "No slot message shown (no availability?)."),
]


@mock.patch.object(tv_announce, "get_config_value", side_effect=_fake_config)
def test_france_and_italy_qualify_others_do_not(_cfg):
    assert tv_announce.applies_to("FRA")
    assert tv_announce.applies_to("IT")      # matched by country name
    assert not tv_announce.applies_to("DEU")


@mock.patch.object(tv_announce, "get_config_value", side_effect=_fake_config)
def test_posts_payload_with_api_key(_cfg):
    with mock.patch("urllib.request.urlopen",
                    return_value=_response({"ok": True, "duplicate": False})) as urlopen:
        assert tv_announce.announce_slots("FRA", REPORT) is True

    req = urlopen.call_args[0][0]
    assert req.full_url == CONFIG["url"]
    assert req.get_method() == "POST"
    assert req.get_header("X-api-key") == "test-key"
    assert req.get_header("Content-type") == "application/json"
    body = json.loads(req.data.decode("utf-8"))
    assert body["kind"] == "urgent"
    assert body["title"] == "France Slot Update:"
    # Only the combo with a slot, centre + category, no date.
    assert body["message"] == "France - Abu Dhabi - Short Stay - Tourist"


@mock.patch.object(tv_announce, "get_config_value", side_effect=_fake_config)
def test_other_destination_or_empty_report_sends_nothing(_cfg):
    with mock.patch("urllib.request.urlopen") as urlopen:
        assert tv_announce.announce_slots("DEU", REPORT) is False
        assert tv_announce.announce_slots("FRA", REPORT[1:]) is False
    urlopen.assert_not_called()


@mock.patch.object(tv_announce, "get_config_value", side_effect=_fake_config)
def test_network_failure_never_raises(_cfg):
    with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert tv_announce.announce_slots("ITA", REPORT) is False


def test_missing_key_skips_send():
    cfg = {**CONFIG, "api_key": ""}
    with mock.patch.object(tv_announce, "get_config_value",
                           side_effect=lambda s, k, d=None: cfg.get(k, d)), \
         mock.patch("urllib.request.urlopen") as urlopen:
        assert tv_announce.announce_slots("FRA", REPORT) is False
    urlopen.assert_not_called()
