"""Booking config loading, inheritance, and the validation that refuses.

Everything here is checked at LOAD time, before a browser exists, because the
alternative is discovering it half-way through a flow that has already touched a
client's account. The refusal tests are the point of the file.
"""

import json

import pytest

from src.booking import config as booking_config
from src.booking.errors import BookingConfigError


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    directory = tmp_path / "booking"
    directory.mkdir()
    monkeypatch.setattr(booking_config, "BOOKING_DIR", str(directory))
    booking_config.clear_cache()
    yield directory
    booking_config.clear_cache()


def write(directory, name, payload):
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def minimal(**overrides):
    """A valid two-step config: one harmless step, one committing step."""
    config = {
        "enabled": True,
        "steps": [
            {"name": "open", "type": "dashboard_resume"},
            {"name": "pick", "type": "slot_pick", "commits": True},
        ],
    }
    config.update(overrides)
    return config


# --------------------------------------------------------------------------- #
# The shipped configs                                                          #
# --------------------------------------------------------------------------- #

def test_the_shipped_configs_all_validate():
    """Guards the real files — a typo there is a production failure."""
    booking_config.clear_cache()
    assert booking_config.check() == []


def test_greece_inherits_the_base_steps():
    booking_config.clear_cache()
    names = [s["name"] for s in booking_config.steps_for("AE-GRC")]
    assert names == ["dashboard_resume", "verify_identity",
                     "appointment_details", "select_slot", "confirmation"]


def test_the_shipped_routes_are_all_disabled():
    """None of these flows has been walked with a browser yet. A config that
    armed itself on merge would be the worst kind of default."""
    booking_config.clear_cache()
    for route in booking_config.configured_routes():
        assert not booking_config.is_enabled(route), route


def test_greece_narrows_the_reference_pattern_but_inherits_the_rest():
    booking_config.clear_cache()
    step = next(s for s in booking_config.steps_for("AE-GRC")
                if s["name"] == "dashboard_resume")
    assert "GRC" in step["row"]["reference_pattern"]      # overridden
    assert step["row"]["open"]["name"] == "Book Now"      # inherited


def test_the_committing_step_is_the_slot_pick():
    """Registration commits at review/pay; booking commits when the slot leaves
    the pool. Different step, same rule."""
    booking_config.clear_cache()
    assert booking_config.commit_step_name("AE-GRC") == "select_slot"


def test_the_identity_policy_defaults_to_strict():
    booking_config.clear_cache()
    policy = booking_config.identity_policy("AE-GRC")
    assert policy["min_confidence"] == "exact"
    assert policy["require_unique_match"] is True
    assert policy["verify_on_detail_page"] is True


# --------------------------------------------------------------------------- #
# The commit boundary — the rules that matter most                             #
# --------------------------------------------------------------------------- #

def test_a_config_with_no_committing_step_is_refused(config_dir):
    """Without one, a taken slot leaves no write-ahead trace."""
    write(config_dir, "AE-XXX", {
        "steps": [{"name": "open", "type": "dashboard_resume"}]
    })
    with pytest.raises(BookingConfigError, match="commits"):
        booking_config.get("AE-XXX")


def test_two_committing_steps_are_refused(config_dir):
    """Both would claim to be the point of no return, and the write-ahead marker
    would land in the wrong place."""
    write(config_dir, "AE-XXX", {"steps": [
        {"name": "a", "type": "slot_pick", "commits": True},
        {"name": "b", "type": "form", "commits": True},
    ]})
    with pytest.raises(BookingConfigError, match="Exactly one"):
        booking_config.get("AE-XXX")


def test_a_non_committable_step_type_cannot_commit(config_dir):
    """A dashboard_resume changes nothing at VFS, so marking it as the point of
    no return is always a mistake."""
    write(config_dir, "AE-XXX", {"steps": [
        {"name": "open", "type": "dashboard_resume", "commits": True},
    ]})
    with pytest.raises(BookingConfigError, match="cannot be the commit point"):
        booking_config.get("AE-XXX")


def test_an_identity_check_after_the_commit_is_refused(config_dir):
    """The whole value of click-then-check is that the check happens while
    backing out is still free. After the commit it verifies nothing."""
    write(config_dir, "AE-XXX", {"steps": [
        {"name": "open", "type": "dashboard_resume"},
        {"name": "pick", "type": "slot_pick", "commits": True},
        {"name": "verify", "type": "identity_assert"},
    ]})
    with pytest.raises(BookingConfigError, match="BEFORE the committing step"):
        booking_config.get("AE-XXX")


def test_an_identity_check_before_the_commit_is_fine(config_dir):
    write(config_dir, "AE-XXX", {"steps": [
        {"name": "open", "type": "dashboard_resume"},
        {"name": "verify", "type": "identity_assert"},
        {"name": "pick", "type": "slot_pick", "commits": True},
    ]})
    assert len(booking_config.steps_for("AE-XXX")) == 3


# --------------------------------------------------------------------------- #
# Step validation                                                              #
# --------------------------------------------------------------------------- #

def test_a_step_without_a_type_is_refused(config_dir):
    write(config_dir, "AE-XXX", {"steps": [
        {"name": "open"},
        {"name": "pick", "type": "slot_pick", "commits": True},
    ]})
    with pytest.raises(BookingConfigError, match="missing \"type\""):
        booking_config.get("AE-XXX")


def test_an_unknown_step_type_is_refused(config_dir):
    """A typo must not surface as 'nothing happened' mid-flow."""
    write(config_dir, "AE-XXX", {"steps": [
        {"name": "open", "type": "dashbored_resume"},
        {"name": "pick", "type": "slot_pick", "commits": True},
    ]})
    with pytest.raises(BookingConfigError, match="unknown type"):
        booking_config.get("AE-XXX")


def test_a_duplicate_step_name_is_refused(config_dir):
    write(config_dir, "AE-XXX", {"steps": [
        {"name": "same", "type": "form"},
        {"name": "same", "type": "slot_pick", "commits": True},
    ]})
    with pytest.raises(BookingConfigError, match="duplicate"):
        booking_config.get("AE-XXX")


def test_a_step_without_a_name_is_refused(config_dir):
    write(config_dir, "AE-XXX", {"steps": [{"type": "slot_pick", "commits": True}]})
    with pytest.raises(BookingConfigError, match="name"):
        booking_config.get("AE-XXX")


def test_an_empty_step_list_is_refused(config_dir):
    write(config_dir, "AE-XXX", {"steps": []})
    with pytest.raises(BookingConfigError, match="no \"steps\""):
        booking_config.get("AE-XXX")


# --------------------------------------------------------------------------- #
# Inheritance                                                                  #
# --------------------------------------------------------------------------- #

def test_a_child_merges_over_the_parent_by_name(config_dir):
    write(config_dir, "_base", minimal())
    write(config_dir, "AE-XXX", {
        "extends": "_base",
        "steps": [{"name": "pick", "strategy": "date_window"}],
    })
    step = next(s for s in booking_config.steps_for("AE-XXX") if s["name"] == "pick")
    assert step["strategy"] == "date_window"    # child key
    assert step["commits"] is True              # parent key survives
    assert step["type"] == "slot_pick"


def test_a_new_step_can_be_positioned_mid_flow(config_dir):
    """Position matters: a portal with an extra page in the MIDDLE would
    otherwise have it appended after the committing step and never reached."""
    write(config_dir, "_base", minimal())
    write(config_dir, "AE-XXX", {
        "extends": "_base",
        "steps": [{"name": "extra", "type": "form", "after": "open"}],
    })
    assert [s["name"] for s in booking_config.steps_for("AE-XXX")] == [
        "open", "extra", "pick"
    ]


def test_a_step_can_be_removed(config_dir):
    write(config_dir, "_base", minimal())
    write(config_dir, "AE-XXX", {
        "extends": "_base",
        "steps": [{"name": "open", "remove": True}],
    })
    assert [s["name"] for s in booking_config.steps_for("AE-XXX")] == ["pick"]


def test_a_nested_block_merges_rather_than_being_replaced(config_dir):
    """A REAL BUG, caught by a test. Steps merge by name, but a shallow merge
    replaces a nested block wholesale — so Greece narrowing `row.reference_pattern`
    silently dropped the inherited `row.container` and `row.open` selectors,
    leaving a step that could not find or click anything."""
    write(config_dir, "_base", {
        "steps": [
            {"name": "open", "type": "dashboard_resume",
             "row": {"container": "mat-card", "open": {"name": "Book Now"},
                     "reference_pattern": "(.+)"}},
            {"name": "pick", "type": "slot_pick", "commits": True},
        ]
    })
    write(config_dir, "AE-XXX", {
        "extends": "_base",
        "steps": [{"name": "open", "row": {"reference_pattern": "(GRC\\d+)"}}],
    })

    row = next(s for s in booking_config.steps_for("AE-XXX")
               if s["name"] == "open")["row"]
    assert row["reference_pattern"] == "(GRC\\d+)"     # overridden
    assert row["container"] == "mat-card"              # survived
    assert row["open"]["name"] == "Book Now"           # survived


def test_the_identity_block_merges_rather_than_replacing(config_dir):
    """A route relaxing one knob must not silently drop the others."""
    write(config_dir, "_base", minimal(identity={
        "min_confidence": "exact", "require_unique_match": True,
        "verify_on_detail_page": True,
    }))
    write(config_dir, "AE-XXX", {
        "extends": "_base",
        "identity": {"min_confidence": "strong"},
    })
    policy = booking_config.identity_policy("AE-XXX")
    assert policy["min_confidence"] == "strong"       # overridden
    assert policy["require_unique_match"] is True     # survived


def test_positioning_against_an_unknown_anchor_is_refused(config_dir):
    write(config_dir, "_base", minimal())
    write(config_dir, "AE-XXX", {
        "extends": "_base",
        "steps": [{"name": "x", "type": "form", "after": "nowhere"}],
    })
    with pytest.raises(BookingConfigError, match="nowhere"):
        booking_config.get("AE-XXX")


def test_a_cyclic_extends_is_caught(config_dir):
    write(config_dir, "A", {"extends": "B", "steps": []})
    write(config_dir, "B", {"extends": "A", "steps": []})
    with pytest.raises(BookingConfigError, match="Cyclic"):
        booking_config.get("A")


def test_extending_a_missing_parent_is_refused(config_dir):
    write(config_dir, "AE-XXX", {"extends": "nope", "steps": []})
    with pytest.raises(BookingConfigError, match="missing parent"):
        booking_config.get("AE-XXX")


# --------------------------------------------------------------------------- #
# Discovery and failure modes                                                  #
# --------------------------------------------------------------------------- #

def test_a_missing_route_config_is_refused(config_dir):
    with pytest.raises(BookingConfigError, match="No booking config"):
        booking_config.get("AE-NOPE")


def test_malformed_json_raises_rather_than_reading_as_empty(config_dir):
    """Booking must never proceed against a half-understood page description."""
    (config_dir / "AE-BAD.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(BookingConfigError, match="Could not read"):
        booking_config.get("AE-BAD")


def test_underscore_files_are_not_routes(config_dir):
    write(config_dir, "_default", minimal())
    write(config_dir, "AE-GRC", {"extends": "_default"})
    assert booking_config.configured_routes() == ["AE-GRC"]


def test_is_enabled_is_false_for_a_broken_config(config_dir):
    """A route that cannot be read is not runnable — never assume otherwise."""
    (config_dir / "AE-BAD.json").write_text("{not json", encoding="utf-8")
    assert booking_config.is_enabled("AE-BAD") is False


def test_check_reports_every_problem(config_dir):
    write(config_dir, "AE-OK", minimal())
    write(config_dir, "AE-BAD", {"steps": [{"name": "x", "type": "form"}]})
    problems = booking_config.check()
    assert len(problems) == 1 and "AE-BAD" in problems[0]
