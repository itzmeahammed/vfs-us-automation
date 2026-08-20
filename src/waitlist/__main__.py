"""CLI for the waitlist feature — this bot runs when YOU need it.

    python -m src.waitlist status
        Every client, grouped by route, plus the gate settings and any
        unresolved registrations. Touches no browser.

    python -m src.waitlist check --route AE-CHE
        Validates the route config, then every client on that route: are their
        combos real, does every {{placeholder}} resolve? No browser. Run it
        after every JSON edit — it catches typos in a second.

    python -m src.waitlist doctor --route AE-CHE
        Does the config still match VFS's page? Logs in and probes every
        configured selector WITHOUT typing, ticking or submitting. Run it before
        a real registration — VFS reskins without warning, and this turns a
        cryptic mid-run timeout into "review_pay: 'I accept the' matched 3
        elements". Add --walk to also reach the later pages when mapping a new
        country.

    python -m src.waitlist run --route AE-CHE
        The real thing: launches Chrome, logs in, reaches Appointment Details,
        and registers every enabled client waiting on that route. Honours
        [waitlist] dry_run unless --live is passed.

    python -m src.waitlist run --registrant ahmed
        Just one client — their file names its own route, so --route is
        optional. Add --combo to narrow to one of their combinations.

    python -m src.waitlist journal [--all]
        Registration history; by default only entries needing attention.

    python -m src.waitlist resolve --route AE-CHE --combo "..." \\
            --registrant ahmed --status success|failed
        Record what you found on the VFS account, unblocking a stuck triple.

Nothing here runs on a schedule and nothing is wired into the hourly supervisor —
registration only ever happens because you typed a command.
"""

import argparse
import logging
import sys

from src.main import initialize_logger
from src.settings import settings
from src.utils.config_reader import initialize_config
from src.waitlist import config as waitlist_config
from src.waitlist import context as ctx
from src.waitlist import (
    accounts,
    guards,
    journal,
    redaction,
    registrant as registrant_mod,
)
from src.waitlist.errors import WaitlistConfigError
from src.waitlist.result import Status


# --------------------------------------------------------------------------- #
# status                                                                       #
# --------------------------------------------------------------------------- #

def cmd_status(args) -> int:
    print(guards.describe())
    print()

    routes = waitlist_config.configured_routes()
    print(f"Route configs (config/waitlist/): {', '.join(routes) or 'none'}")
    print()

    # One bad file must not hide the whole roster, so invalid ones are reported
    # and skipped rather than aborting the listing.
    people = registrant_mod.load_all(skip_invalid=True)
    if not people:
        print("Clients (config/registrants/): none")
        print("  Add one: copy example.json.example to <name>.json and edit.")
    else:
        print(f"Clients (config/registrants/): {len(people)}")
        by_route = {}
        for person in people:
            by_route.setdefault(person.route, []).append(person)
        for route in sorted(by_route):
            has_config = "" if route in routes else "   ⚠️ no config/waitlist file"
            print(f"  {route}{has_config}")
            for person in by_route[route]:
                state = "" if person.enabled else "  [DISABLED]"
                print(f"    {person.id}{state}")
                # The account matters: a waitlist entry belongs to the account
                # that created it, so show which one each client resolves to.
                try:
                    resolved = accounts.resolve(person)
                    print(f"        account: {resolved.masked} "
                          f"({resolved.source})")
                except WaitlistConfigError:
                    print("        account: ⚠️ none configured — a run would stop")
                for combo in person.combos:
                    print(f"        · {combo}")

    stuck = journal.dangling()
    print()
    if stuck:
        print(f"⚠️  {len(stuck)} registration(s) NEED ATTENTION:")
        for row in stuck:
            print(f"    {row.get('route')} / {row.get('combo')} / "
                  f"{row.get('registrant_id')} — {row.get('status')} "
                  f"at {row.get('started_at')}")
        print("    Verify on the VFS account, then use: "
              "python -m src.waitlist resolve ...")
    else:
        print("No unresolved registrations.")
    return 0


# --------------------------------------------------------------------------- #
# check (no browser)                                                           #
# --------------------------------------------------------------------------- #

def _check_route(route: str):
    """Prints the route config summary. Returns the config, or None on error."""
    try:
        cfg = waitlist_config.get(route)
    except WaitlistConfigError as e:
        print(f"✗ Route config: {e}")
        return None

    steps = cfg["steps"]
    print(f"✓ Route config for {route}: {len(steps)} step(s); "
          f"commit step = '{waitlist_config.commit_step_name(route)}'")
    for step in steps:
        bits = [f"{len(step.get('fields') or [])} field(s)"]
        if step.get("settle_seconds"):
            bits.append(f"settle {step['settle_seconds']}s before filling")
        if step.get("dwell_seconds"):
            bits.append(f"dwell {step['dwell_seconds']}s before submit")
        marker = "   ← COMMITS (point of no return)" if step.get("commits") else ""
        print(f"    {step['name']}: {', '.join(bits)}{marker}")
    return cfg


def _check_client(cfg, route: str, person) -> int:
    """Validates one client's data against the route config. Returns problems."""
    from src.waitlist.register import _all_templates

    state = "" if person.enabled else "  [DISABLED — runs will skip them]"
    print(f"\n✓ Client '{person.id}': {len(person.keys())} field(s), "
          f"{len(person.combos)} combo(s){state}")

    # Resolve the login now — a missing account stops a run, so it belongs in
    # the pre-flight check rather than being discovered after Chrome starts.
    try:
        resolved = accounts.resolve(person)
        print(f"    ✓ account {resolved.masked} (from {resolved.source})")
    except WaitlistConfigError as e:
        print(f"    ✗ {e}")
        return 1

    # Every combo must exist in the route's slot-check definitions, or the run
    # cannot select its dropdowns.
    from src.utils.route_schema import get_route_schema
    from src.vfs_bot.slot_check import combo_label

    source, _, dest = route.partition("-")
    known = [combo_label(c) for c in
             get_route_schema(source, dest).get("slot_check", {}).get("combinations", [])]
    known_lower = {k.strip().lower() for k in known}

    problems = 0
    for combo in person.combos:
        if combo.strip().lower() in known_lower:
            print(f"    ✓ {combo}")
        else:
            problems += 1
            print(f"    ✗ {combo}  — not in config/routes/{route}.json")
            print(f"        available: {'; '.join(known) or 'none'}")

    context = ctx.build(person, route=route,
                        combo=person.combos[0] if person.combos else "")
    unresolved = ctx.validate(_all_templates(cfg), context)
    if unresolved:
        problems += len(unresolved)
        print(f"    ✗ {len(unresolved)} unresolved placeholder(s):")
        for problem in unresolved:
            print(f"        {problem}")
    else:
        print("    ✓ every {{placeholder}} resolves")
    return problems


def cmd_check(args) -> int:
    """Validates config + client data without launching a browser."""
    if args.registrant and not args.route:
        person = registrant_mod.load(args.registrant)
        route = person.route
        print(f"Client '{person.id}' targets {route}.\n")
        cfg = _check_route(route)
        return 1 if cfg is None or _check_client(cfg, route, person) else 0

    if not args.route:
        print("Pass --route (e.g. AE-CHE) or --registrant (e.g. ahmed).")
        return 2

    route = args.route.upper()
    cfg = _check_route(route)
    if cfg is None:
        return 1

    if args.registrant:
        person = registrant_mod.load(args.registrant)
        if person.route != route:
            print(f"\n✗ Client '{person.id}' targets {person.route}, not {route}.")
            return 1
        return 1 if _check_client(cfg, route, person) else 0

    people = registrant_mod.for_route(route, include_disabled=True)
    if not people:
        print(f"\n⚠️  No client files target {route} — a run would have nothing "
              f"to do.\n    Create config/registrants/<name>.json with "
              f"\"route\": \"{route}\".")
        return 0

    problems = sum(_check_client(cfg, route, p) for p in people)
    print()
    if problems:
        print(f"✗ {problems} problem(s) found — fix these before running.")
        return 1
    print(f"✓ {len(people)} client(s) ready for {route}.")
    return 0


# --------------------------------------------------------------------------- #
# run (launches a browser)                                                     #
# --------------------------------------------------------------------------- #

# Markers around the machine-readable block. The run's own logging writes to
# the same stream, so the API cannot just json.loads() the whole output — it
# slices between these instead. Deliberately unlikely to appear in a log line.
RESULT_JSON_BEGIN = "---VFS-RESULT-JSON-BEGIN---"
RESULT_JSON_END = "---VFS-RESULT-JSON-END---"


def _emit_result_json(outcome: str, results, combo: str = "",
                      banner: str = "") -> None:
    """Print the run's outcome as JSON between markers, for the webhook API.

    `outcome` is the RUN-level verdict; per-client detail lives in `results`:

        completed        the plan ran to the end (individual results may still
                         be failed/skipped — check them, not just this)
        slots_available  a bookable slot appeared, so the run stopped and
                         nothing was registered. A better outcome, not an error.

    Scrubbed through redaction before printing: results carry client ids and
    reasons, and this block is written to a job log the API reads back.
    """
    import json

    from src.waitlist import redaction

    payload = {
        "outcome": outcome,
        "results": [r.to_dict() for r in (results or [])],
    }
    if combo:
        payload["combo"] = combo
    if banner:
        payload["banner"] = banner

    text = json.dumps(payload, ensure_ascii=False, default=str)
    print(RESULT_JSON_BEGIN)
    print(redaction.scrub(text))
    print(RESULT_JSON_END)


def cmd_run(args) -> int:
    from src.waitlist.runner import SlotsAvailable, run_registration

    # --registrant alone is enough: the client file names its own route.
    if args.route:
        route = args.route.upper()
    elif args.registrant:
        route = registrant_mod.load(args.registrant).route
        print(f"Client '{args.registrant}' targets {route}.")
    else:
        print("Pass --route (e.g. AE-CHE) or --registrant (e.g. ahmed).")
        return 2

    source, _, dest = route.partition("-")
    if not source or not dest:
        print(f"✗ Route must look like 'AE-CHE'; got '{route}'.")
        return 1

    # Clear anything a previous crashed run left on disk BEFORE doing more work.
    _sweep_documents()

    warning = guards.startup_check()
    if warning:
        print(warning)
        print()

    force_dry_run = None
    if args.live:
        force_dry_run = False
    elif args.dry_run:
        force_dry_run = True

    if force_dry_run is False:
        print("*** LIVE MODE — this WILL submit a real waitlist registration. ***")
        if not args.yes:
            reply = input("Type 'yes' to continue: ").strip().lower()
            if reply != "yes":
                print("Aborted.")
                return 1

    try:
        results = run_registration(
            source=source, dest=dest,
            registrant_id=args.registrant,
            only_combo=args.combo,
            force_dry_run=force_dry_run,
            email=args.email, password=args.password,
            proxy=args.proxy_url, keep_open=args.keep_open,
        )
    except SlotsAvailable as e:
        # Not a failure — the opposite. A bookable slot exists, so waitlisting
        # would be the wrong action and the run stopped deliberately.
        print()
        print("=" * 62)
        print("  SLOTS AVAILABLE — run stopped, nothing was registered.")
        print("=" * 62)
        print(f"  Combination: {e.combo}")
        for line in e.banner.splitlines():
            print(f"  {line}")
        print()
        print("  A bookable slot exists, so a waitlist sign-up is not the right")
        print("  action. Go and book it on the portal.")
        if getattr(args, "json_output", False):
            _emit_result_json(outcome="slots_available", results=[],
                              combo=e.combo, banner=e.banner)
        return 2

    print()
    for result in results:
        print(result.summary())
    if getattr(args, "json_output", False):
        _emit_result_json(outcome="completed", results=results)
    return 0 if all(r.status != Status.FAILED for r in results) else 1


# --------------------------------------------------------------------------- #
# journal / resolve                                                            #
# --------------------------------------------------------------------------- #

_SCAFFOLD = """{{
  "_comment": "UAE -> {dest} waitlist registration. Inherits the shared flow from _default.json; only what this portal does DIFFERENTLY belongs here.",
  "_comment_todo": "SCAFFOLD — not usable yet. Fill in the two field lists below, then: python -m src.waitlist doctor --route {route} --walk",

  "extends": "_default",
  "enabled": false,
  "_comment_enabled": "Flip to true only once `doctor --walk` reports every selector resolving.",

  "steps": [
    {{
      "name": "your_details",
      "_comment": "Address fields by their VISIBLE LABEL — this portal family emits no formcontrolname and only positional ids that shift. Mark any field VFS renders inconsistently \\"if_present\\": true. Copy the shape from config/waitlist/AE-CHE.json.",

      "_comment_dwell": "Set to just over this portal's countdown ('Please wait N seconds before saving'). Omit if it has none.",
      "dwell_seconds": 32,

      "fields": [
        {{
          "name": "first_name",
          "label": "First Name",
          "widget": "text",
          "value": "{{{{first_name|upper}}}}",
          "required": true
        }}
      ]
    }},

    {{
      "name": "review_pay",
      "_comment": "The consent checkboxes. Match each by its OWN wording; add \\"index\\" only when two boxes genuinely share text.",
      "fields": [
        {{
          "name": "agree_waitlist",
          "label": "TODO: the waitlist agreement wording on this portal",
          "widget": "checkbox",
          "value": true,
          "required": true
        }}
      ]
    }}
  ],

  "confirmation": {{
    "_comment": "Override with THIS portal's confirmation headline. Avoid a bare 'waitlist' — that also appears in disclaimers.",
    "success_text": ["waitlisted"]
  }}
}}
"""


def cmd_add_route(args) -> int:
    """Scaffolds a waitlist config for a new route, extending _default."""
    import os

    route = args.route.upper()
    source, _, dest = route.partition("-")
    if not source or not dest:
        print(f"✗ Route must look like 'AE-ITA'; got '{args.route}'.")
        return 1

    # The combos come from the slot-check config, so a route the checker does
    # not know about cannot be waitlisted either.
    from src.utils.route_schema import get_route_schema
    from src.vfs_bot.slot_check import combo_label

    schema = get_route_schema(source, dest)
    combos = [combo_label(c) for c
              in schema.get("slot_check", {}).get("combinations", [])
              if not c.get("disabled")]
    if not combos:
        print(f"✗ No enabled combinations in config/routes/{route}.json — add "
              "them there first (the waitlist flow selects them by label).")
        return 1

    path = os.path.join(waitlist_config.WAITLIST_DIR, f"{route}.json")
    if os.path.exists(path) and not args.force:
        # Still print the labels: this is the command that answers "what do I
        # put in a client's combos?", and that question outlives the scaffold.
        print(f"{path} already exists — not overwriting (pass --force to).")
        print()
        print("Combinations on this route (a client's \"combos\" must use these "
              "labels exactly):")
        for combo in combos:
            print(f"    {combo}")
        return 0

    os.makedirs(waitlist_config.WAITLIST_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_SCAFFOLD.format(route=route, dest=dest))

    print(f"✓ Created {path}")
    print()
    print("Combinations available on this route (a client's \"combos\" must use "
          "these labels exactly):")
    for combo in combos:
        print(f"    {combo}")
    print()
    print("Next:")
    print(f"  1. Open the portal by hand and note every field on 'Your Details'")
    print(f"     and every consent on 'Review & Pay'.")
    print(f"  2. Fill them into {path}.")
    print(f"  3. python -m src.waitlist check  --route {route}")
    print(f"  4. python -m src.waitlist doctor --route {route} --walk")
    print(f"  5. Set \"enabled\": true, then a dry run, then --live.")
    return 0


def cmd_doctor(args) -> int:
    """Checks the route's configured selectors against the live portal."""
    from src.waitlist import doctor
    from src.waitlist.runner import run_doctor

    if args.route:
        route = args.route.upper()
    elif args.registrant:
        route = registrant_mod.load(args.registrant).route
    else:
        print("Pass --route (e.g. AE-CHE) or --registrant.")
        return 2

    source, _, dest = route.partition("-")
    if not source or not dest:
        print(f"✗ Route must look like 'AE-CHE'; got '{route}'.")
        return 1

    if args.walk:
        print("*** --walk ticks the waitlist checkbox and submits to reach the")
        print("*** later pages. NO registration is created (it stops before the")
        print("*** committing step), but the booking form DOES advance.")
        if not args.yes:
            if input("Type 'yes' to continue: ").strip().lower() != "yes":
                print("Aborted.")
                return 1

    findings = run_doctor(
        source=source, dest=dest, combo=args.combo,
        registrant_id=args.registrant, walk=args.walk,
        email=args.email, password=args.password,
        proxy=args.proxy_url, keep_open=args.keep_open,
    )
    print()
    print(doctor.report(findings, route))
    return 0 if all(f.ok for f in findings) else 1


def cmd_documents(args) -> int:
    """Manages client identity documents (passport bio pages)."""
    from src.waitlist import documents

    action = args.action

    if action == "list":
        items = documents.inventory()
        print(f"Document store: {documents.root()}")
        if not items:
            print("  (empty)")
            return 0
        print()
        retention = settings().waitlist.document_retention_days
        for item in items:
            stale = ("  ← STALE, next sweep removes it"
                     if retention and item["age_days"] > retention else "")
            print(f"  {item['registrant_id']:14} {item['kind']:14} "
                  f"{item['size_kb']:6.0f} KB  {item['age_days']:5.1f}d old"
                  f"{stale}")
        print()
        print(f"{len(items)} document(s). Deleted automatically once a "
              "registration is confirmed;")
        print(f"anything older than {retention} day(s) is swept on the next run.")
        return 0

    if action == "add":
        if not args.registrant or not args.file:
            print("✗ add needs --registrant and --file.")
            return 2
        path = documents.store(args.registrant, args.file)
        print(f"✓ Stored: {path}")
        print()
        print("Point the client file at it with:")
        print(f'    "passport_scan": "managed"')
        return 0

    if action == "remove":
        if not args.registrant:
            print("✗ remove needs --registrant.")
            return 2
        removed = documents.delete_for(args.registrant, reason="removed manually")
        print(f"✓ Removed {removed} document(s) for '{args.registrant}'."
              if removed else
              f"No documents held for '{args.registrant}'.")
        return 0

    if action == "purge":
        days = args.older_than if args.older_than is not None \
            else settings().waitlist.document_retention_days
        if not days:
            print("Retention is disabled ([waitlist] document_retention_days = 0).")
            return 0
        removed = documents.purge_older_than(days, dry_run=args.dry_run)
        if not removed:
            print(f"Nothing older than {days} day(s).")
            return 0
        verb = "Would delete" if args.dry_run else "Deleted"
        print(f"{verb} {len(removed)} document(s) older than {days} day(s):")
        for path in removed:
            print(f"    {path}")
        return 0

    print(f"✗ Unknown action '{action}'.")
    return 2


def _sweep_documents() -> None:
    """Retention sweep, run before any command that touches a browser.

    Deliberately automatic rather than something to remember: a crashed or
    abandoned run must not be able to leave a passport scan on disk
    indefinitely, and a retention rule nobody runs is not a retention rule.
    """
    try:
        from src.waitlist import documents

        days = settings().waitlist.document_retention_days
        if days:
            documents.purge_older_than(days)
    except Exception as e:
        logging.debug(f"Retention sweep skipped: {e}")


def cmd_journal(args) -> int:
    rows = journal.entries() if args.all else journal.dangling()
    if not rows:
        print("No entries." if args.all else "Nothing needs attention.")
        return 0
    for row in rows:
        reference = f" ref={row.get('vfs_reference')}" if row.get("vfs_reference") else ""
        print(f"{row.get('started_at')}  [{str(row.get('status')).upper():8}] "
              f"{row.get('route')} / {row.get('combo')} / "
              f"{row.get('registrant_id')}{reference}")
        if row.get("reason"):
            print(f"    {row['reason']}")
    return 0


def cmd_resolve(args) -> int:
    result = journal.resolve(
        args.route.upper(), args.combo, args.registrant,
        status=args.status, reason=args.reason or "",
    )
    print(f"Recorded: {result.summary()}")
    return 0


# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m src.waitlist",
        description="VFS waitlist registration — run on demand.",
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Detailed step-by-step (DEBUG) logs.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show what is configured and enabled.").set_defaults(
        func=cmd_status)

    p_check = sub.add_parser("check", help="Validate config + client data (no browser).")
    p_check.add_argument("--route", help="e.g. AE-CHE — checks every client on it.")
    p_check.add_argument("--registrant", help="Check just this client (their file "
                                              "names its own route).")
    p_check.set_defaults(func=cmd_check)

    p_run = sub.add_parser("run", help="Launch a browser and register.")
    p_run.add_argument("--route", help="e.g. AE-CHE — runs every enabled client "
                                       "targeting it.")
    p_run.add_argument("--registrant", help="Run just this client (their file "
                                            "names its own route).")
    p_run.add_argument("--combo", help="Only this combination (default: every "
                                       "combo the client listed).")
    mode = p_run.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="Walk the flow, stop before submitting (default).")
    mode.add_argument("--live", action="store_true",
                      help="ACTUALLY SUBMIT. Prompts for confirmation.")
    p_run.add_argument("--yes", action="store_true",
                       help="Skip the --live confirmation prompt.")
    p_run.add_argument("--email", help="Force a specific VFS account.")
    p_run.add_argument("--password", help="Password for --email.")
    p_run.add_argument("--proxy-url", dest="proxy_url",
                       help="Force a proxy URL ('' for local IP).")
    p_run.add_argument("--keep-open", action="store_true",
                       help="Leave the browser open at the end for inspection.")
    p_run.add_argument("--json", action="store_true", dest="json_output",
                       help="Also print a machine-readable result block between "
                            "VFS-RESULT-JSON markers. Used by the webhook API to "
                            "report per-client outcomes; harmless interactively.")
    p_run.set_defaults(func=cmd_run)

    p_add = sub.add_parser(
        "add-route", help="Scaffold a waitlist config for a new route.")
    p_add.add_argument("--route", required=True, help="e.g. AE-ITA")
    p_add.add_argument("--force", action="store_true",
                       help="Overwrite an existing config for this route.")
    p_add.set_defaults(func=cmd_add_route)

    p_doctor = sub.add_parser(
        "doctor", help="Check this route's selectors against the LIVE page.")
    p_doctor.add_argument("--route", help="e.g. AE-CHE")
    p_doctor.add_argument("--registrant", help="Use this client's route/account.")
    p_doctor.add_argument("--combo", help="Which combination to look at.")
    p_doctor.add_argument("--walk", action="store_true",
                          help="Also tick the checkbox and submit to reach the "
                               "later pages. Advances the form; creates NO "
                               "registration. Use when mapping a new country.")
    p_doctor.add_argument("--yes", action="store_true",
                          help="Skip the --walk confirmation prompt.")
    p_doctor.add_argument("--email", help="Force a specific account.")
    p_doctor.add_argument("--password", help="Password for --email.")
    p_doctor.add_argument("--proxy-url", dest="proxy_url",
                          help="Force a proxy URL ('' for local IP).")
    p_doctor.add_argument("--keep-open", action="store_true",
                          help="Leave the browser open to inspect the page.")
    p_doctor.set_defaults(func=cmd_doctor)

    p_docs = sub.add_parser(
        "documents", help="Manage client passport scans (list/add/remove/purge).")
    p_docs.add_argument("action", choices=["list", "add", "remove", "purge"])
    p_docs.add_argument("--registrant", help="Client id (add/remove).")
    p_docs.add_argument("--file", help="Path to the document (add).")
    p_docs.add_argument("--older-than", type=float, dest="older_than",
                        help="Purge threshold in days (default: "
                             "[waitlist] document_retention_days).")
    p_docs.add_argument("--dry-run", action="store_true",
                        help="purge: show what would go, delete nothing.")
    p_docs.set_defaults(func=cmd_documents)

    p_journal = sub.add_parser("journal", help="Registration history.")
    p_journal.add_argument("--all", action="store_true",
                           help="Every entry (default: only those needing attention).")
    p_journal.set_defaults(func=cmd_journal)

    p_resolve = sub.add_parser(
        "resolve", help="Record what you found on the VFS account.")
    p_resolve.add_argument("--route", required=True)
    p_resolve.add_argument("--combo", required=True)
    p_resolve.add_argument("--registrant", required=True)
    p_resolve.add_argument("--status", required=True,
                           choices=[Status.SUCCESS, Status.FAILED],
                           help="'success' = it did register; 'failed' = it did not.")
    p_resolve.add_argument("--reason", help="Free-text note.")
    p_resolve.set_defaults(func=cmd_resolve)

    args = parser.parse_args()

    initialize_config()
    if args.verbose:
        import os
        os.environ["LOG_LEVEL"] = "DEBUG"
    initialize_logger()
    # Install the PII scrubber BEFORE any client file is loaded — registrant.load
    # registers each client's values, but the filter has to be on the handlers
    # first for those registrations to have anywhere to take effect.
    redaction.install()

    try:
        sys.exit(args.func(args))
    except WaitlistConfigError as e:
        logging.error(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
