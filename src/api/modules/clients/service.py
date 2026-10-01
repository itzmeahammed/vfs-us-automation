"""ONE client resource over the two stores the flows keep.

    flow = "waitlist"   config/registrants/<id>.json     Flow 1 (+ Flow 2)
    flow = "live"       config/booking_requests/<id>.json Flow 3

A sales agent adds a client and picks how they get an appointment; the web app
should not need to know that the two flows keep their records in different
places. This module routes every operation to the right store, by id, and
presents both in one shape:

    {client_id, flow, route, combos, enabled, status, created_at, updated_at,
     details: {...redacted fields...}, problems: [...]}

One status vocabulary across both flows:

    waiting          armed, nothing has happened yet
    registered       on the VFS waitlist (waitlist flow)
    invited          VFS emailed an invitation; booking pending (waitlist flow)
    booking          a booking run is in flight
    booked           appointment booked
    needs_attention  a human must check the VFS account (may have booked/paid)
    expired          the window passed unbooked
    parked           enabled = false (reported in `enabled`, not here)

The validation, writing and redaction stay in the flows' own handlers
(waitlist.py and live.py in this package) — they are tested and they
are the rules. This only dispatches, and refuses an id both stores could claim.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.api.core.errors import ApiError

WAITLIST = "waitlist"
LIVE = "live"
FLOWS = (WAITLIST, LIVE)


def flow_of(client_id: str) -> Optional[str]:
    from src.booking import requests as req_store
    from src.waitlist import store

    cid = str(client_id or "").strip().lower()
    try:
        if req_store.exists(cid):
            return LIVE
    except Exception:                                       # noqa: BLE001
        pass
    try:
        if store.exists(cid):
            return WAITLIST
    except Exception:                                       # noqa: BLE001
        pass
    return None


def require_flow(client_id: str) -> str:
    flow = flow_of(client_id)
    if flow is None:
        raise ApiError(404, f"No client {client_id!r}.")
    return flow


# --------------------------------------------------------------------------- #
# Status                                                                       #
# --------------------------------------------------------------------------- #


def _invite_states() -> Dict[str, str]:
    """client id -> the most advanced invitation state for them."""
    from src.booking import invites

    rank = {"booked": 4, "needs_attention": 3, "booking": 2, "pending": 1}
    out: Dict[str, str] = {}
    for item in invites.list_all():
        ids = set((item.get("clients") or {}).keys())
        for entry in item.get("history") or []:
            ids.update(entry.get("clients_found") or [])
        state = item.get("status", "")
        for cid in ids:
            per_client = ((item.get("clients") or {}).get(cid) or {}).get("status")
            current = per_client or state
            if rank.get(current, 0) > rank.get(out.get(cid, ""), 0):
                out[cid] = current
    return out


def waitlist_status(summary: Dict[str, Any], invite_state: str = "") -> str:
    if invite_state == "booked":
        return "booked"
    if invite_state == "needs_attention":
        return "needs_attention"
    if invite_state in ("pending", "booking"):
        return "invited" if invite_state == "pending" else "booking"
    last = (summary or {}).get("last_status")
    if last == "success":
        return "registered"
    if last in ("pending", "unknown"):
        return "needs_attention"
    return "waiting"


# --------------------------------------------------------------------------- #
# Listing                                                                      #
# --------------------------------------------------------------------------- #


def list_clients(flow: Optional[str] = None, route: Optional[str] = None,
                 status: Optional[str] = None,
                 enabled: Optional[bool] = None,
                 include_problems: bool = False,
                 runnable: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Both flows, newest first.

    `include_problems` runs each client's full pre-flight and adds `runnable`
    and `problem_count` — the dashboard's "which clients are broken" in ONE
    call instead of a GET per client. Off by default: it is real work per
    client, and a picker does not need it. Filtering on `runnable` implies it.
    """
    include_problems = include_problems or runnable is not None
    from src.booking import requests as req_store
    from src.waitlist import journal, store

    rows: List[Dict[str, Any]] = []
    route = route.upper() if route else None

    if flow in (None, WAITLIST):
        history = journal.summarise_by_client()
        invite_state = _invite_states()
        for cid in store.list_ids(route=route):
            try:
                data = store.get_raw(cid)
            except Exception:                               # noqa: BLE001
                continue
            rows.append({
                "client_id": cid, "flow": WAITLIST,
                "route": str(data.get("route", "")),
                "combos": list(data.get("combos") or []),
                "enabled": bool(data.get("enabled")),
                "status": waitlist_status(history.get(cid), invite_state.get(cid, "")),
                "name": _name(data),
                "created_at": str(data.get("created_at") or ""),
                "updated_at": str(data.get("updated_at") or ""),
                "vfs_reference": (history.get(cid) or {}).get("vfs_reference"),
                "last_status": (history.get(cid) or {}).get("last_status"),
                "last_run_at": (history.get(cid) or {}).get("last_run_at"),
                "run_count": int((history.get(cid) or {}).get("run_count") or 0),
                "runnable": None, "problem_count": None,
            })
            if include_problems:
                from src.waitlist import validate
                errors = [p for p in validate.precheck_client(cid, data)
                          if getattr(p, "severity", "error") == "error"]
                rows[-1].update(runnable=not errors, problem_count=len(errors))

    if flow in (None, LIVE):
        for req in req_store.list_all(route=route):
            start, end = req.window()
            rows.append({
                "client_id": req.request_id, "flow": LIVE,
                "route": req.route, "combos": [req.combo],
                "enabled": req.enabled, "status": req.status,
                "name": _name(req.data),
                "created_at": req.created_at,
                "updated_at": str(req.data.get("updated_at") or ""),
                "date_from": str(start or ""), "date_to": str(end or ""),
                "test_mode": bool(req.data.get("test_mode")),
                "appointment": req.data.get("booked") or None,
                "runnable": None, "problem_count": None,
            })
            if include_problems:
                fields = {k: v for k, v in req.data.items()
                          if k not in req_store.STATE_KEYS}
                errors = req_store.precheck(req.request_id, fields)
                rows[-1].update(runnable=not errors, problem_count=len(errors))

    if status:
        rows = [r for r in rows if r["status"] == status]
    if enabled is not None:
        rows = [r for r in rows if r["enabled"] == enabled]
    if runnable is not None:
        rows = [r for r in rows if r["runnable"] == runnable]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def _name(data: Dict[str, Any]) -> str:
    return " ".join(str(data.get(k) or "") for k in ("first_name", "last_name")).strip()


# --------------------------------------------------------------------------- #
# One client                                                                   #
# --------------------------------------------------------------------------- #


def detail(client_id: str) -> Dict[str, Any]:
    from src.api.modules.clients import live as live
    from src.api.modules.clients import waitlist as waitlist

    cid = client_id.strip().lower()
    flow = require_flow(cid)
    row = next((r for r in list_clients(flow=flow) if r["client_id"] == cid), {})
    if flow == WAITLIST:
        resp = waitlist.get_client(cid)
        details, problems = resp.client, [p.model_dump() for p in resp.problems]
        runnable = resp.runnable
    else:
        resp = live.get_request(cid)
        details, problems, runnable = resp.request, [], resp.enabled
    details = {k: v for k, v in details.items()
               if k not in ("history", "client_id", "request_id")}
    return {**row, "client_id": cid, "flow": flow, "runnable": runnable,
            "details": details, "problems": problems}


def _model(cls, **data):
    """Build a request model, turning its validation errors into a 422.

    The router accepts any body (each flow has its own rules), so the flow's
    model is built HERE — and a ValidationError raised here is not FastAPI's
    request validation. Unconverted, an id like '../escape' came back as a
    500 instead of a 422 naming the field.
    """
    from pydantic import ValidationError

    try:
        return cls(**data)
    except ValidationError as exc:
        problems = [{"field": ".".join(str(p) for p in err.get("loc", ())),
                     "message": str(err.get("msg", "")).removeprefix("Value error, ")}
                    for err in exc.errors()]
        raise ApiError(422, f"{len(problems)} problem(s) with this client.",
                       "validation_error", problems) from exc


def _with_outcome(client_id: str, response: Any) -> Dict[str, Any]:
    """The client as it now is, plus what the write had to say.

    `message` is the human sentence ("Client created. It is PARKED ...") and
    `warnings` the NON-blocking findings — e.g. a form email that differs from
    the VFS account, so the invitation lands in a mailbox nobody watches. The
    write succeeded; these are what the agent should still look at.
    """
    out = detail(client_id)
    out["message"] = getattr(response, "message", "") or ""
    out["warnings"] = [w.model_dump() if hasattr(w, "model_dump") else w
                       for w in (getattr(response, "warnings", None) or [])]
    return out


def create(payload: Dict[str, Any]) -> Dict[str, Any]:
    from src.api.modules.clients import live as live
    from src.api.modules.clients import waitlist as waitlist
    from src.api.modules.clients.schemas import ClientCreateRequest

    data = dict(payload)
    flow = data.pop("flow", None)
    cid = str(data.get("client_id") or "").strip().lower()
    if flow not in FLOWS:
        raise ApiError(422, "flow must be 'waitlist' or 'live'.", problems=[
            {"field": "flow", "message": "waitlist: join the VFS waitlist and book "
                                         "when invited. live: book a live slot "
                                         "inside date_from..date_to."}])
    if cid and flow_of(cid):
        raise ApiError(409, f"Client {cid!r} already exists "
                            f"(flow={flow_of(cid)}). Ids are unique across flows.")

    if flow == WAITLIST:
        response = waitlist.create_client(_model(ClientCreateRequest, **data))
    else:
        data["request_id"] = data.pop("client_id", "")
        response = live.create_request(_model(live.BookingRequestCreate, **data))
    return _with_outcome(cid, response)


def update(client_id: str, payload: Dict[str, Any], partial: bool) -> Dict[str, Any]:
    from src.api.modules.clients import live as live
    from src.api.modules.clients import waitlist as waitlist
    from src.api.modules.clients.schemas import ClientCreateRequest, ClientPatchRequest

    cid = client_id.strip().lower()
    flow = require_flow(cid)
    data = {k: v for k, v in payload.items() if k not in ("client_id", "request_id")}
    if data.pop("flow", flow) != flow:
        raise ApiError(422, f"Client {cid!r} is flow={flow}; a flow cannot be "
                            "changed. Delete it and create it again.")
    if flow == WAITLIST:
        if partial:
            response = waitlist.patch_client(cid, _model(ClientPatchRequest, **data))
        else:
            response = waitlist.update_client(cid, _model(ClientCreateRequest, client_id=cid, **data))
    else:
        if partial:
            response = live.patch_request(cid, _model(live.BookingRequestPatch, **data))
        else:
            response = live.replace_request(cid, _model(live.BookingRequestFields, **data))
    return _with_outcome(cid, response)


def set_enabled(client_id: str, enabled: bool) -> Dict[str, Any]:
    from src.api.modules.clients import live as live
    from src.api.modules.clients import waitlist as waitlist

    cid = client_id.strip().lower()
    flow = require_flow(cid)
    if flow == WAITLIST:
        response = (waitlist.enable_client if enabled else waitlist.disable_client)(cid)
    else:
        response = (live.enable_request if enabled else live.disable_request)(cid)
    return _with_outcome(cid, response)


def delete(client_id: str) -> Dict[str, Any]:
    from src.api.modules.clients import live as live
    from src.api.modules.clients import waitlist as waitlist

    cid = client_id.strip().lower()
    flow = require_flow(cid)
    if flow == WAITLIST:
        outcome = waitlist.delete_client(cid)
    else:
        outcome = live.delete_request(cid)
    # The flow's own report rides along — e.g. documents_removed, so the web
    # app can confirm a passport scan went with the client.
    extra = {k: v for k, v in (outcome or {}).items()
             if k not in ("client_id", "request_id")}
    return {**extra, "client_id": cid, "flow": flow, "deleted": True}


# --------------------------------------------------------------------------- #
# Timeline: everything that happened to one client, across every record        #
# --------------------------------------------------------------------------- #


def timeline(client_id: str) -> List[Dict[str, Any]]:
    from src.booking import invites, requests as req_store

    cid = client_id.strip().lower()
    flow = require_flow(cid)
    events: List[Dict[str, Any]] = []

    if flow == LIVE:
        for entry in req_store.get(cid).data.get("history") or []:
            events.append({"at": entry.get("at", ""), "source": "booking_request",
                           **{k: v for k, v in entry.items() if k != "at"}})
    else:
        from src.api.modules.clients import waitlist as waitlist
        for row in waitlist.get_client_journal(cid).rows:
            events.append({"at": row.finished_at or row.started_at or "",
                           "source": "waitlist_journal", "event": row.status,
                           "detail": f"{row.route} / {row.combo}"
                                     + (f": {row.reason}" if row.reason else ""),
                           "vfs_reference": row.vfs_reference})
        for item in invites.list_all():
            mentioned = cid in (item.get("clients") or {}) or any(
                cid in (e.get("clients_found") or []) for e in item.get("history") or [])
            if not mentioned:
                continue
            for entry in item.get("history") or []:
                events.append({"at": entry.get("at", ""), "source": "invitation",
                               "invitation": item["key"],
                               **{k: v for k, v in entry.items() if k != "at"}})

    try:
        from src.api.main import job_manager
        for record in job_manager.recent(500):
            p = record.payload or {}
            if cid in (p.get("registrant"), p.get("request_id")):
                events.append({"at": record.started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "source": "job", "event": record.status.value,
                               "job_id": record.job_id,
                               "detail": f"{p.get('lane', '')} {p.get('mode', '')}".strip()})
    except Exception:                                       # noqa: BLE001
        pass

    # Newest first. Timestamps are per second, so two events in one second
    # tie — the order they were recorded in breaks the tie, otherwise "edited"
    # and "enabled" a moment apart come back in the wrong order.
    ordered = sorted(enumerate(events),
                     key=lambda pair: (str(pair[1].get("at") or ""), pair[0]),
                     reverse=True)
    return [event for _, event in ordered]
