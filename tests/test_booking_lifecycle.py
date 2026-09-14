"""The state machine, and the two commit boundaries it keeps apart.

The interesting assertions here are the REFUSALS. A machine that permits
everything is not a machine, and the transitions it forbids are what stop a
booked appointment being re-booked or an in-flight submit being replayed.
"""

import pytest

from src.booking.lifecycle import (
    PHASE_BOOKING,
    PHASE_REGISTRATION,
    BookingStatus,
    can_transition,
    check_transition,
    deadline_from,
    hours_remaining,
    is_committed,
    is_expired,
    is_terminal,
    needs_attention,
    phase_of,
)
from src.waitlist.result import Status as RegistrationStatus


# --------------------------------------------------------------------------- #
# The two commit boundaries — the subtle part                                  #
# --------------------------------------------------------------------------- #

def test_registered_means_committed_for_registration_but_not_for_booking():
    """THE distinction the whole module exists for. A client can sit
    registration-committed (on the waitlist) for weeks while not
    booking-committed at all — that is the normal resting state."""
    assert is_committed(RegistrationStatus.SUCCESS, PHASE_REGISTRATION) is True
    assert is_committed(RegistrationStatus.SUCCESS, PHASE_BOOKING) is False


def test_booked_means_committed_for_booking():
    assert is_committed(BookingStatus.BOOKED, PHASE_BOOKING) is True


def test_an_in_flight_booking_submit_blocks_a_retry():
    """BOOKING_PENDING is the write-ahead marker: the submit may have landed, so
    it must block exactly as a confirmed booking does."""
    assert is_committed(BookingStatus.BOOKING_PENDING, PHASE_BOOKING) is True


@pytest.mark.parametrize(
    "status",
    [BookingStatus.WAITING, BookingStatus.INVITED, BookingStatus.BOOKING,
     BookingStatus.SLOT_GONE, BookingStatus.BOOKING_FAILED,
     BookingStatus.EXPIRED],
)
def test_uncommitted_booking_states_do_not_block(status):
    assert is_committed(status, PHASE_BOOKING) is False


def test_phase_of_separates_the_two_vocabularies():
    assert phase_of(BookingStatus.BOOKED) == PHASE_BOOKING
    assert phase_of(BookingStatus.WAITING) == PHASE_BOOKING
    assert phase_of(RegistrationStatus.SUCCESS) == PHASE_REGISTRATION
    assert phase_of(RegistrationStatus.PENDING) == PHASE_REGISTRATION


# --------------------------------------------------------------------------- #
# Transitions that must be allowed                                             #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "current,target",
    [
        (RegistrationStatus.SUCCESS, BookingStatus.WAITING),
        (BookingStatus.WAITING, BookingStatus.INVITED),
        (BookingStatus.INVITED, BookingStatus.BOOKING),
        (BookingStatus.BOOKING, BookingStatus.BOOKING_PENDING),
        (BookingStatus.BOOKING_PENDING, BookingStatus.BOOKED),
        (BookingStatus.BOOKING_PENDING, BookingStatus.BOOKING_UNKNOWN),
        (BookingStatus.INVITED, BookingStatus.EXPIRED),
        (BookingStatus.EXPIRED, BookingStatus.INVITED),
    ],
)
def test_the_happy_path_and_its_branches_are_allowed(current, target):
    assert can_transition(current, target)


def test_an_expired_window_does_not_end_the_waitlist_entry():
    """The client keeps their entry, so the NEXT invitation still applies."""
    assert can_transition(BookingStatus.EXPIRED, BookingStatus.WAITING)
    assert can_transition(BookingStatus.EXPIRED, BookingStatus.INVITED)


@pytest.mark.parametrize(
    "status", [BookingStatus.SLOT_GONE, BookingStatus.BOOKING_FAILED]
)
def test_pre_commit_outcomes_are_retryable(status):
    """Nothing was submitted, so trying again is safe."""
    assert can_transition(status, BookingStatus.BOOKING)
    assert status in BookingStatus.RETRYABLE


# --------------------------------------------------------------------------- #
# Transitions that must be REFUSED — the important half                        #
# --------------------------------------------------------------------------- #

def test_a_booked_appointment_can_never_transition():
    """Terminal. Re-booking a confirmed appointment takes a second slot for one
    client and denies it to somebody else."""
    for target in BookingStatus.ALL:
        assert not can_transition(BookingStatus.BOOKED, target), target
    assert is_terminal(BookingStatus.BOOKED)


def test_invited_cannot_jump_straight_to_booked():
    """It must pass through BOOKING, so the write-ahead marker is always on disk
    before anything commits. A shortcut here would lose the only evidence that a
    submit was about to happen."""
    assert not can_transition(BookingStatus.INVITED, BookingStatus.BOOKED)


def test_a_cancelled_entry_is_terminal():
    assert is_terminal(BookingStatus.CANCELLED)
    for target in BookingStatus.ALL:
        assert not can_transition(BookingStatus.CANCELLED, target), target


def test_an_unknown_booking_can_only_be_resolved_by_a_human():
    """Not retried automatically: a slot that may have been taken must not be
    taken twice. The only exits are the two answers a human can give."""
    allowed = [t for t in BookingStatus.ALL
               if can_transition(BookingStatus.BOOKING_UNKNOWN, t)]
    assert sorted(allowed) == sorted(
        [BookingStatus.BOOKED, BookingStatus.BOOKING_FAILED]
    )


def test_waiting_cannot_skip_the_invitation():
    assert not can_transition(BookingStatus.WAITING, BookingStatus.BOOKING)
    assert not can_transition(BookingStatus.WAITING, BookingStatus.BOOKED)


def test_check_transition_explains_a_refusal():
    """The usual cause is a caller assuming a shortcut the machine forbids, so
    the message names what IS allowed."""
    ok, reason = check_transition(BookingStatus.INVITED, BookingStatus.BOOKED)
    assert not ok
    assert "not allowed" in reason
    assert BookingStatus.BOOKING in reason      # names the legal move


def test_check_transition_reports_a_terminal_state_as_such():
    ok, reason = check_transition(BookingStatus.BOOKED, BookingStatus.BOOKING)
    assert not ok
    assert "terminal" in reason


def test_check_transition_rejects_an_unknown_status():
    ok, reason = check_transition(BookingStatus.WAITING, "teleported")
    assert not ok


def test_check_transition_allows_a_legal_move():
    ok, reason = check_transition(BookingStatus.WAITING, BookingStatus.INVITED)
    assert ok and reason == ""


# --------------------------------------------------------------------------- #
# Attention                                                                    #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "status",
    [BookingStatus.BOOKING_PENDING, BookingStatus.BOOKING_UNKNOWN,
     RegistrationStatus.PENDING, RegistrationStatus.UNKNOWN],
)
def test_states_that_need_a_human_are_flagged(status):
    assert needs_attention(status)


@pytest.mark.parametrize(
    "status",
    [BookingStatus.BOOKED, BookingStatus.WAITING, BookingStatus.SLOT_GONE,
     RegistrationStatus.SUCCESS],
)
def test_settled_states_do_not_need_a_human(status):
    assert not needs_attention(status)


def test_slot_gone_is_not_a_failure():
    """First-come-first-served with many invitees makes this EXPECTED. It must
    read as normal so real failures stay visible in reports."""
    assert not needs_attention(BookingStatus.SLOT_GONE)
    assert not is_terminal(BookingStatus.SLOT_GONE)
    assert BookingStatus.SLOT_GONE in BookingStatus.RETRYABLE


# --------------------------------------------------------------------------- #
# The invitation deadline                                                      #
# --------------------------------------------------------------------------- #

RECEIVED = 1756000000.0


def test_the_deadline_is_measured_from_the_email_not_from_now():
    """A watcher that was down for a day must not silently extend a 36-hour
    window — that would act on an invitation VFS has already retired."""
    assert deadline_from(RECEIVED, 36) == RECEIVED + 36 * 3600
    assert deadline_from(RECEIVED, 48) == RECEIVED + 48 * 3600


@pytest.mark.parametrize("received,hours", [(0, 36), (RECEIVED, 0), (0, 0)])
def test_a_missing_input_yields_no_deadline(received, hours):
    """Better to report no deadline than to invent one."""
    assert deadline_from(received, hours) is None


def test_expiry_is_evaluated_against_a_passed_in_clock():
    deadline = deadline_from(RECEIVED, 36)
    assert not is_expired(deadline, RECEIVED + 35 * 3600)
    assert is_expired(deadline, RECEIVED + 37 * 3600)


def test_a_row_without_a_deadline_is_never_expired():
    """An unknown window is not evidence of a closed one."""
    assert not is_expired(None, RECEIVED + 10**6)


def test_hours_remaining_goes_negative_once_lapsed():
    deadline = deadline_from(RECEIVED, 36)
    assert hours_remaining(deadline, RECEIVED) == pytest.approx(36)
    assert hours_remaining(deadline, RECEIVED + 40 * 3600) < 0
    assert hours_remaining(None, RECEIVED) is None


def test_the_36_and_48_hour_windows_differ_as_configured():
    """Greece and Netherlands say 36; Italy says 48. Treating them alike would
    have the system believe a window was open 12 hours after it closed."""
    now = RECEIVED + 40 * 3600
    assert is_expired(deadline_from(RECEIVED, 36), now)
    assert not is_expired(deadline_from(RECEIVED, 48), now)
