"""READ-ONLY waitlist detection (Application Details, step 1).

When a route/combination has no bookable slot, VFS may instead offer a waitlist —
a "Currently No slots are available for selected category, please confirm
waitlist" checkbox (Angular form control ``agreeToWaitlist``). This module
DETECTS that state and nothing more. It never ticks the checkbox and never
clicks Continue, so the page — and the account — are left completely untouched.

This is the module the ALWAYS-ON slot check calls on every run. It is
deliberately kept apart from register.py (which mutates) so that:

  * detection keeps working even if registration is disabled, misconfigured or
    outright broken — the existing slot-check report never regresses, and
  * "did we look?" and "did we act?" can never be confused in the code.

Moved here from src/vfs_bot/waitlist.py unchanged; that module is now a thin
re-export shim so every existing import keeps working.
"""

import logging

# A combo whose message starts with MARKER is a 'waitlist offered' result. It
# carries no date, so the existing result helpers treat it correctly with no
# changes: telegram_message._has_slot() is False (not a slot), it is not the
# "DISABLED" marker, and it lacks the "ERROR:" prefix (not a combo error). So a
# waitlist combo is silently ignored by the slot report and the slot count.
MARKER = "WAITLIST"

# The waitlist checkbox, matched WITHOUT relying on a formcontrolname.
#
# Portals differ: some bind the control (formcontrolname="agreeToWaitlist"),
# others — Switzerland among them — render a bare <mat-checkbox> whose only id is
# a positional 'mat-mdc-checkbox-0' that shifts whenever the page gains a
# control. So the default matches EITHER form, and a route can pin something
# exact via config/waitlist/<ROUTE>.json -> "checkbox".
#
# Note this is a superset: on a page with several checkboxes it can match more
# than the waitlist one, which is why is_offered() also checks the wording and
# why `doctor` reports the match count.
DEFAULT_CHECKBOX_SELECTOR = (
    'mat-checkbox[formcontrolname="agreeToWaitlist"], '
    'mat-checkbox[formcontrolname*="waitlist" i], '
    'mat-checkbox'
)

#: Bound-control selectors, tried FIRST when a portal provides a
#: formcontrolname. Far more reliable than scanning every checkbox — it names
#: the control rather than inferring it from position or wording.
BOUND_CHECKBOX_SELECTORS = (
    'mat-checkbox[formcontrolname="agreeToWaitlist"]',
    'mat-checkbox[formcontrolname*="waitlist" i]',
)

#: The on-page wording that identifies the waitlist consent, used to confirm a
#: checkbox really is the waitlist one. Substring match, case-insensitive.
WAITLIST_TEXT_HINTS = ("confirm waitlist", "waitlist")


def _present(locator) -> bool:
    """True if a locator resolves to something on the page.

    Prefers count(), falling back to is_visible() for locators that do not
    implement it — keeps detection working against simple test doubles as well
    as real Playwright locators.
    """
    try:
        return locator.count() > 0
    except Exception:
        pass
    try:
        return bool(locator.is_visible())
    except Exception:
        return False


def as_result() -> str:
    """The combo-result string stored when the waitlist checkbox is present."""
    return f"{MARKER} — no slots; waitlist sign-up available"


def as_registered_result(reference: str = "") -> str:
    """The combo-result string stored once we have actually REGISTERED.

    Keeps the MARKER prefix so every existing helper (``_has_slot`` false,
    ``count_waitlist``, the summary line) treats it exactly like a plain waitlist
    result — only the wording changes.
    """
    tail = f" (ref {reference})" if reference else ""
    return f"{MARKER} — registered for waitlist{tail}"


def is_waitlist(message: str) -> bool:
    """True if a combo result string was produced by as_result()."""
    return bool(message) and message.startswith(MARKER)


def count_waitlist(results) -> int:
    """How many combinations in a route's results are waitlist-only."""
    return sum(1 for _label, message in (results or []) if is_waitlist(message))


def locate(page, checkbox_selector: str = None):
    """
    Returns a locator for the waitlist checkbox, or None if it is not present.

    Resolution, in order:

      1. If a route pinned an exact "checkbox" selector, use it verbatim.
      2. Otherwise take every <mat-checkbox> on the page and, when there is more
         than one, keep the one whose text mentions the waitlist. Portals that
         emit no formcontrolname (Switzerland) give us nothing else to go on, and
         the review-pay page has consent checkboxes that must NOT be confused
         with this one.
      3. If exactly one exists and nothing mentions the waitlist, use it — on the
         Appointment Details step a lone checkbox is the waitlist offer.
    """
    if checkbox_selector:
        try:
            pinned = page.locator(checkbox_selector).first
            return pinned if _present(pinned) else None
        except Exception:
            return None

    # A bound control is the most reliable anchor when the portal provides one,
    # so try those BEFORE scanning every checkbox on the page.
    for selector in BOUND_CHECKBOX_SELECTORS:
        try:
            bound = page.locator(selector).first
            if _present(bound):
                return bound
        except Exception:
            continue

    try:
        boxes = page.locator("mat-checkbox")
        count = boxes.count()
    except Exception:
        return None
    if count == 0:
        return None

    if count > 1:
        for hint in WAITLIST_TEXT_HINTS:
            try:
                narrowed = boxes.filter(has_text=hint)
                if narrowed.count() > 0:
                    return narrowed.first
            except Exception:
                continue
        logging.debug(
            f"{count} checkboxes on the page and none mention the waitlist — "
            "using the first. Pin \"checkbox\" in the route config if this is "
            "the wrong one.")
    return boxes.first


def is_offered(page, checkbox_selector: str = None) -> bool:
    """
    True if the waitlist checkbox is present on the current Application Details
    step. READ-ONLY — never ticks it or submits the form.

    The page is already settled by the time this runs (the slot-banner read has
    just timed out with no availability), so a non-waiting visibility check is
    enough — no extra wait budget is spent on routes that do have slots.

    `checkbox_selector` lets a route pin an exact selector from its
    config/waitlist/<ROUTE>.json without a code change; omit it for the default.
    """
    try:
        box = locate(page, checkbox_selector)
        if box is not None and box.is_visible():
            return True
    except Exception:
        pass
    # Fallback on the on-page wording, for markup that renders no <mat-checkbox>.
    try:
        return page.get_by_text("confirm waitlist", exact=False).first.is_visible()
    except Exception:
        return False


def is_checked(page, checkbox_selector: str = None) -> bool:
    """True if the waitlist checkbox is already ticked. Read-only.

    Used by register.py to stay idempotent: if a previous partial attempt left
    the box ticked, we must not toggle it back OFF.

    Reads the NATIVE <input type=checkbox> nested inside the <mat-checkbox>;
    Angular Material's wrapper carries no checked state of its own.
    """
    try:
        box = locate(page, checkbox_selector)
        if box is None:
            return False
        native = box.locator("input[type='checkbox']").first
        if native.count() > 0:
            return bool(native.is_checked())
    except Exception as e:
        logging.debug(f"Could not read waitlist checkbox state: {e}")
    return False
