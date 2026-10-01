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
    """All configured routes with readiness summary and client count.

    The web app needs this to populate a route picker before it can call
    GET /routes/{route}/readiness for the form fields.
    """
    from src.waitlist import store, validate

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
            readiness = validate.route_readiness(route_id)
            clients = store.list_ids(route=route_id)
            summaries.append(RouteSummary(
                route=route_id,
                ready=readiness.ready,
                combos=readiness.combos,
                clients=len(clients),
                problems=[p.message for p in readiness.problems],
            ))
        except Exception as exc:                   # noqa: BLE001
            summaries.append(RouteSummary(
                route=route_id, ready=False, problems=[str(exc)],
            ))

    return RoutesListResponse(count=len(summaries), routes=summaries)


def route_readiness(route: str) -> RouteReadinessResponse:
    """Can this route accept waitlist registrations, and what are its combos?

    The web app should call this before showing a signup form: it returns the
    valid combination labels to populate the dropdown, and says plainly when a
    route is not accepting registrations (and why).
    """
    from src.waitlist import validate

    readiness = validate.route_readiness(route)
    return RouteReadinessResponse(
        route=readiness.route,
        ready=readiness.ready,
        combos=readiness.combos,
        # Returned even when ready=False: the web app can still render and
        # validate the form while a route is being brought online, and a caller
        # debugging "why won't this route accept anyone" benefits from seeing
        # what it would ask for.
        fields=validate.required_fields(readiness.route),
        problems=_problems_to_models(readiness.problems),
    )

