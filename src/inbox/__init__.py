"""Watch VFS account mailboxes and classify the mail VFS sends.

VFS drives the whole waitlist lifecycle by email. Two messages matter:

    "Successfully Added to Waitlist"        confirms a registration landed, and
                                            carries the Unique Reference Number.

    "Slots available for booking an ..."    the INVITATION: the client may now
                                            book, for 48 hours. The link in it
                                            is just the ordinary portal login
                                            URL — no token, no deep link.

So the invitation says only "something on this (account, route) is bookable".
It is a TRIGGER, not an instruction: which application it refers to is settled
later, against the VFS dashboard, which is authoritative. That is deliberate —
every fact this package declines to parse out of free prose is a per-country
rule nobody has to maintain.

    matcher.py   PURE. email dict -> Match. No IMAP, no clock, no files.
    seen.py      durable per-mailbox UID state, so a restart re-reads nothing.
    watcher.py   the IMAP loop that feeds matcher.py.

OBSERVATIONAL FIRST
-------------------
This package classifies, records and reports. It triggers NOTHING. That is not
an unfinished state — it is how the per-country configs get written from real
mail instead of guesswork, exactly as config/waitlist/_default.json was
extracted from a route already proven end to end rather than designed up front.
One country's wording (Italy's, in config/inbox/AE-ITA.json) is a sample, not a
specification.

Typical use:

    python -m src.inbox test --route AE-ITA      # matchers vs saved fixtures
    python -m src.inbox watch --once             # one pass over every mailbox
    python -m src.inbox watch                    # keep watching
"""

from src.inbox.matcher import (  # noqa: F401
    CONFIRMATION,
    INVITATION,
    UNMATCHED,
    Email,
    Match,
    classify,
)

__all__ = [
    "Email",
    "Match",
    "classify",
    "INVITATION",
    "CONFIRMATION",
    "UNMATCHED",
]
