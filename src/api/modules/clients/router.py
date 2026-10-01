"""Clients: one resource, two flows. The agent picks the flow.

    POST   /v1/clients                        create (flow: waitlist | live)
    GET    /v1/clients                        list (filter flow/route/status/enabled; paginated)
    GET    /v1/clients/{id}                   one client, with problems
    PUT    /v1/clients/{id}                   replace its fields
    PATCH  /v1/clients/{id}                   change some fields
    DELETE /v1/clients/{id}                   delete (refused mid-booking)
    POST   /v1/clients/{id}/enable            arm
    POST   /v1/clients/{id}/disable           park
    GET    /v1/clients/{id}/timeline          everything that happened, newest first
    POST   /v1/clients/{id}/resolve           settle a live client in needs_attention
    GET    /v1/clients/{id}/journal           waitlist registration history
    GET    /v1/clients/{id}/documents         uploaded documents (waitlist)
    POST   /v1/clients/{id}/documents         upload one (passport scan)
    DELETE /v1/clients/{id}/documents         delete them

flow=waitlist  joins the VFS waitlist; books when VFS emails an invitation.
flow=live      books a live slot inside date_from..date_to (and pays).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from src.api.modules.clients import live as live
from src.api.modules.clients import waitlist as waitlist
from src.api.core.errors import ERROR_RESPONSES, ApiError
from src.api.core.pagination import PageParams, paginate
from src.api.modules.clients import service
from src.api.core.security import require_token

router = APIRouter(prefix="/clients", tags=["clients"],
                   dependencies=[Depends(require_token)], responses=ERROR_RESPONSES)


class ClientCreate(BaseModel):
    """Extra keys are the applicant's form fields; a 422 names any missing."""

    model_config = ConfigDict(extra="allow", json_schema_extra={"examples": [
        {"client_id": "u10432-nor-1", "flow": "live", "route": "AE-NOR",
         "combo": "Norway Visa Application Center - Dubai - Tourist",
         "date_from": "2026-10-10", "date_to": "2026-10-20", "enabled": False,
         "first_name": "AHMED", "last_name": "KHAN", "passport_number": "A1234567",
         "date_of_birth": "1990-04-12", "nationality": "India", "gender": "Male",
         "phone_country_code": "971", "phone_number": "501234567",
         "email": "ahmed@example.com", "address_line_1": "FLAT 101",
         "address_line_2": "AL BARSHA", "city": "Dubai", "postcode": "00000"},
        {"client_id": "u10432-che", "flow": "waitlist", "route": "AE-CHE",
         "combos": ["Dubai - SCHENGEN"], "enabled": False,
         "first_name": "AHMED", "last_name": "KHAN", "email": "ahmed@example.com",
         "account": "waitlist-acc1@example.com", "account_password": "the-password"},
    ]})

    client_id: str = Field(description="Lowercase slug, unique across flows.")
    flow: str = Field(description="waitlist | live")
    route: str


@router.post("", status_code=201, summary="Create a client (pick the flow)")
def create(body: ClientCreate) -> Dict[str, Any]:
    return service.create(body.model_dump())


@router.get("", summary="List clients across both flows")
def list_(page: PageParams = Depends(),
          flow: Optional[str] = Query(default=None, description="waitlist | live"),
          route: Optional[str] = None,
          status: Optional[str] = Query(default=None, description=(
              "waiting | registered | invited | booking | booked | "
              "needs_attention | expired")),
          enabled: Optional[bool] = None,
          include: Optional[str] = Query(default=None, description=(
              "'problems' adds runnable + problem_count per client (runs each "
              "client's pre-flight, so it costs work per client).")),
          runnable: Optional[bool] = Query(default=None, description=(
              "Only clients that would (true) or would not (false) run now. "
              "Implies include=problems."))) -> Dict[str, Any]:
    if flow and flow not in service.FLOWS:
        raise ApiError(422, "flow must be 'waitlist' or 'live'.")
    wanted = {p.strip() for p in (include or "").split(",") if p.strip()}
    if wanted - {"problems"}:
        raise ApiError(422, "include accepts only 'problems'.")
    return paginate(service.list_clients(flow, route, status, enabled,
                                         include_problems="problems" in wanted,
                                         runnable=runnable), page)


@router.get("/{client_id}", summary="One client")
def get(client_id: str) -> Dict[str, Any]:
    return service.detail(client_id)


@router.put("/{client_id}", summary="Replace a client's fields")
def replace(client_id: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    return service.update(client_id, body, partial=False)


@router.patch("/{client_id}", summary="Change some fields")
def patch(client_id: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    if not body:
        raise ApiError(422, "Empty patch — send at least one field.")
    return service.update(client_id, body, partial=True)


@router.delete("/{client_id}", summary="Delete a client")
def delete(client_id: str) -> Dict[str, Any]:
    return service.delete(client_id)


@router.post("/{client_id}/enable", summary="Arm")
def enable(client_id: str) -> Dict[str, Any]:
    return service.set_enabled(client_id, True)


@router.post("/{client_id}/disable", summary="Park")
def disable(client_id: str) -> Dict[str, Any]:
    return service.set_enabled(client_id, False)


@router.get("/{client_id}/timeline", summary="Everything that happened, newest first")
def timeline(client_id: str, page: PageParams = Depends()) -> Dict[str, Any]:
    return paginate(service.timeline(client_id), page)


@router.post("/{client_id}/resolve", summary="Settle a live client after checking VFS")
def resolve(client_id: str, body: live.ResolveBody,
            request: Request) -> Dict[str, Any]:
    if service.require_flow(client_id) != service.LIVE:
        raise ApiError(422, "Waitlist clients are settled per submit: use "
                            "POST /v1/waitlist/dangling/resolve, or for an "
                            "invitation POST /v1/booking/invitations/{key}/resolve.")
    live.resolve_request(client_id, body)
    return service.detail(client_id)


def _waitlist_only(client_id: str) -> None:
    if service.require_flow(client_id) != service.WAITLIST:
        raise ApiError(422, "Only waitlist clients have documents and a "
                            "registration journal.")


@router.get("/{client_id}/journal", summary="Waitlist registration history")
def journal(client_id: str):
    _waitlist_only(client_id)
    return waitlist.get_client_journal(client_id)


@router.get("/{client_id}/documents", summary="Uploaded documents")
async def list_documents(client_id: str):
    _waitlist_only(client_id)
    return await waitlist.list_documents(client_id)


router.post("/{client_id}/documents", status_code=201,
            summary="Upload a document (passport scan)")(waitlist.upload_document)


@router.delete("/{client_id}/documents", summary="Delete documents")
async def delete_documents(client_id: str):
    _waitlist_only(client_id)
    return await waitlist.delete_documents(client_id)
