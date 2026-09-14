"""The booking failure taxonomy.

The shape mirrors src/waitlist/errors.py, and the one distinction that matters
is the same one:

    BookingStepError       failed BEFORE the commit. Nothing was submitted, so
                           abandoning is free and retrying is safe.

    BookingCommittedError  failed AFTER the commit. A slot MAY have been taken.
                           Deliberately NOT a RetryableError, so the
                           supervisor's relaunch logic can never pick it up — a
                           submit that may have landed must never be replayed.

Everything else exists so a caller can tell those two apart without reading a
log message.
"""

from __future__ import annotations


class BookingError(Exception):
    """Base for every booking failure."""


class BookingConfigError(BookingError):
    """A config/booking/<ROUTE>.json is missing, malformed or unsafe.

    Always raised at LOAD time, before a browser exists — the alternative is
    discovering it half-way through a flow that has already touched the account.
    """


class BookingDisabled(BookingError):
    """The route (or booking generally) is switched off. Not a failure."""


class BookingSkipped(BookingError):
    """A guard declined before anything was attempted. Not a failure.

    Rate limits, caps, a dangling row awaiting a human — reasons to hold off,
    not signs anything is wrong.
    """


# --------------------------------------------------------------------------- #
# Pre-commit — safe to abandon                                                 #
# --------------------------------------------------------------------------- #

class BookingStepError(BookingError):
    """A step failed before the point of no return. Nothing was submitted."""


class ApplicationNotFoundError(BookingStepError):
    """No dashboard row matched the client we were sent to book.

    Usually mundane: the invitation was for a different account, or the entry
    was cancelled. It is NOT an occasion to relax the matching and try again.
    """


class IdentityMismatchError(BookingStepError):
    """The opened application is NOT the client we expected.

    The click-then-check design working as intended: opening a row commits
    nothing, so a wrong click costs nothing PROVIDED it is detected. This is
    that detection, and it must always abort rather than continue.
    """


class AmbiguousIdentityError(BookingStepError):
    """Several candidates matched equally well, so none was chosen.

    Refusing to guess. Booking neither client is a bad day; booking the wrong
    one is unrecoverable.
    """


class SlotGoneError(BookingStepError):
    """The slot vanished between seeing it and taking it.

    EXPECTED, not exceptional: first-come-first-served with many invitees.
    Reports must render it as a normal outcome so genuine failures stay visible.
    """


class InvitationExpiredError(BookingStepError):
    """The 36-48h window closed before the booking completed."""


# --------------------------------------------------------------------------- #
# Post-commit — a human's problem                                              #
# --------------------------------------------------------------------------- #

class BookingCommittedError(BookingError):
    """Something failed AFTER the commit. A slot may have been taken.

    NOT a RetryableError, deliberately and permanently: the supervisor's
    relaunch logic must never be able to replay a submit that may have landed.
    The only correct responses are to journal it, alert, and stop.
    """


class BookingUnconfirmedError(BookingCommittedError):
    """The submit went in but the confirmation was never read.

    NEEDS A HUMAN to check the portal. Never resolved by guessing, and never
    retried automatically.
    """
