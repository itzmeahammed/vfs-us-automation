"""The ARMED inbox: only a code that arrived after we asked may be typed.

    *** WHY THESE TESTS ARE THE IMPORTANT ONES IN THIS FEATURE ***

A stale code is always rejected by the bank, and banks lock a card after a
small number of rejections — on a booking that is already paid for and cannot
be re-paid. Every test here is one way a stale or repeated code could reach
the bank's form.

No network: `fetch` is injected, which is the whole reason OtpInbox takes it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.otp_relay.errors import OtpRelayUnavailable
from src.otp_relay.inbox import OtpInbox, request_code
from src.otp_relay.relayion import Message

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


def _msg(ident, body="Your OTP is 483920", sender="MashreqBK", offset_s=0):
    return Message(id=ident, sender=sender, body=body,
                   received_at=NOW + timedelta(seconds=offset_s))


def _inbox(messages, **kw):
    """An inbox whose relay returns `messages`.

    NOTE on poll_s: it is clamped to a minimum of 1 second in the constructor
    (polling an HTTP API at 0s is not a thing this is allowed to do), so a
    test that polls more than once must pass `monotonic=` explicitly rather
    than assume rate limiting is off. Getting this wrong makes a test pass
    because it was rate-limited instead of because the logic worked.
    """
    kw.setdefault("poll_s", 1)
    return OtpInbox(fetch=lambda since, sender: list(messages), **kw)


# --------------------------------------------------------------------------- #
# ARMING                                                                       #
# --------------------------------------------------------------------------- #

def test_polling_before_arming_returns_nothing():
    """A wiring bug must not become a typed code. It is loud, and it is None."""
    inbox = _inbox([_msg("m1")])
    assert not inbox.armed
    assert inbox.poll() is None


def test_a_code_from_after_arming_is_returned():
    inbox = _inbox([_msg("m1", offset_s=30)])
    inbox.arm(NOW)
    assert inbox.poll() == "483920"


def test_a_code_from_before_the_grace_window_is_never_returned():
    """THE TEST THIS FEATURE LIVES OR DIES BY. An SMS from a previous attempt
    is invisible, permanently."""
    inbox = _inbox([_msg("old", offset_s=-600)], grace_s=90)
    inbox.arm(NOW)
    assert inbox.poll() is None


def test_the_clock_grace_admits_a_message_from_just_before_arming():
    """Relayion stamps receivedAt from its clock, we arm from ours. Without
    the grace a machine running a minute fast discards every valid code, and
    the symptom is identical to 'no SMS arrived'."""
    inbox = _inbox([_msg("m1", offset_s=-45)], grace_s=90)
    inbox.arm(NOW)
    assert inbox.poll() == "483920"


def test_the_grace_window_has_an_edge_and_it_is_enforced():
    just_outside = _inbox([_msg("m1", offset_s=-91)], grace_s=90)
    just_outside.arm(NOW)
    assert just_outside.poll() is None

    just_inside = _inbox([_msg("m1", offset_s=-89)], grace_s=90)
    just_inside.arm(NOW)
    assert just_inside.poll() == "483920"


def test_zero_grace_is_honoured():
    inbox = _inbox([_msg("m1", offset_s=-1)], grace_s=0)
    inbox.arm(NOW)
    assert inbox.poll() is None


def test_arming_accepts_a_naive_datetime_as_utc():
    """A caller that passes datetime.utcnow() must not silently break the
    comparison — a naive/aware mismatch raises TypeError deep in a loop that
    is holding a submitted payment."""
    inbox = _inbox([_msg("m1", offset_s=30)])
    inbox.arm(datetime(2026, 10, 15, 12, 0, 0))
    assert inbox.poll() == "483920"


# --------------------------------------------------------------------------- #
# CONSUMING — the same SMS must never be typed twice                           #
# --------------------------------------------------------------------------- #

def test_a_code_is_only_returned_once():
    """The bank's screen can bounce and bring the loop round again. Re-typing
    the same code spends an attempt for nothing.

    The second poll is 60s later on the fake clock, so this asserts the
    CONSUMED set did the work — not the rate limiter.
    """
    inbox = _inbox([_msg("m1", offset_s=30)])
    inbox.arm(NOW)
    assert inbox.poll(monotonic=100.0) == "483920"
    assert inbox.poll(monotonic=160.0) is None


def test_a_second_distinct_code_is_returned_after_the_first():
    """A resend arrives as a new message id, and IS worth trying."""
    messages = [_msg("m1", offset_s=30)]
    inbox = OtpInbox(poll_s=1, fetch=lambda since, sender: list(messages))
    inbox.arm(NOW)
    assert inbox.poll(monotonic=100.0) == "483920"

    messages.append(_msg("m2", body="Your OTP is 112233", offset_s=60))
    assert inbox.poll(monotonic=160.0) == "112233"


def test_a_message_with_no_parseable_code_is_consumed_not_retried():
    """Otherwise an unparseable SMS is re-examined every poll for ten
    minutes, filling the log and hiding the one that matters."""
    inbox = _inbox([_msg("m1", body="Your payment was approved", offset_s=30)])
    inbox.arm(NOW)
    assert inbox.poll() is None
    assert "m1" in inbox.consumed


def test_a_message_with_no_id_is_ignored():
    """No id means no way to remember it was used, so it could be typed on
    every poll. Discard it instead."""
    inbox = _inbox([_msg("", offset_s=30)])
    inbox.arm(NOW)
    assert inbox.poll() is None


def test_a_message_with_no_timestamp_is_ignored():
    """'We do not know when this arrived' must not come to mean 'therefore
    type it'."""
    inbox = _inbox([Message(id="m1", sender="X", body="Your OTP is 483920",
                            received_at=None)])
    inbox.arm(NOW)
    assert inbox.poll() is None


def test_the_oldest_fresh_code_is_tried_first():
    """When two codes are waiting the bank's screen is usually on the first."""
    inbox = _inbox([_msg("new", body="Your OTP is 112233", offset_s=90),
                    _msg("old", body="Your OTP is 483920", offset_s=30)])
    inbox.arm(NOW)
    assert inbox.poll() == "483920"


# --------------------------------------------------------------------------- #
# FILTERING AND FAILURE                                                        #
# --------------------------------------------------------------------------- #

def test_a_sender_filter_excludes_other_senders():
    inbox = _inbox([_msg("m1", sender="SCAMMER", offset_s=30)],
                   senders=["mashreq"])
    inbox.arm(NOW)
    assert inbox.poll() is None


def test_no_sender_filter_accepts_any_sender():
    inbox = _inbox([_msg("m1", sender="WHOEVER", offset_s=30)])
    inbox.arm(NOW)
    assert inbox.poll() == "483920"


def test_a_relay_error_is_counted_and_returns_none():
    """*** poll() MUST NOT RAISE. *** It is called inside a loop that is
    holding a submitted payment; an exception escaping would abandon it and
    report an unknown outcome the browser is about to show us."""
    def boom(since, sender):
        raise OtpRelayUnavailable("device offline")

    inbox = OtpInbox(poll_s=0, fetch=boom)
    inbox.arm(NOW)
    assert inbox.poll() is None
    assert inbox.errors == 1


def test_an_unexpected_exception_also_returns_none():
    def boom(since, sender):
        raise RuntimeError("something nobody predicted")

    inbox = OtpInbox(poll_s=0, fetch=boom)
    inbox.arm(NOW)
    assert inbox.poll() is None
    assert inbox.errors == 1


def test_the_digit_length_is_passed_through_to_the_extractor():
    inbox = _inbox([_msg("m1", body="Your PIN is 1234", offset_s=30)],
                   digits=4)
    inbox.arm(NOW)
    assert inbox.poll() == "1234"


# --------------------------------------------------------------------------- #
# RATE LIMITING                                                                #
# --------------------------------------------------------------------------- #

def test_polling_is_rate_limited():
    """The payment loop ticks at 1Hz for up to ten minutes. Without this that
    is 600 HTTP requests to read one text message."""
    calls = []

    def counting(since, sender):
        calls.append(1)
        return []

    inbox = OtpInbox(poll_s=5, fetch=counting)
    inbox.arm(NOW)
    inbox.poll(monotonic=1000.0)
    inbox.poll(monotonic=1001.0)
    inbox.poll(monotonic=1003.0)
    assert len(calls) == 1

    inbox.poll(monotonic=1006.0)
    assert len(calls) == 2


def test_the_first_poll_is_never_rate_limited():
    """A code that is already waiting must be found on the first tick, not
    five seconds into a challenge that expires in three minutes."""
    calls = []
    inbox = OtpInbox(poll_s=5, fetch=lambda s, n: calls.append(1) or [])
    inbox.arm(NOW)
    inbox.poll(monotonic=50.0)
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# The blocking wrapper (used by the CLI and the waitlist step, not payments)   #
# --------------------------------------------------------------------------- #

def test_request_code_returns_none_on_timeout_rather_than_raising():
    assert request_code(timeout_s=0, fetch=lambda since, sender: []) is None


def test_request_code_returns_a_fresh_code():
    code = request_code(timeout_s=5, since=NOW,
                        fetch=lambda since, sender: [_msg("m1", offset_s=30)])
    assert code == "483920"
