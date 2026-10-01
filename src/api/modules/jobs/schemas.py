"""Job (background run) models."""

from __future__ import annotations
import re
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class JobResponse(BaseModel):
    """Status of a single job."""

    job_id: str
    run_id: str = Field(
        default="",
        description="The correlation key for this run. Equal to job_id for "
                    "API-triggered runs. Every log line the run wrote, every "
                    "journal row, and its screenshots folder all carry it — so "
                    "`jq 'select(.run_id==\"...\")' logs/app.jsonl` returns "
                    "the whole run.",
    )
    status: str = Field(
        description="running | succeeded | failed | timed_out | cancelled | "
                    "slots_available. Note 'slots_available' is a BETTER "
                    "outcome than success: a bookable slot exists, so the run "
                    "stopped and nothing was waitlisted — go book it."
    )
    command: List[str]
    pid: Optional[int] = None
    started_at: str
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    log_file: Optional[str] = None
    detail: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    outcome: Optional[str] = Field(
        default=None,
        description="Run-level verdict parsed from the job: 'completed' or "
                    "'slots_available'. Null if the job produced no block.",
    )
    results: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Per-client outcomes: route, combo, registrant_id, status "
                    "(skipped|dry_run|pending|success|unknown|failed), reason, "
                    "vfs_reference. Empty until the job finishes.",
    )
    needs_attention: bool = Field(
        default=False,
        description="True when a submit is pending/unknown. A HUMAN must verify "
                    "on the VFS account — never retry automatically, that risks "
                    "a duplicate registration.",
    )
class TriggerResponse(BaseModel):
    """202 Accepted body — the job was spawned, not completed."""

    accepted: bool = True
    message: str
    job: JobResponse
    replayed: bool = Field(
        default=False,
        description="True when an Idempotency-Key matched an earlier request "
                    "and the ORIGINAL job is being returned — nothing new was "
                    "started. Treat the job exactly as you would a fresh one.",
    )
class JobLogResponse(BaseModel):
    """Tail of a job's log file.

    Exists because the absolute `log_file` path in JobResponse is meaningless to
    a remote web app (and mildly disclosive). This returns the content instead.
    """

    job_id: str
    lines: List[str] = Field(default_factory=list)
    line_count: int = 0
    truncated: bool = Field(
        default=False,
        description="True when older lines were omitted — this is a TAIL, not "
                    "the whole log.",
    )
    log_available: bool = Field(
        default=True,
        description="False when the log file has been pruned or never existed.",
    )
class JobListResponse(BaseModel):
    """Recent job history."""

    count: int
    active_job_id: Optional[str] = None
    jobs: List[JobResponse]
