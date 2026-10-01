"""A cross-process lock around read-modify-write of one JSON record.

THE BUG IT CLOSES
-----------------
Booking requests and invitations are edited by two processes: the API (an
agent changes dates, disables a request) and a booking child (it moves the
status to booking, then booked). Both read the file, change it, write it back.
Atomic writes stop a torn file, but not a lost update: the API reads, the
child reads, the API writes "disabled", the child writes "booked" from its
older copy — and the disable is gone, silently.

So every read-modify-write takes this lock first, on a sidecar `<file>.lock`.
The OS releases it if the holder dies, so a crashed process cannot wedge a
record.

    with locked(path):
        data = read(path); data[...] = ...; write(path, data)

Re-entrant within a thread, so a locked helper may call another one.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterator

DEFAULT_TIMEOUT = 15.0

_local = threading.local()


def _held() -> Dict[str, int]:
    if not hasattr(_local, "held"):
        _local.held = {}
    return _local.held


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class LockTimeout(TimeoutError):
    """Another process held the record for longer than the timeout."""


@contextmanager
def locked(path: str, timeout: float = DEFAULT_TIMEOUT) -> Iterator[None]:
    key = os.path.abspath(path)
    held = _held()
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return

    lock_path = key + ".lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise LockTimeout(f"{path} is locked by another process.")
            time.sleep(0.05)
        held[key] = 1
        try:
            yield
        finally:
            held.pop(key, None)
            _unlock(fd)
    finally:
        os.close(fd)
