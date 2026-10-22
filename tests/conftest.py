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
