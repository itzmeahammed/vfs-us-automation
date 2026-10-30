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
from src.api.modules.booking.schemas import InvitationStatus
from src.api.modules.booking import payments as payments
from src.api.core import search
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
            # Refused here, before a process is spawned, with the same rule
            # the run re-checks under the booking lock.
            if body.mode != "probe":
                from src.booking import autobook
                refusal = autobook.manual_refusal(req, commit=body.mode == "commit")
                if refusal:
                    raise ApiError(409, refusal, "request_not_runnable")
            actor = actor_for(request)
            fields.update(route=req.route, request_id=cid, entry="new",
                          started_by=actor if actor != "unknown" else None)
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


#: History events that mean "the bot had a go at booking this one".
#: `triggered` is the run being spawned, `attempt_started` one client's booking
#: beginning inside it.
_ATTEMPT_EVENTS = ("triggered", "attempt_started")

#: Which history events produce which status, so "when did it reach the state
#: it is in now?" is answered from the event that actually put it there rather
#: than from whichever terminal-looking event happened last.
#:
#: `resolved_not_booked` is deliberately filed under both `pending` and
#: `expired`: a human saying "nothing was booked" sends a still-valid
#: invitation BACK to pending for the automation to retry, and only an expired
#: one stays settled. See invites.resolve.
_STATUS_EVENTS = {
    "booked": ("booked", "resolved_booked"),
    "expired": ("expired", "resolved_not_booked"),
    "dismissed": ("dismissed", "resolved_dismissed"),
    "needs_attention": ("needs_attention",),
    "manual": ("manual", "test_passed", "test_failed"),
    "pending": ("received", "attempt_failed", "resolved_not_booked"),
    "booking": ("attempt_started", "triggered"),
}

#: Statuses that are the end of the line. Anything else is still in play and
#: the bot may still act on it.
_FINAL_STATUSES = ("booked", "expired", "dismissed")


def _epoch_of(entry: Optional[Dict[str, Any]]) -> Optional[float]:
    """A history entry's timestamp as epoch seconds, or None.

    Entries are stamped by `invites._now()`, which is UTC with a trailing Z,
    so they are read as UTC. A stamp that cannot be read is None and never
    "now": an invented time on a booking timeline is worse than a gap in one.
    """
    if not entry:
        return None
    stamp = str(entry.get("at") or "").strip()
    if not stamp:
        return None
    from datetime import datetime, timezone
    try:
        naive = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        try:
            naive = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return naive.replace(tzinfo=timezone.utc).timestamp()


def _int(value: Any) -> int:
    """A count from a stored field, or 0. Never raises.

    `attempts` is written by the booker and read here on a LIST endpoint: one
    record with a non-numeric value in it must not take down the whole page
    with a 500. A bad count reads as 0, which is visibly wrong on the row
    rather than invisibly fatal for every other row beside it.
    """
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _progress(item: Dict[str, Any]) -> Dict[str, Any]:
    """What has happened to this invitation, as times rather than prose.

    Derived HERE rather than in a console, because what each history event
    means is this module's business. A reader that has to know which event
    names count as an attempt is a second copy of these rules, and the subtle
    ones drift silently.

    Every timestamp is epoch seconds or None. None means "it has not happened"
    or "it was not recorded"; the two are deliberately not distinguished,
    because neither is a time and neither may be rendered as one.
    """
    history = [h for h in (item.get("history") or []) if isinstance(h, dict)]
    status = item.get("status") or ""

    def pick(names, newest=True):
        source = reversed(history) if newest else history
        for entry in source:
            if entry.get("event") in names:
                return entry
        return None

    settled = pick(_STATUS_EVENTS.get(status, ()))
    manual = pick(("manual",))
    newest = history[-1] if history else None

    return {
        "attempts": _int(item.get("attempts")),
        # When the bot first tried, and when it last tried.
        "first_attempt_epoch": _epoch_of(pick(_ATTEMPT_EVENTS, newest=False)),
        "last_attempt_epoch": _epoch_of(pick(_ATTEMPT_EVENTS)),
        # When it reached the state it is in NOW — not merely the last
        # terminal-looking event, which for a retried invitation is in its past.
        "status_since_epoch": _epoch_of(settled),
        "final": status in _FINAL_STATUSES,
        # WHY the bot will not book this one itself. The single most asked
        # question about a "Book by hand" row, and it is already written down.
        "blocked_reason": (manual.get("detail") or "") if manual else "",
        "last_event": ({"event": newest.get("event"),
                        "detail": newest.get("detail") or "",
                        "at": newest.get("at"),
                        "epoch": _epoch_of(newest)} if newest else None),
        "events": len(history),
    }


def _invite_view(item: Dict[str, Any]) -> Dict[str, Any]:
    from src.booking import invites

    out = dict(item)
    # The FULL mailbox address, not a mask.
    #
    # These are our own booking mailboxes, not applicant contact addresses,
    # and an operator deciding which account to go and read an email in cannot
    # act on `a****t@travnook.com` — there are several and the mask collapses
    # them into one another. The record already holds the real address, and
    # refuses a masked one on the way in (see invites.record); this stops
    # throwing it away on the way out.
    out["account"] = item.get("account", "")
    out["deadline"] = invites._deadline(item)
    # Derived from stored history, so it is the part of this view most able to
    # meet something unexpected. It must never cost the caller the rest of the
    # page: a record whose progress cannot be worked out still lists, with an
    # empty progress block, and the row renders without its timeline.
    try:
        out["progress"] = _progress(item)
    except Exception:                                        # noqa: BLE001
        out["progress"] = {"attempts": 0, "first_attempt_epoch": None,
                           "last_attempt_epoch": None, "status_since_epoch": None,
                           "final": False, "blocked_reason": "",
                           "last_event": None, "events": 0, "unreadable": True}
    return out


#: What `q` may look at on an invitation. All of these are columns in the
#: console's table. `clients` is a dict, and `search.matches` reads a dict's
#: KEYS — here that is exactly right, because the keys are the client ids.
#:
#: DELIBERATELY ABSENT and staying absent:
#:   `account`   is now SERVED in full (see _invite_view) but is still not
#:               SEARCHABLE. Displaying an address to someone already looking
#:               at the row is not the same as letting a guess be confirmed by
#:               a hit, and only the first was asked for.
#:   `history`, `progress`   their entries are not cleared for search.
#: See src/api/core/search.py and tests/test_api_search.py.
_INVITE_SEARCHABLE = ("key", "applicant_name", "category", "route",
                      "status", "clients")


@router.get("/invitations", summary="VFS invitation emails and their outcome")
def list_invitations(page: PageParams = Depends(),
                     status: Optional[InvitationStatus] = Query(
                         default=None, description="pending | booking | booked | "
                         "needs_attention | manual | expired | dismissed"),
                     route: Optional[str] = None,
                     q: Optional[str] = Query(default=None, max_length=120,
                         description=("Free-text search over the invitation "
                                      "key, applicant name, category, route, "
                                      "status and matched client ids. All "
                                      "terms must match; each may match a "
                                      "different field."))) -> Dict[str, Any]:
    from src.booking import invites

    needle = search.terms(q)
    items = [i for i in invites.list_all(status=status)
             if not route or i.get("route") == route.upper()]
    items.reverse()                                          # newest first
    views = [_invite_view(i) for i in items]
    return paginate([v for v in views
                     if search.matches(v, needle, _INVITE_SEARCHABLE)], page)


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
