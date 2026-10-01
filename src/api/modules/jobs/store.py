"""Durable job history for the webhook API.

WHY THIS EXISTS
---------------
The JobManager's registry used to live only in memory. That is fine right up
until uvicorn restarts — a crash, a reboot, a Ctrl-C at the wrong moment —
while a waitlist run is mid-submit. Then:

    * the API forgets the job entirely, so GET /jobs/{id} returns 404 for a run
      that genuinely happened, and
    * the child process may well have OUTLIVED the restart, so a registration
      could land with nothing on our side recording it.

In this domain that is the worst possible failure. The whole point of the
journal's pending/unknown states is that "did this submit land?" must never
become unanswerable. Losing the job trail reintroduces exactly that ambiguity
one layer up.

So: every state transition is appended to a JSONL file. Append-only, one JSON
object per line, fsync'd on write.

WHY JSONL AND NOT SQLITE
------------------------
Append-only writes are atomic enough at these sizes that a crash mid-write
costs at most the final partial line, which the reader skips. There are no
concurrent writers (the API is pinned to workers=1 for exactly this reason),
no queries beyond "last N" and "by id", and the file stays greppable next to
the per-job logs it references. SQLite would buy transactions we do not need
and a binary file we could not tail.

REPLAY SEMANTICS
----------------
Records are keyed by job_id and LAST WRITE WINS. A job appends on start and
again on completion; replaying the file in order therefore rebuilds the final
state of every job. Reading is a full-file scan, done once at startup.

RECONCILIATION
--------------
A record still marked `running` at startup describes a job that was in flight
when the server died. We cannot know its outcome — the child was a separate
process and may have finished, crashed, or still be running detached. It is
marked `unknown` and flagged needs_attention, which is the honest answer and
routes it to the same human-verification path as a dangling journal entry.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

log = logging.getLogger("vfs.api.jobstore")

# Refuse to load a single line larger than this. A corrupted file should not be
# able to exhaust memory; no legitimate record comes close.
MAX_LINE_BYTES = 1_000_000

# Compact the file when it exceeds this many lines. Each job writes ~2 lines,
# so this is thousands of jobs — compaction is rare.
COMPACT_THRESHOLD_LINES = 5_000


class JobStore:
    """Append-only JSONL persistence for job records.

    Thread-safe via a plain lock: writes are short and infrequent (two per job),
    so contention is irrelevant and a lock is simpler to reason about than
    async coordination. The lock also guards compaction, which rewrites the
    file underneath any concurrent reader.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._lines_written = 0

    @property
    def path(self) -> Path:
        return self._path

    # -- Writing -------------------------------------------------------------

    def append(self, record: Dict[str, Any]) -> None:
        """Persist one snapshot of a job record. Never raises.

        A failure to write history must not fail the job it describes — the
        job's own log file remains the source of truth for what happened, and
        this is the index over it. So every error here is logged and swallowed.
        """
        try:
            line = json.dumps(record, separators=(",", ":"), default=str)
        except (TypeError, ValueError) as exc:
            log.warning("Job %s: record is not JSON-serialisable (%s).",
                        record.get("job_id"), exc)
            return

        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    # fsync so a power loss cannot lose a completed job's
                    # outcome. Two syncs per job is nothing next to the
                    # minutes-long browser run they describe.
                    os.fsync(fh.fileno())
                self._lines_written += 1
                should_compact = self._lines_written >= COMPACT_THRESHOLD_LINES
            if should_compact:
                self.compact()
        except OSError as exc:
            log.warning("Could not persist job %s: %s", record.get("job_id"), exc)

    # -- Reading -------------------------------------------------------------

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        """Replay the file into {job_id: latest_record}, insertion-ordered.

        Order is FIRST APPEARANCE of each job id, which is start order — so the
        result reads oldest-job-first, matching the in-memory registry it seeds.
        A later record for an existing id updates in place without reordering.
        """
        records: Dict[str, Dict[str, Any]] = {}
        for record in self._iter_records():
            job_id = record.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                continue
            records[job_id] = record
        return records

    def _iter_records(self) -> Iterator[Dict[str, Any]]:
        """Yield each valid record, skipping corrupt lines.

        A truncated final line (the classic crash-mid-append artefact) simply
        fails to parse and is skipped. One bad line must never make the whole
        history unreadable.
        """
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8", errors="replace") as fh:
                for number, raw in enumerate(fh, start=1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    if len(raw) > MAX_LINE_BYTES:
                        log.warning("Job history line %d is oversized — skipped.",
                                    number)
                        continue
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        log.warning("Job history line %d is not valid JSON — "
                                    "skipped.", number)
                        continue
                    if isinstance(record, dict):
                        yield record
        except OSError as exc:
            log.warning("Could not read job history at %s: %s", self._path, exc)

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Look up one job by id, scanning the file.

        Used only for ids that have fallen out of the in-memory cache, so the
        full scan is acceptable: it is the cold path by construction.
        """
        found: Optional[Dict[str, Any]] = None
        for record in self._iter_records():
            if record.get("job_id") == job_id:
                found = record            # last write wins
        return found

    # -- Housekeeping --------------------------------------------------------

    def compact(self) -> None:
        """Rewrite the file with one line per job. Never raises.

        Written to a temp file and atomically replaced, so a crash mid-compact
        leaves the original intact rather than a half-written history.
        """
        try:
            with self._lock:
                records = self.load_all()
                if not records:
                    self._lines_written = 0
                    return
                fd, tmp_name = tempfile.mkstemp(
                    dir=str(self._path.parent), prefix=".jobs-", suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        for record in records.values():
                            fh.write(json.dumps(record, separators=(",", ":"),
                                                default=str) + "\n")
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp_name, self._path)
                except BaseException:
                    # Leave the original file untouched and clean up the temp.
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
                self._lines_written = len(records)
                log.info("Compacted job history to %d record(s).", len(records))
        except OSError as exc:
            log.warning("Could not compact job history: %s", exc)


def reconcile(records: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Mark jobs left `running` by a previous process as unresolved.

    Returns the list of records that were changed, so the caller can re-persist
    them and log what it found.

    We do NOT try to adopt the orphan by pid. A pid is reusable, the child was
    spawned into its own process group, and guessing wrong either way is worse
    than admitting we do not know: claiming it succeeded could mask a duplicate
    registration, and claiming it failed could trigger a retry that creates one.
    """
    changed: List[Dict[str, Any]] = []
    for record in records.values():
        if record.get("status") != "running":
            continue
        record["status"] = "unknown"
        record["needs_attention"] = True
        record["detail"] = (
            "The API restarted while this job was running, so its outcome was "
            "never recorded. The child process may have completed a real "
            "registration. Check the VFS account and the job's log file before "
            "running this client again — see `python -m src.waitlist journal`."
        )
        if not record.get("finished_at"):
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
        changed.append(record)
    return changed


def prune_logs(log_dir: Path, *, max_age_days: int, max_total_mb: int) -> int:
    """Delete old per-job log files. Returns how many were removed.

    Two independent ceilings, because either alone leaves a hole: age misses a
    burst of huge logs inside the window, and size alone would keep a stale file
    forever on a quiet machine. Never raises — housekeeping must not stop the
    server booting.
    """
    if not log_dir.exists():
        return 0

    removed = 0
    try:
        files = [p for p in log_dir.glob("job-*.log") if p.is_file()]
    except OSError as exc:
        log.warning("Could not list %s for pruning: %s", log_dir, exc)
        return 0

    # -- Age ceiling --------------------------------------------------------
    if max_age_days > 0:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max_age_days)).timestamp()
        survivors = []
        for path in files:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
                else:
                    survivors.append(path)
            except OSError:
                survivors.append(path)
        files = survivors

    # -- Size ceiling: drop oldest first until under budget -----------------
    if max_total_mb > 0:
        budget = max_total_mb * 1024 * 1024
        try:
            sized = sorted(
                ((p, p.stat().st_size, p.stat().st_mtime) for p in files),
                key=lambda item: item[2],
                reverse=True,                    # newest first — keep these
            )
        except OSError:
            return removed
        running_total = 0
        for path, size, _mtime in sized:
            running_total += size
            if running_total > budget:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass

    if removed:
        log.info("Pruned %d old job log file(s) from %s.", removed, log_dir)
    return removed
