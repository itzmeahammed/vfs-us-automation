"""One inbox CYCLE: read the mailboxes, then act on what they said.

    pass    watcher.run_pass()      classify mail; record invitations and
                                    appointment confirmations (always)
    act     invites.process_pending()  book / escalate each open invitation
                                    (only while [switches] invite_booking is on)

Three callers share it — the API's background thread (src/inbox/background.py),
the supervisor at the end of a slot-check run, and `python -m src.inbox watch`
— and they are kept from overlapping by a file lock: a second caller finding
the lock held SKIPS, it does not wait, because the pass it wanted is the one
already running. Each cycle writes the heartbeat (src/inbox/heartbeat.py) so
the status endpoint can show the watcher doing its job.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from src.inbox import heartbeat

log = logging.getLogger(__name__)

LOCK_PATH = os.path.join(heartbeat.STATE_DIR, "inbox_pass")


def run_cycle(trigger: str, act: Optional[bool] = None) -> Dict[str, Any]:
    """One pass and, when acting, one round of invitation processing.

    `act` None means "whatever [switches] invite_booking says". Returns a
    summary dict; a cycle that could not take the lock returns
    {"skipped": <why>} and touches nothing.
    """
    from src.utils.filelock import LockTimeout, locked

    try:
        with locked(LOCK_PATH, timeout=1.0):
            return _cycle(trigger, act)
    except LockTimeout:
        log.info("Inbox pass skipped (%s): another pass is running.", trigger)
        return {"skipped": "another inbox pass is running", "trigger": trigger}


def _cycle(trigger: str, act: Optional[bool]) -> Dict[str, Any]:
    from src.settings import settings

    started = time.time()
    heartbeat.write(state=heartbeat.RUNNING, trigger=trigger, pid=os.getpid(),
                    last_pass_started_at=heartbeat._iso(started),
                    last_pass_started_epoch=started)
    summary: Dict[str, Any] = {"trigger": trigger,
                               "started_at": heartbeat._iso(started)}
    error = ""
    try:
        from src.inbox import watcher
        result = watcher.run_pass()
        summary.update({
            "mailboxes_checked": result.mailboxes_checked,
            "mailboxes_failed": list(result.mailboxes_failed),
            "messages_seen": result.messages_seen,
            "matched": len(result.observations),
            "invitations": len(result.invitations()),
            "waitlist_confirmations": len(result.confirmations()),
            "appointment_confirmations": len(result.appointments()),
            "expiries": len(result.expiries()),
        })
        try:
            from src.inbox.report import report
            report(result)
        except Exception as exc:                            # noqa: BLE001
            log.warning("Inbox digest failed: %s", exc)

        if act is None:
            act = bool(settings().switches.invite_booking)
        summary["acting"] = bool(act)
        if act:
            from src.booking import invites
            summary["bookings_started"] = invites.process_pending()
        else:
            summary["bookings_started"] = []
    except Exception as exc:                                # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        log.exception("Inbox cycle failed (%s): %s", trigger, exc)

    finished = time.time()
    summary["finished_at"] = heartbeat._iso(finished)
    summary["seconds"] = round(finished - started, 1)
    if error:
        summary["error"] = error

    try:
        poll = int(settings().inbox.poll_seconds or 300)
    except Exception:                                       # noqa: BLE001
        poll = 300
    previous = heartbeat.read()
    heartbeat.write(
        state=heartbeat.ERROR if error else heartbeat.IDLE,
        last_pass_finished_at=summary["finished_at"],
        last_pass_finished_epoch=finished,
        last_pass_seconds=summary["seconds"],
        last_error=error,
        last_pass=summary,
        passes=int(previous.get("passes") or 0) + 1,
        poll_seconds=poll,
        next_pass_at=heartbeat._iso(finished + poll) if trigger == "api" else None,
    )
    return summary
