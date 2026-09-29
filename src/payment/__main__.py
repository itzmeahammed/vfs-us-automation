"""`python -m src.payment status` — what the payment journal says.

    ═══════════ THE FIRST THING TO RUN AFTER ANY PAYMENT RUN ═══════════

The journal is the only durable record that a payment was attempted, and it is
written BEFORE the irreversible click precisely so that a crash leaves
evidence. But evidence nobody reads is not evidence, and `cat`ing a JSONL file
is not a review — so this prints it in the shape the decision actually takes:

    * UNANSWERED rows are payments that may have been charged with no outcome
      recorded. Each one must be resolved by a human at the gateway or the bank
      BEFORE that booking is attempted again.
    * DECLINED rows carry VFS's own references, which are what you quote when
      you ask them what happened.

A decline is NOT proof that nothing was charged. VFS's failure page says so
itself: "If funds have been deducted from your account, log in after 30 minutes
and check if your appointment is confirmed."
"""

import argparse
import sys
from typing import List

from src.payment import journal


def _fmt(row: dict) -> str:
    bits = [row.get("at", "?")]
    event = row.get("event", "?")
    bits.append(f"{event:20s}")

    outcome = row.get("outcome")
    if outcome:
        bits.append(f"[{str(outcome).upper()}]")

    for key in ("route", "registrant_id", "booking_ref"):
        value = row.get(key)
        if value:
            bits.append(f"{key}={value}")

    for key in ("requestrefno", "transactionid"):
        value = row.get(key)
        if value:
            bits.append(f"{key}={value}")

    return "  ".join(str(b) for b in bits)


def cmd_status(args) -> int:
    rows = journal.read_all()
    pending = journal.unanswered()

    print()
    print("=" * 68)
    print("PAYMENT JOURNAL")
    print("=" * 68)
    print(f"file: {journal.JOURNAL_FILE}")
    print(f"rows: {len(rows)}")

    if not rows:
        print()
        print("No payments have ever been submitted from this machine.")
        print("(The file is created on the first payment, not at startup —")
        print(" its absence means nothing was ever submitted, which is the")
        print(" strongest evidence available that no card was charged.)")
        return 0

    if args.all:
        print()
        for row in rows:
            print(f"  {_fmt(row)}")

    declined = [r for r in rows
                if r.get("event") == "payment_result"
                and r.get("outcome") == "failed"]
    if declined:
        print()
        print(f"DECLINED: {len(declined)}")
        for row in declined:
            print(f"  {_fmt(row)}")
        print()
        print("  A decline is NOT proof that nothing was charged. Check the")
        print("  account 30 minutes after the attempt before re-running.")

    print()
    if pending:
        print("!" * 68)
        print(f"UNANSWERED: {len(pending)} payment(s) submitted with NO "
              "recorded outcome")
        print("!" * 68)
        for row in pending:
            print(f"  {_fmt(row)}")
        print()
        print("Each of these MAY have been charged. Resolve every one at the")
        print("gateway or the bank before re-running that booking — a retry")
        print("double-charges.")
        return 1

    print("UNANSWERED: none — every submitted payment has a recorded outcome.")
    return 0


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.payment",
        description="Inspect the payment journal.")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser(
        "status", help="what the payment journal says (run this after any "
                       "payment run, and after any crash)")
    status.add_argument("--all", action="store_true",
                        help="print every row, not just the ones needing "
                             "attention")

    args = parser.parse_args(argv)
    if args.command == "status":
        return cmd_status(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
