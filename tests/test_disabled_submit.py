"""A disabled control must STOP the run, never be force-clicked.

    ══════════════════ WHY THIS FILE EXISTS ══════════════════

Observed live on AE-NOR, 2026-09-29, step 'appointment_details':

    locator resolved to <button ... disabled="true" ... mat-mdc-button-disabled>
      - element is not enabled          (90 retries, 45 seconds)
    step 'appointment_details' submit: clicked (force).

The button was disabled for the entire 45-second wait, and then the ladder's
`force=True` branch reported SUCCESS. It had not succeeded. force=True skips
Playwright's actionability checks and dispatches a click at the element's
coordinates; a disabled <button> swallows it and no handler runs. Playwright
reports success because the click was dispatched, not because it did anything.

The run then walked on believing the form was submitted, blocked in the next
step's page gate waiting for a navigation that was never coming, and surfaced
45 seconds later as a timeout on an unrelated URL. The real fault — an
incomplete form — was reported nowhere.

So these tests assert the SHAPE of the fix, not its wording: a disabled control
raises, and nothing is clicked.
"""

import pytest

from src.waitlist.errors import WaitlistStepError
from src.waitlist.register import _click, _looks_disabled, _why_disabled


class FakeLocator:
    """The smallest thing that answers what _click and _looks_disabled ask."""

    def __init__(self, attrs=None, checked=False):
        self._attrs = attrs or {}
        self.clicks = []            # every click that reached the element
        self.evaluated = []
        self._checked = checked

    # -- what _looks_disabled reads --------------------------------------- #
    def get_attribute(self, name):
        return self._attrs.get(name)

    # -- what _click does before clicking --------------------------------- #
    def wait_for(self, **kwargs):
        return None

    def scroll_into_view_if_needed(self, **kwargs):
        return None

    def filter(self, **kwargs):
        return self

    @property
    def first(self):
        return self

    def click(self, **kwargs):
        self.clicks.append(kwargs)

    def evaluate(self, script):
        self.evaluated.append(script)

    def is_checked(self):
        return self._checked

    def inner_text(self):
        return ""

    def count(self):
        return 0

    def nth(self, i):
        return self


class FakePage:
    def __init__(self, locator=None):
        self._locator = locator or FakeLocator()
        self.url = "https://visa.vfsglobal.com/are/en/nor/appointment-details"

    def locator(self, selector):
        # _why_disabled probes for mat-error / checkboxes; hand it an empty one
        # so diagnosis never affects the assertions about clicking.
        if self._locator is None:
            return FakeLocator()
        return self._locator

    def get_by_role(self, role, name=None, exact=False):
        return self._locator

    def evaluate(self, script):
        return []


DISABLED = {
    "disabled": "true",
    "class": "btn mat-btn-lg mdc-button mat-mdc-button-disabled mat-unthemed",
}


def test_a_disabled_button_is_NEVER_clicked():
    """The whole bug in one assertion: no click reaches a disabled control."""
    button = FakeLocator(DISABLED)
    page = FakePage(button)

    with pytest.raises(WaitlistStepError):
        _click(page, {"role": "button", "name": "Continue"}, "submit", 1000)

    assert button.clicks == [], (
        "A disabled button was clicked. force=True does not enable it — the "
        "click is swallowed and the run walks on believing it submitted.")
    assert button.evaluated == [], (
        "A disabled button was clicked via JS dispatch, which is the same lie "
        "by another route.")


def test_the_error_says_the_form_is_incomplete():
    """A disabled submit means the FORM is wrong, not the selector.

    Pinned because the operator reads this line and decides what to do next.
    Reporting it as a click failure sends them hunting for a bad selector when
    the selector was correct and a field was empty.
    """
    page = FakePage(FakeLocator(DISABLED))

    with pytest.raises(WaitlistStepError) as caught:
        _click(page, {"role": "button", "name": "Continue"}, "submit", 1000)

    message = str(caught.value).lower()
    assert "disabled" in message
    assert "not complete" in message or "incomplete" in message


def test_an_ENABLED_button_still_gets_clicked():
    """The guard must not break the ordinary path."""
    button = FakeLocator({"class": "btn mdc-button"})
    page = FakePage(button)

    _click(page, {"role": "button", "name": "Continue"}, "submit", 1000)

    assert len(button.clicks) == 1
    assert button.clicks[0].get("force") is not True, (
        "The normal path must not force. Force hides exactly the failure this "
        "file exists to surface.")


def test_aria_disabled_counts_as_disabled():
    """Material keeps a button focusable while logically off."""
    button = FakeLocator({"aria-disabled": "true", "class": "btn"})
    page = FakePage(button)

    with pytest.raises(WaitlistStepError):
        _click(page, {"role": "button", "name": "Continue"}, "submit", 1000)
    assert button.clicks == []


def test_material_disabled_class_alone_counts():
    """A stale render can carry the class without the attribute."""
    button = FakeLocator({"class": "btn mat-mdc-button-disabled"})
    page = FakePage(button)

    with pytest.raises(WaitlistStepError):
        _click(page, {"role": "button", "name": "Continue"}, "submit", 1000)
    assert button.clicks == []


def test_a_button_disabled_MID_WAIT_is_caught_too():
    """45s of retries is long enough for the page to disable it underneath us.

    A countdown restarting or validation clearing a field re-disables the
    button after the first check passed. Dispatching into that is the same
    silent no-op, so there is a second check before the JS fallback.
    """
    class GoesDisabled(FakeLocator):
        def __init__(self):
            super().__init__({"class": "btn"})
            self._reads = 0

        def get_attribute(self, name):
            # Enabled on the first pass, disabled by the time the click fails.
            if name == "class" and self._reads > 0:
                return "btn mat-mdc-button-disabled"
            return super().get_attribute(name)

        def click(self, **kwargs):
            self._reads += 1
            raise RuntimeError("Timeout 45000ms exceeded")

    button = GoesDisabled()
    page = FakePage(button)

    with pytest.raises(WaitlistStepError) as caught:
        _click(page, {"role": "button", "name": "Continue"}, "submit", 1000)

    assert "disabled" in str(caught.value).lower()
    assert button.evaluated == [], "JS dispatch ran on a disabled control."


def test_why_disabled_never_raises_on_a_hostile_page():
    """Diagnosis must not replace the real error with its own.

    _why_disabled runs on a page that has ALREADY failed, which is the page
    most likely to fail again when questioned. If it throws, the operator loses
    the actual reason.
    """
    class Hostile:
        url = "about:blank"

        def locator(self, selector):
            raise RuntimeError("detached")

        def evaluate(self, script):
            raise RuntimeError("execution context destroyed")

    class HostileLocator(FakeLocator):
        def get_attribute(self, name):
            raise RuntimeError("detached")

    text = _why_disabled(Hostile(), HostileLocator())
    assert isinstance(text, str) and text, "must return something to report"


def test_looks_disabled_is_false_for_a_normal_button():
    assert _looks_disabled(FakeLocator({"class": "btn mdc-button"})) is False
