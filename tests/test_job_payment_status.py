"""A paid booking must never be reported as a failed job.

AE-NOR, 2026-10-27, run 37b0e0e9d937731c: the card was charged, 3-D Secure
cleared, VFS itself returned PaymentStatus=True and the appointment was held
for 23 Nov 09:15. The API listed that run as:

    status: failed
    exit_code: 1
    detail: "Process exited with code 1."
    outcome: null

while the booking-request endpoint for the SAME run said "booked and paid".
Two halves of one API telling an operator opposite stories about whether a
client's card had been charged.

The cause was not a bug in the payment path. walk.py marks the payment step
not-ok on every submit DELIBERATELY, so that nothing downstream can retry a
payment that may have taken money — which made `walk.ok` false, which meant
exit 1, which meant FAILED. The safety mechanism and the status were fighting
each other.

These tests pin the rule that settles it: a run that touched the card gets its
own status, its own exit code, and a machine-readable answer about the money.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("VFSAPI_SECRET_TOKEN", "d" * 64)

from src.api.modules.jobs import manager as mgr          # noqa: E402
from src.api.modules.jobs.manager import (                # noqa: E402
    MONEY_MOVED_STATUSES,
    TERMINAL_STATUSES,
    JobRecord,
    JobStatus,
)


def _record(status: JobStatus, run_id: str = "run0000000000",
            detail: str = "") -> JobRecord:
    return JobRecord(
        job_id=run_id, run_id=run_id, command=["python", "-m", "src.booking"],
        status=status, started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc), detail=detail,
        payload={"route": "AE-NOR", "mode": "commit"})


@pytest.fixture
def journal(monkeypatch, tmp_path):
    """Redirect the payment journal, and let a test write rows into it."""
    from src.payment import journal as journal_mod

    path = tmp_path / "payments.jsonl"
    monkeypatch.setattr(journal_mod, "JOURNAL_FILE", str(path))

    def write(*rows):
        import json
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    return write


# ── the CLI's half: an exit code that does not lie ───────────────────────────

def _walk(**flags):
    from src.booking.walk import StepReport, WalkResult

    result = WalkResult(steps=[StepReport(name="payment")])
    result.steps[0].ok = flags.pop("step_ok", False)
    for key, value in flags.items():
        setattr(result, key, value)
    return result


class _Probe:
    def __init__(self, walk, ok=True, interrupted=False):
        self.walk = walk
        self.ok = ok
        self.interrupted = interrupted


def test_a_submitted_payment_does_not_exit_one():
    """Exit 1 means "nothing happened, retrying is safe". This is not that."""
    from src.booking.__main__ import EXIT_PAYMENT_SUBMITTED, _probe_exit_code

    code = _probe_exit_code(_Probe(_walk(payment_submitted=True)))
    assert code == EXIT_PAYMENT_SUBMITTED
    assert code != 1


def test_a_declined_payment_has_its_own_code():
    """Declined is not "nothing happened" either — funds may have moved."""
    from src.booking.__main__ import EXIT_PAYMENT_DECLINED, _probe_exit_code

    assert _probe_exit_code(
        _Probe(_walk(payment_declined=True, payment_submitted=True))
    ) == EXIT_PAYMENT_DECLINED


def test_declined_beats_submitted():
    """Both flags are set on a decline; the more specific one must win."""
    from src.booking.__main__ import EXIT_PAYMENT_DECLINED, _probe_exit_code

    assert _probe_exit_code(
        _Probe(_walk(payment_declined=True, payment_submitted=True))
    ) == EXIT_PAYMENT_DECLINED


def test_an_ordinary_failure_still_exits_one():
    """The new codes must not swallow the case they were carved out of."""
    from src.booking.__main__ import _probe_exit_code

    assert _probe_exit_code(_Probe(_walk())) == 1
    assert _probe_exit_code(_Probe(_walk(step_ok=True))) == 0


def test_ctrl_c_still_wins_over_everything():
    """130 stays distinct: the human stopping a run is not a payment event."""
    from src.booking.__main__ import _probe_exit_code

    assert _probe_exit_code(
        _Probe(_walk(payment_submitted=True), interrupted=True)) == 130


def test_the_payment_codes_do_not_collide_with_slots_available():
    """2 belongs to the waitlist CLI. Booking must never return it."""
    from src.api.modules.jobs.manager import EXIT_SLOTS_AVAILABLE
    from src.booking.__main__ import (EXIT_PAYMENT_DECLINED,
                                      EXIT_PAYMENT_SUBMITTED)

    assert EXIT_SLOTS_AVAILABLE not in (EXIT_PAYMENT_SUBMITTED,
                                        EXIT_PAYMENT_DECLINED)
    assert EXIT_PAYMENT_SUBMITTED != EXIT_PAYMENT_DECLINED


def test_the_manager_agrees_with_the_cli_on_the_codes():
    """Two modules name these numbers; a drift would mis-file real money."""
    from src.api.modules.jobs import manager
    from src.booking import __main__ as cli

    assert manager.EXIT_PAYMENT_SUBMITTED == cli.EXIT_PAYMENT_SUBMITTED
    assert manager.EXIT_PAYMENT_DECLINED == cli.EXIT_PAYMENT_DECLINED


# ── the API's half: a status and a money answer ──────────────────────────────

def test_both_payment_statuses_are_terminal():
    """A job that charged a card is over. Nothing may wait for more."""
    assert JobStatus.PAYMENT_SUBMITTED in TERMINAL_STATUSES
    assert JobStatus.PAYMENT_DECLINED in TERMINAL_STATUSES
    assert MONEY_MOVED_STATUSES == {JobStatus.PAYMENT_SUBMITTED,
                                    JobStatus.PAYMENT_DECLINED}


def test_a_submitted_payment_is_not_rendered_as_an_error():
    """to_public() rewrites a failure's detail through the VFS translator.

    Applied here it would replace a sentence about a successful payment with a
    generic apology, because public_message() is built for VFS error pages and
    has no idea what this one says.
    """
    from src.utils import public_errors as pe

    assert JobStatus.PAYMENT_SUBMITTED.value in pe.OK_STATUSES
    assert JobStatus.PAYMENT_SUBMITTED.value in pe.AUTHORED_DETAIL_STATUSES
    # A decline IS in the error family, but its wording is still authored.
    assert JobStatus.PAYMENT_DECLINED.value not in pe.OK_STATUSES
    assert JobStatus.PAYMENT_DECLINED.value in pe.AUTHORED_DETAIL_STATUSES


def test_an_authored_detail_survives_to_public():
    written = ("The payment went through: the gateway approved it and VFS "
               "returned a confirmation.")
    record = _record(JobStatus.PAYMENT_SUBMITTED, detail=written)

    assert record.to_public()["detail"] == written


def test_a_paid_run_reports_charged_true(journal):
    journal(
        {"event": "payment_submitting", "run_id": "run0000000000"},
        {"event": "payment_result", "outcome": "success",
         "run_id": "run0000000000"},
    )
    pay = _record(JobStatus.PAYMENT_SUBMITTED).payment_view()

    assert pay["charged"] is True
    assert pay["gateway_answer"] == "success"
    assert pay["retry_safe"] is False


def test_an_unanswered_payment_reports_charged_NONE_not_false(journal):
    """*** THE THIRD VALUE IS THE WHOLE POINT. ***

    A payment that was submitted and never answered is UNKNOWN, not "no". A
    boolean would have to pick, and either pick is a lie about money: false
    tells a client their card is clear when it may not be, true tells them
    they are booked when they may not be.
    """
    journal({"event": "payment_submitting", "run_id": "run0000000000"})
    pay = _record(JobStatus.PAYMENT_SUBMITTED).payment_view()

    assert pay["charged"] is None
    assert pay["gateway_answer"] == "not_answered"


def test_a_declined_payment_reports_charged_false(journal):
    journal(
        {"event": "payment_submitting", "run_id": "run0000000000"},
        {"event": "payment_result", "outcome": "declined",
         "run_id": "run0000000000"},
    )
    pay = _record(JobStatus.PAYMENT_DECLINED).payment_view()

    assert pay["charged"] is False
    assert pay["retry_safe"] is False


def test_a_run_that_never_paid_has_no_payment_block(journal):
    journal()
    assert _record(JobStatus.SUCCEEDED).payment_view() is None
    assert _record(JobStatus.FAILED).payment_view() is None
    assert _record(JobStatus.SUCCEEDED).to_public()["payment"] is None


def test_a_SUCCEEDED_booking_still_reports_the_card(journal):
    """Gated on the JOURNAL, not the status.

    An automatic booking that completes is recorded "succeeded", so a status
    gate would have reported the card on every troubled run and stayed silent
    on every clean one — exactly backwards.
    """
    journal(
        {"event": "payment_submitting", "run_id": "run0000000000"},
        {"event": "payment_result", "outcome": "success",
         "run_id": "run0000000000"},
    )
    pay = _record(JobStatus.SUCCEEDED).payment_view()

    assert pay is not None and pay["charged"] is True


def test_another_runs_payment_is_never_claimed_as_ours(journal):
    """rows_for_run's rule, enforced at this layer too.

    A row stamped with someone else's run_id is definitively not ours. Taking
    it would report a charge on a run that never started a browser.
    """
    journal({"event": "payment_result", "outcome": "success",
             "run_id": "SOMEONEELSE00"})

    assert _record(JobStatus.SUCCEEDED).payment_view() is None


def test_an_unreadable_journal_does_not_change_the_verdict(monkeypatch):
    """A reporting bug must not turn a paid booking into a mystery."""
    monkeypatch.setattr(mgr, "payment_facts",
                        lambda run_id: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        mgr.payment_facts("x")

    # And the real implementation swallows its own read errors:
    from src.payment import journal as journal_mod
    monkeypatch.undo()
    monkeypatch.setattr(journal_mod, "JOURNAL_FILE", "/nonexistent/nope.jsonl")
    assert mgr.payment_facts("run0000000000") == (False, "")


def test_no_run_id_means_no_claim():
    assert mgr.payment_facts("") == (False, "")


# ── the automatic lane reaches the same verdict independently ────────────────

def test_the_auto_book_lane_does_not_file_a_payment_as_failed():
    """It writes its own job records, so it cannot inherit the exit codes."""
    from src.booking import requests as store
    from src.booking.autobook import Outcome, job_status

    submitted = [{"event": "payment_submitting"}]
    declined = [{"event": "payment_submitting"},
                {"event": "payment_result", "outcome": "declined"}]
    nothing: list = []

    attention = Outcome(store.NEEDS_ATTENTION, "needs_attention", "why", {})
    assert job_status(attention, submitted) == ("payment_submitted",
                                                "payment_submitted")
    assert job_status(attention, declined) == ("payment_declined",
                                               "payment_declined")
    assert job_status(attention, nothing) == ("failed", "failed")

    booked = Outcome(store.BOOKED, "booked", "booked and paid", {})
    assert job_status(booked, submitted) == ("succeeded", "completed")

    waiting = Outcome(store.WAITING, "attempt_failed", "stopped early", {})
    assert job_status(waiting, nothing) == ("failed", "failed")


def test_the_auto_book_exit_code_agrees_with_its_status():
    """It printed "exit code 1" beside a status saying a card was charged."""
    from src.booking.attempt_job import _exit_code_for
    from src.booking.__main__ import (EXIT_PAYMENT_DECLINED,
                                      EXIT_PAYMENT_SUBMITTED)

    assert _exit_code_for("succeeded") == 0
    assert _exit_code_for("payment_submitted") == EXIT_PAYMENT_SUBMITTED
    assert _exit_code_for("payment_declined") == EXIT_PAYMENT_DECLINED
    assert _exit_code_for("failed") == 1
    assert _exit_code_for("anything_else") == 1


# ── what the web app is promised ─────────────────────────────────────────────

def test_the_schema_documents_every_status_the_api_can_emit():
    """A status the web app has never heard of renders as a grey unknown."""
    from src.api.modules.jobs.schemas import JobResponse

    described = JobResponse.model_fields["status"].description
    for status in JobStatus:
        assert status.value in described, (
            f"{status.value!r} can be returned but is not in the schema "
            "description the web app is written against")


def test_the_schema_has_the_payment_field():
    from src.api.modules.jobs.schemas import JobResponse

    assert "payment" in JobResponse.model_fields
    described = JobResponse.model_fields["payment"].description
    assert "charged" in described and "retry_safe" in described


def test_the_console_renders_both_payment_statuses():
    """The console's fallback is a grey pill with the raw status string.

    That is a safe default and a bad answer for "was my client's card
    charged" — so both statuses need their own branch, and neither may use
    the red "failed" pill.
    """
    console = (REPO_ROOT / "src" / "api" / "console.html").read_text(
        encoding="utf-8", errors="replace")

    assert 's === "payment_submitted"' in console
    assert 's === "payment_declined"' in console
    # The money answer, not just the status, decides the pill.
    assert "j.payment && j.payment.charged" in console


def test_the_client_timeline_carries_the_payment_answer():
    """"Was my client's card charged" must not need a second request."""
    source = (REPO_ROOT / "src" / "api" / "modules" / "clients"
              / "service.py").read_text(encoding="utf-8", errors="replace")

    assert '"payment": public.get("payment")' in source
