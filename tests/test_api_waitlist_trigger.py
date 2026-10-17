"""A waitlist run must never go live by accident.

WHY THIS FILE EXISTS
--------------------
`POST /v1/waitlist/runs` takes real, irreversible VFS waitlist places under
real clients' names. A duplicate can make VFS void both entries.

The body used to carry a single `dry_run: bool = True`. Pydantic coerces that
loosely, so "no", "off", "0", "false", "f", "n" and 0 all arrived as False —
and `to_cli_args` then emitted `--live --yes`, skipping even the CLI's own
confirmation prompt. Any caller that stringifies booleans could ask for a
rehearsal and get live registrations across every armed client. `registrant`
was optional, so a live run naming nobody covered everyone.

Travnooker's Flask proxy blocked that by validating `dry_run` as a strict
boolean, which left the entire safety property in one layer this API cannot
see — and a second consumer of this API already exists.

There was no bot-level test for this endpoint at all. These are it. They
assert the argv that is actually handed to the subprocess, because that is the
only thing that decides whether a real registration happens.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.api.modules.waitlist.schemas import (  # noqa: E402
    TriggerRequest,
    WaitlistRunMode,
)

#: Everything a loose boolean parser used to turn into "live".
TRUTHY_LOOKING_FALSE = ["no", "off", "0", "false", "False", "f", "n", "NO", 0]


@pytest.mark.parametrize("value", TRUTHY_LOOKING_FALSE)
def test_a_stringy_dry_run_is_refused_not_read_as_live(value):
    """The regression this file exists for.

    Each of these used to validate cleanly as dry_run=False and produce
    `--live --yes`.
    """
    with pytest.raises(ValidationError):
        TriggerRequest(dry_run=value, registrant="client-1")


@pytest.mark.parametrize("value", ["yes", "true", "True", "on", "1", 1])
def test_a_stringy_true_is_also_refused(value):
    """Rejected in BOTH directions.

    Accepting "true" while rejecting "false" would leave a caller believing
    strings work, which is how the dangerous one gets sent later.
    """
    with pytest.raises(ValidationError):
        TriggerRequest(dry_run=value, registrant="client-1")


def test_the_default_is_a_rehearsal():
    """An empty body must not submit anything."""
    req = TriggerRequest()
    assert req.is_live is False
    assert req.to_cli_args() == ["--dry-run"]


def test_argv_never_says_live_without_an_explicit_live_mode():
    """The property, asserted over every shape that validates.

    `--live` and `--yes` may appear only when `is_live` is true.
    """
    shapes = [
        {}, {"dry_run": True}, {"mode": "rehearse"},
        {"route": "AE-CHE"}, {"registrant": "client-1"},
        {"combo": "Dubai - SCHENGEN"}, {"reason": "a note"},
    ]
    for kwargs in shapes:
        args = TriggerRequest(**kwargs).to_cli_args()
        assert "--live" not in args, kwargs
        assert "--yes" not in args, kwargs
        assert "--dry-run" in args, kwargs


# --------------------------------------------------------------------------
# The deprecated alias: the console still sends it, so it must keep working
# --------------------------------------------------------------------------


def test_dry_run_false_with_a_registrant_still_runs_live():
    """The existing proxy's exact payload shape."""
    req = TriggerRequest(dry_run=False, registrant="client-1", route="AE-CHE")
    assert req.is_live is True
    assert req.mode is WaitlistRunMode.LIVE
    args = req.to_cli_args()
    assert "--live" in args and "--yes" in args
    assert "--dry-run" not in args


def test_a_live_run_must_name_a_registrant():
    """A live run naming nobody would register every armed client."""
    with pytest.raises(ValidationError, match="must name a registrant"):
        TriggerRequest(dry_run=False)


def test_dry_run_true_is_a_rehearsal():
    assert TriggerRequest(dry_run=True).to_cli_args() == ["--dry-run"]


# --------------------------------------------------------------------------
# The explicit mode path
# --------------------------------------------------------------------------


def test_mode_live_needs_a_matching_confirm():
    req = TriggerRequest(mode="live", registrant="client-1",
                         route="AE-CHE", confirm="AE-CHE")
    assert req.is_live is True
    assert "--live" in req.to_cli_args()


def test_mode_live_without_confirm_is_refused():
    with pytest.raises(ValidationError, match="confirm"):
        TriggerRequest(mode="live", registrant="client-1", route="AE-CHE")


def test_mode_live_with_the_wrong_confirm_is_refused():
    with pytest.raises(ValidationError, match="confirm must equal route"):
        TriggerRequest(mode="live", registrant="client-1",
                       route="AE-CHE", confirm="AE-NOR")


def test_confirm_is_matched_case_insensitively_after_route_normalisation():
    req = TriggerRequest(mode="live", registrant="client-1",
                         route="ae-che", confirm="ae-che")
    assert req.is_live is True


def test_mode_and_dry_run_may_not_contradict_each_other():
    with pytest.raises(ValidationError, match="contradict"):
        TriggerRequest(mode="live", dry_run=True, registrant="client-1")


def test_mode_and_dry_run_agreeing_is_accepted():
    req = TriggerRequest(mode="rehearse", dry_run=True)
    assert req.is_live is False


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValidationError):
        TriggerRequest(mode="LIVE_NOW", registrant="client-1")


def test_unknown_fields_are_still_refused():
    """`extra="forbid"` must survive the new fields being added."""
    with pytest.raises(ValidationError):
        TriggerRequest(definitely_not_a_field=True)
