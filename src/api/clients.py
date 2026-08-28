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
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)

from src.api.schemas import (
    ClientCreateRequest,
    ClientDetailResponse,
    ClientListResponse,
    ClientPatchRequest,
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


def _refuse_if_in_flight(client_id: str, existing: Dict[str, Any]) -> None:
    """409 if a registration for this client is mid-submit.

    An in-flight registration means the portal is being filled from this data
    right now. Editing it mid-submit is how you get a mismatched entry — the
    form says one passport number and the journal records another.
    """
    from src.waitlist import journal

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
def create_client(payload: ClientCreateRequest) -> ClientWriteResponse:
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
def list_clients(
    route: Optional[str] = Query(default=None, description="Filter by route id."),
    include: Optional[str] = Query(
        default=None,
        description="Comma-separated extras to compute: 'status' adds runnable "
                    "+ problem_count, 'journal' adds the last run outcome and "
                    "VFS reference. Both cost work per client, so neither is "
                    "on by default. Use 'all' for everything.",
        examples=["status", "status,journal", "all"],
    ),
    enabled: Optional[bool] = Query(
        default=None,
        description="Only clients that are armed (true) or parked (false).",
    ),
    runnable: Optional[bool] = Query(
        default=None,
        description="Only clients that would (true) or would not (false) run "
                    "right now. Implies include=status, since it needs the "
                    "same pre-flight.",
    ),
) -> ClientListResponse:
    """List clients, with optional per-client state.

    The default response stays deliberately cheap — a file read per client and
    nothing more — because it is what a simple picker needs. A dashboard wants
    more, and `?include=` is how it gets that in ONE call instead of N+1: one
    request for the list, then one GET per client to learn whether any of them
    are broken.
    """
    from src.waitlist import journal, store

    wanted = {p.strip().lower() for p in (include or "").split(",") if p.strip()}
    if "all" in wanted:
        wanted = {"status", "journal"}
    # Filtering on runnable requires the pre-flight, so asking for the filter
    # is asking for the field — quietly computing it beats a confusing 422.
    if runnable is not None:
        wanted.add("status")

    summaries: List[ClientSummary] = []
    # Read the whole history ONCE and bucket it, rather than asking each client
    # for its own and re-reading the journal N times for the same answer.
    history = journal.summarise_by_client() if "journal" in wanted else {}

    for rid in store.list_ids(route=route):
        try:
            data = store.get_raw(rid)
        except Exception:                          # noqa: BLE001
            continue                               # skip unreadable, list the rest

        is_enabled = bool(data.get("enabled"))
        if enabled is not None and is_enabled != enabled:
            continue

        row = ClientSummary(
            client_id=rid,
            route=str(data.get("route", "")),
            combos=list(data.get("combos") or []),
            enabled=is_enabled,
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            enabled_at=str(data.get("enabled_at") or ""),
        )

        if "status" in wanted:
            from src.waitlist import validate

            problems = validate.precheck_client(rid, data)
            row.runnable = not problems
            row.problem_count = len(problems)
            if runnable is not None and row.runnable != runnable:
                continue

        if "journal" in wanted:
            summary = history.get(rid) or {}
            row.last_status = summary.get("last_status")
            row.last_run_at = summary.get("last_run_at")
            row.vfs_reference = summary.get("vfs_reference")
            row.run_count = int(summary.get("run_count") or 0)

        summaries.append(row)

    return ClientListResponse(count=len(summaries), clients=summaries,
                              included=sorted(wanted))


@router.get(
    "/{client_id}",
    response_model=ClientDetailResponse,
    dependencies=[Depends(require_token)],
)
def get_client(client_id: str) -> ClientDetailResponse:
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
def update_client(client_id: str,
                  payload: ClientCreateRequest) -> ClientWriteResponse:
    """REPLACE a client. Fields you do not send are REMOVED.

    True PUT semantics: the stored client becomes exactly what you send. This
    is the only way to delete a field (drop `account` and the client falls back
    to the shared account), which merge semantics cannot express.

    Use PATCH when you want to change one field and leave the rest alone —
    sending a partial body here silently discards everything you omitted.

    Refuses while a registration for this client is in flight — the data being
    typed into the portal must not change underneath the run.
    """
    from src.waitlist import store, validate

    try:
        existing = store.get_raw(client_id)
    except store.ClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=str(exc)) from exc

    _refuse_if_in_flight(client_id, existing)

    replacement = payload.to_client_data()

    # PUT replaces, so an omitted `enabled` genuinely means False — the request
    # model's default. That is safe in the disarming direction (a client is
    # parked, not armed) and is exactly what "replace" should mean.
    problems = validate.precheck_client(client_id, replacement)
    if problems:
        _raise_validation(problems)

    store.update(client_id, replacement, merge=False)
    removed = sorted(set(existing) - set(replacement) - {"_comment"})
    log.info("Replaced client %r (%d field(s) removed).", client_id, len(removed))

    return ClientWriteResponse(
        client_id=client_id,
        created=False,
        enabled=bool(replacement.get("enabled")),
        message=(
            "Client replaced. Removed field(s): " + ", ".join(removed)
            if removed else "Client replaced."
        ),
        client=_public_view(client_id, replacement),
    )


@router.patch(
    "/{client_id}",
    response_model=ClientWriteResponse,
    dependencies=[Depends(require_token)],
)
def patch_client(client_id: str,
                 payload: ClientPatchRequest) -> ClientWriteResponse:
    """Partially update a client: only the fields you send are changed.

    This is what a web app's "edit one thing" form wants. An omitted field
    keeps its current value — including `enabled`, so editing a phone number
    can never silently disarm an armed client.

    Refuses while a registration for this client is in flight.
    """
    from src.waitlist import store, validate

    try:
        existing = store.get_raw(client_id)
    except store.ClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=str(exc)) from exc

    _refuse_if_in_flight(client_id, existing)

    patch = payload.to_patch()
    if not patch:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Empty patch — send at least one field to change.",
        )

    merged = dict(existing)
    merged.update(patch)

    problems = validate.precheck_client(client_id, merged)
    if problems:
        _raise_validation(problems)

    store.update(client_id, merged, merge=False)
    # Field NAMES only — the values include a passport number and a password.
    log.info("Patched client %r: %s", client_id, ", ".join(sorted(patch)))

    return ClientWriteResponse(
        client_id=client_id,
        created=False,
        enabled=bool(merged.get("enabled")),
        message=f"Client updated ({len(patch)} field(s) changed).",
        client=_public_view(client_id, merged),
    )


@router.delete(
    "/{client_id}",
    dependencies=[Depends(require_token)],
)
def delete_client(client_id: str) -> Dict[str, Any]:
    """Delete a client file, and any documents held for them."""
    from src.waitlist import documents, store

    try:
        store.delete(client_id)
    except store.ClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=str(exc)) from exc

    # Documents live OUTSIDE the client file, so deleting the client did not
    # touch them — a passport scan would have outlived the record of whose it
    # was, which is the exact liability documents.py exists to avoid. Nothing
    # references it any more, so there is no case for keeping it.
    #
    # Best-effort: a cleanup failure must not turn a successful delete into an
    # error the caller retries. The retention sweep catches the remainder.
    removed = 0
    try:
        removed = documents.delete_for(client_id, reason="client deleted")
    except Exception as exc:                       # noqa: BLE001
        log.warning("Could not delete documents for %r: %s", client_id, exc)

    return {"client_id": client_id, "deleted": True, "documents_removed": removed}


@router.post(
    "/{client_id}/enable",
    response_model=ClientWriteResponse,
    dependencies=[Depends(require_token)],
)
def enable_client(client_id: str) -> ClientWriteResponse:
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
def disable_client(client_id: str) -> ClientWriteResponse:
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
# Documents
# --------------------------------------------------------------------------
#
# Some portals do not ask for typed details at all: Italy wants the passport
# bio page uploaded and OCRs the fields out of it. So a route can require a
# FILE, and until now there was no way to get one in through the API — a web
# app could create an AE-ITA client that could never actually register.
#
# The bytes are validated BEFORE they are stored: written to a temp file, run
# through documents.validate() (extension, size, and magic-byte sniffing that
# catches a .png which is really something else), and only then handed to
# documents.store(). A rejected upload leaves nothing behind.
#
# Documents live OUTSIDE the repo (per-user app data) and are deleted the
# moment a registration is confirmed — a passport scan kept afterwards is pure
# liability. See src/waitlist/documents.py for that reasoning.


@router.post(
    "/{client_id}/documents",
    dependencies=[Depends(require_token)],
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    client_id: str,
    file: UploadFile = File(..., description="PNG, JPG or PDF. Max 2 MB."),
    kind: str = Form(default="passport_bio",
                     description="Document kind. Only passport_bio today."),
) -> Dict[str, Any]:
    """Attach a document to a client, for routes whose form requires an upload.

    Call this when `GET /routes/{route}/readiness` reports a field with
    `"kind": "file"`. After uploading, set that field's value in the client to
    the sentinel `"managed"` so the run looks the document up in the store.

    Never returns the stored path: it is a location on the bot's filesystem and
    a caller has no use for it.
    """
    from src.waitlist import documents, store

    # The client must exist first: storing a document for a typo'd id would
    # leave an orphaned passport scan on disk that nothing ever cleans up.
    try:
        store.get_raw(client_id)
    except store.ClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=str(exc)) from exc

    if kind != documents.PASSPORT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown document kind {kind!r}. Expected "
                   f"{documents.PASSPORT!r}.",
        )

    # Read with a hard ceiling rather than trusting Content-Length: the
    # advertised size and the delivered bytes need not agree, and this endpoint
    # is reachable through a public tunnel.
    limit = documents.MAX_BYTES
    payload = await file.read(limit + 1)
    await file.close()
    if len(payload) > limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {limit // 1024 // 1024} MB portal limit.",
        )
    if not payload:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="The uploaded file is empty.")

    # Keep the caller's extension only as a CLAIM — validate() sniffs the magic
    # bytes and rejects a mismatch. The name itself is never used as a path.
    suffix = Path(str(file.filename or "")).suffix.lower()
    if suffix not in documents.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{suffix or 'no extension'}' is not accepted. VFS takes "
                   f"{', '.join(documents.ALLOWED_EXTENSIONS)} only.",
        )

    tmp_path = ""
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)

        documents.validate(tmp_path, label=f"{kind} for '{client_id}'")
        stored = documents.store(client_id, tmp_path, kind=kind)
    except HTTPException:
        raise
    except Exception as exc:                       # noqa: BLE001
        # documents.validate raises WaitlistStepError with an actionable
        # message ("named .png but the contents are .pdf"). Pass it through —
        # a generic 500 would hide the one thing the caller can act on.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=str(exc)) from exc
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                log.warning("Could not remove temp upload %s", tmp_path)

    log.info("Stored %s for %r (%d bytes).", kind, client_id, len(payload))
    return {
        "client_id": client_id,
        "kind": kind,
        "stored": True,
        "size_bytes": len(payload),
        "extension": Path(stored).suffix,
        "message": (
            f"Document stored. Set this client's file field to \"managed\" so "
            "the run finds it, then re-check GET /clients/"
            f"{client_id} for runnable."
        ),
    }


@router.get(
    "/{client_id}/documents",
    dependencies=[Depends(require_token)],
)
async def list_documents(client_id: str) -> Dict[str, Any]:
    """What documents are held for this client. Never returns the file itself.

    Metadata only — the point is to let a web app show "passport uploaded"
    without the bytes ever leaving the machine again.
    """
    from src.waitlist import documents, store

    try:
        store.get_raw(client_id)
    except store.ClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=str(exc)) from exc

    held: List[Dict[str, Any]] = []
    path = documents.path_for(client_id, documents.PASSPORT)
    if path and os.path.isfile(path):
        held.append({
            "kind": documents.PASSPORT,
            "extension": Path(path).suffix,
            "size_bytes": os.path.getsize(path),
        })
    return {"client_id": client_id, "count": len(held), "documents": held}


@router.delete(
    "/{client_id}/documents",
    dependencies=[Depends(require_token)],
)
async def delete_documents(client_id: str) -> Dict[str, Any]:
    """Remove every document held for a client.

    Also happens automatically on a confirmed registration — this is for a
    client who withdraws, or an upload that turned out to be the wrong file.
    """
    from src.waitlist import documents

    removed = documents.delete_for(client_id, reason="deleted via API")
    return {"client_id": client_id, "removed": removed}


# --------------------------------------------------------------------------
# Route readiness (not client-specific, but the web app needs it to build a form)
# --------------------------------------------------------------------------

routes_router = APIRouter(prefix="/routes", tags=["routes"])


@routes_router.get(
    "/{route}/readiness",
    response_model=RouteReadinessResponse,
    dependencies=[Depends(require_token)],
)
def route_readiness(route: str) -> RouteReadinessResponse:
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
        # Returned even when ready=False: the web app can still render and
        # validate the form while a route is being brought online, and a caller
        # debugging "why won't this route accept anyone" benefits from seeing
        # what it would ask for.
        fields=validate.required_fields(readiness.route),
        problems=_problems_to_models(readiness.problems),
    )
