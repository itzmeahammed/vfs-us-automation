"""Telegram reporting for the waitlist feature.

Two distinct messages, deliberately separate:

  * ``notify()``            — "a waitlist is AVAILABLE for these combos"
                              (the existing, detection-only notice; rate-limited
                              per destination country by waitlist_cooldown)
  * ``notify_registered()`` — "we REGISTERED for the waitlist"
                              (new; never rate-limited — a real registration is
                              always worth telling you about, and it happens at
                              most a handful of times)

Moved here from src/vfs_bot/waitlist.py unchanged (plus the new registration
message); that module is now a thin re-export shim.
"""

import logging

from src.waitlist.detect import is_waitlist
from src.waitlist.result import Status


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


# --------------------------------------------------------------------------- #
# Registration reporting (new)                                                 #
# --------------------------------------------------------------------------- #

_STATUS_ICON = {
    Status.SUCCESS: "✅",
    Status.DRY_RUN: "🧪",
    Status.UNKNOWN: "⚠️",
    Status.PENDING: "⚠️",
    Status.FAILED: "❌",
    Status.SKIPPED: "⏭️",
}


def build_registration_message(result, login_url: str = "") -> str:
    """Render one WaitlistResult as a Telegram message."""
    from src.utils import telegram_message as tm

    icon = _STATUS_ICON.get(result.status, "•")
    flag = tm._flag(result.route.split("-")[-1])
    head = f"{icon} Waitlist {result.status.upper()}"

    lines = [f"{flag} {head}".strip(), ""]
    lines.append(f"Route: {result.route}")
    lines.append(f"Combo: {result.combo}")
    lines.append(f"Registrant: {result.registrant_id}")
    if result.account:
        lines.append(f"Account: {result.account}")
    if result.vfs_reference:
        lines.append(f"Reference: {result.vfs_reference}")
    if result.reason:
        lines.append(f"Note: {result.reason}")

    if result.needs_attention:
        lines.append("")
        lines.append(
            "⚠️ NEEDS A HUMAN — the submit may or may not have gone through. "
            "Check this account's bookings on the VFS portal before re-running."
        )

    if login_url:
        lines.append("")
        lines.append(login_url)
    return "\n".join(lines)


def notify_registered(result, login_url: str = "") -> None:
    """
    Report a registration outcome — to the LOG always, to Telegram only if
    [waitlist] telegram_enabled is on (it is OFF by default).

    Telegram is off by default because this bot runs ON DEMAND, with you
    watching the terminal: the CLI already prints every outcome, so a message is
    redundant noise in a chat whose whole value is that it only pings when the
    hourly slot checker finds something.

    The message is scrubbed before sending. Telegram is the one sink that leaves
    the machine, so a stray passport number in an error string would be a real
    disclosure — the logging filter cannot help here because this text is passed
    to the API directly, not logged.
    """
    from src.settings import settings
    from src.waitlist import redaction

    if result.status == Status.SKIPPED:
        return  # a guard declining is a log line, not a message

    message = redaction.scrub(build_registration_message(result, login_url))
    logging.info("Waitlist registration:\n" + message)

    if not settings().waitlist.telegram_enabled:
        logging.debug("Telegram off for waitlist runs "
                      "([waitlist] telegram_enabled = false) — logged only.")
        return

    from src.utils import telegram

    if result.needs_attention or result.status == Status.FAILED:
        if telegram.is_error_configured():
            telegram.send_error(message)
            return
    if telegram.is_configured():
        telegram.send_message(message)
    else:
        logging.warning("Telegram not configured — registration notice logged only.")
