"""Operational visibility for the webhook API (Phase 5).

Answers the questions you would otherwise SSH in to answer:

    Is the system armed, or is a switch off somewhere?
    Which routes can actually register right now?
    Is anything stuck needing a human?
    Did any webhook fail to reach the app?

The dangling-entry surface is the important one. A `pending`/`unknown` journal
row means a submit went out and we do not know whether it landed. Those BLOCK
the client from being registered again (correctly — a retry could duplicate a
real appointment), so an unnoticed one means a client silently stops being
served. It must be visible, not buried in a JSONL file.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, status as http_status

from src.api.modules.system.schemas import SwitchState, SwitchesUpdateRequest, SwitchesUpdateResponse
from src.api.modules.waitlist.schemas import DanglingEntry, ResolveRequest, StatusResponse
from src.api.core.security import require_token

log = logging.getLogger("vfs.api.status")


def _switches() -> SwitchState:
    """Every gate between "a waitlist opened" and "a client is registered".

    Reported together because they compose: ALL must be right for an automatic
    live registration, and the usual confusion is one being off.
    """
    from src.settings import settings

    cfg = settings().waitlist
    masters = settings().switches
    return SwitchState(
        waitlist=bool(masters.waitlist),
        invite_booking=bool(masters.invite_booking),
        live_booking=bool(masters.live_booking),
        test_booking=bool(masters.test_booking),
        register_enabled=bool(cfg.register_enabled),
        dry_run=bool(cfg.dry_run),
        auto_trigger_enabled=bool(getattr(cfg, "auto_trigger_enabled", False)),
        auto_trigger_dry_run=bool(getattr(cfg, "auto_trigger_dry_run", True)),
        max_per_run=int(cfg.max_per_run),
        max_per_day=int(cfg.max_per_day),
    )


def _describe_posture(switches: SwitchState) -> str:
    # Only a master that was actually READ as off reports OFF. A SwitchState
    # built without it (older callers describing just the detail switches)
    # has the field defaulted, and that default is not a statement about
    # this machine.
    explicit = "waitlist" in getattr(switches, "model_fields_set", {"waitlist"})
    if getattr(switches, "waitlist", True) is False and explicit:
        return ("OFF — [switches] waitlist is false: the waitlist flow does "
                "nothing, whatever the settings below say.")
    """One plain sentence: would a waitlist opening register anyone right now?"""
    if not switches.register_enabled:
        return ("PARKED — [waitlist] register_enabled is false, so nothing can "
                "register. Detection and notification still work.")
    if not switches.auto_trigger_enabled:
        return ("MANUAL — registration is enabled but the auto-trigger is off, "
                "so runs happen only when you or the API start one.")
    # ONLY auto_trigger_dry_run governs an auto-triggered run. It is not ANDed
    # with dry_run — register.py:474 picks one or the other:
    #
    #     dry_run = guards.dry_run() if force_dry_run is None else force_dry_run
    #
    # and the auto-trigger always passes force_dry_run=auto_trigger_dry_run.
    # So `or dry_run` here was actively dangerous: with dry_run=true and
    # auto_trigger_dry_run=false this reported "AUTO (DRY RUN) — nothing is
    # committed" while the bot was submitting real registrations unattended.
    # A posture line that under-states the risk is worse than none at all.
    if switches.auto_trigger_dry_run:
        return ("AUTO (DRY RUN) — an opening waitlist fires a run that walks the "
                "whole flow but stops before submitting. Nothing is committed.")
    return ("AUTO (LIVE) — an opening waitlist will REGISTER real clients "
            "without a human in the loop.")


def get_status() -> StatusResponse:
    """Everything an operator needs in one call.

    Declared `def`, not `async def`. This handler reads the journal, every
    route config, and the client store — all synchronous file I/O. Inside an
    `async def` that runs ON the event loop and blocks every other request for
    its duration; FastAPI runs a sync handler in a threadpool instead.
    """
    from src.waitlist import journal, store, validate

    switches = _switches()

    # Routes: which can accept registrations, and how many clients wait on each.
    routes: List[Dict[str, Any]] = []
    try:
        from src.utils.config_reader import get_config_section
        route_ids = sorted(get_config_section("vfs-url") or {})
    except Exception:                              # noqa: BLE001
        log.exception("Could not read the route list from the bot config.")
        route_ids = []

    for route_id in route_ids:
        route_id = route_id.upper()
        try:
            readiness = validate.route_readiness(route_id)
            clients = store.list_ids(route=route_id)
            routes.append({
                "route": route_id,
                "ready": readiness.ready,
                "combos": readiness.combos,
                "clients": len(clients),
                "problems": [p.message for p in readiness.problems],
            })
        except Exception as exc:                   # noqa: BLE001
            log.exception("Route %s: readiness check failed.", route_id)
            routes.append({"route": route_id, "ready": False, "combos": [],
                           "clients": 0, "problems": [str(exc)]})

    # Anything this endpoint could not actually determine. THE POINT: every
    # block below used to swallow its exception and fall back to a value that
    # reads as healthy — no dangling entries, zero undelivered webhooks. On the
    # one endpoint whose job is answering "is anything stuck?", a failure that
    # looks identical to "all clear" is the worst possible default. Now a
    # failure is reported as a degradation the caller can see.
    degraded: List[str] = []

    # Dangling entries — the ones that need a human.
    dangling: List[DanglingEntry] = []
    dangling_known = True
    try:
        for row in journal.dangling():
            dangling.append(DanglingEntry(
                route=str(row.get("route", "")),
                combo=str(row.get("combo", "")),
                registrant_id=str(row.get("registrant_id", "")),
                status=str(row.get("status", "")),
                reason=str(row.get("reason") or ""),
                started_at=str(row.get("started_at") or ""),
            ))
    except Exception as exc:                       # noqa: BLE001
        dangling_known = False
        log.exception("Could not read the waitlist journal.")
        degraded.append(
            f"The waitlist journal could not be read ({exc}). Unresolved "
            "registrations CANNOT be listed — treat this as 'unknown', not "
            "'none', and check `python -m src.waitlist journal` directly."
        )

    # Undelivered webhooks.
    try:
        from src.utils import webhook
        deadletters = webhook.deadletter_count()
        webhook_configured = webhook.is_configured()
    except Exception as exc:                       # noqa: BLE001
        deadletters, webhook_configured = 0, False
        log.exception("Could not read webhook delivery state.")
        degraded.append(
            f"Webhook delivery state could not be read ({exc}). The "
            "undelivered count of 0 is a placeholder, not a measurement."
        )

    try:
        clients_total = len(store.list_ids())
    except Exception as exc:                       # noqa: BLE001
        clients_total = 0
        log.exception("Could not count clients.")
        degraded.append(f"The client store could not be listed ({exc}).")

    if not route_ids:
        degraded.append(
            "No routes could be read from the bot config, so route readiness "
            "is unknown rather than empty."
        )

    return StatusResponse(
        posture=_describe_posture(switches),
        switches=switches,
        routes=routes,
        clients_total=clients_total,
        dangling=dangling,
        # A journal we could not read might well hold a dangling entry, so an
        # unreadable journal ALSO demands attention. Reporting False here would
        # be asserting a fact we do not have.
        needs_attention=bool(dangling) or not dangling_known or bool(degraded),
        webhook_configured=webhook_configured,
        undelivered_webhooks=deadletters,
        degraded=degraded,
    )


def get_dangling() -> List[DanglingEntry]:
    """Journal rows needing a human decision.

    Each of these BLOCKS its client from being registered again. That is
    deliberate — retrying a submit whose outcome is unknown risks a duplicate
    appointment — but it means an unresolved row silently parks a client.
    """
    from src.waitlist import journal

    return [
        DanglingEntry(
            route=str(row.get("route", "")),
            combo=str(row.get("combo", "")),
            registrant_id=str(row.get("registrant_id", "")),
            status=str(row.get("status", "")),
            reason=str(row.get("reason") or ""),
            started_at=str(row.get("started_at") or ""),
        )
        for row in journal.dangling()
    ]


def resolve_entry(payload: ResolveRequest) -> Dict[str, Any]:
    """Record what a human found on the VFS portal, unblocking the client.

    This is the API face of `python -m src.waitlist resolve`. Only 'success' or
    'failed' are accepted: the whole point is to replace an ambiguous state with
    a checked fact, so a third ambiguous value would defeat it.

    CHECK THE PORTAL FIRST. Marking a real registration 'failed' lets the bot
    register the same client again — a duplicate appointment.
    """
    from src.waitlist import journal

    try:
        result = journal.resolve(
            route=payload.route, combo=payload.combo,
            registrant_id=payload.registrant_id,
            status=payload.status, reason=payload.reason or "resolved via API",
        )
    except Exception as exc:                       # noqa: BLE001
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"Could not resolve that entry: {exc}",
        ) from exc

    log.info("Resolved %s/%s/%s as %s via API.", payload.route, payload.combo,
             payload.registrant_id, payload.status)
    return {"resolved": True, "entry": result.to_dict()}


# --------------------------------------------------------------------------
# Switches (operational toggles)
# --------------------------------------------------------------------------

# Each switch field -> (INI section, key).
_SWITCH_INI_KEYS = {
    "register_enabled": ("waitlist", "register_enabled"),
    "dry_run": ("waitlist", "dry_run"),
    "auto_trigger_enabled": ("waitlist", "auto_trigger_enabled"),
    "auto_trigger_dry_run": ("waitlist", "auto_trigger_dry_run"),
    "max_per_run": ("waitlist", "max_per_run"),
    "max_per_day": ("waitlist", "max_per_day"),
    "waitlist": ("switches", "waitlist"),
    "invite_booking": ("switches", "invite_booking"),
    "live_booking": ("switches", "live_booking"),
    "test_booking": ("switches", "test_booking"),
}


def set_ini_value(text: str, section: str, key: str, value: str) -> str:
    """Set `key = value` in `[section]` of INI `text`, changing nothing else.

    Text-level on purpose. configparser.write() re-emits the file without a
    single comment, and config.local.ini is mostly comments — the operator's
    notes on why each value is what it is. The old writer erased them on the
    first API toggle. Commented-out keys (`; key = ...`) are left alone; the
    live line is replaced in place, or added at the end of the section.
    """
    lines = text.splitlines()
    header = f"[{section}]".lower()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip().lower() == header), None)
    new_line = f"{key} = {value}"
    if start is None:
        body = text.rstrip("\n")
        return (body + "\n\n" if body else "") + f"[{section}]\n{new_line}\n"

    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip().startswith("[")), len(lines))
    for i in range(start + 1, end):
        stripped = lines[i].strip()
        if stripped.startswith((";", "#")) or "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip().lower() == key.lower():
            lines[i] = new_line
            return "\n".join(lines) + "\n"

    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, new_line)
    return "\n".join(lines) + "\n"


def _write_ini_switches(updates: Dict[str, Any]) -> None:
    """Write switch values to config/config.local.ini, keeping its comments."""
    import os

    ini_path = os.path.join("config", "config.local.ini")
    text = ""
    if os.path.exists(ini_path):
        with open(ini_path, "r", encoding="utf-8") as fh:
            text = fh.read()

    for field_name, value in updates.items():
        target = _SWITCH_INI_KEYS.get(field_name)
        if target is None:
            continue
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        text = set_ini_value(text, target[0], target[1], rendered)

    with open(ini_path, "w", encoding="utf-8") as fh:
        fh.write(text)


def update_switches(payload: SwitchesUpdateRequest) -> SwitchesUpdateResponse:
    """Toggle operational switches (register_enabled, dry_run, etc.).

    Writes to config/config.local.ini and reloads settings so the change
    takes effect immediately. Only the fields you send are changed.
    """
    sent = payload.model_fields_set or set()
    updates = {k: v for k, v in payload.model_dump().items()
               if k in sent and v is not None}

    if not updates:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Empty update — send at least one switch to change.",
        )

    _write_ini_switches(updates)

    # Reload so the in-memory settings reflect the new values.
    from src.utils.config_reader import initialize_config
    initialize_config()
    try:
        from src.settings import reload_settings
        reload_settings()
    except Exception:                              # noqa: BLE001
        pass

    log.info("Switches updated via API: %s", ", ".join(
        f"{k}={v}" for k, v in updates.items()))

    new_switches = _switches()
    return SwitchesUpdateResponse(
        updated=sorted(updates.keys()),
        switches=new_switches,
        message=f"{len(updates)} switch(es) updated.",
    )
