"""Correct job records that filed a real payment as a plain failure.

WHAT THIS IS FOR

Until 2026-10-27 the booking CLI returned exit 1 whenever the payment step was
reached, because walk.py marks that step not-ok on every submit — deliberately,
so that nothing downstream can retry a payment that may have taken money. The
API read the 1 and recorded:

    status: failed      exit_code: 1      outcome: null
    detail: "Process exited with code 1."

for runs where the card HAD been charged, 3-D Secure had cleared, and VFS's own
redirect carried PaymentStatus=True. The same runs were recorded correctly on
the booking-request side ("booked and paid"), so the two halves of the API told
an operator opposite stories about whether a client's card had been charged.

The cause is fixed at source (exit codes 3 and 4, and the payment_submitted /
payment_declined job statuses). This repairs the records written before that.

WHY IT IS SAFE

It changes nothing about the payment and asserts nothing new about it. The
verdict comes from state/payments.jsonl — the row the gateway's own answer
produced — and a record is only touched when that journal says, unambiguously,
that a payment happened. The journal is never written here, only read.

HOW IT WRITES

By APPENDING a corrected record, never by rewriting the file. logs/api_jobs/
jobs.jsonl is an append-only log that JobStore.load_all() replays with
last-record-wins, which is the same mechanism the server's own startup
reconcile uses to mark abandoned jobs. The original line stays on disk, so the
correction is auditable and the history is not falsified — a record about money
should be correctable without being erasable.

    python -m src.utils.jobs_backfill          # dry run: say what would change
    python -m src.utils.jobs_backfill --apply  # append the corrections

*** A RUNNING API SERVES FROM MEMORY. *** It loads jobs.jsonl once at startup,
so a correction appended underneath it is not visible until it restarts. The
tool says so rather than leaving someone to wonder why the console still shows
red.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

JOBS_FILE = os.path.join("logs", "api_jobs", "jobs.jsonl")

#: The statuses a misfiled payment was recorded under. A job already carrying
#: one of the payment statuses has been corrected, or was written after the
#: fix, and must be left alone — re-appending would add a duplicate record
#: saying nothing new.
_WRONG_STATUSES = frozenset({"failed", "unknown", "timed_out"})


@dataclass
class Correction:
    """One record this tool would change, and what to."""

    job_id: str
    run_id: str
    was: str
    now: str
    gateway_answer: str
    registrant: str = ""
    slot: str = ""
    record: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __str__(self) -> str:
        who = f" {self.registrant}" if self.registrant else ""
        when = f" for {self.slot}" if self.slot else ""
        return (f"{self.job_id}  {self.was} -> {self.now}  "
                f"(gateway said {self.gateway_answer!r}{who}{when})")


@dataclass
class BackfillReport:
    scanned: int = 0
    corrections: List[Correction] = field(default_factory=list)
    applied: bool = False

    def __str__(self) -> str:
        if not self.corrections:
            return (f"Scanned {self.scanned} job record(s). "
                    "None filed a real payment as a failure.")
        verb = "Corrected" if self.applied else "Would correct"
        lines = [f"Scanned {self.scanned} job record(s). "
                 f"{verb} {len(self.corrections)}:"]
        lines += [f"  {c}" for c in self.corrections]
        return "\n".join(lines)


def _detail_for(answer: str) -> Tuple[str, str, bool]:
    """(detail, status, needs_attention) for a gateway answer.

    The wording is the same an affected run would get today, so a corrected
    record is indistinguishable from one written after the fix.
    """
    if answer == "success":
        return ("The payment went through: the gateway approved it and VFS "
                "returned a confirmation. The appointment is booked and paid.",
                "payment_submitted", False)
    if answer == "declined":
        return ("The gateway DECLINED the payment. VFS's own failure page says "
                "funds may still have been deducted, so declined is not the "
                "same as nothing happened. Check the card before re-arming — "
                "DO NOT re-run.",
                "payment_declined", True)
    return ("The payment was SUBMITTED and the gateway's answer was not read. "
            "The card may have been charged. Check the portal or the bank "
            "before doing anything else — DO NOT re-run.",
            "payment_submitted", False)


def plan(jobs_file: str = JOBS_FILE) -> BackfillReport:
    """Every record a payment contradicts, without writing anything."""
    from src.api.modules.jobs.manager import payment_facts
    from src.api.modules.jobs.store import JobStore

    report = BackfillReport()
    store = JobStore(jobs_file)
    records = store.load_all()
    report.scanned = len(records)

    for job_id, data in records.items():
        status = str(data.get("status") or "")
        if status not in _WRONG_STATUSES:
            continue

        run_id = str(data.get("run_id") or job_id)
        submitted, answer = payment_facts(run_id)
        if not submitted:
            continue

        detail, new_status, attention = _detail_for(answer)
        corrected = dict(data)
        corrected.update({
            "status": new_status,
            "outcome": new_status,
            "exit_code": 3 if new_status == "payment_submitted" else 4,
            "detail": detail,
            "needs_attention": bool(data.get("needs_attention")) or attention,
        })
        # The reason this line exists, on the line itself. A record that
        # contradicts an older one in the same file should say why.
        corrected["corrected_by"] = "src.utils.jobs_backfill"
        corrected["corrected_from"] = {
            "status": status,
            "exit_code": data.get("exit_code"),
            "detail": data.get("detail"),
        }

        report.corrections.append(Correction(
            job_id=job_id, run_id=run_id, was=status, now=new_status,
            gateway_answer=answer or "not_answered",
            registrant=str((data.get("payload") or {}).get("request_id") or ""),
            slot=_slot_for(run_id), record=corrected))

    return report


def _slot_for(run_id: str) -> str:
    """The appointment the payment was for, for a human-readable line."""
    try:
        from src.booking.autobook import rows_for_run
        from src.payment import journal

        for row in rows_for_run(journal.read_all(), run_id):
            if row.get("booking_ref"):
                return str(row["booking_ref"])
    except Exception:                                       # noqa: BLE001
        pass
    return ""


def backfill(jobs_file: str = JOBS_FILE,
             dry_run: bool = True) -> BackfillReport:
    """Append a corrected record for every misfiled payment."""
    from src.api.modules.jobs.store import JobStore

    report = plan(jobs_file)
    if dry_run or not report.corrections:
        return report

    store = JobStore(jobs_file)
    for correction in report.corrections:
        store.append(correction.record)
    report.applied = True
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.utils.jobs_backfill",
        description="Correct job records that filed a real payment as a "
                    "failure. Dry run unless --apply is given.")
    parser.add_argument("--apply", action="store_true",
                        help="append the corrections (default: only report)")
    parser.add_argument("--jobs-file", default=JOBS_FILE,
                        help=f"the job history to read (default: {JOBS_FILE})")
    args = parser.parse_args(argv)

    report = backfill(args.jobs_file, dry_run=not args.apply)
    print(report)

    if report.corrections and not args.apply:
        print("\nNothing was written. Re-run with --apply to append the "
              "corrections.")
    elif report.applied:
        print("\nThe original records are still on disk — the corrections were "
              "APPENDED, not substituted.")
        print("*** RESTART THE API to see them. *** It loads this file once at "
              "startup and serves from memory, so the console will keep "
              "showing the old status until it does.")
    return 0


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(main())
