"""System: liveness, readiness, the master switches, config, overview, audit.

    GET   /v1/health          liveness — unauthenticated, says nothing
    GET   /v1/health/ready    can this machine do what its switches say?
    GET   /v1/switches        the four master switches
    PATCH /v1/switches        flip them (written to config.local.ini)
    GET   /v1/config          effective configuration, secrets removed
    GET   /v1/overview        every client and where it is in its pipeline
    GET   /v1/audit           who changed what (X-Actor), newest first
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from src.api.modules.system import config_view as config_view
from src.api.modules.system import overview as pipeline
from src.api.modules.waitlist import status as waitlist_status
from src.api import __version__
from src.api.core.context import read_audit
from src.api.core.schemas import HealthResponse
from src.api.core.errors import ERROR_RESPONSES
from src.api.modules.system import service
from src.api.core.security import require_token

public_router = APIRouter(tags=["system"])
router = APIRouter(tags=["system"], dependencies=[Depends(require_token)],
                   responses=ERROR_RESPONSES)


@public_router.get("/health", summary="Liveness (no auth)")
async def health() -> HealthResponse:
    """Liveness. Unauthenticated on purpose and says nothing sensitive, so a
    tunnel can be checked without handing out the token."""
    return HealthResponse(status="ok", version=__version__,
                          server_time=datetime.now(timezone.utc).isoformat())


class Check(BaseModel):
    name: str
    state: str
    detail: str


class Readiness(BaseModel):
    status: str
    checks: List[Check]


@router.get("/health/ready", response_model=Readiness,
            summary="Readiness: card, accounts, mail, Telegram, slot checker")
def ready():
    """200 when ready or degraded, 503 when a switched-on flow cannot work."""
    results = service.checks()
    state = service.overall(results)
    body = {"status": state, "checks": results}
    return JSONResponse(status_code=503 if state == "not_ready" else 200, content=body)


class Switches(BaseModel):
    waitlist: bool
    invite_booking: bool
    live_booking: bool
    test_booking: bool
    note: str = ""


class SwitchesPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    waitlist: Optional[bool] = None
    invite_booking: Optional[bool] = None
    live_booking: Optional[bool] = None
    test_booking: Optional[bool] = None


@router.get("/switches", response_model=Switches, summary="The four master switches")
def get_switches() -> Dict[str, Any]:
    return service.switches_view()


@router.patch("/switches", response_model=Switches, summary="Flip master switches")
def patch_switches(body: SwitchesPatch) -> Dict[str, Any]:
    """Only the switches sent change. Applies from the NEXT slot-check run."""
    from src.api.modules.system.schemas import SwitchesUpdateRequest

    sent = body.model_fields_set
    waitlist_status.update_switches(SwitchesUpdateRequest(
        **{k: v for k, v in body.model_dump().items() if k in sent}))
    return service.switches_view()


router.get("/config", summary="Effective configuration (no secrets)")(config_view.get_config)
router.get("/overview", summary="Every client and its pipeline stage")(pipeline.get_pipeline)


@router.get("/audit", summary="Who changed what, newest first")
def audit(limit: int = Query(default=100, ge=1, le=1000),
          actor: str = Query(default="", description="Only this X-Actor"),
          path_prefix: str = Query(default="", description="e.g. /v1/clients")
          ) -> Dict[str, Any]:
    rows = read_audit(limit=limit, actor=actor, path_prefix=path_prefix)
    return {"count": len(rows), "items": rows}
