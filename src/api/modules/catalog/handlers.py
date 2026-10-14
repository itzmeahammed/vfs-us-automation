"""Catalog handlers: the routes (countries) and what each needs from a client."""

from __future__ import annotations

import logging
from typing import List

from fastapi import Request

from src.api.core.errors import ApiError
from src.api.modules.catalog.schemas import (RouteFlowPatch, RouteFlowPatchResponse,
                                             RouteReadinessResponse, RouteSummary,
                                             RoutesListResponse)
from src.api.modules.clients.waitlist import _problems_to_models

log = logging.getLogger("vfs.api.catalog")


def list_routes() -> RoutesListResponse:
    """All configured routes: the flow each runs on, and client count.

    `flow` is the bot's decision per country — "live" or "waitlist" — and is
    what to send as `flow` in POST /clients. `flows` says whether each of the
    two is ready, and why not. The web app needs this to populate a route
    picker before it can call GET /routes/{route}/readiness for the form fields.
    """
    route_ids: List[str] = []
    try:
        from src.utils.config_reader import get_config_section
        route_ids = sorted(
            r.upper() for r in (get_config_section("vfs-url") or {})
        )
    except Exception:                              # noqa: BLE001
        log.exception("Could not read the route list from the bot config.")

    return RoutesListResponse(count=len(route_ids),
                              routes=[_summary(r) for r in route_ids])


def _summary(route_id: str) -> RouteSummary:
    """One route as the listing reports it. Never raises."""
    from src.api.modules.catalog import service
    from src.waitlist import store

    try:
        flows = service.route_flows(route_id)
        return RouteSummary(
            route=flows.route,
            flow=flows.flow,
            available_flows=flows.available,
            flows=service.flow_views(flows, as_text=True),
            ready=flows.flow is not None,
            combos=flows.combos,
            clients=len(store.list_ids(route=flows.route)),
            problems=[p.message for p in flows.blockers],
        )
    except Exception as exc:                       # noqa: BLE001
        return RouteSummary(route=route_id, ready=False, problems=[str(exc)])


def route_readiness(route: str) -> RouteReadinessResponse:
    """Which flow does this route run on, and what are its combos?

    `flow` is the bot's decision for this country — "live" or "waitlist" — and
    is what to send as `flow` in POST /clients; `flows` reports each of the two
    separately. The web app should call this before showing a signup form: it
    returns the valid combination labels to populate the dropdown, and says
    plainly when a route is not accepting clients (and why).

    `fields` is the form for that flow: for a live country, the date window,
    the booking pages' fields and the payment page's billing fields.
    """
    from src.api.modules.catalog import service

    readiness = service.route_flows(route)
    fields = service.flow_fields(readiness.route)
    return RouteReadinessResponse(
        route=readiness.route,
        flow=readiness.flow,
        available_flows=readiness.available,
        flows=service.flow_views(readiness, fields=fields),
        ready=readiness.flow is not None,
        combos=readiness.combos,
        # Returned even when ready=False: the web app can still render and
        # validate the form while a route is being brought online, and a caller
        # debugging "why won't this route accept anyone" benefits from seeing
        # what it would ask for.
        # The form of the flow the caller is told to use, so one call is
        # enough to render it and submit.
        fields=fields[readiness.flow or service.WAITLIST],
        problems=_problems_to_models(readiness.blockers),
    )


def set_route_flow(route: str, flow: str, body: RouteFlowPatch,
                   request: Request) -> RouteFlowPatchResponse:
    """Turn ONE flow off for ONE country, or back on.

    For when something is off with a country today — the portal changed, a
    run is misbehaving — and that flow should stop there without touching the
    other countries or the other flow.

    `{"enabled": false, "reason": "..."}` pauses it: the route stops reporting
    that flow as ready, new clients for it are refused, and the bot's own run
    gates stop acting on it from their next check. A run already inside the
    browser is not interrupted; cancel it with POST /jobs/{job_id}/cancel.

    `{"enabled": true}` lifts the pause. It restores what the route's config
    says and nothing more: it cannot switch on a flow whose config is missing
    or disabled, so read `readiness` in the answer to see where the route
    stands.

    Pausing `waitlist` stops new registrations. It does not stop booking a
    client VFS has already invited; that is [switches] invite_booking.
    """
    from src.api.modules.catalog import service
    from src.utils.config_reader import get_config_value
    from src.waitlist import validate

    route = route.strip().upper()
    if flow not in (service.WAITLIST, service.LIVE):
        raise ApiError(422, "flow must be 'waitlist' or 'live'.")
    if not validate._ROUTE_RE.match(route) or not get_config_value("vfs-url", route, ""):
        raise ApiError(404, f"No route '{route}'. GET /v1/routes lists them.")

    try:
        changed = service.set_flow(route, flow, body.enabled, body.reason,
                                   by=getattr(request.state, "actor", "unknown"))
    except (OSError, ValueError) as exc:
        raise ApiError(500, f"The pause could not be saved: {exc}") from exc

    summary = _summary(route)
    now_ready = summary.flows is not None and getattr(summary.flows, flow).ready
    if not body.enabled:
        message = f"The {flow} flow is paused for {route}."
    elif not changed:
        message = f"The {flow} flow was not paused for {route}; nothing changed."
    elif now_ready:
        message = f"The {flow} flow is back on for {route}."
    else:
        message = (f"The pause is lifted, but the {flow} flow is still not "
                   f"ready for {route}: see readiness.flows.{flow}.problems.")
    return RouteFlowPatchResponse(route=route, flow_changed=flow, changed=changed,
                                  message=message, readiness=summary)
