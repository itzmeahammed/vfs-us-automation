"""Tests for parsing waitlist run outcomes into job records (Phase 2).

The API must report WHAT HAPPENED per client, not just a process exit code.
Three outcomes need distinguishing and none may be confused with the others:

    succeeded         the run completed (individual clients may still be skipped)
    slots_available   a bookable slot exists — BETTER than success, not an error
    needs_attention   a submit is pending/unknown — a human must resolve it
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("VFSAPI_SECRET_TOKEN", "d" * 64)

from src.api.jobs import (  # noqa: E402
    RESULT_JSON_BEGIN,
    RESULT_JSON_END,
    JobManager,
    JobRecord,
    JobStatus,
)


def _record(tmp_path, log_body: str) -> JobRecord:
    """A finished JobRecord whose log file contains `log_body`."""
    log_file = tmp_path / "job.log"
    log_file.write_text(log_body, encoding="utf-8")
    return JobRecord(
        job_id="test1234",
        command=["python", "-m", "src.waitlist", "run", "--json"],
        status=JobStatus.SUCCEEDED,
        started_at=datetime.now(timezone.utc),
        log_file=str(log_file),
    )


def _block(payload: dict) -> str:
    """Render a result block the way the CLI prints it."""
    return (f"{RESULT_JSON_BEGIN}\n"
            f"{json.dumps(payload)}\n"
            f"{RESULT_JSON_END}\n")


# This is a VERBATIM block from a real dry run against the live VFS portal, so
# the parser is tested against the actual format rather than an idealised one.
REAL_RUN_LOG = """\
[2026-08-19 18:26:36,704] INFO [runner.py:366] === Waitlist run: AE-CHE ===
[2026-08-19 18:27:36,654] INFO [runner.py:99] Browser traffic this run: 0.8 MB.

[SKIPPED] AE-CHE · Dubai - SCHENGEN · ahmed — registration is switched off
---VFS-RESULT-JSON-BEGIN---
{"outcome": "completed", "results": [{"route": "AE-CHE", "combo": "Dubai - SCHENGEN",\
 "registrant_id": "ahmed", "status": "skipped", "account": "pa***@travnook.com",\
 "reason": "waitlist registration is switched off", "vfs_reference": null,\
 "started_at": "2026-08-19T18:27:36", "finished_at": "2026-08-19T18:27:36",\
 "screenshots": [], "steps_completed": []}]}
---VFS-RESULT-JSON-END---
"""


def test_parses_a_real_run_log(tmp_path):
    """The parser must handle a genuine log — interleaved logging and all."""
    record = _record(tmp_path, REAL_RUN_LOG)
    JobManager._attach_results(record)

    assert record.outcome == "completed"
    assert len(record.results) == 1
    result = record.results[0]
    assert result["registrant_id"] == "ahmed"
    assert result["combo"] == "Dubai - SCHENGEN"
    assert result["status"] == "skipped"
    assert record.needs_attention is False


def test_pending_submit_flags_needs_attention(tmp_path):
    """A pending submit must be surfaced, never quietly retried."""
    record = _record(tmp_path, _block({
        "outcome": "completed",
        "results": [{"registrant_id": "a", "combo": "Dubai - SCHENGEN",
                     "status": "pending"}],
    }))
    JobManager._attach_results(record)
    assert record.needs_attention is True
    assert "unresolved" in (record.detail or "").lower()


def test_unknown_submit_flags_needs_attention(tmp_path):
    """'unknown' means submitted-but-unconfirmed: the most dangerous state."""
    record = _record(tmp_path, _block({
        "outcome": "completed",
        "results": [{"registrant_id": "a", "status": "unknown"}],
    }))
    JobManager._attach_results(record)
    assert record.needs_attention is True


def test_success_does_not_need_attention(tmp_path):
    record = _record(tmp_path, _block({
        "outcome": "completed",
        "results": [{"registrant_id": "a", "status": "success",
                     "vfs_reference": "REF123"}],
    }))
    JobManager._attach_results(record)
    assert record.needs_attention is False
    assert record.results[0]["vfs_reference"] == "REF123"


def test_mixed_results_are_all_reported(tmp_path):
    """One client failing must not hide the others' outcomes."""
    record = _record(tmp_path, _block({
        "outcome": "completed",
        "results": [
            {"registrant_id": "a", "status": "success"},
            {"registrant_id": "b", "status": "failed", "reason": "no waitlist"},
            {"registrant_id": "c", "status": "skipped"},
        ],
    }))
    JobManager._attach_results(record)
    assert len(record.results) == 3
    assert {r["status"] for r in record.results} == {"success", "failed", "skipped"}
    assert record.needs_attention is False


def test_slots_available_block_is_parsed(tmp_path):
    record = _record(tmp_path, _block({
        "outcome": "slots_available",
        "results": [],
        "combo": "Dubai - SCHENGEN",
        "banner": "Earliest available slot: 2026-09-01",
    }))
    JobManager._attach_results(record)
    assert record.outcome == "slots_available"
    assert record.results == []


# --------------------------------------------------------------------------
# Robustness — a parsing problem must never corrupt the job's status
# --------------------------------------------------------------------------


def test_missing_block_is_not_an_error(tmp_path):
    """The placeholder job prints no block; that must not break anything."""
    record = _record(tmp_path, "just some log output\nno json here\n")
    JobManager._attach_results(record)
    assert record.results == []
    assert record.outcome is None
    assert record.status is JobStatus.SUCCEEDED       # unchanged


def test_malformed_json_is_survived(tmp_path):
    """A truncated block (killed mid-write) must not raise."""
    record = _record(tmp_path, f"{RESULT_JSON_BEGIN}\n{{'not': valid json\n{RESULT_JSON_END}\n")
    JobManager._attach_results(record)
    assert record.results == []
    assert record.status is JobStatus.SUCCEEDED


def test_unterminated_block_is_survived(tmp_path):
    """A begin marker with no end marker — the run died mid-print."""
    record = _record(tmp_path, f"{RESULT_JSON_BEGIN}\n{{\"outcome\": \"completed\"\n")
    JobManager._attach_results(record)
    assert record.results == []


def test_missing_log_file_is_survived(tmp_path):
    record = JobRecord(
        job_id="x", command=["x"], status=JobStatus.SUCCEEDED,
        started_at=datetime.now(timezone.utc),
        log_file=str(tmp_path / "does-not-exist.log"),
    )
    JobManager._attach_results(record)
    assert record.results == []


def test_last_block_wins(tmp_path):
    """If a log somehow holds two blocks, the final one is authoritative."""
    body = (_block({"outcome": "completed", "results": [{"status": "failed"}]})
            + "more log output\n"
            + _block({"outcome": "completed", "results": [{"status": "success"}]}))
    record = _record(tmp_path, body)
    JobManager._attach_results(record)
    assert record.results[0]["status"] == "success"


def test_to_dict_exposes_the_new_fields(tmp_path):
    """The API response must carry results/outcome/needs_attention."""
    record = _record(tmp_path, _block({
        "outcome": "completed",
        "results": [{"registrant_id": "a", "status": "pending"}],
    }))
    JobManager._attach_results(record)
    body = record.to_dict()
    assert body["outcome"] == "completed"
    assert body["needs_attention"] is True
    assert body["results"][0]["status"] == "pending"


def test_slots_available_status_exists():
    """Exit code 2 maps to a dedicated status, not to 'failed'."""
    assert JobStatus.SLOTS_AVAILABLE.value == "slots_available"
    assert JobStatus.SLOTS_AVAILABLE is not JobStatus.FAILED
