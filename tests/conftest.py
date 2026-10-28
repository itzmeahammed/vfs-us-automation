"""Shared test setup.

Keeps the suite off the real slot database: `src.slots.store` resolves its path
from VFS_SLOTS_DB first, so pointing that at a temp file means a test which
drives the supervisor can never write into `state/slots.db`.

The same hazard applies to the inbox watcher's seen-state, for the same reason
and with a worse consequence — see `_isolate_inbox_seen_state` below.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# The four [switches] masters default OFF, so a fresh machine does nothing
# unattended. The suite tests the behaviour BENEATH them, so it runs with them
# on; tests that assert a switch being off set it off explicitly. Env vars
# outrank the INI and survive reload_settings(), unlike patching the object.
for _flow in ("WAITLIST", "INVITE_BOOKING", "LIVE_BOOKING", "TEST_BOOKING"):
    os.environ.setdefault(f"VFSCFG_SWITCHES__{_flow}", "true")


@pytest.fixture(autouse=True)
def _isolate_booking_state(tmp_path, monkeypatch):
    """Keeps the suite out of the REAL booking requests and invitations.

    The inbox watcher records every invitation it classifies into
    state/invites/, so a test that drives a watcher pass would otherwise leave
    a fake invitation in the queue the supervisor books from.
    """
    from src.booking import invites, requests

    monkeypatch.setattr(invites, "INVITE_DIR", str(tmp_path / "invites"))
    monkeypatch.setattr(requests, "REQUEST_DIR", str(tmp_path / "booking_requests"))
    # The API audits every mutating call into state/audit.jsonl — the record
    # of who spent money. Test calls must never appear in it.
    monkeypatch.setattr("src.api.core.context.AUDIT_FILE",
                        str(tmp_path / "audit.jsonl"))


@pytest.fixture(autouse=True)
def _isolate_slot_database(tmp_path, monkeypatch):
    monkeypatch.setenv("VFS_SLOTS_DB", str(tmp_path / "slots-test.db"))
    from src.slots import store
    store.reset()
    yield
    store.reset()


@pytest.fixture(autouse=True)
def _isolate_inbox_seen_state(tmp_path, monkeypatch):
    """Keeps the suite out of the REAL `state/inbox_seen.json`.

    `watcher.run_pass()` saves the seen-state after every mailbox, and
    `seen.save()` reads STATE_DIR/STATE_FILE at call time. The `run_pass` tests
    in test_inbox_watcher.py patch the IMAP layer but not those paths, so
    running the suite overwrote the real file with the fixtures' empty state.

    That is worse than it looks. The seen-state is how the watcher knows which
    messages it has already read; emptying it does not merely lose data, it
    makes the next pass re-report every message in every mailbox as new. And
    because a matcher classifies invitations, a re-reported backlog is
    indistinguishable from a fresh invitation arriving.

    An autouse fixture rather than a per-file one: this is a global the whole
    suite can reach, and the next test to call run_pass() would reintroduce it.
    """
    from src.inbox import seen

    directory = tmp_path / "inbox-state"
    monkeypatch.setattr(seen, "STATE_DIR", str(directory))
    monkeypatch.setattr(seen, "STATE_FILE", str(directory / "inbox_seen.json"))
    yield


# --------------------------------------------------------------------------- #
# Keep the developer's real mailboxes and the live watcher out of the suite    #
# --------------------------------------------------------------------------- #

import pytest as _pytest_inbox_guard  # noqa: E402


@_pytest_inbox_guard.fixture(autouse=True)
def _no_real_mailboxes_or_background_watcher(monkeypatch):
    """[inbox] watch_all_accounts pulls every account in the REAL
    config/credentials.local.ini into mailbox_accounts(), and background_watch
    would start an IMAP-polling thread inside any TestClient that runs the
    lifespan. Neither belongs in a test: both are off here unless a test turns
    them on against its own stubs."""
    try:
        from src.settings import settings
        monkeypatch.setattr(settings().inbox, "watch_all_accounts", False)
        monkeypatch.setattr(settings().inbox, "background_watch", False)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        from src.inbox import background
        monkeypatch.setattr(background, "start", lambda: False)
    except Exception:                                   # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def _isolate_job_history(tmp_path, monkeypatch):
    """Keeps the suite out of the REAL `logs/api_jobs/jobs.jsonl`.

    2026-10-28: the web app reported 44 booking runs for the day. Three were
    real. The other 42 were `test_autobook.py` — it books ROUTE "AE-NOR" for
    requests "r1"/"t1", and `attempt_job._append` resolves JOBS_FILE at call
    time, so every run of the suite appended a fake booking to the live job
    history.

    That is not merely untidy. The API re-reads the history whenever its mtime
    changes (JobManager._absorb_external), so the fakes reached the dashboard;
    18 of them reported "succeeded", inflating the success rate; and 42 records
    a day evict genuine ones from the in-memory registry, whose ceiling is
    job_history_limit (100). The one log an operator consults to answer "did
    this job register anyone?" was being filled with runs that never happened.

    Autouse rather than per-file: the history is a global that the API module,
    the autobook child and the supervisor all reach, and the next test to spawn
    an attempt would reintroduce it. Both the env vars and the already-imported
    module constants are set — the env var alone is too late for a module that
    read it at import, and the constant alone does not reach a child process.
    """
    directory = tmp_path / "api-jobs"
    history = directory / "jobs.jsonl"

    monkeypatch.setenv("VFSAPI_JOB_HISTORY_FILE", str(history))
    monkeypatch.setenv("VFSAPI_JOB_LOG_DIR", str(directory))

    from src.booking import attempt_job
    monkeypatch.setattr(attempt_job, "JOBS_FILE", str(history))
    monkeypatch.setattr(attempt_job, "LOG_DIR", str(directory))

    # get_settings() is lru_cached, so the env vars above miss an instance that
    # was already built. Patch the live object too; pydantic may refuse, and a
    # refusal is not worth failing a test over when the two above still hold.
    try:
        from src.api.core.config import get_settings
        current = get_settings()
        monkeypatch.setattr(current, "job_history_file", history)
        monkeypatch.setattr(current, "job_log_dir", directory)
    except Exception:                                   # noqa: BLE001
        pass
    yield
