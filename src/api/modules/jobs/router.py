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

from src.api.core import search
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
              needs_attention: bool = Query(default=False),
              q: Optional[str] = Query(default=None, max_length=120, description=(
                  "Free-text search over job id, status, detail and the "
                  "payload's registrant/client/route/mode. All terms must "
                  "match; each may match a different field."))) -> Dict[str, Any]:
    from src.api.modules.jobs.schemas import JobResponse

    needle = search.terms(q)
    rows = []
    for record in _manager().recent(500):
        if lane and (record.payload or {}).get("lane", "waitlist") != lane:
            continue
        if status and record.status.value != status:
            continue
        if needs_attention and not record.needs_attention:
            continue
        row = JobResponse(**record.to_public()).model_dump()
        if not search.matches(_haystack(row), needle, _SEARCHABLE):
            continue
        rows.append(row)
    return paginate(rows, page)


#: What `q` may look at on a job. `payload` is flattened into its own row by
#: `_haystack` rather than named here, because `search.matches` only reads a
#: dict's KEYS — and for a payload it is the values (which client, which route)
#: that an operator searches for.
_SEARCHABLE = ("job_id", "status", "detail", "lane",
               "registrant", "client_id", "route", "mode")


def _haystack(row: Dict[str, Any]) -> Dict[str, Any]:
    """A job row flattened so the payload's own fields are searchable.

    Only the payload keys named in `_SEARCHABLE` are lifted. A job payload can
    carry anything a caller sent — including, on a live-flow run, applicant
    fields — so lifting the whole payload would make `q` a search over data the
    clients endpoint deliberately masks.
    """
    payload = row.get("payload") or {}
    merged = {k: v for k, v in row.items() if k != "payload"}
    for key in ("registrant", "client_id", "route", "mode", "lane"):
        if key in payload and key not in merged:
            merged[key] = payload[key]
    return merged


@router.get("/active", summary="Running jobs, per lane")
def active() -> Dict[str, Any]:
    from src.api.modules.jobs.schemas import JobResponse

    return {lane: JobResponse(**rec.to_public()).model_dump()
            for lane, rec in _manager().active_jobs().items()}


router.get("/{job_id}", summary="One job")(handlers.get_job)
router.get("/{job_id}/logs", summary="Tail of a job's log")(handlers.get_job_logs)
router.get("/{job_id}/stream", summary="Follow a job live (SSE)")(handlers.stream_job_log)
router.post("/{job_id}/cancel", summary="Cancel a running job")(handlers.cancel_job)
