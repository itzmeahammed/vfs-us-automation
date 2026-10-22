"""The inbox watcher as a background thread inside the long-lived API process.

Why here and not a fourth scheduled task: the API is already the one process
that is always up (it serves the web app through the tunnel), it already has
the mail credentials and the booking code, and a status endpoint that lives
next to the thread can say exactly what it is doing. If the API restarts, the
watcher restarts with it; nothing else needs supervising.

The thread re-reads config each cycle, so PATCH /v1/switches (or an edit to
config.local.ini) takes effect at the next pass without a restart. With
[inbox] background_watch = false it idles and says so in the heartbeat.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from src.inbox import heartbeat

log = logging.getLogger(__name__)

_thread: Optional[threading.Thread] = None
_stop = threading.Event()

#: How long the idle loop sleeps while background_watch is off, and the floor
#: for the poll interval so a mis-set config cannot hammer the mail server.
IDLE_SECONDS = 30
MIN_POLL_SECONDS = 30
#: A short first delay so the API finishes booting before the first IMAP pass.
FIRST_DELAY_SECONDS = 5


def _loop() -> None:
    from src.inbox import service
    from src.settings import reload_from_disk, settings

    if _stop.wait(FIRST_DELAY_SECONDS):
        return
    while not _stop.is_set():
        try:
            reload_from_disk()
        except Exception as exc:                            # noqa: BLE001
            log.debug("Config reload failed (keeping current): %s", exc)
        try:
            cfg = settings().inbox
            enabled = bool(cfg.background_watch)
            poll = max(MIN_POLL_SECONDS, int(cfg.poll_seconds or 300))
        except Exception as exc:                            # noqa: BLE001
            log.warning("Inbox settings unreadable; watcher idling: %s", exc)
            enabled, poll = False, IDLE_SECONDS

        if not enabled:
            heartbeat.write(state=heartbeat.DISABLED, pid=os.getpid(),
                            detail="[inbox] background_watch = false")
            _stop.wait(IDLE_SECONDS)
            continue

        try:
            service.run_cycle("api")
        except Exception as exc:                            # noqa: BLE001
            log.exception("Inbox watcher cycle crashed (thread survives): %s", exc)
        _stop.wait(poll)

    heartbeat.write(state=heartbeat.STOPPED, pid=os.getpid())


def start() -> bool:
    """Start the watcher thread once. Returns True if it started now."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="inbox-watcher", daemon=True)
    _thread.start()
    log.info("Inbox watcher thread started.")
    return True


def stop(timeout: float = 10.0) -> None:
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=timeout)
        _thread = None


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()
