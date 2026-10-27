"""Make an automatic booking visible to the web app as a JOB.

A manual booking goes through the API, so the web app can follow it:
GET /v1/jobs/{id} for its status, /logs for what it printed, /stream to watch.
An AUTOMATIC booking is started by the scheduler — a detached child the API
never sees — so until now the web app had nothing between "attempt_started"
and the outcome, for exactly the runs nobody is watching.

This writes the same job records the API writes, to the same file, so the same
endpoints serve them. Two rows per attempt (running, then finished), matching
JobRecord.to_record(); the API picks them up by re-reading the file.

Never raises. A job record is an INDEX over a booking; failing to write one
must not stop the booking it describes.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Same defaults as the API's settings (src/api/core/config.py). Read from the
#: environment first so an operator who moved them keeps one file, not two.
JOBS_FILE = os.environ.get("VFSAPI_JOB_HISTORY_FILE") or os.path.join(
    REPO_ROOT, "logs", "api_jobs", "jobs.jsonl")
LOG_DIR = os.environ.get("VFSAPI_JOB_LOG_DIR") or os.path.join(
    REPO_ROOT, "logs", "api_jobs")

#: The lane name the API uses for bookings, so GET /v1/jobs?lane=booking finds
#: these too.
LANE = "booking"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(record: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(JOBS_FILE), exist_ok=True)
        line = json.dumps(record, separators=(",", ":"), default=str)
        with open(JOBS_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception as exc:                                # noqa: BLE001
        log.debug("Could not write the job record: %s", exc)


def is_running(pid: Optional[int]) -> bool:
    """Is that process still alive? Unknown counts as alive.

    Used to tell a booking that is still working from one whose process died.
    Being wrong towards "alive" is the safe direction: a run that IS working
    must never be declared dead and retried while it holds a payment page.
    """
    if not pid:
        return True
    try:
        if os.name == "nt":
            import subprocess
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True, text=True, timeout=10).stdout
            return str(int(pid)) in out
        os.kill(int(pid), 0)                 # signal 0: existence check only
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True                          # exists, owned by someone else
    except Exception:                        # noqa: BLE001
        return True


def find(job_id: str) -> Dict[str, Any]:
    """The newest record for `job_id`, or {}. Reads the whole history file."""
    best: Dict[str, Any] = {}
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("job_id") == job_id:
                    best = row
    except OSError:
        return {}
    return best


def log_path(job_id: str) -> str:
    """Where this attempt's own log goes, in the directory the API serves."""
    return os.path.join(LOG_DIR, f"job-{job_id}.log")


def started(job_id: str, *, request_id: str, route: str, combo: str = "",
            test: bool = False, log_file: str = "") -> None:
    """Record that this attempt is running. Call it before the browser starts."""
    _append({
        "job_id": job_id, "run_id": job_id, "status": "running",
        "command": ["python", "-m", "src.booking", "autobook",
                    "--route", route, "--request", request_id],
        "pid": os.getpid(), "started_at": _now(), "finished_at": None,
        "exit_code": None, "log_file": log_file or log_path(job_id),
        "detail": "automatic booking", "results": [], "outcome": None,
        "error": None, "needs_attention": False, "idempotency_key": None,
        "payload": {
            "lane": LANE,
            # The key the clients timeline joins on — the same one the API's
            # own booking trigger sets (src/api/modules/booking/router.py).
            "request_id": request_id,
            "route": route, "combo": combo,
            "mode": "walk" if test else "commit",
            "started_by": "auto-book (scheduler)",
            "source": "autobook",
        },
    })


#: What a status WOULD have exited with, had this lane been a subprocess.
#:
#: The automatic lane writes its records in-process, so there is no real exit
#: code — but the field is in the job schema and the console prints it, so it
#: has to agree with the status rather than contradict it. Kept in step with
#: src/booking/__main__.py, which defines the codes.
_STATUS_EXIT_CODES = {
    "succeeded": 0,
    "payment_submitted": 3,
    "payment_declined": 4,
}


def _exit_code_for(status: str) -> int:
    """0 for success, the payment codes for the payment statuses, else 1.

    It was `0 if status == "succeeded" else 1`, which printed "exit code 1"
    beside a status saying a card had been charged — the same contradiction
    the payment statuses exist to remove.
    """
    return _STATUS_EXIT_CODES.get(str(status), 1)


def finished(job_id: str, *, request_id: str, route: str, combo: str = "",
             status: str, detail: str = "", outcome: str = "",
             needs_attention: bool = False, started_at: str = "",
             log_file: str = "", results: Optional[List[Dict[str, Any]]] = None,
             test: bool = False) -> None:
    """Record how this attempt ended. `status` is a JobStatus value."""
    _append({
        "job_id": job_id, "run_id": job_id, "status": status,
        "command": ["python", "-m", "src.booking", "autobook",
                    "--route", route, "--request", request_id],
        "pid": os.getpid(), "started_at": started_at or _now(),
        "finished_at": _now(), "exit_code": _exit_code_for(status),
        "log_file": log_file or log_path(job_id), "detail": detail,
        "results": results or [], "outcome": outcome or None, "error": None,
        "needs_attention": bool(needs_attention), "idempotency_key": None,
        "payload": {
            "lane": LANE, "request_id": request_id, "route": route,
            "combo": combo, "mode": "walk" if test else "commit",
            "started_by": "auto-book (scheduler)", "source": "autobook",
        },
    })
