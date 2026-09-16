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

from src.api.config import ApiSettings, get_settings
from src.api.jobstore import JobStore, prune_logs, reconcile

log = logging.getLogger("vfs.api.jobs")


# The waitlist CLI returns 2 when it stopped because a real, bookable slot
# exists. That is a BETTER outcome than a waitlist registration, so it gets its
# own job status rather than being lumped in with failures.
EXIT_SLOTS_AVAILABLE = 2

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
    # The API restarted while this job was running; its outcome was never
    # recorded and cannot be reconstructed. Needs a human.
    UNKNOWN = "unknown"


# Statuses that mean the job is over. Anything else is still in flight.
TERMINAL_STATUSES = frozenset({
    JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.TIMED_OUT,
    JobStatus.CANCELLED, JobStatus.SLOTS_AVAILABLE, JobStatus.UNKNOWN,
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


@dataclass
class JobRecord:
    """Everything we know about one triggered job."""

    job_id: str
    command: List[str]
    status: JobStatus
    started_at: datetime
    pid: Optional[int] = None
    finished_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    log_file: Optional[str] = None
    detail: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    # Per-client outcomes parsed from the run's JSON block. Empty when the job
    # produced none (placeholder job, crash before the block, older CLI).
    results: List[Dict[str, Any]] = field(default_factory=list)
    # Run-level verdict from the same block: "completed" | "slots_available".
    outcome: Optional[str] = None
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
            "results": self.results,
            "needs_attention": self.needs_attention,
        }

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


def _machine_lock_busy() -> str:
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
        with runlock.acquire("api-trigger-probe", lane=runlock.LANE_WAITLIST,
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
        self._active_id: Optional[str] = None
        # Durable history. Survives the restart that the in-memory dict cannot.
        self._store = JobStore(self._settings.job_history_file)
        # Idempotency-Key -> (job_id, monotonic timestamp).
        self._idempotency: "OrderedDict[str, Tuple[str, float]]" = OrderedDict()

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
        """The currently running job, if any. O(1)."""
        if self._active_id is None:
            return None
        record = self._jobs.get(self._active_id)
        # Defensive: if the record was evicted or finished without clearing the
        # pointer, treat the slot as free rather than blocking every trigger.
        if record is None or record.status is not JobStatus.RUNNING:
            self._active_id = None
            return None
        return record

    def get(self, job_id: str) -> Optional[JobRecord]:
        """Look up one job by id, falling back to the durable history.

        The in-memory registry is a bounded CACHE. An id that has aged out of it
        is still on disk, so a web app polling an older job gets its real
        outcome instead of a 404 that reads as "this never happened".
        """
        record = self._jobs.get(job_id)
        if record is not None:
            return record
        data = self._store.get(job_id)
        if data is None:
            return None
        return JobRecord.from_record(data)

    def recent(self, limit: int = 20) -> List[JobRecord]:
        """Most-recent-first list of tracked jobs."""
        return list(reversed(self._jobs.values()))[:limit]

    # -- Execution -----------------------------------------------------------

    async def trigger(
        self,
        extra_args: Optional[List[str]] = None,
        payload: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[JobRecord, bool]:
        """Spawn the configured job and return immediately.

        Args:
            extra_args: Additional argv entries appended to the configured
                command. Passed as a list, so they are arguments — never shell.
            payload: Caller-supplied metadata, echoed back in status responses.
            idempotency_key: When supplied and already seen, the ORIGINAL job is
                returned instead of spawning a second one.

        Returns:
            (record, replayed). `replayed` is True when an idempotency key
            matched an existing job and nothing new was started.

        Raises:
            JobAlreadyRunningError: single-flight rejection (caller should 409).
            JobStartError: the process would not start at all (caller: 500).
        """
        settings = self._settings
        command: List[str] = [*settings.job_command, *(extra_args or [])]

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
                running = self.active_job
                if running is not None:
                    raise JobAlreadyRunningError(
                        f"A job is already running (job_id={running.job_id}, "
                        f"pid={running.pid}). Wait for it to finish or cancel it."
                    )
                # The manager's own check only sees THIS process. A manual
                # `python -m src.waitlist run`, the autotrigger, or a scheduler
                # tick drives a browser too, and two at once can double-register
                # a client. Probe the machine-wide lock so we refuse cleanly
                # instead of spawning a child that will block on it for 900s.
                busy = _machine_lock_busy()
                if busy:
                    raise JobAlreadyRunningError(
                        "Another browser-driving run already holds the machine "
                        f"lock{f' ({busy})' if busy != 'busy' else ''}. Runs are "
                        "serialised deliberately: two at once can double-register "
                        "a client or corrupt the waitlist journal."
                    )

            job_id = secrets.token_hex(8)
            log_path = self._prepare_log_path(job_id)

            record = JobRecord(
                job_id=job_id,
                command=command,
                status=JobStatus.RUNNING,
                started_at=datetime.now(timezone.utc),
                log_file=str(log_path),
                payload=payload or {},
                idempotency_key=idempotency_key,
            )

            try:
                process = await self._spawn(command, log_path)
            except (OSError, ValueError) as exc:
                # The exec itself failed — missing interpreter, bad cwd, etc.
                # Nothing is tracked, so the next trigger is free to retry.
                log.error("Failed to spawn job %s: %s", job_id, exc)
                raise JobStartError(f"Could not start job process: {exc}") from exc

            record.pid = process.pid
            self._processes[job_id] = process
            self._jobs[job_id] = record
            self._active_id = job_id
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
        self, command: List[str], log_path: Path
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
            child_env["VFS_TRIGGERED_BY"] = "webhook-api"
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
            if exit_code == EXIT_SLOTS_AVAILABLE:
                record.status = JobStatus.SLOTS_AVAILABLE
                record.detail = (
                    "A bookable slot exists — the run stopped and nothing was "
                    "registered. Book it on the portal instead."
                )
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
            if self._active_id == job_id:
                self._active_id = None
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
            if self._active_id == job_id:
                self._active_id = None
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
