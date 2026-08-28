"""Create, update and delete client files under config/registrants/.

`registrant.py` deliberately only READS. This module is the write half, kept
separate so the read path can never accidentally mutate a client file.

Three properties matter here, all of them about not losing or leaking data:

  * **Atomic.** Written to a temp file in the same directory, fsync'd, then
    os.replace()d over the target. A crash mid-write leaves the old file
    intact, never a half-written one. A client file holds a passport number;
    truncating one is not recoverable from here.

  * **Restrictive permissions where the OS honours them.** chmod 0600 is applied
    and is meaningful on POSIX (EC2). On WINDOWS IT IS A NO-OP — files land at
    the directory's inherited ACL (verified: existing client files all report
    mode 666 there). Windows protection therefore comes from the ACL on
    config/registrants/, which grants only the owning user and Administrators.
    That is the same posture as the hand-written client files this replaces, so
    the API does not weaken it — but do not read 0600 in the code below as a
    guarantee on Windows. If these files ever need stronger local protection,
    the fix is an explicit `icacls` grant, not chmod.

  * **Refuses silent overwrites.** `create()` fails if the file exists;
    replacing one is a separate, explicit call. An accidental double-submit
    from a flaky network must not clobber live client data.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.waitlist.errors import WaitlistConfigError
from src.waitlist.registrant import REGISTRANT_DIR, path_for

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Keys we refuse to persist: they are transport-level, not client data.
_STRIP_KEYS = frozenset({"_id"})

# Timestamps the STORE owns. A caller may send them — a web app round-tripping
# a client it just read will — but they are always overwritten from the clock
# here, never trusted from input. Otherwise "when was this created?" becomes
# whatever the caller last claimed, and the audit trail is worth nothing.
CREATED_AT = "created_at"
UPDATED_AT = "updated_at"
ENABLED_AT = "enabled_at"
_TIMESTAMP_KEYS = frozenset({CREATED_AT, UPDATED_AT, ENABLED_AT})


def _now() -> str:
    """UTC, ISO 8601, second precision — the format every timestamp here uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ClientExistsError(WaitlistConfigError):
    """Raised by create() when the client file is already there."""


class ClientNotFoundError(WaitlistConfigError):
    """Raised by update()/delete()/get() for an unknown client id."""


def _check_id(registrant_id: str) -> str:
    """Validate the id, which becomes the filename."""
    rid = str(registrant_id or "").strip().lower()
    if not _ID_RE.match(rid):
        raise WaitlistConfigError(
            f"Client id {registrant_id!r} must be lowercase letters, digits, "
            "underscore or hyphen, starting with a letter or digit.")
    # Defence in depth: the regex already excludes these, but the value becomes
    # a path, so an explicit traversal check earns its keep.
    if os.sep in rid or (os.altsep and os.altsep in rid) or ".." in rid:
        raise WaitlistConfigError(f"Client id {registrant_id!r} is not a bare name.")
    return rid


def _clean(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop transport-only keys before persisting."""
    return {k: v for k, v in data.items() if k not in _STRIP_KEYS}


def exists(registrant_id: str) -> bool:
    """Is there already a client file with this id?"""
    return os.path.exists(path_for(_check_id(registrant_id)))


def _atomic_write(path: str, payload: Dict[str, Any]) -> None:
    """Write `payload` as pretty JSON, atomically, mode 0600."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    # Same directory as the target, so os.replace is a true atomic rename
    # (a cross-filesystem move would not be).
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)     # no-op on Windows, meaningful on POSIX
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        # Never leave a stray temp file behind on failure.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def create(registrant_id: str, data: Dict[str, Any]) -> str:
    """Write a NEW client file. Raises ClientExistsError if one exists.

    Returns the path written. Callers should validate first — this writes what
    it is given (see waitlist.validate.precheck_client).
    """
    rid = _check_id(registrant_id)
    path = path_for(rid)
    if os.path.exists(path):
        raise ClientExistsError(
            f"Client '{rid}' already exists. Use update() to change it, or pick "
            "a different id.")

    payload = _clean(data)
    stamp = _now()
    payload[CREATED_AT] = stamp
    payload[UPDATED_AT] = stamp
    # A client created already armed was armed NOW; one created parked has
    # never been armed, so the field stays absent rather than being back-dated
    # by the next edit.
    if payload.get("enabled"):
        payload[ENABLED_AT] = stamp

    _atomic_write(path, payload)
    log.info("Created client file for '%s' (%d field(s)).", rid, len(data))
    return path


def update(registrant_id: str, data: Dict[str, Any], *,
           merge: bool = True) -> str:
    """Replace or merge an EXISTING client file.

    Args:
        merge: True (default) updates only the supplied keys, leaving the rest
            of the file — including comments — intact. False replaces the whole
            document.

    Raises ClientNotFoundError if the client does not exist.
    """
    rid = _check_id(registrant_id)
    path = path_for(rid)
    if not os.path.exists(path):
        raise ClientNotFoundError(f"No client '{rid}' to update.")

    payload = _clean(data)

    with open(path, "r", encoding="utf-8") as fh:
        existing = json.load(fh)

    # The store owns the timestamps, so whatever the caller sent is discarded
    # before merging. A web app that GETs a client and PUTs it back would
    # otherwise write its own stale values straight over ours.
    for key in _TIMESTAMP_KEYS:
        payload.pop(key, None)

    was_enabled = bool(existing.get("enabled"))

    if merge:
        existing.update(payload)
        payload = existing
    else:
        # merge=False is a REPLACE (true PUT), which is exactly how created_at
        # would get lost: the caller cannot send it, and nothing else carries
        # it forward. Creation time is a fact about the client, not a field of
        # the current document, so it survives the replace.
        if existing.get(CREATED_AT):
            payload[CREATED_AT] = existing[CREATED_AT]
        if existing.get(ENABLED_AT):
            payload[ENABLED_AT] = existing[ENABLED_AT]

    stamp = _now()
    payload[UPDATED_AT] = stamp
    payload.setdefault(CREATED_AT, stamp)   # pre-timestamp file being touched

    # enabled_at marks the last ARMING, not every edit while armed — so it moves
    # only on a false -> true transition. Disabling clears it: a parked client
    # has no "armed since", and leaving a stale one reads as though it were
    # still live.
    now_enabled = bool(payload.get("enabled"))
    if now_enabled and not was_enabled:
        payload[ENABLED_AT] = stamp
    elif not now_enabled:
        payload.pop(ENABLED_AT, None)

    _atomic_write(path, payload)
    log.info("Updated client file for '%s' (merge=%s).", rid, merge)
    return path


def set_enabled(registrant_id: str, enabled: bool) -> str:
    """Park or un-park a client without touching their data."""
    return update(registrant_id, {"enabled": bool(enabled)}, merge=True)


def delete(registrant_id: str) -> None:
    """Remove a client file. Raises ClientNotFoundError if absent."""
    rid = _check_id(registrant_id)
    path = path_for(rid)
    if not os.path.exists(path):
        raise ClientNotFoundError(f"No client '{rid}' to delete.")
    os.unlink(path)
    log.info("Deleted client file for '%s'.", rid)


def get_raw(registrant_id: str) -> Dict[str, Any]:
    """The client file's raw contents. Raises ClientNotFoundError if absent.

    Note this includes secrets (account_password). Anything heading for an API
    response must go through waitlist.redaction / the API's own scrubber.
    """
    rid = _check_id(registrant_id)
    path = path_for(rid)
    if not os.path.exists(path):
        raise ClientNotFoundError(f"No client '{rid}'.")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def list_ids(route: Optional[str] = None) -> List[str]:
    """Every client id, optionally filtered to one route.

    Skips unreadable files rather than failing the whole listing — one corrupt
    client should not hide the others.
    """
    if not os.path.isdir(REGISTRANT_DIR):
        return []

    ids: List[str] = []
    for name in sorted(os.listdir(REGISTRANT_DIR)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        rid = name[: -len(".json")]
        if not _ID_RE.match(rid):
            continue
        if route:
            try:
                data = get_raw(rid)
            except (OSError, ValueError, WaitlistConfigError):
                continue
            if str(data.get("route", "")).strip().upper() != route.strip().upper():
                continue
        ids.append(rid)
    return ids


def backfill_timestamps(stamp: Optional[str] = None) -> List[str]:
    """Give pre-timestamp client files a created_at/updated_at.

    Clients written before the store kept timestamps have none, which would
    force every consumer to handle null forever — sorting a list by "newest"
    breaks, and a UI has to render a blank column. Stamping them once removes
    that special case permanently.

    The value is deliberately TODAY rather than the file's mtime: mtime records
    the last EDIT, not the creation, so it would look precise while being wrong.
    A single honest "backfilled on this date" is easier to reason about than
    four different plausible-looking lies. Fields already present are never
    overwritten.

    Returns the ids that were changed.
    """
    stamp = stamp or _now()
    changed: List[str] = []
    for rid in list_ids():
        try:
            data = get_raw(rid)
        except Exception:                              # noqa: BLE001
            continue                                   # unreadable: leave alone
        needs_enabled_at = bool(data.get("enabled")) and not data.get(ENABLED_AT)
        if data.get(CREATED_AT) and data.get(UPDATED_AT) and not needs_enabled_at:
            continue
        data.setdefault(CREATED_AT, stamp)
        data.setdefault(UPDATED_AT, stamp)
        # An already-armed client has no arming date to recover, but leaving it
        # blank would render as "never armed" next to enabled=true — a
        # contradiction. It gets the migration stamp, same as the others.
        if needs_enabled_at:
            data[ENABLED_AT] = stamp
        _atomic_write(path_for(rid), data)
        changed.append(rid)
    if changed:
        log.info("Backfilled timestamps for %d client(s): %s",
                 len(changed), ", ".join(changed))
    return changed
