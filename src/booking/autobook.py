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


def is_test(req: store.BookingRequest) -> bool:
    """A TEST request walks every page and stops before the payment click.

    The full chain runs for real — slot check, match, Telegram, background
    process, login, every form, the slot pick — and nothing is booked or
    charged, because nothing past the commit boundary is clicked. It exists
    so the pipeline can be proven end to end without a card, which is the one
    thing a fake card cannot do: on Norway the appointment is created BEFORE
    the card page, so a fake card leaves a real, unpaid booking behind.
    """
    return bool(req.data.get("test_mode"))


def gate_problem(route: str, test: bool = False) -> str:
    """Why nothing may be booked on `route` right now, or ''."""
    from src.booking import config as booking_config

    from src.settings import settings

    switches = settings().switches
    if test and not switches.test_booking:
        return "test booking is switched off ([switches] test_booking = false)"
    if not test and not switches.live_booking:
        return "live booking is switched off ([switches] live_booking = false)"
    if not booking_config.is_enabled(route):
        return f"booking is disabled in config/booking/{route}.json"
    return "" if test else card_problem()


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
        for request_id in store.flag_stale():
            _telegram(format_message("needs_attention", store.get(request_id)))
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

        by_id = {r.request_id: r for r in requests}
        runnable = []
        for m in matches:
            blocked = gate_problem(route, test=is_test(by_id[m.request_id]))
            if blocked:
                log.error("Auto-book %s: %s matched but NOT booking — %s.",
                          route, m.request_id, blocked)
                _alert_once(f"autobook-gate:{m.request_id}:{blocked}",
                            format_message("blocked", by_id[m.request_id],
                                           seen_date=m.seen_date, reason=blocked))
            else:
                runnable.append(m)
        if not runnable:
            return matches
        matches = runnable

        log.warning("Auto-book %s: %s", route, ", ".join(
            f"{m.request_id} (earliest {m.seen_date} on {m.combo})" for m in matches))
        log_path = spawn(route, matches)
        for position, m in enumerate(matches, start=1):
            _telegram(format_message(
                "triggered", by_id[m.request_id], seen_date=m.seen_date,
                log_path=log_path if isinstance(log_path, str) else "",
                queue=f"{position} of {len(matches)}" if len(matches) > 1 else ""))
        return matches
    except Exception as exc:                                # noqa: BLE001
        log.exception("Auto-book check failed for %s (non-fatal): %s", route, exc)
        return []


def spawn(route: str, matches: List[Match]) -> str:
    """Start the booking in a detached child. Returns its log file."""
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
    return os.path.relpath(log_path, REPO_ROOT)


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
            for request_id in request_ids:
                try:
                    _telegram(format_message("busy", store.get(request_id),
                                             seen_date=seen.get(request_id, "")))
                except Exception:                           # noqa: BLE001
                    pass
            return 0

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
    test = is_test(req)
    problem = gate_problem(route, test=test)
    if problem:
        # Said on Telegram too: the supervisor already announced "triggered",
        # and a refusal that only reaches a log file reads as silence.
        log.error("Not booking %s: %s", request_id, problem)
        _alert_once(f"autobook-gate:{request_id}:{problem}",
                    format_message("blocked", req, seen_date=seen_date,
                                   reason=problem))
        return
    if not test and committed_today(store.list_all()) >= cfg.max_per_day:
        log.error("Daily cap reached ([booking] max_per_day = %d) — %s waits "
                  "for tomorrow.", cfg.max_per_day, request_id)
        _alert_once(f"autobook-cap:{date.today()}",
                    format_message("cap", req, seen_date=seen_date,
                                   reason=f"{cfg.max_per_day} payment(s) today"))
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
                       ("TEST RUN (stops before payment) — " if test else "")
                       + f"earliest {seen_date or '?'} seen on {req.combo}",
                       status=store.BOOKING, seen_date=seen_date)
    rows_before = len(payment_journal.read_all())
    source, _, dest = route.partition("-")

    try:
        from src.booking.probe import run_probe
        result = run_probe(source=source, dest=dest,
                           person=req.as_registrant(),
                           entry="new", combo=req.combo,
                           walk=True, commit=not test, capture="failure")
    except Exception as exc:                                # noqa: BLE001
        # run_probe raises only before Chrome: config, account, window. The
        # payment journal is still checked, because "before Chrome" is an
        # assumption and a wrong one would be the expensive direction.
        result = _Failed(str(exc))

    if test:
        _finish_test(request_id, result, commit_step, seen_date,
                     payment_journal.read_all()[rows_before:])
        return

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
        _notify(store.get(request_id), outcome, seen_date)
    else:
        _telegram(format_message("retry", store.get(request_id),
                                 seen_date=seen_date, reason=outcome.detail))


def classify_test(result: Any, commit_step: str) -> Tuple[bool, str]:
    """(passed, detail) for a TEST run. PURE.

    Passed means the walk reached the committing step with every page before
    it done — the dry run's own "stopped at the commit boundary" — which is
    everything a real booking does short of the payment click.
    """
    walk = getattr(result, "walk", None)
    if walk is None:
        return False, "; ".join(getattr(result, "errors", None) or []) or "no walk ran"
    steps = getattr(walk, "steps", None) or []
    if walk.ok and walk.stopped_at == commit_step:
        picked = " ".join(str(_found(walk, k) or "") for k in
                          ("chosen_date", "chosen_time")).strip()
        return True, (f"walked {len(steps)} page(s) and stopped at "
                      f"'{commit_step}' before paying"
                      + (f"; slot that would be booked: {picked}" if picked else ""))
    return False, (f"stopped at '{walk.stopped_at or '?'}': "
                   f"{walk.reason or 'no reason given'}")


def _finish_test(request_id: str, result: Any, commit_step: str,
                 seen_date: str, payment_rows: List[Dict[str, Any]]) -> None:
    """Record a test run and PARK the request.

    Parked whether it passed or failed: a test request left armed would log in
    again on every slot check, and VFS blocks an account after a few logins in
    a short window. Re-enable it to test again.
    """
    if payment_rows:
        # Cannot happen with commit=False — and if it ever does, it is the
        # most important thing in this file to shout about.
        outcome = Outcome(store.NEEDS_ATTENTION, "needs_attention",
                          "TEST run wrote payment journal rows — a payment may "
                          "have been submitted. Check the card and VFS NOW.", {})
        store.record_event(request_id, outcome.event, outcome.detail,
                           status=outcome.status)
        _notify(store.get(request_id), outcome, seen_date)
        return

    passed, detail = classify_test(result, commit_step)
    store.record_event(request_id, "test_passed" if passed else "test_failed",
                       detail, status=store.WAITING)
    req = store.set_enabled(request_id, False)
    log.warning("Auto-book TEST %s -> %s: %s", request_id,
                "PASSED" if passed else "FAILED", detail)
    _telegram(format_message("test_passed" if passed else "test_failed", req,
                             seen_date=seen_date, reason=detail))


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


_HEADLINES = {
    "triggered": "🎯 BOOKING TRIGGERED",
    "blocked": "⛔ SLOT MATCHED — NOT BOOKING",
    "busy": "⏳ WAITING FOR ANOTHER BOOKING",
    "retry": "↩️ ATTEMPT FAILED — WILL RETRY",
    "booked": "✅ BOOKED AND PAID",
    "needs_attention": "⚠️ BOOKING NEEDS A HUMAN",
    "cap": "🛑 DAILY BOOKING CAP REACHED",
    "test_passed": "🧪✅ TEST RUN PASSED",
    "test_failed": "🧪❌ TEST RUN FAILED",
}

#: What the reader should do next, per message kind.
_FOOTERS = {
    "triggered": "Logging in and booking now. Next message: the result.",
    "triggered_test": "TEST MODE: logging in and walking every page; it stops "
                      "BEFORE the payment click. Next message: the result.",
    "blocked": "Nothing was booked or paid. Fix the reason above; the next "
               "slot check retries by itself.",
    "busy": "Nothing was booked. It stays waiting and the next slot check "
            "tries again.",
    "retry": "Nothing was paid. It stays waiting; the next matching slot "
             "check tries again.",
    "booked": "Appointment booked and paid with the company card.",
    "needs_attention": "It will NOT retry. Check the VFS account and the card, "
                       "then POST /booking-requests/{id}/resolve.",
    "cap": "Further matches wait until tomorrow. Raise [booking] max_per_day "
           "to allow more.",
    "test_passed": "Nothing was booked or paid. The request is now PARKED; "
                   "remove test_mode and enable it to book for real.",
    "test_failed": "Nothing was booked or paid. The request is now PARKED; "
                   "fix the reason and enable it to test again.",
}


def _mask_email(email: str) -> str:
    local, at, domain = str(email or "").partition("@")
    if not at:
        return "shared booking account"
    return f"{local[:2]}***@{domain}"


def format_message(kind: str, req: store.BookingRequest, **info: Any) -> str:
    """The Telegram text for one booking event. PURE apart from settings.

    Carries enough to act on without opening a file: who, what, which dates,
    why. Never the passport number or the password.
    """
    start, end = req.window()
    name = " ".join(str(req.data.get(k) or "") for k in
                    ("first_name", "last_name")).strip()
    headline = _HEADLINES.get(kind, kind.upper())
    if is_test(req) and kind in ("triggered", "blocked", "busy"):
        headline = f"🧪 TEST — {headline}"
    lines = [f"{headline} — {req.route}",
             f"Request: {req.request_id}" + (f" ({name})" if name else ""),
             f"Combo: {req.combo}",
             f"Window: {start} → {end}"]
    if info.get("seen_date"):
        lines.append(f"Earliest seen: {info['seen_date']}")
    if kind in ("triggered", "retry", "needs_attention", "booked"):
        attempt = int(req.data.get("attempts") or 0) + (1 if kind == "triggered" else 0)
        lines.append(f"Account: {_mask_email(req.data.get('account'))}")
        lines.append(f"Attempt: {attempt} of {_cfg().max_attempts}")
    if info.get("queue"):
        lines.append(f"Queue: {info['queue']}")
    booked = req.data.get("booked") or {}
    if kind == "booked":
        when = " ".join(str(booked.get(k, "")) for k in
                        ("appointment_date", "appointment_time")).strip()
        lines.append(f"Appointment: {when or 'see the VFS account'}")
        for key, label in (("requestrefno", "Payment ref"),
                           ("transactionid", "Transaction"),
                           ("booking_ref", "Booking ref")):
            if booked.get(key):
                lines.append(f"{label}: {booked[key]}")
    reason = info.get("reason") or (req.data.get("last_error")
                                    if kind in ("needs_attention", "retry") else "")
    if reason:
        lines.append(f"Reason: {str(reason)[:400]}")
    if info.get("log_path"):
        lines.append(f"Log: {info['log_path']}")
    lines.append("")
    footer_key = f"{kind}_test" if is_test(req) and f"{kind}_test" in _FOOTERS else kind
    lines.append(_FOOTERS.get(footer_key, "").replace("{id}", req.request_id))
    return "\n".join(lines).strip()


def _telegram(text: str) -> None:
    """To the 'testing bot' chat (the [telegram] summary channel). Never raises."""
    try:
        if not _cfg().telegram_enabled:
            return
        from src.utils import telegram
        telegram.send_error(text)
    except Exception as exc:                                # noqa: BLE001
        log.warning("Could not send the booking Telegram: %s", exc)


def _notify(req: store.BookingRequest, outcome: Outcome,
            seen_date: str = "") -> None:
    payload = {"request_id": req.request_id, "route": req.route,
               "combo": req.combo, "status": outcome.status,
               "detail": outcome.detail, **outcome.details}
    try:
        from src.utils import webhook
        webhook.notify_booking(payload)
    except Exception as exc:                                # noqa: BLE001
        log.warning("Could not post the booking webhook: %s", exc)

    kind = "booked" if outcome.status == store.BOOKED else "needs_attention"
    _telegram(format_message(kind, req, seen_date=seen_date,
                             reason=outcome.detail if kind != "booked" else ""))


def _alert_once(key: str, text: str) -> None:
    """One Telegram per key per cooldown window — a gate that blocks every
    tick must not page someone every 30 minutes."""
    try:
        from src.utils import telegram, waitlist_cooldown
        if waitlist_cooldown.is_on_cooldown(key):
            return
        if not _cfg().telegram_enabled:
            return
        waitlist_cooldown.record_sent(key)
        telegram.send_error(text)
    except Exception as exc:                                # noqa: BLE001
        log.debug("Could not send alert %s: %s", key, exc)
