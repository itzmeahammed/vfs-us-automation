"""Where a client is in the waitlist-to-appointment journey.

ONE RECORD, NOT TWO
-------------------
A client's journey has two halves — get onto the waitlist, then book the
appointment when VFS invites them — and it is tempting to give the second half
its own store. That would be a mistake: two stores can disagree about whether a
client is booked, and reconciling them is a bug class nobody should own.

So the existing journal stays the single record, and this module adds the states
it needs to describe the second half:

    registered ──▶ waiting ──▶ invited ──▶ booking ──▶ booked
                                  │           │
                                  │           ├─▶ booking_unknown  (needs a human)
                                  │           └─▶ slot_gone        (normal! retry)
                                  ├─▶ expired      (the 36-48h window lapsed)
                                  └─▶ cancelled    (entry cancelled at VFS)

TWO COMMIT BOUNDARIES ON ONE ROW
--------------------------------
This is the subtle part, and the reason phase and status are separate concepts.

`waitlist.Status.COMMITTED_STATES` currently means "this triple is spoken for" —
it stops a client being registered twice. Once a row can also mean "booked",
that single flag has to answer two different questions:

    registration committed?   don't put them on the waitlist again
    booking committed?        don't book them an appointment again

They are independent. A client can be registration-committed (on the waitlist)
for weeks while not booking-committed at all — that is the normal resting state.
`phase_of()` and `is_committed()` below are what keep the two apart, so a
booking-phase question never accidentally reads a registration-phase answer.

WHY A SEPARATE MODULE
---------------------
`waitlist/result.py` describes ONE registration attempt and is imported by the
read-only slot-check path. Booking states do not belong there: the registration
half must keep working unchanged even if none of this exists. This module
imports Status, extends it, and never modifies it.

Everything here is a PURE FUNCTION over plain strings. No I/O, no clock (except
where a deadline is explicitly passed in), so the whole state machine is
testable without a browser, a mailbox, or a database.
"""

from __future__ import annotations

from typing import Optional, Tuple

from src.waitlist.result import Status as RegistrationStatus

# --------------------------------------------------------------------------- #
# Phases                                                                       #
# --------------------------------------------------------------------------- #

#: Getting the client onto the waitlist. Owned by src/waitlist/.
PHASE_REGISTRATION = "registration"

#: Turning a waitlist entry into a booked appointment. Owned by src/booking/.
PHASE_BOOKING = "booking"


class BookingStatus:
    """States a row can hold once registration has succeeded.

    Deliberately NOT added to waitlist.Status: that class describes a single
    registration attempt and is read by the always-on slot-check path, which
    must not acquire booking concepts it never uses.
    """

    # --- waiting for VFS ---------------------------------------------------- #

    WAITING = "waiting"
    """On the waitlist, no invitation yet. The normal resting state, often for
    weeks. Distinct from registration's 'success' only in that it is explicit:
    'success' says the registration landed; 'waiting' says we are now watching
    for an invitation."""

    # --- VFS has invited us ------------------------------------------------- #

    INVITED = "invited"
    """VFS emailed 'slots available'. A DEADLINE applies (36h for Greece and
    Netherlands, 48h for Italy — per route, never assumed) and it is measured
    from the email's server timestamp, not from when we read it."""

    EXPIRED = "expired"
    """The invitation window closed with no booking. A real outcome that must be
    recorded and reported, not a row silently rotting in 'invited'. The client
    keeps their waitlist entry, so the next invitation still applies."""

    # --- booking in flight -------------------------------------------------- #

    BOOKING = "booking"
    """A booking run is walking the pages. Nothing is committed yet — the commit
    is the slot pick."""

    BOOKING_PENDING = "booking_pending"
    """WRITE-AHEAD MARKER. Written and fsync'd immediately BEFORE the committing
    click. If the process dies here we still know a slot MAY have been taken —
    the difference between 'we can recover' and 'no idea what happened'."""

    # --- terminal ----------------------------------------------------------- #

    BOOKED = "booked"
    """Confirmed appointment. Terminal, and the only genuinely happy ending."""

    BOOKING_UNKNOWN = "booking_unknown"
    """Submitted, outcome unconfirmed. NEEDS A HUMAN to check the portal. Never
    retried automatically — a slot that may have been taken must not be taken
    twice."""

    SLOT_GONE = "slot_gone"
    """The slot vanished between seeing it and taking it. NOT A FAILURE — with
    first-come-first-served and many invitees this is expected, and it must read
    as normal in reports so real failures stay visible. The client is still
    waitlisted and still invited (if the window is open), so a retry is fine."""

    BOOKING_FAILED = "booking_failed"
    """Failed BEFORE the commit. Nothing was submitted, so a retry is safe."""

    CANCELLED = "cancelled"
    """The waitlist entry was cancelled at VFS (their 'Waitlist cancellation'
    email, or someone cancelling in the portal). The client is no longer
    waitlisted — a 'success' row that says otherwise is now stale."""

    # ----------------------------------------------------------------------- #
    # Sets. Membership questions belong here, not spelled out at call sites.
    # ----------------------------------------------------------------------- #

    #: A booking may have been created at VFS. These BLOCK a further attempt.
    #: BOOKING_PENDING counts: an in-flight submit may well have landed.
    COMMITTED = (BOOKING_PENDING, BOOKED, BOOKING_UNKNOWN)

    #: A human must look at the VFS account before this row can move on.
    NEEDS_ATTENTION = (BOOKING_PENDING, BOOKING_UNKNOWN)

    #: Nothing further will happen on its own.
    TERMINAL = (BOOKED, CANCELLED, EXPIRED)

    #: Safe to attempt again: nothing was committed, and the entry still exists.
    RETRYABLE = (SLOT_GONE, BOOKING_FAILED)

    #: Every booking-phase state, for validation.
    ALL = (WAITING, INVITED, EXPIRED, BOOKING, BOOKING_PENDING, BOOKED,
           BOOKING_UNKNOWN, SLOT_GONE, BOOKING_FAILED, CANCELLED)


# --------------------------------------------------------------------------- #
# Which phase does a status belong to?                                         #
# --------------------------------------------------------------------------- #

def phase_of(status: str) -> str:
    """Whether a status describes the registration half or the booking half.

    The guard that keeps the two commit boundaries apart: a booking-phase
    question must never be answered by a registration-phase status, or a client
    merely on the waitlist would read as already booked.
    """
    return PHASE_BOOKING if status in BookingStatus.ALL else PHASE_REGISTRATION


def is_committed(status: str, phase: str = PHASE_BOOKING) -> bool:
    """Whether `status` blocks a further attempt IN THAT PHASE.

    Explicitly phase-scoped, because the answer genuinely differs:

        is_committed("success", PHASE_REGISTRATION)  -> True   already waitlisted
        is_committed("success", PHASE_BOOKING)       -> False  but NOT booked

    A single `committed` flag would have to pick one of those and be wrong about
    the other.
    """
    if phase == PHASE_REGISTRATION:
        return status in RegistrationStatus.COMMITTED_STATES
    return status in BookingStatus.COMMITTED


def needs_attention(status: str) -> bool:
    """Whether a human must check the VFS account before this row moves on."""
    return (status in BookingStatus.NEEDS_ATTENTION
            or status in RegistrationStatus.NEEDS_ATTENTION)


def is_terminal(status: str) -> bool:
    """Whether nothing further will happen on its own."""
    return status in BookingStatus.TERMINAL


# --------------------------------------------------------------------------- #
# Transitions                                                                  #
# --------------------------------------------------------------------------- #

#: What may follow what. A machine that permits anything is not a machine, and
#: the transitions it forbids are the interesting ones:
#:
#:   * nothing returns from BOOKED — a booked appointment is not re-bookable
#:   * nothing returns from BOOKING_UNKNOWN except a human's resolution
#:   * INVITED cannot go straight to BOOKED — it must pass through BOOKING, so
#:     the write-ahead marker is always on disk before a commit
_TRANSITIONS = {
    RegistrationStatus.SUCCESS: (BookingStatus.WAITING, BookingStatus.CANCELLED),

    BookingStatus.WAITING: (BookingStatus.INVITED, BookingStatus.CANCELLED),

    BookingStatus.INVITED: (BookingStatus.BOOKING, BookingStatus.EXPIRED,
                            BookingStatus.CANCELLED),

    BookingStatus.BOOKING: (BookingStatus.BOOKING_PENDING,
                            BookingStatus.SLOT_GONE,
                            BookingStatus.BOOKING_FAILED,
                            BookingStatus.EXPIRED),

    BookingStatus.BOOKING_PENDING: (BookingStatus.BOOKED,
                                    BookingStatus.BOOKING_UNKNOWN,
                                    BookingStatus.SLOT_GONE),

    # Retryable: the entry still exists and the window may still be open.
    BookingStatus.SLOT_GONE: (BookingStatus.INVITED, BookingStatus.BOOKING,
                              BookingStatus.EXPIRED, BookingStatus.CANCELLED),
    BookingStatus.BOOKING_FAILED: (BookingStatus.INVITED, BookingStatus.BOOKING,
                                   BookingStatus.EXPIRED, BookingStatus.CANCELLED),

    # An expired window does not end the waitlist entry — the next invitation
    # still applies.
    BookingStatus.EXPIRED: (BookingStatus.WAITING, BookingStatus.INVITED,
                            BookingStatus.CANCELLED),

    # Terminal, or human-only.
    BookingStatus.BOOKED: (),
    BookingStatus.CANCELLED: (),
    BookingStatus.BOOKING_UNKNOWN: (BookingStatus.BOOKED,
                                    BookingStatus.BOOKING_FAILED),
}


def can_transition(current: str, target: str) -> bool:
    """Whether `current -> target` is a legal move."""
    return target in _TRANSITIONS.get(current, ())


def check_transition(current: str, target: str) -> Tuple[bool, str]:
    """`can_transition`, plus a reason when it is refused.

    Returns (allowed, reason). The reason names what IS allowed, because the
    common cause of a refusal is a caller assuming a shortcut that the machine
    deliberately forbids — INVITED straight to BOOKED, say.
    """
    if target not in BookingStatus.ALL and phase_of(target) == PHASE_BOOKING:
        return False, f"'{target}' is not a known booking status."

    if can_transition(current, target):
        return True, ""

    allowed = _TRANSITIONS.get(current)
    if allowed is None:
        return False, f"'{current}' has no transitions defined (is it terminal?)."
    if not allowed:
        return False, f"'{current}' is terminal — nothing may follow it."
    return False, (
        f"'{current}' -> '{target}' is not allowed. "
        f"From '{current}' the legal moves are: {', '.join(allowed)}."
    )


# --------------------------------------------------------------------------- #
# The invitation deadline                                                      #
# --------------------------------------------------------------------------- #

def deadline_from(received_epoch: float, validity_hours: int) -> Optional[float]:
    """When an invitation window closes, as epoch seconds.

    Measured from the EMAIL'S OWN timestamp, never from now. A watcher that was
    down for a day must not silently extend a 36-hour window — that would have
    the system act on an invitation VFS has already retired.

    Returns None when either input is missing: better to report no deadline than
    to invent one.
    """
    if not received_epoch or not validity_hours:
        return None
    return received_epoch + validity_hours * 3600


def is_expired(deadline_epoch: Optional[float], now_epoch: float) -> bool:
    """Whether an invitation window has closed.

    `now` is passed in rather than read from the clock so this stays pure and
    testable. A row with no deadline is never expired — an unknown window is not
    evidence of a closed one.
    """
    if not deadline_epoch:
        return False
    return now_epoch >= deadline_epoch


def hours_remaining(deadline_epoch: Optional[float],
                    now_epoch: float) -> Optional[float]:
    """Hours left on an invitation. Negative once lapsed; None if unknown."""
    if not deadline_epoch:
        return None
    return (deadline_epoch - now_epoch) / 3600.0
