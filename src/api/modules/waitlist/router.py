"""Waitlist (Flow 1): registration runs and the journal they write.

    POST /v1/waitlist/runs                 register now (dry run by default)
    GET  /v1/waitlist/status               posture, switches, today's counts
    GET  /v1/waitlist/dangling             submits with no known outcome
    POST /v1/waitlist/dangling/resolve     a human settles one
    POST /v1/waitlist/reconcile            settle dangling rows from VFS emails

Clients themselves (flow=waitlist) are under /v1/clients, including their
documents and journal.
"""

from fastapi import APIRouter, Depends

from src.api.modules.waitlist import reconcile as reconcile
from src.api.modules.waitlist import status as status
from src.api.core.errors import ERROR_RESPONSES
from src.api.core.security import require_token
from src.api.modules.waitlist import handlers

router = APIRouter(prefix="/waitlist", tags=["waitlist"],
                   dependencies=[Depends(require_token)], responses=ERROR_RESPONSES)


router.post("/runs", status_code=202, summary="Register now")(handlers.trigger_waitlist)


router.get("/status", summary="Waitlist posture and counts")(status.get_status)
router.get("/dangling", summary="Submits with no known outcome")(status.get_dangling)
router.post("/dangling/resolve", summary="Settle a dangling submit")(status.resolve_entry)
router.post("/reconcile", summary="Settle dangling rows from VFS emails")(
    reconcile.reconcile_inbox)
