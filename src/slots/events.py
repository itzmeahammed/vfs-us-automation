"""Transitions between one check of a combination and the next.

An `opened` row is the moment a combination went from nothing to bookable. That
is the event the whole system is ultimately aiming to predict, and it only
exists if someone computes it at write time — reconstructing it later from raw
checks is possible but slow, and impossible to keep consistent once checks
arrive out of order from a backfill.

The one rule that matters: **compare against the last INFORMATIVE check**. An
`error` (a dropdown that wouldn't open) or a `disabled` marker says nothing
about availability. Treating those as state would manufacture a fake 'closed'
the moment a dropdown misbehaved, and a fake 'opened' when it recovered — which
would be the single most misleading signal on the dashboard, and poison for a
model trained on openings.
"""

from typing import Dict, List, Optional

from src.slots.parse import NONE, SLOT, WAITLIST

# Outcomes that tell us something about availability. `error` and `disabled`
# are gaps in observation, not states.
INFORMATIVE = (SLOT, WAITLIST, NONE)

OPENED = "opened"
CLOSED = "closed"
DATE_MOVED = "date_moved"
WAITLIST_OPENED = "waitlist_opened"
WAITLIST_CLOSED = "waitlist_closed"


def reference_date(dates: Optional[Dict[int, str]]) -> Optional[str]:
    """The date to track a combination by: the smallest party size quoted.

    Single applicants are the common sale and the size VFS always quotes, so
    it's the one series that exists for every combination. Bigger parties are
    still stored in full on `slot_dates` — this only picks the series that
    'the slot moved' is measured against.
    """
    if not dates:
        return None
    return dates[min(dates)]


def _delta_days(prev_date: Optional[str], new_date: Optional[str]) -> Optional[int]:
    from datetime import date
    if not prev_date or not new_date:
        return None
    try:
        return (date.fromisoformat(new_date) - date.fromisoformat(prev_date)).days
    except ValueError:
        return None


def diff(prev: Optional[dict], new: dict) -> List[dict]:
    """Events implied by moving from `prev` to `new`.

    Both are `{"outcome": str, "dates": {applicants: iso_date}}`; `prev` is None
    for a combination's very first informative check. Returns a list because one
    step can be two things at once — a combination that had a waitlist and now
    has a real slot both `opened` and closed its waitlist.

    The first-ever check deliberately emits nothing but an `opened` when a slot
    is already there: without a previous observation we cannot say availability
    *changed*, and inventing a transition would put a phantom opening at the
    start of every combination's history.
    """
    out: List[dict] = []
    new_outcome = new.get("outcome")
    if new_outcome not in INFORMATIVE:
        return out
    if prev is None:
        # Nothing to compare against. A combination whose first ever reading
        # already shows a slot did not 'open' — we simply arrived late, and
        # recording an opening here would put a phantom spike at the start of
        # every combination's history (and at the start of every backfill).
        return out

    new_date = reference_date(new.get("dates"))
    prev_outcome = prev.get("outcome") if prev else None
    prev_date = reference_date(prev.get("dates")) if prev else None

    def event(kind, **extra):
        out.append(dict(
            kind=kind,
            prev_outcome=prev_outcome,
            new_outcome=new_outcome,
            prev_date=prev_date,
            new_date=new_date,
            delta_days=None,
            **extra,
        ))

    had_slot = prev_outcome == SLOT
    has_slot = new_outcome == SLOT

    if has_slot and not had_slot:
        event(OPENED)
    elif had_slot and not has_slot:
        event(CLOSED)
    elif had_slot and has_slot and prev_date != new_date:
        # The date moving forward means slots were taken; moving back means VFS
        # released earlier ones. Both are worth a row — the sign carries it.
        out.append(dict(
            kind=DATE_MOVED,
            prev_outcome=prev_outcome,
            new_outcome=new_outcome,
            prev_date=prev_date,
            new_date=new_date,
            delta_days=_delta_days(prev_date, new_date),
        ))

    had_waitlist = prev_outcome == WAITLIST
    has_waitlist = new_outcome == WAITLIST
    if has_waitlist and not had_waitlist:
        event(WAITLIST_OPENED)
    elif had_waitlist and not has_waitlist:
        event(WAITLIST_CLOSED)

    return out
