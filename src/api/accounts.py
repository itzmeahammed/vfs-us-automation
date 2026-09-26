"""Account health management endpoints.

Exposes the circuit-breaker state for VFS accounts. When an account gets
rate-limited (429) or fails repeatedly, it is benched — and these endpoints
let the web app see that and clear it without SSH.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status

from src.api.schemas import (
    AccountBenchRequest,
    AccountClearResponse,
    AccountHealth,
    AccountHealthResponse,
    AccountRouteHealth,
)
from src.api.security import require_token

log = logging.getLogger("vfs.api.accounts")

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get(
    "/health",
    response_model=AccountHealthResponse,
    dependencies=[Depends(require_token)],
)
def get_account_health() -> AccountHealthResponse:
    """All accounts' circuit-breaker state.

    Shows which accounts are benched, disabled, or accumulating strikes.
    """
    from src.utils import account_health

    raw = account_health.snapshot()
    now = time.time()
    accounts: List[AccountHealth] = []

    for email, rec in raw.items():
        routes: List[AccountRouteHealth] = []
        for route, detail in (rec.get("routes") or {}).items():
            cooldown = float(detail.get("cooldown_until", 0) or 0)
            routes.append(AccountRouteHealth(
                route=route,
                cooldown_until=cooldown,
                fails=int(detail.get("fails", 0)),
                last_reason=str(detail.get("last_reason") or ""),
                benched=cooldown > now,
            ))

        accounts.append(AccountHealth(
            email=email,
            disabled=bool(rec.get("disabled")),
            disabled_reason=str(rec.get("disabled_reason") or ""),
            routes=routes,
        ))

    return AccountHealthResponse(count=len(accounts), accounts=accounts)


@router.post(
    "/health/{email}/clear",
    response_model=AccountClearResponse,
    dependencies=[Depends(require_token)],
)
def clear_account(
    email: str,
    route: Optional[str] = Query(
        default=None,
        description="Clear only this route. Without it, clears everything "
                    "including a global disable.",
    ),
) -> AccountClearResponse:
    """Flag an account healthy again.

    With `?route=AE-CHE`, clears only that route's cooldown and strikes.
    Without it, removes the entire record (clears a global disable too).
    """
    from src.utils import account_health

    cleared = account_health.clear(email, route=route)
    if not cleared:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"No health record for '{email}'"
                   + (f" on route '{route}'" if route else "") + ".",
        )

    log.info("Cleared account health for %s%s via API.",
             email, f" on {route}" if route else "")
    return AccountClearResponse(email=email, route=route, cleared=True)


@router.post(
    "/health/{email}/bench",
    dependencies=[Depends(require_token)],
)
def bench_account(email: str, payload: AccountBenchRequest) -> Dict[str, Any]:
    """Bench an account on a specific route for a number of hours.

    Use this when you know an account is blocked and want to prevent
    the bot from attempting to use it.
    """
    from src.utils import account_health

    account_health.bench(email, payload.route, payload.hours, payload.reason)
    until = account_health.benched_until(email, payload.route)

    log.info("Benched %s on %s for %dh via API.", email, payload.route,
             payload.hours)
    return {
        "email": email,
        "route": payload.route,
        "benched": True,
        "hours": payload.hours,
        "until": until,
        "reason": payload.reason,
    }
