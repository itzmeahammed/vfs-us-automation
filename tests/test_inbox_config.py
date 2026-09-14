"""Config loading, inheritance and the fail-loud contract.

The rule this file defends: a broken or missing matcher config must never look
like "no mail arrived". A matcher that silently never fires is indistinguishable
from an empty mailbox, and that is the exact failure the inbox package exists to
prevent — so errors surface at LOAD time, with a filename, not at match time.

The one deliberate exception is `all_matchers()`, where one country's broken
file must not blind the watcher to every other country's mail.
"""

import json

import pytest

from src.inbox import config as inbox_config
from src.inbox.matcher import CONFIRMATION, INVITATION, MatcherConfigError


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Redirects the loader at a temp config directory."""
    directory = tmp_path / "inbox"
    directory.mkdir()
    monkeypatch.setattr(inbox_config, "INBOX_DIR", str(directory))
    inbox_config.clear_cache()
    yield directory
    inbox_config.clear_cache()


def write(directory, name, payload):
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


BASE = {
    "matchers": [
        {
            "name": "invite",
            "classify": INVITATION,
            "subject_contains": ["Slots available"],
            "validity_hours": 48,
        },
        {
            "name": "confirm",
            "classify": CONFIRMATION,
            "subject_contains": ["Successfully Added"],
        },
    ]
}


# --------------------------------------------------------------------------- #
# The real shipped config                                                      #
# --------------------------------------------------------------------------- #

def test_the_shipped_configs_all_validate():
    """Guards the files actually in config/inbox/ — a typo there is a silent
    production failure, so it must break the build instead."""
    inbox_config.clear_cache()
    assert inbox_config.check() == []


def test_the_shipped_italy_config_inherits_the_base_matchers():
    inbox_config.clear_cache()
    names = [m["name"] for m in inbox_config.matchers_for("AE-ITA")]
    assert names == [
        "waitlist_invitation",
        "waitlist_confirmation",
        "appointment_confirmed",   # found live 2026-09-03: booking confirmed
        "waitlist_cancellation",   # found live 2026-09-02
        "vfs_otp",                 # 111 of 123 real messages
        "vfs_other",               # catch-all, and it must stay LAST
    ]


def test_the_catch_all_is_last_so_it_cannot_swallow_the_others():
    """classify() takes the first match, so ordering is load-bearing: a
    catch-all listed first would classify every VFS email as 'other'."""
    inbox_config.clear_cache()
    matchers = inbox_config.matchers_for("AE-ITA")
    assert matchers[-1]["name"] == "vfs_other"


def test_italy_overrides_only_what_it_declares():
    """The whole point of extends: a route file states its differences, and the
    base's other keys survive."""
    inbox_config.clear_cache()
    invite = next(
        m for m in inbox_config.matchers_for("AE-ITA")
        if m["name"] == "waitlist_invitation"
    )
    # Overridden. MEASURED, not assumed: the real sender is donotreply@ on these
    # domains, not the info.italyuae@ address printed in the body's signature.
    assert "vfshelpline.com" in invite["from_contains"]
    assert invite["validity_hours"] == 48                     # inherited
    assert invite["subject_contains"] == ["Slots available for booking"]


# --------------------------------------------------------------------------- #
# Inheritance                                                                  #
# --------------------------------------------------------------------------- #

def test_a_child_merges_over_the_parent_by_name(config_dir):
    write(config_dir, "_default", BASE)
    write(config_dir, "AE-XXX", {
        "extends": "_default",
        "matchers": [{"name": "invite", "from_contains": ["x.com"]}],
    })

    invite = next(m for m in inbox_config.matchers_for("AE-XXX") if m["name"] == "invite")
    assert invite["from_contains"] == ["x.com"]        # child key wins
    assert invite["subject_contains"] == ["Slots available"]   # parent survives
    assert invite["validity_hours"] == 48


def test_a_new_matcher_is_appended(config_dir):
    write(config_dir, "_default", BASE)
    write(config_dir, "AE-XXX", {
        "extends": "_default",
        "matchers": [{"name": "extra", "classify": "other", "subject_contains": ["x"]}],
    })
    assert [m["name"] for m in inbox_config.matchers_for("AE-XXX")] == [
        "invite", "confirm", "extra"
    ]


def test_a_matcher_can_be_positioned_before_an_inherited_one(config_dir):
    """Needed when a route must pre-empt an inherited matcher, since first match
    wins."""
    write(config_dir, "_default", BASE)
    write(config_dir, "AE-XXX", {
        "extends": "_default",
        "matchers": [{
            "name": "special", "classify": INVITATION,
            "subject_contains": ["Special"], "before": "invite",
        }],
    })
    assert [m["name"] for m in inbox_config.matchers_for("AE-XXX")] == [
        "special", "invite", "confirm"
    ]


def test_a_matcher_can_be_removed(config_dir):
    write(config_dir, "_default", BASE)
    write(config_dir, "AE-XXX", {
        "extends": "_default",
        "matchers": [{"name": "confirm", "remove": True}],
    })
    assert [m["name"] for m in inbox_config.matchers_for("AE-XXX")] == ["invite"]


def test_positioning_against_an_unknown_anchor_is_rejected(config_dir):
    write(config_dir, "_default", BASE)
    write(config_dir, "AE-XXX", {
        "extends": "_default",
        "matchers": [{
            "name": "x", "classify": "other",
            "subject_contains": ["x"], "after": "nonexistent",
        }],
    })
    with pytest.raises(MatcherConfigError, match="nonexistent"):
        inbox_config.matchers_for("AE-XXX")


# --------------------------------------------------------------------------- #
# Failing loud                                                                 #
# --------------------------------------------------------------------------- #

def test_a_missing_route_config_raises(config_dir):
    with pytest.raises(MatcherConfigError, match="No inbox config"):
        inbox_config.get("AE-NOPE")


def test_malformed_json_raises_rather_than_reading_as_empty(config_dir):
    """Skipping it would mean classifying nothing for that country while looking
    exactly like 'no mail arrived'."""
    (config_dir / "AE-BAD.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(MatcherConfigError, match="Could not read"):
        inbox_config.get("AE-BAD")


def test_a_config_with_no_matchers_raises(config_dir):
    write(config_dir, "AE-EMPTY", {"matchers": []})
    with pytest.raises(MatcherConfigError, match="no \"matchers\""):
        inbox_config.get("AE-EMPTY")


def test_an_invalid_matcher_is_reported_with_its_filename(config_dir):
    """The error must name the file, or a typo is a scavenger hunt."""
    write(config_dir, "AE-BAD", {
        "matchers": [{"name": "x", "classify": "not-a-classification",
                      "subject_contains": ["x"]}]
    })
    with pytest.raises(MatcherConfigError, match="AE-BAD"):
        inbox_config.get("AE-BAD")


def test_extending_a_missing_parent_raises(config_dir):
    write(config_dir, "AE-XXX", {"extends": "nope", "matchers": []})
    with pytest.raises(MatcherConfigError, match="missing parent"):
        inbox_config.get("AE-XXX")


def test_a_cyclic_extends_is_caught_rather_than_recursing(config_dir):
    write(config_dir, "A", {"extends": "B", "matchers": []})
    write(config_dir, "B", {"extends": "A", "matchers": []})
    with pytest.raises(MatcherConfigError, match="Cyclic"):
        inbox_config.get("A")


# --------------------------------------------------------------------------- #
# Discovery                                                                    #
# --------------------------------------------------------------------------- #

def test_underscore_files_are_not_routes(config_dir):
    """_default is a base to inherit, not a country to watch."""
    write(config_dir, "_default", BASE)
    write(config_dir, "AE-ITA", {"extends": "_default", "matchers": []})
    assert inbox_config.configured_routes() == ["AE-ITA"]


def test_a_missing_config_directory_yields_no_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox_config, "INBOX_DIR", str(tmp_path / "nope"))
    inbox_config.clear_cache()
    assert inbox_config.configured_routes() == []


def test_one_broken_country_does_not_blind_the_watcher_to_the_others(config_dir, caplog):
    """The deliberate exception to failing loud: all_matchers() skips a bad file
    and logs it, so a typo in one country still leaves the rest working."""
    write(config_dir, "_default", BASE)
    write(config_dir, "AE-GOOD", {"extends": "_default", "matchers": []})
    write(config_dir, "AE-BAD", {
        "matchers": [{"name": "x", "classify": "bogus", "subject_contains": ["x"]}]
    })

    found = inbox_config.all_matchers()
    assert "AE-GOOD" in found
    assert "AE-BAD" not in found
    assert any("AE-BAD" in r.message for r in caplog.records)


def test_check_reports_every_problem_at_once(config_dir):
    write(config_dir, "AE-OK", BASE)
    write(config_dir, "AE-BAD", {
        "matchers": [{"name": "x", "classify": "bogus", "subject_contains": ["x"]}]
    })
    problems = inbox_config.check()
    assert len(problems) == 1
    assert "AE-BAD" in problems[0]


def test_the_route_key_is_case_insensitive(config_dir):
    write(config_dir, "AE-ITA", BASE)
    assert inbox_config.matchers_for("ae-ita") == inbox_config.matchers_for("AE-ITA")


def test_clear_cache_picks_up_an_edited_file(config_dir):
    write(config_dir, "AE-XXX", BASE)
    assert len(inbox_config.matchers_for("AE-XXX")) == 2

    write(config_dir, "AE-XXX", {"matchers": BASE["matchers"][:1]})
    inbox_config.clear_cache()
    assert len(inbox_config.matchers_for("AE-XXX")) == 1
