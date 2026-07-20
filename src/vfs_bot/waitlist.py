"""VFS 'join the waitlist' detection + notification (Application Details, step 1).

When a route/combination has no bookable slot, VFS may instead offer a waitlist —
a "Currently No slots are available for selected category, please confirm
waitlist" checkbox (Angular form control ``agreeToWaitlist``). This module
DETECTS that state and reports it. It is deliberately READ-ONLY: it never ticks
the checkbox and never clicks Continue, so the page — and the account — are left
completely untouched.

Reporting goes to both Telegram channels:
  * Success chat — a per-route message naming the waitlist combination(s)
                   (``notify`` / ``build_message`` below).
  * Summary chat — the run summary shows 'waitlist' in place of 'no slots'
                   (rendered in telegram_message.run_summary, which keys off the
                   ``waitlist`` count that supervisor._outcome derives from the
                   MARKER via ``count_waitlist``).

Kept as its own module so the waitlist feature can evolve without touching the
working slot-read flow. Integration points (all tiny):
  - slot_check.run_slot_check -> is_offered() + as_result(), only in the no-slot branch
  - slot_check.run_slot_check -> notify() for the success-chat message
  - supervisor._outcome       -> count_waitlist() for the summary flag
  - telegram_message          -> is_waitlist() to render the summary line
"""

import logging

# A combo whose message starts with MARKER is a 'waitlist offered' result. It
# carries no date, so the existing result helpers treat it correctly with no
# changes: telegram_message._has_slot() is False (not a slot), it is not the
# "DISABLED" marker, and it lacks the "ERROR:" prefix (not a combo error). So a
# waitlist combo is silently ignored by the slot report and the slot count.
MARKER = "WAITLIST"


def as_result() -> str:
    """The combo-result string stored when the waitlist checkbox is present."""
    return f"{MARKER} — no slots; waitlist sign-up available"


def is_waitlist(message: str) -> bool:
    """True if a combo result string was produced by as_result()."""
    return bool(message) and message.startswith(MARKER)


def count_waitlist(results) -> int:
    """How many combinations in a route's results are waitlist-only."""
    return sum(1 for _label, message in (results or []) if is_waitlist(message))


# Angular binds the checkbox to this form control; the name is stable across
# VFS's UI reskins where element ids (e.g. mat-mdc-checkbox-0) are not.
_CHECKBOX_SELECTOR = 'mat-checkbox[formcontrolname="agreeToWaitlist"]'


def is_offered(page) -> bool:
    """
    True if the 'confirm waitlist' checkbox is present on the current
    Application Details step. READ-ONLY — never ticks it or submits the form.

    The page is already settled by the time this runs (the slot-banner read has
    just timed out with no availability), so a non-waiting visibility check is
    enough — no extra wait budget is spent on routes that do have slots.
    """
    try:
        if page.locator(_CHECKBOX_SELECTOR).first.is_visible():
            return True
    except Exception:
        pass
    # Fallback on the on-page wording, in case the form-control name changes.
    try:
        return page.get_by_text("confirm waitlist", exact=False).first.is_visible()
    except Exception:
        return False


def build_message(source_code: str, dest_code: str, results,
                  login_url: str = "") -> str:
    """
    Build the success-chat waitlist message naming each waitlist combination, or
    "" when no combination is waitlist-only (caller then sends nothing).
    """
    from src.utils import telegram_message as tm

    waitlisted = [label for label, message in (results or []) if is_waitlist(message)]
    if not waitlisted:
        return ""

    flag = tm._flag(dest_code)
    prefix = f"{flag} " if flag else ""
    lines = []
    for label in waitlisted:
        # e.g. '🇮🇹 Dubai - Italy - Tourist visa:' / '- Waitlist'
        lines.append(f"{prefix}{tm._label_with_country(label, dest_code)}:")
        lines.append("- Waitlist")
        lines.append("")
    body = "\n".join(lines).strip()
    if login_url:
        body += f"\n{login_url}"
    return body


def notify(source_code: str, dest_code: str, results, login_url: str = "") -> None:
    """
    Send the success-chat waitlist message for a route, if any combination is
    waitlist-only. No-op when there are none (so routes with real availability or
    genuine no-slots send nothing here), or when Telegram is unconfigured.

    Rate-limited per destination country: after a message is sent for a country,
    further waitlist messages for that country are suppressed for the configured
    cooldown (see waitlist_cooldown). The cooldown is only started on a real send.
    """
    from src.utils import telegram, waitlist_cooldown

    message = build_message(source_code, dest_code, results, login_url)
    if not message:
        return

    if waitlist_cooldown.is_on_cooldown(dest_code):
        mins = waitlist_cooldown.seconds_left(dest_code) / 60
        logging.info(
            f"Waitlist for {dest_code} still on cooldown ({mins:.0f} min left) "
            "— not resending."
        )
        return

    logging.info("Waitlist notice:\n" + message)
    if telegram.is_configured():
        telegram.send_message(message)
        # Start the cooldown only after an actual send.
        waitlist_cooldown.record_sent(dest_code)
    else:
        logging.warning("Telegram not configured — waitlist notice logged only.")