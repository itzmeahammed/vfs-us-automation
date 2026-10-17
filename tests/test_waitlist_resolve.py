"""`journal.resolve()` may only settle a row that is actually dangling.

WHY THIS FILE EXISTS
--------------------
Resolving records what a human found on the VFS portal, and it moves the
duplicate gates: a row resolved 'failed' stops blocking, so the next run
registers that client again. That is correct for a submit whose outcome was
never learned, and catastrophic for anything else.

The old implementation validated only the status VALUE, never the row, so:

  * resolving an already-SUCCESS row to 'failed' was accepted — it silenced
    both duplicate gates and let the bot take a SECOND VFS place for a client
    who already held one. The row it wrote was self-contradictory: status
    'failed' carrying a real VFS reference, copied forward from the row it
    overwrote.
  * resolving a triple with no history at all manufactured a 'success' row,
    which then blocked that client on that route permanently, with no account
    and no reference to investigate with. A mistyped combo did this silently,
    and the proxy accepts any combo label up to 80 characters.

`journal.resolve` had no test of its own; the API-level test monkeypatched it
away entirely.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.waitlist import journal  # noqa: E402
from src.waitlist.result import Status, WaitlistResult  # noqa: E402

ROUTE = "AE-CHE"
COMBO = "Dubai - SCHENGEN"
WHO = "u1"


@pytest.fixture()
def jrnl(tmp_path, monkeypatch):
    """An isolated journal file, so these never touch real registrations."""
    monkeypatch.setattr(journal, "JOURNAL_FILE", str(tmp_path / "journal.jsonl"))
    return journal


def _row(jrnl, status, reference=None, combo=COMBO):
    result = WaitlistResult(route=ROUTE, combo=combo, registrant_id=WHO,
                            status=status, account="acc@example.com",
                            reason="a run", vfs_reference=reference)
    result.finish(status, "a run")
    jrnl.append(result)


def _blocking(jrnl, combo=COMBO):
    entry = jrnl.blocking_entry(ROUTE, combo, WHO)
    return (entry or {}).get("status")


# --------------------------------------------------------------------------
# What may NOT be resolved
# --------------------------------------------------------------------------


def test_a_confirmed_registration_cannot_be_re_resolved(jrnl):
    """The headline regression: this used to unblock a real registration."""
    _row(jrnl, Status.SUCCESS, reference="SWDB79918334684")
    assert _blocking(jrnl) == "success"

    with pytest.raises(jrnl.NotDanglingError) as exc:
        jrnl.resolve(ROUTE, COMBO, WHO, Status.FAILED)

    assert exc.value.current_status == "success"
    assert _blocking(jrnl) == "success", (
        "the client must still be blocked — unblocking a confirmed "
        "registration is how a duplicate VFS place gets taken")


def test_a_failed_row_cannot_be_re_resolved(jrnl):
    """'failed' is settled too: it is not awaiting anyone's answer."""
    _row(jrnl, Status.FAILED)
    with pytest.raises(jrnl.NotDanglingError):
        jrnl.resolve(ROUTE, COMBO, WHO, Status.SUCCESS)


def test_a_triple_with_no_history_cannot_be_resolved(jrnl):
    """A mistyped combo must not invent a result and park the client."""
    _row(jrnl, Status.PENDING)                      # real row, different combo
    with pytest.raises(jrnl.NotDanglingError) as exc:
        jrnl.resolve(ROUTE, "Typo - NOT A COMBO", WHO, Status.SUCCESS)

    assert exc.value.current_status is None
    assert _blocking(jrnl, "Typo - NOT A COMBO") is None, (
        "a triple that never ran must not end up blocked")


def test_an_ambiguous_status_is_still_refused(jrnl):
    """Unchanged behaviour, asserted so it cannot regress alongside the rest."""
    _row(jrnl, Status.PENDING)
    with pytest.raises(ValueError, match="Resolve status must be"):
        jrnl.resolve(ROUTE, COMBO, WHO, "unknown")


# --------------------------------------------------------------------------
# What MAY be resolved
# --------------------------------------------------------------------------


@pytest.mark.parametrize("dangling", [Status.PENDING, Status.UNKNOWN])
def test_a_dangling_row_can_be_resolved_either_way(jrnl, dangling):
    _row(jrnl, dangling)
    result = jrnl.resolve(ROUTE, COMBO, WHO, Status.SUCCESS,
                          reference="SWDB-123")
    assert result.status == Status.SUCCESS


def test_resolving_failed_unblocks_the_client(jrnl):
    """The whole point of 'it did not register' — the client may run again."""
    _row(jrnl, Status.PENDING)
    assert _blocking(jrnl) is not None
    jrnl.resolve(ROUTE, COMBO, WHO, Status.FAILED)
    assert _blocking(jrnl) is None


def test_resolving_success_keeps_the_client_blocked(jrnl):
    _row(jrnl, Status.PENDING)
    jrnl.resolve(ROUTE, COMBO, WHO, Status.SUCCESS, reference="SWDB-123")
    assert _blocking(jrnl) == "success"


# --------------------------------------------------------------------------
# The reference is data, not prose
# --------------------------------------------------------------------------


def test_the_operators_reference_is_stored_on_the_row(jrnl):
    """Downstream readers use the structured field, not `reason`."""
    _row(jrnl, Status.PENDING)
    result = jrnl.resolve(ROUTE, COMBO, WHO, Status.SUCCESS,
                          reason="checked the portal", reference="SWDB-NEW-1")
    assert result.vfs_reference == "SWDB-NEW-1"


def test_a_failed_resolution_never_carries_a_reference(jrnl):
    """'it did not register' plus a real VFS reference is a contradiction.

    The old code copied the prior row's reference forward unconditionally, so
    marking a submit failed wrote exactly that.
    """
    _row(jrnl, Status.PENDING, reference="SWDB-FROM-EARLIER")
    result = jrnl.resolve(ROUTE, COMBO, WHO, Status.FAILED)
    assert result.vfs_reference is None


def test_an_earlier_reference_is_kept_when_none_is_supplied(jrnl):
    """A success with no typed reference should not lose a known one."""
    _row(jrnl, Status.PENDING, reference="SWDB-FROM-EARLIER")
    result = jrnl.resolve(ROUTE, COMBO, WHO, Status.SUCCESS)
    assert result.vfs_reference == "SWDB-FROM-EARLIER"
