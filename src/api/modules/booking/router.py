"""Booking: manual runs, VFS invitations, payments.

    POST /v1/booking/runs                         run a booking now (probe | walk | commit)
    GET  /v1/booking/status                       booking configs per country
    GET  /v1/booking/invitations                  VFS invitation emails and their outcome
    GET  /v1/booking/invitations/{key}            one invitation
    POST /v1/booking/invitations/{key}/resolve    a human settles one
    GET  /v1/booking/payments/unanswered          payments with no recorded outcome

Live booking requests are CLIENTS (flow=live) and live under /v1/clients. A
manual run takes a client_id of either flow: a live client supplies its own
applicant, account, combo and date window; a waitlist client resumes their
invited application.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field

from src.api.modules.booking import handlers as handlers
from src.api.modules.booking import payments as payments
from src.api.core.context import actor_for
from src.api.core.errors import ERROR_RESPONSES, ApiError
from src.api.core.pagination import PageParams, paginate
from src.api.core.security import require_token

router = APIRouter(prefix="/booking", tags=["booking"],
                   dependencies=[Depends(require_token)], responses=ERROR_RESPONSES)


class RunRequest(BaseModel):
    """A manual booking run. `client_id` is the normal way in."""

    client_id: Optional[str] = Field(
        default=None, description="A client of either flow. Supplies everything.")
    mode: Literal["probe", "walk", "commit"] = Field(
        default="probe", description="probe: log in and read. walk: every page, "
                                     "stop before paying. commit: BOOK AND PAY.")
    confirm: Optional[str] = Field(
        default=None, description="mode=commit only: must equal the route, e.g. AE-NOR.")
    route: Optional[str] = Field(default=None, description="Only without client_id.")
    combo: Optional[str] = Field(default=None, description="Only without client_id.")
    applicant: Dict[str, str] = Field(default_factory=dict,
                                      description="Only without client_id.")
    capture: Literal["off", "failure", "full"] = "failure"
    to_step: Optional[str] = None
    reason: Optional[str] = Field(default=None, max_length=280)


@router.post("/runs", status_code=202, summary="Run a booking now")
async def run_booking(body: RunRequest, request: Request,
                      idempotency_key: Optional[str] = Header(
                          default=None, alias="Idempotency-Key", max_length=200)):
    """Starts a background job in the booking lane; follow it with
    GET /v1/jobs/{job_id}/stream. mode=commit spends money — send an
    Idempotency-Key so a retry cannot book twice."""
    from src.api.modules.booking.schemas import BookingTriggerRequest
    from src.booking import requests as req_store

    fields: Dict[str, Any] = {
        "mode": body.mode, "confirm": body.confirm, "capture": body.capture,
        "to_step": body.to_step,
        "reason": (f"[{actor_for(request)}] " + (body.reason or "")).strip(),
    }
    if body.client_id:
        if body.route or body.combo or body.applicant:
            raise ApiError(422, "client_id already supplies route, combo and "
                                "applicant; send it alone.")
        cid = body.client_id.strip().lower()
        if req_store.exists(cid):
            req = req_store.get(cid)
            fields.update(route=req.route, request_id=cid, entry="new")
        else:
            from src.waitlist import registrant as registrant_mod
            try:
                person = registrant_mod.load(cid)
            except Exception as exc:                        # noqa: BLE001
                raise ApiError(404, f"No client {cid!r}.") from exc
            fields.update(route=person.route, registrant=cid, entry="waitlist")
    else:
        if not body.route:
            raise ApiError(422, "Send client_id, or route (+ combo and applicant "
                                "for a live-slot walk).")
        fields.update(route=body.route, combo=body.combo, applicant=body.applicant)

    try:
        payload = BookingTriggerRequest(**{k: v for k, v in fields.items()
                                           if v not in (None, {}, "")})
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc
    return await handlers.trigger_booking(payload, idempotency_key)


router.get("/status", summary="Booking configs per country")(handlers.get_booking_status)
router.get("/payments/unanswered", summary="Payments with no recorded outcome")(
    payments.get_unanswered_payments)


# --------------------------------------------------------------------------- #
# Invitations                                                                  #
# --------------------------------------------------------------------------- #


def _invite_view(item: Dict[str, Any]) -> Dict[str, Any]:
    from src.booking import invites

    out = dict(item)
    out["account"] = invites._mask(item.get("account", ""))
    out["deadline"] = invites._deadline(item)
    return out


@router.get("/invitations", summary="VFS invitation emails and their outcome")
def list_invitations(page: PageParams = Depends(),
                     status: Optional[str] = Query(
                         default=None, description="pending | booking | booked | "
                         "needs_attention | manual | expired | dismissed"),
                     route: Optional[str] = None) -> Dict[str, Any]:
    from src.booking import invites

    items = [i for i in invites.list_all(status=status)
             if not route or i.get("route") == route.upper()]
    items.reverse()                                          # newest first
    return paginate([_invite_view(i) for i in items], page)


@router.get("/invitations/{key}", summary="One invitation")
def get_invitation(key: str) -> Dict[str, Any]:
    from src.booking import invites

    try:
        return _invite_view(invites.get(key))
    except (OSError, ValueError) as exc:
        raise ApiError(404, f"No invitation {key!r}.") from exc


class InvitationResolve(BaseModel):
    outcome: Literal["booked", "not_booked", "dismissed"]
    reason: Optional[str] = Field(default=None, max_length=280)
    appointment_date: Optional[str] = None
    appointment_time: Optional[str] = None
    reference: Optional[str] = None


@router.post("/invitations/{key}/resolve", summary="A human settles an invitation")
def resolve_invitation(key: str, body: InvitationResolve,
                       request: Request) -> Dict[str, Any]:
    from src.booking import invites

    try:
        invites.get(key)
    except (OSError, ValueError) as exc:
        raise ApiError(404, f"No invitation {key!r}.") from exc
    details = {k: v for k, v in body.model_dump().items()
               if k in ("appointment_date", "appointment_time", "reference") and v}
    try:
        item = invites.resolve(key, body.outcome, body.reason or "",
                               actor=actor_for(request), details=details)
    except ValueError as exc:
        raise ApiError(409, str(exc)) from exc
    return _invite_view(item)
