"""Shared test setup.

Keeps the suite off the real slot database: `src.slots.store` resolves its path
from VFS_SLOTS_DB first, so pointing that at a temp file means a test which
drives the supervisor can never write into `state/slots.db`.
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
