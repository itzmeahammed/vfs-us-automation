"""Catalog: the countries (routes) and their centre/category combos.

    GET /v1/routes                       every route and what it supports
    GET /v1/routes/{route}/readiness     combos, required fields, blockers

A web form builds its route and combo dropdowns from these, so it can never
offer a combo the bot cannot select.
"""

from fastapi import APIRouter, Depends

from src.api.modules.catalog import handlers
from src.api.core.errors import ERROR_RESPONSES
from src.api.core.security import require_token

router = APIRouter(prefix="/routes", tags=["catalog"],
                   dependencies=[Depends(require_token)], responses=ERROR_RESPONSES)

router.get("", summary="List routes")(handlers.list_routes)
router.get("/{route}/readiness", summary="Route readiness and combos")(handlers.route_readiness)
