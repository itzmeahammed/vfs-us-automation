"""VFS modals that mean STOP, not "click Continue".

    ═══════════════ THE FAILURE THIS PREVENTS ═══════════════

Observed live 2026-09-29 on athul@travnook.com, mid-booking, as a mat-dialog
over the Your Details page:

    ⚠ Reminder
    We have received your booking request and your payment is under process.
    Kindly check status of your booking upto 5 hours.
                          [ Continue ]

The account ALREADY HAD a booking in flight. The dialog's only button says
Continue, and `dismiss_wait_dialog` was written to click exactly that — its
keyword list ("received", "please") matched this text. So the dismisser would
have closed it and walked on into booking a SECOND appointment and charging
the card a SECOND time.

It did not, purely because the modal rendered after that step's dismiss pass
had already run. Timing, not logic. These tests make it logic.
"""

import pytest

from src.vfs_bot import turnstile
from src.vfs_bot.turnstile import BlockingDialogError

# The real text, from the screenshot.
REAL_BLOCKER = (
    "Reminder\n"
    "We have received your booking request and your payment is under "
    "process. Kindly check status of your booking upto 5 hours."
)

# A genuine interstitial that SHOULD be dismissed — VFS's countdown reminder.
REAL_INTERSTITIAL = (
    "Reminder\n"
    "Please wait for some time before saving and continuing."
)


class FakeDialog:
    def __init__(self, text, visible=True):
        self.text = text
        self.visible = visible
        self.clicked = []

    def count(self):
        return 1 if self.text else 0

    def is_visible(self):
        return self.visible

    def inner_text(self):
        return self.text

    @property
    def first(self):
        return self

    def get_by_role(self, role, name=None):
        return FakeButton(self, name)


class FakeButton:
    def __init__(self, dialog, name):
        self.dialog = dialog
        self.name = name

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_visible(self):
        return True

    def click(self, **kwargs):
        self.dialog.clicked.append(self.name)


class FakePage:
    def __init__(self, text, visible=True):
        self.dialog = FakeDialog(text, visible)
        self.waits = []

    def locator(self, selector):
        return self.dialog

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


# --------------------------------------------------------------------------- #
# The blocker                                                                  #
# --------------------------------------------------------------------------- #

def test_the_real_booking_in_flight_modal_RAISES():
    page = FakePage(REAL_BLOCKER)

    with pytest.raises(BlockingDialogError):
        turnstile.dismiss_wait_dialog(page)


def test_the_blocker_is_NEVER_clicked():
    """The entire point. Clicking Continue here books a second appointment."""
    page = FakePage(REAL_BLOCKER)

    with pytest.raises(BlockingDialogError):
        turnstile.dismiss_wait_dialog(page)

    assert page.dialog.clicked == [], (
        "The 'booking under process' dialog was CLICKED. Its button says "
        "Continue; continuing books and charges a second time.")


def test_the_error_carries_the_portal_text():
    """The operator decides what to do about the account; VFS's exact
    wording is the evidence they need to do it."""
    page = FakePage(REAL_BLOCKER)

    with pytest.raises(BlockingDialogError) as caught:
        turnstile.dismiss_wait_dialog(page)

    message = str(caught.value)
    assert "under process" in message
    assert "SECOND" in message, "must say what re-running would cost"


@pytest.mark.parametrize("text", [
    "Your booking is under process, please wait.",
    "You already have an appointment for this application.",
    "This appears to be a duplicate request.",
    "There is a pending payment on this account.",
])
def test_other_account_state_phrases_block_too(text):
    page = FakePage(text)
    with pytest.raises(BlockingDialogError):
        turnstile.dismiss_wait_dialog(page)
    assert page.dialog.clicked == []


# --------------------------------------------------------------------------- #
# The ordinary interstitial must still be dismissed                            #
# --------------------------------------------------------------------------- #

def test_the_countdown_reminder_is_still_dismissed():
    """The guard must not turn every modal into a dead run."""
    page = FakePage(REAL_INTERSTITIAL)

    turnstile.dismiss_wait_dialog(page)

    assert page.dialog.clicked == ["Continue"]


def test_no_dialog_is_a_silent_no_op():
    page = FakePage("")
    turnstile.dismiss_wait_dialog(page)
    assert page.dialog.clicked == []


def test_an_invisible_dialog_is_ignored():
    page = FakePage(REAL_BLOCKER, visible=False)
    turnstile.dismiss_wait_dialog(page)   # must not raise
    assert page.dialog.clicked == []


def test_the_captcha_dialog_is_left_to_its_own_handler():
    page = FakePage("Please verify captcha to continue")
    turnstile.dismiss_wait_dialog(page)
    assert page.dialog.clicked == []


# --------------------------------------------------------------------------- #
# It must reach the caller                                                     #
# --------------------------------------------------------------------------- #

def test_wait_for_loader_does_not_swallow_it():
    """wait_for_loader wraps its work in try/except. If the blocker is caught
    there, every guarantee above is inert."""
    import inspect

    source = inspect.getsource(turnstile.wait_for_loader)
    dismiss_line = source.index("dismiss_wait_dialog(page)")
    try_line = source.index("try:")

    assert dismiss_line < try_line, (
        "dismiss_wait_dialog runs inside wait_for_loader's try/except, which "
        "swallows the BlockingDialogError and lets the run continue.")


def test_the_walk_marks_the_result_blocked():
    """A blocked run must be distinguishable from an ordinary stop: the
    remedy is 'check the account', and re-running risks a double booking."""
    import inspect

    from src.booking import walk

    source = inspect.getsource(walk.walk_flow)
    assert "except BlockingDialogError" in source
    assert "result.blocked = True" in source


def test_walk_result_has_the_blocked_flag():
    from src.booking.walk import WalkResult

    assert WalkResult().blocked is False
