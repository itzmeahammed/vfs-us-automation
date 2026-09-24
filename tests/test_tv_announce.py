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
    "priority": "Short Stay (any purpose), Tourist, Tourism",
    "title_format": "{country} {visa_type} Available",
}


def _fake_config(section, key, default=None):
    return CONFIG.get(key, default) if section == "tv_announce" else default


def _response(body: dict):
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.__enter__.return_value = resp
    return resp


def _bodies(urlopen):
    """The JSON payloads POSTed, in call order."""
    return [json.loads(call[0][0].data.decode("utf-8"))
            for call in urlopen.call_args_list]


REPORT = [
    ({"centre": "Abu Dhabi", "category": "Short Stay", "sub_category": "Tourist"},
     "Earliest available slot for 1 Applicants is : 11-08-2026"),
    ({"centre": "Dubai", "category": "Short Stay", "sub_category": "Business"},
     "No slot message shown (no availability?)."),
]

# France as the bot actually reads it, tv_title/tv_category included exactly as
# config/routes/AE-FRA.json sets them: business listed first in the route file,
# both open. The tourist row must still be announced first.
FRANCE_BOTH_OPEN = [
    ({"centre": "Abu Dhabi", "category": "Short Stay - Business", "sub_category": "",
      "tv_title": "Business"},
     "Earliest available slot for 1,2 applicants is : 27-10-2026"),
    ({"centre": "Abu Dhabi", "category": "Short Stay (any purpose)", "sub_category": "",
      "tv_title": "Tourist", "tv_category": "Short Stay"},
     "Earliest available slot for 1,2,3,4,5 applicants is : 28-09-2026"),
    ({"centre": "Temporary Enrolment Location - Damac Hills",
      "category": "Temporary Location-Damac Hills", "sub_category": ""},
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
    # The title carries the visa type: the server deduplicates on it.
    assert body["title"] == "France Short Stay - Tourist Available"
    # Only the combo with a slot, centre + category, no date.
    assert body["message"] == "France - Abu Dhabi - Short Stay - Tourist"


@mock.patch.object(tv_announce, "get_config_value", side_effect=_fake_config)
def test_one_call_per_open_combination_any_purpose_first(_cfg):
    with mock.patch("urllib.request.urlopen",
                    return_value=_response({"ok": True})) as urlopen:
        assert tv_announce.announce_slots("FRA", FRANCE_BOTH_OPEN) is True

    # Two open combinations -> two POSTs. The closed Damac Hills one is dropped.
    assert urlopen.call_count == 2
    titles = [b["title"] for b in _bodies(urlopen)]
    assert titles == [
        "France Tourist Available",
        "France Business Available",
    ]
    messages = [b["message"] for b in _bodies(urlopen)]
    assert messages == [
        "France - Abu Dhabi - Short Stay",
        "France - Abu Dhabi - Short Stay - Business",
    ]


@mock.patch.object(tv_announce, "get_config_value", side_effect=_fake_config)
def test_tv_wording_comes_from_the_route_file_not_the_portal(_cfg):
    """The route file renames a combination for the screen; nothing else moves."""
    combo = {"centre": "Abu Dhabi", "category": "Short Stay (any purpose)",
             "sub_category": "", "tv_title": "Tourist", "tv_category": "Short Stay"}
    payload = tv_announce.build_announcement("FRA", combo)
    assert payload["title"] == "France Tourist Available"
    assert payload["message"] == "France - Abu Dhabi - Short Stay"

    # Without the overrides the portal's own wording is used, unchanged.
    plain = {k: v for k, v in combo.items() if not k.startswith("tv_")}
    payload = tv_announce.build_announcement("FRA", plain)
    assert payload["title"] == "France Short Stay (any purpose) Available"
    assert payload["message"] == "France - Abu Dhabi - Short Stay (any purpose)"


def test_renaming_for_the_tv_does_not_reclassify_the_slot():
    """registry.purpose() keeps any-purpose as `any`, so it still counts for
    business clients on the dashboard even though the TV says 'Tourist'."""
    from src.slots import registry
    assert registry.purpose("Short Stay (any purpose)", "") == registry.ANY_PURPOSE
    assert registry.purpose("Short Stay - Business", "") == registry.BUSINESS


@mock.patch.object(tv_announce, "get_config_value", side_effect=_fake_config)
def test_unprioritised_categories_keep_the_order_they_were_checked_in(_cfg):
    entries = [
        ({"centre": "Dubai", "category": "Schengen Visa", "sub_category": ""},
         "Earliest available slot is : 01-10-2026"),
        ({"centre": "Dubai", "category": "Short Stay", "sub_category": "Tourist"},
         "Earliest available slot is : 02-10-2026"),
        ({"centre": "Abu Dhabi", "category": "Long Stay", "sub_category": ""},
         "Earliest available slot is : 03-10-2026"),
    ]
    payloads = tv_announce.build_announcements("ITA", entries)
    assert [p["title"] for p in payloads] == [
        "Italy Short Stay - Tourist Available",   # prioritised, jumps the queue
        "Italy Schengen Visa Available",          # then report order
        "Italy Long Stay Available",
    ]


@mock.patch.object(tv_announce, "get_config_value", side_effect=_fake_config)
def test_same_category_in_two_cities_gets_distinct_titles(_cfg):
    """Identical titles would be swallowed by the server's 5-minute suppression."""
    entries = [
        ({"centre": "Abu Dhabi", "category": "Short Stay", "sub_category": "Tourist"},
         "Earliest available slot is : 01-10-2026"),
        ({"centre": "Dubai", "category": "Short Stay", "sub_category": "Tourist"},
         "Earliest available slot is : 02-10-2026"),
    ]
    titles = [p["title"] for p in tv_announce.build_announcements("ITA", entries)]
    assert titles == [
        "Italy Short Stay - Tourist Available (Abu Dhabi)",
        "Italy Short Stay - Tourist Available (Dubai)",
    ]
    assert len(set(titles)) == 2


def test_title_format_is_configurable():
    cfg = {**CONFIG, "title_format": "{city}: {visa_type} slots open ({country})"}
    with mock.patch.object(tv_announce, "get_config_value",
                           side_effect=lambda s, k, d=None: cfg.get(k, d)):
        payloads = tv_announce.build_announcements("FRA", FRANCE_BOTH_OPEN)
    assert [p["title"] for p in payloads] == [
        "Abu Dhabi: Tourist slots open (France)",
        "Abu Dhabi: Business slots open (France)",
    ]


def test_an_unusable_title_format_falls_back_instead_of_breaking_the_screen():
    cfg = {**CONFIG, "title_format": "{country} {nonsense} Available"}
    with mock.patch.object(tv_announce, "get_config_value",
                           side_effect=lambda s, k, d=None: cfg.get(k, d)):
        payloads = tv_announce.build_announcements("FRA", FRANCE_BOTH_OPEN)
    assert [p["title"] for p in payloads] == [
        "France Tourist Available",
        "France Business Available",
    ]


@mock.patch.object(tv_announce, "get_config_value", side_effect=_fake_config)
def test_a_failed_announcement_does_not_cancel_the_rest(_cfg):
    responses = [OSError("down"), _response({"ok": True})]
    with mock.patch("urllib.request.urlopen", side_effect=responses) as urlopen:
        assert tv_announce.announce_slots("FRA", FRANCE_BOTH_OPEN) is False
    assert urlopen.call_count == 2


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


@mock.patch.object(tv_announce, "get_config_value", side_effect=_fake_config)
def test_the_real_france_route_file_announces_tourist_then_business(_cfg):
    """End to end on the shipped route file, so an edit there cannot quietly
    put 'Short Stay (any purpose)' back on the screen."""
    import json as _json
    route = _json.loads((REPO_ROOT / "config" / "routes" / "AE-FRA.json").read_text("utf-8"))
    combos = [c for c in route["slot_check"]["combinations"] if not c.get("disabled")]
    entries = [(c, "Earliest available slot is : 28-09-2026") for c in combos]

    payloads = tv_announce.build_announcements("FRA", entries)
    assert [p["title"] for p in payloads][:2] == [
        "France Tourist Available",
        "France Business Available",
    ]
    assert [p["message"] for p in payloads][:2] == [
        "France - Abu Dhabi - Short Stay",
        "France - Abu Dhabi - Short Stay - Business",
    ]
    # Every title distinct, or the server's 5-minute suppression eats one.
    assert len({p["title"] for p in payloads}) == len(payloads)


def test_missing_key_skips_send():
    cfg = {**CONFIG, "api_key": ""}
    with mock.patch.object(tv_announce, "get_config_value",
                           side_effect=lambda s, k, d=None: cfg.get(k, d)), \
         mock.patch("urllib.request.urlopen") as urlopen:
        assert tv_announce.announce_slots("FRA", REPORT) is False
    urlopen.assert_not_called()
