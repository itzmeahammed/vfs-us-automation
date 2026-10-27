"""Background job execution for the webhook API.

Design notes:

* `asyncio.create_subprocess_exec` (not `subprocess.run`, not `shell=True`).
  - It is awaitable, so the event loop keeps serving requests while a job runs.
  - `_exec` takes an argv LIST straight to the OS, with no shell in between.
    There is no string for a metacharacter to hide in, so command injection is
    structurally impossible rather than merely filtered against.

* The endpoint returns as soon as the process is SPAWNED, not when it finishes.
  We await only `create_subprocess_exec` itself (milliseconds — it returns once
  the OS has forked the child), then hand the waiting to a detached asyncio
  task. A failure to *start* is therefore still reported synchronously as a
  clean 500, while the job's own runtime never blocks the response.

* Output goes to a per-job log file rather than a pipe. An unread PIPE fills its
  OS buffer and deadlocks a chatty child (the Playwright bot is very chatty);
  a file has no such limit.

* Every state transition is also appended to a JSONL history (see jobstore.py),
  so a restart mid-run cannot turn "did this job register anyone?" into an
  unanswerable question.

* Single-flight is enforced at TWO levels: this manager's own lock (fast, and
  the only one that can produce a clean 409) and the machine-wide run lock in
  src/utils/runlock.py, which also excludes manual runs and the autotrigger.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import subprocess
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.api.core.config import ApiSettings, get_settings
from src.utils import run_context
from src.api.modules.jobs.store import JobStore, prune_logs, reconcile

log = logging.getLogger("vfs.api.jobs")


# The waitlist CLI returns 2 when it stopped because a real, bookable slot
# exists. That is a BETTER outcome than a waitlist registration, so it gets its
# own job status rather than being lumped in with failures.
EXIT_SLOTS_AVAILABLE = 2

# The BOOKING CLI's money-moved exit codes. Kept in sync with
# src/booking/__main__.py, where the full table and the reasoning live.
#
# *** NEITHER OF THESE IS A FAILURE, AND NEITHER IS A SUCCESS. *** Both mean
# the card was touched, so both mean "stop and check, never retry". They exist
# because the alternative was exit 1: on 2026-10-27 a fully paid booking
# (run 37b0e0e9d937731c, 3-D Secure cleared, PaymentStatus=True from VFS) was
# listed by this API as "failed — Process exited with code 1", while the
# booking-request endpoint for the same run said "booked and paid". An operator
# reading the job list would have concluded the payment had not gone through.
EXIT_PAYMENT_SUBMITTED = 3
EXIT_PAYMENT_DECLINED = 4

# Markers the CLI prints around its machine-readable result block. Kept in sync
# with src/waitlist/__main__.py — a mismatch degrades gracefully (no parsed
# results) rather than breaking the job.
RESULT_JSON_BEGIN = "---VFS-RESULT-JSON-BEGIN---"
RESULT_JSON_END = "---VFS-RESULT-JSON-END---"

# How much of a job log to read when hunting for the result block. The block is
# emitted last and is a few KB at most; 512 KB is generous headroom while
# keeping the read bounded regardless of how chatty the run was.
RESULT_TAIL_BYTES = 512 * 1024

# Ceiling on a single GET /jobs/{id}/logs response.
MAX_LOG_TAIL_LINES = 2_000
MAX_LOG_TAIL_BYTES = 1024 * 1024


def _read_tail(path: str, max_bytes: int) -> str:
    """Return the last `max_bytes` of a file, decoded leniently.

    Seeks rather than reads forward, so cost is independent of file size. The
    first line of the window is usually cut mid-character or mid-line; decoding
    with errors="replace" absorbs that, and every caller here searches for a
    marker rather than assuming the window starts cleanly.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        raw = fh.read()
    return raw.decode("utf-8", errors="replace")


def read_log_tail(path: str, lines: int) -> Tuple[List[str], bool]:
    """Last `lines` lines of a job log, plus whether it was truncated.

    Exposed so the API can serve logs to a remote caller: handing back an
    absolute path on this desktop is useless to a web app and mildly
    disclosive, whereas the content is exactly what an operator wants.
    """
    lines = max(1, min(lines, MAX_LOG_TAIL_LINES))
    text = _read_tail(path, MAX_LOG_TAIL_BYTES)
    all_lines = text.splitlines()
    # The first line of a byte-window is probably a fragment — drop it unless
    # the window covered the whole file.
    truncated = False
    try:
        truncated = os.path.getsize(path) > MAX_LOG_TAIL_BYTES
    except OSError:
        pass
    if truncated and all_lines:
        all_lines = all_lines[1:]
    tail = all_lines[-lines:]
    return tail, truncated or len(all_lines) > len(tail)


class JobStatus(str, Enum):
    """Lifecycle of a triggered job."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    # A bookable slot appeared, so the run stopped without registering.
    SLOTS_AVAILABLE = "slots_available"
    # THE CARD WAS CHARGED, or may have been. Not a success (the gateway's
    # answer is the journal row, not this status) and emphatically not a
    # failure. Re-running is a SECOND charge.
    PAYMENT_SUBMITTED = "payment_submitted"
    # The gateway answered and refused. Still not an ordinary failure: VFS's
    # own page says funds may have been deducted anyway, so this needs a human
    # and never a retry.
    PAYMENT_DECLINED = "payment_declined"
    # The API restarted while this job was running; its outcome was never
    # recorded and cannot be reconstructed. Needs a human.
    UNKNOWN = "unknown"


# Statuses that mean the job is over. Anything else is still in flight.
TERMINAL_STATUSES = frozenset({
    JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.TIMED_OUT,
    JobStatus.CANCELLED, JobStatus.SLOTS_AVAILABLE, JobStatus.UNKNOWN,
    JobStatus.PAYMENT_SUBMITTED, JobStatus.PAYMENT_DECLINED,
})

#: Statuses where money may have moved. A caller must not re-run one of these,
#: and the web app should not offer a retry button on one. Grouped rather than
#: string-matched at each call site, because the whole point is that the rule
#: is the same for both and must not drift apart.
MONEY_MOVED_STATUSES = frozenset({
    JobStatus.PAYMENT_SUBMITTED, JobStatus.PAYMENT_DECLINED,
})


class JobStartError(RuntimeError):
    """Raised when the child process could not be spawned at all.

    Distinct from "the job ran and failed": this means the interpreter or
    script was missing, the cwd did not exist, or the OS refused the exec.
    """


class JobAlreadyRunningError(JobStartError):
    """Raised when single-flight refuses a second concurrent job.

    A DISTINCT type rather than a message the caller string-matches on: the
    endpoint maps this to 409 (retry later) and every other JobStartError to
    500 (broken), and that mapping must not depend on the wording of a message.
    """


def _booking_failure(record: "JobRecord") -> str:
    """The failure a booking job recorded on its request, or "".

    Booking runs write no result block; their reason is the request's own
    history entry for this run (attempt_failed / needs_attention / ...),
    so the newest error entry at or after the job's start is the one.
    """
    request_id = (record.payload or {}).get("request_id")
    if not request_id:
        return ""
    try:
        from src.booking import requests as req_store
        from src.utils.public_errors import ERROR_EVENTS

        started = record.started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        for entry in reversed(req_store.get(request_id).data.get("history")
                              or []):
            if str(entry.get("at", "")) < started:
                break
            if str(entry.get("event", "")).lower() in ERROR_EVENTS:
                return str(entry.get("detail") or "")
    except Exception:                                       # noqa: BLE001
        pass
    return ""


def payment_facts(run_id: str) -> Tuple[bool, str]:
    """(was a payment submitted, what the gateway answered) for this run.

    The answer is "success" | "declined" | whatever the row said | "" when the
    gateway never answered. The two halves are returned together because they
    are different questions and the second does not imply the first: a run can
    submit a payment and never learn the outcome, which is the state that most
    needs saying out loud.

    READ FROM THE JOURNAL, not inferred from an exit code or a job status. The
    exit code says the click happened; only the journal says what came back.

    Matched on run_id, which is why run_id exists: for an API-triggered job it
    equals job_id, so the journal, the log lines and the runs/ folder are all
    reachable from the one id the web app already holds.
    """
    if not run_id:
        return False, ""
    try:
        from src.payment import journal
        from src.booking.autobook import rows_for_run

        rows = rows_for_run(journal.read_all(), run_id)
        if not rows:
            return False, ""
        submitted = any(r.get("event") == "payment_submitting" for r in rows)
        answered = [r for r in rows if r.get("event") == "payment_result"]
        answer = str(answered[-1].get("outcome") or "") if answered else ""
        # An answer without a submitting row still means a payment happened:
        # the row exists because something was paid for.
        return bool(submitted or answer), answer
    except Exception:                                       # noqa: BLE001
        # A journal that cannot be read must never change what a job reports:
        # the status already carries the safe reading on its own.
        log.warning("Could not read the payment journal for run %s.", run_id,
                    exc_info=True)
    return False, ""


def payment_outcome(run_id: str) -> str:
    """Just the gateway's answer. See payment_facts."""
    return payment_facts(run_id)[1]


def _payment_detail(record: "JobRecord", paid: str, unresolved: str) -> str:
    """`paid` if the gateway approved it, else `unresolved`.

    So the API stops hedging when it does not have to. A run whose journal
    carries `payment_result: success` HAS a definite answer, and telling the
    operator to "check the card" on one of those is how a real confirmation
    gets lost among the ones that need looking at.
    """
    return paid if payment_outcome(
        record.run_id or record.job_id) == "success" else unresolved


@dataclass
class JobRecord:
    """Everything we know about one triggered job."""

    job_id: str
    command: List[str]
    status: JobStatus
    started_at: datetime
    # The join key across every record system: this job, the child's log lines,
    # its journal rows and its runs/ folder all carry it. Equal to job_id for
    # API-triggered runs — the API mints one id and uses it for both, so there
    # is no mapping table to consult. Kept as its own field anyway, because a
    # manually-started run has a run_id and no job_id, and code that correlates
    # should read the field that means "correlate on this".
    run_id: str = ""
    pid: Optional[int] = None
    finished_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    log_file: Optional[str] = None
    detail: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    # Per-client outcomes parsed from the run's JSON block. Empty when the job
    # produced none (placeholder job, crash before the block, older CLI).
    results: List[Dict[str, Any]] = field(default_factory=list)
    # Run-level verdict from the same block: "completed" | "slots_available"
    # | "failed".
    outcome: Optional[str] = None
    # Why a failed run stopped, {code, message}, from the same block.
    error: Optional[Dict[str, str]] = None
    needs_attention: bool = False
    # Set by cancel() BEFORE the child is signalled. The supervisor reads it
    # when the process exits and reports CANCELLED rather than FAILED — see
    # _supervise. Without this the terminate's non-zero exit code overwrites
    # the cancellation and the CANCELLED state is unreachable via the API.
    cancel_requested: bool = False
    # Echoed back on an idempotent replay so a caller can tell the difference
    # between "I started this" and "you already had this".
    idempotency_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe view for API responses."""
        return {
            "job_id": self.job_id,
            "run_id": self.run_id or self.job_id,
            "status": self.status.value,
            "command": self.command,
            "pid": self.pid,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "exit_code": self.exit_code,
            "log_file": self.log_file,
            "detail": self.detail,
            "payload": self.payload,
            "outcome": self.outcome,
            "error": self.error,
            "results": self.results,
            "needs_attention": self.needs_attention,
        }

    def payment_view(self) -> Optional[Dict[str, Any]]:
        """What happened to the card, or None when no payment was attempted.

        THE WEB APP SHOULD NOT HAVE TO READ A LOG FILE TO ANSWER "was it
        paid?". Before this, the only machine-readable answer lived in
        state/payments.jsonl on the server's disk, so the console showed a
        status and a sentence and left the operator to open a terminal. The
        question is asked of every commit run, so it gets a field.

        Gated on THE JOURNAL, not on this job's status. Gating on the status
        would have hidden it on the one path that matters most: an automatic
        booking that completes is recorded as "succeeded", so a status gate
        would report the card for every troubled run and stay silent on every
        clean one.

        `charged` is deliberately three-valued:
            True   the gateway approved it - safe to tell the client
            False  the gateway refused it, though funds may still have moved
            None   SUBMITTED AND UNANSWERED. Not "no": unknown. The honest
                   answer, and the one a boolean would have had to lie about.
        """
        submitted, answer = payment_facts(self.run_id or self.job_id)
        if not submitted:
            return None

        if answer == "success":
            charged = True
        elif answer == "declined" or self.status == JobStatus.PAYMENT_DECLINED:
            charged = False
        else:
            charged = None

        return {
            "submitted": True,
            "charged": charged,
            "gateway_answer": answer or "not_answered",
            # Never true. A payment that may have been taken is never safe to
            # re-run, and the field exists so a web app does not have to
            # rediscover that rule per status.
            "retry_safe": False,
            "journal": "state/payments.jsonl",
        }

    def to_public(self) -> Dict[str, Any]:
        """What the web app sees: every error as ONE sentence.

        to_dict() stays raw — it is what gets persisted, and operators read
        the raw text in the log. A failed booking job's own detail was only
        "Process exited with code 1"; the actual reason lives in the booking
        request's history, so it is fetched from there.
        """
        from src.utils import public_errors as pe

        out = self.to_dict()
        # Derived from the payment journal on every read, so it is added
        # HERE and not in to_dict(): to_dict() is the persisted form, and a
        # stored snapshot of this would be read back by nothing and could
        # drift from the journal — in the one record that is about money.
        out["payment"] = self.payment_view()
        failed = self.status.value not in pe.OK_STATUSES and \
            self.status.value != JobStatus.SUCCEEDED.value
        # A detail written here, for an operator, about whether a card
        # was charged. The translator below would replace it with a
        # generic fallback.
        if self.status.value in pe.AUTHORED_DETAIL_STATUSES:
            failed = False
        # A crash the code did not foresee is "problem not identified", not
        # the proxy fallback: nothing says the proxy was at fault.
        fallback = (pe.UNIDENTIFIED
                    if (self.error or {}).get("code") == "unexpected_error"
                    else pe.FALLBACK)
        if failed:
            raw = (self.error or {}).get("message") or \
                _booking_failure(self) or self.detail
            out["detail"] = pe.public_message(raw, fallback=fallback)
        elif self.detail:
            out["detail"] = pe.one_line(self.detail)
        if self.error:
            out["error"] = {**self.error,
                            "message": pe.public_message(
                                self.error.get("message"), fallback=fallback)}
        out["results"] = [
            {**r, "reason": pe.public_reason(r.get("reason"), r.get("status"))}
            if isinstance(r, dict) and r.get("reason") else r
            for r in (self.results or [])]
        return out

    def to_record(self) -> Dict[str, Any]:
        """Full persistence view — to_dict plus the fields only we need back."""
        record = self.to_dict()
        record["idempotency_key"] = self.idempotency_key
        return record

    @classmethod
    def from_record(cls, data: Dict[str, Any]) -> Optional["JobRecord"]:
        """Rebuild a record loaded from the JSONL history.

        Returns None for anything unparseable rather than raising: a single
        corrupt record must cost that one job's history, not the server's boot.
        """
        try:
            job_id = data["job_id"]
            if not isinstance(job_id, str) or not job_id:
                return None
            status_value = str(data.get("status", ""))
            try:
                status = JobStatus(status_value)
            except ValueError:
                status = JobStatus.UNKNOWN
            started = _parse_iso(data.get("started_at"))
            if started is None:
                return None
            return cls(
                job_id=job_id,
                # Older records predate run_id; fall back to job_id, which is
                # what a new record would have used anyway.
                run_id=str(data.get("run_id") or job_id),
                command=list(data.get("command") or []),
                status=status,
                started_at=started,
                pid=data.get("pid"),
                finished_at=_parse_iso(data.get("finished_at")),
                exit_code=data.get("exit_code"),
                log_file=data.get("log_file"),
                detail=data.get("detail"),
                payload=dict(data.get("payload") or {}),
                results=list(data.get("results") or []),
                outcome=data.get("outcome"),
                error=data.get("error") if isinstance(data.get("error"), dict) else None,
                needs_attention=bool(data.get("needs_attention")),
                idempotency_key=data.get("idempotency_key"),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _key_fingerprint(key: str) -> str:
    """A short, non-reversible label for an idempotency key, safe to log.

    The key is caller-chosen and may well be a request id that appears in their
    own logs; we still avoid writing it verbatim, on the same principle as never
    logging the token.
    """
    import hashlib
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


#: Job lanes. Each lane has its own single-flight slot here and its own
#: machine-wide lock in src/utils/runlock.py, so a booking run never waits
#: behind a waitlist registration (or the other way round). Within a lane,
#: runs stay strictly one at a time — that is what protects the journal and
#: stops one client being booked twice.
LANE_WAITLIST = "waitlist"
LANE_BOOKING = "booking"
LANES = (LANE_WAITLIST, LANE_BOOKING)


def _machine_lock_busy(lane: str = LANE_WAITLIST) -> str:
    """Is another browser-driving run holding the machine-wide lock?

    Returns a short holder hint when busy, or "" when the lock is free.

    We take the lock non-blockingly and RELEASE it immediately. The child
    process takes it properly for the duration of its run; holding it here
    would deadlock that child, which is a separate process and so does not
    benefit from runlock's in-process re-entrancy.

    That leaves a millisecond-wide race between this check and the child's own
    acquire. The check is therefore a fast-fail courtesy — it turns the common
    case ("a run is already going") into an immediate clean 409 instead of a
    child that blocks for 900s and then dies — not the correctness boundary.
    The real mutual exclusion is the child's own acquire, which is unchanged.
    """
    try:
        from src.utils import runlock
    except Exception:                              # noqa: BLE001
        return ""                                  # lock unavailable: do not block

    try:
        # The WAITLIST lane specifically. Probing the slot-check lane would 409
        # a perfectly valid registration just because a routine slot check
        # happened to be running — the two are independent now.
        machine_lane = (runlock.LANE_BOOKING if lane == LANE_BOOKING
                        else runlock.LANE_WAITLIST)
        with runlock.acquire("api-trigger-probe", lane=machine_lane,
                             timeout=0, on_busy="skip") as handle:
            if handle.held:
                return ""
            return handle.holder_hint or "busy"
    except Exception as exc:                       # noqa: BLE001
        # A broken lock primitive must not make the API unusable. Log and let
        # the trigger through — the child still takes the lock for real.
        log.warning("Could not probe the machine run lock: %s", exc)
        return ""


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp back into an aware datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Everything we write is UTC-aware; a naive value from a hand-edited file
    # is assumed UTC rather than rejected.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class JobManager:
    """Spawns and tracks background jobs. One instance per server process."""

    def __init__(self, settings: Optional[ApiSettings] = None) -> None:
        self._settings = settings or get_settings()
        # Ordered so we can evict the oldest record when history fills up.
        self._jobs: "OrderedDict[str, JobRecord]" = OrderedDict()
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._tasks: Dict[str, "asyncio.Task[None]"] = {}
        # Guards the single-flight check + insert against two simultaneous
        # requests both seeing "nothing running" and both spawning.
        self._lock = asyncio.Lock()
        # The id of the one running job, maintained as state rather than
        # rediscovered by scanning every record on each single-flight check.
        # lane -> the job running in it. One slot per lane, not one overall.
        self._active: Dict[str, str] = {}
        # Durable history. Survives the restart that the in-memory dict cannot.
        self._store = JobStore(self._settings.job_history_file)
        # Idempotency-Key -> (job_id, monotonic timestamp).
        self._idempotency: "OrderedDict[str, Tuple[str, float]]" = OrderedDict()
        #: (mtime_ns, size) of the history file at the last external sync.
        self._external_stamp: Optional[Tuple[int, int]] = None

    # -- Startup -------------------------------------------------------------

    def load_history(self) -> int:
        """Seed the in-memory registry from disk and reconcile orphans.

        Called once at startup, before the server accepts requests. Returns the
        number of jobs that had to be marked `unknown` because a previous
        process died while they were running.
        """
        records = self._store.load_all()
        changed = reconcile(records)
        for record in changed:
            log.error(
                "Job %s was still 'running' when the API last stopped — marked "
                "unknown. A registration may have completed unrecorded; check "
                "the VFS account and %s.",
                record.get("job_id"), record.get("log_file") or "its log file",
            )
            self._store.append(record)

        loaded = 0
        for data in records.values():
            job = JobRecord.from_record(data)
            if job is None:
                continue
            self._jobs[job.job_id] = job
            if job.idempotency_key:
                self._idempotency[job.idempotency_key] = (
                    job.job_id, time.monotonic()
                )
            loaded += 1

        # Keep only the most recent `job_history_limit` in MEMORY; the rest stay
        # on disk and are still served by get() via the store.
        self._trim_memory()
        if loaded:
            log.info("Loaded %d job(s) from history (%d reconciled).",
                     loaded, len(changed))
        return len(changed)

    def prune_logs(self) -> int:
        """Delete aged-out per-job log files. Returns how many were removed."""
        return prune_logs(
            self._settings.job_log_dir,
            max_age_days=self._settings.job_log_max_age_days,
            max_total_mb=self._settings.job_log_max_total_mb,
        )

    # -- Introspection -------------------------------------------------------

    @property
    def active_job(self) -> Optional[JobRecord]:
        """A running job in ANY lane, if any. Kept for callers that ask
        "is anything running?"; use active_in(lane) for a single lane."""
        for lane in LANES:
            record = self.active_in(lane)
            if record is not None:
                return record
        return None

    def active_in(self, lane: str) -> Optional[JobRecord]:
        """The job running in `lane`, if any. O(1)."""
        job_id = self._active.get(lane)
        if job_id is None:
            return None
        record = self._jobs.get(job_id)
        # Defensive: if the record was evicted or finished without clearing the
        # pointer, treat the slot as free rather than blocking every trigger.
        if record is None or record.status is not JobStatus.RUNNING:
            self._active.pop(lane, None)
            return None
        return record

    def active_jobs(self) -> Dict[str, JobRecord]:
        return {lane: rec for lane in LANES
                if (rec := self.active_in(lane)) is not None}

    def _release_lane(self, job_id: str) -> None:
        for lane, active in list(self._active.items()):
            if active == job_id:
                self._active.pop(lane, None)

    def get(self, job_id: str) -> Optional[JobRecord]:
        """Look up one job by id, falling back to the durable history.

        The in-memory registry is a bounded CACHE. An id that has aged out of it
        is still on disk, so a web app polling an older job gets its real
        outcome instead of a 404 that reads as "this never happened".
        """
        record = self._jobs.get(job_id)
        if record is not None and job_id in self._processes:
            return record           # ours and running: memory is authoritative
        self.sync_external()
        record = self._jobs.get(job_id)
        if record is not None:
            return record
        data = self._store.get(job_id)
        if data is None:
            return None
        return JobRecord.from_record(data)

    def recent(self, limit: int = 20) -> List[JobRecord]:
        """Most-recent-first list of tracked jobs."""
        self.sync_external()
        return list(reversed(self._jobs.values()))[:limit]

    def sync_external(self) -> int:
        """Pick up job records written by a process this API did not spawn.

        The SCHEDULER's automatic booker is one: it runs detached, writes its
        own records to the same history file (src/booking/attempt_job.py), and
        would otherwise be invisible until the next restart — which is exactly
        the run nobody is watching.

        Cheap: the file's size and mtime are checked first, so the common case
        (nothing new) is one stat. Records this process owns are never
        overwritten — its own in-memory state is newer than anything on disk.
        """
        try:
            stat = os.stat(self._store.path)
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return 0
        if stamp == getattr(self, "_external_stamp", None):
            return 0
        self._external_stamp = stamp

        added = 0
        try:
            records = self._store.load_all()
        except Exception as exc:                            # noqa: BLE001
            log.debug("Could not re-read the job history: %s", exc)
            return 0
        for job_id, data in records.items():
            if job_id in self._processes or job_id in self._tasks:
                continue                    # ours, and running: memory wins
            known = self._jobs.get(job_id)
            if known is not None and known.status.value == str(
                    data.get("status", "")):
                continue
            job = JobRecord.from_record(data)
            if job is None:
                continue
            self._jobs[job.job_id] = job
            added += 1
        if added:
            self._trim_memory()
            log.debug("Picked up %d job record(s) from outside this process.",
                      added)
        return added

    # -- Execution -----------------------------------------------------------

    async def trigger(
        self,
        extra_args: Optional[List[str]] = None,
        payload: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        command_override: Optional[List[str]] = None,
        lane: Optional[str] = None,
    ) -> Tuple[JobRecord, bool]:
        """Spawn the configured job and return immediately.

        Args:
            extra_args: Additional argv entries appended to the configured
                command. Passed as a list, so they are arguments — never shell.
            payload: Caller-supplied metadata, echoed back in status responses.
            idempotency_key: When supplied and already seen, the ORIGINAL job is
                returned instead of spawning a second one.
            command_override: When set, replaces the configured job_command
                entirely (used by booking trigger which runs a different CLI).

        Returns:
            (record, replayed). `replayed` is True when an idempotency key
            matched an existing job and nothing new was started.

        Raises:
            JobAlreadyRunningError: single-flight rejection (caller should 409).
            JobStartError: the process would not start at all (caller: 500).
        """
        settings = self._settings
        # A command override is a booking CLI; the configured command is the
        # waitlist one. Callers may say so explicitly.
        lane = lane or (LANE_BOOKING if command_override is not None
                        else LANE_WAITLIST)
        if command_override is not None:
            command = [*command_override, *(extra_args or [])]
        else:
            command = [*settings.job_command, *(extra_args or [])]

        async with self._lock:
            # -- Idempotent replay -------------------------------------------
            # Checked inside the lock so two simultaneous retries of the same
            # key cannot both miss the cache and both spawn.
            if idempotency_key:
                self._expire_idempotency()
                existing = self._idempotency.get(idempotency_key)
                if existing is not None:
                    prior = self.get(existing[0])
                    if prior is not None:
                        log.info("Idempotent replay of key %s -> job %s.",
                                 _key_fingerprint(idempotency_key), prior.job_id)
                        return prior, True

            if settings.single_flight:
                running = self.active_in(lane)
                if running is not None:
                    raise JobAlreadyRunningError(
                        f"A {lane} job is already running (job_id="
                        f"{running.job_id}, pid={running.pid}). Wait for it to "
                        "finish or cancel it."
                    )
                # The manager's own check only sees THIS process. A manual
                # `python -m src.waitlist run`, the autotrigger, or a scheduler
                # tick drives a browser too, and two at once can double-register
                # a client. Probe the machine-wide lock so we refuse cleanly
                # instead of spawning a child that will block on it for 900s.
                busy = _machine_lock_busy(lane)
                if busy:
                    raise JobAlreadyRunningError(
                        "Another browser-driving run already holds the machine "
                        f"lock{f' ({busy})' if busy != 'busy' else ''}. Runs are "
                        "serialised deliberately: two at once can double-register "
                        "a client or corrupt the waitlist journal."
                    )

            job_id = secrets.token_hex(8)
            log_path = self._prepare_log_path(job_id)

            # ONE id for the job and the run. The child is told this id via
            # VFS_RUN_ID and stamps it on every log line and journal row it
            # writes, so GET /jobs/{id}/trace can gather the whole story
            # without a mapping table.
            record = JobRecord(
                job_id=job_id,
                run_id=job_id,
                command=command,
                status=JobStatus.RUNNING,
                started_at=datetime.now(timezone.utc),
                log_file=str(log_path),
                payload={**(payload or {}), "lane": lane},
                idempotency_key=idempotency_key,
            )

            try:
                process = await self._spawn(command, log_path, run_id=job_id)
            except (OSError, ValueError) as exc:
                # The exec itself failed — missing interpreter, bad cwd, etc.
                # Nothing is tracked, so the next trigger is free to retry.
                log.error("Failed to spawn job %s: %s", job_id, exc)
                raise JobStartError(f"Could not start job process: {exc}") from exc

            record.pid = process.pid
            self._processes[job_id] = process
            self._jobs[job_id] = record
            self._active[lane] = job_id
            if idempotency_key:
                self._idempotency[idempotency_key] = (job_id, time.monotonic())
                self._expire_idempotency()
            # Persist BEFORE the supervisor can finish, so a crash between here
            # and completion still leaves a record to reconcile.
            self._store.append(record.to_record())
            self._trim_memory()

            # Detached supervisor: awaits the child, records the outcome. The
            # reference is kept so the task is not garbage-collected mid-flight.
            self._tasks[job_id] = asyncio.create_task(
                self._supervise(job_id), name=f"job-{job_id}"
            )

        log.info("Job %s started (pid=%s): %s", job_id, record.pid, " ".join(command))
        return record, False

    async def _spawn(
        self, command: List[str], log_path: Path, *, run_id: str = ""
    ) -> asyncio.subprocess.Process:
        """Start the child with stdout+stderr redirected to `log_path`."""
        settings = self._settings

        # Opened here (not in the child) so a failure to create the log file
        # surfaces as a clean start error rather than a silent dead job.
        log_handle = log_path.open("ab", buffering=0)
        try:
            # A copied environment: the child inherits what it needs, plus a
            # marker it can branch on. PYTHONUNBUFFERED keeps the log live so
            # you can tail a running job.
            child_env = os.environ.copy()
            child_env["PYTHONUNBUFFERED"] = "1"
            # UTF-8 console: a VFS page's "✓" must not crash a print().
            child_env["PYTHONIOENCODING"] = "utf-8:replace"
            child_env["VFS_TRIGGERED_BY"] = "webhook-api"
            # The correlation key. src/utils/run_context.run_id() reads this,
            # so the child adopts our id instead of minting its own — which is
            # the entire mechanism by which the chain survives the process
            # boundary. Must match run_context.ENV_VAR.
            if run_id:
                child_env[run_context.ENV_VAR] = run_id
            # The shared secret must never reach the child.
            child_env.pop("VFSAPI_SECRET_TOKEN", None)

            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(settings.job_cwd),
                env=child_env,
                stdout=log_handle,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                # New process group so a timeout kill takes the child's own
                # children (Chrome!) with it rather than orphaning them.
                **self._process_group_kwargs(),
            )
        finally:
            # The child holds its own dup of the descriptor; ours is now dead
            # weight and would keep the file open for the process lifetime.
            log_handle.close()
        return process

    @staticmethod
    def _process_group_kwargs() -> Dict[str, Any]:
        """Platform-specific flags for creating a killable process group."""
        if sys.platform == "win32":
            # CREATE_NEW_PROCESS_GROUP lets us signal the whole tree.
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}

    async def _supervise(self, job_id: str) -> None:
        """Await the child, apply the timeout, and record the final status."""
        record = self._jobs[job_id]
        process = self._processes[job_id]
        timeout = self._settings.job_timeout_seconds

        try:
            exit_code = await asyncio.wait_for(process.wait(), timeout=timeout)
            record.exit_code = exit_code

            # A cancellation asked for by cancel() got us here: terminate()
            # made the child exit, so we are looking at that signal's exit code
            # (-15, or 1 on Windows), NOT a real failure. Reporting FAILED here
            # would overwrite the cancellation and make CANCELLED unreachable
            # through the API. The flag is set before the signal, so seeing it
            # set means the exit was ours.
            if record.cancel_requested:
                record.status = JobStatus.CANCELLED
                record.detail = record.detail or "Cancelled via API."
                log.info("Job %s exited after cancellation (exit=%s).",
                         job_id, exit_code)
                self._attach_results(record)
                return

            # The waitlist CLI's exit codes carry meaning beyond pass/fail:
            #   0  ran to completion (individual results may still be skipped)
            #   1  at least one client FAILED
            #   2  SLOTS AVAILABLE — the run stopped deliberately; a bookable
            #      slot exists, so waitlisting was the wrong action. This is a
            #      BETTER outcome than success, and must not read as an error.
            #   3  PAYMENT SUBMITTED — the click happened. Paid, unanswered,
            #      or an OTP nobody typed. Money may have moved.
            #   4  PAYMENT DECLINED — the gateway refused, but VFS's own
            #      failure page says funds may have been deducted anyway.
            if exit_code == EXIT_SLOTS_AVAILABLE:
                record.status = JobStatus.SLOTS_AVAILABLE
                record.detail = (
                    "A bookable slot exists — the run stopped and nothing was "
                    "registered. Book it on the portal instead."
                )
            elif exit_code == EXIT_PAYMENT_SUBMITTED:
                record.status = JobStatus.PAYMENT_SUBMITTED
                record.outcome = "payment_submitted"
                record.detail = _payment_detail(
                    record,
                    paid="The payment went through: the gateway approved it "
                         "and VFS returned a confirmation. The appointment is "
                         "booked and paid.",
                    unresolved="The payment was SUBMITTED and the gateway's "
                               "answer was not read. The card may have been "
                               "charged. Check the portal or the bank before "
                               "doing anything else — DO NOT re-run.")
            elif exit_code == EXIT_PAYMENT_DECLINED:
                record.status = JobStatus.PAYMENT_DECLINED
                record.outcome = "payment_declined"
                record.needs_attention = True
                record.detail = (
                    "The gateway DECLINED the payment. VFS's own failure page "
                    "says funds may still have been deducted, so declined is "
                    "not the same as nothing happened. Check the card before "
                    "re-arming — DO NOT re-run.")
            elif exit_code == 0:
                record.status = JobStatus.SUCCEEDED
            else:
                record.status = JobStatus.FAILED
                record.detail = f"Process exited with code {exit_code}."

            # Parse the machine-readable block so callers get per-client
            # outcomes, not just a process exit code.
            self._attach_results(record)

            log.info(
                "Job %s finished: %s (exit=%s)", job_id, record.status.value, exit_code
            )

        except asyncio.TimeoutError:
            record.status = JobStatus.TIMED_OUT
            record.detail = f"Killed after exceeding job_timeout_seconds={timeout}."
            log.error("Job %s timed out after %ss — killing.", job_id, timeout)
            await self._terminate(process)
            record.exit_code = process.returncode

        except asyncio.CancelledError:
            record.status = JobStatus.CANCELLED
            record.detail = "Cancelled by server shutdown."
            await self._terminate(process)
            raise

        except Exception as exc:  # pragma: no cover — defensive
            record.status = JobStatus.FAILED
            record.detail = f"Supervisor error: {exc}"
            log.exception("Unexpected error supervising job %s", job_id)

        finally:
            record.finished_at = datetime.now(timezone.utc)
            self._processes.pop(job_id, None)
            self._tasks.pop(job_id, None)
            self._release_lane(job_id)
            # The completed state is the one that MUST survive a restart: it
            # carries the parsed per-client results and the needs_attention
            # flag that routes an unresolved submit to a human.
            self._store.append(record.to_record())

    @staticmethod
    def _attach_results(record: JobRecord) -> None:
        """Parse the run's JSON result block out of its log file.

        The block is marker-delimited because the run's own logging shares the
        stream, so the log as a whole is not valid JSON. Best-effort by design:
        a job whose block is missing or malformed keeps its exit-code-derived
        status and simply reports no per-client results. Never raises — a
        parsing bug must not turn a successful registration into a failed job.
        """
        path = record.log_file
        if not path or not os.path.exists(path):
            return

        try:
            # Read only the TAIL. A Playwright run's log reaches tens of MB and
            # the block we want is at the very end, so slurping the whole file
            # would spend hundreds of megabytes to parse a few kilobytes. The
            # window is far larger than any real result block, and a block that
            # somehow exceeded it degrades to "no results parsed" — the same
            # graceful failure as a missing marker.
            text = _read_tail(path, RESULT_TAIL_BYTES)

            start = text.rfind(RESULT_JSON_BEGIN)      # last block wins
            if start == -1:
                return
            end = text.find(RESULT_JSON_END, start)
            if end == -1:
                return

            blob = text[start + len(RESULT_JSON_BEGIN):end].strip()
            data = json.loads(blob)
        except (OSError, ValueError) as exc:
            log.warning("Job %s: could not parse the result block (%s).",
                        record.job_id, exc)
            return

        record.outcome = data.get("outcome")
        results = data.get("results") or []
        record.results = results if isinstance(results, list) else []

        # The run said why it stopped: that sentence is the detail, not
        # "Process exited with code 1".
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            record.error = {"code": str(error.get("code") or "run_failed"),
                            "message": str(error["message"])}
            if record.status == JobStatus.FAILED:
                record.detail = record.error["message"]

        # 'pending' and 'unknown' mean a submit is outstanding or its outcome is
        # ambiguous. Those need a HUMAN (`python -m src.waitlist resolve`) —
        # never an automatic retry, which risks a duplicate registration.
        record.needs_attention = any(
            str(r.get("status", "")).lower() in ("pending", "unknown")
            for r in record.results if isinstance(r, dict)
        )
        if record.needs_attention:
            record.detail = (
                "A submit is unresolved (status pending/unknown). Verify on the "
                "VFS account and resolve it before running this client again."
            )
            log.error("Job %s needs human attention: unresolved submit.",
                      record.job_id)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        """Ask the child to exit, then kill it if it will not."""
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5)

    async def cancel(self, job_id: str, *, reason: str = "Cancelled via API.") -> bool:
        """Stop a running job. Returns False if it was not running.

        ORDER MATTERS. `cancel_requested` is set BEFORE the child is signalled,
        because the supervisor is concurrently blocked in `process.wait()` and
        wakes the instant the signal lands. If we set the flag afterwards, the
        supervisor can observe the terminate's non-zero exit code first and
        record FAILED — which is what used to make CANCELLED unreachable here.

        Finalisation (status, finished_at, persistence) is left entirely to the
        supervisor, so there is exactly ONE writer of a job's terminal state
        rather than two racing to describe the same exit.
        """
        process = self._processes.get(job_id)
        record = self._jobs.get(job_id)
        if process is None or record is None:
            return False

        record.cancel_requested = True
        record.detail = reason
        await self._terminate(process)

        # Give the supervisor a moment to observe the exit and write the
        # terminal state, so a caller reading the record straight after this
        # returns sees CANCELLED rather than a still-RUNNING row.
        task = self._tasks.get(job_id)
        if task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=15)

        # Fallback: if the supervisor is wedged, record the cancellation here
        # rather than leaving the job reading as RUNNING forever.
        if record.status is JobStatus.RUNNING:
            record.status = JobStatus.CANCELLED
            record.finished_at = datetime.now(timezone.utc)
            self._release_lane(job_id)
            self._store.append(record.to_record())

        log.info("Job %s cancelled: %s", job_id, reason)
        return True

    async def shutdown(self) -> None:
        """Terminate every running job. Called on server shutdown."""
        for job_id in list(self._processes):
            await self.cancel(
                job_id, reason="Cancelled by server shutdown."
            )

    # -- Housekeeping --------------------------------------------------------

    def _prepare_log_path(self, job_id: str) -> Path:
        """Build (and make room for) this job's log file path."""
        log_dir = self._settings.job_log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return log_dir / f"job-{stamp}-{job_id}.log"

    def _trim_memory(self) -> None:
        """Bound the in-memory registry. Evicted jobs remain on disk.

        `_jobs` is ordered oldest-first, so the eviction candidate is normally
        the FIRST entry — no scan needed. Only when that entry is still running
        (rare: it means the oldest tracked job is the active one) do we step
        forward to find the next finished record.

        Eviction is no longer data loss: `get()` falls back to the JSONL store,
        so an evicted job still answers GET /jobs/{id} with its real outcome.
        """
        limit = self._settings.job_history_limit
        while len(self._jobs) > limit:
            victim: Optional[str] = None
            for job_id, record in self._jobs.items():
                if record.status is not JobStatus.RUNNING:
                    victim = job_id
                    break
            if victim is None:
                return          # everything tracked is still running
            del self._jobs[victim]

    def _expire_idempotency(self) -> None:
        """Drop idempotency keys past their TTL, and cap the total kept.

        Called under the manager lock. The TTL is what makes a key safe to
        reuse eventually; the size cap is what stops a caller sending a fresh
        random key per request from growing this without bound.
        """
        ttl = self._settings.idempotency_ttl_seconds
        cutoff = time.monotonic() - ttl
        for key in [k for k, (_, ts) in self._idempotency.items() if ts < cutoff]:
            self._idempotency.pop(key, None)
        while len(self._idempotency) > self._settings.idempotency_max_keys:
            self._idempotency.popitem(last=False)
