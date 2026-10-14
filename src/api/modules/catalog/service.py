"""Which flow a country runs on: waitlist, live booking, both, or neither.

A client is created with `flow: waitlist | live` (POST /clients), and the two
are answered by DIFFERENT configs:

    waitlist   config/waitlist/<ROUTE>.json   register, wait for an invitation
    live       config/booking/<ROUTE>.json    book an open slot and pay

So the caller has to know which one a country is on before it can create
anyone. This is the one place that reads both and decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

WAITLIST, LIVE = "waitlist", "live"

#: Which flow `flow` names when a country is ready for BOTH. Live first: an
#: open slot books now, where the waitlist only queues for an invitation.
PREFERENCE = (LIVE, WAITLIST)

#: Each flow's master switch in [switches].
_SWITCH = {WAITLIST: "waitlist", LIVE: "live_booking"}


@dataclass
class RouteFlows:
    """One country's readiness, per flow, and the flow the bot picks."""

    route: str
    flow: Optional[str]                       # the pick; None = neither is ready
    ready: Dict[str, bool]
    problems: Dict[str, List[Any]]            # validate.Problem, per flow
    combos: List[str] = field(default_factory=list)
    #: Operator pauses in force, per flow: {reason, by, at} or None.
    paused: Dict[str, Optional[Dict[str, Any]]] = field(default_factory=dict)

    @property
    def available(self) -> List[str]:
        return [f for f in PREFERENCE if self.ready[f]]

    @property
    def blockers(self) -> List[Any]:
        """What stands between this route and the picked flow. With no flow
        ready that is every flow's problems, each naming its own config."""
        if self.flow:
            return list(self.problems[self.flow])
        return [p for f in (WAITLIST, LIVE) for p in self.problems[f]]


def _switch_warning(flow: str) -> List[Any]:
    """A master switch that is off does not make a country un-ready — clients
    are still accepted — but nothing will act on them, so say so."""
    from src.settings import settings
    from src.waitlist.validate import WARNING, Problem

    key = _SWITCH[flow]
    try:
        on = bool(getattr(settings().switches, key))
    except Exception:                              # noqa: BLE001
        return []
    if on:
        return []
    return [Problem(
        field="", severity=WARNING,
        message=f"[switches] {key} is off on this machine: {flow} clients are "
                "accepted but nothing runs for them until it is on.",
        hint="PATCH /v1/switches.")]


def route_flows(route: str) -> RouteFlows:
    """Decide the flow for one country. Never raises."""
    from src.booking import requests as booking_requests
    from src.utils import route_pause
    from src.waitlist import validate

    waitlist = validate.route_readiness(route)
    route = waitlist.route
    problems: Dict[str, List[Any]] = {WAITLIST: list(waitlist.problems), LIVE: []}

    # A malformed key or a route with no login URL can run NEITHER flow, so
    # live booking is held to the same two conditions the waitlist check opens
    # with, before its own config is consulted.
    try:
        from src.utils.config_reader import get_config_value

        if not validate._ROUTE_RE.match(route):
            problems[LIVE] = [validate.Problem(
                field="route",
                message=f"'{route}' is not a valid route key. Use the form 'AE-CHE'.")]
        elif not get_config_value("vfs-url", route, ""):
            problems[LIVE] = [validate.Problem(
                field="route",
                message=f"No login URL for {route} in config/vfs_urls.ini "
                        "(the route is commented out or absent).",
                hint="Uncomment the route there to make it live.")]
        else:
            problems[LIVE] = booking_requests.route_problems(route)
    except Exception as exc:                       # noqa: BLE001
        problems[LIVE] = [validate.Problem(
            field="route", message=f"Could not check live booking for {route}: {exc}")]

    ready = {f: not any(p.severity == validate.ERROR for p in problems[f])
             for f in (WAITLIST, LIVE)}
    for f in (WAITLIST, LIVE):
        if ready[f]:
            problems[f].extend(_switch_warning(f))

    return RouteFlows(
        route=route,
        flow=next((f for f in PREFERENCE if ready[f]), None),
        ready=ready,
        problems=problems,
        combos=waitlist.combos,
        paused={f: route_pause.paused(route, f) for f in (WAITLIST, LIVE)},
    )


def flow_views(flows: RouteFlows, as_text: bool = False,
               fields: Optional[Dict[str, List[Any]]] = None
               ) -> Dict[str, Dict[str, Any]]:
    """`flows` for a response body. as_text: problems as plain sentences."""
    return {name: {
        "ready": flows.ready[name],
        "paused": flows.paused.get(name),
        "problems": [p.message if as_text else p.to_dict()
                     for p in flows.problems[name]],
        **({"fields": fields[name]} if fields else {}),
    } for name in (WAITLIST, LIVE)}


def flow_fields(route: str) -> Dict[str, List[Dict[str, Any]]]:
    """The form each flow needs from a client, per flow. Described even for a
    flow that is not ready, so a form can be built while a route is being
    brought online."""
    from src.waitlist import validate

    return {WAITLIST: validate.required_fields(route),
            LIVE: validate.required_fields_live(route)}


def set_flow(route: str, flow: str, enabled: bool, reason: str, by: str) -> bool:
    """Pause or resume one flow on one country. Returns whether anything
    changed (resuming a flow that was not paused changes nothing)."""
    from src.utils import route_pause

    if enabled:
        return route_pause.resume(route, flow)
    route_pause.pause(route, flow, reason, by)
    return True
