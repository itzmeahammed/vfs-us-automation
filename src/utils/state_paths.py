"""Where runtime state lives, and the one-time move that got it there.

    ═════════════════ THE LAYOUT ═════════════════

    config/     hand-edited, in git            you write these
    state/      machine-written, gitignored    the bot writes these
    logs/       rotatable text + JSONL         append-only, prunable
    runs/       per-run artifacts              runs/<ROUTE>/<run_id>/

`account_health.json`, `waitlist_cooldown.json` and `bandwidth_budget.json`
were written to the repository ROOT, next to pyproject.toml and the source
tree. That is a poor place for mutable state for three reasons that all cost
real time:

  * `git status` shows them, so they need gitignore entries, and a new one
    added without an entry gets committed — including whatever it happens to
    hold about accounts.
  * There is no single directory to back up, wipe, or exclude from a deploy.
  * "Is this file source or state?" is answerable only by reading it.

    ═════════════════ WHY THE MOVE IS AUTOMATIC ═════════════════

These files hold state you cannot regenerate: which accounts are cooling off
after a 429, how much proxy bandwidth is spent this month. Relocating the
constant and letting the old file sit unread would silently reset all of it —
an account benched for a real reason would look healthy, and the bot would
walk straight back into the block that benched it.

So `migrate()` moves the file the first time the new path is read. It is
idempotent, it never overwrites a newer file at the destination, and it leaves
the old one alone if anything goes wrong — losing the move is recoverable,
losing the state is not.
"""

from __future__ import annotations

import logging
import os
import shutil

log = logging.getLogger(__name__)

#: Machine-written state. One directory to back up, wipe, or gitignore.
STATE_DIR = "state"

#: Append-only text and JSONL. Prunable.
LOG_DIR = "logs"

#: Per-run artifacts: runs/<ROUTE>/<run_id>/.
RUNS_DIR = "runs"


def state_file(name: str, legacy: str = "") -> str:
    """Path to a state file under state/, migrating a root-level one once.

    Call this in place of a bare filename constant. The migration happens on
    the first call per process and is cheap after that (an os.path.exists on a
    file that is no longer there).
    """
    new_path = os.path.join(STATE_DIR, name)
    # `legacy` names an old location that is not the repo root — e.g. a journal
    # that used to live in logs/. Tried first, because that is where the real
    # history is for the files that moved out of logs/.
    old_path = legacy or name
    migrate(old_path, new_path)
    if os.path.isfile(old_path) and not os.path.exists(new_path):
        return old_path
    # If the move failed, the DATA is still at the old path, so that is the
    # path to use. Returning new_path unconditionally would hand back an empty
    # location and silently reset whatever the file held — the exact failure
    # the migration exists to avoid.
    return new_path


def migrate(name: str, new_path: str) -> bool:
    """Move a root-level state file to state/, once. Returns True if it moved.

    Deliberately conservative. Every early return is a case where doing nothing
    is the safe answer:

      * no old file                  nothing to do, the normal case after once
      * a file already at new_path   the new one is authoritative; never
                                     clobber it with a stale root copy
      * the move fails               keep the old file and carry on reading it,
                                     rather than leaving state in neither place
    """
    if not os.path.isfile(name):
        return False
    if os.path.exists(new_path):
        log.debug("Both %s and %s exist; using %s and leaving the old file.",
                  name, new_path, new_path)
        return False
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        shutil.move(name, new_path)
        log.info("Moved %s -> %s (state now lives under %s/).",
                 name, new_path, STATE_DIR)
        return True
    except OSError as exc:
        log.warning("Could not move %s to %s: %s. Continuing with the old "
                    "path so no state is lost.", name, new_path, exc)
        return False
