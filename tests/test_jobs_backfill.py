"""The backfill may only correct what the payment journal actually proves.

It edits records about money, so the bar is: change nothing unless the journal
says a payment happened, change nothing twice, and never write the journal.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("VFSAPI_SECRET_TOKEN", "d" * 64)

from src.utils import jobs_backfill                          # noqa: E402


def _job(job_id: str, status: str = "failed", **extra) -> dict:
    record = {
        "job_id": job_id, "run_id": job_id, "status": status,
        "command": ["python", "-m", "src.booking"], "pid": 1,
        "started_at": "2026-10-27T07:57:40+00:00",
        "finished_at": "2026-10-27T08:01:17+00:00",
        "exit_code": 1, "log_file": "x.log",
        "detail": "Process exited with code 1.",
        "payload": {"route": "AE-NOR", "mode": "commit",
                    "request_id": "someone-ae-nor-1"},
        "outcome": None, "error": None, "results": [],
        "needs_attention": False, "idempotency_key": None,
    }
    record.update(extra)
    return record


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A jobs file and a payment journal, both isolated to this test."""
    from src.payment import journal as journal_mod

    jobs = tmp_path / "jobs.jsonl"
    payments = tmp_path / "payments.jsonl"
    monkeypatch.setattr(journal_mod, "JOURNAL_FILE", str(payments))

    def setup(jobs_rows, payment_rows):
        with open(jobs, "w", encoding="utf-8") as fh:
            for row in jobs_rows:
                fh.write(json.dumps(row) + "\n")
        with open(payments, "w", encoding="utf-8") as fh:
            for row in payment_rows:
                fh.write(json.dumps(row) + "\n")
        return str(jobs)

    setup.jobs = jobs
    setup.payments = payments
    return setup


def _paid(run_id: str) -> list:
    return [
        {"event": "payment_submitting", "run_id": run_id,
         "booking_ref": "2026-11-23 09:15"},
        {"event": "payment_result", "outcome": "success", "run_id": run_id,
         "booking_ref": "2026-11-23 09:15"},
    ]


# ── what it corrects ─────────────────────────────────────────────────────────

def test_a_paid_run_filed_as_failed_is_corrected(world):
    path = world([_job("aaa1")], _paid("aaa1"))

    report = jobs_backfill.backfill(path, dry_run=False)

    assert len(report.corrections) == 1
    fixed = report.corrections[0]
    assert fixed.was == "failed" and fixed.now == "payment_submitted"
    assert fixed.record["exit_code"] == 3
    assert fixed.record["outcome"] == "payment_submitted"
    assert "booked and paid" in fixed.record["detail"]
    assert fixed.slot == "2026-11-23 09:15"


def test_a_declined_run_is_corrected_and_flagged(world):
    path = world([_job("bbb1")], [
        {"event": "payment_submitting", "run_id": "bbb1"},
        {"event": "payment_result", "outcome": "declined", "run_id": "bbb1"},
    ])

    report = jobs_backfill.backfill(path, dry_run=False)
    fixed = report.corrections[0].record

    assert fixed["status"] == "payment_declined"
    assert fixed["exit_code"] == 4
    # A decline is the one of the two that does need a human.
    assert fixed["needs_attention"] is True


def test_an_unanswered_payment_is_not_called_paid(world):
    """Submitted with no result row: the honest reading is "unknown"."""
    path = world([_job("ccc1")],
                 [{"event": "payment_submitting", "run_id": "ccc1"}])

    fixed = jobs_backfill.backfill(path, dry_run=False).corrections[0].record

    assert fixed["status"] == "payment_submitted"
    assert "may have been charged" in fixed["detail"]
    assert "booked and paid" not in fixed["detail"]


def test_a_timed_out_or_unknown_record_is_eligible_too(world):
    """The same exit code reached these statuses by other routes."""
    path = world([_job("d1", status="timed_out"), _job("d2", status="unknown")],
                 _paid("d1") + _paid("d2"))

    assert len(jobs_backfill.plan(path).corrections) == 2


# ── what it must NOT touch ───────────────────────────────────────────────────

def test_a_run_with_no_payment_is_left_alone(world):
    """The whole safety property: silence in the journal means no change."""
    path = world([_job("eee1")], [])

    assert jobs_backfill.plan(path).corrections == []


def test_another_runs_payment_never_corrects_this_one(world):
    """A row stamped with someone else's run_id is definitively not ours.

    This is the failure mode that matters: claiming a charge for a run that
    never made one would invent a payment in a record about money.
    """
    path = world([_job("fff1")], _paid("SOMEONEELSE"))

    assert jobs_backfill.plan(path).corrections == []


def test_a_succeeded_run_is_never_downgraded(world):
    """A booking that completed stays "succeeded" — payment_view carries the
    card answer for it, so it needs no status change."""
    path = world([_job("ggg1", status="succeeded")], _paid("ggg1"))

    assert jobs_backfill.plan(path).corrections == []


def test_it_is_idempotent(world):
    """Running it twice must not append a second, identical record."""
    path = world([_job("hhh1")], _paid("hhh1"))

    first = jobs_backfill.backfill(path, dry_run=False)
    second = jobs_backfill.backfill(path, dry_run=False)

    assert len(first.corrections) == 1
    assert second.corrections == [], "a corrected record was corrected again"


def test_a_dry_run_writes_nothing(world):
    path = world([_job("iii1")], _paid("iii1"))
    before = Path(path).read_text(encoding="utf-8")

    report = jobs_backfill.backfill(path, dry_run=True)

    assert len(report.corrections) == 1
    assert report.applied is False
    assert Path(path).read_text(encoding="utf-8") == before


def test_the_journal_is_never_written(world):
    path = world([_job("jjj1")], _paid("jjj1"))
    before = world.payments.read_text(encoding="utf-8")

    jobs_backfill.backfill(path, dry_run=False)

    assert world.payments.read_text(encoding="utf-8") == before


# ── how it writes ────────────────────────────────────────────────────────────

def test_the_original_record_is_appended_over_not_erased(world):
    """An append-only log stays auditable; a rewrite falsifies history."""
    path = world([_job("kkk1")], _paid("kkk1"))

    jobs_backfill.backfill(path, dry_run=False)
    lines = [json.loads(line) for line in
             Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]

    assert len(lines) == 2, "the correction must be an extra line, not a swap"
    assert lines[0]["status"] == "failed", "the original line was modified"
    assert lines[1]["status"] == "payment_submitted"


def test_the_correction_says_why_it_exists(world):
    """A line that contradicts an earlier one should carry its own reason."""
    path = world([_job("lll1")], _paid("lll1"))

    fixed = jobs_backfill.backfill(path, dry_run=False).corrections[0].record

    assert fixed["corrected_by"] == "src.utils.jobs_backfill"
    assert fixed["corrected_from"]["status"] == "failed"
    assert fixed["corrected_from"]["exit_code"] == 1


def test_the_store_replays_to_the_corrected_record(world):
    """Last-record-wins is what makes the append a correction at all."""
    from src.api.modules.jobs.store import JobStore

    path = world([_job("mmm1")], _paid("mmm1"))
    jobs_backfill.backfill(path, dry_run=False)

    replayed = JobStore(path).load_all()["mmm1"]
    assert replayed["status"] == "payment_submitted"
    assert replayed["exit_code"] == 3


def test_a_corrected_record_rehydrates_into_a_job(world):
    """The extra keys must not stop the server loading the record at boot."""
    from src.api.modules.jobs.manager import JobRecord, JobStatus
    from src.api.modules.jobs.store import JobStore

    path = world([_job("nnn1")], _paid("nnn1"))
    jobs_backfill.backfill(path, dry_run=False)

    record = JobRecord.from_record(JobStore(path).load_all()["nnn1"])
    assert record is not None
    assert record.status is JobStatus.PAYMENT_SUBMITTED
    assert record.to_public()["payment"]["charged"] is True


# ── the CLI ──────────────────────────────────────────────────────────────────

def test_the_cli_is_a_dry_run_by_default(world, capsys):
    path = world([_job("ooo1")], _paid("ooo1"))

    assert jobs_backfill.main(["--jobs-file", path]) == 0
    out = capsys.readouterr().out

    assert "Would correct 1" in out
    assert "Nothing was written" in out
    assert len(Path(path).read_text(encoding="utf-8").splitlines()) == 1


def test_the_cli_says_the_api_must_restart(world, capsys):
    """A running server loads this file once and then serves from memory."""
    path = world([_job("ppp1")], _paid("ppp1"))

    jobs_backfill.main(["--jobs-file", path, "--apply"])
    out = capsys.readouterr().out

    assert "RESTART THE API" in out
    assert "Corrected 1" in out


def test_the_cli_is_quiet_when_there_is_nothing_to_do(world, capsys):
    path = world([_job("qqq1", status="succeeded")], [])

    jobs_backfill.main(["--jobs-file", path, "--apply"])

    assert "None filed a real payment" in capsys.readouterr().out
