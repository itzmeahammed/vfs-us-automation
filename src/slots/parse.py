"""Turns a slot banner's text into structured data.

Pure functions, no I/O — the single interpreter of VFS's wording, used by BOTH
the live recorder and the log seeder so a message can never be read one way
during a run and another way during a backfill.

The five things a combination check can say:

    'Earliest available slot for 1 Applicants is : 16-09-2026'   -> slot
    'WAITLIST - no slots; waitlist sign-up available'            -> waitlist
    'No slot message shown (no availability?).'                  -> none
    'ERROR: could not select centre 'Dubai''                     -> error
    'DISABLED'                                                   -> disabled

VFS quotes a different date per party size, and says so in two shapes:

    'for 1 Applicants is : 16-09-2026'      -> {1: ...}
    'for 1,2 applicants is : 28-09-2026'    -> {1: ..., 2: ...}

Several banners arrive newline-joined in one message, so a single check can
carry dates for 1, 2 and 3 applicants at once.

Dates are DD-MM-YYYY on the portal (also seen with '/'), and are normalised to
ISO 'YYYY-MM-DD' here — the one place that knows the portal's day-first order.
"""

import re
from datetime import date
from typing import Dict, Optional, Tuple

# Outcome vocabulary. Stored verbatim in checks.outcome.
SLOT = "slot"
WAITLIST = "waitlist"
NONE = "none"
ERROR = "error"
DISABLED = "disabled"

# 'for 1,2 Applicants is : 28-09-2026' — the applicant list and the date. The
# wording drifts between portals ('Applicants'/'applicants', spacing around the
# colon), so everything flexible is optional here.
_SLOT_RE = re.compile(
    r"earliest\s+available\s+slot\s*"
    r"(?:for\s+(?P<who>[\d\s,&and]+?)\s*applicants?\s*)?"
    r"(?:is)?\s*:?\s*"
    r"(?P<date>\d{1,2}[-/]\d{1,2}[-/]\d{2,4})",
    re.IGNORECASE,
)

_DATE_ONLY_RE = re.compile(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}")
_NUM_RE = re.compile(r"\d+")


def parse_date(text: str) -> Optional[str]:
    """'16-09-2026' (day-first, as VFS writes it) -> '2026-09-16'. None if unparseable.

    Two-digit years are read as 20xx; VFS has no 19xx appointments.
    """
    m = _DATE_ONLY_RE.search(text or "")
    if not m:
        return None
    parts = re.split(r"[-/]", m.group(0))
    if len(parts) != 3:
        return None
    try:
        day, month, year = (int(p) for p in parts)
    except ValueError:
        return None
    if year < 100:
        year += 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        # Guards a portal that ever switches to month-first: an impossible
        # day/month combination is dropped rather than silently mis-stored.
        return None


def _applicant_counts(who: Optional[str]) -> Tuple[int, ...]:
    """'1,2' -> (1, 2). A banner with no party size means a single applicant."""
    if not who:
        return (1,)
    counts = tuple(int(n) for n in _NUM_RE.findall(who))
    return counts or (1,)


def parse_message(message: str) -> Tuple[str, Dict[int, str]]:
    """Classifies a combination's result text.

    Returns `(outcome, {applicants: 'YYYY-MM-DD'})`. The date map is empty for
    every outcome except `slot`.

    Order matters: a date anywhere in the text wins, because a real availability
    banner is the only message that carries one. Only then do the no-slot
    wordings get considered — so a future banner that mentions 'waitlist' *and*
    quotes a date is still stored as the slot it is.
    """
    text = (message or "").strip()
    if not text:
        return NONE, {}

    dates: Dict[int, str] = {}
    for m in _SLOT_RE.finditer(text):
        iso = parse_date(m.group("date"))
        if not iso:
            continue
        for count in _applicant_counts(m.group("who")):
            # First banner wins for a party size: VFS repeats the same size only
            # when it re-renders, and the earliest read is the honest one.
            dates.setdefault(count, iso)

    if dates:
        return SLOT, dates

    low = text.lower()
    if low.startswith("error:") or low.startswith("could not select"):
        return ERROR, {}
    if text == "DISABLED" or low == "disabled":
        return DISABLED, {}
    if "waitlist" in low:
        return WAITLIST, {}
    return NONE, {}


def error_reason(message: str) -> Optional[str]:
    """The reason out of an error message, without the 'ERROR:' prefix."""
    text = (message or "").strip()
    if text.lower().startswith("error:"):
        return text[len("error:"):].strip() or None
    return text or None


def lead_days(slot_date: str, checked_on: str) -> Optional[int]:
    """Days from the check's local date to the appointment date.

    Anchored to when the check happened, so it stays meaningful forever — unlike
    a 'days from now' computed at read time, which rots the moment it's stored.
    Negative values are possible (a date that has already passed) and are kept
    rather than clamped, because they mark a stale portal banner worth seeing.
    """
    try:
        return (date.fromisoformat(slot_date) - date.fromisoformat(checked_on)).days
    except (TypeError, ValueError):
        return None
