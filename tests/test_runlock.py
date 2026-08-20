"""Tests for the global run lock (src/utils/runlock.py).

The important assertion is CROSS-PROCESS exclusion — an in-process test would
pass trivially against a threading.Lock and prove nothing. So the contention
tests spawn a real child process that takes the lock and holds it.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.utils import runlock  # noqa: E402


# --------------------------------------------------------------------------
# Helper: a child process that holds the lock for N seconds
# --------------------------------------------------------------------------


def _spawn_holder(hold_seconds: float) -> subprocess.Popen:
    """Start a child that takes the run lock and holds it."""
    code = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from src.utils import runlock
        with runlock.acquire("test-holder", timeout=10):
            print("HELD", flush=True)
            time.sleep({hold_seconds})
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Wait for the child to confirm it actually holds the lock, so the test
    # never races the child's startup.
    line = proc.stdout.readline().strip()
    assert line == "HELD", f"child failed to take the lock: {line!r}"
    return proc


# --------------------------------------------------------------------------
# Basic behaviour
# --------------------------------------------------------------------------


def test_acquire_yields_held_handle():
    """The happy path: an uncontended lock is granted."""
    with runlock.acquire("test") as lock:
        assert lock.held is True
        assert lock.owner == "test"


def test_released_after_block():
    """The lock must not leak once the block exits."""
    with runlock.acquire("first"):
        pass
    # If it leaked, this second acquire would time out.
    with runlock.acquire("second", timeout=2) as lock:
        assert lock.held is True


def test_released_even_if_body_raises():
    """An exception inside the block must still release the lock."""
    with pytest.raises(ValueError):
        with runlock.acquire("boom"):
            raise ValueError("kaboom")
    with runlock.acquire("after", timeout=2) as lock:
        assert lock.held is True


def test_reentrant_within_one_process():
    """Nested acquires in one process must not deadlock.

    A supervisor that calls the waitlist runner in-process hits exactly this.
    """
    with runlock.acquire("outer") as outer:
        assert outer.held
        with runlock.acquire("inner", timeout=1) as inner:
            assert inner.held is True
        # Still held by the outer block after the inner one exits.
        assert runlock.is_held_locally() is True
    assert runlock.is_held_locally() is False


def test_held_by_reports_owner():
    """held_by() names the current in-process owner."""
    assert runlock.held_by() is None
    with runlock.acquire("slot-check"):
        assert runlock.held_by() == "slot-check"
    assert runlock.held_by() is None


def test_invalid_on_busy_rejected():
    """A typo in on_busy must fail loudly, not silently behave as 'raise'."""
    with pytest.raises(ValueError):
        with runlock.acquire("x", on_busy="wat"):
            pass


# --------------------------------------------------------------------------
# Cross-process exclusion — the point of the module
# --------------------------------------------------------------------------


def test_second_process_is_blocked():
    """While a child holds the lock, this process must NOT get it."""
    holder = _spawn_holder(hold_seconds=5)
    try:
        with pytest.raises(runlock.LockBusy):
            with runlock.acquire("blocked", timeout=0):
                pytest.fail("acquired a lock another process holds")
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_on_busy_skip_yields_unheld_handle():
    """on_busy='skip' returns held=False instead of raising.

    This mirrors the scheduler's existing "previous run still in progress —
    skipping this tick" behaviour.
    """
    holder = _spawn_holder(hold_seconds=5)
    try:
        with runlock.acquire("skipper", timeout=0, on_busy="skip") as lock:
            assert lock.held is False
            assert lock.owner == "skipper"
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_lock_is_granted_after_holder_exits():
    """Once the holder finishes, the lock becomes available."""
    holder = _spawn_holder(hold_seconds=1.5)
    try:
        started = time.monotonic()
        with runlock.acquire("waiter", timeout=15) as lock:
            assert lock.held is True
            # We should have actually waited for the holder, not raced past it.
            assert time.monotonic() - started > 0.5
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)


def test_lock_survives_a_killed_holder():
    """A crashed run must not wedge the lock forever.

    Windows reports WAIT_ABANDONED and transfers ownership; POSIX releases the
    flock when the fd closes on process death. Either way the next run proceeds.
    """
    holder = _spawn_holder(hold_seconds=30)
    holder.kill()
    holder.wait(timeout=10)
    # Give the OS a moment to reap the handle.
    time.sleep(0.5)
    with runlock.acquire("after-crash", timeout=10) as lock:
        assert lock.held is True


# --------------------------------------------------------------------------
# Interop with the EXISTING shell locks
# --------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex only")
def test_excludes_the_powershell_scheduler_mutex():
    r"""Python and run_task.ps1 must contend for the SAME lock.

    This is the whole point of reusing 'Global\VfsSlotChecker' rather than
    inventing a new name: the scheduled slot check (run_task.ps1:42) and a
    waitlist run must exclude each other with no change to the shell script.

    Regression guard — if someone renames WINDOWS_MUTEX_NAME without updating
    run_task.ps1, this fails and says so.
    """
    ps_probe = (
        "$m=New-Object System.Threading.Mutex($false,"
        f"'{runlock.WINDOWS_MUTEX_NAME}');"
        "$g=$m.WaitOne(0); Write-Output $g; if($g){$m.ReleaseMutex()}; $m.Dispose()"
    )

    def powershell_can_take_it() -> bool:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_probe],
            capture_output=True, text=True, timeout=60,
        )
        return out.stdout.strip() == "True"

    # Free before we start.
    assert powershell_can_take_it(), "lock was already held before the test"

    with runlock.acquire("python-run", timeout=5) as lock:
        assert lock.held is True
        # The scheduler must NOT be able to start a run right now.
        assert not powershell_can_take_it(), (
            "PowerShell acquired the mutex while Python held the run lock — "
            "the scheduler and the waitlist runner are NOT excluding each other"
        )

    # Released cleanly, so the next scheduled tick proceeds normally.
    assert powershell_can_take_it(), "run lock was not released"
