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
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import subprocess
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.api.config import ApiSettings, get_settings

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


class JobStatus(str, Enum):
    """Lifecycle of a triggered job."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    # A bookable slot appeared, so the run stopped without registering.
    SLOTS_AVAILABLE = "slots_available"


class JobStartError(RuntimeError):
    """Raised when the child process could not be spawned at all.

    Distinct from "the job ran and failed": this means the interpreter or
    script was missing, the cwd did not exist, or the OS refused the exec.
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

    # -- Introspection -------------------------------------------------------

    @property
    def active_job(self) -> Optional[JobRecord]:
        """The currently running job, if any."""
        for record in reversed(self._jobs.values()):
            if record.status is JobStatus.RUNNING:
                return record
        return None

    def get(self, job_id: str) -> Optional[JobRecord]:
        """Look up one job by id."""
        return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> List[JobRecord]:
        """Most-recent-first list of tracked jobs."""
        return list(reversed(self._jobs.values()))[:limit]

    # -- Execution -----------------------------------------------------------

    async def trigger(
        self,
        extra_args: Optional[List[str]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> JobRecord:
        """Spawn the configured job and return immediately.

        Args:
            extra_args: Additional argv entries appended to the configured
                command. Passed as a list, so they are arguments — never shell.
            payload: Caller-supplied metadata, echoed back in status responses.

        Raises:
            JobStartError: single-flight rejection, or the process would not start.
        """
        settings = self._settings
        command: List[str] = [*settings.job_command, *(extra_args or [])]

        async with self._lock:
            if settings.single_flight:
                running = self.active_job
                if running is not None:
                    raise JobStartError(
                        f"A job is already running (job_id={running.job_id}, "
                        f"pid={running.pid}). Wait for it to finish or cancel it."
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
            self._evict_old_records()

            # Detached supervisor: awaits the child, records the outcome. The
            # reference is kept so the task is not garbage-collected mid-flight.
            self._tasks[job_id] = asyncio.create_task(
                self._supervise(job_id), name=f"job-{job_id}"
            )

        log.info("Job %s started (pid=%s): %s", job_id, record.pid, " ".join(command))
        return record

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
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()

            start = text.rfind(RESULT_JSON_BEGIN)      # last block wins
            if start == -1:
                return
            end = text.find(RESULT_JSON_END, start)
            if end == -1:
                return

            import json
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

    async def cancel(self, job_id: str) -> bool:
        """Stop a running job. Returns False if it was not running."""
        process = self._processes.get(job_id)
        if process is None:
            return False
        record = self._jobs[job_id]
        await self._terminate(process)
        record.status = JobStatus.CANCELLED
        record.detail = "Cancelled via API."
        record.finished_at = datetime.now(timezone.utc)
        log.info("Job %s cancelled via API.", job_id)
        return True

    async def shutdown(self) -> None:
        """Terminate every running job. Called on server shutdown."""
        for job_id in list(self._processes):
            await self.cancel(job_id)

    # -- Housekeeping --------------------------------------------------------

    def _prepare_log_path(self, job_id: str) -> Path:
        """Build (and make room for) this job's log file path."""
        log_dir = self._settings.job_log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return log_dir / f"job-{stamp}-{job_id}.log"

    def _evict_old_records(self) -> None:
        """Trim finished jobs beyond the history limit (never a running one)."""
        limit = self._settings.job_history_limit
        while len(self._jobs) > limit:
            for job_id, record in self._jobs.items():
                if record.status is not JobStatus.RUNNING:
                    del self._jobs[job_id]
                    break
            else:
                return  # everything tracked is still running — nothing to evict
