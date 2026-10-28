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

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.api.core.errors import ApiError
from src.api.core import search
from src.utils import public_errors

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
    # What the client HOLDS on the portal decides, not the latest row: a later
    # skip, dry run or failure on another combo does not undo a registration.
    held = (summary or {}).get("holding_status")
    if held in ("pending", "unknown"):
        return "needs_attention"
    if held == "success":
        return "registered"
    return "waiting"


# --------------------------------------------------------------------------- #
# Listing                                                                      #
# --------------------------------------------------------------------------- #


def list_clients(flow: Optional[str] = None, route: Optional[str] = None,
                 status: Optional[str] = None,
                 enabled: Optional[bool] = None,
                 include_problems: bool = False,
                 runnable: Optional[bool] = None,
                 q: Optional[str] = None) -> List[Dict[str, Any]]:
    """Both flows, newest first.

    `include_problems` runs each client's full pre-flight and adds `runnable`
    and `problem_count` — the dashboard's "which clients are broken" in ONE
    call instead of a GET per client. Off by default: it is real work per
    client, and a picker does not need it. Filtering on `runnable` implies it.

    Runs maintenance first, so an expired window or a booking whose process
    died is already settled in what this returns.

    `q` is a free-text match over the fields a list row ALREADY carries — see
    `_SEARCHABLE`. It deliberately cannot reach a passport number, date of
    birth, email or phone: `detail()` masks those and a list row never holds
    them at all, so letting `q` match them would turn this endpoint into an
    oracle for confirming a guessed value the API refuses to show.

    `q` is applied BEFORE the pre-flight, so searching a large fleet costs one
    cheap string match per client rather than a full validation per client.
    """
    _maintain()
    include_problems = include_problems or runnable is not None
    from src.booking import requests as req_store
    from src.waitlist import journal, store

    rows: List[Dict[str, Any]] = []
    route = route.upper() if route else None
    needle = search.terms(q)

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
                "registered_combo": (history.get(cid) or {}).get("holding_combo"),
                "last_status": (history.get(cid) or {}).get("last_status"),
                "last_run_at": (history.get(cid) or {}).get("last_run_at"),
                "run_count": int((history.get(cid) or {}).get("run_count") or 0),
                "runnable": None, "problem_count": None,
            })
            if not search.matches(rows[-1], needle, _SEARCHABLE):
                rows.pop()
                continue
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
                # Without these a live client read "Last run: Never" however
                # many times it had run — the waitlist branch above filled
                # them from its journal and this one filled nothing
                # (ahmed-travel-ae-hun-87afd0, 2026-10-28, after 2 runs).
                **req.last_run(),
                "runnable": None, "problem_count": None,
            })
            if not search.matches(rows[-1], needle, _SEARCHABLE):
                rows.pop()
                continue
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


#: What `q` may look at on a client. Every one of these is already in the list
#: response, so searching cannot reveal anything a plain GET /clients would
#: not. DELIBERATELY ABSENT: passport_number, date_of_birth, email,
#: phone_number — `detail()` masks them, a list row never holds them, and
#: making them searchable would let a caller confirm a guessed value one
#: request at a time. See src/api/core/search.py.
_SEARCHABLE = ("client_id", "name", "route", "combos", "vfs_reference", "status")


# --------------------------------------------------------------------------- #
# One client                                                                   #
# --------------------------------------------------------------------------- #


def detail(client_id: str) -> Dict[str, Any]:
    from src.api.modules.clients import live as live
    from src.api.modules.clients import waitlist as waitlist

    from src.booking import requests as req_store

    _maintain()
    cid = client_id.strip().lower()
    flow = require_flow(cid)
    row = next((r for r in list_clients(flow=flow) if r["client_id"] == cid), {})
    if flow == WAITLIST:
        resp = waitlist.get_client(cid)
        details, problems = resp.client, [p.model_dump() for p in resp.problems]
        runnable = resp.runnable
    else:
        resp = live.get_request(cid)
        details = resp.request
        # Run the SAME pre-flight the listing runs. This used to be hardcoded
        # `problems=[], runnable=resp.enabled`, which made the one gate in front
        # of "Book and pay" unfalsifiable: a live client with a missing billing
        # postcode reported runnable, and a parked one reported not-runnable
        # with an empty reason list. It also meant the list and the detail view
        # disagreed about the same client.
        #
        # Read from the stored record rather than `resp.request`, which is the
        # masked view — precheck needs the real values.
        fields = {k: v for k, v in req_store.get(cid).data.items()
                  if k not in req_store.STATE_KEYS}
        problems = [p.to_dict() for p in req_store.precheck(cid, fields)]
        runnable = not problems
    details = {k: v for k, v in details.items()
               if k not in ("history", "client_id", "request_id")}
    return {**row, "client_id": cid, "flow": flow, "runnable": runnable,
            # `row` comes from list_clients(include_problems=False), so its
            # problem_count is None. Now that the count is known, say it —
            # otherwise a caller cannot tell "no problems" from "not computed".
            #
            # Errors only, matching the listing: a waitlist pre-flight also
            # returns warnings, and counting those here would make the same
            # client show a different number in the list and in the drawer.
            # Live problems carry no severity at all, so they all count.
            "problem_count": sum(1 for p in problems
                                 if (p.get("severity") or "error") != "warning"),
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


def _utc_z(value: Any) -> str:
    """Any timestamp as `YYYY-MM-DDTHH:MM:SSZ`.

    The feed merges three sources that each spell time differently: the
    request history already uses this form, job records use an ISO string with
    a +00:00 offset, and journal rows carry whatever was written. A web app
    sorting or formatting a mixture of those gets the order wrong, and a reader
    comparing them to the bot's logs — which are LOCAL time — is out by the
    offset. One shape, always UTC, so the web app converts once.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text                      # unparseable: pass it through as-is
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _maintain() -> None:
    """Expire dead windows and unstick dead runs before answering a read.

    The bot only did this inside a slot check, so with the scheduler off a
    crashed booking stayed 'booking' and an expired window stayed 'waiting' —
    and the web app showed both as live. Rate-limited inside maintain() to one
    pass a minute, so a list endpoint does not rescan on every call.
    """
    try:
        from src.booking import autobook
        autobook.maintain()
    except Exception:                                       # noqa: BLE001
        pass


def timeline(client_id: str, since: int = 0) -> List[Dict[str, Any]]:
    """Everything that happened to one client, newest first.

    Every entry carries `id`, stable for the life of the client, so a web app
    can render a feed without showing the same thing twice. For a live client
    the id is `req:<seq>`, where seq is the request's own monotonic counter —
    pass the highest one you have seen as `since` and only newer entries come
    back. `at` cannot be that cursor: it has second resolution, so two events
    in the same second are indistinguishable.

    Entries from the other sources — the job records, the waitlist journal,
    invitations — have no such counter, so `since` filters them by time
    instead: anything at or before the moment of the `since` entry is dropped.
    Their ids (`job:<job_id>`) remain what a web app de-duplicates them by,
    because a job record is rewritten when the job finishes and so legitimately
    appears twice.
    """
    from src.booking import invites, requests as req_store

    _maintain()
    cid = client_id.strip().lower()
    flow = require_flow(cid)
    events: List[Dict[str, Any]] = []
    #: The moment the caller has already seen up to, for the sources that have
    #: no sequence of their own.
    cutoff_at = ""

    if flow == LIVE:
        history = public_errors.public_history(
            req_store.get(cid).data.get("history") or [])
        if since:
            for index, entry in enumerate(history, start=1):
                if int(entry.get("seq") or index) == int(since):
                    cutoff_at = str(entry.get("at") or "")
                    break
        for index, entry in enumerate(history, start=1):
            seq = entry.get("seq")
            if seq is None:
                # Written before seq existed. Position is the best id there
                # is, and it is stable as long as the history is not trimmed.
                seq = index
            if since and int(seq) <= int(since):
                continue
            events.append({"id": f"req:{seq}", "at": entry.get("at", ""),
                           "source": "booking_request",
                           **{k: v for k, v in entry.items() if k != "at"}})
    else:
        from src.api.modules.clients import waitlist as waitlist
        for row in waitlist.get_client_journal(cid).rows:
            events.append({"id": f"journal:{row.started_at or ''}:{row.combo}",
                           "at": row.finished_at or row.started_at or "",
                           "source": "waitlist_journal", "event": row.status,
                           "detail": f"{row.route} / {row.combo}"
                                     + (f": {row.reason}" if row.reason else ""),
                           # row.reason is already public (get_client_journal).
                           "vfs_reference": row.vfs_reference})
        for item in invites.list_all():
            mentioned = cid in (item.get("clients") or {}) or any(
                cid in (e.get("clients_found") or []) for e in item.get("history") or [])
            if not mentioned:
                continue
            for entry in item.get("history") or []:
                events.append({"id": f"invite:{item['key']}:{entry.get('at', '')}",
                               "at": entry.get("at", ""), "source": "invitation",
                               "invitation": item["key"],
                               **{k: v for k, v in entry.items() if k != "at"}})

    try:
        from src.api.main import job_manager
        for record in job_manager.recent(500):
            p = record.payload or {}
            if cid in (p.get("registrant"), p.get("request_id")):
                public = record.to_public()
                events.append({
                    "id": f"job:{record.job_id}",
                    "at": record.started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "source": "job", "event": record.status.value,
                    "job_id": record.job_id, "run_id": record.run_id,
                    "finished_at": public.get("finished_at"),
                    # Where the web app reads what this attempt printed:
                    # GET /v1/jobs/{job_id}/logs
                    "logs_url": f"/v1/jobs/{record.job_id}/logs",
                    "started_by": p.get("started_by", ""),
                    # WHETHER THE CARD WAS CHARGED, on the entry for the
                    # attempt that charged it. Null on every attempt that
                    # reached no payment, which is most of them. Carried here
                    # as well as on the job itself because this timeline is
                    # what the web app renders for a client, and "was my
                    # client's card charged" must not need a second request to
                    # a different endpoint to answer. See
                    # JobRecord.payment_view for the three-valued `charged`.
                    "payment": public.get("payment"),
                    "detail": public.get("detail")
                    or f"{p.get('lane', '')} {p.get('mode', '')}".strip()})
    except Exception:                                       # noqa: BLE001
        pass

    if cutoff_at:
        events = [e for e in events
                  if e.get("source") == "booking_request"
                  or str(e.get("at") or "") > cutoff_at]

    for event in events:
        event["at"] = _utc_z(event.get("at"))
        if event.get("finished_at"):
            event["finished_at"] = _utc_z(event["finished_at"])

    # Newest first. Timestamps are per second, so two events in one second
    # tie — the order they were recorded in breaks the tie, otherwise "edited"
    # and "enabled" a moment apart come back in the wrong order.
    ordered = sorted(enumerate(events),
                     key=lambda pair: (str(pair[1].get("at") or ""), pair[0]),
                     reverse=True)
    return [event for _, event in ordered]
