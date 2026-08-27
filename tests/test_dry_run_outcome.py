"""A dry run must report DRY_RUN, not FAILED.

The bug this closes, found while doing a real Stage 2 verification against the
live Swiss portal:

  · A dry run walked to the first submit, correctly declined to click it, and
    RETURNED.
  · The caller's loop then advanced to the next step, which waited out its full
    120s timeout for a navigation that could not happen — nothing had been
    submitted.
  · The run ended `failed` with "never reached a URL containing 'your-details'".

So every dry run failed, and Stage 2 could not distinguish "the form is filled
correctly" from "the portal is broken" — which is the entire purpose of Stage 2.
The fix makes the refusal-to-submit a control-flow signal (_DryRunStop) instead
of an ordinary return.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

from src.waitlist import register  # noqa: E402
from src.waitlist.result import Status, WaitlistResult  # noqa: E402


class _Page:
    """Enough of a Playwright page for _run_step to walk over."""

    url = "https://visa.vfsglobal.com/are/en/che/application-detail"

    def wait_for_timeout(self, ms):        pass
    def wait_for_url(self, *a, **k):       pass
    def wait_for_selector(self, *a, **k):  pass
    def screenshot(self, *a, **k):         pass
    def query_selector(self, *a, **k):     return None
    def get_by_role(self, *a, **k):        return self
    def first(self):                       return self
    def click(self, *a, **k):              raise AssertionError("submitted in a dry run")


def _step(name="your_details"):
    return {
        "name": name,
        "fields": [],
        "submit": {"role": "button", "name": "Save"},
    }


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """Keep the unit under test off the disk and off the network."""
    monkeypatch.setattr(register, "_screenshot", lambda *a, **k: None)
    monkeypatch.setattr(register, "_await_page", lambda *a, **k: None)
    monkeypatch.setattr(register, "_dwell", lambda *a, **k: None)
    monkeypatch.setattr(register, "_fill_fields", lambda *a, **k: None,
                        raising=False)


def test_dry_run_raises_the_stop_signal():
    """The refusal to submit is signalled, not silently returned."""
    result = WaitlistResult("AE-CHE", "Dubai - SCHENGEN", "test", Status.PENDING)
    with pytest.raises(register._DryRunStop) as exc:
        register._run_step(_Page(), _step(), {}, result, dry_run=True)
    assert exc.value.step_name == "your_details"


def test_the_signal_names_the_step_it_stopped_at():
    """So the operator can see how far the rehearsal actually got."""
    result = WaitlistResult("AE-CHE", "Dubai - SCHENGEN", "test", Status.PENDING)
    with pytest.raises(register._DryRunStop) as exc:
        register._run_step(_Page(), _step("appointment_details"), {}, result,
                           dry_run=True)
    assert "appointment_details" in str(exc.value)


def test_a_dry_run_never_clicks_submit():
    """The whole point. _Page.click raises if anything tries."""
    result = WaitlistResult("AE-CHE", "Dubai - SCHENGEN", "test", Status.PENDING)
    with pytest.raises(register._DryRunStop):
        register._run_step(_Page(), _step(), {}, result, dry_run=True)


def test_the_stop_signal_is_not_a_step_error():
    """It must not be caught by the handlers that report a FAILED run.

    If _DryRunStop were a WaitlistStepError subclass, the caller's existing
    `except WaitlistStepError` would mark the run failed — reintroducing the
    exact bug.
    """
    from src.waitlist.errors import WaitlistStepError

    assert not issubclass(register._DryRunStop, WaitlistStepError)


def test_a_step_with_no_submit_still_returns_normally():
    """Only a declined SUBMIT ends the walk; a fill-only step carries on."""
    result = WaitlistResult("AE-CHE", "Dubai - SCHENGEN", "test", Status.PENDING)
    step = {"name": "details_summary", "fields": []}      # no "submit"
    register._run_step(_Page(), step, {}, result, dry_run=True)
    assert "details_summary" in result.steps_completed
