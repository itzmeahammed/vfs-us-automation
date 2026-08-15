"""Compatibility shim — the waitlist feature now lives in src/waitlist/.

The read-only detection and Telegram-notification code that used to live here
moved to its own package so that registration (which MUTATES the VFS account)
could be added alongside it without entangling the two:

    src/waitlist/detect.py     is_offered, MARKER, as_result, is_waitlist,
                               count_waitlist   (read-only — this file's old job)
    src/waitlist/notify.py     build_message, notify
    src/waitlist/register.py   the new, opt-in registration flow

This module re-exports the read-only names ONLY, so every existing import keeps
working unchanged:

    src/vfs_bot/slot_check.py     is_offered / as_result / notify
    src/supervisor.py             count_waitlist
    src/utils/telegram_message.py is_waitlist
    tests/test_waitlist*.py

Registration is deliberately NOT re-exported here: the always-on slot-check path
should never be one attribute access away from a mutating call. Import it
explicitly from src.waitlist when you mean it.
"""

from src.waitlist.detect import (  # noqa: F401
    MARKER,
    as_registered_result,
    as_result,
    count_waitlist,
    is_offered,
    is_waitlist,
)
from src.waitlist.notify import build_message, notify  # noqa: F401

__all__ = [
    "MARKER",
    "as_result",
    "as_registered_result",
    "is_waitlist",
    "count_waitlist",
    "is_offered",
    "build_message",
    "notify",
]
