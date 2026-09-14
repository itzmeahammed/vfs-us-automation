"""Turn a pass's observations into something a human reads.

The whole point of the observational phase is that a person looks at real VFS
mail, per country, and decides whether the matchers are right. This module is
that feedback loop, so it is written for the reader rather than for a machine:
it says what was found, what it thinks each message is, and — importantly — what
it did NOT recognise.

TWO RULES
---------
1. **Redaction, always.** Bodies and greetings carry client names; extracted
   fields carry names and reference numbers. Everything user-facing goes through
   waitlist.redaction.scrub() first, exactly as the registration path does.
   Telegram is an external service; a client's name must not be posted to it.

2. **Unmatched is the interesting part.** A digest that only reports successes
   hides the thing worth knowing: an email type nobody has written a matcher for
   yet. Those are surfaced first, not buried.

Nothing here triggers anything. Reporting is the terminal action of the
observational phase.
"""

from __future__ import annotations

import logging
from typing import List

from src.waitlist import redaction

log = logging.getLogger(__name__)


def _scrub(text: str) -> str:
    """Redaction that is safe to call even if nothing has been registered."""
    try:
        return redaction.scrub(text or "")
    except Exception:
        # Redaction failing must never be the reason an operator loses a digest,
        # but nor may raw text leak. Fall back to withholding the value.
        return "[redacted]"


def _line(observation) -> str:
    """One digest line for one observation."""
    match = observation.match
    bits = [f"{match.classification.upper()}"]
    if match.route:
        bits.append(match.route)
    if observation.account:
        bits.append(observation.account)

    shown = {k: v for k, v in (match.fields or {}).items() if v}
    if shown:
        rendered = ", ".join(f"{k}={_scrub(str(v))}" for k, v in sorted(shown.items()))
        bits.append(rendered)

    hours = observation.hours_left()
    if hours is not None:
        bits.append(f"{hours:.0f}h left" if hours > 0 else "EXPIRED")

    return " · ".join(bits)


def build_digest(result) -> str:
    """The human-readable summary of one pass. Empty string if there is nothing.

    Deliberately returns '' rather than 'nothing found' for a quiet pass: the
    watcher polls every few minutes and a message per pass would train the
    reader to ignore the channel.
    """
    lines: List[str] = []

    invitations = result.invitations()
    confirmations = result.confirmations()
    others = [
        o for o in result.observations
        if not o.match.is_invitation and not o.match.is_confirmation
    ]

    if invitations:
        lines.append(f"INVITATIONS ({len(invitations)})")
        lines.extend(f"  {_line(o)}" for o in invitations)

    if confirmations:
        if lines:
            lines.append("")
        lines.append(f"CONFIRMATIONS ({len(confirmations)})")
        lines.extend(f"  {_line(o)}" for o in confirmations)

    if others:
        if lines:
            lines.append("")
        lines.append(f"UNRECOGNISED VFS MAIL ({len(others)})")
        lines.append("  These matched no specific matcher. If the same shape keeps")
        lines.append("  appearing, it needs its own entry in config/inbox/.")
        for observation in others:
            subject = _scrub(observation.email.subject)[:90]
            lines.append(f"  {observation.account} · {subject}")

    if result.mailboxes_failed:
        if lines:
            lines.append("")
        lines.append(f"MAILBOXES THAT FAILED ({len(result.mailboxes_failed)})")
        lines.extend(f"  {m}" for m in result.mailboxes_failed)

    if not lines:
        return ""

    header = (
        f"VFS inbox watch — {result.mailboxes_checked} mailbox(es), "
        f"{result.messages_seen} message(s) examined"
    )
    return header + "\n\n" + "\n".join(lines)


def report(result) -> None:
    """Logs the digest, and Telegrams it when there is something worth sending.

    Observational only: this is where a pass ENDS. Nothing downstream is called.

    Telegram goes to the SUMMARY channel, not the success channel. The success
    channel's value is that it only fires when the hourly checker finds a
    bookable slot; filling it with mail digests would destroy that. Same
    reasoning as [waitlist] telegram_enabled defaulting off.
    """
    digest = build_digest(result)
    if not digest:
        log.info(
            f"Inbox pass: nothing new "
            f"({result.mailboxes_checked} mailbox(es) checked)."
        )
        return

    for line in digest.splitlines():
        log.info(line)

    # An invitation is the only thing time-critical enough to push. Everything
    # else sits in the log for whoever is reviewing the matchers.
    if not result.invitations() and not result.mailboxes_failed:
        return

    try:
        from src.utils import telegram

        if telegram.is_error_configured():
            telegram.send_error(digest)
    except Exception as e:
        log.warning(f"Could not send the inbox digest to Telegram: {e}")
