"""VFS rewording a dropdown must not cost a route its coverage, silently.

2026-10-16: VFS renamed two AE-FRA sub-categories by punctuation alone —
'Short Stay - Business' -> 'Short Stay (Business)' and
'Temporary Location-Damac Hills' -> 'Temporary Location - Damac Hills'. The
match is a case-insensitive SUBSTRING, so neither matched, both combinations
errored on every run, and the supervisor logged "Route AE-FRA OK" and sent
nothing for NINE DAYS. France reported on one of three combinations and looked
healthy.

Two defences are pinned here: a selection that survives a rewording, and an
alert for a combination that could not be checked at all.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("VFSAPI_SECRET_TOKEN", "e" * 64)

from src.utils import combo_alerts                         # noqa: E402
from src.vfs_bot import slot_check                         # noqa: E402

#: The exact pair that broke, as the portal and the config spelled them.
RENAMES = (
    ("Short Stay - Business", "Short Stay (Business)"),
    ("Temporary Location-Damac Hills", "Temporary Location - Damac Hills"),
)


# ── matching a reworded label ────────────────────────────────────────────────

class _Panel:
    """An open mat-select panel offering exactly `texts`."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.clicked_index = None

    # what _visible_option_texts walks
    def get_by_role(self, role, name=None, exact=None):
        return _Options(self, name)

    def locator(self, selector):
        return _Options(self, None)


class _Options:
    def __init__(self, panel, name, index=None):
        self.panel = panel
        self.name = name
        self.index = index

    def count(self):
        return len(self.panel.texts)

    def nth(self, i):
        return _Options(self.panel, self.name, i)

    @property
    def first(self):
        return _Options(self.panel, self.name, 0)

    def filter(self, **kw):
        return self

    def inner_text(self, timeout=0):
        return self.panel.texts[self.index or 0]

    def wait_for(self, **kw):
        return None


@pytest.mark.parametrize("configured,offered", RENAMES)
def test_a_reworded_label_still_resolves(configured, offered):
    """The option was renamed, not removed."""
    panel = _Panel([offered])

    option = slot_check._reworded_option(panel, configured)

    assert option.index == 0


def test_normalisation_ignores_only_punctuation():
    for configured, offered in RENAMES:
        assert slot_check._normalised(configured) == slot_check._normalised(offered)
    # ...and does NOT make genuinely different categories equal.
    assert slot_check._normalised("Short Stay") != slot_check._normalised("Long Stay")


def test_two_candidates_are_refused_rather_than_guessed():
    """Guessing a visa sub-category on a form that leads to a payment is not
    an acceptable recovery."""
    panel = _Panel(["Short Stay (Business)", "Short stay, business"])

    with pytest.raises(RuntimeError, match="need exactly 1"):
        slot_check._reworded_option(panel, "Short Stay - Business")


def test_a_shorter_option_cannot_swallow_a_longer_request():
    """Containment runs one way only: the configured value must be IN the
    offered label. Reversed, asking for 'Short Stay - Business' would settle
    for a plain 'Short Stay' and check the wrong category."""
    panel = _Panel(["Short Stay"])

    with pytest.raises(RuntimeError):
        slot_check._reworded_option(panel, "Short Stay - Business")


def test_nothing_on_offer_is_not_a_match():
    with pytest.raises(RuntimeError, match="none on offer"):
        slot_check._reworded_option(_Panel([]), "Short Stay - Business")


# ── alerting on a combination that was never checked ─────────────────────────

@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(combo_alerts, "STATE_FILE", str(tmp_path / "combo_alerts.json"))
    monkeypatch.setattr(combo_alerts, "alert_hours", lambda: 12.0)
    sent = []
    import src.utils.telegram as telegram
    monkeypatch.setattr(telegram, "is_error_configured", lambda: True)
    monkeypatch.setattr(telegram, "send_error", lambda text: sent.append(text) or True)
    return sent


def _entries(*rows):
    """(combo, message) pairs as run_slot_check builds them."""
    return [({"centre": "Abu Dhabi", "category": c, "sub_category": c}, m)
            for c, m in rows]


BROKEN = _entries(
    ("Short Stay - Business", "ERROR: could not select sub-category 'Short Stay - Business'"),
    ("Short Stay (any purpose)", "No slot message shown (no availability?)."),
)


def test_an_unselectable_combination_alerts(state):
    assert combo_alerts.notify("AE-FRA", BROKEN) is True

    assert len(state) == 1
    text = state[0]
    assert "1 of 2 combination(s) could NOT be checked" in text
    assert "UNKNOWN" in text, "the point is that we did not learn 'no slots'"
    assert "AE-FRA.json" in text, "the message should say where to fix it"


def test_a_clean_route_sends_nothing(state):
    clean = _entries(("Short Stay (any purpose)", "No slot message shown (no availability?)."))

    assert combo_alerts.notify("AE-FRA", clean) is False
    assert state == []


def test_no_slots_is_never_treated_as_a_failure(state):
    """An empty portal is a normal answer, not a broken check."""
    empty = _entries(
        ("A", "No slot message shown (no availability?)."),
        ("B", "Earliest available slot for 1,2,3 applicants is : 05-11-2026"),
    )

    assert combo_alerts.notify("AE-FRA", empty) is False


def test_the_same_failure_does_not_repeat_every_run(state):
    """A stale config fails every run; a channel that cries wolf gets muted."""
    assert combo_alerts.notify("AE-FRA", BROKEN) is True
    assert combo_alerts.notify("AE-FRA", BROKEN) is False
    assert combo_alerts.notify("AE-FRA", BROKEN) is False

    assert len(state) == 1


def test_a_second_combination_breaking_alerts_immediately(state):
    """Inside the window, but it is new information."""
    assert combo_alerts.notify("AE-FRA", BROKEN) is True

    worse = BROKEN + _entries(
        ("Temporary Location-Damac Hills",
         "ERROR: could not select sub-category 'Temporary Location-Damac Hills'"))
    assert combo_alerts.notify("AE-FRA", worse) is True

    assert "2 of 3 combination(s)" in state[-1]


def test_the_same_combination_failing_for_a_new_reason_alerts(state):
    assert combo_alerts.notify("AE-FRA", BROKEN) is True

    different = _entries(
        ("Short Stay - Business", "ERROR: option list never populated"),
        ("Short Stay (any purpose)", "No slot message shown (no availability?)."))
    assert combo_alerts.notify("AE-FRA", different) is True


def test_recovery_forgets_the_route_so_the_next_break_alerts(state):
    assert combo_alerts.notify("AE-FRA", BROKEN) is True
    clean = _entries(("Short Stay (any purpose)", "No slot message shown (no availability?)."))
    assert combo_alerts.notify("AE-FRA", clean) is False

    assert combo_alerts.notify("AE-FRA", BROKEN) is True, (
        "a route that broke, was fixed and broke again sat out the rest of the "
        "original window in silence")


def test_routes_do_not_share_a_cooldown(state):
    assert combo_alerts.notify("AE-FRA", BROKEN) is True
    assert combo_alerts.notify("AE-HUN", BROKEN) is True


def test_an_unreadable_state_file_still_alerts(state, tmp_path, monkeypatch):
    """Fail open: a corrupt store must not swallow a real alert."""
    path = tmp_path / "combo_alerts.json"
    path.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(combo_alerts, "STATE_FILE", str(path))

    assert combo_alerts.notify("AE-FRA", BROKEN) is True


def test_alerting_never_breaks_the_slot_check(state, monkeypatch):
    """It reports a broken check; it must not become one."""
    monkeypatch.setattr(combo_alerts, "should_alert",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    assert combo_alerts.notify("AE-FRA", BROKEN) is False   # swallowed, not raised


def test_zero_hours_disables_the_throttle(state, monkeypatch):
    monkeypatch.setattr(combo_alerts, "alert_hours", lambda: 0.0)

    assert combo_alerts.notify("AE-FRA", BROKEN) is True
    assert combo_alerts.notify("AE-FRA", BROKEN) is True


def test_the_error_prefix_matches_the_one_the_slot_check_writes():
    """Two files have to agree on this string or nothing is ever alerted."""
    source = (REPO_ROOT / "src" / "vfs_bot" / "slot_check.py").read_text(
        encoding="utf-8", errors="replace")

    assert f'message = f"{combo_alerts.ERROR_PREFIX}' in source, (
        "slot_check no longer prefixes a failed combination with "
        f"{combo_alerts.ERROR_PREFIX!r}, so combo_alerts.notify sees none")
