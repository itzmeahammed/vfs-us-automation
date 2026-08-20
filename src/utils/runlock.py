"""One lock for every browser-driving run, across processes AND languages.

WHY THIS EXISTS
---------------
`journal.py` documents that the waitlist journal assumes exactly ONE writer:

    "this bot is run ON DEMAND, so there is exactly one writer and the
     read-check-write in blocking_entry() cannot race. If waitlist registration
     is ever moved onto the scheduler (concurrent runs), swap this for SQLite
     with a partial UNIQUE INDEX."

Auto-triggering waitlist registration from the slot checker does exactly what
that warning describes. Three writers become possible:

    1. the scheduled slot check      (run_task.ps1 / run_ec2.sh, twice hourly)
    2. an auto-triggered waitlist run
    3. a manual `python -m src.waitlist run`

Two of those overlapping can double-register a client — burning a real
appointment slot — or interleave appends and leave the journal inconsistent.
They also collide on a single fixed Chrome debugging port (see `cdp_port`).

This module is the cheap correct fix: a single mutual-exclusion lock that every
browser-driving entry point takes. It deliberately contends with the EXISTING
shell locks rather than inventing a new namespace:

    Windows   Global\\VfsSlotChecker      <- the same named mutex run_task.ps1:42 takes
    POSIX     /tmp/vfs-slot-checker.lock  <- the same lockfile run_ec2.sh:37 flocks

So a Python run and a scheduler tick exclude each other correctly with no
change to either shell script.

USAGE
-----
    from src.utils import runlock

    with runlock.acquire("waitlist-run"):
        ...                                    # only one such block runs at a time

    # Non-blocking: skip this tick rather than queue behind the other run.
    with runlock.acquire("slot-check", timeout=0, on_busy="skip") as lock:
        if not lock.held:
            return
        ...

The lock is REENTRANT within a single process (a supervisor that calls the
waitlist runner in-process does not deadlock against itself), but strictly
exclusive between processes.
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

# Names chosen to MATCH the existing shell locks — do not rename without
# updating run_task.ps1:42 and run_ec2.sh:37 in the same commit, or the
# scheduler and Python will stop excluding each other.
WINDOWS_MUTEX_NAME = "Global\\VfsSlotChecker"
POSIX_LOCKFILE = "/tmp/vfs-slot-checker.lock"

# Default ceiling on how long to wait for the other run. A slot-check route can
# legitimately take several minutes; a waitlist registration longer. Waiting
# forever would let a stuck run block the scheduler indefinitely.
DEFAULT_TIMEOUT_SECONDS = 900.0

# Re-entrancy bookkeeping: the OS primitives below are per-process, so nested
# acquire() calls in one process must be counted rather than re-taken.
_local_lock = threading.RLock()
_depth = 0
_holder: Optional[str] = None


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


def _make_primitive():
    """Return the right platform lock primitive."""
    if sys.platform == "win32":
        return _WindowsMutex(WINDOWS_MUTEX_NAME)
    return _PosixLockFile(POSIX_LOCKFILE)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


@contextmanager
def acquire(
    owner: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    on_busy: str = "raise",
) -> Iterator[LockHandle]:
    """Hold the global run lock for the duration of the block.

    Args:
        owner: Short label for who is taking it ("slot-check", "waitlist-run").
            Logged, so a blocked run says what it is waiting for.
        timeout: Seconds to wait. 0 = try once and give up immediately.
        on_busy: What to do if the lock is unavailable within `timeout`:
            "raise" (default) -> LockBusy; "skip" -> yield a handle with
            held=False so the caller can return quietly, matching the
            scheduler's existing "skip this tick" behaviour.

    Yields:
        LockHandle. Check `.held` when using on_busy="skip".

    Re-entrant within one process; exclusive between processes.
    """
    global _depth, _holder

    if on_busy not in ("raise", "skip"):
        raise ValueError("on_busy must be 'raise' or 'skip'")

    # -- Re-entrant fast path: this process already holds it ----------------
    with _local_lock:
        if _depth > 0:
            _depth += 1
            log.debug("Run lock re-entered by %r (depth=%d)", owner, _depth)
            try:
                yield LockHandle(held=True, owner=owner)
            finally:
                with _local_lock:
                    _depth -= 1
            return

    primitive = _make_primitive()
    started = time.monotonic()
    try:
        got = primitive.acquire(timeout)
        waited = time.monotonic() - started

        if not got:
            hint = ""
            if isinstance(primitive, _PosixLockFile):
                hint = primitive.peek_holder()
            message = (
                f"Another browser-driving run is already in progress"
                f"{f' ({hint})' if hint else ''}. "
                f"{owner!r} waited {waited:.0f}s and gave up. Runs are "
                "serialised deliberately: two at once can double-register a "
                "client or corrupt the waitlist journal."
            )
            if on_busy == "raise":
                log.error(message)
                raise LockBusy(message)
            log.info("%s — skipping.", message)
            yield LockHandle(held=False, owner=owner, waited_seconds=waited,
                             holder_hint=hint)
            return

        with _local_lock:
            _depth = 1
            _holder = owner

        if waited > 1.0:
            log.info("Run lock acquired by %r after waiting %.0fs.", owner, waited)
        else:
            log.debug("Run lock acquired by %r.", owner)

        try:
            yield LockHandle(held=True, owner=owner, waited_seconds=waited)
        finally:
            with _local_lock:
                _depth = 0
                _holder = None
            primitive.release()
            log.debug("Run lock released by %r.", owner)
    finally:
        primitive.close()


def held_by() -> Optional[str]:
    """The owner label if THIS process holds the lock, else None.

    Only ever reports this process — it cannot see another process's holder.
    """
    with _local_lock:
        return _holder if _depth > 0 else None


def is_held_locally() -> bool:
    """True if this process currently holds the run lock."""
    return held_by() is not None
