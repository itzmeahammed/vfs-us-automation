"""Catalog handlers: the routes (countries) and what each needs from a client."""

from __future__ import annotations

import logging
from typing import List

from fastapi import HTTPException, status

from src.api.modules.catalog.schemas import (RouteReadinessResponse, RouteSummary,
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
    from src.api.modules.catalog import service
    from src.waitlist import store

    route_ids: List[str] = []
    try:
        from src.utils.config_reader import get_config_section
        route_ids = sorted(
            r.upper() for r in (get_config_section("vfs-url") or {})
        )
    except Exception:                              # noqa: BLE001
        log.exception("Could not read the route list from the bot config.")

    summaries: List[RouteSummary] = []
    for route_id in route_ids:
        try:
            flows = service.route_flows(route_id)
            clients = store.list_ids(route=route_id)
            summaries.append(RouteSummary(
                route=route_id,
                flow=flows.flow,
                available_flows=flows.available,
                flows={name: {"ready": flows.ready[name],
                              "problems": [p.message for p in flows.problems[name]]}
                       for name in (service.WAITLIST, service.LIVE)},
                ready=flows.flow is not None,
                combos=flows.combos,
                clients=len(clients),
                problems=[p.message for p in flows.blockers],
            ))
        except Exception as exc:                   # noqa: BLE001
            summaries.append(RouteSummary(
                route=route_id, ready=False, problems=[str(exc)],
            ))

    return RoutesListResponse(count=len(summaries), routes=summaries)


def route_readiness(route: str) -> RouteReadinessResponse:
    """Which flow does this route run on, and what are its combos?

    `flow` is the bot's decision for this country — "live" or "waitlist" — and
    is what to send as `flow` in POST /clients; `flows` reports each of the two
    separately. The web app should call this before showing a signup form: it
    returns the valid combination labels to populate the dropdown, and says
    plainly when a route is not accepting clients (and why).
    """
    from src.api.modules.catalog import service
    from src.waitlist import validate

    readiness = service.route_flows(route)
    return RouteReadinessResponse(
        route=readiness.route,
        flow=readiness.flow,
        available_flows=readiness.available,
        flows={name: {"ready": readiness.ready[name],
                      "problems": _problems_to_models(readiness.problems[name])}
               for name in (service.WAITLIST, service.LIVE)},
        ready=readiness.flow is not None,
        combos=readiness.combos,
        # Returned even when ready=False: the web app can still render and
        # validate the form while a route is being brought online, and a caller
        # debugging "why won't this route accept anyone" benefits from seeing
        # what it would ask for.
        fields=validate.required_fields(readiness.route),
        problems=_problems_to_models(readiness.blockers),
    )

