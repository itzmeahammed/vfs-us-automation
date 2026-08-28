"""Pre-flight gates. Every one must pass before anything is submitted.

The gates, in the order they run (cheapest and most decisive first):

  1. master kill switch      [waitlist] register_enabled — default OFF
  2. route config enabled    config/waitlist/<ROUTE>.json "enabled"
  3. client enabled          the client file's "enabled"
  4. client wants this combo the combo is listed in the client's "combos"
  5. dangling journal entry  a previous attempt needs a human first
  6. already registered      dedup on (route, combo, client)
 6b. one entry per route     a client already waitlisted on this route is done
  7. per-run cap             max_per_run
  8. per-day cap             max_per_day

Gates 1-4 are INDEPENDENT opt-ins: registration needs the master switch on, the
route configured, the client enabled, AND that exact combination listed in their
file. Creating a client file is therefore the act that arms registration for that
person — which is why the master switch and dry_run sit above it.

Gate 6b is why a client's "combos" list is a PREFERENCE ORDER rather than a list
to register for all of: one person wants one appointment, so the first committed
entry on a route ends their run for that route.

Every gate returns a reason string rather than raising, so the caller can report
precisely why nothing happened. `check()` bundles them.
"""

import logging
from datetime import date
from typing import Optional

from src.settings import settings
from src.waitlist import config as waitlist_config
from src.waitlist import journal
from src.waitlist.result import Status


class GuardVerdict:
    """The outcome of the gate battery: allowed, or blocked with a reason."""

    def __init__(self, allowed: bool, reason: str = "", needs_human: bool = False):
        self.allowed = allowed
        self.reason = reason
        self.needs_human = needs_human

    def __bool__(self) -> bool:
        return self.allowed

    def __repr__(self) -> str:
        return f"<GuardVerdict {'ALLOW' if self.allowed else 'BLOCK'} {self.reason}>"


ALLOW = GuardVerdict(True)


def registration_enabled() -> bool:
    """The master kill switch, [waitlist] register_enabled. Defaults to False."""
    return bool(settings().waitlist.register_enabled)


def dry_run() -> bool:
    """Whether to walk the flow but stop before the committing submit."""
    return bool(settings().waitlist.dry_run)


def _today_iso() -> str:
    return date.today().isoformat()


def check(route: str, combo: str, registrant,
          attempted_this_run: int = 0) -> GuardVerdict:
    """
    Runs every gate. Returns ALLOW, or a GuardVerdict carrying the block reason.

    `attempted_this_run` is the count of registrations already COMMITTED in this
    process, for the per-run cap (the journal cannot supply it — a dry run or a
    crash would skew it).
    """
    cfg = settings().waitlist

    # 1 — master switch.
    if not cfg.register_enabled:
        return GuardVerdict(
            False,
            "waitlist registration is switched off "
            "([waitlist] register_enabled = false)",
        )

    # 2 — the route has a waitlist config and it is not disabled.
    if not waitlist_config.is_enabled(route):
        return GuardVerdict(
            False,
            f"no enabled waitlist config for {route} "
            f"(expected config/waitlist/{route}.json)",
        )

    # 3 — the client is not parked.
    if not registrant.enabled:
        return GuardVerdict(
            False,
            f"client '{registrant.id}' is disabled "
            f"(\"enabled\": false in their file)",
        )

    # 4 — the client is actually waiting on THIS combination.
    if not registrant.wants(combo):
        return GuardVerdict(
            False,
            f"'{combo}' is not in {registrant.id}'s \"combos\" list",
        )

    # 5/6 — a previous attempt blocks this triple.
    blocking = journal.blocking_entry(route, combo, registrant.id)
    if blocking:
        status = blocking.get("status")
        if status in Status.NEEDS_ATTENTION:
            return GuardVerdict(
                False,
                f"a previous attempt on {blocking.get('started_at')} is "
                f"unresolved (status '{status}'). Check this account on the VFS "
                f"portal, then run: python -m src.waitlist resolve "
                f"--route {route} --combo \"{combo}\" "
                f"--registrant {registrant.id} --status success|failed",
                needs_human=True,
            )
        if status == Status.SUCCESS:
            when = blocking.get("finished_at") or blocking.get("started_at")
            reference = blocking.get("vfs_reference")
            return GuardVerdict(
                False,
                f"already registered on {when}"
                + (f" (ref {reference})" if reference else ""),
            )

    # 6b — ONE REGISTRATION PER CLIENT, PER ROUTE.
    #
    # Gate 5/6 above only blocks the exact (route, combo, client) triple, so a
    # client listing two combos on one route would register for BOTH. That is
    # never right: one person wants ONE appointment. Two entries hold two slots
    # for one need, deny one to somebody else, and risk VFS voiding both as
    # duplicates.
    #
    # So `combos` is a PREFERENCE ORDER — "I'll take whichever opens first" —
    # and the first committed entry ends this client's run on this route, in
    # this run and in every future one, until it is resolved or cancelled.
    #
    # Note this deliberately fires AFTER the checks above, so a client blocked
    # by their own dangling entry still gets that more specific message.
    held = journal.blocking_entry_for_route(route, registrant.id)
    if held and journal._normalise(held.get("combo")) != journal._normalise(combo):
        when = held.get("finished_at") or held.get("started_at")
        reference = held.get("vfs_reference")
        return GuardVerdict(
            False,
            f"{registrant.id} already holds a {route} waitlist entry for "
            f"'{held.get('combo')}'"
            + (f" (ref {reference})" if reference else "")
            + f" from {when}. One client gets ONE appointment per route — the "
            "\"combos\" list is a preference order, not a list to register for "
            "all of. Cancel that entry on the portal and resolve it "
            f"(python -m src.waitlist resolve --route {route} "
            f"--combo \"{held.get('combo')}\" --registrant {registrant.id} "
            "--status failed) to free this client for a different combination.",
        )

    # 7 — per-run cap.
    if attempted_this_run >= cfg.max_per_run:
        return GuardVerdict(
            False,
            f"per-run cap reached ({cfg.max_per_run}); "
            "raise [waitlist] max_per_run to allow more",
        )

    # 8 — per-day cap.
    today_count = journal.count_since(_today_iso())
    if today_count >= cfg.max_per_day:
        return GuardVerdict(
            False,
            f"per-day cap reached ({today_count}/{cfg.max_per_day} today); "
            "raise [waitlist] max_per_day to allow more",
        )

    return ALLOW


def startup_check() -> Optional[str]:
    """
    Called once before any browser work. Returns a warning string if the journal
    holds unresolved entries, else None.

    Registration is NOT globally halted by a dangling entry — gate 5 already
    blocks the specific triple involved, and one stuck client should not stop a
    different one. This surfaces it loudly instead.
    """
    stuck = journal.dangling()
    if not stuck:
        return None
    lines = [
        f"{len(stuck)} unresolved waitlist registration(s) need a human — those "
        "route/combo/client triples are BLOCKED until resolved:"
    ]
    for row in stuck:
        lines.append(
            f"  · {row.get('route')} / {row.get('combo')} / "
            f"{row.get('registrant_id')} — {row.get('status')} "
            f"at {row.get('started_at')}"
        )
    lines.append("Check the VFS account, then: python -m src.waitlist resolve ...")
    message = "\n".join(lines)
    logging.warning(message)
    return message


def describe() -> str:
    """A one-line summary of the current gate settings, for the CLI banner."""
    cfg = settings().waitlist
    mode = "DRY RUN (nothing submitted)" if cfg.dry_run else "LIVE (will submit)"
    state = "ENABLED" if cfg.register_enabled else "DISABLED"
    return (f"Waitlist registration: {state} · {mode} · "
            f"caps {cfg.max_per_run}/run, {cfg.max_per_day}/day")
