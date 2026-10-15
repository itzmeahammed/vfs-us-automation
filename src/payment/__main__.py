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
import os
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


def cmd_card(args) -> int:
    """Prove the configured card loads, offline, before a run needs it.

    Every check this runs is one that otherwise fires on a live payment page
    with a client's booking sitting on it: a mistyped digit, an expiry that
    passed last month, a CVN from a different card, a card file the gateway
    will never see because it is not where the bot looks.

    It prints the masked card and nothing else. There is no --show.
    """
    from src.payment import card as card_mod

    path = card_mod.resolve_card_file(os.environ, explicit_env=False)
    env_set = [k for k in (card_mod.ENV_NUMBER, card_mod.ENV_EXPIRY,
                           card_mod.ENV_CVN, card_mod.ENV_TYPE,
                           card_mod.ENV_NAME)
               if str(os.environ.get(k, "")).strip()]

    print()
    print("=" * 68)
    print("PAYMENT CARD")
    print("=" * 68)
    print(f"environment: {', '.join(env_set) if env_set else 'nothing set'}")
    print(f"card file:   {path}"
          f"{'' if path and os.path.isfile(path) else '  (not present)'}")
    print()

    try:
        card = card_mod.load()
    except card_mod.CardError as exc:
        print("UNUSABLE")
        print()
        for line in str(exc).splitlines():
            print(f"  {line}")
        print()
        print("Nothing can be paid for until this is fixed. A route with a")
        print("payment step will refuse at the start of the run, which is")
        print("the intended behaviour — it refuses before booking a slot.")
        return 1

    if card is None:
        print("NOT CONFIGURED — no card number in either source.")
        print()
        print("This is fine unless a route has a payment step. To set one up:")
        print("  cp config/card.local.txt.example config/card.local.txt")
        print("  # then edit it, and on POSIX: chmod 600 config/card.local.txt")
        return 1

    print(f"OK: {card.masked}, expires {card.expiry_mm_yy}"
          f"{', ' + card.name if card.name else ''}")
    print()
    print("Valid shape, passes Luhn, not expired. That is everything that")
    print("can be checked without charging it — only the issuer knows")
    print("whether it has funds or is blocked for online use.")
    return 0


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.payment",
        description="Inspect the payment journal and the configured card.")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser(
        "status", help="what the payment journal says (run this after any "
                       "payment run, and after any crash)")
    status.add_argument("--all", action="store_true",
                        help="print every row, not just the ones needing "
                             "attention")

    sub.add_parser(
        "card", help="check the configured card loads and is not expired "
                     "(offline; prints only the masked number)")

    args = parser.parse_args(argv)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "card":
        return cmd_card(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
