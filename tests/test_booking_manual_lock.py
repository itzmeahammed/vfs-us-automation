"""Every manual walk or commit must hold the machine-wide booking lock.

WHY THIS FILE EXISTS
--------------------
`POST /v1/booking/runs` sends `--registrant <id> --entry waitlist --walk
--commit --yes` for any client that is NOT in the booking request store — i.e.
every waitlist-flow client. In `cmd_probe` that landed in a bare `_run()`:

    if request_id and args.walk:
        result = autobook.manual_run(...)   # takes the lock
    else:
        result = _run()                     # took NOTHING

`runlock` appears nowhere in probe.py, walk.py or runner.py — only autobook
and invites acquire it — so that branch held no lock at all. The automatic
lanes acquire with `on_busy="skip"`, so they saw the lane free and started a
second Chrome session against the same shared VFS booking account while the
manual one was inside its select_slot/payment walk. That walk takes minutes,
so the window is wide, and the step it collides on is the step that spends
money.

The job manager's own single-flight probe does not help: its docstring is
explicit that it is "a fast-fail courtesy" and that "the real mutual exclusion
is the child's own acquire".

These tests assert the acquisition, not the booking behaviour: what matters is
that the lane is taken, with the same lane and non-blocking semantics the
automatic path uses, and that a busy lane refuses instead of running.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.booking import __main__ as booking_main  # noqa: E402
from src.utils import runlock  # noqa: E402


class _Handle:
    def __init__(self, held: bool):
        self.held = held
        self.holder_hint = "" if held else "auto-booking (pid 123)"


class _Empty:
    """Reads as absent however cmd_probe's reporting tail asks.

    Empty when iterated or counted, and any attribute access returns another
    one — so `result.walk.steps` and `result.rows` both work without this test
    having to mirror the probe's result dataclass.
    """

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __bool__(self):
        return False

    def __getattr__(self, name):
        return _Empty()


class _Result:
    """A probe result that satisfies cmd_probe's reporting tail.

    Only the lock acquisition is under test here; everything the summary
    printing reads is deliberately empty.
    """

    interrupted = False
    route = "AE-NOR"
    account = "booker@example.com"
    outcome = "walked"
    error = ""
    booked = None

    def __getattr__(self, name):            # errors, rows, bookable, walk, …
        return _Empty()


@pytest.fixture()
def spy(monkeypatch):
    """Record lock acquisitions and whether the probe actually ran."""
    seen = {"acquired": [], "ran": 0, "held": True}

    from contextlib import contextmanager

    @contextmanager
    def fake_acquire(owner, *, lane=None, timeout=None, on_busy=None):
        seen["acquired"].append(
            {"owner": owner, "lane": lane, "timeout": timeout,
             "on_busy": on_busy})
        yield _Handle(seen["held"])

    monkeypatch.setattr(runlock, "acquire", fake_acquire)

    def fake_run_probe(**kwargs):
        seen["ran"] += 1
        return _Result()

    # cmd_probe imports run_probe from src.booking.probe inside the function,
    # so patch it at its source rather than on the __main__ module.
    import src.booking.probe as probe_mod
    monkeypatch.setattr(probe_mod, "run_probe", fake_run_probe)
    return seen


def _args(**over):
    """The argv shape the API sends for a waitlist-flow commit."""
    base = dict(source_country="AE", dest_country="NOR", registrant=None,
                email="", password="", proxy_url="", keep_open=False,
                hold_seconds=0, entry="waitlist", combo="", walk=True,
                to_step="", applicant=[], commit=True, capture="",
                request=None, yes=True, started_by="operator@example.com")
    base.update(over)
    return SimpleNamespace(**base)


def test_a_waitlist_commit_takes_the_booking_lane(spy, monkeypatch):
    """The regression this file exists for."""
    monkeypatch.setattr(booking_main, "_confirm_commit", lambda args: True)
    booking_main.cmd_probe(_args(registrant="client-1"))

    assert spy["acquired"], (
        "a manual commit for a waitlist client took no booking lock, so an "
        "automatic lane could start a second browser on the same account")
    lock = spy["acquired"][0]
    assert lock["lane"] == runlock.LANE_BOOKING
    assert lock["timeout"] == 0, "must not block a booking behind another"
    assert lock["on_busy"] == "skip", "must refuse quietly, matching autobook"


def test_a_busy_lane_refuses_and_does_not_run(spy, monkeypatch):
    """Exit 3 and nothing clicked — not a second concurrent payment."""
    monkeypatch.setattr(booking_main, "_confirm_commit", lambda args: True)
    spy["held"] = False

    code = booking_main.cmd_probe(_args(registrant="client-1"))

    assert code == 3
    assert spy["ran"] == 0, (
        "the probe ran while another booking held the lane — this is the "
        "concurrent-payment case")


def test_a_walk_without_commit_also_takes_the_lane(spy, monkeypatch):
    """A walk drives the same pages up to the committing step."""
    booking_main.cmd_probe(_args(registrant="client-1", commit=False))
    assert [a["lane"] for a in spy["acquired"]] == [runlock.LANE_BOOKING]


def test_a_plain_probe_does_not_contend_for_the_lane(spy, monkeypatch):
    """A probe reads the dashboard and books nothing, so it must not block.

    Taking the lane here would make a read-only check refuse while a booking
    runs, which is a regression in the other direction.
    """
    booking_main.cmd_probe(_args(registrant="client-1", walk=False,
                                 commit=False))
    assert spy["acquired"] == []
    assert spy["ran"] == 1
