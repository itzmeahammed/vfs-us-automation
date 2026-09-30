"""Command line for the booking package.

    python -m src.booking check                      validate configs   OFFLINE
    python -m src.booking status                     what's configured  OFFLINE
    python -m src.booking probe -sc AE -dc GRC       log in, read the dashboard
    python -m src.booking probe -sc AE -dc GRC --keep-open

`probe` is READ-ONLY. It logs in, reads the dashboard, reports what it found,
and stops. It does not click "Book Now", submit anything, or change VFS state.

`--keep-open` leaves the browser up afterwards, which is how the selectors for
the pages after "Book Now" get captured: click through by hand with devtools
open while the authenticated session is still live.

There is no `book` command. The booking runner does not exist yet — it is
blocked on exactly those selectors.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List

from src.booking import config as booking_config
from src.booking.errors import BookingConfigError


#: Where a probe's log lands, in addition to the console.
LOG_FILE = os.path.join("logs", "booking.log")

#: The queryable log. One JSON object per line, every line stamped with the
#: run_id, so a run can be reconstructed exactly rather than guessed at from
#: timestamps. This is the one to keep when disk is tight.
JSON_LOG_FILE = os.path.join("logs", "app.jsonl")


def _setup_logging(verbose: bool) -> None:
    """Console + logs/booking.log + the queryable logs/app.jsonl.

    The text file is the point for a human. basicConfig alone writes to stderr,
    and a probe is routinely run detached — in the background, over SSH, from a
    scheduler — where stderr goes nowhere. A live run that fails then leaves no
    trace of WHY, which cost a whole diagnosis cycle on 2026-09-26: the
    dropdown that failed had already logged the options it WAS offered, and
    that message was thrown away.

    The JSONL file is the point for a machine. Every line carries the run_id,
    so one query returns every line of one run:

        jq 'select(.run_id=="<id>")' logs/app.jsonl

    Both append rather than truncate: consecutive runs of the same flow are
    exactly what you want to compare when one of them behaves differently.

    The run id is printed on the first line. When the API spawned this process
    it was inherited from VFS_RUN_ID, so it equals the job_id the caller is
    holding — which is what makes the API able to serve this run's trace.
    """
    from src.utils.log_setup import setup

    run = setup(verbose=verbose, text_log=LOG_FILE, json_log=JSON_LOG_FILE)
    logging.getLogger(__name__).info(
        f"run_id={run}  (logs/app.jsonl, {LOG_FILE})")


def _install_redaction() -> None:
    """Loads client values into the log redaction filter before anything logs.

    The probe prints applicant names off the dashboard; without this they reach
    the terminal and the log file in the clear.
    """
    try:
        from src.waitlist import redaction, registrant

        redaction.install()
        redaction.register_all(registrant.load_all(skip_invalid=True))
    except Exception as e:
        logging.warning(f"Could not install log redaction: {e}")


# --------------------------------------------------------------------------- #
# Commands                                                                     #
# --------------------------------------------------------------------------- #

def cmd_check(args) -> int:
    """Validate every config/booking/*.json. Offline."""
    routes = booking_config.configured_routes()
    if not routes:
        print("No booking configs found in config/booking/.")
        return 1

    problems = booking_config.check()
    for route in routes:
        try:
            steps = booking_config.steps_for(route)
            commit = booking_config.commit_step_name(route)
            state = "enabled" if booking_config.is_enabled(route) else "disabled"
            print(f"  OK   {route}: {len(steps)} step(s), commits at "
                  f"'{commit}' [{state}]")
        except BookingConfigError as e:
            print(f"  FAIL {route}: {e}")

    client_problems = _check_client_windows()

    if problems or client_problems:
        if problems:
            print(f"\n{len(problems)} config problem(s).")
        return 1
    print(f"\n{len(routes)} route config(s), all valid.")
    return 0


def _check_client_windows() -> int:
    """Validate every client's requested date window. OFFLINE. Returns a count.

        ═══════ WHY THIS RUNS BEFORE A BROWSER EXISTS ═══════

    The window comes from a sales agent typing dates into a client record, so
    the realistic mistakes are typing mistakes: 10/11/2026 instead of
    2026-11-10, one end filled and not the other, the range backwards, a date
    already past. walk.check_window catches all of them — but it was only ever
    called from INSIDE the walk, which is after a login.

    A login is the scarce resource: VFS blocks an account after roughly three
    in a short window, and that block outlives a 12-hour invitation. Spending
    one to be told a date was typed with slashes is the most avoidable failure
    in this system.
    """
    from src.booking import walk
    from src.waitlist import registrant as registrant_mod

    try:
        ids = registrant_mod.available_ids()
    except Exception as e:                                  # noqa: BLE001
        print(f"\n(could not read client records: {e})")
        return 0

    checked = 0
    bad = 0
    for client_id in ids:
        try:
            person = registrant_mod.load(client_id)
            values = person.as_context()
        except Exception as e:                              # noqa: BLE001
            print(f"  FAIL {client_id}: unreadable ({e})")
            bad += 1
            continue

        # EVERY bookable client is checked, including those with no window —
        # a missing range is now a REFUSAL, not a fallback to "earliest", so
        # skipping them here would report "all valid" for records that cannot
        # book at all. That is the stale behaviour this check exists to catch.
        window = walk.date_window(values)
        checked += 1
        try:
            strategy = walk.resolve_strategy(values, {})
        except Exception as e:                              # noqa: BLE001
            print(f"  FAIL {client_id}: {e}")
            bad += 1
            continue

        issues = walk.check_window(values)
        if issues:
            bad += 1
            print(f"  FAIL {client_id}: " + " ".join(issues))
        else:
            start, end = window
            when = (f"{start} .. {end}" if start and end else "no window")
            print(f"  OK   {client_id}: strategy '{strategy}', {when}")

    if checked:
        print(f"\n{checked} client date window(s) checked, {bad} problem(s).")
    return bad


def cmd_status(args) -> int:
    """What is configured, and what is still unverified."""
    routes = booking_config.configured_routes()
    print(f"Booking configs ({len(routes)}): {', '.join(routes) or 'none'}\n")

    for route in routes:
        try:
            steps = booking_config.steps_for(route)
            policy = booking_config.identity_policy(route)
            enabled = booking_config.is_enabled(route)

            modes = booking_config.entry_modes(route)
            print(f"{route}  [{'ENABLED' if enabled else 'disabled'}]  "
                  f"flows: {', '.join(modes) or 'NONE'}")

            # Per flow, because they no longer share a step list: a waitlist
            # booking resumes an application VFS already made, a live-slot
            # booking creates one. Printing the file's raw steps would show a
            # sequence neither flow actually runs.
            for mode in modes:
                print(f"  {mode}:")
                for step in booking_config.steps_for(route, mode):
                    mark = " <-- COMMITS" if step.get("commits") else ""
                    print(f"    {step.get('type', '?'):18} {step['name']}{mark}")
            print(f"    identity: min_confidence={policy['min_confidence']}, "
                  f"require_unique={policy['require_unique_match']}")
            print()
        except BookingConfigError as e:
            print(f"{route}  UNUSABLE: {e}\n")

    print("NOTE: every booking route ships disabled. The steps after "
          "'Book Now' have never been walked in a browser, so their selectors "
          "are placeholders. Use `probe --keep-open` to capture the real ones.")
    return 0


def _applicant_fields(pairs) -> dict:
    """Parse repeated --applicant KEY=VALUE into the walk's context.

    Rejects a malformed pair rather than ignoring it: a typo'd
    "--applicant first_nameZaid" would otherwise resolve to an empty First Name
    and stop the walk on the page it was supplied to get past, which is a login
    spent for nothing.
    """
    fields = {}
    for pair in pairs or []:
        key, sep, value = str(pair).partition("=")
        if not sep or not key.strip():
            raise ValueError(
                f"--applicant expects KEY=VALUE, got {pair!r} (e.g. "
                "--applicant first_name=Zaid)")
        fields[key.strip()] = value
    return fields


def _confirm_commit(args) -> bool:
    """Make a person say yes before a run can spend money.

    Deliberately a PROMPT and not just a flag. --commit is one word on a line
    that otherwise looks exactly like the dry run everyone has been typing for
    days, and the difference between them is a real appointment and a real
    charge. Scheduled runs pass --yes; a human at a terminal gets asked.

    Prints what it is about to do first, because "are you sure?" with no
    subject is a question nobody reads.
    """
    from src.booking import config as booking_config
    from src.payment import card as card_mod

    route = f"{args.source_country}-{args.dest_country}".upper()

    print()
    print("=" * 68)
    print("  --commit: THIS RUN WILL BOOK AN APPOINTMENT AND PAY FOR IT")
    print("=" * 68)
    print(f"  route      {route}")
    print(f"  client     {args.registrant or '(none — --applicant fields)'}")

    try:
        card = card_mod.load()
        print(f"  card       {card.masked if card else 'NOT SET'}")
    except Exception as e:                                  # noqa: BLE001
        print(f"  card       UNUSABLE: {e}")
        return False

    if card is None:
        print()
        print("  No card is configured, so the payment step cannot run. Set "
              "VFS_CARD_NUMBER, VFS_CARD_EXPIRY and VFS_CARD_CVN.")
        return False

    try:
        commit_step = booking_config.commit_step_name(route,
                                                      args.entry or "")
        print(f"  commits at '{commit_step}'")
    except Exception:                                       # noqa: BLE001
        pass

    print()
    print("  The appointment is real, the charge is real, and neither can be "
          "undone from here.")
    print()

    if args.yes:
        print("  (--yes given, proceeding without asking)")
        return True

    try:
        answer = input("  Type BOOK to continue, anything else to abort: ")
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        return False

    if answer.strip() != "BOOK":
        print("  Aborted — nothing was clicked.")
        return False
    return True


def cmd_probe(args) -> int:
    """Log in and read the dashboard. Read-only unless --commit is given."""
    _install_redaction()

    # Imported once for the whole function: several of the messages below name
    # the payment journal's path, and they are read by an operator mid-incident.
    # Naming it from the constant rather than hardcoding "logs/payments.jsonl"
    # means the journal can move without sending someone to a file that is no
    # longer there.
    from src.payment import journal as payment_journal

    if getattr(args, "commit", False) and not getattr(args, "walk", False):
        print("--commit needs --walk.\n\n"
              "  Without --walk the probe is READ-ONLY: it logs in, reads the "
              "dashboard and stops,\n"
              "  so --commit would have nothing to commit. Add --walk to "
              "actually walk the booking\n"
              "  pages, or drop --commit to keep this run read-only.")
        return 1

    if getattr(args, "commit", False) and not _confirm_commit(args):
        return 1

    from src.booking.probe import run_probe

    try:
        result = run_probe(
            source=args.source_country,
            dest=args.dest_country,
            registrant_id=args.registrant,
            email=args.email,
            password=args.password,
            proxy=args.proxy_url,
            keep_open=args.keep_open,
        hold_seconds=getattr(args, 'hold_seconds', 0),
        entry=getattr(args, 'entry', '') or '',
        combo=getattr(args, 'combo', '') or '',
            walk=args.walk,
            to_step=args.to_step,
            applicant=_applicant_fields(args.applicant),
            commit=getattr(args, "commit", False),
            capture=getattr(args, "capture", "") or "",
        )
    except KeyboardInterrupt:
        # Ctrl-C before run_probe's own handler is reachable — during
        # launch, login, or after it has returned. Nothing is in flight
        # here, so this is a clean exit rather than a traceback.
        print("\nInterrupted before the probe started. Nothing was clicked.")
        return 130
    except Exception as e:
        print(f"\nProbe could not start: {e}")
        return 1

    print()
    print("=" * 68)
    if result.interrupted:
        print("INTERRUPTED BY Ctrl-C")
    print(f"BOOKING PROBE — {result.route}  ({result.account})")
    print("=" * 68)

    if result.errors:
        print("\nERRORS:")
        for error in result.errors:
            print(f"  {error}")

    print(f"\nApplication cards found: {len(result.rows)}")
    for row in result.rows:
        print(f"  {row.summary()}")

    if not result.rows:
        print("  (none — this account may hold no active applications, or the "
              "card selector may not match this portal)")

    print(f"\nBookable now (waitlist status says slots available): "
          f"{len(result.bookable)}")
    for row in result.bookable:
        print(f"  {row.summary()}")

    if result.match_reason:
        print(f"\nClient match: "
              f"{result.matched.summary() if result.matched else 'NONE'}")
        print(f"  reason: {result.match_reason}")

    if result.walk is None:
        print("\nNothing was clicked. No VFS state changed.")
        return _probe_exit_code(result)

    print()
    print("-" * 68)
    print("BOOKING WALK")
    print("-" * 68)
    for step in result.walk.steps:
        print(f"  {step.summary()}")
        if step.url:
            print(f"        url: {step.url}")
        for key, value in (step.found or {}).items():
            shown = ", ".join(value) if isinstance(value, list) else value
            print(f"        {key}: {shown or '(none)'}")
        # The page's own text is what makes an UNRECOGNISED page useful rather
        # than a dead end — it is the raw material for the next config.
        if not step.ok and step.page_text:
            print(f"        page: {step.page_text[:300]}")

    if getattr(result.walk, "payment_declined", False):
        print()
        print("!" * 68)
        print("PAYMENT DECLINED — DO NOT SIMPLY RE-RUN")
        print("!" * 68)
        print(result.walk.reason)
        print()
        print("A decline is NOT proof that nothing was charged. VFS's own")
        print("page says: if funds were deducted, log in after 30 minutes and")
        print("check whether the appointment was confirmed. Do that first.")
        print()
        print("The references above are what you quote to VFS or the bank.")
        print(f"They are also in {payment_journal.JOURNAL_FILE}.")
    elif getattr(result.walk, "blocked", False):
        # Loud, and phrased as an instruction. This is not a bug to
        # debug: VFS has said the account already has a booking in
        # progress, and the one dangerous response is the reflex one —
        # run it again.
        print()
        print("!" * 68)
        print("BLOCKED BY VFS — DO NOT RE-RUN")
        print("!" * 68)
        print(result.walk.reason)
        print()
        print("Re-running would book a SECOND appointment and charge the "
              "card a SECOND time. Check the account at VFS first.")
    elif result.walk.stopped_at:
        print(f"\nStopped at '{result.walk.stopped_at}': {result.walk.reason}")

    if getattr(args, "commit", False):
        print("\nThis was a --commit run. If the payment step was reached, an "
              "appointment was booked and a card was charged — check "
              f"{payment_journal.JOURNAL_FILE} and the captures above. DO NOT "
              "re-run to 'try again' without confirming the outcome first.")
    else:
        print("\nNo slot was reserved and no payment was made. The slot is not "
              "held at any point, so abandoning here costs only this attempt — "
              "the client keeps their waitlist entry and their invitation.")
    return _probe_exit_code(result)


def cmd_autobook(args) -> int:
    """Book stored requests. Started by the supervisor, not by hand."""
    _install_redaction()
    from src.booking import autobook

    seen = {}
    for pair in args.seen or []:
        rid, _, day = str(pair).partition("=")
        seen[rid.strip()] = day.strip()
    return autobook.run_queue(args.route.upper(), args.request or [], seen)


def cmd_requests(args) -> int:
    """List booking requests and where each one is. Offline."""
    from src.booking import requests as store

    rows = store.list_all(route=args.route, status=args.status)
    if not rows:
        print("No booking requests.")
        return 0
    for req in rows:
        start, end = req.window()
        last = (req.data.get("history") or [{}])[-1]
        print(f"  {req.request_id:<28} {req.route:<7} {req.status:<16} "
              f"{'on ' if req.enabled else 'off'} {start}..{end}  "
              f"attempts={req.data.get('attempts', 0)}  "
              f"last: {last.get('event', '')} {last.get('detail', '')}"[:200])
    return 0


# --------------------------------------------------------------------------- #

def _probe_exit_code(result) -> int:
    """0 success, 130 the operator stopped it, 1 it failed.

    130 is the shell's SIGINT convention (128 + 2). Keeping it distinct from 1
    matters because a wrapper or a supervisor reads these: "the human pressed
    Ctrl-C" must never look like "the run failed and should be retried" — and
    on a --commit run a retry is a second booking and a second charge.
    """
    if getattr(result, "interrupted", False):
        return 130
    return 0 if result.ok else 1


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.booking",
        description="Booking: config checks and a READ-ONLY dashboard probe. "
                    "The booking runner does not exist yet.",
    )
    # -v is accepted BOTH before and after the subcommand. argparse would
    # otherwise reject `probe ... -v`, which is the order everyone types.
    verbose = argparse.ArgumentParser(add_help=False)
    verbose.add_argument("-v", "--verbose", action="store_true",
                         help="DEBUG logging")

    parser.add_argument("-v", "--verbose", action="store_true",
                        help="DEBUG logging")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check", parents=[verbose],
                   help="validate config/booking/*.json (offline)")
    sub.add_parser("status", parents=[verbose],
                   help="what is configured (offline)")

    probe = sub.add_parser(
        "probe", parents=[verbose],
        help="log in and read the dashboard (READ-ONLY, clicks nothing)")
    probe.add_argument("-sc", "--source-country", required=True, help="e.g. AE")
    probe.add_argument("-dc", "--dest-country", required=True, help="e.g. GRC")
    probe.add_argument("--registrant", help="client id, for the expected-row match")
    probe.add_argument("--email", help="force a VFS account (needs --password)")
    probe.add_argument("--password", help="that account's password")
    probe.add_argument("--proxy-url", help='force a proxy; "" forces local IP')
    probe.add_argument("--keep-open", action="store_true",
                       help="leave the browser open to capture selectors by "
                            "hand. RECONNAISSANCE ONLY — off by default, and a "
                            "scheduled run must never set it: it holds the "
                            "account session and the run lock waiting for an "
                            "operator who is not there.")
    probe.add_argument(
        "--capture", choices=["off", "failure", "full"], default="failure",
        help="what to leave on disk. 'failure' (default) writes a screenshot "
             "when a step fails; 'full' adds the rendered DOM of every page, "
             "for mapping a new country; 'off' writes nothing.")
    probe.add_argument(
        "--walk", action="store_true",
        help="click 'Book Now' and walk the booking pages, reporting what is "
             "offered. REVERSIBLE — the slot is not reserved, and it stops "
             "before the committing step.")
    probe.add_argument(
        "--combo", default="",
        help="which combination to book, e.g. 'Norway Visa Application Center "
             "- Dubai - Tourist'. Supplies the centre/category/sub-category "
             "dropdowns; required for the 'new' flow.")
    probe.add_argument(
        "--entry", choices=["waitlist", "new"], default="",
        help="which way in: 'waitlist' resumes an invited application, 'new' "
             "creates one from a live slot. Defaults to what the route "
             "supports.")
    probe.add_argument(
        "--hold", dest="hold_seconds", type=int, default=0, metavar="SECONDS",
        help="with --keep-open, hold the session this long instead of waiting "
             "for Enter. Use it to keep ONE login alive across several "
             "inspections: a fresh login per run is what trips VFS's 429001.")
    probe.add_argument(
        "--applicant", action="append", default=[], metavar="KEY=VALUE",
        help="supply one applicant field for the walk, e.g. "
             "--applicant first_name=Zaid. Repeatable. The live-slot flow has "
             "no client roster (there is no invitation to match a client to), "
             "so without this the walk dies on 'Your Details' — the page where "
             "the five unmapped pages begin. Overrides the client file when "
             "both are given.")
    probe.add_argument(
        "--commit", action="store_true",
        help="ACTUALLY BOOK AND PAY. REQUIRES --walk. Without this the walk "
             "stops in front of the committing step having changed nothing. "
             "With it, the run completes the booking and submits a real "
             "payment from the card in "
             "VFS_CARD_NUMBER/VFS_CARD_EXPIRY/VFS_CARD_CVN. There is no undo. "
             "Requires the route AND the payment processor to be enabled, and "
             "asks for confirmation unless --yes is given.")
    probe.add_argument(
        "--yes", action="store_true",
        help="skip the --commit confirmation prompt (for scheduled runs)")
    probe.add_argument(
        "--to", dest="to_step", metavar="STEP",
        help="stop after this step (e.g. select_slot), for capturing one page "
             "at a time")

    auto = sub.add_parser(
        "autobook", parents=[verbose],
        help="book stored booking requests AND PAY. Started by the supervisor "
             "when a slot inside a request's window is seen; not for hand use.")
    auto.add_argument("--route", required=True, help="e.g. AE-NOR")
    auto.add_argument("--request", action="append", default=[],
                      help="request id, in booking order. Repeatable.")
    auto.add_argument("--seen", action="append", default=[], metavar="ID=DATE",
                      help="the earliest date that triggered this request")

    reqs = sub.add_parser("requests", parents=[verbose],
                          help="list booking requests (offline)")
    reqs.add_argument("--route", default=None)
    reqs.add_argument("--status", default=None)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    # Load the INI before any command runs. Every other entry point does this
    # (waitlist/__main__.py:766); booking did not, and got away with it only
    # because resolving a --registrant happened to initialise config as a side
    # effect. Passing --email skips that path, so the first config read hit
    # _config = None and the probe died with
    # "'NoneType' object has no attribute 'has_section'" — a confusing error
    # for a missing call, on the exact path used when there is no client file.
    from src.utils.config_reader import initialize_config

    initialize_config()

    commands = {"check": cmd_check, "status": cmd_status, "probe": cmd_probe,
                "autobook": cmd_autobook, "requests": cmd_requests}
    if args.command not in commands:
        parser.print_help()
        return 1
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
