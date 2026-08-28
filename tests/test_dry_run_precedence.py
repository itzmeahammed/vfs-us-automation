"""`dry_run` and `auto_trigger_dry_run` REPLACE each other — they do not stack.

The single line that decides, in register.py:

    dry_run = guards.dry_run() if force_dry_run is None else force_dry_run

`force_dry_run` is None for a manual run, and is set to `auto_trigger_dry_run`
for an auto-triggered one. So exactly one switch applies to any given run:

    started by a human  ->  dry_run
    started by the bot  ->  auto_trigger_dry_run

The dangerous corner, and the reason this file exists: with

    dry_run              = true     <- looks safe
    auto_trigger_dry_run = false

an auto-triggered run SUBMITS FOR REAL. `dry_run = true` does not protect you
from the auto-trigger. /status used to report "AUTO (DRY RUN) — nothing is
committed" for exactly that combination.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

from src.api.status import _describe_posture  # noqa: E402
from src.settings import settings  # noqa: E402
from src.waitlist import guards  # noqa: E402


def _effective_dry_run(*, auto: bool, dry_run: bool,
                       auto_dry_run: bool) -> bool:
    """Reproduce register.py's resolution for a run from either source."""
    force = auto_dry_run if auto else None
    return guards.dry_run() if force is None else force


@pytest.fixture
def cfg(monkeypatch):
    w = settings().waitlist
    monkeypatch.setattr(w, "register_enabled", True, raising=False)
    return w


class _Switches:
    """The shape _describe_posture reads."""

    def __init__(self, **kw):
        self.register_enabled = kw.get("register_enabled", True)
        self.dry_run = kw.get("dry_run", True)
        self.auto_trigger_enabled = kw.get("auto_trigger_enabled", True)
        self.auto_trigger_dry_run = kw.get("auto_trigger_dry_run", True)


# --------------------------------------------------------------------------
# Which switch wins
# --------------------------------------------------------------------------


def test_a_manual_run_obeys_dry_run(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "dry_run", False, raising=False)
    assert _effective_dry_run(auto=False, dry_run=False,
                              auto_dry_run=True) is False


def test_a_manual_run_ignores_the_auto_switch(cfg, monkeypatch):
    """auto_trigger_dry_run=True must not make a manual live run a rehearsal."""
    monkeypatch.setattr(cfg, "dry_run", False, raising=False)
    monkeypatch.setattr(cfg, "auto_trigger_dry_run", True, raising=False)
    assert _effective_dry_run(auto=False, dry_run=False,
                              auto_dry_run=True) is False


def test_an_auto_run_obeys_the_auto_switch(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "dry_run", True, raising=False)
    assert _effective_dry_run(auto=True, dry_run=True,
                              auto_dry_run=False) is False


def test_dry_run_true_does_NOT_protect_an_auto_run(cfg, monkeypatch):
    """THE TRAP. dry_run=true reads as safe; the bot still submits."""
    monkeypatch.setattr(cfg, "dry_run", True, raising=False)
    effective = _effective_dry_run(auto=True, dry_run=True, auto_dry_run=False)
    assert effective is False, (
        "dry_run=true made an auto-triggered run look safe, but the run "
        "submits for real — auto_trigger_dry_run is the only switch that "
        "governs it."
    )


def test_an_auto_run_can_rehearse_while_manual_is_live(cfg, monkeypatch):
    """The useful combination: register by hand, let the bot only rehearse."""
    monkeypatch.setattr(cfg, "dry_run", False, raising=False)
    assert _effective_dry_run(auto=True, dry_run=False,
                              auto_dry_run=True) is True
    assert _effective_dry_run(auto=False, dry_run=False,
                              auto_dry_run=True) is False


# --------------------------------------------------------------------------
# The posture line must not under-state the risk
# --------------------------------------------------------------------------


def test_posture_says_LIVE_when_the_bot_would_submit():
    """Regression: this reported AUTO (DRY RUN) while the bot submitted."""
    posture = _describe_posture(_Switches(
        dry_run=True,                 # looks safe...
        auto_trigger_dry_run=False,   # ...but this is what governs
    ))
    assert "LIVE" in posture, posture
    assert "DRY RUN" not in posture, (
        "posture claimed nothing is committed while auto-triggered runs "
        "submit for real"
    )


def test_posture_says_dry_run_when_the_bot_only_rehearses():
    posture = _describe_posture(_Switches(
        dry_run=False, auto_trigger_dry_run=True))
    assert "DRY RUN" in posture


def test_posture_parked_overrides_everything():
    posture = _describe_posture(_Switches(
        register_enabled=False, dry_run=False, auto_trigger_dry_run=False))
    assert posture.startswith("PARKED")


def test_posture_manual_when_auto_trigger_is_off():
    posture = _describe_posture(_Switches(
        auto_trigger_enabled=False, auto_trigger_dry_run=False))
    assert posture.startswith("MANUAL")


# --------------------------------------------------------------------------
# The master switch beats both
# --------------------------------------------------------------------------


def test_register_enabled_false_blocks_every_path(monkeypatch):
    """Whatever the dry-run switches say, gate 1 stops it."""
    w = settings().waitlist
    monkeypatch.setattr(w, "register_enabled", False, raising=False)
    monkeypatch.setattr(w, "dry_run", False, raising=False)
    monkeypatch.setattr(w, "auto_trigger_enabled", True, raising=False)
    monkeypatch.setattr(w, "auto_trigger_dry_run", False, raising=False)

    class _R:
        id = "x"
        enabled = True
        combos = ["Dubai - SCHENGEN"]

        def wants(self, c):
            return True

    verdict = guards.check("AE-CHE", "Dubai - SCHENGEN", _R())
    assert not verdict.allowed
    assert "register_enabled" in verdict.reason
