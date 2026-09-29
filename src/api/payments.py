"""Payment visibility over HTTP. Read-only — nothing here moves money.

    ═════════════════ WHY THIS ENDPOINT EARNS ITS PLACE ═════════════════

`src/payment/journal.py` already computes the one fact that matters most in
this whole system: which payments were submitted with no outcome ever recorded
after them. Each of those may be a real charge on a real card, and none may be
retried until a human has checked the gateway.

Until now that fact was reachable only by `python -m src.payment status` on the
machine itself. So the single worst state the system can be in was the one
state a remote operator could not see, and the only way to find it was to SSH
into an EC2 box and hope you thought to look.

    ═════════════════ THE FAIL-CLOSED RULE ═════════════════

An unreadable journal is reported as `needs_attention: true` with
`journal_readable: false` — NOT as an empty list. "I cannot read the file" and
"there is nothing in the file" are the same shape over JSON and have opposite
meanings, and picking the reassuring one would hide exactly the emergency this
endpoint exists to surface.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from src.api.schemas import UnansweredPayment, UnansweredPaymentsResponse
from src.api.security import require_token

log = logging.getLogger("vfs.api.payments")

router = APIRouter(prefix="/payments", tags=["payments"])

#: Fields lifted from a journal row onto the response. An allow-list rather
#: than passing the row through: the journal is append-only and a future writer
#: may add a field, and a new field should not reach an HTTP response without
#: someone deciding it should.
_EXPOSED = ("run_id", "booking_ref", "at", "url", "route", "registrant_id")


def _to_model(row: Dict[str, Any]) -> UnansweredPayment:
    return UnansweredPayment(**{
        key: row.get(key) for key in _EXPOSED if row.get(key) is not None
    })


@router.get(
    "/unanswered",
    response_model=UnansweredPaymentsResponse,
    dependencies=[Depends(require_token)],
    summary="Payments submitted with no recorded outcome",
)
def get_unanswered_payments() -> UnansweredPaymentsResponse:
    """THE FIRST THING TO CHECK after a run dies unexpectedly.

    A non-empty list means one or more cards may have been charged with no
    result recorded. Resolve each one by looking at the gateway, not by
    retrying — see RUNBOOK.md.

    Declared `def`, not `async def`: it reads a file, and FastAPI runs a sync
    endpoint in a threadpool rather than blocking the event loop.
    """
    from src.payment import journal

    try:
        rows: List[Dict[str, Any]] = journal.unanswered()
    except Exception as exc:                                 # noqa: BLE001
        # Fail closed and loudly. See the module docstring: an unreadable
        # journal must never be served as "nothing to worry about".
        log.exception("Could not read the payment journal.")
        return UnansweredPaymentsResponse(
            count=0,
            needs_attention=True,
            payments=[],
            journal_file=journal.JOURNAL_FILE,
            journal_readable=False,
        )

    payments = [_to_model(row) for row in rows]
    if payments:
        log.warning(
            "%d payment(s) submitted with no recorded outcome: %s",
            len(payments),
            ", ".join(p.booking_ref or p.run_id or "?" for p in payments),
        )
    return UnansweredPaymentsResponse(
        count=len(payments),
        needs_attention=bool(payments),
        payments=payments,
        journal_file=journal.JOURNAL_FILE,
        journal_readable=True,
    )
