"""Inbox reconciliation endpoint.

Combines an inbox poll (IMAP read of VFS account mailboxes) with the
reconciliation logic that settles uncertain journal rows using VFS's own
confirmation emails.

This is the API face of `python -m src.inbox reconcile`. The web app can
trigger it on demand rather than waiting for a human to SSH in.

IMPORTANT: The inbox poll opens IMAP connections to real mailboxes. It is
I/O-heavy but not minutes-long (typically 5-30 seconds). Declared as sync
so FastAPI runs it in a threadpool.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, status as http_status

from src.api.schemas import ReconcileProposal, ReconcileRequest, ReconcileResponse
from src.api.security import require_token

log = logging.getLogger("vfs.api.inbox")

router = APIRouter(prefix="/inbox", tags=["inbox"])


@router.post(
    "/reconcile",
    response_model=ReconcileResponse,
    dependencies=[Depends(require_token)],
)
def reconcile_inbox(payload: ReconcileRequest) -> ReconcileResponse:
    """Poll mailboxes and reconcile uncertain journal rows.

    Runs an inbox pass (reads VFS account mailboxes via IMAP), then matches
    confirmation emails against pending/unknown journal rows to settle them.

    dry_run=true (default) shows what WOULD be settled without writing.
    dry_run=false applies the settlements to the journal.
    """
    from src.inbox import reconcile
    from src.inbox.watcher import run_pass

    try:
        result = run_pass()
    except Exception as exc:                       # noqa: BLE001
        log.exception("Inbox pass failed during reconcile.")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Inbox poll failed: {exc}",
        ) from exc

    proposals_raw = reconcile.reconcile(
        result.observations,
        dry_run=payload.dry_run,
    )

    applied = 0
    if not payload.dry_run:
        applied = sum(1 for p in proposals_raw if p.will_apply)

    proposals = [
        ReconcileProposal(
            registrant_id=p.registrant_id,
            route=p.route,
            combo=p.combo,
            reference=p.reference,
            new_status=p.new_status,
            action=p.action,
            blocked_reason=p.blocked_reason,
            will_apply=p.will_apply,
        )
        for p in proposals_raw
    ]

    return ReconcileResponse(
        mailboxes_checked=result.mailboxes_checked,
        mailboxes_failed=result.mailboxes_failed,
        messages_seen=result.messages_seen,
        proposals=proposals,
        applied=applied,
        dry_run=payload.dry_run,
    )
