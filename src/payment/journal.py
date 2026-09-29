"""The write-ahead record of a payment. Fsync'd before the click, or no click.

This is a separate journal from the waitlist's, and separate on purpose: that
one appends a `WaitlistResult`, and bending payment rows into that shape would
put card-adjacent data in a file the slot checker reads on every run.

    ══════════════════ WHY THE FSYNC IS THE ENTIRE POINT ══════════════════

A payment has three states, and only two of them are knowable from the outside:

    never submitted     nothing happened; retry freely
    submitted, answered the gateway told us yes or no
    SUBMITTED, UNANSWERED the process died between the click and the response

The third is the dangerous one. Without a row on disk it is indistinguishable
from the first — and treating it as "never submitted" means retrying, which
double-charges a real card. So the row is written and flushed to the platter
BEFORE the click. A crash then leaves evidence that a submit was imminent, which
is exactly what a human needs in order to go and look at the gateway.

`append()` returns the file path so the caller can name it in the exception it
raises. Never write card values here: only an amount, a URL, and identifiers.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict

log = logging.getLogger(__name__)

JOURNAL_DIR = os.path.join("logs")
JOURNAL_FILE = os.path.join(JOURNAL_DIR, "payments.jsonl")

#: Keys that must never be written, however they arrive. A belt to the braces
#: of "the caller does not pass them" — this file is the one place a card value
#: would be durable rather than transient.
_FORBIDDEN = ("number", "card_number", "cvn", "card_cvn", "pan", "expiry",
              "card_expiry", "jwk", "password")


def _clean(row: Dict[str, Any]) -> Dict[str, Any]:
    """Drop anything that looks like a card value, at any depth."""
    out = {}
    for key, value in (row or {}).items():
        if any(bad in str(key).lower() for bad in _FORBIDDEN):
            continue
        out[key] = _clean(value) if isinstance(value, dict) else value
    return out


def append(row: Dict[str, Any]) -> str:
    """Append one row and FLUSH IT TO DISK. Returns the path.

    Raises on failure rather than logging and continuing: the caller
    (gateway.submit_payment) treats an unwritable journal as a reason to refuse
    the payment entirely, which is the correct trade. A payment whose outcome
    could become unknowable must not be made.
    """
    os.makedirs(JOURNAL_DIR, exist_ok=True)

    record = _clean(row)
    record.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    line = json.dumps(record, ensure_ascii=False)
    with open(JOURNAL_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())     # survive a crash, not just a clean exit

    log.info(f"  payment journal -> {JOURNAL_FILE} ({record.get('event')})")
    return JOURNAL_FILE


def read_all() -> list:
    """Every row written so far. For `python -m src.payment status`."""
    if not os.path.isfile(JOURNAL_FILE):
        return []
    rows = []
    with open(JOURNAL_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                # A torn final line is expected after a crash — which is
                # precisely the case this journal exists for. Keep the rest.
                log.warning("payments.jsonl has a truncated final row "
                            "(a crash mid-write?) — skipping it.")
    return rows


def unanswered() -> list:
    """Rows saying a payment was submitted with no outcome recorded after it.

    THE FIRST THING TO CHECK after a run dies unexpectedly. Each of these is a
    payment that may have been charged, and none of them may be retried without
    a human confirming at the gateway.
    """
    rows = read_all()
    submitted = [r for r in rows if r.get("event") == "payment_submitting"]
    answered = {r.get("booking_ref") for r in rows
                if r.get("event") in ("payment_result", "payment_confirmed")}
    return [r for r in submitted if r.get("booking_ref") not in answered]
