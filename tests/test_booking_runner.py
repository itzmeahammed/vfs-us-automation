"""Tests for the booking runner's orchestration and commit discipline.

These are about the RULES, not the selectors. The pages after "Book Now" have
never been captured, so nothing here asserts that a real portal behaves a
certain way — it asserts that the runner refuses to do the dangerous things
regardless of what a portal does.

The fake page is deliberately dumb. Every test that matters here is about
ordering and refusal, and a realistic page object would only make it harder to
see which rule is being checked.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.booking import lifecycle, runner                       # noqa: E402
from src.booking.errors import (                                # noqa: E402
    AmbiguousIdentityError,
    ApplicationNotFoundError,
    BookingCommittedError,
    BookingConfigError,
    BookingDisabled,
    IdentityMismatchError,
    InvitationExpiredError,
)


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #

class FakePage:
    """Records what was asked of it. Does nothing."""

    def __init__(self, body_text: str = ""):
        self.body_text = body_text
        self.url = "https://example.test/dashboard"
        self.mouse = mock.Mock()

    def wait_for_timeout(self, _ms):
        pass

    def inner_text(self, _selector, timeout=None):
        return self.body_text


class FakeRegistrant:
    def __init__(self, rid="client-1", first="MUFADDAL", last="CALCUTTAWALA"):
        self.id = rid
        self._data = {"first_name": first, "last_name": last}

    def get(self, key, default=None):
        return self._data.get(key, default)


class FakeRow:
    def __init__(self, index=0, name="MUFADDAL CALCUTTAWALA",
                 reference="SWDB1", bookable=True):
        self.index = index
        self.name = name
        self.reference = reference
        self.bookable = bookable

    def summary(self):
        return f"[{self.index}] {self.reference}"


def _steps(*step_dicts):
    """A config whose steps are exactly those given."""
    return list(step_dicts)


def _patch_config(test, steps, enabled=True, commit_name="select_slot"):
    """Point booking_config at a made-up route."""
    test.enterContext(mock.patch.object(
        runner.booking_config, "get", return_value={"enabled": enabled}))
    test.enterContext(mock.patch.object(
        runner.booking_config, "steps_for", return_value=steps))
    test.enterContext(mock.patch.object(
        runner.booking_config, "commit_step_name", return_value=commit_name))


# --------------------------------------------------------------------------- #
# Gates that run before the browser does anything                              #
# --------------------------------------------------------------------------- #

class TestPreflightGates(unittest.TestCase):

    def test_a_disabled_route_refuses_to_run(self):
        """Every route ships disabled; running one anyway must be impossible."""
        with mock.patch.object(runner.booking_config, "get",
                               return_value={"enabled": False}):
            with self.assertRaises(BookingDisabled):
                runner.book(FakePage(), "AE-CHE", FakeRegistrant())

    def test_an_expired_invitation_is_refused_before_the_browser_acts(self):
        """A lapsed window cannot be booked, and finding out early matters.

        A login costs a Turnstile solve and counts against VFS's per-account
        rate limit — which is how an account gets restricted for 12 hours. That
        must not be spent on an invitation that already expired.
        """
        _patch_config(self, _steps())
        with self.assertRaises(InvitationExpiredError):
            runner.book(FakePage(), "AE-CHE", FakeRegistrant(),
                        deadline_epoch=1.0)          # 1970

    def test_an_unknown_step_type_is_refused(self):
        """A typo in a config must stop the run, not silently skip a page."""
        _patch_config(self, _steps({"name": "mystery", "type": "teleport"}))
        with self.assertRaises(BookingConfigError) as caught:
            runner.book(FakePage(), "AE-CHE", FakeRegistrant())
        self.assertIn("teleport", str(caught.exception))


# --------------------------------------------------------------------------- #
# Identity — the decision that must never be wrong                             #
# --------------------------------------------------------------------------- #

class TestIdentity(unittest.TestCase):

    def setUp(self):
        self.page = FakePage()
        self.step = {"name": "dashboard_resume", "type": "dashboard_resume",
                     "row": {"open": {"role": "button", "name": "Book Now"}}}

    def _run(self, rows, **kwargs):
        _patch_config(self, _steps(self.step))
        self.enterContext(mock.patch("src.booking.probe.read_dashboard",
                                     return_value=rows))
        self.enterContext(mock.patch.object(runner, "_click"))
        self.enterContext(mock.patch.object(runner, "_capture"))
        return runner.book(self.page, "AE-CHE", FakeRegistrant(), **kwargs)

    def test_no_rows_is_application_not_found(self):
        with self.assertRaises(ApplicationNotFoundError):
            self._run([])

    def test_two_equally_matching_rows_resolve_NEITHER(self):
        """The rule the whole design rests on: ambiguity books nobody.

        Two rows carrying the same name is not hypothetical — a family books
        together. Picking either one risks booking a real person's slot under
        the wrong passport, which cannot be undone.
        """
        twins = [FakeRow(0, "SAME NAME", "REF-A"),
                 FakeRow(1, "SAME NAME", "REF-B")]
        with self.assertRaises((AmbiguousIdentityError,
                                ApplicationNotFoundError)):
            self._run(twins, expected_reference="")

    def test_a_row_that_is_not_bookable_is_skipped_not_forced(self):
        """'Not invited yet' is a normal state, not an error to push through."""
        run = self._run([FakeRow(bookable=False, reference="SWDB1")],
                        expected_reference="SWDB1")
        self.assertEqual(run.status, lifecycle.BookingStatus.INVITED)
        self.assertIn("not bookable", run.reason)

    def test_a_contradicting_reference_aborts(self):
        """Click-then-check: a field present on both sides that DISAGREES."""
        step_pair = _steps(
            self.step,
            {"name": "verify_identity", "type": "identity_assert"},
        )
        _patch_config(self, step_pair)
        self.enterContext(mock.patch(
            "src.booking.probe.read_dashboard",
            return_value=[FakeRow(reference="SWDB-WRONG")]))
        self.enterContext(mock.patch.object(runner, "_click"))
        self.enterContext(mock.patch.object(runner, "_capture"))
        self.enterContext(mock.patch.object(runner, "_await_page"))

        with self.assertRaises(IdentityMismatchError):
            runner.book(self.page, "AE-CHE", FakeRegistrant(),
                        expected_reference="SWDB-RIGHT")


# --------------------------------------------------------------------------- #
# The commit boundary                                                          #
# --------------------------------------------------------------------------- #

class TestCommitBoundary(unittest.TestCase):

    def setUp(self):
        self.commit_step = {"name": "select_slot", "type": "slot_pick",
                            "commits": True,
                            "submit": {"role": "button", "name": "Continue"}}

    def _prepare(self):
        self.enterContext(mock.patch.object(runner, "_await_page"))
        self.enterContext(mock.patch.object(runner, "_capture"))
        self.enterContext(mock.patch("src.booking.walk.available_dates",
                                     return_value=["2026-10-01"]))
        self.enterContext(mock.patch("src.booking.walk.pick_date"))
        self.enterContext(mock.patch("src.booking.walk.available_times",
                                     return_value=["09:00"]))
        self.enterContext(mock.patch("src.booking.walk.pick_time",
                                     return_value="09:00"))

    def test_dry_run_stops_IN_FRONT_of_the_commit_and_submits_nothing(self):
        """The default must never submit. This is the whole safety contract."""
        _patch_config(self, _steps(self.commit_step))
        self._prepare()
        submit = self.enterContext(mock.patch.object(runner, "_submit"))

        run = runner.book(FakePage(), "AE-CHE", FakeRegistrant())

        submit.assert_not_called()
        self.assertEqual(run.status, lifecycle.BookingStatus.INVITED)
        self.assertIn("commit boundary", run.reason)

    def test_the_write_ahead_marker_is_journalled_BEFORE_the_submit(self):
        """Ordering, not merely presence.

        A crash between the marker and the click must leave evidence that a
        submit was imminent. A marker written afterwards would be worthless —
        the crash it exists for happens in between.
        """
        _patch_config(self, _steps(self.commit_step))
        self._prepare()
        order = []
        self.enterContext(mock.patch.object(
            runner, "_journal", side_effect=lambda run: order.append(
                f"journal:{run.status}")))
        self.enterContext(mock.patch.object(
            runner, "_submit", side_effect=lambda ctx: order.append("submit")))

        runner.book(FakePage(), "AE-CHE", FakeRegistrant(), live=True)

        self.assertEqual(
            order[:2],
            [f"journal:{lifecycle.BookingStatus.BOOKING_PENDING}", "submit"],
            "the pending marker must be on disk before the committing click")

    def test_a_failed_commit_submit_is_COMMITTED_not_retryable(self):
        """A click that raised may still have reached VFS.

        Reporting it as a clean pre-commit failure would invite a retry, and a
        replayed submit is how one client gets two appointments.
        """
        _patch_config(self, _steps(self.commit_step))
        self._prepare()
        self.enterContext(mock.patch.object(runner, "_journal"))
        self.enterContext(mock.patch.object(
            runner, "_submit", side_effect=RuntimeError("network died")))

        with self.assertRaises(BookingCommittedError):
            runner.book(FakePage(), "AE-CHE", FakeRegistrant(), live=True)

    def test_an_empty_calendar_is_slot_gone_not_a_failure(self):
        """Losing a race is expected and must read as normal.

        If slot_gone reported as a fault, the real faults would be buried in
        noise from a completely healthy system.
        """
        _patch_config(self, _steps(self.commit_step))
        self.enterContext(mock.patch.object(runner, "_await_page"))
        self.enterContext(mock.patch.object(runner, "_capture"))
        self.enterContext(mock.patch.object(runner, "_journal"))
        self.enterContext(mock.patch("src.booking.walk.available_dates",
                                     return_value=[]))

        # RETURNED, not raised — unlike every other pre-commit error. Losing a
        # race is an outcome, not an incident, and it gets its own status so a
        # digest can render it as routine.
        run = runner.book(FakePage(), "AE-CHE", FakeRegistrant(), live=True)

        self.assertEqual(run.status, lifecycle.BookingStatus.SLOT_GONE)
        self.assertIn("lost race", run.reason)
        self.assertFalse(run.committed, "no slot was taken, so nothing committed")


# --------------------------------------------------------------------------- #
# Finishing                                                                    #
# --------------------------------------------------------------------------- #

class TestOutcome(unittest.TestCase):

    def test_completing_every_step_without_a_reference_is_NOT_success(self):
        """A flow with no confirm step cannot assert a booking happened.

        Reporting 'booked' here would be the worst possible lie: the journal
        would say done, and the client would find out at the visa centre.
        """
        _patch_config(self, _steps(
            {"name": "services", "type": "form",
             "submit": {"role": "button", "name": "Continue"}}))
        self.enterContext(mock.patch.object(runner, "_await_page"))
        self.enterContext(mock.patch.object(runner, "_capture"))
        self.enterContext(mock.patch.object(runner, "_submit"))

        run = runner.book(FakePage(), "AE-CHE", FakeRegistrant(), live=True)

        self.assertEqual(run.status, lifecycle.BookingStatus.BOOKING_UNKNOWN)
        self.assertIn("no booking reference", run.reason)

    def test_a_read_reference_is_what_makes_a_run_booked(self):
        _patch_config(self, _steps(
            {"name": "confirmation", "type": "confirm",
             "reference_pattern": r"Reference\s+([A-Z0-9-]+)"}))
        self.enterContext(mock.patch.object(runner, "_await_page"))
        self.enterContext(mock.patch.object(runner, "_capture"))
        self.enterContext(mock.patch.object(runner, "_submit"))
        page = FakePage("Your booking is confirmed. Reference SWDB123456")

        run = runner.book(page, "AE-CHE", FakeRegistrant(), live=True)

        self.assertEqual(run.status, lifecycle.BookingStatus.BOOKED)
        self.assertEqual(run.reference, "SWDB123456")
        self.assertTrue(run.committed)

    def test_a_confirm_page_with_no_reference_needs_a_human(self):
        """Unconfirmed is never resolved by guessing."""
        from src.booking.errors import BookingUnconfirmedError

        _patch_config(self, _steps(
            {"name": "confirmation", "type": "confirm",
             "reference_pattern": r"Reference\s+([A-Z0-9-]+)"}))
        self.enterContext(mock.patch.object(runner, "_await_page"))
        self.enterContext(mock.patch.object(runner, "_capture"))
        self.enterContext(mock.patch.object(runner, "_submit"))

        with self.assertRaises(BookingUnconfirmedError):
            runner.book(FakePage("Something else entirely"), "AE-CHE",
                        FakeRegistrant(), live=True)


if __name__ == "__main__":
    unittest.main()
