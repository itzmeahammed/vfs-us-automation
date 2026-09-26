"""Booking pipeline endpoints.

GET  /booking/status  — read-only overview of booking routes + client phases
POST /booking/trigger — spawn a booking probe as a background job
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status as http_status

from src.api.schemas import (
    BookingClientStatus,
    BookingRouteStatus,
    BookingStatusResponse,
    BookingTriggerRequest,
    ErrorResponse,
    TriggerResponse,
    JobResponse,
)
from src.api.security import require_token

log = logging.getLogger("vfs.api.booking")

router = APIRouter(prefix="/booking", tags=["booking"])


@router.get(
    "/status",
    response_model=BookingStatusResponse,
    dependencies=[Depends(require_token)],
)
def get_booking_status() -> BookingStatusResponse:
    """Booking pipeline overview: routes, their config, and per-client phases.

    Reads booking configs and the waitlist journal to determine where each
    client sits in the waitlist-to-appointment journey.
    """
    from src.booking import config as booking_config
    from src.booking import lifecycle
    from src.waitlist import journal, store

    # Routes
    routes: List[BookingRouteStatus] = []
    for route in booking_config.configured_routes():
        try:
            cfg = booking_config.get(route)
            steps = booking_config.steps_for(route)
            routes.append(BookingRouteStatus(
                route=route,
                enabled=bool(cfg.get("enabled")),
                commit_step=booking_config.commit_step_name(route),
                steps=[{
                    "name": s.get("name", "?"),
                    "type": s.get("type", "?"),
                    "commits": bool(s.get("commits")),
                } for s in steps],
            ))
        except Exception as exc:                   # noqa: BLE001
            routes.append(BookingRouteStatus(
                route=route, steps=[{"error": str(exc)}],
            ))

    # Per-client: latest journal row determines their booking phase.
    clients: List[BookingClientStatus] = []
    try:
        all_rows = journal.entries()
        # Build latest row per (route, registrant_id)
        latest: Dict[tuple, dict] = {}
        for row in all_rows:
            key = (
                (row.get("route") or "").upper(),
                (row.get("registrant_id") or "").lower(),
            )
            latest[key] = row

        for (route, client_id), row in latest.items():
            status_val = str(row.get("status") or "")
            # Only include clients with committed/booking-phase statuses
            from src.waitlist.result import Status
            if status_val not in Status.COMMITTED_STATES and \
               status_val not in lifecycle.BookingStatus.ALL:
                continue

            phase = lifecycle.phase_of(status_val)
            clients.append(BookingClientStatus(
                client_id=client_id,
                route=route,
                status=status_val,
                vfs_reference=row.get("vfs_reference"),
                phase=phase,
                needs_attention=lifecycle.needs_attention(status_val),
            ))
    except Exception as exc:                       # noqa: BLE001
        log.exception("Could not read client booking statuses.")

    return BookingStatusResponse(routes=routes, clients=clients)


@router.post(
    "/trigger",
    response_model=TriggerResponse,
    status_code=http_status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_token)],
    responses={
        401: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def trigger_booking(
    payload: BookingTriggerRequest,
    idempotency_key: Optional[str] = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=200,
    ),
) -> TriggerResponse:
    """Spawn a booking probe/walk as a background job.

    This runs `python -m src.booking probe` which logs in, reads the
    dashboard, and reports what it found. With `walk: true` it also clicks
    'Book Now' and walks the booking pages (still dry-run by default).

    Same single-flight and idempotency as POST /trigger/waitlist.
    """
    from src.api.jobs import JobAlreadyRunningError, JobStartError

    # Import the shared job_manager from main
    from src.api.main import job_manager

    # Build the booking probe command — a different CLI than the waitlist one
    booking_command = [sys.executable, "-m", "src.booking", "probe"]
    extra_args = payload.to_cli_args()

    try:
        record, replayed = await job_manager.trigger(
            extra_args=extra_args,
            payload=payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            command_override=booking_command,
        )
    except JobAlreadyRunningError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except JobStartError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    return TriggerResponse(
        accepted=True,
        message=(
            "Replayed: this Idempotency-Key already started a job."
            if replayed else "Booking probe started in the background."
        ),
        job=JobResponse(**record.to_dict()),
        replayed=replayed,
    )
