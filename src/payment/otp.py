"""Type a 3-D Secure code into the bank's challenge screen, and submit IT.

    ═══════════ THE ONE THING THIS MODULE MUST NEVER DO ═══════════

Click the Pay button. The payment has already been submitted by the time any
of this runs; a second click on the gateway's own submit is how a client gets
charged twice, and it is a realistic accident rather than a theoretical one,
because a selector as ordinary as `input[type=submit]` matches the Pay button
AND the bank's Continue button.

Three independent guards, in order of how much they are relied on:

    1. FRAME SCOPE. Only frames whose URL matches a `challenge_markers` entry
       are touched at all. The checkout form is not one of them.
    2. SAME-FRAME SUBMIT. The submit control is searched for in the frame the
       code field was found in, never across frames.
    3. AN EXPLICIT VETO. Any control that also matches the processor's own
       `submit` selector is refused, with a loud log line. This should be
       unreachable given 1 and 2 — it exists because "should be unreachable"
       is not a property anybody can check at 2am on a live payment.

    ═════════════════ NOT EVERY CHALLENGE HAS A CODE FIELD ═════════════════

Observed live on AE/NOR 2026-10-14: Mashreq's ACS served an OUT-OF-BAND
challenge — approve in the bank's app, press Continue, no code and no field to
type one into (see `_comment_challenge_is_not_always_an_otp` in
config/payment/CYBERSOURCE.json). The method is the ISSUER's choice and can
differ per card and per transaction.

So `find_code_field` returning None is a NORMAL, EXPECTED answer and means
"this challenge is not a typed code". The caller falls back to waiting for a
human, which is exactly what it did before this module existed. Nothing here
presses a Continue button on an out-of-band screen: approving in the app is
the cardholder's action, and a bot pressing Continue before they have done it
fails the authentication.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

#: Fallbacks for a processor whose config does not name the field yet. Ordered
#: most-specific first: `autocomplete=one-time-code` is the standard attribute
#: for exactly this, and a maxlength-bounded numeric text input is the shape
#: every ACS that asks for a typed code ends up with.
DEFAULT_FIELD_SELECTORS: Tuple[str, ...] = (
    "input[autocomplete='one-time-code']",
    "input[name='challengeDataEntry']",
    "input[id*='otp' i]",
    "input[name*='otp' i]",
    "input[id*='passcode' i]",
    "input[name*='passcode' i]",
    "input[type='tel'][maxlength]",
    "input[type='text'][maxlength='6']",
)

#: Submit controls, searched ONLY inside the frame that held the field.
DEFAULT_SUBMIT_SELECTORS: Tuple[str, ...] = (
    "input[type='submit']",
    "button[type='submit']",
    "button[name*='submit' i]",
    "input[value='Submit']",
)


def _as_list(value: Any, default: Tuple[str, ...]) -> List[str]:
    """A config value that may be a string, a list, or absent."""
    if not value:
        return list(default)
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if str(v).strip()]


def _markers(spec: Dict[str, Any]) -> List[str]:
    from src.payment import gateway

    return [m.lower() for m in gateway._challenge_markers(spec)]


def _challenge_frames(page, spec: Dict[str, Any]) -> List[Any]:
    """Frames that are part of the bank's challenge, parent included.

    GUARD 1. A frame whose URL matches no marker is not looked at, which is
    what keeps every selector here away from the checkout form.
    """
    markers = _markers(spec)
    out: List[Any] = []
    try:
        frames = list(getattr(page, "frames", None) or [])
    except Exception:                                           # noqa: BLE001
        frames = []

    for frame in frames:
        try:
            url = (frame.url or "").lower()
        except Exception:                                       # noqa: BLE001
            continue
        if not url or url == "about:blank":
            continue
        # The device-fingerprinting iframe matches markers too and holds no
        # field, so it costs one wasted lookup and nothing else.
        if any(marker in url for marker in markers):
            out.append(frame)
    return out


def _visible_one(scope, selector: str):
    """The first VISIBLE match for `selector` in `scope`, or None.

    Visibility matters: an ACS commonly ships a hidden code input alongside
    the out-of-band form, and filling an invisible field would look like
    success while submitting nothing.
    """
    try:
        locator = scope.locator(selector)
        if locator.count() < 1:
            return None
        first = locator.first
        if not first.is_visible():
            return None
        if not first.is_enabled():
            return None
        return first
    except Exception:                                           # noqa: BLE001
        return None


def find_code_field(page, spec: Dict[str, Any]) -> Optional[Tuple[Any, Any]]:
    """(frame, locator) for the code box, or None when there is not one.

    None means "this challenge does not want a typed code" — an out-of-band
    app approval, or a screen that has not rendered yet. Both are normal and
    neither is an error.
    """
    selectors = _as_list(spec.get("otp_field"), DEFAULT_FIELD_SELECTORS)
    for frame in _challenge_frames(page, spec):
        for selector in selectors:
            found = _visible_one(frame, selector)
            if found is not None:
                try:
                    where = frame.url
                except Exception:                               # noqa: BLE001
                    where = "?"
                log.info(f"  3-D Secure code field found: {selector} in "
                         f"{where}")
                return frame, found
    return None


def _vetoed(scope, control, spec: Dict[str, Any]) -> bool:
    """GUARD 3. Is this control the processor's own Pay button?"""
    pay = spec.get("submit")
    if not pay:
        return False
    candidate = _visible_one(scope, str(pay))
    if candidate is None:
        return False
    try:
        # Same element? Playwright has no identity test, so compare the
        # handles the two locators resolve to.
        if candidate.element_handle() == control.element_handle():
            log.error("  REFUSING to click the challenge submit: it is also "
                      "the gateway's Pay control. The payment is already "
                      "submitted and clicking it again could charge twice.")
            return True
    except Exception:                                           # noqa: BLE001
        return False
    return False


def fill_and_submit(page, spec: Dict[str, Any], code: str,
                    timeout_ms: int = 15000) -> bool:
    """Type `code` into the challenge and press ITS submit. True if both done.

    False on anything unexpected — no field, no submit, a veto, a Playwright
    error. False always means "nothing was changed on the bank's screen, keep
    waiting", so the caller's fallback to a human stays available.

    Never logs `code`.
    """
    found = find_code_field(page, spec)
    if found is None:
        return False
    frame, field = found

    try:
        field.fill(str(code), timeout=timeout_ms)
    except Exception as exc:                                    # noqa: BLE001
        log.warning(f"  could not type the code into the challenge: {exc}")
        return False

    # GUARD 2. The submit is looked for in `frame`, the frame the field came
    # from — never on the page, and never across frames.
    selectors = _as_list(spec.get("otp_submit"), DEFAULT_SUBMIT_SELECTORS)
    for selector in selectors:
        control = _visible_one(frame, selector)
        if control is None:
            continue
        if _vetoed(frame, control, spec):
            return False
        try:
            control.click(timeout=timeout_ms)
            log.info(f"  code submitted to the bank ({selector})")
            return True
        except Exception as exc:                                # noqa: BLE001
            log.warning(f"  could not click the challenge submit "
                        f"{selector!r}: {exc}")
            continue

    # The code IS typed at this point. Many ACS forms auto-submit on the last
    # digit, so this is reported as a partial success rather than a failure —
    # but it is said plainly, because the alternative reading ("we typed it and
    # nothing happened") needs an operator to look.
    log.warning("  the code was typed but no submit control was found in the "
                "challenge frame. If the bank's form does not auto-submit, "
                "press it in the open browser.")
    return False
