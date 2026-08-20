"""Durable record of every waitlist registration attempt.

Purpose, in order of importance:

  1. WRITE-AHEAD. The 'pending' row is written and flushed to disk BEFORE the
     committing submit is clicked. If the process dies mid-submit we still know a
     registration MAY have landed — which is the whole difference between "we can
     recover" and "no idea what happened".
  2. DANGLING DETECTION. A row left 'pending' (or 'unknown') from an earlier run
     blocks that same (route, combo, registrant) from being attempted again until
     a human confirms what happened on the VFS account. Preventing the SECOND bad
     registration is most of the value.
  3. DEDUP. A successful registration blocks a repeat for the same triple.

Storage is an append-only JSON-lines file, matching the atomic-write pattern
account_health.py and waitlist_cooldown.py already use — right-sized for a
manually-invoked tool that registers a handful of times.

Scaling note: the read-check-write in `blocking_entry()` is not atomic, so it is
only safe while there is exactly ONE writer at a time. That invariant is now
enforced explicitly rather than assumed: every browser-driving entry point takes
the global run lock in `src/utils/runlock.py` —

    supervisor.main()              runlock.acquire("slot-check", on_busy="skip")
    runner.run_registration()      runlock.acquire("waitlist-run")
    run_task.ps1 / run_ec2.sh      the same OS mutex / lockfile

so a scheduled slot check, an auto-triggered registration and a manual run
serialise instead of interleaving. DO NOT add a new writer without taking that
lock; two concurrent runs can double-register a client, which costs a real
appointment slot.

If registration ever needs genuine PARALLELISM (several runs at once, rather
than several callers taking turns), the lock stops being enough — swap this for
SQLite with

    CREATE UNIQUE INDEX ... ON registrations(route, combo, registrant_id)
        WHERE status IN ('pending','success','unknown');

so dedup becomes a database guarantee instead of a check. The public API here is
deliberately narrow (append / blocking_entry / dangling / update_status) so that
swap touches this file only.
"""

import json
import logging
import os
from typing import List, Optional

from src.waitlist.result import Status, WaitlistResult

JOURNAL_DIR = "state"
JOURNAL_FILE = os.path.join(JOURNAL_DIR, "waitlist_journal.jsonl")


def _normalise(text: str) -> str:
    """Collapses ALL whitespace runs and lowercases.

    Internal whitespace must be collapsed, not just trimmed: a combo label
    retyped as 'Dubai  -  SCHENGEN' has to dedup against 'Dubai - SCHENGEN', or
    a stray double space silently permits a duplicate registration. Matches
    registrant._normalise so a label lands on the same identity in both places.
    """
    return " ".join((text or "").split()).lower()


def _key(route: str, combo: str, registrant_id: str) -> tuple:
    """The dedup identity of a registration. Case- and whitespace-insensitive."""
    return (
        _normalise(route).upper(),
        _normalise(combo),
        _normalise(registrant_id),
    )


# --------------------------------------------------------------------------- #
# Read                                                                         #
# --------------------------------------------------------------------------- #

def entries() -> List[dict]:
    """Every journal row, oldest first. Missing/corrupt file reads as empty."""
    if not os.path.isfile(JOURNAL_FILE):
        return []
    rows = []
    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    # A truncated final line (killed mid-write) is expected and
                    # survivable; skip it rather than failing the whole read.
                    logging.warning(
                        f"Skipping malformed journal line {line_no} in {JOURNAL_FILE}."
                    )
    except OSError as e:
        # Fail CLOSED: an unreadable journal must not look like "nothing has ever
        # been registered", because that would permit a duplicate registration.
        raise RuntimeError(
            f"Could not read the waitlist journal {JOURNAL_FILE}: {e}. "
            "Refusing to register without it (a duplicate registration is worse "
            "than a skipped run)."
        ) from e
    return rows


def results() -> List[WaitlistResult]:
    """Journal rows as WaitlistResult objects."""
    return [WaitlistResult.from_dict(row) for row in entries()]


def latest_for(route: str, combo: str, registrant_id: str) -> Optional[dict]:
    """The most recent row for a (route, combo, registrant), or None."""
    want = _key(route, combo, registrant_id)
    for row in reversed(entries()):
        if _key(row.get("route"), row.get("combo"), row.get("registrant_id")) == want:
            return row
    return None


def blocking_entry(route: str, combo: str, registrant_id: str) -> Optional[dict]:
    """
    Returns the row that should PREVENT a new attempt for this triple, or None.

    Blocking states are Status.COMMITTED_STATES — success (already registered),
    pending (an in-flight submit that may have landed) and unknown (a committed
    failure awaiting a human). Everything else — failed, skipped, dry_run — never
    blocks, because nothing was submitted.
    """
    row = latest_for(route, combo, registrant_id)
    if row and row.get("status") in Status.COMMITTED_STATES:
        return row
    return None


def blocking_entry_for_route(route: str, registrant_id: str) -> Optional[dict]:
    """The row proving this client already holds an entry on this ROUTE, or None.

    Stronger than `blocking_entry`, and deliberately so. That one keys on
    (route, combo, registrant_id), which is right for tracking entries: two
    combos ARE two separate registrations on the portal.

    But one client wants ONE appointment. A person already waitlisted for
    'Dubai - SCHENGEN' must not also be queued for 'Abu Dhabi - SCHENGEN' on the
    same route — that holds two slots for one need, denies one to somebody else,
    and risks VFS voiding both as duplicates.

    So a client's `combos` list is a PREFERENCE ORDER ("I'll take whichever
    opens first"), not a shopping list. This is the query that enforces it:
    the first committed entry on a route ends that client's run for that route,
    now and in future runs, until the entry is resolved or cancelled.

    Returns the row for whichever combo they hold, so callers can name it.
    """
    want_route = _normalise(route).upper()
    want_client = _normalise(registrant_id)

    # Latest row per combo, then the first still in a committed state.
    latest: dict = {}
    for row in entries():
        if _normalise(row.get("route")).upper() != want_route:
            continue
        if _normalise(row.get("registrant_id")) != want_client:
            continue
        latest[_normalise(row.get("combo"))] = row

    for row in latest.values():
        if row.get("status") in Status.COMMITTED_STATES:
            return row
    return None


def dangling() -> List[dict]:
    """Rows needing a human: 'pending' or 'unknown' (see Status.NEEDS_ATTENTION).

    Only the LATEST row per triple counts — an old 'pending' later resolved to
    'success' is settled, not dangling.
    """
    latest = {}
    for row in entries():
        latest[_key(row.get("route"), row.get("combo"), row.get("registrant_id"))] = row
    return [row for row in latest.values()
            if row.get("status") in Status.NEEDS_ATTENTION]


def count_since(iso_timestamp: str) -> int:
    """How many committing attempts were STARTED at/after a timestamp.

    Used for the per-day cap. Dry runs and guard skips are excluded — they never
    touched VFS.
    """
    return sum(
        1 for row in entries()
        if (row.get("started_at") or "") >= iso_timestamp
        and row.get("status") not in (Status.SKIPPED, Status.DRY_RUN)
    )


# --------------------------------------------------------------------------- #
# Write                                                                        #
# --------------------------------------------------------------------------- #

def append(result: WaitlistResult) -> None:
    """
    Appends a row and FLUSHES IT TO DISK before returning.

    The fsync is the point: register.py calls this with status='pending'
    immediately before the committing click, so a power cut between the two
    still leaves evidence that a submit was about to happen.
    """
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    line = json.dumps(result.to_dict(), ensure_ascii=False)
    try:
        with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())   # survive a crash, not just a clean exit
    except OSError as e:
        # Fail CLOSED again: if we cannot record the intent, we must not act.
        raise RuntimeError(
            f"Could not write the waitlist journal {JOURNAL_FILE}: {e}. "
            "Refusing to proceed without a durable record."
        ) from e
    logging.debug(f"Journal: {result.status} {result.route}/{result.combo}"
                  f"/{result.registrant_id}")


def update_status(result: WaitlistResult) -> None:
    """
    Records the terminal outcome of an attempt.

    Append-only by design: the 'pending' row is never rewritten, so the full
    history stays auditable and a crashed run's evidence can't be erased by the
    code that follows it. Readers always take the LATEST row per triple.

    On SUCCESS this is also where a client's identity documents are deleted —
    see _delete_documents_on_success.
    """
    append(result)
    if result.status == Status.SUCCESS:
        _delete_documents_on_success(result)


def _delete_documents_on_success(result: WaitlistResult) -> None:
    """Removes the client's uploaded documents once a registration is confirmed.

    This is the retention rule, and the journal is the right place for it: it is
    the single point that knows a document has served its only purpose. A
    passport scan kept after that is pure liability with no remaining use.

    Deleted ONLY on 'success'. Never on 'pending' or 'unknown' — those may be
    retried or need a human to reconcile them against the portal, and deleting
    the document would make that impossible. The retention sweep
    (documents.purge_older_than) is what eventually clears those.

    Best-effort: a cleanup failure must never turn a successful registration
    into a failed one. It is logged, and the sweep retries.
    """
    try:
        from src.waitlist import documents

        removed = documents.delete_for(
            result.registrant_id,
            reason=f"registration confirmed ({result.route} / {result.combo})")
        if removed:
            logging.debug(
                f"Retention: {removed} document(s) removed for "
                f"'{result.registrant_id}' after a confirmed registration.")
    except Exception as e:
        logging.warning(
            f"Could not delete documents for '{result.registrant_id}' after a "
            f"successful registration: {e}. The retention sweep will retry.")


def clear() -> None:
    """Deletes the journal. Destructive — for tests and deliberate resets only."""
    if os.path.isfile(JOURNAL_FILE):
        os.remove(JOURNAL_FILE)
        logging.warning(f"Waitlist journal {JOURNAL_FILE} cleared.")


def resolve(route: str, combo: str, registrant_id: str,
            status: str, reason: str = "") -> WaitlistResult:
    """
    Human resolution of a dangling entry: records what was actually found on the
    VFS account, unblocking (or permanently blocking) the triple.

    Used by the CLI's `resolve` command after someone has checked the portal.
    """
    if status not in (Status.SUCCESS, Status.FAILED):
        raise ValueError(
            f"Resolve status must be '{Status.SUCCESS}' (it did register) or "
            f"'{Status.FAILED}' (it did not); got '{status}'."
        )
    row = latest_for(route, combo, registrant_id)
    result = WaitlistResult(
        route=route, combo=combo, registrant_id=registrant_id,
        status=status,
        account=(row or {}).get("account", ""),
        reason=reason or "resolved manually",
        vfs_reference=(row or {}).get("vfs_reference"),
    )
    result.finish(status, result.reason)
    append(result)
    return result
