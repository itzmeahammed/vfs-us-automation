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

from src.utils import run_context, state_paths

log = logging.getLogger(__name__)

# A durable LEDGER, not a log — so it belongs in state/ beside the waitlist
# journal, not in logs/ where a retention sweep prunes by age. Deleting a log
# costs you a diagnosis; deleting this costs you the only record that a card
# may have been charged.
#
# state_file() moves an existing logs/payments.jsonl here once, so the history
# of past payments survives the relocation rather than appearing to be empty —
# which would read as "no unanswered payments" on the endpoint whose entire job
# is to say otherwise.
JOURNAL_DIR = state_paths.STATE_DIR
JOURNAL_FILE = state_paths.state_file(
    "payments.jsonl", legacy=os.path.join("logs", "payments.jsonl"))

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
    # The join key. An unanswered payment is the worst thing in this system to
    # investigate, and this is what turns that investigation into one query:
    # the same id is on the API job, every log line, and the screenshots folder.
    record.setdefault("run_id", run_context.run_id())

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


#: Events that record the gateway's answer to a submitted payment.
#:
#: `payment_confirmed` has no writer in this repo today (only gateway.py writes
#: `payment_submitting` and `payment_result`). It is kept because an
#: out-of-band reconciler is the obvious thing to add here — but such a writer
#: MUST carry the original payment's `run_id`, or its row will not pair with
#: the submit it answers and the submit will stay flagged.
_ANSWERING_EVENTS = ("payment_result", "payment_confirmed")

#: Submits whose row carries no `run_id` at all. `append()` sets one from the
#: run context, so this should not happen — but a hand-edited or pre-`run_id`
#: row must not silently vanish from the report, so it gets its own bucket
#: rather than being dropped or merged with a real run.
_NO_RUN_ID = object()


def unanswered() -> list:
    """Rows saying a payment was submitted with no outcome recorded after it.

    THE FIRST THING TO CHECK after a run dies unexpectedly. Each of these is a
    payment that may have been charged, and none of them may be retried without
    a human confirming at the gateway.

    Correlation is by `run_id`, PAIRED IN ORDER within each run.

    This used to match on `booking_ref`, which is not a reference: runner.py
    writes `reference or slot`, and the walk leaves the reference empty, so in
    practice every row carries the appointment slot string — and the live
    journal confirms it. Two different runs that pick the same first-available
    slot therefore share a key. That made the most likely real retry — "the
    payment failed, try again" — collapse into one key, so if the retry died
    between the click and the gateway's answer, the earlier run's `payment_result`
    marked the later submit as answered and this function returned nothing.
    The console then renders "Every payment has a recorded outcome", which is
    the one thing it must never say wrongly. Rows with an empty `booking_ref`
    collapsed together for the same reason.

    `run_id` is the right key: `append()` stamps every row with it, and it also
    ties the row to the API job, the log lines and the screenshots folder.

    Pairing in order rather than by set membership matters because one run can
    submit more than once (a retry inside the same run). Set membership would
    mark every submit in that run answered as soon as any one of them was. A
    result pops the OLDEST still-open submit for its run, so a run with two
    submits and one result correctly reports one unanswered payment.
    """
    pending: Dict[Any, list] = {}

    # Ordered walk: the file is append-only, so file order is time order.
    for row in read_all():
        event = row.get("event")
        if event not in ("payment_submitting",) + _ANSWERING_EVENTS:
            continue
        key = row.get("run_id") or _NO_RUN_ID
        if event == "payment_submitting":
            pending.setdefault(key, []).append(row)
        elif pending.get(key):
            pending[key].pop(0)

    # Flatten back to a single list. Sorted by `at` so the report reads
    # oldest-first regardless of how the runs interleaved in the file.
    out = [row for queue in pending.values() for row in queue]
    out.sort(key=lambda r: str(r.get("at") or ""))
    return out
