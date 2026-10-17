"""A reconcile PREVIEW must not consume the emails the apply step needs.

WHY THIS FILE EXISTS
--------------------
`POST /v1/waitlist/reconcile` and `python -m src.inbox reconcile` are both
two-step by design: look first, then apply. The console states the contract
plainly — "Always two steps, and the first one writes nothing."

It wrote plenty. The preview called `run_pass()` unconditionally, before
`dry_run` was even read. A pass fetches only UIDs it has not seen, marks every
fetched UID in a `finally`, and `seen.save()` fsyncs that to disk — so the
preview marked the VFS confirmation emails seen, wrote nothing to the journal,
and the apply then found `fresh == []` and reported "Settled 0 registrations"
as a success toast.

That is worse than a no-op. The confirmation email is the independent second
source for exactly the fact the browser failed to capture, so the dangling
rows stayed dangling with their proof destroyed, and recovery meant
hand-editing `state/inbox_seen.json` on the bot machine.

`run_pass(persist=False)` is the fix. These tests pin the two side effects
that make a pass consuming: saving the seen-state, and recording invitations
into the Flow 2 booking queue.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.inbox import seen as seen_state  # noqa: E402
from src.inbox import watcher  # noqa: E402


@pytest.fixture()
def pass_spy(monkeypatch):
    """Run `run_pass` against stubs, counting its persistent side effects."""
    calls = {"save": 0, "record": 0}

    monkeypatch.setattr(seen_state, "save",
                        lambda state: calls.__setitem__("save", calls["save"] + 1))

    import src.booking.invites as invites
    monkeypatch.setattr(invites, "record",
                        lambda items: calls.__setitem__("record",
                                                        calls["record"] + 1))

    # Enough stubbing to reach the mailbox loop without touching IMAP.
    monkeypatch.setattr(watcher, "_imap_settings", lambda: ("imap.test", 993))
    monkeypatch.setattr(watcher.inbox_config, "all_matchers",
                        lambda: {"AE-CHE": object()})
    monkeypatch.setattr(watcher, "mailbox_accounts",
                        lambda: [("acc@example.com", "pw")])
    monkeypatch.setattr(watcher, "fetch_mailbox", lambda *a, **k: ([], 3))
    return calls


def test_a_previewing_pass_does_not_save_the_seen_state(pass_spy):
    """The regression this file exists for.

    Saving is what makes a fetched UID unavailable to the next pass.
    """
    watcher.run_pass(persist=False)
    assert pass_spy["save"] == 0, (
        "a preview marked the confirmation emails seen, so the apply that "
        "follows it found nothing left to settle")


def test_a_previewing_pass_does_not_enqueue_booking_work(pass_spy):
    """Recording invitations is a write into the Flow 2 queue."""
    watcher.run_pass(persist=False)
    assert pass_spy["record"] == 0


def test_a_normal_pass_still_persists(pass_spy):
    """The default must be unchanged: a real pass records its progress.

    Without this, the fix could silently turn every supervisor pass into a
    no-op and the inbox would be re-read forever.
    """
    watcher.run_pass(persist=True)
    assert pass_spy["save"] >= 1
    assert pass_spy["record"] == 1


def test_persisting_is_the_default(pass_spy):
    """Callers that predate the flag must behave exactly as before."""
    watcher.run_pass()
    assert pass_spy["save"] >= 1
    assert pass_spy["record"] == 1


def test_the_api_preview_asks_for_a_non_consuming_pass(monkeypatch):
    """End to end through the endpoint: dry_run must imply persist=False."""
    seen_persist = {}

    class _Result:
        observations: list = []
        mailboxes_checked = 1
        mailboxes_failed: list = []
        messages_seen = 0

    def fake_run_pass(state=None, persist=True):
        seen_persist["persist"] = persist
        return _Result()

    monkeypatch.setattr(watcher, "run_pass", fake_run_pass)
    monkeypatch.setattr("src.inbox.reconcile.reconcile",
                        lambda observations, dry_run: [])

    from src.api.modules.waitlist.reconcile import reconcile_inbox
    from src.api.modules.waitlist.schemas import ReconcileRequest

    reconcile_inbox(ReconcileRequest(dry_run=True))
    assert seen_persist["persist"] is False

    reconcile_inbox(ReconcileRequest(dry_run=False))
    assert seen_persist["persist"] is True
