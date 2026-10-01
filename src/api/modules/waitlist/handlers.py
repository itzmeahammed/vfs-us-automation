"""Waitlist run handler: start a registration run as a background job."""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from src.api.modules.jobs.manager import JobAlreadyRunningError, JobStartError
from src.api.modules.jobs.runtime import job_manager
from src.api.modules.jobs.schemas import JobResponse, TriggerResponse
from src.api.modules.waitlist.schemas import TriggerRequest


async def trigger_waitlist(
    payload: TriggerRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=200,
        description=(
            "Optional. Send a stable unique value (a UUID) to make retries "
            "safe: a repeat with the same key returns the ORIGINAL job instead "
            "of starting a second run. Strongly recommended for live triggers."
        ),
    ),
) -> TriggerResponse:
    """Spawn the waitlist job in the background and return at once.

    202 Accepted means "spawned", never "finished". Poll GET /jobs/{job_id}
    for the outcome, or read the log with GET /jobs/{job_id}/logs.

    IDEMPOTENCY. Without a key, a client that retries after a network timeout
    can start a SECOND live registration run — single-flight blocks the
    concurrent case but not a sequential retry after the first finished. Send
    an Idempotency-Key and the retry returns the original job untouched.
    """
    try:
        record, replayed = await job_manager.trigger(
            extra_args=payload.to_cli_args(),
            payload=payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
        )
    except JobAlreadyRunningError as exc:
        # A distinct exception type, not a substring of the message: rewording
        # the message must never silently turn a 409 into a 500.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except JobStartError as exc:
        # The process would not start at all — a broken install, not a busy one.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return TriggerResponse(
        accepted=True,
        message=(
            "Replayed: this Idempotency-Key already started a job, so nothing "
            "new was spawned."
            if replayed else "Job started in the background."
        ),
        job=JobResponse(**record.to_dict()),
        replayed=replayed,
    )

