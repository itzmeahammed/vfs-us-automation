"""Job handlers: one job, its log, its live stream, cancelling it.

Registered by router.py. The list endpoint lives in router.py itself, because it
filters by lane.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, AsyncIterator, Dict

from fastapi import HTTPException, Query, status
from fastapi.responses import StreamingResponse

from src.api.modules.jobs.manager import JobStatus, TERMINAL_STATUSES, read_log_tail
from src.api.modules.jobs.runtime import job_manager
from src.api.modules.jobs.schemas import JobLogResponse, JobResponse

import logging

log = logging.getLogger("vfs.api")

# -- GET /jobs/{id}/stream tuning ------------------------------------------- #
#: How often the log file is re-read. Short enough to feel live, long enough
#: that a dozen watchers cost nothing.
STREAM_POLL_SECONDS = 0.5
#: Bytes per read. Caps what one pass can take when a job suddenly logs a great
#: deal (a --capture full DOM dump, say).
STREAM_CHUNK_BYTES = 64 * 1024
#: Idle passes between heartbeat comments — 20 x 0.5s = every 10 seconds.
STREAM_HEARTBEAT_TICKS = 20
#: Hard ceiling on one connection. A booking walk is minutes, not hours, and an
#: unbounded stream is a resource leak wearing a feature's clothes.
STREAM_MAX_SECONDS = 3600


def _sse(event: str, data: Dict[str, Any]) -> str:
    """One Server-Sent Event frame.

    json.dumps matters here beyond tidiness: an SSE `data:` field is
    newline-delimited, so a log line containing a newline would otherwise be
    read as two frames. JSON escapes it and the frame stays one frame.
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def get_job(job_id: str) -> JobResponse:
    """Status of one job."""
    record = job_manager.get(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No job with id {job_id!r}.",
        )
    return JobResponse(**record.to_dict())


def get_job_logs(
    job_id: str,
    lines: int = Query(default=200, ge=1, le=2000,
                       description="How many trailing lines to return."),
) -> JobLogResponse:
    """Tail of a job's log.

    The `log_file` field elsewhere is an absolute path on the machine running
    this API — useless to a remote web app, and a small disclosure besides.
    This serves the content instead, so an operator can diagnose a failed run
    without shell access.

    Declared `def`, not `async def`: it reads a file, and FastAPI runs a sync
    endpoint in a threadpool rather than blocking the event loop.
    """
    record = job_manager.get(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No job with id {job_id!r}.",
        )

    path = record.log_file
    if not path or not os.path.exists(path):
        # A pruned or never-created log is not a 404 on the JOB — the job is
        # real and its status still means something.
        return JobLogResponse(
            job_id=job_id, lines=[], line_count=0,
            truncated=False, log_available=False,
        )

    try:
        tail, truncated = read_log_tail(path, lines)
    except OSError as exc:
        log.warning("Could not read log for job %s: %s", job_id, exc)
        return JobLogResponse(
            job_id=job_id, lines=[], line_count=0,
            truncated=False, log_available=False,
        )

    return JobLogResponse(
        job_id=job_id,
        lines=tail,
        line_count=len(tail),
        truncated=truncated,
        log_available=True,
    )


async def stream_job_log(
    job_id: str,
    from_start: bool = Query(
        default=False,
        description="Replay the log from byte 0 before following. Default "
                    "follows from the end, like `tail -f`.",
    ),
) -> StreamingResponse:
    """Follow a running job's log as Server-Sent Events.

    GET /jobs/{id}/logs answers "what happened"; this answers "what is
    happening". A booking walk takes minutes, and an operator watching one
    should not have to poll a tail endpoint on a timer, re-reading the same
    kilobytes to notice one new line.

    Ends by itself when the job reaches a terminal status, so a client can loop
    over the events and simply fall out rather than deciding when to stop.
    Event names are `log`, `status` and `end`.

    Two implementation notes, both load-bearing:

    * The file is polled, not watched. A cross-platform file watch would mean
      another dependency for a file this process's own child is appending to,
      and the child writes unbuffered (PYTHONUNBUFFERED), so a short poll is
      already near-live.
    * A heartbeat comment is emitted while idle. Tunnels and reverse proxies
      close connections that go quiet, and a comment keeps the stream alive
      without the client having to interpret an empty event.
    """
    record = job_manager.get(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No job with id {job_id!r}.",
        )

    path = record.log_file

    async def events() -> AsyncIterator[str]:
        # Announce the run id first: it is the join key the caller needs in
        # order to find this run's journal rows and screenshots folder.
        yield _sse("status", {
            "job_id": job_id,
            "run_id": record.run_id or job_id,
            "status": record.status.value,
        })

        if not path or not os.path.exists(path):
            yield _sse("end", {"job_id": job_id, "reason": "no log file"})
            return

        offset = 0
        if not from_start:
            try:
                offset = os.path.getsize(path)
            except OSError:
                offset = 0

        idle_ticks = 0
        last_status = record.status.value
        # A hard ceiling on the connection. Without it, a client that opens a
        # stream and never reads holds a task and a file handle indefinitely.
        deadline = time.monotonic() + STREAM_MAX_SECONDS

        while time.monotonic() < deadline:
            chunk = ""
            try:
                size = os.path.getsize(path)
                if size < offset:
                    # Truncated or rotated under us. Start over rather than
                    # reading from a stale offset into the middle of a line.
                    offset = 0
                if size > offset:
                    with open(path, "rb") as handle:
                        handle.seek(offset)
                        raw = handle.read(STREAM_CHUNK_BYTES)
                    offset += len(raw)
                    chunk = raw.decode("utf-8", errors="replace")
            except OSError as exc:
                yield _sse("end", {"job_id": job_id,
                                   "reason": f"log unreadable: {exc}"})
                return

            if chunk:
                idle_ticks = 0
                for line in chunk.splitlines():
                    if line.strip():
                        yield _sse("log", {"line": line})
            else:
                idle_ticks += 1

            # Re-read the record each pass: the supervisor mutates it in place
            # when the child exits, which is how this loop learns to stop.
            current = job_manager.get(job_id)
            current_status = current.status.value if current else last_status
            if current_status != last_status:
                last_status = current_status
                yield _sse("status", {"job_id": job_id,
                                      "status": current_status})

            if current is not None and current.status in TERMINAL_STATUSES:
                # One final drain: the child's last writes may have landed
                # between the read above and its exit.
                try:
                    if os.path.getsize(path) > offset:
                        with open(path, "rb") as handle:
                            handle.seek(offset)
                            tail = handle.read(STREAM_CHUNK_BYTES)
                        for line in tail.decode("utf-8", errors="replace").splitlines():
                            if line.strip():
                                yield _sse("log", {"line": line})
                except OSError:
                    pass
                yield _sse("end", {
                    "job_id": job_id,
                    "run_id": current.run_id or job_id,
                    "status": current.status.value,
                    "exit_code": current.exit_code,
                    "outcome": current.outcome,
                    "needs_attention": current.needs_attention,
                })
                return

            if idle_ticks and idle_ticks % STREAM_HEARTBEAT_TICKS == 0:
                yield ": heartbeat\n\n"

            await asyncio.sleep(STREAM_POLL_SECONDS)

        yield _sse("end", {"job_id": job_id,
                           "reason": "stream time limit reached"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Tells nginx not to buffer the stream into uselessness.
            "X-Accel-Buffering": "no",
        },
    )


async def cancel_job(job_id: str) -> JobResponse:
    """Terminate a running job (and its child processes)."""
    record = job_manager.get(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No job with id {job_id!r}.",
        )
    if record.status is not JobStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job {job_id} is not running (status={record.status.value}).",
        )
    await job_manager.cancel(job_id)
    return JobResponse(**record.to_dict())

