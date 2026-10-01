"""GET /payments/unanswered, GET /jobs/{id}/stream, and the run_id chain.

The payment tests care about one property above all others: an unreadable
journal must NOT be reported as "nothing unanswered". Those two states are the
same shape over JSON and have opposite meanings, and serving the reassuring one
would hide the exact emergency the endpoint exists to surface.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

TEST_TOKEN = "e" * 64
os.environ.setdefault("VFSAPI_SECRET_TOKEN", TEST_TOKEN)

from src.utils.config_reader import initialize_config  # noqa: E402

initialize_config()

AUTH = {"X-Webhook-Secret-Token": TEST_TOKEN}


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A TestClient with the payment journal pointed at a temp file."""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")

    monkeypatch.setenv("VFSAPI_SECRET_TOKEN", TEST_TOKEN)
    from src.api.core.config import get_settings
    get_settings.cache_clear()

    from src.payment import journal

    monkeypatch.setattr(journal, "JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(journal, "JOURNAL_FILE", str(tmp_path / "payments.jsonl"))

    from src.api.main import app

    with fastapi_testclient.TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()


def _write_journal(tmp_path, rows):
    path = tmp_path / "payments.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


# --------------------------------------------------------------------------- #
# GET /payments/unanswered                                                     #
# --------------------------------------------------------------------------- #

def test_unanswered_requires_a_token(client):
    assert client.get("/v1/booking/payments/unanswered").status_code in (401, 403, 429)


def test_no_journal_means_nothing_unanswered(client):
    """A system that has never taken a payment is healthy, not broken."""
    body = client.get("/v1/booking/payments/unanswered", headers=AUTH).json()
    assert body["count"] == 0
    assert body["needs_attention"] is False
    assert body["journal_readable"] is True


def test_a_submitted_payment_with_no_result_is_reported(client, tmp_path):
    _write_journal(tmp_path, [
        {"event": "payment_submitting", "booking_ref": "REF1",
         "run_id": "abc123", "at": "2026-09-29T10:00:00Z",
         "url": "https://gateway.example/pay"},
    ])
    body = client.get("/v1/booking/payments/unanswered", headers=AUTH).json()
    assert body["count"] == 1
    assert body["needs_attention"] is True
    payment = body["payments"][0]
    assert payment["booking_ref"] == "REF1"
    assert payment["run_id"] == "abc123", (
        "the run_id is the whole point: it is how an operator finds the log "
        "and journal rows for the run that may have charged a card"
    )


def test_a_payment_with_a_recorded_result_is_not_reported(client, tmp_path):
    _write_journal(tmp_path, [
        {"event": "payment_submitting", "booking_ref": "REF1"},
        {"event": "payment_result", "booking_ref": "REF1", "ok": True},
    ])
    body = client.get("/v1/booking/payments/unanswered", headers=AUTH).json()
    assert body["count"] == 0
    assert body["needs_attention"] is False


def test_only_the_answered_payment_clears(client, tmp_path):
    """Two submits, one answer — the other must still be flagged."""
    _write_journal(tmp_path, [
        {"event": "payment_submitting", "booking_ref": "REF1"},
        {"event": "payment_submitting", "booking_ref": "REF2"},
        {"event": "payment_result", "booking_ref": "REF1", "ok": True},
    ])
    body = client.get("/v1/booking/payments/unanswered", headers=AUTH).json()
    assert body["count"] == 1
    assert body["payments"][0]["booking_ref"] == "REF2"


def test_an_unreadable_journal_needs_attention_rather_than_reading_as_empty(
    client, monkeypatch
):
    """THE fail-closed property. "I cannot read it" must never serve as "it is
    empty" — that would hide every unanswered payment behind a disk error."""
    from src.payment import journal

    def boom():
        raise OSError("disk is on fire")

    monkeypatch.setattr(journal, "unanswered", boom)

    body = client.get("/v1/booking/payments/unanswered", headers=AUTH).json()
    assert body["journal_readable"] is False
    assert body["needs_attention"] is True, (
        "an unreadable payment journal must be an alert, not a clean bill "
        "of health"
    )


def test_no_card_field_can_reach_the_response(client, tmp_path):
    """The journal filters card keys on write; the response allow-lists on
    read. Both, because this is the one place a PAN would be durable."""
    _write_journal(tmp_path, [
        {"event": "payment_submitting", "booking_ref": "REF1",
         "card_number": "4111111111111111", "cvn": "123",
         "expiry": "12/28", "password": "hunter2"},
    ])
    raw = client.get("/v1/booking/payments/unanswered", headers=AUTH).text
    for leak in ("4111111111111111", "123", "12/28", "hunter2"):
        assert leak not in raw, f"{leak!r} reached an HTTP response"


def test_the_journal_write_path_strips_card_keys_and_stamps_a_run_id(tmp_path,
                                                                     monkeypatch):
    """Belt and braces on the writer itself, not only the reader."""
    from src.payment import journal

    monkeypatch.setattr(journal, "JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(journal, "JOURNAL_FILE", str(tmp_path / "p.jsonl"))

    journal.append({
        "event": "payment_submitting",
        "booking_ref": "REF9",
        "card_number": "4111111111111111",
        "card_cvn": "999",
    })

    row = json.loads((tmp_path / "p.jsonl").read_text(encoding="utf-8").strip())
    assert "card_number" not in row and "card_cvn" not in row
    assert row["run_id"], "every journal row must carry the correlation key"


# --------------------------------------------------------------------------- #
# The run_id chain                                                             #
# --------------------------------------------------------------------------- #

def test_run_context_prefers_an_inherited_id_over_minting_one():
    """This inheritance IS the chain: it is what makes the child's rows join
    to the parent's job."""
    from src.utils import run_context

    run_context.reset_for_tests()
    try:
        os.environ[run_context.ENV_VAR] = "abc123def456"
        assert run_context.run_id() == "abc123def456"
    finally:
        run_context.reset_for_tests()


def test_run_context_is_stable_within_a_process():
    from src.utils import run_context

    run_context.reset_for_tests()
    try:
        first = run_context.run_id()
        assert run_context.run_id() == first
        assert os.environ[run_context.ENV_VAR] == first, (
            "the id must be exported so a grandchild joins the same chain"
        )
    finally:
        run_context.reset_for_tests()


@pytest.mark.parametrize("hostile", ["../../etc/passwd", "a/b", "x" * 80,
                                     "has space", "semi;colon"])
def test_a_hostile_inherited_id_is_replaced_not_sanitised(hostile):
    """The id reaches a directory name, so traversal must not survive. It is
    REPLACED rather than cleaned: a caller sending a strange id has a bug we
    want a fresh id for, not a quietly mangled one."""
    from src.utils import run_context

    run_context.reset_for_tests()
    try:
        os.environ[run_context.ENV_VAR] = hostile
        got = run_context.run_id()
        assert got != hostile
        assert got.isalnum()
    finally:
        run_context.reset_for_tests()


def test_waitlist_rows_carry_a_run_id_and_old_rows_still_load():
    from src.waitlist.result import WaitlistResult

    row = WaitlistResult(route="AE-NOR", combo="Dubai - SCHENGEN",
                         registrant_id="someone", status="pending").to_dict()
    assert row["run_id"]

    # A row written before run_id existed must still deserialise.
    legacy = {k: v for k, v in row.items() if k != "run_id"}
    assert WaitlistResult.from_dict(legacy).route == "AE-NOR"


def test_job_records_expose_a_run_id_equal_to_the_job_id():
    """One id, not two with a mapping table between them."""
    from datetime import datetime, timezone

    from src.api.modules.jobs.manager import JobRecord, JobStatus

    record = JobRecord(
        job_id="feedface", run_id="feedface", command=["x"],
        status=JobStatus.RUNNING, started_at=datetime.now(timezone.utc),
    )
    assert record.to_dict()["run_id"] == "feedface"


def test_a_job_record_persisted_before_run_id_existed_falls_back_to_job_id():
    from src.api.modules.jobs.manager import JobRecord

    record = JobRecord.from_record({
        "job_id": "oldjob1", "command": [], "status": "succeeded",
        "started_at": "2026-09-01T10:00:00+00:00",
    })
    assert record is not None
    assert record.run_id == "oldjob1"


def test_the_spawn_env_carries_the_run_id_to_the_child(monkeypatch):
    """The single line on which the whole chain depends.

    Asserted by capturing the env handed to create_subprocess_exec, because
    this is the exact boundary the id has to cross and nothing else in the
    codebase proves it does.
    """
    import asyncio

    from src.api.modules.jobs.manager import JobManager

    captured = {}

    class FakeProcess:
        pid = 4321

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs.get("env") or {})
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    manager = JobManager()

    async def run():
        return await manager._spawn(
            [sys.executable, "-c", "pass"],
            Path(os.devnull),
            run_id="chainlink99",
        )

    asyncio.run(run())

    from src.utils.run_context import ENV_VAR

    assert captured.get(ENV_VAR) == "chainlink99", (
        "without this the child mints its own id and its journal rows and log "
        "lines no longer join to the API job"
    )
    assert "VFSAPI_SECRET_TOKEN" not in captured, (
        "the API's shared secret must never reach a child process"
    )


# --------------------------------------------------------------------------- #
# GET /jobs/{id}/stream                                                        #
# --------------------------------------------------------------------------- #

def test_stream_requires_a_token(client):
    assert client.get("/v1/jobs/whatever/stream").status_code in (401, 403, 429)


def test_stream_404s_for_an_unknown_job(client):
    response = client.get("/v1/jobs/nosuchjob/stream", headers=AUTH)
    assert response.status_code == 404


def test_stream_replays_a_finished_jobs_log_and_ends_by_itself(client, tmp_path):
    """A stream against a terminal job must drain and close, not hang: a client
    looping over the events has to be able to fall out of the loop."""
    from datetime import datetime, timezone

    from src.api.modules.jobs.manager import JobRecord, JobStatus
    from src.api.modules.jobs.runtime import job_manager

    log_file = tmp_path / "job.log"
    log_file.write_text("first line\nsecond line\n", encoding="utf-8")

    record = JobRecord(
        job_id="streamtest", run_id="streamtest", command=["x"],
        status=JobStatus.SUCCEEDED,
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        exit_code=0, log_file=str(log_file),
    )
    job_manager._jobs["streamtest"] = record
    try:
        with client.stream("GET", "/v1/jobs/streamtest/stream?from_start=true",
                           headers=AUTH) as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
            body = "".join(response.iter_text())
    finally:
        job_manager._jobs.pop("streamtest", None)

    assert "first line" in body and "second line" in body
    assert "event: end" in body, "the stream must terminate itself"
    assert '"run_id": "streamtest"' in body


def test_stream_announces_the_run_id_before_any_log_line(client, tmp_path):
    """The first frame is the join key, so a watcher has it immediately —
    including when the run then fails before writing anything useful."""
    from datetime import datetime, timezone

    from src.api.modules.jobs.manager import JobRecord, JobStatus
    from src.api.modules.jobs.runtime import job_manager

    record = JobRecord(
        job_id="announce1", run_id="announce1", command=["x"],
        status=JobStatus.SUCCEEDED, started_at=datetime.now(timezone.utc),
        log_file=None,
    )
    job_manager._jobs["announce1"] = record
    try:
        with client.stream("GET", "/v1/jobs/announce1/stream", headers=AUTH) as r:
            body = "".join(r.iter_text())
    finally:
        job_manager._jobs.pop("announce1", None)

    assert body.index("event: status") < body.index("event: end")
    assert '"run_id": "announce1"' in body


def test_sse_frames_survive_a_log_line_containing_a_newline():
    """An SSE data field is newline-delimited, so an unescaped newline in a log
    line would split one frame into two and corrupt the stream."""
    from src.api.modules.jobs.handlers import _sse

    frame = _sse("log", {"line": "before\nafter"})
    assert frame.count("data:") == 1
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload["line"] == "before\nafter"
