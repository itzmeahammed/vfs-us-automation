"""Booking requests: "book this person, on this combo, between these dates."

WHAT A REQUEST IS
-----------------
A sales agent sells ONE live-slot booking to ONE person: "Norway, Dubai,
Tourist, any day from the 10th to the 20th". This module stores that sale on
this machine, one file per request, and answers the questions the auto-booker
asks of it. It is NOT a waitlist client:

    config/registrants/<id>.json      waitlist client — wait for an invitation
    config/booking_requests/<id>.json booking request — book a LIVE slot

They differ in what they require (a booking needs passport, phone and billing
fields a waitlist sign-up never asks for), in their lifecycle, and in who acts
on them. Folding requests into registrants would put them on the waitlist
runner's roster for any route that has both flows, and would make a Norway
request fail waitlist validation — which is exactly why POST /clients rejects
one today.

ONE APPLICANT PER REQUEST
-------------------------
VFS's own summary page says "You must book appointments individually", so a
request is one person and one booking. A family is several requests.

THE LIFECYCLE
-------------
                     ┌──────── slot gone / nothing in range / pre-commit fail
                     ▼                                                      │
    waiting ──(earliest date inside window)──▶ booking ──▶ booked            │
       │                                          │ └──────────────────────────┘
       │                                          └──▶ needs_attention
       └──(date_to passes)──▶ expired

    needs_attention  the run REACHED the committing step (payment) and did not
                     come back with a clean confirmation, or VFS declined /
                     blocked it. A booking may exist and a card may have been
                     charged, so nothing retries it: a human checks the VFS
                     account and resolves it (POST /booking-requests/{id}/resolve).

`enabled` is separate from `status`: it arms or parks a request without losing
where it is in the lifecycle.

Files hold passport numbers: config/booking_requests/ is gitignored, and every
write is atomic and 0600, via the same helper the client store uses.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from src.waitlist.store import _atomic_write

log = logging.getLogger(__name__)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REQUEST_DIR = os.path.join(REPO_ROOT, "config", "booking_requests")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# --- lifecycle ------------------------------------------------------------- #

WAITING = "waiting"
BOOKING = "booking"
BOOKED = "booked"
NEEDS_ATTENTION = "needs_attention"
EXPIRED = "expired"
STATUSES = (WAITING, BOOKING, BOOKED, NEEDS_ATTENTION, EXPIRED)

#: Only these can be edited or deleted. A request mid-run is being read by a
#: browser; a booked or unresolved one is a record of money spent.
EDITABLE = frozenset({WAITING, EXPIRED})

# Keys the STORE owns. Never taken from a caller, never part of the form data
# handed to the booking walk.
STATE_KEYS = frozenset({
    "status", "history", "attempts", "created_at", "updated_at",
    "status_changed_at", "booked", "last_error", "event_seq",
    "last_event_at", "consecutive_failures", "last_attempt_trigger",
})
#: Transport/meta keys that are not applicant data either.
META_KEYS = frozenset({"request_id", "route", "combo", "enabled",
                       "account", "account_password", "date_from", "date_to"})

#: How many ORDINARY history entries to keep. A request that retries for weeks
#: would otherwise push its own beginning out of the file.
HISTORY_LIMIT = 200

#: Entries that are NEVER trimmed, however old. Each one is either money or a
#: decision a person made, and "when did this become needs_attention?" must
#: stay answerable after a hundred routine ticks.
KEEP_FOREVER_EVENTS = frozenset({
    "created", "booked", "needs_attention", "resolved", "test_stopped",
    "expired", "confirmed_by_email",
})

#: THE BILLING COUNTRY, FIXED. Every applicant this system books for is
#: resident in the UAE and the gateway's country select offers one usable
#: option, so this is not a field a caller supplies — normalise() overwrites
#: whatever arrives. Callers sent "United Arab Emirates", a name their field
#: cut to "United A", and the dialling code "971"; each matched no option and
#: only failed on the live payment page, with the slot already taken.
BILLING_COUNTRY = "AE"

#: Old name, kept because other modules and the API docs import it.
DEFAULT_COUNTRY_CODE = BILLING_COUNTRY


class RequestError(ValueError):
    """Base class for store errors."""


class RequestExistsError(RequestError):
    """create() found a request with this id already."""


class RequestNotFoundError(RequestError):
    """No request with this id."""


class RequestLockedError(RequestError):
    """The request is in a state that must not be edited."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_id(request_id: str) -> str:
    rid = str(request_id or "").strip().lower()
    if not _ID_RE.match(rid) or ".." in rid:
        raise RequestError(
            f"Request id {request_id!r} must be 1-64 lowercase letters, digits, "
            "underscore or hyphen, starting with a letter or digit.")
    return rid


def path_for(request_id: str) -> str:
    return os.path.join(REQUEST_DIR, f"{check_id(request_id)}.json")


def _locked_record(fn):
    """Run a read-modify-write of one request under its cross-process lock.

    The API and a booking child both edit these files; without the lock, one
    can write back a stale copy over the other's change (a disable lost under
    a status update). Re-entrant, so resolve() -> record_event() is fine.
    """
    import functools

    from src.utils.filelock import locked

    @functools.wraps(fn)
    def wrapper(request_id, *args, **kwargs):
        with locked(path_for(request_id)):
            return fn(request_id, *args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------- #
# The record                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class BookingRequest:
    """One stored request. `data` is the whole file, state keys included."""

    request_id: str
    data: Dict[str, Any]

    @property
    def route(self) -> str:
        return str(self.data.get("route", "")).upper()

    @property
    def combo(self) -> str:
        return str(self.data.get("combo", ""))

    @property
    def status(self) -> str:
        return str(self.data.get("status", WAITING))

    @property
    def enabled(self) -> bool:
        return bool(self.data.get("enabled", False))

    @property
    def created_at(self) -> str:
        return str(self.data.get("created_at", ""))

    def window(self) -> tuple:
        from src.booking.walk import date_window
        return date_window(self.data)

    def is_armed(self) -> bool:
        return self.enabled and self.status == WAITING

    def as_registrant(self):
        """The record as the booking walk consumes it.

        State keys are stripped so {{history}} or {{status}} can never resolve
        into a form field, and `combos` is synthesised because the walk's
        context builder and account resolver speak the client shape.
        """
        from src.waitlist.registrant import Registrant

        fields = {k: v for k, v in self.data.items() if k not in STATE_KEYS}
        fields.pop("request_id", None)
        # Records written before the country was fixed still hold "United
        # Arab Emirates" or "971", and this is what the payment page reads.
        fields["country_code"] = BILLING_COUNTRY
        fields["combos"] = [self.combo]
        fields["enabled"] = True
        return Registrant(self.request_id, fields)

    def public_view(self) -> Dict[str, Any]:
        """Everything except the account password. Passport data IS returned:
        the web app that submitted it is the one reading it back."""
        view = {k: v for k, v in self.data.items() if k != "account_password"}
        view["request_id"] = self.request_id
        view["has_account_password"] = bool(self.data.get("account_password"))
        return view


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #


def _problem(field: str, message: str, hint: str = ""):
    from src.waitlist.validate import Problem
    return Problem(field=field, message=message, hint=hint)


def route_combos(route: str) -> List[str]:
    """The combo labels clients use, from config/routes/<ROUTE>.json."""
    try:
        from src.utils.route_schema import get_route_schema
        from src.vfs_bot.slot_check import combo_label

        source, _, dest = route.partition("-")
        schema = get_route_schema(source, dest) or {}
        return [combo_label(c) for c in
                (schema.get("slot_check") or {}).get("combinations") or []]
    except Exception:                                      # noqa: BLE001
        return []


def route_problems(route: str) -> List[Any]:
    """Why `route` cannot book from a LIVE slot. [] means it can.

    The country-level half of precheck(), on its own so GET /routes can answer
    "is live booking set up here?" without a request to validate.
    """
    from src.booking import config as booking_config

    if route not in booking_config.configured_routes():
        return [_problem(
            "route", f"{route} has no booking config (config/booking/{route}.json).",
            hint="Live booking is set up per country; see RUNBOOK.md.")]
    try:
        modes = booking_config.entry_modes(route)
    except Exception as exc:                                # noqa: BLE001
        return [_problem("route", f"{route}'s booking config is unreadable: {exc}")]
    if not modes:
        return [_problem("route", f"{route}'s booking config describes no "
                                  "complete flow, so nothing can be booked.")]
    if booking_config.ENTRY_NEW not in modes:
        return [_problem(
            "route", f"{route} books only from a waitlist invitation, not "
                     "from a live slot.",
            hint="Use POST /clients to put this client on the waitlist.")]
    if not booking_config.is_enabled(route, booking_config.ENTRY_NEW):
        return [_problem(
            "route", f"Booking is switched off for {route} "
                     f"(config/booking/{route}.json \"enabled\": false).")]
    from src.utils import route_pause
    paused = route_pause.problem(route, route_pause.LIVE)
    if paused:
        return [_problem(
            "route", paused[0].upper() + paused[1:] + ".",
            hint=f"Lift it with PATCH /v1/routes/{route}/flows/live "
                 "{\"enabled\": true}.")]
    return []


def _slot_step(route: str) -> Optional[Dict[str, Any]]:
    from src.booking import config as booking_config
    from src.booking.walk import SLOT_STEP_TYPE

    for step in booking_config.steps_for(route, booking_config.ENTRY_NEW):
        if step.get("type") == SLOT_STEP_TYPE:
            return step
    return None


def normalise(data: Dict[str, Any]) -> Dict[str, Any]:
    """Canonical form of a caller's payload, before validation and storage."""
    out = {k: v for k, v in (data or {}).items() if k not in STATE_KEYS}
    if "route" in out:
        out["route"] = str(out["route"] or "").strip().upper()
    if "combo" in out:
        out["combo"] = " ".join(str(out["combo"] or "").split())
    for key in ("date_from", "date_to"):
        if isinstance(out.get(key), str):
            out[key] = out[key].strip()
    # THE BILLING COUNTRY IS NOT THE CALLER'S TO SET. Every applicant this
    # system books for is resident in the UAE, the gateway's country select
    # offers one usable option, and whatever the web app sent was wrong in
    # three different ways on 2026-10-23 ("United Arab Emirates", a name its
    # field cut to "United A", and the dialling code "971") — each discovered
    # on the payment page, with the slot already taken. Overwritten, not
    # validated.
    out["country_code"] = BILLING_COUNTRY
    return out

def precheck(request_id: str, data: Dict[str, Any],
             today: Optional[date] = None) -> List[Any]:
    """Every reason this request could not be booked. [] means bookable.

    Browser-free, and ALL errors: the auto-booker acts with nobody watching, so
    anything it would trip over must be refused here, while the agent is still
    looking at the form — not discovered after a login, or after the slot is
    taken and payment is half filled.
    """
    from src.booking import walk
    from src.waitlist import validate

    problems: List[Any] = []
    try:
        check_id(request_id)
    except RequestError as exc:
        problems.append(_problem("request_id", str(exc)))

    # --- route: must book from a LIVE slot, and be armed ------------------ #
    route = str(data.get("route") or "").strip().upper()
    route_ok = False
    if not route:
        problems.append(_problem("route", "Missing \"route\", e.g. \"AE-NOR\"."))
    else:
        route_blockers = route_problems(route)
        problems.extend(route_blockers)
        route_ok = not route_blockers

    # --- combo ------------------------------------------------------------- #
    combo = str(data.get("combo") or "").strip()
    if not combo:
        problems.append(_problem("combo", "Missing \"combo\" — the centre/"
                                          "category label to book."))
    elif route_ok:
        known = route_combos(route)
        if validate._normalise(combo) not in {validate._normalise(k) for k in known}:
            problems.append(_problem(
                "combo", f"\"{combo}\" is not a combination of "
                         f"config/routes/{route}.json.",
                hint=f"Available: {'; '.join(known) or 'none'}"))

    # --- the date window: REQUIRED, and nothing else chooses a date -------- #
    if str(data.get("slot_strategy") or "").strip().lower() not in ("", walk.STRATEGY_IN_RANGE):
        problems.append(_problem(
            "slot_strategy", "A booking request books only inside its date "
                             "range; remove \"slot_strategy\"."))
    start, end = walk.date_window(data)
    if start is None and end is None and not data.get("date_from") and not data.get("date_to"):
        problems.append(_problem(
            "date_from", "A date range is required: date_from and date_to, "
                         "YYYY-MM-DD, both inclusive.",
            hint="The same date twice asks for a single day."))
    else:
        step = _slot_step(route) if route_ok else None
        # The walk's own horizon (walk.py pick_date), so a window it would
        # never page far enough to reach is refused here instead.
        max_months = (int((step or {}).get("max_months_ahead")
                          or walk.DEFAULT_MAX_MONTHS_AHEAD) if step else 0)
        for message in walk.check_window(data, step, today=today,
                                         max_months=max_months):
            field = "date_to" if message.startswith(("date_to", "the whole")) else "date_from"
            problems.append(_problem(field, message))

    # --- applicant fields the booking pages and the payment page need ------ #
    if route_ok and combo:
        probe_data = dict(data)
        probe_data["combos"] = [combo]
        for p in validate.check_booking_templates(route, probe_data, combo=combo):
            problems.append(_problem(
                p.field, p.message.replace("Not bookable yet: no",
                                           "Required for booking:"),
                hint=""))
        # A field both the booking pages and the payment page need is one
        # thing to fix, so it is reported once.
        named = {p.field for p in problems}
        problems.extend(p for p in _check_billing(data) if p.field not in named)

    # --- account: all or nothing, and one must exist ------------------------ #
    if bool(str(data.get("account") or "").strip()) != bool(str(data.get("account_password") or "").strip()):
        problems.append(_problem(
            "account", "Send account and account_password together, or neither "
                       "(to use the shared booking account)."))
    elif route_ok and combo:
        try:
            from src.waitlist import accounts
            candidate = BookingRequest(request_id or "candidate",
                                       {**data, "combo": combo})
            accounts.resolve(candidate.as_registrant())
        except Exception:                                   # noqa: BLE001
            # The resolver's own message is written for waitlist operators at
            # a terminal. The reader here is a sales agent at a form.
            problems.append(_problem(
                "account", "No VFS account to book with.",
                hint="Send account + account_password with the request, or "
                     "have the operator set the shared account ([waitlist] "
                     "account / account_password in config/config.local.ini)."))

    return problems


def _route_processor(route: str) -> str:
    """The processor named on the route's payment step, or ""."""
    if not route:
        return ""
    try:
        from src.booking import config as booking_config

        for step in booking_config.steps_for(route, booking_config.ENTRY_NEW):
            if step.get("type") == "payment":
                return str(step.get("processor") or "")
    except Exception:                                       # noqa: BLE001
        pass
    return ""


def _check_billing(data: Dict[str, Any]) -> List[Any]:
    """The payment page's required billing fields, read from its own config.

    Checked here because the payment page is AFTER the slot is taken: a
    missing postcode found there leaves a booked, unpaid appointment.
    """
    try:
        from src.payment import config as payment_config
        spec = payment_config.load()
        # The route's own processor too (AE-HUN: CYBERSOURCE_UC). Its config
        # failing to load is otherwise discovered on the gateway page, with
        # the appointment already taken.
        processor = _route_processor(str(data.get("route") or ""))
        if processor:
            payment_config.load(processor)
    except Exception as exc:                                # noqa: BLE001
        return [_problem("", f"Payment config is unreadable: {exc}")]

    problems = []
    for field in spec.get("billing_fields") or []:
        key = field.get("value_key") or field.get("name")
        if key == "country_code":
            continue
        if field.get("required") and not str(data.get(key) or "").strip():
            problems.append(_problem(
                key, f"Required for payment: {key}.",
                hint="The card payment page asks for it after the slot is taken."))

    # No country check. normalise() sets it to BILLING_COUNTRY, so there is
    # nothing a caller can get wrong and nothing for an agent to fix.
    return problems


# --------------------------------------------------------------------------- #
# Store                                                                        #
# --------------------------------------------------------------------------- #


def _write(request_id: str, data: Dict[str, Any]) -> None:
    data["updated_at"] = _now()
    _atomic_write(path_for(request_id), data)


def get(request_id: str) -> BookingRequest:
    path = path_for(request_id)
    if not os.path.exists(path):
        raise RequestNotFoundError(f"No booking request {request_id!r}.")
    with open(path, "r", encoding="utf-8") as fh:
        return BookingRequest(check_id(request_id), json.load(fh))


def exists(request_id: str) -> bool:
    return os.path.exists(path_for(request_id))


@_locked_record
def create(request_id: str, data: Dict[str, Any]) -> BookingRequest:
    rid = check_id(request_id)
    if exists(rid):
        raise RequestExistsError(f"Booking request {rid!r} already exists.")
    record = normalise(data)
    record.pop("request_id", None)
    stamp = _now()
    record.update({"status": WAITING, "attempts": 0, "created_at": stamp,
                   "status_changed_at": stamp, "event_seq": 1,
                   "last_event_at": stamp, "consecutive_failures": 0,
                   "history": [{"seq": 1, "at": stamp, "event": "created",
                                "detail": "enabled" if record.get("enabled") else "parked"}]})
    _write(rid, record)
    req = BookingRequest(rid, record)
    notify_update(req, record["history"][0])
    return req


def list_all(route: Optional[str] = None,
             status: Optional[str] = None) -> List[BookingRequest]:
    """Oldest first — the order the auto-booker serves them in."""
    if not os.path.isdir(REQUEST_DIR):
        return []
    out = []
    for name in sorted(os.listdir(REQUEST_DIR)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            req = get(name[:-5])
        except Exception:                                   # noqa: BLE001
            continue
        if route and req.route != route.upper():
            continue
        if status and req.status != status:
            continue
        out.append(req)
    out.sort(key=lambda r: (r.created_at, r.request_id))
    return out


@_locked_record
def _replace_fields(request_id: str, data: Dict[str, Any]) -> tuple:
    """Swap the caller-owned fields, keeping state. Only when EDITABLE."""
    req = get(request_id)
    if req.status not in EDITABLE:
        raise RequestLockedError(
            f"Request {req.request_id} is '{req.status}' and cannot be edited.")
    state = {k: v for k, v in req.data.items() if k in STATE_KEYS}
    record = normalise(data)
    record.pop("request_id", None)
    record.update(state)
    if req.status == EXPIRED:
        # New dates on an expired request are a new chance to book.
        record["status"] = WAITING
        record["status_changed_at"] = _now()
    entry = _append(record, "edited",
                    ", ".join(sorted(k for k in data if k not in STATE_KEYS)))
    record["last_event_at"] = entry["at"]
    # New dates, or a corrected field, are a new situation: the automatic
    # retry budget starts again rather than staying spent on the old one.
    record["consecutive_failures"] = 0
    _write(req.request_id, record)
    return BookingRequest(req.request_id, record), entry


def replace_fields(request_id: str, data: Dict[str, Any]) -> BookingRequest:
    """Swap the caller-owned fields, keeping state. Only when EDITABLE."""
    req, entry = _replace_fields(request_id, data)
    notify_update(req, entry)
    return req


@_locked_record
def _set_enabled(request_id: str, enabled: bool) -> tuple:
    req = get(request_id)
    if req.status == BOOKING:
        raise RequestLockedError(
            f"Request {req.request_id} is being booked right now.")
    req.data["enabled"] = bool(enabled)
    entry = _append(req.data, "enabled" if enabled else "disabled")
    req.data["last_event_at"] = entry["at"]
    if enabled:
        req.data["consecutive_failures"] = 0
    _write(req.request_id, req.data)
    return req, entry


def set_enabled(request_id: str, enabled: bool) -> BookingRequest:
    """Arm or park a request, and tell the web app."""
    req, entry = _set_enabled(request_id, enabled)
    notify_update(req, entry)
    return req


@_locked_record
def delete(request_id: str) -> None:
    req = get(request_id)
    if req.status == BOOKING:
        raise RequestLockedError(
            f"Request {req.request_id} is being booked right now.")
    os.unlink(path_for(req.request_id))


def _append(record: Dict[str, Any], event: str, detail: str = "",
            **extra: Any) -> Dict[str, Any]:
    """Add one history entry and return it.

    Every entry carries `seq`, a per-request counter that only ever goes up.
    `at` cannot do that job: it has second resolution, so two events in the
    same second are indistinguishable, and a web app polling "what is new"
    would either repeat one or miss one. The counter lives on the record
    (`event_seq`) rather than being len(history), because history is trimmed
    to HISTORY_LIMIT and a length-based id would then be reused.
    """
    history_now = list(record.get("history") or [])
    if "event_seq" not in record:
        # A record written before seq existed. Number its entries in the order
        # they are already in, so the counter continues past them instead of
        # restarting at 1 and colliding with history a web app has seen.
        for index, old_entry in enumerate(history_now, start=1):
            old_entry.setdefault("seq", index)
        record["event_seq"] = len(history_now)
        record["history"] = history_now
    seq = int(record.get("event_seq") or 0) + 1
    record["event_seq"] = seq
    entry = {"seq": seq, "at": _now(), "event": event}
    if detail:
        entry["detail"] = detail
    entry.update({k: v for k, v in extra.items() if v not in (None, "", {}, [])})
    history_now.append(entry)
    record["history"] = _trim(history_now)
    return entry


def _trim(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The last HISTORY_LIMIT entries, plus every entry worth keeping forever.

    A plain tail would drop the 'booked' that says a card was charged while
    keeping fifty 'skipped' ticks that say nothing happened.
    """
    if len(history) <= HISTORY_LIMIT:
        return history
    recent = history[-HISTORY_LIMIT:]
    recent_seqs = {id(e) for e in recent}
    kept = [e for e in history[:-HISTORY_LIMIT]
            if e.get("event") in KEEP_FOREVER_EVENTS and id(e) not in recent_seqs]
    return kept + recent


def notify_update(req: "BookingRequest", entry: Dict[str, Any]) -> None:
    """Push one history entry to the web app. Never raises, never blocks long.

    Called with the record's lock RELEASED. A webhook has retries measured in
    seconds, and holding a cross-process file lock across them would make the
    next attempt on this request wait on an HTTP call to someone else's server.
    """
    try:
        from src.utils import webhook
        if not webhook.is_configured():
            return
        webhook.notify_booking_update({
            "request_id": req.request_id,
            "route": req.route,
            "combo": req.combo,
            "status": req.status,
            "enabled": req.enabled,
            "attempts": int(req.data.get("attempts") or 0),
            "seq": entry.get("seq"),
            "at": entry.get("at"),
            "event": entry.get("event"),
            "detail": entry.get("detail", ""),
            **{k: v for k, v in entry.items()
               if k not in ("seq", "at", "event", "detail")},
        })
    except Exception as exc:                                # noqa: BLE001
        # Freshness, not correctness: GET /v1/clients/{id}/timeline?since=
        # still returns this entry.
        log.debug("Could not push the booking update webhook: %s", exc)


@_locked_record
def _record_event(request_id: str, event: str, detail: str = "", *,
                  status: Optional[str] = None, **extra: Any) -> tuple:
    """Append a history entry and, optionally, move the status. Re-reads the
    file first, so a concurrent enable/disable from the API is not lost."""
    req = get(request_id)
    entry = _append(req.data, event, detail, **extra)
    # ALWAYS, unlike status_changed_at: two failures in a row leave the status
    # at 'waiting', so a web app sorting by "last activity" saw nothing move.
    req.data["last_event_at"] = entry["at"]
    if status and status != req.status:
        req.data["status"] = status
        req.data["status_changed_at"] = _now()
    if event == "attempt_started":
        req.data["attempts"] = int(req.data.get("attempts") or 0) + 1
        # WHOSE attempt this is decides whether its failure counts against the
        # automatic retry budget. A human testing a request used to spend it.
        req.data["last_attempt_trigger"] = str(extra.get("trigger") or "auto")
    if event in ("attempt_failed", "needs_attention"):
        req.data["last_error"] = detail
        if req.data.get("last_attempt_trigger", "auto") == "auto":
            req.data["consecutive_failures"] = int(
                req.data.get("consecutive_failures") or 0) + 1
    # ANY of these means the situation changed, so the budget starts again:
    # a booking worked, a human looked, or the request itself was edited.
    if event in ("booked", "resolved", "edited", "enabled", "created"):
        req.data["consecutive_failures"] = 0
    if event == "booked":
        req.data["booked"] = {k: v for k, v in extra.items()}
        req.data.pop("last_error", None)
    _write(req.request_id, req.data)
    return req, entry


def record_event(request_id: str, event: str, detail: str = "", *,
                 status: Optional[str] = None, **extra: Any) -> BookingRequest:
    """Append a history entry, move the status, and tell the web app."""
    req, entry = _record_event(request_id, event, detail, status=status, **extra)
    notify_update(req, entry)
    return req


STALE_SECONDS = 3 * 3600

#: Grace before a 'booking' whose process is GONE is declared dead. Short,
#: because the process is the evidence: it covers only the seconds between the
#: child writing its last record and the parent noticing.
DEAD_PROCESS_GRACE_SECONDS = 120


def _attempt_pid(req: "BookingRequest") -> Optional[int]:
    """The pid of the run that put this request into 'booking', if known."""
    for entry in reversed(req.data.get("history") or []):
        if entry.get("event") == "attempt_started":
            run_id = str(entry.get("run_id") or "")
            if not run_id:
                return None
            try:
                from src.booking import attempt_job
                record = attempt_job.find(run_id)
            except Exception:                               # noqa: BLE001
                return None
            # A finished record means the child got far enough to report, so
            # the request is not stuck on a dead process.
            if not record or record.get("status") != "running":
                return None
            return record.get("pid")
        if entry.get("event") in ("booked", "needs_attention", "resolved"):
            return None
    return None


def flag_stale(now: Optional[float] = None) -> List[str]:
    """Requests stuck in 'booking'. Never reset to waiting: one may have paid.

    Two ways to be stuck, and the first is the one that matters:

      the process is GONE      a crash, a kill, a reboot. Known within a
                               couple of minutes, because the attempt's job
                               record names the pid.
      nothing for STALE_SECONDS  a process still alive but wedged — a browser
                               waiting on a dialog nobody will answer.

    Before the pid check this took three hours either way, during which the
    request was not armed, nothing retried, and the web app showed a booking
    that had already died.
    """
    import time as _time

    now = now or _time.time()
    moved = []
    for req in list_all(status=BOOKING):
        try:
            changed = datetime.strptime(req.data.get("status_changed_at", ""),
                                        "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
        age = now - changed.replace(tzinfo=timezone.utc).timestamp()
        reason = ""
        if age > STALE_SECONDS:
            reason = ("the booking process stopped reporting 3h ago (crash or "
                      "reboot?). Whether it booked or paid is unknown.")
        elif age > DEAD_PROCESS_GRACE_SECONDS:
            pid = _attempt_pid(req)
            if pid is not None:
                try:
                    from src.booking import attempt_job
                    alive = attempt_job.is_running(pid)
                except Exception:                           # noqa: BLE001
                    alive = True
                if not alive:
                    reason = ("the booking process exited without reporting "
                              f"(pid {pid} is gone). Whether it booked or paid "
                              "is unknown.")
        if reason:
            record_event(req.request_id, "needs_attention", reason,
                         status=NEEDS_ATTENTION)
            moved.append(req.request_id)
    return moved


def expire_past(today: Optional[date] = None) -> List[str]:
    """Move waiting requests whose window has fully passed to 'expired'."""
    today = today or date.today()
    moved = []
    for req in list_all(status=WAITING):
        _, end = req.window()
        if end is not None and end < today:
            record_event(req.request_id, "expired",
                         f"date_to {end} has passed", status=EXPIRED)
            moved.append(req.request_id)
    return moved


@_locked_record
def resolve(request_id: str, outcome: str, reason: str = "",
            details: Optional[Dict[str, Any]] = None) -> BookingRequest:
    """A human settles a needs_attention request after checking VFS.

    outcome 'booked'     the appointment exists — record it, never retry.
    outcome 'not_booked' nothing was booked or charged — back to waiting.
    """
    req = get(request_id)
    if req.status != NEEDS_ATTENTION:
        raise RequestLockedError(
            f"Only a needs_attention request can be resolved; "
            f"{req.request_id} is '{req.status}'.")
    if outcome == "booked":
        return record_event(req.request_id, "booked", reason or "resolved by hand",
                            status=BOOKED, by="human", **(details or {}))
    if outcome == "not_booked":
        return record_event(req.request_id, "resolved", reason or "nothing was booked",
                            status=WAITING, by="human")
    raise RequestError("outcome must be 'booked' or 'not_booked'.")
