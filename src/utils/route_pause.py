"""Operator pauses: turn ONE flow off for ONE country, without editing config.

    [switches] waitlist / live_booking     the whole machine, every country
    config/<flow>/<ROUTE>.json "enabled"   "this page mapping is trusted" — an
                                           engineering judgement, kept in git
    a pause (this module)                  "something is off on AE-CHE today"

A pause can only take a flow AWAY. Lifting it restores whatever the config
says; it cannot switch on a country whose mapping is not enabled, which is why
it is safe to hand to an API caller.

The state is one small JSON file under state/, read fresh on every question:
the API writes it and the slot-check and booking processes read it, so a cache
in any of them would be a pause that has not happened yet.

    {"AE-CHE": {"waitlist": {"reason": "...", "by": "...", "at": "..."}}}
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.utils import state_paths

log = logging.getLogger(__name__)

WAITLIST, LIVE = "waitlist", "live"
FLOWS = (WAITLIST, LIVE)

STATE_FILE = state_paths.state_file("route_pauses.json")


def _load() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Every pause. Raises ValueError/OSError on a file that cannot be read."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh) or {}
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("not a JSON object")
    return {str(route).upper(): {f: dict(v) for f, v in flows.items()
                                 if f in FLOWS and isinstance(v, dict)}
            for route, flows in raw.items() if isinstance(flows, dict)}


def _save(data: Dict[str, Any]) -> None:
    """Atomic, and it RAISES: answering "paused" for a pause that was never
    written is the one failure this module must not have."""
    parent = os.path.dirname(STATE_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, STATE_FILE)


def snapshot() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Every pause in force, {route: {flow: {reason, by, at}}}."""
    return {route: flows for route, flows in _load().items() if flows}


def paused(route: str, flow: str) -> Optional[Dict[str, Any]]:
    """The pause on this route's flow, or None. Never raises.

    A pause file that exists but cannot be read counts as a pause on
    EVERYTHING: this is a stop control, and one that reads as "nothing is
    paused" when it breaks would let a run through that someone stopped.
    """
    try:
        return _load().get(str(route or "").strip().upper(), {}).get(flow)
    except (ValueError, OSError) as exc:
        log.error("%s is unreadable (%s); treating every flow as paused.",
                  STATE_FILE, exc)
        return {"reason": f"{STATE_FILE} is unreadable: {exc}", "by": "", "at": ""}


def problem(route: str, flow: str) -> str:
    """One sentence for a log line or an API problem, or '' when not paused."""
    entry = paused(route, flow)
    if not entry:
        return ""
    who = f" by {entry['by']}" if entry.get("by") else ""
    when = f" at {entry['at']}" if entry.get("at") else ""
    return (f"the {flow} flow is paused for {str(route).upper()}{who}{when}: "
            f"{entry.get('reason') or 'no reason given'}")


def pause(route: str, flow: str, reason: str, by: str = "") -> Dict[str, Any]:
    """Pause `flow` on `route`. Re-pausing replaces the reason."""
    if flow not in FLOWS:
        raise ValueError(f"flow must be one of {', '.join(FLOWS)}")
    route = str(route).strip().upper()
    data = _load()
    entry = {"reason": str(reason).strip(), "by": by,
             "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    data.setdefault(route, {})[flow] = entry
    _save(data)
    log.warning("Paused the %s flow for %s (%s): %s", flow, route,
                by or "unknown", entry["reason"])
    return entry


def resume(route: str, flow: str) -> bool:
    """Lift a pause. False when there was none."""
    route = str(route).strip().upper()
    data = _load()
    if flow not in data.get(route, {}):
        return False
    del data[route][flow]
    if not data[route]:
        del data[route]
    _save(data)
    log.warning("Lifted the pause on the %s flow for %s.", flow, route)
    return True
