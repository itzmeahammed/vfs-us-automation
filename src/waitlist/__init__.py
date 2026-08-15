"""VFS waitlist: detection (always on) and registration (opt-in, gated).

Two halves, deliberately kept apart:

    detect.py / notify.py    READ-ONLY. Called by the slot-check flow on every
                             run. Never mutates the page or the account. If the
                             registration half is disabled, broken or absent,
                             this keeps working exactly as it always has.

    register.py + friends    MUTATING. Runs only when explicitly enabled, for
                             combinations explicitly opted in, under caps, with
                             a durable journal and a hard commit boundary.

Configuration is split along the same line as the code:

    config/waitlist/<ROUTE>.json   WHERE things are — selectors, steps, pages
    config/registrants/<id>.json   ONE CLIENT — their route, their combos, and
                                   the data to type into the form

Adding a country is a new file in config/waitlist/. Adding a client is a new file
in config/registrants/ — one file, nothing else to touch. Neither requires a code
change.

A client file targets exactly ONE route; a client wanting two countries gets two
files. Nothing registers unless the combination is named in a client's "combos".

Typical use:

    python -m src.waitlist status                 # what's configured & enabled
    python -m src.waitlist check --route AE-CHE   # validate config, no browser
    python -m src.waitlist run --route AE-CHE --registrant ahmed --dry-run

IMPORT NOTE — why register() is not imported here
--------------------------------------------------
This module imports only the READ-ONLY half (detect, errors, result). The
registration half is reached via `from src.waitlist.register import register`.

That is deliberate. src/vfs_bot/waitlist.py (the shim the always-on slot check
imports) resolves through this package, and importing register here would pull
settings -> pydantic, playwright and the whole registration stack into the
slot-check path. The read-only path must stay light and must not acquire
dependencies it does not use: a missing pydantic should never be able to stop a
slot check from reporting.
"""

from src.waitlist.detect import (  # noqa: F401
    MARKER,
    as_registered_result,
    as_result,
    count_waitlist,
    is_offered,
    is_waitlist,
)
from src.waitlist.errors import (  # noqa: F401
    WaitlistCommittedError,
    WaitlistConfigError,
    WaitlistError,
    WaitlistNotOfferedError,
    WaitlistSkipped,
    WaitlistStepError,
)
from src.waitlist.result import Status, WaitlistResult  # noqa: F401

__all__ = [
    "MARKER",
    "as_result",
    "as_registered_result",
    "is_waitlist",
    "count_waitlist",
    "is_offered",
    "build_message",
    "notify",
    "notify_registered",
    "register",
    "WaitlistResult",
    "Status",
    "WaitlistError",
    "WaitlistConfigError",
    "WaitlistNotOfferedError",
    "WaitlistSkipped",
    "WaitlistStepError",
    "WaitlistCommittedError",
]
