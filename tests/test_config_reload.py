"""A long-lived process must see a config file that changed under it.

The bug this pins: PATCH /v1/switches {"live_booking": true} wrote
config.local.ini and answered 200 with "live_booking": false, and went on
saying false until the API was restarted. Every slot-check run is a fresh
process, so the paying flow was ON while the panel showed it OFF.

  * initialize_config() stays idempotent (a run keeps its starting values);
  * reload_config() really re-reads, and keeps set_config_value() overrides;
  * reload_if_changed() notices a hand edit;
  * the switches endpoint answers with the value now in effect, and refuses
    to say "updated" when an environment variable still overrides the file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils import config_reader  # noqa: E402


@pytest.fixture()
def config_dir(tmp_path, monkeypatch):
    """A private config/ under tmp_path, with the reader's globals restored after."""
    import src.settings as settings_mod

    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "config.ini").write_text(
        "[switches]\nlive_booking = false\ntest_booking = true\n", encoding="utf-8")
    (directory / "config.local.ini").write_text(
        "; operator notes\n[waitlist]\ncooldown_hours = 2\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VFS_BOT_CONFIG_PATH", raising=False)
    monkeypatch.delenv("VFSCFG_SWITCHES__LIVE_BOOKING", raising=False)
    monkeypatch.setattr(config_reader, "_config", None)
    monkeypatch.setattr(config_reader, "_config_dir", "config")
    monkeypatch.setattr(config_reader, "_loaded_signature", None)
    monkeypatch.setattr(config_reader, "_runtime_overrides", {})
    monkeypatch.setattr(settings_mod, "_cached", None)
    config_reader.initialize_config("config")
    return directory


def _turn_live_booking_on(directory):
    (directory / "config.local.ini").write_text(
        "; operator notes\n[waitlist]\ncooldown_hours = 2\n\n"
        "[switches]\nlive_booking = true\n", encoding="utf-8")


def test_initialize_is_idempotent_but_reload_reads_the_file(config_dir):
    _turn_live_booking_on(config_dir)

    config_reader.initialize_config("config")
    assert config_reader.get_config_value("switches", "live_booking") == "false"

    config_reader.reload_config()
    assert config_reader.get_config_value("switches", "live_booking") == "true"


def test_reload_keeps_runtime_overrides(config_dir):
    config_reader.set_config_value("browser", "cdp_url", "http://127.0.0.1:9222")
    _turn_live_booking_on(config_dir)

    config_reader.reload_config()
    assert config_reader.get_config_value("browser", "cdp_url") == "http://127.0.0.1:9222"


def test_reload_if_changed_only_reloads_after_an_edit(config_dir):
    assert config_reader.reload_if_changed() is False

    _turn_live_booking_on(config_dir)
    assert config_reader.reload_if_changed() is True
    assert config_reader.get_config_value("switches", "live_booking") == "true"
    assert config_reader.reload_if_changed() is False


def test_a_file_that_does_not_parse_keeps_the_previous_config(config_dir):
    (config_dir / "config.local.ini").write_text(
        "live_booking = true\n", encoding="utf-8")       # no section header

    with pytest.raises(Exception):
        config_reader.reload_if_changed()
    assert config_reader.get_config_value("switches", "live_booking") == "false"
    assert config_reader.reload_if_changed() is False     # reported once


def test_switch_update_answers_with_the_value_now_in_effect(config_dir):
    from src.api.modules.system.schemas import SwitchesUpdateRequest
    from src.api.modules.waitlist.status import update_switches

    result = update_switches(SwitchesUpdateRequest(live_booking=True))

    assert result.switches.live_booking is True
    text = (config_dir / "config.local.ini").read_text(encoding="utf-8")
    assert "live_booking = true" in text and "; operator notes" in text
    assert not os.path.exists(config_dir / "config.local.ini.tmp")


def test_switch_update_says_so_when_an_env_var_overrides_the_file(config_dir, monkeypatch):
    from fastapi import HTTPException

    from src.api.modules.system.schemas import SwitchesUpdateRequest
    from src.api.modules.waitlist.status import update_switches

    monkeypatch.setenv("VFSCFG_SWITCHES__LIVE_BOOKING", "false")
    with pytest.raises(HTTPException) as caught:
        update_switches(SwitchesUpdateRequest(live_booking=True))
    assert caught.value.status_code == 409
    assert "live_booking" in caught.value.detail
