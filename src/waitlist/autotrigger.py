"""Fire the waitlist runner when the slot checker finds an open waitlist.

This is the connection between the two bots. It is deliberately the LAST thing
built and the most heavily gated, because it is the step that lets the system
register real appointments with no human watching.

WHY IT LIVES HERE, NOT IN slot_check.py
---------------------------------------
The tempting hook is the moment `detect.is_offered()` returns True — it has
per-combo granularity. Three independent reasons make that wrong:

  1. WRONG ACCOUNT. The slot checker runs on a rotating slot-check account.
     Waitlist accounts are a separate pool by design (accounts.py:14-18), and a
     waitlist entry BELONGS TO the account that created it. Registering on the
     slot-check account creates an entry the client can never see or cancel.
  2. WRONG EGRESS. A waitlist run resolves its own proxy and a Chrome profile
     keyed to the waitlist account.
  3. REENTRANCY. It would launch a second Chrome from inside a live page
     context, mid-slot-check, colliding on the same debugging port.

So the trigger fires from the supervisor AFTER the route's browser has closed.
A waitlist run is always a fresh login — a correctness requirement, not an
optimisation.

THE LABEL TRAP
--------------
The supervisor's outcome carries labels from `slot_check.result_label()`, which
DELIBERATELY IGNORES the route file's "label" field — and that field is exactly
what clients put in their `combos[]`. For AE-NLD the two diverge completely:

    client combos[]  : "Dubai - Tourist Visa"
    result_label()   : "Netherlands Visa application center- Dubai -
                        Tourist Visa - Tourist Purpose"

A naive string match works for AE-CHE and silently matches NOTHING for AE-NLD —
which looks exactly like "no clients waiting". `resolve_combo_label()` exists to
close that; see tests/test_autotrigger.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class TriggerPlan:
    """What the auto-trigger decided to do for one (route, combo).

    Built even when nothing will run, so the caller can log/report the reason
    rather than silently doing nothing.
    """

    route: str
    combo: str
    clients: List[str] = field(default_factory=list)
    account_groups: Dict[str, List[str]] = field(default_factory=dict)
    skipped_reason: str = ""

    @property
    def will_run(self) -> bool:
        return bool(self.account_groups) and not self.skipped_reason

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route": self.route,
            "combo": self.combo,
            "clients": self.clients,
            "account_groups": {k: v for k, v in self.account_groups.items()},
            "will_run": self.will_run,
            "skipped_reason": self.skipped_reason,
        }


def _normalise(label: str) -> str:
    """Collapse whitespace and case, matching registrant._normalise."""
    return " ".join(str(label or "").split()).strip().lower()


def resolve_combo_label(route: str, result_label: str) -> Optional[str]:
    """Map a supervisor result label back to the label clients use.

    `result_label()` joins centre / category / sub_category and ignores the
    route file's explicit "label". Clients name that "label". This walks the
    route's combination dicts, rebuilds the result-style label for each, and
    returns the client-facing label of whichever one matches.

    Returns None when nothing matches — the caller must treat that as "unknown
    combo", never as "no clients".
    """
    try:
        from src.utils.route_schema import get_route_schema
        from src.vfs_bot.slot_check import combo_label, result_label as build_result_label
    except Exception as exc:                       # noqa: BLE001
        log.warning("Could not load route schema helpers: %s", exc)
        return None

    source, _, dest = str(route).partition("-")
    try:
        schema = get_route_schema(source, dest) or {}
    except Exception as exc:                       # noqa: BLE001
        log.warning("Could not read the route schema for %s: %s", route, exc)
        return None

    wanted = _normalise(result_label)
    for combo in (schema.get("slot_check") or {}).get("combinations") or []:
        if _normalise(build_result_label(combo)) == wanted:
            return combo_label(combo)

    # Fall back to a direct match on the client-facing label: for routes like
    # AE-CHE the two forms are identical, and a route file could omit "label".
    for combo in (schema.get("slot_check") or {}).get("combinations") or []:
        if _normalise(combo_label(combo)) == wanted:
            return combo_label(combo)

    log.warning("Could not map result label %r back to a combo in %s.",
                result_label, route)
    return None


def find_waiting_clients(route: str, combo: str) -> List[Any]:
    """Enabled clients on this (route, combo) with no blocking journal entry.

    Cheap and browser-free — this is the common case (a waitlist opens and
    nobody is queued for it), so it must never cost a browser launch.
    """
    try:
        from src.waitlist import journal, registrant as registrant_mod
    except Exception as exc:                       # noqa: BLE001
        log.warning("Could not load the waitlist roster: %s", exc)
        return []

    try:
        people = registrant_mod.for_route(route, include_disabled=False)
    except Exception as exc:                       # noqa: BLE001
        log.warning("Could not read clients for %s: %s", route, exc)
        return []

    waiting = []
    for person in people:
        if not any(_normalise(c) == _normalise(combo) for c in person.combos):
            continue
        # A committed entry (pending/success/unknown) means this client is
        # already on the waitlist — or has a submit outstanding. Either way,
        # registering again would create a duplicate.
        blocking = journal.blocking_entry(route, combo, person.id)
        if blocking is not None:
            log.info("Client %s already has a %s entry for '%s' — skipping.",
                     person.id, blocking.get("status"), combo)
            continue
        waiting.append(person)
    return waiting


def group_by_account(people: List[Any]) -> Dict[str, List[Any]]:
    """Group clients by the VFS account they resolve to.

    ONE RUN IS ONE LOGIN (runner.py:402): a run whose clients pin different
    accounts is a config error, caught before Chrome starts. So N accounts means
    N separate sequential runs, and the caller must respect that grouping.
    """
    from src.waitlist import accounts

    groups: Dict[str, List[Any]] = {}
    for person in people:
        try:
            resolved = accounts.resolve(person)
        except Exception as exc:                   # noqa: BLE001
            log.warning("Client %s has no resolvable account (%s) — skipping.",
                        person.id, exc)
            continue
        groups.setdefault(resolved.email.lower(), []).append(person)
    return groups


def plan_for(route: str, result_label: str) -> TriggerPlan:
    """Decide what (if anything) to run for one opened waitlist combination.

    Pure decision-making: reads config and the journal, launches nothing.
    """
    combo = resolve_combo_label(route, result_label)
    if not combo:
        return TriggerPlan(route=route, combo=result_label,
                           skipped_reason=f"could not map '{result_label}' to a "
                                          f"combination in config/routes/{route}.json")

    people = find_waiting_clients(route, combo)
    if not people:
        return TriggerPlan(route=route, combo=combo,
                           skipped_reason="no enabled clients waiting on this combination")

    groups = group_by_account(people)
    if not groups:
        return TriggerPlan(route=route, combo=combo,
                           clients=[p.id for p in people],
                           skipped_reason="no client has a resolvable VFS account")

    return TriggerPlan(
        route=route, combo=combo,
        clients=[p.id for p in people],
        account_groups={email: [p.id for p in members]
                        for email, members in groups.items()},
    )


def _cooldown_key(route: str, combo: str) -> str:
    """Debounce key. A waitlist stays open for hours; we check twice an hour."""
    return f"autotrigger:{route}:{_normalise(combo)}"


def _on_cooldown(route: str, combo: str) -> bool:
    """Has this (route, combo) been triggered recently?"""
    try:
        from src.utils import waitlist_cooldown
        return waitlist_cooldown.is_on_cooldown(_cooldown_key(route, combo))
    except Exception:                              # noqa: BLE001
        return False


def _record_trigger(route: str, combo: str) -> None:
    """Start the debounce window for this (route, combo)."""
    try:
        from src.utils import waitlist_cooldown
        waitlist_cooldown.record_sent(_cooldown_key(route, combo))
    except Exception as exc:                       # noqa: BLE001
        log.debug("Could not record the auto-trigger cooldown: %s", exc)


def handle_waitlist_opened(route: str, result_labels: List[str]) -> List[TriggerPlan]:
    """Entry point: the slot checker saw a waitlist on these combinations.

    Called from the supervisor AFTER the route's browser has closed. Returns the
    plan(s) it acted on, for logging and reporting. NEVER raises — a fault here
    must not fail the slot-check run that called it.

    Gating, in order (each one can stop the whole thing):
        1. [waitlist] auto_trigger_enabled   — master switch, default FALSE
        2. per-(route, combo) debounce       — a waitlist stays open for hours
        3. clients actually waiting          — the cheap common case
        4. a resolvable account per client   — one run is one login
    then the runner applies its own gates (register_enabled, caps, guards).
    """
    plans: List[TriggerPlan] = []
    try:
        from src.settings import settings
        cfg = settings().waitlist

        if not getattr(cfg, "auto_trigger_enabled", False):
            log.info("Waitlist opened on %s but auto-trigger is OFF "
                     "([waitlist] auto_trigger_enabled = false).", route)
            return plans

        for label in result_labels or []:
            try:
                plan = _handle_one(route, label)
                if plan is not None:
                    plans.append(plan)
            except Exception as exc:               # noqa: BLE001
                log.exception("Auto-trigger failed for %s / %s: %s",
                              route, label, exc)
    except Exception as exc:                       # noqa: BLE001
        log.exception("Auto-trigger aborted for %s: %s", route, exc)
    return plans


def _handle_one(route: str, result_label: str) -> Optional[TriggerPlan]:
    """Plan and (if warranted) execute for a single opened combination."""
    plan = plan_for(route, result_label)

    if plan.skipped_reason:
        log.info("Auto-trigger for %s / %s: %s",
                 route, plan.combo, plan.skipped_reason)
        return plan

    if _on_cooldown(route, plan.combo):
        plan.skipped_reason = "debounced — already triggered recently"
        log.info("Auto-trigger for %s / %s: %s", route, plan.combo,
                 plan.skipped_reason)
        return plan

    # Tell the web app the window opened, whatever happens next.
    _notify_opened(route, plan)

    _record_trigger(route, plan.combo)
    _run_groups(route, plan)
    return plan


def _notify_opened(route: str, plan: TriggerPlan) -> None:
    """Post the waitlist.opened event. Never raises."""
    try:
        from src.utils import webhook
        if webhook.is_configured():
            webhook.notify_waitlist_opened(
                route=route, combos=[plan.combo],
                clients_waiting=len(plan.clients))
    except Exception as exc:                       # noqa: BLE001
        log.warning("Could not post waitlist.opened (non-fatal): %s", exc)


def _run_groups(route: str, plan: TriggerPlan) -> None:
    """Run the waitlist registration, one account group at a time.

    Sequential on purpose: one run is one login, and the global run lock
    serialises browser work anyway. `force_dry_run` follows the
    [waitlist] auto_trigger_dry_run switch, which defaults to TRUE — the
    auto-trigger walks the whole flow but stops before the committing click
    until you deliberately turn that off.
    """
    from src.settings import settings
    from src.waitlist.runner import SlotsAvailable, run_registration

    source, _, dest = route.partition("-")
    dry_run = getattr(settings().waitlist, "auto_trigger_dry_run", True)

    for email, client_ids in plan.account_groups.items():
        log.info("Auto-trigger: %s / %s — %d client(s) on account %s (%s).",
                 route, plan.combo, len(client_ids), email,
                 "DRY RUN" if dry_run else "LIVE")
        for client_id in client_ids:
            try:
                run_registration(
                    source=source, dest=dest,
                    registrant_id=client_id,
                    only_combo=plan.combo,
                    force_dry_run=dry_run,
                )
            except SlotsAvailable as exc:
                # A bookable slot beats a waitlist. The run stopped on purpose;
                # tell the app to book, and stop trying to waitlist this combo.
                log.warning("Auto-trigger stopped: slots available for '%s'.",
                            exc.combo)
                _notify_slots(route, exc)
                return
            except Exception as exc:               # noqa: BLE001
                # One client's failure must not stop the others. The runner has
                # already journalled and reported this client's outcome.
                log.error("Auto-trigger: client %s failed on %s / %s: %s",
                          client_id, route, plan.combo, exc)


def _notify_slots(route: str, exc: Any) -> None:
    """Post the slots.available event. Never raises."""
    try:
        from src.utils import webhook
        if webhook.is_configured():
            webhook.notify_slots_available(
                route=route, combo=getattr(exc, "combo", ""),
                banner=getattr(exc, "banner", ""))
    except Exception as error:                     # noqa: BLE001
        log.warning("Could not post slots.available (non-fatal): %s", error)
