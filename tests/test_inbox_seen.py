"""Read-state tracking: the quietest way to lose an invitation.

If this is wrong the watcher either re-reports its whole backlog forever
(noise), or — far worse — silently skips a message and nobody ever learns the
client was invited. Neither failure raises anything, so it has to be tested
directly rather than noticed in production.

The subtle part is the high-water mark plus tail. A bare mark would skip a
message that arrives mid-pass with a lower UID than one already recorded; the
tail exists to close exactly that window, and these tests pin that behaviour.
"""

import json
import os

import pytest

from src.inbox import seen as seen_mod
from src.inbox.seen import TAIL_SIZE, SeenState


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """Points the module's paths at a temp dir."""
    directory = tmp_path / "state"
    directory.mkdir()
    path = directory / "inbox_seen.json"
    monkeypatch.setattr(seen_mod, "STATE_DIR", str(directory))
    monkeypatch.setattr(seen_mod, "STATE_FILE", str(path))
    return path


# --------------------------------------------------------------------------- #
# The basics                                                                   #
# --------------------------------------------------------------------------- #

def test_a_fresh_mailbox_has_seen_nothing():
    state = SeenState()
    assert state.high_water("a@b.com") == 0
    assert not state.is_seen("a@b.com", "1")


def test_marking_advances_the_high_water_mark():
    state = SeenState()
    for uid in ("1", "2", "3"):
        state.mark("a@b.com", uid)
    assert state.high_water("a@b.com") == 3
    assert state.is_seen("a@b.com", "2")


def test_mailboxes_are_independent():
    """One account's read state must never mask another's mail."""
    state = SeenState()
    state.mark("a@b.com", "5")
    assert state.is_seen("a@b.com", "5")
    assert not state.is_seen("other@b.com", "5")


def test_everything_at_or_below_the_mark_reads_as_seen():
    state = SeenState()
    for uid in ("1", "2", "3"):
        state.mark("m", uid)
    assert state.is_seen("m", "1")
    assert state.is_seen("m", "3")
    assert not state.is_seen("m", "4")


# --------------------------------------------------------------------------- #
# The gap case — why the tail exists                                           #
# --------------------------------------------------------------------------- #

def test_a_gap_is_not_swallowed_by_the_high_water_mark():
    """THE case this design exists for.

    Marking 10 while 1-9 are unread must NOT advance the mark past them: a
    message that arrives mid-pass can carry a lower UID than one already
    processed, and a bare high-water mark would skip it forever.
    """
    state = SeenState()
    state.mark("m", "10")

    assert state.high_water("m") == 0, "the mark must not jump over unread UIDs"
    assert state.is_seen("m", "10")
    assert not state.is_seen("m", "5"), "an unread UID below a marked one is still unread"


def test_the_mark_advances_once_the_gap_is_filled():
    state = SeenState()
    state.mark("m", "3")
    assert state.high_water("m") == 0

    state.mark("m", "1")
    assert state.high_water("m") == 1      # 2 is still missing

    state.mark("m", "2")
    assert state.high_water("m") == 3      # now the run 1,2,3 collapses


def test_marking_out_of_order_is_safe():
    state = SeenState()
    for uid in ("5", "3", "1", "4", "2"):
        state.mark("m", uid)
    assert state.high_water("m") == 5
    for uid in ("1", "2", "3", "4", "5"):
        assert state.is_seen("m", uid)


def test_marking_the_same_uid_twice_is_harmless():
    state = SeenState()
    state.mark("m", "1")
    state.mark("m", "1")
    assert state.high_water("m") == 1


# --------------------------------------------------------------------------- #
# Bounding                                                                     #
# --------------------------------------------------------------------------- #

def test_a_contiguous_run_keeps_the_tail_empty():
    """The whole point of collapsing: normal sequential mail costs O(1) state,
    however long the watcher runs."""
    state = SeenState()
    for uid in range(1, 1000):
        state.mark("m", str(uid))
    assert state.high_water("m") == 999
    assert state._mailbox("m")["recent"] == []


def test_the_tail_is_capped_even_with_a_permanent_gap():
    """A UID that never arrives must not grow the tail without bound."""
    state = SeenState()
    for uid in range(2, 2 + TAIL_SIZE + 50):   # 1 never marked
        state.mark("m", str(uid))
    assert state.high_water("m") == 0
    assert len(state._mailbox("m")["recent"]) <= TAIL_SIZE


def test_a_non_numeric_uid_is_ignored_rather_than_crashing():
    state = SeenState()
    state.mark("m", "not-a-uid")
    assert state.high_water("m") == 0
    assert not state.is_seen("m", "not-a-uid")
    assert not state.is_seen("m", None)


# --------------------------------------------------------------------------- #
# UIDVALIDITY — the silent-death case                                          #
# --------------------------------------------------------------------------- #

def test_first_uidvalidity_is_recorded_without_a_reset():
    state = SeenState()
    assert state.check_uidvalidity("m", "12345") is False
    assert state._mailbox("m")["uidvalidity"] == "12345"


def test_an_unchanged_uidvalidity_preserves_the_state():
    state = SeenState()
    state.check_uidvalidity("m", "12345")
    state.mark("m", "1")
    state.mark("m", "7")
    assert state.check_uidvalidity("m", "12345") is False
    assert state.high_water("m") == 1        # 2-6 never arrived
    assert state.is_seen("m", "7")           # but 7 is still remembered


def test_a_changed_uidvalidity_resets_the_mailbox():
    """A server that renumbers a mailbox invalidates every UID we hold. Keeping
    them would mean skipping real mail forever — the backlog is re-read once
    instead, which is only noise."""
    state = SeenState()
    state.check_uidvalidity("m", "111")
    state.mark("m", "50")
    assert state.is_seen("m", "50")

    assert state.check_uidvalidity("m", "222") is True
    assert state.high_water("m") == 0
    assert not state.is_seen("m", "50")
    assert state._mailbox("m")["uidvalidity"] == "222"


def test_a_missing_uidvalidity_changes_nothing():
    """Not every server reports it; absence must not look like a change."""
    state = SeenState()
    state.mark("m", "5")
    assert state.check_uidvalidity("m", None) is False
    assert state.is_seen("m", "5")


def test_uidvalidity_is_compared_as_a_string():
    """imaplib hands it back as bytes/str inconsistently; 111 and '111' are the
    same server state and must not trigger a reset."""
    state = SeenState()
    state.check_uidvalidity("m", "111")
    state.mark("m", "5")
    assert state.check_uidvalidity("m", 111) is False
    assert state.is_seen("m", "5"), "an int/str mismatch must not wipe the state"


# --------------------------------------------------------------------------- #
# Persistence                                                                  #
# --------------------------------------------------------------------------- #

def test_state_survives_a_save_and_load(state_file):
    state = SeenState()
    state.check_uidvalidity("a@b.com", "999")
    for uid in ("1", "2", "3"):
        state.mark("a@b.com", uid)
    state.mark("a@b.com", "10")
    seen_mod.save(state)

    reloaded = seen_mod.load()
    assert reloaded.high_water("a@b.com") == 3
    assert reloaded.is_seen("a@b.com", "10")
    assert not reloaded.is_seen("a@b.com", "5")
    assert reloaded.check_uidvalidity("a@b.com", "999") is False


def test_a_missing_file_loads_as_empty(state_file):
    assert not state_file.exists()
    assert seen_mod.load().mailboxes() == []


def test_a_corrupt_file_loads_as_empty_rather_than_raising(state_file):
    """Worst case is re-reporting a backlog, which is noise. Refusing to start
    the watcher because a cache file is malformed would be the greater harm."""
    state_file.write_text("{not json", encoding="utf-8")
    assert seen_mod.load().mailboxes() == []


def test_a_file_containing_the_wrong_shape_loads_as_empty(state_file):
    state_file.write_text('["a", "list"]', encoding="utf-8")
    assert seen_mod.load().mailboxes() == []


def test_saving_is_atomic_and_leaves_no_temp_files(state_file):
    state = SeenState()
    state.mark("m", "1")
    seen_mod.save(state)

    leftovers = [f for f in os.listdir(os.path.dirname(str(state_file)))
                 if f.startswith(".inbox_seen-")]
    assert leftovers == [], f"temp files left behind: {leftovers}"
    assert json.loads(state_file.read_text(encoding="utf-8"))["m"]["high_water"] == 1


def test_an_unwritable_location_is_survived_not_raised(state_file, monkeypatch):
    """Failure costs a repeated digest entry, nothing more — so unlike the
    journal (which fails closed) this logs and carries on."""
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(seen_mod.tempfile, "mkstemp", boom)
    seen_mod.save(SeenState())          # must not raise


def test_save_then_load_round_trips_many_mailboxes(state_file):
    state = SeenState()
    for i in range(5):
        state.mark(f"acct{i}@example.com", str(i + 1))
    seen_mod.save(state)

    reloaded = seen_mod.load()
    assert len(reloaded.mailboxes()) == 5
    for i in range(5):
        assert reloaded.is_seen(f"acct{i}@example.com", str(i + 1))


# --------------------------------------------------------------------------- #
# Incremental scanning — narrowing the server-side SEARCH                      #
# --------------------------------------------------------------------------- #

def test_a_mailbox_starts_with_no_last_pass():
    assert SeenState().last_pass("m") == 0.0


def test_record_pass_stamps_the_time():
    state = SeenState()
    state.record_pass("m", 1756000000.0)
    assert state.last_pass("m") == 1756000000.0


def test_last_pass_survives_a_save_and_load(state_file):
    state = SeenState()
    state.record_pass("a@b.com", 1756000000.0)
    seen_mod.save(state)
    assert seen_mod.load().last_pass("a@b.com") == 1756000000.0


def test_a_uidvalidity_reset_also_clears_last_pass():
    """Otherwise the re-read this reset exists to force would be narrowed to a
    recent window and would silently skip the backlog it needs to cover."""
    state = SeenState()
    state.check_uidvalidity("m", "111")
    state.record_pass("m", 1756000000.0)
    state.mark("m", "50")

    assert state.check_uidvalidity("m", "222") is True
    assert state.last_pass("m") == 0.0, "a renumbered mailbox must be re-read in full"


def test_last_pass_is_per_mailbox():
    state = SeenState()
    state.record_pass("a@b.com", 1756000000.0)
    assert state.last_pass("other@b.com") == 0.0
