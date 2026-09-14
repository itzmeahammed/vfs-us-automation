"""Command line for the inbox watcher.

    python -m src.inbox check                    validate every config/inbox/*.json
    python -m src.inbox status                   what is configured, which mailboxes
    python -m src.inbox test                     matchers vs tests/fixtures/emails/
    python -m src.inbox test --file x.eml        matchers vs one file
    python -m src.inbox watch --once             one pass over every mailbox
    python -m src.inbox watch                    keep watching

`check` and `test` touch no network at all — they are the offline preflight, and
the way to iterate on a matcher without going near a mailbox.

Nothing here triggers a booking, a browser, or any VFS request. This package
observes.
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import sys
from typing import List

from src.inbox import config as inbox_config
from src.inbox.matcher import Email, MatcherConfigError, classify_all

FIXTURE_DIR = os.path.join("tests", "fixtures", "emails")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _install_redaction() -> None:
    """Loads client values into the redaction filter before anything logs.

    The watcher prints subjects and extracted names; without this they would
    reach the terminal and the log file in the clear. Best-effort: a broken
    registrant file must not stop the watcher, but it does get reported.
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
    """Validates every inbox config. Offline."""
    routes = inbox_config.configured_routes()
    if not routes:
        print("No inbox configs found in config/inbox/.")
        return 1

    problems = inbox_config.check()
    for route in routes:
        try:
            matchers = inbox_config.matchers_for(route)
            names = ", ".join(m.get("name", "?") for m in matchers)
            print(f"  OK   {route}: {len(matchers)} matcher(s) — {names}")
        except MatcherConfigError as e:
            print(f"  FAIL {route}: {e}")

    if problems:
        print(f"\n{len(problems)} config problem(s).")
        return 1
    print(f"\n{len(routes)} route config(s), all valid.")
    return 0


def cmd_status(args) -> int:
    """What is configured and which mailboxes would be read."""
    routes = inbox_config.configured_routes()
    print(f"Inbox configs ({len(routes)}): {', '.join(routes) or 'none'}")

    for route in routes:
        try:
            for matcher in inbox_config.matchers_for(route):
                print(
                    f"    {route:8} {matcher.get('classify', '?'):12} "
                    f"{matcher.get('name', '?')}"
                )
        except MatcherConfigError as e:
            print(f"    {route:8} UNUSABLE: {e}")

    print()
    try:
        from src.inbox import seen as seen_mod
        from src.inbox.watcher import _ago, mailbox_accounts
        from src.waitlist.accounts import mask

        accounts = mailbox_accounts()
        state = seen_mod.load()

        print(f"Mailboxes to watch ({len(accounts)}):")
        for user, _ in accounts:
            last = state.last_pass(user)
            high = state.high_water(user)
            if last:
                detail = f"last read {_ago(last):>9}   (up to uid {high})"
            else:
                detail = "never read"
            print(f"    {mask(user):28} {detail}")
        if not accounts:
            print("    none — check config/registrants/ and [waitlist] account settings")

        # The liveness answer: "is the watcher actually running?" is really
        # "how long since any mailbox was read?". A scheduled `watch --once`
        # leaves no process behind, so a timestamp is the only honest signal.
        reads = [state.last_pass(u) for u, _ in accounts if state.last_pass(u)]
        print()
        if reads:
            print(f"Most recent pass: {_ago(max(reads))}")
        else:
            print("Most recent pass: never — the watcher has not run yet.")
    except Exception as e:
        print(f"Could not resolve mailboxes: {e}")

    print()
    try:
        from src.settings import settings

        otp = settings().otp
        print(f"IMAP host: {otp.imap_host or '(not configured — [otp] imap_host)'}"
              f":{otp.imap_port}")
    except Exception as e:
        print(f"Could not read IMAP settings: {e}")

    return 0


def _load_eml(path: str) -> Email:
    """Reads a saved .eml into the plain Email value the matcher takes."""
    from src.inbox.watcher import _parse_message

    with open(path, "rb") as f:
        raw = f.read()
    return _parse_message(raw, uid="0", mailbox=os.path.basename(path), received=0.0)


def cmd_test(args) -> int:
    """Runs the matchers against saved .eml fixtures. Offline.

    This is how a country's matchers get written: save its real mail into
    tests/fixtures/emails/, run this, and adjust the config until it classifies
    correctly. No mailbox, no network, no risk.
    """
    if args.file:
        paths = [args.file]
    else:
        paths = sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.eml")))

    if not paths:
        print(f"No .eml fixtures found in {FIXTURE_DIR}/.")
        print("Save real VFS emails there (redact PII first) and re-run.")
        return 1

    try:
        matchers_by_route = (
            {args.route.upper(): inbox_config.matchers_for(args.route)}
            if args.route else inbox_config.all_matchers()
        )
    except MatcherConfigError as e:
        print(f"Config error: {e}")
        return 1

    if not matchers_by_route:
        print("No usable inbox configs.")
        return 1

    unmatched = 0
    for path in paths:
        try:
            message = _load_eml(path)
        except OSError as e:
            print(f"  SKIP {os.path.basename(path)}: {e}")
            continue

        found = classify_all(message, matchers_by_route)
        name = os.path.basename(path)
        if found.matched:
            print(f"  {found.classification.upper():13} {name}")
            print(f"                {found.route} · {found.matcher_name}")
            for key, value in sorted((found.fields or {}).items()):
                marker = " " if value else "!"
                print(f"              {marker} {key} = {value!r}")
            if found.validity_hours:
                print(f"                valid for {found.validity_hours}h")
        else:
            unmatched += 1
            print(f"  UNMATCHED     {name}")
            print(f"                subject: {message.subject[:70]}")
            print(f"                from:    {message.sender[:70]}")

    print(f"\n{len(paths)} fixture(s), {unmatched} unmatched.")
    return 1 if unmatched else 0


def cmd_reconcile(args) -> int:
    """Settle uncertain journal rows using VFS's confirmation emails.

    Reads mail and the journal; contacts VFS not at all. DRY RUN BY DEFAULT —
    it writes a 'registered' record, and a wrong one would silently stop a
    client ever being retried, so applying it is an explicit --apply.
    """
    _install_redaction()

    from src.inbox.reconcile import reconcile
    from src.inbox.watcher import run_pass

    print("Reading mailboxes for confirmation emails...")
    result = run_pass()
    confirmations = result.confirmations()
    print(
        f"{result.mailboxes_checked} mailbox(es), "
        f"{len(confirmations)} confirmation email(s) found.\n"
    )

    if result.mailboxes_failed:
        print(f"WARNING: {len(result.mailboxes_failed)} mailbox(es) could not be read:")
        for mailbox in result.mailboxes_failed:
            print(f"    {mailbox}")
        print("  Rows those accounts could settle will stay dangling.\n")

    proposals = reconcile(result.observations, dry_run=not args.apply)
    if not proposals:
        print("Nothing to reconcile — no journal row matched a confirmation email.")
        return 0

    for proposal in proposals:
        print(f"  {proposal.describe()}")

    applicable = [p for p in proposals if p.will_apply]
    print()
    if args.apply:
        print(f"Applied {len(applicable)} change(s) to the journal.")
    else:
        print(f"{len(applicable)} change(s) WOULD be applied. Re-run with --apply.")
    return 0


def cmd_watch(args) -> int:
    """Reads the real mailboxes. Read-only IMAP; triggers nothing."""
    _install_redaction()

    from src.inbox.report import report
    from src.inbox.watcher import _tunables, run_pass, watch

    if args.once:
        result = run_pass()
        report(result)
        return 0 if not result.mailboxes_failed else 1

    interval = args.interval or _tunables()[2]
    print(f"Watching every {interval}s. Ctrl-C to stop.")
    try:
        watch(poll_seconds=interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


# --------------------------------------------------------------------------- #

def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.inbox",
        description="Watch VFS account mailboxes and classify VFS mail. "
                    "Observational: it reports, and triggers nothing.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check", help="validate every config/inbox/*.json (offline)")
    sub.add_parser("status", help="what is configured and which mailboxes")

    test = sub.add_parser("test", help="run matchers against saved .eml fixtures (offline)")
    test.add_argument("--file", help="one .eml file instead of the fixture directory")
    test.add_argument("--route", help="test only this route's matchers")

    watch_cmd = sub.add_parser("watch", help="read the real mailboxes (read-only)")
    watch_cmd.add_argument("--once", action="store_true", help="one pass, then exit")
    watch_cmd.add_argument("--interval", type=int, help="seconds between passes")

    rec = sub.add_parser(
        "reconcile",
        help="settle uncertain journal rows from VFS confirmation emails",
    )
    rec.add_argument(
        "--apply", action="store_true",
        help="actually write the changes (default is a dry run)",
    )

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    commands = {
        "check": cmd_check,
        "status": cmd_status,
        "test": cmd_test,
        "watch": cmd_watch,
        "reconcile": cmd_reconcile,
    }
    if args.command not in commands:
        parser.print_help()
        return 1
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
