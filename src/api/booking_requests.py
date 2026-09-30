"""Booking requests: the web app's way to say "book this person, these dates".

    POST   /booking-requests                    store + arm a request
    GET    /booking-requests                    list (filter by route/status)
    GET    /booking-requests/{id}               one request, with its history
    PUT    /booking-requests/{id}               replace its fields
    PATCH  /booking-requests/{id}               change some fields
    DELETE /booking-requests/{id}               remove it
    POST   /booking-requests/{id}/enable        arm
    POST   /booking-requests/{id}/disable       park
    POST   /booking-requests/{id}/resolve       settle a needs_attention one

A stored, armed request is booked AUTOMATICALLY — and paid for with the
company card — when the slot checker sees a date inside its window. See
src/booking/autobook.py. That is why every write is fully validated up front:
the agent is looking at the form now, and nobody is looking when it books.

Distinct from POST /clients, which puts a client on a WAITLIST. A live-slot
country (Norway) has no waitlist, so POST /clients rejects it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from src.api.clients import _public_view
from src.api.security import require_token

log = logging.getLogger("vfs.api.booking_requests")

router = APIRouter(prefix="/booking-requests", tags=["booking-requests"],
                   dependencies=[Depends(require_token)])


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #


class BookingRequestFields(BaseModel):
    """The fields a sales agent supplies. EXTRA KEYS ARE ALLOWED: they are the
    applicant's form data, and which ones are required depends on the route's
    booking pages — the 422 names exactly the missing ones."""

    model_config = ConfigDict(extra="allow", json_schema_extra={"example": {
        "route": "AE-NOR",
        "combo": "Norway Visa Application Center - Dubai - Tourist",
        "date_from": "2026-10-10", "date_to": "2026-10-20",
        "first_name": "AHMED", "last_name": "KHAN",
        "passport_number": "A1234567", "date_of_birth": "1990-04-12",
        "nationality": "India", "gender": "Male",
        "phone_country_code": "971", "phone_number": "501234567",
        "email": "ahmed@example.com",
        "address_line_1": "FLAT 101, AL BARSHA TOWER",
        "address_line_2": "SHEIKH ZAYED ROAD", "city": "Dubai",
        "postcode": "00000", "country_code": "AE",
    }})

    route: str = Field(description="Route id, e.g. AE-NOR.")
    combo: str = Field(description="Combination label exactly as in "
                                   "config/routes/<ROUTE>.json (GET /routes).")
    date_from: str = Field(description="First acceptable date, YYYY-MM-DD, inclusive.")
    date_to: str = Field(description="Last acceptable date, YYYY-MM-DD, inclusive.")
    enabled: bool = Field(default=True, description="Armed on creation unless "
                                                    "false is sent.")
    account: Optional[str] = Field(default=None, description="VFS account to book "
                                   "under. Omit to use the shared booking account.")
    account_password: Optional[str] = Field(default=None, description="Stored, "
                                            "never returned.")


class BookingRequestCreate(BookingRequestFields):
    request_id: str = Field(description="Your id for this sale; becomes the "
                                        "filename. Lowercase slug, e.g. 'u10432-nor-1'.")


class BookingRequestPatch(BaseModel):
    """Only the fields sent change. The result is re-validated as a whole."""
    model_config = ConfigDict(extra="allow")


class ResolveBody(BaseModel):
    outcome: Literal["booked", "not_booked"] = Field(
        description="What you found on the VFS account. 'booked' records it "
                    "and never retries; 'not_booked' re-arms the request.")
    reason: Optional[str] = Field(default=None, max_length=280)
    appointment_date: Optional[str] = None
    appointment_time: Optional[str] = None
    reference: Optional[str] = None


class BookingRequestResponse(BaseModel):
    request_id: str
    status: str = Field(description="waiting | booking | booked | "
                                    "needs_attention | expired")
    enabled: bool
    message: str = ""
    request: Dict[str, Any] = Field(default_factory=dict,
                                    description="Redacted: password dropped, "
                                                "passport masked. Includes history.")


class BookingRequestListResponse(BaseModel):
    count: int
    requests: List[Dict[str, Any]]


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _view(req) -> Dict[str, Any]:
    out = _public_view(req.request_id, req.data)
    out.pop("client_id", None)
    out["request_id"] = req.request_id
    return out


def _response(req, message: str = "") -> BookingRequestResponse:
    return BookingRequestResponse(request_id=req.request_id, status=req.status,
                                  enabled=req.enabled, message=message,
                                  request=_view(req))


def _validate_or_422(request_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    from src.booking import requests as store

    record = store.normalise(data)
    problems = store.precheck(request_id, record)
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "booking_request_invalid",
                    "detail": f"{len(problems)} problem(s) with this request.",
                    "problems": [p.to_dict() for p in problems]})
    return record


def _get_or_404(request_id: str):
    from src.booking import requests as store
    try:
        return store.get(request_id)
    except (store.RequestNotFoundError, store.RequestError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=str(exc)) from exc


def _locked(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


# --------------------------------------------------------------------------- #
# Endpoints                                                                    #
# --------------------------------------------------------------------------- #


@router.post("", response_model=BookingRequestResponse,
             status_code=status.HTTP_201_CREATED)
def create_request(payload: BookingRequestCreate) -> BookingRequestResponse:
    """Store a booking request. Armed by default: from now on, a slot inside
    its window is booked and paid for automatically."""
    from src.booking import requests as store

    data = payload.model_dump(exclude_none=True)
    request_id = str(data.pop("request_id", "")).strip().lower()
    record = _validate_or_422(request_id, data)
    try:
        req = store.create(request_id, record)
    except store.RequestExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=str(exc)) from exc
    log.info("Booking request %s stored on %s (enabled=%s).",
             req.request_id, req.route, req.enabled)
    return _response(req, "Stored and ARMED: a slot inside the window will be "
                          "booked and paid automatically." if req.enabled else
                          "Stored and parked. POST .../enable to arm it.")


@router.get("", response_model=BookingRequestListResponse)
def list_requests(route: Optional[str] = Query(default=None),
                  status_filter: Optional[str] = Query(
                      default=None, alias="status",
                      description="waiting | booking | booked | "
                                  "needs_attention | expired")
                  ) -> BookingRequestListResponse:
    from src.booking import requests as store

    rows = store.list_all(route=route, status=status_filter)
    return BookingRequestListResponse(count=len(rows),
                                      requests=[_view(r) for r in rows])


@router.get("/{request_id}", response_model=BookingRequestResponse)
def get_request(request_id: str) -> BookingRequestResponse:
    return _response(_get_or_404(request_id))


def _replace(request_id: str, data: Dict[str, Any], message: str):
    from src.booking import requests as store

    record = _validate_or_422(request_id, data)
    try:
        req = store.replace_fields(request_id, record)
    except store.RequestLockedError as exc:
        raise _locked(exc) from exc
    return _response(req, message)


@router.put("/{request_id}", response_model=BookingRequestResponse)
def replace_request(request_id: str,
                    payload: BookingRequestFields) -> BookingRequestResponse:
    """Replace every caller-owned field. Refused while booking or once booked."""
    existing = _get_or_404(request_id)
    data = payload.model_dump(exclude_none=True)
    # A password is never sent back out, so a PUT that omits it keeps it.
    if data.get("account") == existing.data.get("account") and not data.get("account_password"):
        if existing.data.get("account_password"):
            data["account_password"] = existing.data["account_password"]
    return _replace(request_id, data, "Request replaced.")


@router.patch("/{request_id}", response_model=BookingRequestResponse)
def patch_request(request_id: str,
                  payload: BookingRequestPatch) -> BookingRequestResponse:
    """Change only the fields sent (e.g. new dates). Refused while booking."""
    from src.booking import requests as store

    existing = _get_or_404(request_id)
    patch = {k: v for k, v in payload.model_dump().items()
             if k not in store.STATE_KEYS and k != "request_id"}
    if not patch:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Empty patch — send at least one field.")
    merged = {k: v for k, v in existing.data.items() if k not in store.STATE_KEYS}
    merged.update(patch)
    return _replace(request_id, merged,
                    f"Request updated ({len(patch)} field(s)).")


@router.delete("/{request_id}")
def delete_request(request_id: str) -> Dict[str, Any]:
    from src.booking import requests as store

    _get_or_404(request_id)
    try:
        store.delete(request_id)
    except store.RequestLockedError as exc:
        raise _locked(exc) from exc
    return {"request_id": request_id, "deleted": True}


@router.post("/{request_id}/enable", response_model=BookingRequestResponse)
def enable_request(request_id: str) -> BookingRequestResponse:
    from src.booking import requests as store

    req = _get_or_404(request_id)
    # Re-validated on arming: a request parked for weeks may have dates that
    # have since passed, or a route that has since been switched off.
    _validate_or_422(request_id, {k: v for k, v in req.data.items()
                                  if k not in store.STATE_KEYS})
    try:
        req = store.set_enabled(request_id, True)
    except store.RequestLockedError as exc:
        raise _locked(exc) from exc
    return _response(req, "Armed.")


@router.post("/{request_id}/disable", response_model=BookingRequestResponse)
def disable_request(request_id: str) -> BookingRequestResponse:
    from src.booking import requests as store

    _get_or_404(request_id)
    try:
        req = store.set_enabled(request_id, False)
    except store.RequestLockedError as exc:
        raise _locked(exc) from exc
    return _response(req, "Parked. It will not be booked until enabled.")


@router.post("/{request_id}/resolve", response_model=BookingRequestResponse)
def resolve_request(request_id: str, body: ResolveBody) -> BookingRequestResponse:
    """Settle a needs_attention request AFTER checking the VFS account."""
    from src.booking import requests as store

    _get_or_404(request_id)
    details = {k: v for k, v in {
        "appointment_date": body.appointment_date,
        "appointment_time": body.appointment_time,
        "reference": body.reference}.items() if v}
    try:
        req = store.resolve(request_id, body.outcome, body.reason or "", details)
    except store.RequestLockedError as exc:
        raise _locked(exc) from exc
    return _response(req, f"Resolved as {body.outcome}.")
