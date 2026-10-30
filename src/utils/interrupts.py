"""Make a cancelled job stop the way Ctrl-C does, on Windows too.

*** WHY THIS FILE EXISTS. ***

`src/booking/probe.py` treats an interrupt as a first-class outcome: it names
the step it was on, says whether a payment is in flight, and its `finally`
closes Chrome. All of that hangs on one thing - KeyboardInterrupt actually being
raised in the child.

On POSIX that is free: the API sends SIGINT and Python raises it.

On Windows there is no SIGINT to send between processes. The only interrupt
that crosses a process boundary is a console control event, and the one that can
be aimed at a single process group is CTRL_BREAK_EVENT. That arrives as SIGBREAK
- and Python's default action for SIGBREAK is to terminate the process, NOT to
raise KeyboardInterrupt. Only SIGINT gets that treatment. So the child died
exactly as abruptly as before: no handler, no `finally`, Chrome left on screen
(AE-FRA job aa0986956806d353, 2026-10-30, cancelled from the web app one step
before payment).

Hence this: the child opts in, by making SIGBREAK raise KeyboardInterrupt like
SIGINT already does. One line in each entry point, and every existing interrupt
handler starts working for a web-app cancel as well as a terminal Ctrl-C.

Deliberately NOT a handler that cleans up by itself. The cleanup lives with the
code that knows what is open - raising the exception is all this does, so there
is exactly one teardown path for both a terminal Ctrl-C and a remote cancel.
"""

from __future__ import annotations

import logging
import signal
import sys

log = logging.getLogger(__name__)


def _raise_keyboard_interrupt(signum, frame):      # noqa: ANN001, ARG001
    raise KeyboardInterrupt()


def install_graceful_stop() -> bool:
    """Make a remote cancel reach this process as KeyboardInterrupt.

    Returns True when a handler was installed. Safe to call more than once, and
    safe off the main thread (where `signal.signal` raises) - it reports False
    rather than bringing down a worker over a signal handler.
    """
    if sys.platform != "win32":
        # SIGINT already raises KeyboardInterrupt, and that is what the API
        # sends to the process group. Nothing to add.
        return False
    if not hasattr(signal, "SIGBREAK"):
        return False
    try:
        signal.signal(signal.SIGBREAK, _raise_keyboard_interrupt)
        return True
    except (ValueError, OSError, RuntimeError) as exc:
        log.debug("Could not install the SIGBREAK handler: %s", exc)
        return False
