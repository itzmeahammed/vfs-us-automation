"""Turning a waitlist invitation into a booked appointment.

WHERE THIS SITS
---------------
    src/waitlist/   gets a client ONTO the waitlist          (works today)
    src/inbox/      notices VFS's "slots available" email    (works today)
    src/booking/    books the appointment                    (this package)

THE FLOW
--------
VFS emails "Appointment slots ... are now available for booking", valid for
36 hours (Greece, Netherlands) or 48 (Italy). The link is the ordinary portal
login page — no token, no deep link — so booking means: log in, find that
client's waitlisted application on the dashboard, and finish the steps.

    invitation ─▶ resolve WHO ─▶ open their row ─▶ VERIFY ─▶ forms ─▶ pick slot
                                                      │                   │
                                                   abort if           COMMITS
                                                   wrong

IDENTITY IS EXACT, NOT FUZZY
----------------------------
The confirmation email's "Unique Reference Number" is the same value the
dashboard shows as "Group Reference Number" (confirmed for Greece). So the
reference is stored at registration and used to select the dashboard row
exactly; the name in the invitation only narrows the shortlist first.

Where a reference is missing, name matching stands alone — and that is exactly
where `require_unique_match` refuses rather than guesses. Booking the wrong
client is unrecoverable; missing an invitation is a bad day. The code encodes
that asymmetry throughout.

THE COMMIT BOUNDARY
-------------------
Exactly one step is marked "commits": true — for booking, the SLOT PICK, where
the slot leaves the pool. Before it, a failure is BookingStepError: abandon
quietly, nothing changed. After it, BookingCommittedError, which is deliberately
NOT retryable. Same rule the waitlist registration half already follows.

MODULE MAP
----------
    lifecycle.py   the states a client moves through, and the two independent
                   commit boundaries on one journal row      PURE
    identity.py    who is this? name normalisation, confidence, verification
                                                             PURE
    config.py      config/booking/<ROUTE>.json: steps, identity policy
    errors.py      the failure taxonomy, split at the commit boundary

The pure modules carry the decisions that must not be wrong, which is why they
hold no I/O: they can be tested exhaustively offline, including the adversarial
cases that are hard to produce against a live portal and unaffordable to get
wrong there.
"""

from src.booking.errors import (  # noqa: F401
    AmbiguousIdentityError,
    ApplicationNotFoundError,
    BookingCommittedError,
    BookingConfigError,
    BookingDisabled,
    BookingError,
    BookingSkipped,
    BookingStepError,
    BookingUnconfirmedError,
    IdentityMismatchError,
    InvitationExpiredError,
    SlotGoneError,
)
from src.booking.identity import (  # noqa: F401
    Candidate,
    Confidence,
    MatchResult,
    normalise_name,
    normalise_reference,
    resolve,
    verify,
)
from src.booking.lifecycle import (  # noqa: F401
    PHASE_BOOKING,
    PHASE_REGISTRATION,
    BookingStatus,
    can_transition,
    check_transition,
    deadline_from,
    is_committed,
    is_expired,
    is_terminal,
    needs_attention,
)

__all__ = [
    # lifecycle
    "BookingStatus", "PHASE_BOOKING", "PHASE_REGISTRATION",
    "can_transition", "check_transition", "is_committed", "is_terminal",
    "needs_attention", "deadline_from", "is_expired",
    # identity
    "Candidate", "Confidence", "MatchResult",
    "resolve", "verify", "normalise_name", "normalise_reference",
    # errors
    "BookingError", "BookingConfigError", "BookingDisabled", "BookingSkipped",
    "BookingStepError", "ApplicationNotFoundError", "IdentityMismatchError",
    "AmbiguousIdentityError", "SlotGoneError", "InvitationExpiredError",
    "BookingCommittedError", "BookingUnconfirmedError",
]
