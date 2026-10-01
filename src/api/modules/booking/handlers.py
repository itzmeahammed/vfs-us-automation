"""Booking pipeline endpoints.

GET  /booking/status  — read-only overview of booking routes + client phases
POST /booking/trigger — spawn a booking run (probe | walk | commit)

The trigger renders argv for `python -m src.booking probe`. Those flag names
are a contract between two files in two languages with nothing in the type
system connecting them, so they are asserted against the real parser in
tests/test_api_booking_trigger.py rather than trusted.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional

from fastapi import Depends, Header, HTTPException, status as http_status

from src.api.core.schemas import ErrorResponse
from src.api.modules.booking.schemas import BookingClientStatus, BookingMode, BookingRouteStatus, BookingStatusResponse, BookingTriggerRequest
from src.api.modules.jobs.schemas import TriggerResponse, JobResponse
from src.api.core.security import require_token

log = logging.getLogger("vfs.api.booking")


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


async def trigger_booking(
    payload: BookingTriggerRequest,
    idempotency_key: Optional[str] = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=200,
    ),
) -> TriggerResponse:
    """Spawn a booking run as a background job. Returns 202 immediately.

    `mode` decides how far it goes, and nothing else does:

        probe    log in, read the dashboard, click nothing.        READ-ONLY
        walk     click 'Book Now', walk the pages, stop at the
                 committing step.                                  REVERSIBLE
        commit   complete the booking and submit a real payment.   NO UNDO

    `mode=commit` additionally requires `confirm` to equal `route`. That check
    lives in the request model, so a malformed commit is a 422 before a job is
    spawned, before a browser opens, and before an account session is spent.

    Single-flight and Idempotency-Key behave exactly as on POST
    /trigger/waitlist — and matter more here. A retried POST without an
    Idempotency-Key is how you book twice.

    The response carries `run_id`. Use it for:

        GET /jobs/{run_id}          status and per-client results
        GET /jobs/{run_id}/stream   follow the run live
        GET /jobs/{run_id}/logs     the log after it finishes
        GET /payments/unanswered    check this FIRST if the run died

    and to grep the run out of the queryable log:

        jq 'select(.run_id=="<run_id>")' logs/app.jsonl
    """
    from src.api.modules.jobs.manager import JobAlreadyRunningError, JobStartError

    # Import the shared job_manager from main
    from src.api.main import job_manager

    # Build the booking probe command — a different CLI than the waitlist one
    booking_command = [sys.executable, "-m", "src.booking", "probe"]
    extra_args = payload.to_cli_args()

    # An irreversible run is logged BEFORE it is spawned, at WARNING, naming
    # who and what. If a card is charged, this line is the first evidence of
    # the request that did it — and it has to exist even if the spawn then
    # fails, which is why it is not logged alongside the success return below.
    if payload.mode is BookingMode.COMMIT:
        log.warning(
            "COMMIT BOOKING REQUESTED — route=%s registrant=%s combo=%s "
            "reason=%s. This submits a real payment.",
            payload.route, payload.registrant or "-",
            payload.combo or "-", payload.reason or "-",
        )

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
            if replayed else
            f"Booking run started in the background (mode={payload.mode.value})."
        ),
        job=JobResponse(**record.to_dict()),
        replayed=replayed,
    )
