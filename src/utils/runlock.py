"""Run locks for browser-driving runs: TWO independent lanes, not one.

WHY THIS EXISTS
---------------
`journal.py` documents that the waitlist journal assumes exactly ONE writer: the
read-check-write in `blocking_entry()` cannot race while that holds. Two
registration runs at once can therefore double-register a client — burning a real
appointment slot — or interleave appends and leave the journal inconsistent.

THE LANES
---------
    LANE_WAITLIST    every waitlist REGISTRATION run — auto-triggered or manual.
                     This is the one protecting the journal. It stays.

    LANE_SLOT_CHECK  the scheduled slot check. Excludes only another slot check
                     (a tick that overruns into the next one).

They are SEPARATE, so a registration and a slot check run at the same time. That
was not always true: originally everything took one lock, and a waitlist run cost
the next scheduled tick outright. The conflicts that justified it were:

    the journal        — a slot check never writes it, so no conflict
    VFS accounts       — waitlist uses a separate pool (waitlist/accounts.py)
    double-registering — a slot check never registers anyone
    Chrome CDP port    — REAL, and now fixed: chrome_launcher.resolve_port()
    Chrome profile dir — REAL, and now fixed: cleanup is scoped per run

Only the last two were genuine, and both were process-level rather than
data-level, so they were fixed where they belonged instead of by serialising two
unrelated jobs.

DO NOT put the lock back in run_task.ps1 / run_ec2.sh. Those wrappers used to
take the slot-check mutex and then launch the supervisor, which takes it too —
the parent held it, the child could never get it, and every tick silently
skipped while the wrapper still logged "Run finished (exit 0)". That killed slot
checking for 20 hours on 2026-08-28. Python owns these locks now, because only
Python can see a waitlist run started by the API.

    Windows   Global\\VfsSlotChecker   /  Global\\VfsWaitlistRun
    POSIX     /tmp/vfs-slot-checker.lock  /  /tmp/vfs-waitlist-run.lock

USAGE
-----
    from src.utils import runlock

    with runlock.acquire("waitlist-run", lane=runlock.LANE_WAITLIST):
        ...                                    # one registration run at a time

    # Non-blocking: skip this tick rather than queue behind the other run.
    with runlock.acquire("slot-check", lane=runlock.LANE_SLOT_CHECK,
                         timeout=0, on_busy="skip") as lock:
        if not lock.held:
            return
        ...

Each lane is REENTRANT within a single process and strictly exclusive between
processes. Holding one lane never blocks the other.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)

# LANES. There are two independent locks, not one.
#
# Originally everything took a single lock, so a waitlist registration blocked
# the next scheduled slot check outright — the tick was skipped, not delayed.
# That is heavier than the actual conflict: a slot check never writes the
# waitlist journal and never registers anyone, and waitlist accounts are a
# separate pool (src/waitlist/accounts.py). The only real collisions were
# process-level (Chrome's debug port and profile dir), and those are now fixed
# in chrome_launcher.py, so the two can genuinely run side by side.
#
# What still needs serialising is WAITLIST AGAINST WAITLIST: journal.py's
# read-check-write is not atomic, so two registration runs at once can
# double-register a client. That is the LANE_WAITLIST lock, and it stays.
#
# LANE_SLOT_CHECK keeps the historical name so a slot check still excludes
# another slot check.
LANE_SLOT_CHECK = "slot-check"
LANE_WAITLIST = "waitlist"

_LANE_NAMES = {
    LANE_SLOT_CHECK: ("Global\\VfsSlotChecker", "/tmp/vfs-slot-checker.lock"),
    LANE_WAITLIST: ("Global\\VfsWaitlistRun", "/tmp/vfs-waitlist-run.lock"),
}

# Kept for callers that referenced these directly.
WINDOWS_MUTEX_NAME = _LANE_NAMES[LANE_SLOT_CHECK][0]
POSIX_LOCKFILE = _LANE_NAMES[LANE_SLOT_CHECK][1]

# Default ceiling on how long to wait for the other run. A slot-check route can
# legitimately take several minutes; a waitlist registration longer. Waiting
# forever would let a stuck run block the scheduler indefinitely.
DEFAULT_TIMEOUT_SECONDS = 900.0

# Re-entrancy bookkeeping: the OS primitives below are per-process, so nested
# acquire() calls in one process must be counted rather than re-taken. Counted
# PER LANE — a process holding the waitlist lane must still be able to take the
# slot-check lane, so one shared counter would wrongly report it as re-entry.
_local_lock = threading.RLock()
_depth: dict = {}
_holder: dict = {}


class LockBusy(RuntimeError):
    """Raised when the lock is held elsewhere and the caller asked to fail."""


@dataclass
class LockHandle:
    """What `acquire()` yields. `held` is False only when on_busy='skip'."""

    held: bool
    owner: str
    waited_seconds: float = 0.0
    holder_hint: str = ""


# --------------------------------------------------------------------------
# Platform primitives
# --------------------------------------------------------------------------


class _WindowsMutex:
    """Wraps the same named mutex run_task.ps1 uses, via the Win32 API.

    A named kernel mutex is the Windows equivalent of flock: it is released
    automatically if the holding process dies, so a crashed run cannot wedge
    the lock permanently (we get WAIT_ABANDONED and take ownership).
    """

    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_ABANDONED = 0x00000080
    _WAIT_TIMEOUT = 0x00000102

    def __init__(self, name: str) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR
        ]
        self._kernel32.CreateMutexW.restype = wintypes.HANDLE
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self._kernel32.ReleaseMutex.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        # bInitialOwner=False: create it unowned, then contend for it below,
        # so this behaves identically whether or not we created it.
        self._handle = self._kernel32.CreateMutexW(None, False, name)
        if not self._handle:
            raise OSError(
                ctypes.get_last_error(),
                f"CreateMutexW failed for {name!r}",
            )

    def acquire(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds. True if we now own the mutex."""
        millis = 0 if timeout <= 0 else int(timeout * 1000)
        rc = self._kernel32.WaitForSingleObject(self._handle, millis)
        if rc == self._WAIT_OBJECT_0:
            return True
        if rc == self._WAIT_ABANDONED:
            # The previous holder died without releasing. We own it now; the
            # journal is append-only and fsync'd, so there is nothing to repair.
            log.warning(
                "Run lock was abandoned by a dead process — taking ownership. "
                "If a waitlist run died mid-submit, check `python -m src.waitlist "
                "journal` for a dangling 'pending' entry."
            )
            return True
        if rc == self._WAIT_TIMEOUT:
            return False
        raise OSError(
            self._ctypes.get_last_error(),
            f"WaitForSingleObject returned {rc}",
        )

    def release(self) -> None:
        self._kernel32.ReleaseMutex(self._handle)

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class _PosixLockFile:
    """Wraps the same lockfile run_ec2.sh flocks."""

    def __init__(self, path: str) -> None:
        import fcntl

        self._fcntl = fcntl
        self._path = path
        # Opened append-only so we never truncate a file another run holds.
        self._fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)

    def acquire(self, timeout: float) -> bool:
        """Poll for the flock until `timeout` elapses."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                self._fcntl.flock(self._fd, self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
                # Record who holds it, for a useful "busy" message elsewhere.
                os.ftruncate(self._fd, 0)
                os.write(self._fd, f"{os.getpid()}\n".encode())
                os.fsync(self._fd)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.25)

    def peek_holder(self) -> str:
        """Best-effort PID of the current holder, for logging only."""
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                pid = fh.read().strip()
            return f"pid {pid}" if pid else ""
        except OSError:
            return ""

    def release(self) -> None:
        try:
            self._fcntl.flock(self._fd, self._fcntl.LOCK_UN)
        except OSError:
            pass

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def _make_primitive(lane: str = LANE_SLOT_CHECK):
    """Return the right platform lock primitive for `lane`."""
    win_name, posix_path = _LANE_NAMES.get(lane, _LANE_NAMES[LANE_SLOT_CHECK])
    if sys.platform == "win32":
        return _WindowsMutex(win_name)
    return _PosixLockFile(posix_path)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


@contextmanager
def acquire(
    owner: str,
    *,
    lane: str = LANE_SLOT_CHECK,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    on_busy: str = "raise",
) -> Iterator[LockHandle]:
    """Hold one lane of the run lock for the duration of the block.

    Args:
        owner: Short label for who is taking it ("slot-check", "waitlist-run").
            Logged, so a blocked run says what it is waiting for.
        lane: Which lock to take — LANE_SLOT_CHECK or LANE_WAITLIST. The lanes
            are INDEPENDENT: a waitlist registration and a scheduled slot check
            no longer block each other. See the lane notes at the top of this
            module for why that is safe.
        timeout: Seconds to wait. 0 = try once and give up immediately.
        on_busy: What to do if the lock is unavailable within `timeout`:
            "raise" (default) -> LockBusy; "skip" -> yield a handle with
            held=False so the caller can return quietly, matching the
            scheduler's existing "skip this tick" behaviour.

    Yields:
        LockHandle. Check `.held` when using on_busy="skip".

    Re-entrant within one process (per lane); exclusive between processes.
    """
    if on_busy not in ("raise", "skip"):
        raise ValueError("on_busy must be 'raise' or 'skip'")

    # -- Re-entrant fast path: this process already holds THIS lane ---------
    with _local_lock:
        if _depth.get(lane, 0) > 0:
            _depth[lane] += 1
            log.debug("Run lock (%s) re-entered by %r (depth=%d)",
                      lane, owner, _depth[lane])
            try:
                yield LockHandle(held=True, owner=owner)
            finally:
                with _local_lock:
                    _depth[lane] -= 1
            return

    primitive = _make_primitive(lane)
    started = time.monotonic()
    try:
        got = primitive.acquire(timeout)
        waited = time.monotonic() - started

        if not got:
            hint = ""
            if isinstance(primitive, _PosixLockFile):
                hint = primitive.peek_holder()
            if lane == LANE_WAITLIST:
                why = ("Waitlist runs are serialised deliberately: two at once "
                       "can double-register a client.")
            else:
                why = ("Slot checks are serialised deliberately: two at once "
                       "would stack a second browser on the same schedule.")
            message = (
                f"Another {lane} run is already in progress"
                f"{f' ({hint})' if hint else ''}. "
                f"{owner!r} waited {waited:.0f}s and gave up. {why}"
            )
            if on_busy == "raise":
                log.error(message)
                raise LockBusy(message)
            log.info("%s — skipping.", message)
            yield LockHandle(held=False, owner=owner, waited_seconds=waited,
                             holder_hint=hint)
            return

        with _local_lock:
            _depth[lane] = 1
            _holder[lane] = owner

        if waited > 1.0:
            log.info("Run lock (%s) acquired by %r after waiting %.0fs.",
                     lane, owner, waited)
        else:
            log.debug("Run lock (%s) acquired by %r.", lane, owner)

        try:
            yield LockHandle(held=True, owner=owner, waited_seconds=waited)
        finally:
            with _local_lock:
                _depth[lane] = 0
                _holder.pop(lane, None)
            primitive.release()
            log.debug("Run lock (%s) released by %r.", lane, owner)
    finally:
        primitive.close()


def held_by(lane: str = LANE_SLOT_CHECK) -> Optional[str]:
    """The owner label if THIS process holds `lane`, else None.

    Only ever reports this process — it cannot see another process's holder.
    """
    with _local_lock:
        return _holder.get(lane) if _depth.get(lane, 0) > 0 else None


def is_held_locally(lane: str = LANE_SLOT_CHECK) -> bool:
    """True if this process currently holds `lane` of the run lock."""
    return held_by(lane) is not None
