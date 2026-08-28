"""Job durability, cancellation, idempotency, and log handling.

These cover the failure modes that only appear at the seams — a restart mid-run,
a cancel racing the supervisor, a retried request — none of which the existing
result-parsing tests touch.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

import pytest

from src.api.config import ApiSettings
from src.api.jobs import (
    JobAlreadyRunningError,
    JobManager,
    JobRecord,
    JobStatus,
    read_log_tail,
)
from src.api.jobstore import JobStore, prune_logs, reconcile


def _settings(tmp_path, **overrides):
    """An ApiSettings pointed entirely at a temp directory."""
    base = dict(
        secret_token="x" * 64,
        job_command=[sys.executable, "-c", "pass"],
        job_cwd=tmp_path,
        job_log_dir=tmp_path / "logs",
        job_history_file=tmp_path / "logs" / "jobs.jsonl",
        job_timeout_seconds=30,
        single_flight=True,
    )
    base.update(overrides)
    return ApiSettings(**base)


@pytest.fixture(autouse=True)
def _no_machine_lock(monkeypatch):
    """Neutralise the machine-wide run lock.

    These tests exercise the manager, not runlock (tests/test_runlock.py owns
    that). Leaving it live would make them contend with any real run on the
    developer's machine.
    """
    monkeypatch.setattr("src.api.jobs._machine_lock_busy", lambda: "")


# --------------------------------------------------------------------------
# Cancellation — the supervisor must not overwrite the cancelled state
# --------------------------------------------------------------------------


def test_cancel_reports_cancelled_not_failed(tmp_path):
    """Regression: cancel() raced _supervise and lost.

    cancel() terminated the child and set CANCELLED, but the supervisor was
    still blocked in process.wait(). It woke on the terminate, saw a non-zero
    exit code, and overwrote the record with FAILED — so CANCELLED was
    unreachable through the API and a deliberate stop looked like a crash.
    """
    settings = _settings(
        tmp_path,
        # A child that would run far longer than the test, so the ONLY way it
        # exits is our cancellation.
        job_command=[sys.executable, "-c", "import time; time.sleep(120)"],
    )

    async def scenario():
        manager = JobManager(settings)
        record, replayed = await manager.trigger()
        assert replayed is False
        assert record.status is JobStatus.RUNNING

        await manager.cancel(record.job_id)
        return manager, record

    manager, record = asyncio.run(scenario())

    assert record.status is JobStatus.CANCELLED, (
        f"expected CANCELLED, got {record.status.value} "
        f"(detail={record.detail!r}) — the supervisor overwrote the cancellation"
    )
    assert record.finished_at is not None
    assert manager.active_job is None


def test_cancelled_job_persists_as_cancelled(tmp_path):
    """The cancelled state must survive to disk, not just live in memory."""
    settings = _settings(
        tmp_path,
        job_command=[sys.executable, "-c", "import time; time.sleep(120)"],
    )

    async def scenario():
        manager = JobManager(settings)
        record, _ = await manager.trigger()
        await manager.cancel(record.job_id)
        return record.job_id

    job_id = asyncio.run(scenario())

    stored = JobStore(settings.job_history_file).load_all()
    assert stored[job_id]["status"] == "cancelled"


# --------------------------------------------------------------------------
# Durability — a restart must not erase what happened
# --------------------------------------------------------------------------


def test_completed_job_survives_a_restart(tmp_path):
    """A new JobManager over the same history sees the finished job."""
    settings = _settings(tmp_path)

    async def scenario():
        manager = JobManager(settings)
        record, _ = await manager.trigger()
        task = manager._tasks[record.job_id]        # noqa: SLF001 — test seam
        await asyncio.wait_for(task, timeout=30)
        return record.job_id

    job_id = asyncio.run(scenario())

    reborn = JobManager(settings)
    reborn.load_history()
    found = reborn.get(job_id)
    assert found is not None, "the job vanished across the restart"
    assert found.status is JobStatus.SUCCEEDED


def test_interrupted_job_is_reconciled_to_unknown(tmp_path):
    """A job left 'running' by a dead process becomes unknown + needs_attention.

    This is the important one. The child may have completed a REAL registration
    that was never recorded, so reporting success or failure would both be
    guesses — and either guess can cause a duplicate appointment.
    """
    settings = _settings(tmp_path)
    store = JobStore(settings.job_history_file)
    store.append({
        "job_id": "deadbeef",
        "status": "running",
        "command": ["python", "-m", "src.waitlist"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pid": 4242,
        "log_file": str(tmp_path / "logs" / "job-deadbeef.log"),
        "payload": {},
        "results": [],
        "needs_attention": False,
    })

    manager = JobManager(settings)
    orphaned = manager.load_history()

    assert orphaned == 1
    record = manager.get("deadbeef")
    assert record is not None
    assert record.status is JobStatus.UNKNOWN
    assert record.needs_attention is True
    assert "restarted" in (record.detail or "").lower()

    # And the reconciliation itself is persisted, so a second restart does not
    # re-report the same orphan as a fresh discovery.
    assert JobManager(settings).load_history() == 0


def test_evicted_job_is_still_retrievable(tmp_path):
    """Falling out of the memory cache is not the same as ceasing to exist."""
    settings = _settings(tmp_path, job_history_limit=2)

    async def scenario():
        manager = JobManager(settings)
        ids = []
        for _ in range(4):
            record, _ = await manager.trigger()
            await asyncio.wait_for(
                manager._tasks[record.job_id], timeout=30)      # noqa: SLF001
            ids.append(record.job_id)
        return manager, ids

    manager, ids = asyncio.run(scenario())

    assert len(manager._jobs) <= 2                              # noqa: SLF001
    oldest = ids[0]
    assert oldest not in manager._jobs                          # noqa: SLF001
    recovered = manager.get(oldest)
    assert recovered is not None, "an evicted job 404'd instead of reading disk"
    assert recovered.status is JobStatus.SUCCEEDED


def test_corrupt_history_line_does_not_break_loading(tmp_path):
    """A truncated final line is the classic crash artefact — skip it, load the rest."""
    settings = _settings(tmp_path)
    path = settings.job_history_file
    path.parent.mkdir(parents=True, exist_ok=True)
    good = {
        "job_id": "goodjob", "status": "succeeded",
        "command": [], "started_at": datetime.now(timezone.utc).isoformat(),
        "payload": {}, "results": [], "needs_attention": False,
    }
    path.write_text(
        json.dumps(good) + "\n" + '{"job_id": "trunc", "sta',
        encoding="utf-8",
    )

    manager = JobManager(settings)
    manager.load_history()
    assert manager.get("goodjob") is not None
    assert manager.get("trunc") is None


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_same_idempotency_key_replays_instead_of_spawning(tmp_path):
    """A retried trigger must not start a second live registration run."""
    settings = _settings(tmp_path)

    async def scenario():
        manager = JobManager(settings)
        first, replayed_a = await manager.trigger(idempotency_key="abc-123")
        await asyncio.wait_for(
            manager._tasks[first.job_id], timeout=30)          # noqa: SLF001
        second, replayed_b = await manager.trigger(idempotency_key="abc-123")
        return first, replayed_a, second, replayed_b

    first, replayed_a, second, replayed_b = asyncio.run(scenario())

    assert replayed_a is False
    assert replayed_b is True
    assert second.job_id == first.job_id, "the retry started a second job"


def test_different_idempotency_keys_start_separate_jobs(tmp_path):
    settings = _settings(tmp_path)

    async def scenario():
        manager = JobManager(settings)
        first, _ = await manager.trigger(idempotency_key="key-a")
        await asyncio.wait_for(
            manager._tasks[first.job_id], timeout=30)          # noqa: SLF001
        second, replayed = await manager.trigger(idempotency_key="key-b")
        return first, second, replayed

    first, second, replayed = asyncio.run(scenario())
    assert replayed is False
    assert second.job_id != first.job_id


# --------------------------------------------------------------------------
# Single flight
# --------------------------------------------------------------------------


def test_second_trigger_raises_the_distinct_busy_error(tmp_path):
    """Single-flight must raise a TYPE the endpoint can map to 409.

    Not a substring of the message: rewording the message must never silently
    turn a 409 into a 500.
    """
    settings = _settings(
        tmp_path,
        job_command=[sys.executable, "-c", "import time; time.sleep(120)"],
    )

    async def scenario():
        manager = JobManager(settings)
        await manager.trigger()
        try:
            await manager.trigger()
            return None
        except JobAlreadyRunningError as exc:
            return exc
        finally:
            await manager.shutdown()

    error = asyncio.run(scenario())
    assert isinstance(error, JobAlreadyRunningError)


def test_machine_lock_busy_blocks_a_trigger(tmp_path, monkeypatch):
    """A manual run holding the machine lock must produce a clean refusal.

    Without this the API would spawn a child that blocks on the lock for 900s
    and then dies — a confusing timeout instead of an immediate 409.
    """
    settings = _settings(tmp_path)
    monkeypatch.setattr("src.api.jobs._machine_lock_busy", lambda: "pid 999")

    async def scenario():
        manager = JobManager(settings)
        try:
            await manager.trigger()
            return None
        except JobAlreadyRunningError as exc:
            return exc

    error = asyncio.run(scenario())
    assert isinstance(error, JobAlreadyRunningError)
    assert "machine lock" in str(error)


# --------------------------------------------------------------------------
# Log handling
# --------------------------------------------------------------------------


def test_read_log_tail_returns_the_last_lines(tmp_path):
    path = tmp_path / "job.log"
    path.write_text("\n".join(f"line {i}" for i in range(500)), encoding="utf-8")

    lines, truncated = read_log_tail(str(path), 10)
    assert lines[-1] == "line 499"
    assert len(lines) == 10
    assert truncated is True                # 500 lines, 10 returned


def test_read_log_tail_handles_a_short_file(tmp_path):
    path = tmp_path / "job.log"
    path.write_text("only line\n", encoding="utf-8")
    lines, truncated = read_log_tail(str(path), 100)
    assert lines == ["only line"]
    assert truncated is False


def test_result_block_is_found_without_reading_the_whole_log(tmp_path):
    """The parser tails the file, so a huge log must still yield its results."""
    from src.api.jobs import RESULT_JSON_BEGIN, RESULT_JSON_END

    log_path = tmp_path / "big.log"
    payload = {"outcome": "completed",
               "results": [{"registrant_id": "c1", "status": "success"}]}
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("noise\n" * 200_000)        # a few MB of chatter
        fh.write(f"{RESULT_JSON_BEGIN}\n{json.dumps(payload)}\n{RESULT_JSON_END}\n")

    record = JobRecord(
        job_id="j1", command=[], status=JobStatus.SUCCEEDED,
        started_at=datetime.now(timezone.utc), log_file=str(log_path),
    )
    JobManager._attach_results(record)      # noqa: SLF001 — static method

    assert record.outcome == "completed"
    assert record.results[0]["registrant_id"] == "c1"


def test_prune_logs_removes_aged_files(tmp_path):
    import os
    import time as _time

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old = log_dir / "job-20200101-000000-aaaa.log"
    new = log_dir / "job-20990101-000000-bbbb.log"
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    ancient = _time.time() - (60 * 60 * 24 * 400)
    os.utime(old, (ancient, ancient))

    removed = prune_logs(log_dir, max_age_days=30, max_total_mb=0)

    assert removed == 1
    assert not old.exists()
    assert new.exists()


def test_reconcile_leaves_finished_jobs_alone(tmp_path):
    records = {
        "done": {"job_id": "done", "status": "succeeded"},
        "stuck": {"job_id": "stuck", "status": "running"},
    }
    changed = reconcile(records)
    assert [r["job_id"] for r in changed] == ["stuck"]
    assert records["done"]["status"] == "succeeded"
    assert records["stuck"]["status"] == "unknown"
