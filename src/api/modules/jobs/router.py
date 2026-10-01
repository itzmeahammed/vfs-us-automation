"""Jobs: every background run the API started.

    GET  /v1/jobs                     list (filter by lane/status; paginated)
    GET  /v1/jobs/active              what is running now, per lane
    GET  /v1/jobs/{job_id}            one job
    GET  /v1/jobs/{job_id}/logs       tail of its log
    GET  /v1/jobs/{job_id}/stream     follow it live (Server-Sent Events)
    POST /v1/jobs/{job_id}/cancel     stop it

Two lanes, each one-at-a-time: `waitlist` (registrations) and `booking`
(manual booking runs). A run in one lane never blocks the other.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from src.api.core.errors import ERROR_RESPONSES
from src.api.core.pagination import PageParams, paginate
from src.api.core.security import require_token
from src.api.modules.jobs import handlers

router = APIRouter(prefix="/jobs", tags=["jobs"],
                   dependencies=[Depends(require_token)], responses=ERROR_RESPONSES)


def _manager():
    from src.api.modules.jobs.runtime import job_manager
    return job_manager


@router.get("", summary="List jobs, newest first")
def list_jobs(page: PageParams = Depends(),
              lane: Optional[str] = Query(default=None, description="waitlist | booking"),
              status: Optional[str] = Query(default=None,
                                            description="running | succeeded | failed | ..."),
              needs_attention: bool = Query(default=False)) -> Dict[str, Any]:
    from src.api.modules.jobs.schemas import JobResponse

    rows = []
    for record in _manager().recent(500):
        if lane and (record.payload or {}).get("lane", "waitlist") != lane:
            continue
        if status and record.status.value != status:
            continue
        if needs_attention and not record.needs_attention:
            continue
        rows.append(JobResponse(**record.to_dict()).model_dump())
    return paginate(rows, page)


@router.get("/active", summary="Running jobs, per lane")
def active() -> Dict[str, Any]:
    from src.api.modules.jobs.schemas import JobResponse

    return {lane: JobResponse(**rec.to_dict()).model_dump()
            for lane, rec in _manager().active_jobs().items()}


router.get("/{job_id}", summary="One job")(handlers.get_job)
router.get("/{job_id}/logs", summary="Tail of a job's log")(handlers.get_job_logs)
router.get("/{job_id}/stream", summary="Follow a job live (SSE)")(handlers.stream_job_log)
router.post("/{job_id}/cancel", summary="Cancel a running job")(handlers.cancel_job)
