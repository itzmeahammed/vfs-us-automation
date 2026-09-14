"""Remember which messages have already been processed, across restarts.

Without this the watcher re-reports its whole backlog every time it starts, and
once it triggers anything (phase 9, not now) it would re-trigger it too. The
state is therefore a correctness requirement, not a convenience.

WHAT IS STORED, AND WHY IT IS A HIGH-WATER MARK
-----------------------------------------------
Per mailbox: the highest IMAP UID seen, plus a small tail of recent UIDs.

IMAP UIDs increase monotonically within a mailbox, so "highest seen" is nearly
enough on its own and stays O(1) however long the watcher runs. The tail exists
for one real case: a message that arrives while a pass is mid-flight can be
fetched with a UID below one already recorded, and a bare high-water mark would
skip it forever. Keeping the last few hundred UIDs closes that window at a cost
of a few KB.

    UIDVALIDITY is stored too. A server may renumber a mailbox (a restore, a
    migration); when it does, every stored UID becomes meaningless. Detecting
    the change and resetting is the difference between "re-reads the backlog
    once" and "silently never reads anything again".

Storage is one atomic JSON file, matching account_health.py / store.py: written
to a temp file in the same directory, fsync'd, then os.replace()d. A crash
mid-write leaves the previous state intact, never a truncated one.

NOT a source of truth about the world — only about what THIS process has looked
at. The journal records what happened; this records what has been read.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

STATE_DIR = "state"
STATE_FILE = os.path.join(STATE_DIR, "inbox_seen.json")

#: How many recent UIDs to keep per mailbox beyond the high-water mark.
#: Generous: a few hundred UIDs is a few KB, and the cost of being wrong here is
#: a silently skipped invitation.
TAIL_SIZE = 300


class SeenState:
    """Per-mailbox read state. Load once, mutate, save.

    Deliberately NOT auto-saving on every mark(): a pass over a mailbox marks
    many messages, and one fsync per pass is right where one per message would
    be wasteful. The watcher saves at the end of each mailbox.
    """

    def __init__(self, data: Optional[Dict] = None):
        self._data: Dict[str, Dict] = data or {}

    # ----------------------------------------------------------------- read --

    def _mailbox(self, mailbox: str) -> Dict:
        return self._data.setdefault(
            mailbox, {"high_water": 0, "recent": [], "uidvalidity": None}
        )

    def high_water(self, mailbox: str) -> int:
        """The highest UID processed for this mailbox (0 = nothing yet)."""
        return int(self._mailbox(mailbox).get("high_water") or 0)

    def is_seen(self, mailbox: str, uid: str) -> bool:
        """Whether this UID has already been processed."""
        try:
            numeric = int(uid)
        except (TypeError, ValueError):
            return False
        entry = self._mailbox(mailbox)
        if numeric <= int(entry.get("high_water") or 0):
            # Below the mark: seen, UNLESS it is in the tail, which only holds
            # UIDs that WERE processed. Anything at or under the mark that is
            # not in the tail was processed in an earlier pass.
            return True
        return numeric in set(entry.get("recent") or [])

    def last_pass(self, mailbox: str) -> float:
        """When this mailbox was last read, epoch seconds (0.0 = never).

        Used to narrow the server-side SEARCH so a pass does not re-list the
        whole mailbox every time. It NARROWS; it never decides what has been
        seen — the UID mark does that. Clocks skew and mail arrives out of
        order, so a timestamp is a safe hint and an unsafe test.
        """
        return float(self._mailbox(mailbox).get("last_pass") or 0.0)

    def record_pass(self, mailbox: str, when: float) -> None:
        """Stamps when this mailbox was last successfully read."""
        self._mailbox(mailbox)["last_pass"] = float(when)

    def check_uidvalidity(self, mailbox: str, uidvalidity: Optional[str]) -> bool:
        """Records UIDVALIDITY, resetting the mailbox if the server changed it.

        Returns True if a reset happened (the caller should expect to re-read
        the backlog once). A server that renumbers its mailbox invalidates every
        UID we hold; keeping them would mean skipping real mail forever.
        """
        if uidvalidity is None:
            return False
        entry = self._mailbox(mailbox)
        known = entry.get("uidvalidity")
        if known is None:
            entry["uidvalidity"] = str(uidvalidity)
            return False
        if str(known) != str(uidvalidity):
            log.warning(
                f"Mailbox {mailbox}: UIDVALIDITY changed {known} -> {uidvalidity}. "
                "Every stored UID is now meaningless; resetting the read state "
                "for this mailbox (its backlog will be re-read once)."
            )
            # last_pass is cleared along with the UIDs. Keeping it would narrow
            # the next SEARCH to a recent window, so the re-read this reset
            # exists to force would silently skip the backlog it needs to cover.
            self._data[mailbox] = {
                "high_water": 0, "recent": [], "uidvalidity": str(uidvalidity),
                "last_pass": 0.0,
            }
            return True
        return False

    # ---------------------------------------------------------------- write --

    def mark(self, mailbox: str, uid: str) -> None:
        """Records one UID as processed."""
        try:
            numeric = int(uid)
        except (TypeError, ValueError):
            return
        entry = self._mailbox(mailbox)
        recent: List[int] = list(entry.get("recent") or [])
        if numeric not in recent:
            recent.append(numeric)
        recent.sort()
        # Advance the mark over any unbroken run at the bottom of the tail, then
        # drop what the mark now covers — this is what keeps the tail bounded
        # without ever losing a gap that still matters.
        high = int(entry.get("high_water") or 0)
        while recent and recent[0] == high + 1:
            high = recent.pop(0)
        if len(recent) > TAIL_SIZE:
            recent = recent[-TAIL_SIZE:]
        entry["high_water"] = high
        entry["recent"] = recent

    def mailboxes(self) -> List[str]:
        return sorted(self._data)

    def to_dict(self) -> Dict:
        return self._data


# --------------------------------------------------------------------------- #
# Persistence                                                                  #
# --------------------------------------------------------------------------- #

def load() -> SeenState:
    """Reads the state file. A missing or corrupt file yields empty state.

    Corruption is logged and stepped over rather than raised: the worst case is
    re-reading a backlog into the digest, which is noise. Refusing to start the
    watcher because a cache file is malformed would be the greater harm.
    """
    if not os.path.isfile(STATE_FILE):
        return SeenState()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return SeenState(data)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log.warning(
            f"Could not read {STATE_FILE} ({e}); starting with empty read state. "
            "Recent mail may be reported twice."
        )
        return SeenState()


def save(state: SeenState) -> None:
    """Writes the state atomically. Never raises.

    Failure here costs a repeated digest entry, nothing more — so unlike the
    journal (which fails closed, because losing it means not knowing whether a
    registration landed) this logs and carries on.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=STATE_DIR, prefix=".inbox_seen-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_FILE)
        tmp_path = None
    except OSError as e:
        log.warning(f"Could not save {STATE_FILE}: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
