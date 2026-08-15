"""Scrubs client PII out of everything the process writes.

Client files hold passport numbers, dates of birth, phone numbers and addresses.
Those values flow through the code as ordinary strings, so without this they
would eventually reach:

    app.log                  (truncated each run, but readable)
    logs/app-YYYY-MM-DD.log  (kept indefinitely)
    the console
    Telegram error messages  (which leave the machine entirely)

A logging.Filter is the right mechanism because it sits at the LAST point before
a record is formatted — so it catches PII regardless of which module logged it,
including exception text and third-party libraries that echo a value back.

Usage — install once, as early as possible, then register values as they load:

    redaction.install()                  # in the CLI, right after the logger
    redaction.register(person)           # for each client file loaded

Deliberately fail-open: if redaction itself errors, the log line is emitted
unchanged rather than lost. Losing an error message while debugging a live
registration is worse than the residual disclosure risk on a machine that
already holds the plaintext client files.

LIMITS — worth being honest about:
  * It redacts values we KNOW about (registered from loaded client files). A
    value that never passed through register() is not covered.
  * It is a safety net, not a licence to log PII deliberately. Code should still
    log person.label() and field NAMES, never field values.
  * It does not touch screenshots, which can obviously show a filled-in form.
"""

import logging
import re
from typing import Iterable, List, Set

#: Values currently being scrubbed. Module-level because logging filters are
#: process-wide; a run only ever handles a handful of clients.
_SECRETS: Set[str] = set()

#: Compiled alternation of every registered value, longest first so that a
#: longer value is replaced before a shorter one it contains.
_PATTERN = None

#: Values shorter than this are not redacted — they would match far too much
#: ordinary text ("50", "AE") and turn the log into noise.
MIN_LENGTH = 5

REPLACEMENT = "[redacted]"


def _rebuild() -> None:
    global _PATTERN
    if not _SECRETS:
        _PATTERN = None
        return
    ordered = sorted(_SECRETS, key=len, reverse=True)
    _PATTERN = re.compile("|".join(re.escape(s) for s in ordered),
                          re.IGNORECASE)


def add_values(values: Iterable[str]) -> int:
    """Registers literal values to scrub. Returns how many were added."""
    added = 0
    for value in values or ():
        text = str(value or "").strip()
        if len(text) >= MIN_LENGTH and text not in _SECRETS:
            _SECRETS.add(text)
            added += 1
    if added:
        _rebuild()
    return added


def register(registrant) -> int:
    """Registers one client's sensitive values (see registrant.SENSITIVE_FIELDS).

    Call this for every client file loaded, BEFORE anything that might log their
    data. Returns how many new values are now being scrubbed.
    """
    from src.waitlist.registrant import redaction_values

    count = add_values(redaction_values(registrant))
    if count:
        logging.debug(
            f"Redaction: now scrubbing {len(_SECRETS)} value(s) from logs "
            f"(+{count} from client '{registrant.id}')."
        )
    return count


def register_all(registrants: Iterable) -> int:
    return sum(register(r) for r in registrants or ())


def scrub(text: str) -> str:
    """Returns `text` with every registered value replaced. Safe on any input."""
    if not text or _PATTERN is None:
        return text
    try:
        return _PATTERN.sub(REPLACEMENT, text)
    except Exception:
        return text   # fail-open: never lose a log line to a redaction bug


class RedactionFilter(logging.Filter):
    """Scrubs registered values from a log record before it is formatted.

    Rewrites record.msg AND record.args, because "%s" formatting means a
    passport number is commonly in args rather than the message template.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if _PATTERN is None:
            return True
        try:
            if isinstance(record.msg, str):
                record.msg = scrub(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: scrub(v) if isinstance(v, str) else v
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(
                        scrub(a) if isinstance(a, str) else a
                        for a in record.args)
            # Exception text is a common leak path: a Playwright timeout often
            # quotes the value it was trying to type.
            if record.exc_text:
                record.exc_text = scrub(record.exc_text)
        except Exception:
            pass   # fail-open
        return True


_installed = False


def install() -> None:
    """Attaches the filter to every handler on the root logger.

    Idempotent. Call AFTER initialize_logger() (handlers must exist) and BEFORE
    loading any client file.

    The filter goes on HANDLERS rather than the root logger itself: a filter on a
    logger is not applied to records propagated from child loggers, so a handler
    filter is the only placement that catches everything.
    """
    global _installed
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RedactionFilter) for f in handler.filters):
            handler.addFilter(RedactionFilter())
    _installed = True
    logging.debug("Redaction filter installed on all log handlers.")


def is_installed() -> bool:
    return _installed


def clear() -> None:
    """Forgets every registered value (tests only)."""
    _SECRETS.clear()
    _rebuild()


def count() -> int:
    """How many values are currently being scrubbed."""
    return len(_SECRETS)
