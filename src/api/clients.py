"""API surface for creating and managing waitlist client files.

The web app owns client data; this is the endpoint set it drives. Everything
here is browser-free and returns quickly — no run is triggered by creating a
client (see /trigger/waitlist for that).

TWO SECURITY RULES, both load-bearing:

1. **Secrets go in, never out.** Client payloads now carry VFS account
   passwords (the web app supplies per-client credentials). They are persisted
   0600 and are NEVER echoed back in a response, NEVER logged, and reported
   only as a boolean "has_account_password". `_public_view()` is the single
   choke point — if a field would leak, it leaks there and nowhere else.

2. **Created ≠ armed.** A new client is written with `enabled: false` unless
   the caller explicitly asks otherwise. A bug in the web app must not be able
   to arm a fleet of clients for live registration. Enabling is a separate,
   deliberate call.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.api.schemas import (
    ClientCreateRequest,
    ClientDetailResponse,
    ClientListResponse,
    ClientSummary,
    ClientWriteResponse,
    ProblemModel,
    RouteReadinessResponse,
)
from src.api.security import require_token

log = logging.getLogger("vfs.api.clients")

router = APIRouter(prefix="/clients", tags=["clients"])

# Keys never returned to a caller, in any endpoint.
_SECRET_KEYS = frozenset({"account_password", "password", "proxy"})

# Keys shown only as a masked/derived value.
_MASKED_KEYS = frozenset({"passport_number", "date_of_birth", "email",
                          "phone_number"})


def _mask(value: Any) -> str:
    """Partially hide a PII value: keep enough to recognise, not to use."""
    text = str(value or "")
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


def _public_view(registrant_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """The ONLY shape client data may take in a response.

    Secrets are dropped entirely; PII is masked; everything else passes. New
    sensitive fields must be added to _SECRET_KEYS or _MASKED_KEYS — the
    default is to pass through, because most fields (first_name, nationality)
    are what the web app needs back to render its own UI.
    """
    out: Dict[str, Any] = {"client_id": registrant_id}
    for key, value in data.items():
        if key.startswith("_"):
            continue                      # config comments
        if key in _SECRET_KEYS:
            continue                      # never leaves the machine
        if key in _MASKED_KEYS:
            out[key] = _mask(value)
            continue
        out[key] = value
    # Report the presence of a pinned account without revealing the password.
    out["has_account_password"] = bool(data.get("account_password"))
    return out


def _problems_to_models(problems: List[Any]) -> List[ProblemModel]:
    """Convert validate.Problem records into response models."""
    return [ProblemModel(**p.to_dict()) for p in problems]


def _raise_validation(problems: List[Any]) -> None:
    """Reject a payload with the full problem list attached."""
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "error": "client_invalid",
            "detail": f"{len(problems)} problem(s) with this client.",
            "problems": [p.to_dict() for p in problems],
        },
    )


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@router.post(
    "",
    response_model=ClientWriteResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_token)],
)
async def create_client(payload: ClientCreateRequest) -> ClientWriteResponse:
    """Create a client file from the web app's data.

    Validates everything browser-free first — route readiness, combo existence,
    and that every {{placeholder}} the route's form needs is satisfied — and
    returns all problems at once on failure.

    The client is created **parked** (`enabled: false`) unless `enabled: true`
    is sent explicitly, so creating is never the same act as arming.
    """
    from src.waitlist import store, validate

    data = payload.to_client_data()
    client_id = payload.client_id

    problems = validate.precheck_client(client_id, data)
    if problems:
        log.info("Rejected client %r: %d problem(s).", client_id, len(problems))
        _raise_validation(problems)

    try:
        store.create(client_id, data)
    except store.ClientExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=str(exc)) from exc
    except Exception as exc:                       # noqa: BLE001
        log.exception("Failed to write client %r", client_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not write the client file: {exc}",
        ) from exc

    # Never log the payload itself — it holds a passport number and password.
    log.info("Created client %r on %s (enabled=%s).",
             client_id, data.get("route"), data.get("enabled"))

    return ClientWriteResponse(
        client_id=client_id,
        created=True,
        enabled=bool(data.get("enabled")),
        message=(
            "Client created. It is PARKED (enabled=false) — call "
            f"POST /clients/{client_id}/enable to arm it."
            if not data.get("enabled") else "Client created and enabled."
        ),
        client=_public_view(client_id, data),
    )


@router.get(
    "",
    response_model=ClientListResponse,
    dependencies=[Depends(require_token)],
)
async def list_clients(
    route: Optional[str] = Query(default=None, description="Filter by route id."),
) -> ClientListResponse:
    """List client ids, optionally filtered to one route."""
    from src.waitlist import store

    ids = store.list_ids(route=route)
    summaries: List[ClientSummary] = []
    for rid in ids:
        try:
            data = store.get_raw(rid)
        except Exception:                          # noqa: BLE001
            continue                               # skip unreadable, list the rest
        summaries.append(ClientSummary(
            client_id=rid,
            route=str(data.get("route", "")),
            combos=list(data.get("combos") or []),
            enabled=bool(data.get("enabled")),
        ))
    return ClientListResponse(count=len(summaries), clients=summaries)


@router.get(
    "/{client_id}",
    response_model=ClientDetailResponse,
    dependencies=[Depends(require_token)],
)
async def get_client(client_id: str) -> ClientDetailResponse:
    """One client, with secrets stripped and PII masked.

    Also re-runs the pre-flight, so the web app can show whether this client
    would actually register right now.
    """
    from src.waitlist import store, validate

    try:
        data = store.get_raw(client_id)
    except store.ClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=str(exc)) from exc

    problems = validate.precheck_client(client_id, data)
    return ClientDetailResponse(
        client_id=client_id,
        client=_public_view(client_id, data),
        runnable=not problems,
        problems=_problems_to_models(problems),
    )


@router.put(
    "/{client_id}",
    response_model=ClientWriteResponse,
    dependencies=[Depends(require_token)],
)
async def update_client(client_id: str,
                        payload: ClientCreateRequest) -> ClientWriteResponse:
    """Update an existing client.

    Refuses while a registration for this client is in flight — the data being
    typed into the portal must not change underneath the run.
    """
    from src.waitlist import journal, store, validate

    try:
        existing = store.get_raw(client_id)
    except store.ClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=str(exc)) from exc

    # An in-flight registration means the portal is being filled from this data
    # right now. Editing it mid-submit is how you get a mismatched entry.
    route = str(existing.get("route", "")).strip().upper()
    for combo in existing.get("combos") or []:
        blocking = journal.blocking_entry(route, combo, client_id)
        if blocking is not None and str(blocking.get("status")) == "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"A registration for '{client_id}' on '{combo}' is in "
                    "flight (status=pending). Wait for it to resolve, or clear "
                    "it with `python -m src.waitlist resolve`, before editing."
                ),
            )

    merged = dict(existing)
    incoming = payload.to_client_data()

    # `enabled` defaults to False on the request model, so an update that simply
    # does not mention it is indistinguishable from one asking to park the
    # client — and would SILENTLY DISARM a client you had already enabled.
    # An omitted field must preserve the current state; parking is done
    # deliberately, via /disable or by sending enabled=false explicitly.
    if "enabled" not in (payload.model_fields_set or set()):
        incoming.pop("enabled", None)

    merged.update(incoming)

    problems = validate.precheck_client(client_id, merged)
    if problems:
        _raise_validation(problems)

    store.update(client_id, merged, merge=False)
    log.info("Updated client %r.", client_id)

    return ClientWriteResponse(
        client_id=client_id,
        created=False,
        enabled=bool(merged.get("enabled")),
        message="Client updated.",
        client=_public_view(client_id, merged),
    )


@router.delete(
    "/{client_id}",
    dependencies=[Depends(require_token)],
)
async def delete_client(client_id: str) -> Dict[str, Any]:
    """Delete a client file."""
    from src.waitlist import store

    try:
        store.delete(client_id)
    except store.ClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=str(exc)) from exc
    return {"client_id": client_id, "deleted": True}


@router.post(
    "/{client_id}/enable",
    response_model=ClientWriteResponse,
    dependencies=[Depends(require_token)],
)
async def enable_client(client_id: str) -> ClientWriteResponse:
    """Arm a client for registration.

    Deliberately refuses to enable a client that would not run — arming
    something broken just moves the failure to 3am.
    """
    from src.waitlist import store, validate

    try:
        data = store.get_raw(client_id)
    except store.ClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=str(exc)) from exc

    problems = validate.precheck_client(client_id, data)
    if problems:
        _raise_validation(problems)

    store.set_enabled(client_id, True)
    data["enabled"] = True
    log.info("Enabled client %r.", client_id)
    return ClientWriteResponse(
        client_id=client_id, created=False, enabled=True,
        message="Client enabled — it will be included in waitlist runs.",
        client=_public_view(client_id, data),
    )


@router.post(
    "/{client_id}/disable",
    response_model=ClientWriteResponse,
    dependencies=[Depends(require_token)],
)
async def disable_client(client_id: str) -> ClientWriteResponse:
    """Park a client without deleting their data."""
    from src.waitlist import store

    try:
        data = store.get_raw(client_id)
    except store.ClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=str(exc)) from exc

    store.set_enabled(client_id, False)
    data["enabled"] = False
    log.info("Disabled client %r.", client_id)
    return ClientWriteResponse(
        client_id=client_id, created=False, enabled=False,
        message="Client parked — runs will skip them until re-enabled.",
        client=_public_view(client_id, data),
    )


# --------------------------------------------------------------------------
# Route readiness (not client-specific, but the web app needs it to build a form)
# --------------------------------------------------------------------------

routes_router = APIRouter(prefix="/routes", tags=["routes"])


@routes_router.get(
    "/{route}/readiness",
    response_model=RouteReadinessResponse,
    dependencies=[Depends(require_token)],
)
async def route_readiness(route: str) -> RouteReadinessResponse:
    """Can this route accept waitlist registrations, and what are its combos?

    The web app should call this before showing a signup form: it returns the
    valid combination labels to populate the dropdown, and says plainly when a
    route is not accepting registrations (and why).
    """
    from src.waitlist import validate

    readiness = validate.route_readiness(route)
    return RouteReadinessResponse(
        route=readiness.route,
        ready=readiness.ready,
        combos=readiness.combos,
        problems=_problems_to_models(readiness.problems),
    )
