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
from datetime import date, datetime, timezone
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


def minutes_since_last_attempt(req: store.BookingRequest,
                               now: Optional[datetime] = None) -> float:
    """Minutes since this request last started an attempt. inf if never."""
    now = now or datetime.now(timezone.utc)
    for entry in reversed(req.data.get("history") or []):
        if entry.get("event") != "attempt_started":
            continue
        try:
            when = datetime.strptime(str(entry.get("at", "")),
                                     "%Y-%m-%dT%H:%M:%SZ").replace(
                                         tzinfo=timezone.utc)
        except ValueError:
            return float("inf")
        return (now - when).total_seconds() / 60.0
    return float("inf")


def find_matches(route: str, earliest: Dict[str, Tuple[str, str]],
                 requests: List[store.BookingRequest],
                 today: Optional[date] = None,
                 before_window_minutes: Optional[int] = None,
                 now: Optional[datetime] = None) -> List[Match]:
    """Armed requests on `route` that are worth a login right now.

    TWO reasons to fire, and the second is why a client whose window sat one
    page ahead of VFS's earliest date used to wait forever:

      INSIDE   date_from <= earliest <= date_to. A date the client agreed to
               is on offer. Always fires.

      BEFORE   earliest < date_from. Nothing says a date inside the window is
               free, but nothing says it is not either — the checker only ever
               sees the FIRST date, and calendars run forward. So this fires
               too, at most once every `before_window_retry_minutes`, and the
               walk books inside the window or books nothing.

    An earliest date AFTER date_to never fires: no later date can be inside a
    window that has already ended.

    Oldest request first: the first sale gets the first chance.
    """
    today = today or date.today()
    if before_window_minutes is None:
        before_window_minutes = int(_cfg().before_window_retry_minutes)
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
        if seen_date < today or seen_date > end:
            continue
        if seen_date >= start:
            matches.append(Match(req.request_id, route, req.combo, seen[1]))
        elif before_window_minutes and minutes_since_last_attempt(
                req, now) >= before_window_minutes:
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
    They, not the walk, decide "booked": the walk marks its payment step NOT
    ok on every submit (so it is never retried), and the gateway's answer
    lives in the journal.
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
    if getattr(walk, "test_stopped", False):
        # Test mode: the bank never got the code, so nothing was charged. Back
        # to waiting — the journal row is answered with outcome test_stopped.
        return Outcome(store.WAITING, "test_stopped",
                       f"test mode: {walk.reason} VFS holds the appointment "
                       "unpaid until it expires.", booked_details)

    if reached:
        answered = [r for r in payment_rows if r.get("event") == "payment_result"]
        # THE GATEWAY'S ANSWER DECIDES, not the walk's flag on the payment step.
        # walk.py marks the payment step not-ok on EVERY submit (success
        # included) so that it is never retried, which made `walk.ok` false
        # on the one path that mattered: a paid booking was filed as
        # needs_attention and never reached this branch. Only the pages
        # BEFORE payment have to be clean; the payment step's own verdict is
        # the journal row.
        pre_payment_ok = all(s.ok for s in walk.steps if s.name != commit_step)
        if pre_payment_ok and any(r.get("outcome") == "success" for r in answered):
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
            "VFS_CARD_CVN for the user that runs the scheduled task, or "
            "fill in config/card.local.txt")
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
    if not booking_config.is_enabled(route, booking_config.ENTRY_NEW):
        return f"live booking is disabled in config/booking/{route}.json"
    from src.utils import route_pause
    paused = route_pause.problem(route, route_pause.LIVE)
    if paused:
        return paused
    return "" if test else card_problem()


# --------------------------------------------------------------------------- #
# Supervisor side                                                              #
# --------------------------------------------------------------------------- #


#: Monotonic time of the last maintain(), so a caller that runs often (the
#: API, on every client read) costs one comparison rather than a directory
#: scan. Per process; every process does its own first pass.
_LAST_MAINTENANCE: float = 0.0
MAINTENANCE_INTERVAL_SECONDS = 60.0


def maintain(force: bool = False) -> Dict[str, List[str]]:
    """Expire dead windows and unstick dead runs. NEVER raises.

    Used to run only inside a slot check, which meant it did not run at all
    when the scheduled task was off, when every route was paused, or for a
    route with no requests on it — so a crashed booking could sit in 'booking'
    and an expired window stay 'waiting' indefinitely. Now the API calls it
    too, on every read of a client, which is the moment someone is actually
    looking at the answer.
    """
    global _LAST_MAINTENANCE
    import time as _time

    now = _time.monotonic()
    if not force and now - _LAST_MAINTENANCE < MAINTENANCE_INTERVAL_SECONDS:
        return {"expired": [], "stuck": []}
    _LAST_MAINTENANCE = now
    out: Dict[str, List[str]] = {"expired": [], "stuck": []}
    try:
        out["expired"] = store.expire_past()
    except Exception as exc:                                # noqa: BLE001
        log.warning("Could not expire past windows: %s", exc)
    try:
        out["stuck"] = store.flag_stale()
    except Exception as exc:                                # noqa: BLE001
        log.warning("Could not check for stuck bookings: %s", exc)
    for request_id in out["stuck"]:
        try:
            _telegram(format_message("needs_attention", store.get(request_id)))
        except Exception:                                   # noqa: BLE001
            pass
    return out


def handle_route_checked(route: str, slot_results: List[Any]) -> List[Match]:
    """Called by the supervisor after each route. NEVER raises.

    Cheap in the common case — no request, or no date inside any window —
    and starts a background booking process only when something matches.
    """
    try:
        maintain()
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
                                env={**os.environ, "PYTHONUNBUFFERED": "1",
                                     "PYTHONIOENCODING": "utf-8:replace"},
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
                    _skipped(request_id, "another booking was running on this "
                                         "machine; it stays in the queue",
                             seen.get(request_id, ""))
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
        _skipped(request_id, problem, seen_date)
        _alert_once(f"autobook-gate:{request_id}:{problem}",
                    format_message("blocked", req, seen_date=seen_date,
                                   reason=problem))
        return
    if not test and committed_today(store.list_all()) >= cfg.max_per_day:
        log.error("Daily cap reached ([booking] max_per_day = %d) — %s waits "
                  "for tomorrow.", cfg.max_per_day, request_id)
        _skipped(request_id, f"the daily booking cap ({cfg.max_per_day}) is "
                             "reached; it will be tried again tomorrow",
                 seen_date)
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

    # THE JOIN KEY, recorded before the browser starts. Without it an attempt
    # cannot be tied to its log lines (logs/app.jsonl), its screenshots
    # (runs/<ROUTE>/<run_id>/) or its payment rows — all of which carry the
    # run id and none of which the web app could find from the request alone.
    from src.booking import attempt_job
    from src.utils import run_context

    # A FRESH id per request, not per process: one spawn books a queue of them,
    # and they are separate runs by every measure — log lines, capture folder,
    # payment rows, and the job record the web app opens.
    run_id = run_context.begin_run()
    job_log = attempt_job.log_path(run_id)
    _tee_log(job_log)
    attempt_job.started(run_id, request_id=request_id, route=route,
                        combo=req.combo, test=test, log_file=job_log)
    started_at = datetime.now(timezone.utc).isoformat()
    store.record_event(request_id, "attempt_started",
                       ("TEST RUN (stops before payment) — " if test else "")
                       + f"earliest {seen_date or '?'} seen on {req.combo}",
                       status=store.BOOKING, seen_date=seen_date,
                       run_id=run_id, job_id=run_id, trigger="auto")
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

    new_rows = payment_journal.read_all()[rows_before:]
    if test:
        passed = _finish_test(request_id, result, commit_step, seen_date,
                              rows_for_run(new_rows, run_id, new_rows))
        attempt_job.finished(
            run_id, request_id=request_id, route=route, combo=req.combo,
            status="succeeded" if passed else "failed",
            detail=store.get(request_id).data.get("last_error") or
            ("test run passed" if passed else "test run failed"),
            outcome="completed" if passed else "failed",
            needs_attention=store.get(request_id).status == store.NEEDS_ATTENTION,
            started_at=started_at, log_file=job_log, test=True)
        _close_tee()
        return

    payment_rows = rows_for_run(new_rows, run_id, new_rows)
    outcome = classify(result, commit_step, payment_rows)

    if outcome.status == store.WAITING:
        # A FAILURE THAT CANNOT COME RIGHT ON ITS OWN is not worth another
        # login: VFS blocks an account after a few, and the next five ticks
        # would spend them reaching the same wrong password.
        permanent = permanent_problem(outcome.detail)
        if permanent:
            outcome = Outcome(store.NEEDS_ATTENTION, "needs_attention",
                              f"{outcome.detail} {permanent}", {})
        else:
            # +1 because THIS failure has not been recorded yet: it is the
            # (failures + 1)-th in a row.
            failures = int(store.get(request_id).data.get(
                "consecutive_failures") or 0) + 1
            if failures >= cfg.max_attempts:
                outcome = Outcome(
                    store.NEEDS_ATTENTION, "needs_attention",
                    f"gave up after {failures} failed attempt(s) in a row; "
                    f"last: {outcome.detail}", {})

    store.record_event(request_id, outcome.event, outcome.detail,
                       status=outcome.status, run_id=run_id,
                       **outcome.details)
    log.warning("Auto-book %s -> %s: %s", request_id, outcome.status, outcome.detail)
    job_state, job_outcome = job_status(outcome, payment_rows)
    attempt_job.finished(
        run_id, request_id=request_id, route=route, combo=req.combo,
        status=job_state,
        detail=outcome.detail,
        outcome=job_outcome,
        needs_attention=outcome.status == store.NEEDS_ATTENTION,
        started_at=started_at, log_file=job_log,
        results=[{"route": route, "combo": req.combo,
                  "registrant_id": request_id, "status": outcome.status,
                  "reason": outcome.detail}])
    _close_tee()
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
                 seen_date: str, payment_rows: List[Dict[str, Any]]) -> bool:
    """Record a test run and PARK the request. True when it passed.

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
        return False

    passed, detail = classify_test(result, commit_step)
    store.record_event(request_id, "test_passed" if passed else "test_failed",
                       detail, status=store.WAITING)
    req = store.set_enabled(request_id, False)
    log.warning("Auto-book TEST %s -> %s: %s", request_id,
                "PASSED" if passed else "FAILED", detail)
    _telegram(format_message("test_passed" if passed else "test_failed", req,
                             seen_date=seen_date, reason=detail))
    return passed


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
# Manual runs on a stored request                                              #
# --------------------------------------------------------------------------- #
#
# A run started by hand (POST /v1/booking/runs with a client_id, or
# `probe --request`) used to bypass all of the above: no lock, no status, no
# attempt count. A manual commit that PAID left the request "waiting", and the
# next slot check inside the window would book and charge the client again.
# It also took no booking lock, so it could run at the same moment as the
# automatic booking of the same client.
#
# Now a manual run goes through the same lane lock, the same status machine
# and the same classify() as the automatic path. Only the trigger differs.


class ManualRunRefused(Exception):
    """A manual run on a request that must not run now. Nothing was started."""


def payment_submitted_since_last_attempt(req: store.BookingRequest) -> str:
    """When the payment journal shows a card submit for this request's route
    since its last attempt started: that row's time. "" when there is none.

    The journal row is fsync'd BEFORE the click that charges the card
    (src/payment/journal.py), so no row means no charge was possible. Rows
    are matched by route and time rather than by request id, which errs
    towards finding one — i.e. towards refusing. An unreadable journal is
    reported as a submit for the same reason.
    """
    started = ""
    for entry in reversed(req.data.get("history") or []):
        if entry.get("event") == "attempt_started":
            started = str(entry.get("at") or "")
            break
    try:
        from src.payment import journal as payment_journal

        rows = payment_journal.read_all()
    except Exception as e:                                  # noqa: BLE001
        return f"payment journal unreadable: {e}"
    route = str(req.data.get("route") or "").upper()
    for row in reversed(rows):
        if row.get("event") != "payment_submitting":
            continue
        if str(row.get("at") or "") < started:
            continue
        if str(row.get("route") or route).upper() == route:
            return str(row.get("at") or "time unknown")
    return ""


def manual_refusal(req: store.BookingRequest, commit: bool) -> str:
    """Why this request cannot take a manual run, or "". PURE.

    A walk spends nothing, so it is refused only while another run is driving
    the request. A commit may spend money, so it needs status "waiting":
    "booked" would book twice, and "needs_attention" means a human has not yet
    said whether the last attempt paid.
    """
    if req.status == store.BOOKING:
        return (f"{req.request_id} is being booked right now by another run. "
                "Wait for its result.")
    if not commit:
        return ""
    if req.status == store.BOOKED:
        return (f"{req.request_id} is already BOOKED — a commit would book and "
                "charge a second time.")
    if req.status == store.NEEDS_ATTENTION:
        # MANUAL ONLY, and only when no card can have been charged (operator
        # decision 2026-10-23). Most needs_attention runs stopped AFTER Pay
        # Online but BEFORE the card was submitted — gateway never reached,
        # card form failed, VFS ended the session — and re-running those risks
        # a held slot, not money. Auto-book still leaves them alone.
        submitted = payment_submitted_since_last_attempt(req)
        if submitted:
            return (f"{req.request_id} needs a human first: a payment was "
                    f"submitted on its last attempt ({submitted}) and VFS has "
                    "not confirmed the result. Running it again could charge "
                    "the card twice. Check VFS and the card, then POST "
                    f"/v1/clients/{req.request_id}/resolve.")
        return ""
    if req.status == store.EXPIRED:
        return f"{req.request_id} has expired (its window has passed)."
    if req.status != store.WAITING:
        return f"{req.request_id} is {req.status}, not waiting."
    return ""


def manual_run(request_id: str, *, commit: bool, run: Any,
               started_by: str = "") -> Any:
    """Run `run()` (a run_probe call) as a tracked attempt on a request.

    Raises ManualRunRefused before anything starts. Returns run()'s result.
    """
    from src.booking import config as booking_config
    from src.payment import journal as payment_journal
    from src.utils import runlock

    who = f"MANUAL ({started_by})" if started_by else "MANUAL"
    with runlock.acquire("manual-booking", lane=runlock.LANE_BOOKING,
                         timeout=0, on_busy="skip") as lock:
        if not lock.held:
            raise ManualRunRefused(
                "Another booking is running on this machine"
                + (f" ({lock.holder_hint})" if getattr(lock, "holder_hint", "")
                   else "") + ". Try again when it finishes.")

        # Re-read under the lock: the automatic path may have just finished.
        req = store.get(request_id)
        refusal = manual_refusal(req, commit)
        if refusal:
            raise ManualRunRefused(refusal)

        if not commit:
            # Nothing can be spent, so no status change and no attempt counted:
            # a rehearsal must not use up the client's automatic attempts.
            result = run()
            commit_step = booking_config.commit_step_name(
                req.route, booking_config.ENTRY_NEW)
            passed, detail = classify_test(result, commit_step)
            store.record_event(request_id,
                               "manual_walk_passed" if passed
                               else "manual_walk_failed",
                               f"{who}: {detail}")
            return result

        # Telegram first: "triggered" counts the attempt that is about to start.
        _telegram(format_message("triggered", req,
                                 manual=started_by or "operator"))
        # trigger="manual": a run a person started must not spend the
        # AUTOMATIC retry budget. Testing a request used to exhaust it, and the
        # bot then refused to book that client ever again on its own.
        from src.utils import run_context
        store.record_event(request_id, "attempt_started",
                           f"{who} commit", status=store.BOOKING,
                           trigger="manual", run_id=run_context.run_id())
        rows_before = len(payment_journal.read_all())
        raised: Optional[Exception] = None
        try:
            result = run()
        except Exception as exc:                            # noqa: BLE001
            # run_probe raises only before Chrome (config, account, window),
            # the same as on the automatic path, which treats it as a failed
            # attempt. classify() still reads the payment journal, so if that
            # assumption is ever wrong it fails towards needs_attention.
            raised, result = exc, _Failed(str(exc))
        except BaseException as exc:
            # Ctrl-C or a kill mid-run. The request must not stay "booking"
            # forever, and an interrupt gives no proof nothing was paid.
            store.record_event(
                request_id, "needs_attention",
                f"{who}: the run crashed ({type(exc).__name__}: {exc}). "
                "Whether it booked or paid is unknown.",
                status=store.NEEDS_ATTENTION)
            _notify(store.get(request_id),
                    Outcome(store.NEEDS_ATTENTION, "needs_attention",
                            f"manual run crashed: {exc}", {}))
            raise

        commit_step = booking_config.commit_step_name(
            req.route, booking_config.ENTRY_NEW)
        new_rows = payment_journal.read_all()[rows_before:]
        outcome = classify(result, commit_step,
                           rows_for_run(new_rows, run_context.run_id(),
                                        new_rows))
        store.record_event(request_id, outcome.event, f"{who}: {outcome.detail}",
                           status=outcome.status, **outcome.details)
        log.warning("Manual booking %s -> %s: %s", request_id, outcome.status,
                    outcome.detail)
        if outcome.notify:
            _notify(store.get(request_id), outcome)
        else:
            _telegram(format_message("retry", store.get(request_id),
                                     reason=outcome.detail,
                                     manual=started_by or "operator"))
        if raised is not None:
            raise raised
        return result


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
                       "then POST /v1/clients/{id}/resolve.",
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
    if info.get("manual"):
        lines.append(f"Started by hand: {info['manual']}")
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


#: The handler writing the current attempt's own log file, or None.
_TEE: Optional[logging.Handler] = None


def _tee_log(path: str) -> None:
    """Also write this attempt's log lines to `path`, for the jobs API.

    The spawned child already logs to logs/booking_auto/<stamp>_<route>.log —
    one file for the WHOLE queue, and in a directory the API does not serve. A
    web app asking "what happened on this attempt?" needs this attempt's lines,
    at the path its job record names.
    """
    global _TEE
    _close_tee()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(handler)
        _TEE = handler
    except Exception as exc:                                # noqa: BLE001
        log.debug("Could not open the attempt log %s: %s", path, exc)


def _close_tee() -> None:
    global _TEE
    if _TEE is None:
        return
    try:
        logging.getLogger().removeHandler(_TEE)
        _TEE.close()
    except Exception:                                       # noqa: BLE001
        pass
    _TEE = None


#: Failures that the next tick cannot do any better on, and what the operator
#: has to change. Matched against the attempt's own reason text, lower-cased.
#: Deliberately narrow: anything not listed is assumed RETRYABLE, because
#: retrying costs a login while giving up costs a booking.
_PERMANENT = (
    (("invalidcredentials", "wrong credentials", "is disabled",
      "not registered with us", "emailnotregistered", "accessrestricted",
      "accountblocked"),
     "VFS will reject this account on every retry — fix the account, then "
     "resolve this request."),
    (("required for booking", "required for payment", "could not select '",
      "no vfs account to book with", "is not a country this system knows"),
     "Every retry fills the same form with the same value — correct the "
     "request, which also re-arms it."),
    (("booking config", "payment config is unreadable", "is disabled in "
      "config/booking", "no live booking"),
     "A configuration problem; no retry can help until it is fixed."),
)


def permanent_problem(reason: str) -> str:
    """What makes `reason` unretryable, or "" when another tick might work."""
    text = " ".join(str(reason or "").split()).lower()
    for needles, advice in _PERMANENT:
        if any(n in text for n in needles):
            return advice
    return ""


def job_status(outcome, payment_rows: List[Dict[str, Any]]) -> tuple:
    """(job status, run-level outcome) for the API's job list.

    The AUTOMATIC lane writes its own job records (attempt_job.finished)
    instead of being supervised as a subprocess, so it cannot inherit the
    CLI's exit codes and has to reach the same verdict independently.

    *** "NOT BOOKED" IS NOT THE SAME AS "FAILED". *** This used to be
    `"succeeded" if BOOKED else "failed"`, which filed a DECLINED payment and
    an UNANSWERED one — both of which may have taken money — under the same
    red word as a mistyped selector. The remedy differs completely: a failure
    is re-armed once it is fixed, and a payment is never re-run at all.

    A booked run stays "succeeded": the card question is answered separately,
    by JobRecord.payment_view reading the same journal, so the happy path does
    not need a special status to carry it.
    """
    if outcome.status == store.BOOKED:
        return "succeeded", "completed"

    submitted = any(r.get("event") == "payment_submitting"
                    for r in payment_rows)
    answered = [r for r in payment_rows if r.get("event") == "payment_result"]
    if answered and str(answered[-1].get("outcome") or "") == "declined":
        return "payment_declined", "payment_declined"
    if submitted or answered:
        return "payment_submitted", "payment_submitted"
    return "failed", "failed"


def rows_for_run(rows: List[Dict[str, Any]], run_id: str,
                 fallback: Optional[List[Dict[str, Any]]] = None
                 ) -> List[Dict[str, Any]]:
    """The payment-journal rows this run wrote, or that nobody owns.

    Matched on `run_id`, which journal.append() stamps on every row. The old
    rule — "everything appended since this attempt started" — is only correct
    while exactly one process can be writing, and it decides whether a card was
    charged.

    Exactly two kinds of row count as this run's:

        stamped with OUR run_id   ours, definitively
        stamped with NO run_id    nobody's, and a payment row nobody attributes
                                  is the one failure worth being loud about

    A row stamped with ANOTHER run's id is, definitively, NOT OURS — and that
    is the case the old `fallback` got wrong. When no row matched and none was
    unstamped it returned the whole positional slice, other runs' rows
    included, so a concurrent booking's payment was reported as this run's. On
    2026-10-26 that produced "TEST run wrote payment journal rows — a payment
    may have been submitted. Check the card and VFS NOW." for a run that lasted
    150ms and never started Chrome (job 6adf314a89a1ff). An alarm that cries
    wolf on the one thing that must never be ignored is worse than no alarm.

    `fallback` is accepted and IGNORED, so the three call sites that pass the
    slice twice keep working. It never added anything: every caller passes the
    same list as `rows`, and the unstamped rows it was meant to rescue are
    already picked up from `rows` directly.
    """
    return [r for r in rows
            if not r.get("run_id") or str(r.get("run_id")) == run_id]


def _skipped(request_id: str, reason: str, seen_date: str = "") -> None:
    """Record a tick that matched this request and then did NOT book it.

    These four — a gate, the daily cap, a busy lane, a request no longer
    armed — used to reach Telegram and the log only. The web app showed
    "waiting" with nothing to explain it, which made "why was my client not
    booked?" unanswerable from the API. Status is deliberately NOT changed:
    nothing was attempted, so nothing is spent and the next tick may book it.
    """
    try:
        store.record_event(request_id, "skipped", reason, seen_date=seen_date)
    except Exception as exc:                                # noqa: BLE001
        log.debug("Could not record the skip for %s: %s", request_id, exc)


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
