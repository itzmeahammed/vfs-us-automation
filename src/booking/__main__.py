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
import sys
from typing import List

from src.booking import config as booking_config
from src.booking.errors import BookingConfigError


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )


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

    if problems:
        print(f"\n{len(problems)} config problem(s).")
        return 1
    print(f"\n{len(routes)} route config(s), all valid.")
    return 0


def cmd_status(args) -> int:
    """What is configured, and what is still unverified."""
    routes = booking_config.configured_routes()
    print(f"Booking configs ({len(routes)}): {', '.join(routes) or 'none'}\n")

    for route in routes:
        try:
            steps = booking_config.steps_for(route)
            policy = booking_config.identity_policy(route)
            enabled = booking_config.is_enabled(route)

            print(f"{route}  [{'ENABLED' if enabled else 'disabled'}]")
            for step in steps:
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


def cmd_probe(args) -> int:
    """Log in and read the dashboard. Read-only."""
    _install_redaction()

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
            walk=args.walk,
            to_step=args.to_step,
        )
    except Exception as e:
        print(f"\nProbe could not start: {e}")
        return 1

    print()
    print("=" * 68)
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
        return 0 if result.ok else 1

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

    if result.walk.stopped_at:
        print(f"\nStopped at '{result.walk.stopped_at}': {result.walk.reason}")

    print("\nNo slot was reserved and no payment was made. The slot is not held "
          "at any point, so abandoning here costs only this attempt — the client "
          "keeps their waitlist entry and their invitation.")
    return 0 if result.ok else 1


# --------------------------------------------------------------------------- #

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
                       help="leave the browser open to capture selectors by hand")
    probe.add_argument(
        "--walk", action="store_true",
        help="click 'Book Now' and walk the booking pages, reporting what is "
             "offered. REVERSIBLE — the slot is not reserved, and it stops "
             "before the committing step.")
    probe.add_argument(
        "--hold", dest="hold_seconds", type=int, default=0, metavar="SECONDS",
        help="with --keep-open, hold the session this long instead of waiting "
             "for Enter. Use it to keep ONE login alive across several "
             "inspections: a fresh login per run is what trips VFS's 429001.")
    probe.add_argument(
        "--to", dest="to_step", metavar="STEP",
        help="stop after this step (e.g. select_slot), for capturing one page "
             "at a time")

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

    commands = {"check": cmd_check, "status": cmd_status, "probe": cmd_probe}
    if args.command not in commands:
        parser.print_help()
        return 1
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
