"""Flow 2: a VFS invitation email books that waitlisted client.

    inbox pass ──▶ record() ──▶ state/invites/<key>.json ──▶ process_pending()
    (email says             one file per invitation             find the client,
     "Slots available")                                         spawn a booking

VFS emails "Slots available for booking an appointment" to the ACCOUNT that
holds the waitlist entry, valid for 12-48 hours depending on the country. The
email names the country (by its portal URL) and usually the applicant; it does
not link to the application. So an invitation is resolved to a client here:

    route  = the email's country
    client = enabled clients on that route
             whose VFS account is the mailbox the email arrived in
             who are registered on the waitlist (journal: success)
             and, when the email names the applicant, whose name matches

Exactly one client -> book them. None -> Telegram, a human looks. Several and
the email names nobody -> Telegram: guessing which person to book is the one
mistake this flow must never make.

The booking itself is the existing waitlist-entry walk (run_probe, entry
"waitlist"): log in, find the client's row on the VFS dashboard by their
reference, verify identity, book, pay. Every rule from Flow 3 applies — past
the payment click nothing is retried; it goes to needs_attention.

A COUNTRY BOOKS ONLY WHEN ITS BOOKING MAP DOES
----------------------------------------------
config/booking/<ROUTE>.json must support the "waitlist" entry and be enabled.
Until then an invitation is not dropped: it goes to Telegram as BOOK MANUALLY
with its deadline, which is the difference between a client keeping their slot
and losing it overnight.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.booking import autobook
from src.booking import requests as req_store
from src.waitlist.store import _atomic_write

log = logging.getLogger(__name__)

INVITE_DIR = os.path.join(req_store.REPO_ROOT, "state", "invites")

PENDING = "pending"          # waiting to be booked (or retried)
BOOKING = "booking"          # a run is in flight
BOOKED = "booked"
NEEDS_ATTENTION = "needs_attention"
MANUAL = "manual"            # cannot be automated: a human must book
EXPIRED = "expired"
OPEN = (PENDING, BOOKING)

HISTORY_LIMIT = 50

#: A run that has said nothing for this long died. Whether it paid is unknown,
#: so it is never reset to pending — a human checks.
STALE_SECONDS = 3 * 3600
STALE_DETAIL = ("the booking process stopped reporting 3h ago (crash or "
                "reboot?). Whether it booked or paid is unknown — check the "
                "VFS account and the card.")


def _stale(item: Dict[str, Any], now: float) -> bool:
    try:
        changed = datetime.strptime(item.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return now - (changed - datetime(1970, 1, 1)).total_seconds() > STALE_SECONDS


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(account: str, route: str, uid: str, mailbox_uid: str = "") -> str:
    raw = f"{account.lower()}|{route}|{uid}|{mailbox_uid}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def path_for(key: str) -> str:
    if not key.isalnum():
        raise ValueError(f"bad invite key {key!r}")
    return os.path.join(INVITE_DIR, f"{key}.json")


def get(key: str) -> Dict[str, Any]:
    with open(path_for(key), "r", encoding="utf-8") as fh:
        return json.load(fh)


def save(item: Dict[str, Any]) -> None:
    item["updated_at"] = _now()
    _atomic_write(path_for(item["key"]), item)


def list_all(status: Optional[str] = None) -> List[Dict[str, Any]]:
    if not os.path.isdir(INVITE_DIR):
        return []
    out = []
    for name in sorted(os.listdir(INVITE_DIR)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            item = get(name[:-5])
        except Exception:                                   # noqa: BLE001
            continue
        if status is None or item.get("status") == status:
            out.append(item)
    out.sort(key=lambda i: i.get("received_epoch") or 0)
    return out


def event(item: Dict[str, Any], name: str, detail: str = "",
          status: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    from src.utils.filelock import locked

    with locked(path_for(item["key"])):
        return _event(item, name, detail, status, **extra)


def _event(item: Dict[str, Any], name: str, detail: str = "",
           status: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    entry = {"at": _now(), "event": name}
    if detail:
        entry["detail"] = detail
    entry.update({k: v for k, v in extra.items() if v not in (None, "", {}, [])})
    item["history"] = (list(item.get("history") or []) + [entry])[-HISTORY_LIMIT:]
    if status:
        item["status"] = status
    save(item)
    return item


# --------------------------------------------------------------------------- #
# Recording invitations (called by the inbox watcher)                          #
# --------------------------------------------------------------------------- #


def record(observations: List[Any]) -> List[str]:
    """Store every new invitation. Returns the keys created. NEVER raises.

    Called from inbox.watcher.run_pass, so ANY pass — the supervisor's or a
    manual `python -m src.inbox watch` — feeds the same queue. The watcher
    marks a message seen once it has classified it, so an invitation not
    recorded here would never be offered again.
    """
    created = []
    for obs in observations or []:
        try:
            match = obs.match
            if not getattr(match, "is_invitation", False):
                continue
            email = obs.email
            account = str(obs.account or email.mailbox or "").strip().lower()
            key = _key(account, match.route, str(email.uid), email.mailbox)
            if os.path.exists(path_for(key)):
                continue
            item = {
                "key": key,
                "route": match.route,
                "account": account,
                "applicant_name": (match.fields or {}).get("applicant_name") or "",
                "category": (match.fields or {}).get("category") or "",
                "received_epoch": email.received_epoch or time.time(),
                "expires_epoch": obs.expires_at(),
                "validity_hours": match.validity_hours,
                "uid": str(email.uid),
                "status": PENDING,
                "clients": {},
                "attempts": 0,
                "created_at": _now(),
                "history": [],
            }
            os.makedirs(INVITE_DIR, exist_ok=True)
            event(item, "received", f"invitation for {match.route}")
            created.append(key)
            log.warning("Invitation recorded: %s on %s (%s).", key, match.route,
                        _mask(account))
        except Exception as exc:                            # noqa: BLE001
            log.exception("Could not record an invitation: %s", exc)
    return created


# --------------------------------------------------------------------------- #
# Resolving WHO the invitation is for — PURE given its inputs                  #
# --------------------------------------------------------------------------- #


def _norm(text: str) -> str:
    return " ".join(str(text or "").upper().replace(",", " ").split())


def name_matches(applicant_name: str, person) -> bool:
    """Every word of the client's first and last name appears in the email's
    name. VFS writes names as entered, in capitals, sometimes with a middle
    name the client file does not carry — so containment, not equality."""
    want = _norm(applicant_name).split()
    have = (_norm(person.get("first_name")) + " " + _norm(person.get("last_name"))).split()
    return bool(have) and all(word in want for word in have)


def registered_on(route: str, person) -> bool:
    """Is this client on the waitlist for this route (any of their combos)?"""
    from src.waitlist import journal
    from src.waitlist.result import Status

    for combo in person.combos:
        row = journal.latest_for(route, combo, person.id)
        if row and row.get("status") == Status.SUCCESS:
            return True
    return False


def already_booked(client_id: str) -> bool:
    return any(c.get("status") == BOOKED
               for item in list_all()
               for cid, c in (item.get("clients") or {}).items() if cid == client_id)


def candidates(item: Dict[str, Any]) -> Tuple[List[Any], str]:
    """(clients to book, why). Empty list = do not book automatically."""
    from src.waitlist import accounts, registrant as registrant_mod

    route = item["route"]
    on_account = []
    for person in registrant_mod.for_route(route):
        try:
            if accounts.resolve(person).email.lower() != item["account"]:
                continue
        except Exception:                                   # noqa: BLE001
            continue
        if not registered_on(route, person) or already_booked(person.id):
            continue
        on_account.append(person)

    if not on_account:
        return [], (f"no enabled, waitlist-registered, unbooked client on "
                    f"{route} uses {_mask(item['account'])}")

    name = item.get("applicant_name") or ""
    if name:
        named = [p for p in on_account if name_matches(name, p)]
        if len(named) == 1:
            return named, f"matched by name ({name})"
        if not named:
            return [], (f"the email names '{name}', and no client on "
                        f"{_mask(item['account'])} matches")
        return [], f"the email names '{name}', and {len(named)} clients match it"

    if len(on_account) == 1:
        return on_account, "the only waitlisted client on this account"
    return [], (f"{len(on_account)} waitlisted clients share this account and "
                "the email names none of them")


# --------------------------------------------------------------------------- #
# Gates                                                                        #
# --------------------------------------------------------------------------- #


def route_problem(route: str) -> str:
    """Why this country cannot book from an invitation, or ''."""
    from src.booking import config as booking_config

    try:
        if route not in booking_config.configured_routes():
            return f"no booking map for {route} (config/booking/{route}.json)"
        if booking_config.ENTRY_WAITLIST not in booking_config.entry_modes(route):
            return f"{route}'s booking map has no waitlist entry"
        if not booking_config.is_enabled(route):
            return f"{route}'s booking map is not enabled yet"
    except Exception as exc:                                # noqa: BLE001
        return f"{route}'s booking map is unreadable: {exc}"
    return ""


def gate_problem(test: bool = False) -> str:
    from src.settings import settings

    switches = settings().switches
    if not switches.invite_booking:
        return "invite booking is switched off ([switches] invite_booking = false)"
    if test:
        return "" if switches.test_booking else (
            "test booking is switched off ([switches] test_booking = false)")
    return autobook.card_problem()


def _is_test(person) -> bool:
    return bool(person.get("test_mode"))


# --------------------------------------------------------------------------- #
# Supervisor side                                                              #
# --------------------------------------------------------------------------- #


def process_pending(now: Optional[float] = None) -> List[str]:
    """Act on every open invitation. Called after each slot-check run.
    Returns the keys a booking was started for. NEVER raises."""
    started = []
    try:
        now = now or time.time()
        for item in list_all():
            if item.get("status") == BOOKING and _stale(item, now):
                event(item, "needs_attention", STALE_DETAIL, status=NEEDS_ATTENTION)
                _telegram(format_message("needs_attention", item, reason=STALE_DETAIL))
                continue
            if item.get("status") not in (PENDING,):
                continue
            expires = item.get("expires_epoch")
            if expires and now >= expires:
                event(item, "expired", "the invitation window closed", status=EXPIRED)
                _telegram(format_message("expired", item))
                continue

            route_issue = route_problem(item["route"])
            people, why = candidates(item)
            if route_issue:
                event(item, "manual", route_issue, status=MANUAL,
                      clients_found=[p.id for p in people])
                _telegram(format_message("manual", item, reason=route_issue,
                                         people=people, why=why))
                continue
            if not people:
                event(item, "manual", why, status=MANUAL)
                _telegram(format_message("manual", item, reason=why))
                continue

            test = all(_is_test(p) for p in people)
            blocked = gate_problem(test=test)
            if blocked:
                autobook._alert_once(f"invite-gate:{item['key']}:{blocked}",
                                     format_message("blocked", item, reason=blocked,
                                                    people=people))
                continue

            log_path = spawn(item["key"], [p.id for p in people])
            event(item, "triggered", why, clients_found=[p.id for p in people])
            _telegram(format_message("triggered", item, people=people, why=why,
                                     log_path=log_path, test=test))
            started.append(item["key"])
    except Exception as exc:                                # noqa: BLE001
        log.exception("Invite processing failed (non-fatal): %s", exc)
    return started


def spawn(key: str, client_ids: List[str]) -> str:
    """Start the booking in a detached child, like Flow 3. Returns its log."""
    import subprocess
    import sys

    os.makedirs(autobook.SPAWN_LOG_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(autobook.SPAWN_LOG_DIR, f"{stamp}_invite_{key}.log")
    argv = [sys.executable, "-m", "src.booking", "invitebook", "--invite", key]
    for cid in client_ids:
        argv += ["--client", cid]
    kwargs: Dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kwargs["start_new_session"] = True
    with open(log_path, "ab") as out:
        subprocess.Popen(argv, cwd=req_store.REPO_ROOT, stdout=out,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         env={**os.environ, "PYTHONUNBUFFERED": "1"}, **kwargs)
    return os.path.relpath(log_path, req_store.REPO_ROOT)


# --------------------------------------------------------------------------- #
# Child side                                                                   #
# --------------------------------------------------------------------------- #


def run_invite(key: str, client_ids: List[str]) -> int:
    """Book each client for one invitation, under the booking lock."""
    from src.booking import config as booking_config
    from src.utils import runlock

    with runlock.acquire("invite-booking", lane=runlock.LANE_BOOKING,
                         timeout=0, on_busy="skip") as lock:
        if not lock.held:
            log.warning("Another booking is running — invitation %s waits for "
                        "the next run.", key)
            return 0
        item = get(key)
        if item.get("status") != PENDING:
            log.info("Invitation %s is %s now — nothing to do.", key, item.get("status"))
            return 0
        commit_step = booking_config.commit_step_name(item["route"],
                                                      booking_config.ENTRY_WAITLIST)
        for client_id in client_ids:
            try:
                _book_client(key, client_id, commit_step)
            except Exception as exc:                        # noqa: BLE001
                log.exception("Invite booking for %s crashed: %s", client_id, exc)
    return 0


def _book_client(key: str, client_id: str, commit_step: str) -> None:
    from src.payment import journal as payment_journal
    from src.settings import settings
    from src.waitlist import registrant as registrant_mod

    item = get(key)
    person = registrant_mod.load(client_id)
    test = _is_test(person)
    blocked = gate_problem(test=test)
    if blocked:
        log.error("Not booking %s: %s", client_id, blocked)
        autobook._alert_once(f"invite-gate:{key}:{blocked}",
                             format_message("blocked", item, reason=blocked,
                                            people=[person]))
        return
    cap = settings().booking.max_per_day
    if not test and _payments_today() >= cap:
        autobook._alert_once(f"invite-cap:{datetime.now().date()}",
                             format_message("cap", item, reason=f"{cap} payment(s) today"))
        return

    item["attempts"] = int(item.get("attempts") or 0) + 1
    event(item, "attempt_started", client_id, status=BOOKING)
    rows_before = len(payment_journal.read_all())
    source, _, dest = item["route"].partition("-")
    try:
        from src.booking.probe import run_probe
        result = run_probe(source=source, dest=dest, registrant_id=client_id,
                           entry="waitlist", walk=True, commit=not test,
                           capture="failure")
    except Exception as exc:                                # noqa: BLE001
        result = autobook._Failed(str(exc))
    rows = payment_journal.read_all()[rows_before:]

    item = get(key)
    if test and not rows:
        passed, detail = autobook.classify_test(result, commit_step)
        item.setdefault("clients", {})[client_id] = {
            "status": "test_passed" if passed else "test_failed", "detail": detail}
        event(item, "test_passed" if passed else "test_failed", detail, status=MANUAL)
        _telegram(format_message("test_passed" if passed else "test_failed",
                                 item, reason=detail, people=[person]))
        return

    outcome = autobook.classify(result, commit_step, rows)
    status = {req_store.BOOKED: BOOKED,
              req_store.NEEDS_ATTENTION: NEEDS_ATTENTION}.get(outcome.status, PENDING)
    if status == PENDING and item["attempts"] >= settings().booking.max_attempts:
        status, outcome.detail = NEEDS_ATTENTION, (
            f"gave up after {item['attempts']} attempts; last: {outcome.detail}")
    item.setdefault("clients", {})[client_id] = {"status": status,
                                                 "detail": outcome.detail,
                                                 **outcome.details}
    event(item, status if status != PENDING else "attempt_failed",
          outcome.detail, status=status, **outcome.details)
    kind = {BOOKED: "booked", NEEDS_ATTENTION: "needs_attention"}.get(status, "retry")
    _telegram(format_message(kind, item, reason=outcome.detail, people=[person],
                             details=outcome.details))
    try:
        from src.utils import webhook
        if status in (BOOKED, NEEDS_ATTENTION):
            webhook.notify_booking({"invite": key, "client_id": client_id,
                                    "route": item["route"], "status": status,
                                    "detail": outcome.detail, **outcome.details})
    except Exception as exc:                                # noqa: BLE001
        log.warning("Could not post the booking webhook: %s", exc)


def _payments_today() -> int:
    """Payments attempted today across BOTH booking flows."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = autobook.committed_today(req_store.list_all())
    for item in list_all():
        for entry in item.get("history") or []:
            if (entry.get("event") in (BOOKED, NEEDS_ATTENTION)
                    and str(entry.get("at", "")).startswith(day)):
                count += 1
    return count


# --------------------------------------------------------------------------- #
# Telegram                                                                     #
# --------------------------------------------------------------------------- #


_HEADLINES = {
    "triggered": "📩🎯 INVITATION — BOOKING TRIGGERED",
    "manual": "📩✋ INVITATION — BOOK MANUALLY",
    "blocked": "📩⛔ INVITATION — NOT BOOKING",
    "retry": "📩↩️ INVITATION BOOKING FAILED — WILL RETRY",
    "booked": "📩✅ INVITATION BOOKED AND PAID",
    "needs_attention": "📩⚠️ INVITATION BOOKING NEEDS A HUMAN",
    "expired": "📩⌛ INVITATION EXPIRED",
    "cap": "📩🛑 DAILY BOOKING CAP REACHED",
    "test_passed": "📩🧪✅ INVITATION TEST RUN PASSED",
    "test_failed": "📩🧪❌ INVITATION TEST RUN FAILED",
}

_FOOTERS = {
    "triggered": "Logging in and booking now. Next message: the result.",
    "manual": "NOT booked automatically. Log in to the account and book before "
              "the deadline.",
    "blocked": "Nothing was booked. Fix the reason; the next run retries while "
               "the invitation is valid.",
    "retry": "Nothing was paid. The next run retries while the invitation is valid.",
    "booked": "Appointment booked and paid with the company card.",
    "needs_attention": "It will NOT retry. Check the VFS account and the card now.",
    "expired": "The window closed before a booking was confirmed.",
    "cap": "Further bookings wait until tomorrow — but the invitation may "
           "expire first. Book by hand if it matters.",
    "test_passed": "Nothing was booked or paid (test_mode on the client).",
    "test_failed": "Nothing was booked or paid (test_mode on the client).",
}


def _mask(email: str) -> str:
    return autobook._mask_email(email)


def _deadline(item: Dict[str, Any]) -> str:
    exp = item.get("expires_epoch")
    if not exp:
        return "unknown (no validity in this country's email config)"
    left = (exp - time.time()) / 3600
    when = datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M")
    return f"{when} ({left:.1f}h left)" if left > 0 else f"{when} (passed)"


def format_message(kind: str, item: Dict[str, Any], **info: Any) -> str:
    """Telegram text for one invitation event. No passport, no password."""
    headline = _HEADLINES.get(kind, kind.upper())
    if info.get("test"):
        headline = f"🧪 TEST — {headline}"
    lines = [f"{headline} — {item['route']}",
             f"Account: {_mask(item.get('account'))}",
             f"Deadline: {_deadline(item)}"]
    if item.get("applicant_name"):
        lines.append(f"Email names: {item['applicant_name']}")
    if item.get("category"):
        lines.append(f"Category: {item['category']}")
    people = info.get("people") or []
    if people:
        lines.append("Client: " + ", ".join(
            f"{p.id} ({' '.join(str(p.get(k) or '') for k in ('first_name', 'last_name')).strip()})"
            for p in people))
    if info.get("why") and kind == "triggered":
        lines.append(f"Matched: {info['why']}")
    details = info.get("details") or {}
    if kind == "booked":
        when = " ".join(str(details.get(k, "")) for k in
                        ("appointment_date", "appointment_time")).strip()
        lines.append(f"Appointment: {when or 'see the VFS account'}")
        for key, label in (("requestrefno", "Payment ref"),
                           ("transactionid", "Transaction")):
            if details.get(key):
                lines.append(f"{label}: {details[key]}")
    if info.get("reason"):
        lines.append(f"Reason: {str(info['reason'])[:400]}")
    if info.get("log_path"):
        lines.append(f"Log: {info['log_path']}")
    lines.append(f"Invite id: {item['key']}")
    lines.append("")
    lines.append(_FOOTERS.get(kind, ""))
    return "\n".join(lines).strip()


def _telegram(text: str) -> None:
    autobook._telegram(text)


# --------------------------------------------------------------------------- #
# A human settles an invitation                                                #
# --------------------------------------------------------------------------- #

DISMISSED = "dismissed"
RESOLVABLE = (MANUAL, NEEDS_ATTENTION, EXPIRED, PENDING)


def resolve(key: str, outcome: str, reason: str = "", actor: str = "",
            details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """After a human checked the VFS account:

        booked       booked (by hand, or the run did after all) — final
        not_booked   nothing was booked — back to pending while still valid,
                     so the automation tries again; expired once the window closed
        dismissed    not for this system (wrong person, cancelled) — final
    """
    from src.utils.filelock import locked

    with locked(path_for(key)):
        item = get(key)
        if item.get("status") not in RESOLVABLE:
            raise ValueError(f"invitation {key} is '{item.get('status')}' and "
                             "cannot be resolved")
        if outcome == "booked":
            status = BOOKED
        elif outcome == "not_booked":
            expired = item.get("expires_epoch") and time.time() >= item["expires_epoch"]
            status = EXPIRED if expired else PENDING
        elif outcome == "dismissed":
            status = DISMISSED
        else:
            raise ValueError("outcome must be booked, not_booked or dismissed")
        return _event(item, f"resolved_{outcome}", reason or "resolved by hand",
                      status=status, by=actor or "human", **(details or {}))
