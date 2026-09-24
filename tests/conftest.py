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
