"""What a run is allowed to leave on disk.

Two separate concerns, both pinned here:

1. PRODUCTION WRITES A SCREENSHOT ON FAILURE AND NOTHING ELSE. Norway is
   mapped; a DOM dump per page per run was right while discovering the flow and
   is noise now. The operator reads logs.

2. *** THE PAYMENT PATH MUST NEVER WRITE THE DOM. *** page.content() on a
   filled card form returns the card number and CVN inside input value=""
   attributes. The old 'payment_filled' capture did exactly that, immediately
   after fill_card, by design. Every other part of this codebase keeps the PAN
   out of files — card.py refuses to load from one, __repr__ masks it,
   journal.py filters forbidden keys — and that single call undid all of it.
"""

import inspect
import re

import pytest

from src.booking import runner, walk


def _body(fn) -> str:
    """Source with comments and docstrings stripped.

    Necessary because these files DOCUMENT the thing being asserted against —
    a naive substring check matches the comment explaining why the call is
    forbidden and passes while the call itself is restored.
    """
    source = inspect.getsource(fn)
    source = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
    source = re.sub(r"#[^\n]*", "", source)
    return source


# --------------------------------------------------------------------------- #
# The card must never reach a file                                             #
# --------------------------------------------------------------------------- #

def test_the_gateway_capture_helper_does_NOT_write_the_DOM():
    """_capture_page runs on the card form. A DOM dump there leaks the PAN."""
    body = _body(runner._capture_page)

    assert "_capture_html" not in body, (
        "runner._capture_page calls _capture_html. It is used on the payment "
        "gateway, and page.content() of a filled card form contains the card "
        "number and CVN in plaintext input values.")
    assert "_capture_shot" in body


def test_the_payment_step_captures_only_a_screenshot():
    """No path through _step_payment may reach _capture_html."""
    body = _body(runner._step_payment)

    assert "_capture_html" not in body, (
        "The payment step writes a DOM capture. On the gateway that is the "
        "card number on disk.")


def test_the_runner_capture_helper_is_a_screenshot_too():
    assert "_capture_html" not in _body(runner._capture)


def test_no_module_level_DOM_capture_survives_in_the_runner():
    """Belt and braces: the whole runner, not just the functions above."""
    source = inspect.getsource(runner)
    source = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
    source = re.sub(r"#[^\n]*", "", source)

    assert "_capture_html" not in source, (
        "Something in src/booking/runner.py writes a rendered DOM. The runner "
        "is the production path and reaches the card form.")


# --------------------------------------------------------------------------- #
# Capture modes                                                                #
# --------------------------------------------------------------------------- #

def test_the_default_is_failure_only():
    """Not 'full' (noise) and not 'off' (no evidence when a payment fails)."""
    assert walk.DEFAULT_CAPTURE == walk.CAPTURE_FAILURE


def test_the_three_modes_exist():
    assert walk.CAPTURE_MODES == ("off", "failure", "full")


def test_walk_flow_defaults_to_the_failure_mode():
    signature = inspect.signature(walk.walk_flow)
    assert signature.parameters["capture"].default == walk.DEFAULT_CAPTURE


def test_per_page_DOM_capture_is_gated_on_full():
    """The happy-path capture must be behind CAPTURE_FULL, not unconditional."""
    source = _body(walk.walk_flow)

    for line in source.splitlines():
        if "_capture_html(page, name, route)" in line:
            break
    else:
        pytest.fail("the per-page DOM capture vanished entirely; "
                    "'full' mode needs it for mapping a new country")

    assert "CAPTURE_FULL" in source, (
        "walk_flow writes DOM without checking the capture mode.")


def test_probe_rejects_an_unknown_capture_mode():
    """A typo must fail before a browser launches, not after a login.

    Every invocation is a fresh login and VFS blocks an account after roughly
    three in a short window, so an argument typo that surfaces mid-run costs
    real scarcity.
    """
    from src.booking.probe import run_probe

    with pytest.raises(ValueError) as caught:
        run_probe(source="AE", dest="NOR", capture="screenshots")

    assert "capture must be one of" in str(caught.value)


def test_capture_shot_never_raises():
    """A screenshot failure must not take down a run that is racing a slot."""
    class Hostile:
        url = "about:blank"

        def screenshot(self, **kwargs):
            raise RuntimeError("target closed")

    assert walk._capture_shot(Hostile(), "whatever", "AE-NOR") == ""


def test_capture_shot_asks_for_the_full_page():
    """VFS puts the failing control below the fold as often as above it."""
    seen = {}

    class Fake:
        url = "https://visa.vfsglobal.com/are/en/nor/review-pay"

        def screenshot(self, **kwargs):
            seen.update(kwargs)

    walk._capture_shot(Fake(), "review_pay_FAILED", "AE-NOR")
    assert seen.get("full_page") is True
    assert str(seen.get("path", "")).endswith(".png")


# --------------------------------------------------------------------------- #
# Reporting WHERE a run was interrupted                                        #
# --------------------------------------------------------------------------- #

def test_the_interrupt_handler_reads_the_live_step_marker():
    """Not result.walk, which is None until the walk RETURNS.

    Observed 2026-09-29: a run interrupted four seconds after the payment
    disclaimer reported "(pre-walk)". On the one step that can spend money,
    "where was it" is the entire question, and the answer was the most wrong
    one available.
    """
    import inspect

    from src.booking import probe

    source = inspect.getsource(probe.run_probe)
    handler = source[source.index("except KeyboardInterrupt"):]
    handler = handler[:handler.index("except AccessRestrictedError")]

    assert "current_step()" in handler, (
        "The interrupt handler does not read walk.current_step(). "
        "result.walk is only assigned when the walk finishes, so mid-walk it "
        "is None and the step is reported as '(pre-walk)'.")


def test_the_step_marker_tracks_the_walk():
    """CURRENT_STEP must be updated as each step begins, not at the end."""
    import inspect

    from src.booking import walk as walk_module

    source = inspect.getsource(walk_module.walk_flow)
    loop = source[source.index("for step in steps:"):]
    head = loop[:loop.index("report = StepReport")]

    assert "CURRENT_STEP = name" in head, (
        "the step marker is not set at the top of the loop, so an interrupt "
        "reports the previous step or none at all")


def test_screenshots_are_bounded():
    """A screenshot must not outlive the browser it is reading.

    A full-page shot still rendering when Chrome is killed leaves a pending
    asyncio task and a TargetClosedError after the run has otherwise finished
    cleanly — noise on exactly the runs that need a readable log.
    """
    from src.booking import walk as walk_module

    assert walk_module.SCREENSHOT_TIMEOUT_MS <= 10000

    seen = {}

    class Fake:
        url = "https://example.test"

        def screenshot(self, **kwargs):
            seen.update(kwargs)

    walk_module._capture_shot(Fake(), "x", "AE-NOR")
    assert seen.get("timeout") == walk_module.SCREENSHOT_TIMEOUT_MS


def test_the_popup_wait_is_short():
    """Same-tab is the normal path; 30s of waiting for a window is dead time.

    window.open() during a click handler is synchronous — a popup that has not
    appeared within a few seconds is not appearing.
    """
    from src.payment import gateway

    assert gateway.POPUP_TIMEOUT_MS <= 5000, (
        "attach_popup blocks this long before falling back to the same tab, "
        "on the page where VFS warns not to close the browser.")
