"""Book stored requests automatically when the slot checker sees their dates.

    slot check (supervisor) ──▶ handle_route_checked() ──spawn──▶ run_queue()
       reads each combo's         which waiting requests          one login per
       EARLIEST date              does that date satisfy?         request, books
                                                                  and pays

THE TRIGGER RULE, AND ONLY THIS ONE
-----------------------------------
The slot checker sees one number per combo: the earliest date VFS offers (for
one applicant — VFS books individually). A request fires when

        date_from <= earliest <= date_to

and at no other time. An earliest date BEFORE the window does not fire, even
though later dates might be free: that was a deliberate product decision
(2026-09-30) — spend a login only when a date the client agreed to has been
seen. An earliest date after the window cannot contain one, because calendars
run forward.

The walk then books inside the window by its own rules (walk.resolve_strategy
-> in_range): it picks the earliest offered date in [date_from, date_to] and
books NOTHING if none is left. The checker's date is the reason to look, never
the date that gets booked.

WHY A SEPARATE PROCESS
----------------------
The supervisor checks every route in turn, several minutes each. Booking inline
would hold the remaining routes hostage to one booking walk; returning first
would lose minutes on a slot competitors are also watching. So the supervisor
spawns `python -m src.booking autobook` and moves on. The child owns
LANE_BOOKING (src/utils/runlock.py): at most one automatic booking at a time,
machine-wide, and a second spawn while one runs simply exits — its requests are
still waiting and the next check tries again.

WHAT IS NEVER RETRIED
---------------------
A run that reached the committing step (payment) and did not come back with the
gateway's own "success" goes to needs_attention and stays there. It may have
booked and charged; retrying could book and charge twice. Only a human, having
looked at the VFS account, moves it on (POST /booking-requests/{id}/resolve).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from src.booking import requests as store

log = logging.getLogger(__name__)

REPO_ROOT = store.REPO_ROOT
SPAWN_LOG_DIR = os.path.join(REPO_ROOT, "logs", "booking_auto")

#: The party size whose date is compared. VFS books one applicant at a time.
APPLICANTS = 1


# --------------------------------------------------------------------------- #
# Matching — PURE                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class Match:
    request_id: str
    route: str
    combo: str
    seen_date: str          # the earliest date the checker saw, ISO


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


def earliest_by_combo(route: str, slot_results: List[Any],
                      resolve_label=None) -> Dict[str, Tuple[str, str]]:
    """{normalised combo label: (combo label, earliest ISO date)} for every
    combo whose banner quoted a date for one applicant.

    `slot_results` is the supervisor's [(result_label, message), ...]. The
    result label is NOT the label clients use (see autotrigger's label trap),
    so it is mapped back through `resolve_label`; an unmappable one is skipped
    loudly rather than matched against nothing.
    """
    from src.slots import parse

    if resolve_label is None:
        from src.waitlist.autotrigger import resolve_combo_label
        resolve_label = resolve_combo_label

    out: Dict[str, Tuple[str, str]] = {}
    for item in slot_results or []:
        try:
            label, message = item[0], item[1]
        except (TypeError, IndexError):
            continue
        outcome, dates = parse.parse_message(str(message or ""))
        if outcome != parse.SLOT or APPLICANTS not in dates:
            continue
        combo = resolve_label(route, label)
        if not combo:
            log.warning("Auto-book: slot banner on unmappable combo %r (%s) — "
                        "no request can match it.", label, route)
            continue
        out[_norm(combo)] = (combo, dates[APPLICANTS])
    return out


def find_matches(route: str, earliest: Dict[str, Tuple[str, str]],
                 requests: List[store.BookingRequest],
                 today: Optional[date] = None) -> List[Match]:
    """Armed requests on `route` whose window contains the seen date.
    Oldest request first: the first sale gets the first chance."""
    today = today or date.today()
    matches = []
    for req in sorted(requests, key=lambda r: (r.created_at, r.request_id)):
        if req.route != route or not req.is_armed():
            continue
        seen = earliest.get(_norm(req.combo))
        if not seen:
            continue
        try:
            seen_date = date.fromisoformat(seen[1])
        except ValueError:
            continue
        start, end = req.window()
        if start is None or end is None:
            continue            # precheck refuses these; never book without one
        if start <= seen_date <= end and seen_date >= today:
            matches.append(Match(req.request_id, route, req.combo, seen[1]))
    return matches


# --------------------------------------------------------------------------- #
# Outcome classification — PURE                                                #
# --------------------------------------------------------------------------- #


@dataclass
class Outcome:
    status: str             # the request's next status
    event: str              # history event name
    detail: str
    details: Dict[str, Any]

    @property
    def notify(self) -> bool:
        return self.status in (store.BOOKED, store.NEEDS_ATTENTION)


def _found(walk, key: str) -> Any:
    for step in reversed(getattr(walk, "steps", None) or []):
        value = (getattr(step, "found", None) or {}).get(key)
        if value:
            return value
    return None


def classify(result: Any, commit_step: str,
             payment_rows: List[Dict[str, Any]]) -> Outcome:
    """Turn a ProbeResult into what happens to the request next.

    `payment_rows` are the payment-journal rows written DURING this attempt.
    They, not the walk, decide "booked": the walk marks its payment step ok as
    soon as the click happened, and the gateway's answer lives in the journal.
    """
    walk = getattr(result, "walk", None)
    steps = [s.name for s in (getattr(walk, "steps", None) or [])]
    reached = commit_step in steps or any(
        r.get("event") == "payment_submitting" for r in payment_rows)
    booked_details = {k: v for k, v in {
        "appointment_date": _found(walk, "chosen_date"),
        "appointment_time": _found(walk, "chosen_time"),
    }.items() if v}
    for row in payment_rows:
        for key in ("requestrefno", "transactionid", "booking_ref"):
            if row.get(key):
                booked_details[key] = row[key]

    if getattr(result, "interrupted", False):
        if reached:
            return Outcome(store.NEEDS_ATTENTION, "needs_attention",
                           "interrupted on or after the payment step — check "
                           "the VFS account and the card", booked_details)
        return Outcome(store.WAITING, "attempt_failed",
                       "interrupted before payment; nothing was spent", {})

    if walk is None:
        errors = "; ".join(getattr(result, "errors", None) or []) or "no walk ran"
        return Outcome(store.WAITING, "attempt_failed", errors, {})

    if getattr(walk, "payment_declined", False):
        return Outcome(store.NEEDS_ATTENTION, "needs_attention",
                       f"payment DECLINED: {walk.reason}. Funds may still have "
                       "been taken — check before re-arming.", booked_details)
    if getattr(walk, "blocked", False):
        return Outcome(store.NEEDS_ATTENTION, "needs_attention",
                       f"VFS blocked the account: {walk.reason}", booked_details)

    if reached:
        answered = [r for r in payment_rows if r.get("event") == "payment_result"]
        if walk.ok and any(r.get("outcome") == "success" for r in answered):
            return Outcome(store.BOOKED, "booked", "booked and paid",
                           booked_details)
        why = ("payment submitted but the gateway's answer was "
               f"{(answered[-1].get('outcome') if answered else 'never read')}"
               if any(r.get("event") == "payment_submitting" for r in payment_rows)
               else f"stopped at '{walk.stopped_at}' on the payment step: "
                    f"{walk.reason}")
        return Outcome(store.NEEDS_ATTENTION, "needs_attention", why,
                       booked_details)

    return Outcome(store.WAITING, "attempt_failed",
                   f"stopped at '{walk.stopped_at or '?'}' before payment: "
                   f"{walk.reason or 'no reason given'}", {})


# --------------------------------------------------------------------------- #
# Gates                                                                        #
# --------------------------------------------------------------------------- #


def _cfg():
    from src.settings import settings
    return settings().booking


def committed_today(requests: List[store.BookingRequest],
                    today: Optional[date] = None) -> int:
    """Attempts today that reached payment: the ones that can cost money."""
    day = (today or date.today()).isoformat()
    count = 0
    for req in requests:
        for entry in req.data.get("history") or []:
            if (entry.get("event") in ("booked", "needs_attention")
                    and str(entry.get("at", "")).startswith(day)
                    and entry.get("by") != "human"):
                count += 1
    return count


def card_problem() -> str:
    """'' when the company card is loadable; otherwise why not."""
    try:
        from src.payment import card as card_mod
        return "" if card_mod.load() is not None else (
            "no company card: set VFS_CARD_NUMBER, VFS_CARD_EXPIRY and "
            "VFS_CARD_CVN for the user that runs the scheduled task")
    except Exception as exc:                                # noqa: BLE001
        return f"company card unusable: {exc}"


def gate_problem(route: str) -> str:
    """Why nothing may be booked on `route` right now, or ''."""
    from src.booking import config as booking_config

    if not _cfg().auto_book_enabled:
        return "[booking] auto_book_enabled is false"
    if not booking_config.is_enabled(route):
        return f"booking is disabled in config/booking/{route}.json"
    return card_problem()


# --------------------------------------------------------------------------- #
# Supervisor side                                                              #
# --------------------------------------------------------------------------- #


def handle_route_checked(route: str, slot_results: List[Any]) -> List[Match]:
    """Called by the supervisor after each route. NEVER raises.

    Cheap in the common case — no request, or no date inside any window —
    and starts a background booking process only when something matches.
    """
    try:
        store.expire_past()
        requests = store.list_all(route=route)
        if not any(r.is_armed() for r in requests):
            return []

        matches = find_matches(route, earliest_by_combo(route, slot_results),
                               requests)
        if not matches:
            log.info("Auto-book %s: %d waiting request(s), no earliest date "
                     "inside any window.", route,
                     sum(r.is_armed() for r in requests))
            return []

        blocked = gate_problem(route)
        if blocked:
            log.error("Auto-book %s: %d request(s) matched but NOT booking — %s.",
                      route, len(matches), blocked)
            _alert_once(f"autobook-gate:{route}:{blocked}",
                        f"Booking requests matched a live slot on {route} but "
                        f"were NOT booked: {blocked}.")
            return matches

        log.warning("Auto-book %s: %s", route, ", ".join(
            f"{m.request_id} (earliest {m.seen_date} on {m.combo})" for m in matches))
        spawn(route, matches)
        return matches
    except Exception as exc:                                # noqa: BLE001
        log.exception("Auto-book check failed for %s (non-fatal): %s", route, exc)
        return []


def spawn(route: str, matches: List[Match]) -> Optional[int]:
    """Start the booking in a detached child. Returns its pid."""
    os.makedirs(SPAWN_LOG_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(SPAWN_LOG_DIR, f"{stamp}_{route}.log")
    argv = [sys.executable, "-m", "src.booking", "autobook", "--route", route]
    for m in matches:
        argv += ["--request", m.request_id, "--seen", f"{m.request_id}={m.seen_date}"]

    kwargs: Dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kwargs["start_new_session"] = True

    with open(log_path, "ab") as out:
        proc = subprocess.Popen(argv, cwd=REPO_ROOT, stdout=out,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                                **kwargs)
    log.warning("Auto-book %s: booking process started (pid %s), log %s",
                route, proc.pid, log_path)
    return proc.pid


# --------------------------------------------------------------------------- #
# Child side                                                                   #
# --------------------------------------------------------------------------- #


def run_queue(route: str, request_ids: List[str],
              seen: Optional[Dict[str, str]] = None) -> int:
    """Book each request in order, one login each. Returns an exit code.

    Every gate is re-checked here, under the lock: the files may have changed
    between the supervisor's decision and this process starting, and this is
    the last point before money is involved.
    """
    from src.booking import config as booking_config
    from src.utils import runlock

    seen = seen or {}
    with runlock.acquire("auto-booking", lane=runlock.LANE_BOOKING,
                         timeout=0, on_busy="skip") as lock:
        if not lock.held:
            log.warning("Another booking is running — leaving %s for the next "
                        "slot check.", ", ".join(request_ids))
            return 0

        problem = gate_problem(route)
        if problem:
            log.error("Not booking on %s: %s", route, problem)
            return 1

        commit_step = booking_config.commit_step_name(route, booking_config.ENTRY_NEW)
        for request_id in request_ids:
            try:
                _book_one(route, request_id, seen.get(request_id, ""), commit_step)
            except Exception as exc:                        # noqa: BLE001
                # _book_one records its own outcomes; this is a bug guard so
                # one request can never stop the ones queued behind it.
                log.exception("Auto-book: %s crashed: %s", request_id, exc)
    return 0


def _book_one(route: str, request_id: str, seen_date: str,
              commit_step: str) -> None:
    from src.payment import journal as payment_journal

    try:
        req = store.get(request_id)
    except store.RequestNotFoundError:
        log.info("Request %s was deleted before its turn.", request_id)
        return
    if not req.is_armed():
        log.info("Request %s is %s/%s now — skipping.", request_id, req.status,
                 "enabled" if req.enabled else "disabled")
        return

    cfg = _cfg()
    if committed_today(store.list_all()) >= cfg.max_per_day:
        log.error("Daily cap reached ([booking] max_per_day = %d) — %s waits "
                  "for tomorrow.", cfg.max_per_day, request_id)
        _alert_once(f"autobook-cap:{date.today()}",
                    f"Auto-booking daily cap ({cfg.max_per_day}) reached; "
                    "further matches wait until tomorrow.")
        return

    problems = [p for p in store.precheck(request_id, req.data)
                if getattr(p, "severity", "error") == "error"]
    if problems:
        store.record_event(request_id, "needs_attention",
                           "request no longer valid: " + "; ".join(
                               p.message for p in problems[:3]),
                           status=store.NEEDS_ATTENTION)
        return

    store.record_event(request_id, "attempt_started",
                       f"earliest {seen_date or '?'} seen on {req.combo}",
                       status=store.BOOKING, seen_date=seen_date)
    rows_before = len(payment_journal.read_all())
    source, _, dest = route.partition("-")

    try:
        from src.booking.probe import run_probe
        result = run_probe(source=source, dest=dest,
                           person=req.as_registrant(),
                           entry="new", combo=req.combo,
                           walk=True, commit=True, capture="failure")
    except Exception as exc:                                # noqa: BLE001
        # run_probe raises only before Chrome: config, account, window. The
        # payment journal is still checked, because "before Chrome" is an
        # assumption and a wrong one would be the expensive direction.
        result = _Failed(str(exc))

    payment_rows = payment_journal.read_all()[rows_before:]
    outcome = classify(result, commit_step, payment_rows)

    if outcome.status == store.WAITING:
        attempts = int(store.get(request_id).data.get("attempts") or 0)
        if attempts >= cfg.max_attempts:
            outcome = Outcome(store.NEEDS_ATTENTION, "needs_attention",
                              f"gave up after {attempts} attempts; last: "
                              f"{outcome.detail}", {})

    store.record_event(request_id, outcome.event, outcome.detail,
                       status=outcome.status, **outcome.details)
    log.warning("Auto-book %s -> %s: %s", request_id, outcome.status, outcome.detail)
    if outcome.notify:
        _notify(store.get(request_id), outcome)


@dataclass
class _Failed:
    """Stands in for a ProbeResult when run_probe raised before returning."""
    error: str
    walk: Any = None
    interrupted: bool = False

    @property
    def errors(self) -> List[str]:
        return [self.error]


# --------------------------------------------------------------------------- #
# Notification — never raises                                                  #
# --------------------------------------------------------------------------- #


def _notify(req: store.BookingRequest, outcome: Outcome) -> None:
    payload = {"request_id": req.request_id, "route": req.route,
               "combo": req.combo, "status": outcome.status,
               "detail": outcome.detail, **outcome.details}
    try:
        from src.utils import webhook
        webhook.notify_booking(payload)
    except Exception as exc:                                # noqa: BLE001
        log.warning("Could not post the booking webhook: %s", exc)

    if not _cfg().telegram_enabled:
        return
    try:
        from src.utils import telegram
        if outcome.status == store.BOOKED:
            when = " ".join(str(outcome.details.get(k, "")) for k in
                            ("appointment_date", "appointment_time")).strip()
            telegram.send_message(
                f"✅ BOOKED {req.request_id} — {req.combo}"
                + (f" on {when}" if when else "") + ". Paid with the company card.")
        else:
            telegram.send_error(
                f"⚠️ BOOKING NEEDS A HUMAN: {req.request_id} ({req.route}, "
                f"{req.combo}) — {outcome.detail}. It will not retry. Check the "
                "VFS account, then POST /booking-requests/"
                f"{req.request_id}/resolve.")
    except Exception as exc:                                # noqa: BLE001
        log.warning("Could not send the booking Telegram: %s", exc)


def _alert_once(key: str, text: str) -> None:
    """One Telegram per key per cooldown window — a gate that blocks every
    tick must not page someone every 30 minutes."""
    try:
        from src.utils import telegram, waitlist_cooldown
        if waitlist_cooldown.is_on_cooldown(key):
            return
        waitlist_cooldown.record_sent(key)
        telegram.send_error(text)
    except Exception as exc:                                # noqa: BLE001
        log.debug("Could not send alert %s: %s", key, exc)
