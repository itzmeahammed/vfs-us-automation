"""The `progress` block on an invitation view.

An invitation's history is the only record of what was actually done about it,
and the console reads this block rather than the history itself — so the
meaning of each event name is settled HERE, once, where the events are written.

Every timestamp is epoch seconds or None. None means "did not happen" or "was
not recorded"; neither may be rendered as a time, so neither may be invented.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.api.modules.booking.router import (
    _INVITE_SEARCHABLE, _epoch_of, _int, _invite_view, _progress,
)


def at(hour: int) -> str:
    """A stamp in exactly the format `invites._now()` writes: UTC, trailing Z."""
    return datetime(2026, 10, 30, hour, 0, 0,
                    tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hour_of(epoch: float) -> int:
    return datetime.fromtimestamp(epoch, timezone.utc).hour


# ── reading the stamps invites.py writes ──────────────────────────────────── #


def test_a_stamp_is_read_as_utc_not_as_local_time():
    """`invites._now()` is UTC with a trailing Z. Read as local time, every
    timeline on the console would be wrong by the reader's offset."""
    assert _epoch_of({"at": at(8)}) == datetime(
        2026, 10, 30, 8, 0, tzinfo=timezone.utc).timestamp()


def test_an_unreadable_stamp_is_none_and_never_now():
    for bad in ({}, {"at": ""}, {"at": None}, {"at": "not a date"},
                {"at": "30/10/2026"}):
        assert _epoch_of(bad) is None
    assert _epoch_of(None) is None


def test_a_count_that_is_not_a_number_does_not_take_down_the_page():
    """`attempts` is read on a LIST endpoint. One corrupt record must not cost
    the caller every other row beside it."""
    assert _int("3") == 3
    assert _int(None) == 0
    assert _int("many") == 0
    assert _int(-5) == 0


# ── what the block says ───────────────────────────────────────────────────── #


def test_an_untouched_invitation_has_no_attempt_times():
    p = _progress({"status": "pending", "attempts": 0,
                   "history": [{"at": at(8), "event": "received"}]})
    assert p["attempts"] == 0
    assert p["first_attempt_epoch"] is None
    assert p["last_attempt_epoch"] is None
    assert p["final"] is False
    # It has been "waiting since" the moment it arrived.
    assert hour_of(p["status_since_epoch"]) == 8


def test_first_and_last_attempt_are_the_ends_of_the_run_of_tries():
    p = _progress({"status": "needs_attention", "attempts": 2, "history": [
        {"at": at(8), "event": "received"},
        {"at": at(9), "event": "triggered"},
        {"at": at(9), "event": "attempt_started"},
        {"at": at(10), "event": "attempt_failed"},
        {"at": at(11), "event": "attempt_started"},
        {"at": at(12), "event": "needs_attention", "detail": "no answer"},
    ]})
    assert hour_of(p["first_attempt_epoch"]) == 9
    assert hour_of(p["last_attempt_epoch"]) == 11
    assert hour_of(p["status_since_epoch"]) == 12
    assert p["last_event"]["event"] == "needs_attention"
    assert p["events"] == 6


def test_book_by_hand_carries_the_reason_the_bot_would_not():
    """The most asked question about a manual row, already written down."""
    p = _progress({"status": "manual", "history": [
        {"at": at(8), "event": "received"},
        {"at": at(9), "event": "manual", "detail": 'no client matches "A K"'},
    ]})
    assert p["blocked_reason"] == 'no client matches "A K"'
    assert p["final"] is False, "manual still needs doing; it is not an ending"


def test_status_since_follows_the_status_it_is_in_now():
    """The subtle one. A human answering "not booked" sends a still-valid
    invitation BACK to pending, so the newest terminal-looking event is in its
    past. Reading the last such event would date it to the wrong moment."""
    p = _progress({"status": "pending", "attempts": 1, "history": [
        {"at": at(8), "event": "received"},
        {"at": at(9), "event": "attempt_started"},
        {"at": at(10), "event": "needs_attention"},
        {"at": at(11), "event": "resolved_not_booked", "detail": "nothing there"},
    ]})
    assert hour_of(p["status_since_epoch"]) == 11
    assert p["final"] is False


def test_booked_by_hand_is_final():
    p = _progress({"status": "booked", "history": [
        {"at": at(8), "event": "received"},
        {"at": at(10), "event": "resolved_booked"},
    ]})
    assert p["final"] is True
    assert hour_of(p["status_since_epoch"]) == 10


def test_a_record_with_nothing_in_it_still_answers():
    for item in ({"status": "pending"},
                 {"status": "pending", "history": []},
                 {"status": "pending", "history": ["junk", None, 42]},
                 {"status": "not-a-status", "history": [{"at": at(8),
                                                         "event": "received"}]},
                 {"status": "booked", "history": [{"event": "booked"}]}):
        p = _progress(item)
        assert p["attempts"] == 0
        assert isinstance(p["events"], int)


# ── the view around it ────────────────────────────────────────────────────── #


def test_the_mailbox_is_served_in_full():
    """An operator deciding which account to sign in to cannot act on
    `a****t@travnook.com` — there are several and the mask collapses them."""
    view = _invite_view({"key": "k", "status": "pending", "route": "AE-FRA",
                         "account": "bookings2@travnook.com", "history": []})
    assert view["account"] == "bookings2@travnook.com"
    assert "*" not in view["account"]


def test_the_mailbox_is_shown_but_never_searched():
    """Showing an address on a row someone is already reading is not the same
    as letting a guessed address be confirmed by a search hit."""
    assert "account" not in _INVITE_SEARCHABLE
    assert "progress" not in _INVITE_SEARCHABLE
    assert "history" not in _INVITE_SEARCHABLE


def test_a_record_whose_progress_cannot_be_read_still_lists():
    """`progress` is derived, so it is the part of this view most able to meet
    something unexpected. It must never cost the caller the rest of the page."""
    class Exploding(dict):
        def get(self, key, default=None):
            if key == "history":
                raise RuntimeError("corrupt record")
            return super().get(key, default)

    item = Exploding({"key": "k", "status": "pending", "account": "a@b.com"})
    view = _invite_view(item)
    assert view["progress"]["unreadable"] is True
    assert view["account"] == "a@b.com"
