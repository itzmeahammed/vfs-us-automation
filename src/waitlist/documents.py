"""Client identity documents — the one owner of document paths.

Some portals (Italy) do not ask for typed applicant details at all: you upload
the passport bio page and VFS OCRs the fields out of it. That means holding a
scan of someone's passport, which is a materially bigger liability than the
passport NUMBER already in their client file — a bio page carries the photo, the
MRZ and the signature, i.e. enough for identity takeover.

Three decisions shape this module (see DOCUMENT_STORAGE_TASKS.md for the
research behind them):

  1. DOCUMENTS LIVE OUTSIDE THE REPO. config/registrants/ is inside the git
     working tree and its ignore rules cover *.json only, so a scan dropped
     there would be committed — and a passport in git history cannot be recalled.
     The default root is per-user application data, never the project directory.

  2. DELETION MATTERS MORE THAN ENCRYPTION. For an unattended bot, any
     encryption key must be retrievable without a human, so it sits on the same
     machine as the ciphertext and an attacker with code execution gets both.
     Full-disk encryption covers the realistic threats (stolen laptop, leaked
     snapshot). What actually reduces exposure is not keeping the file: delete on
     confirmed success, and sweep anything stale regardless.

  3. ONE MODULE OWNS THE PATHS. Every read, write and delete goes through here,
     with an id -> path mapping rather than paths built inline from client names.
     That is what keeps a later move to S3 or envelope encryption a contained
     change instead of a refactor, and it is the same discipline registrant.py
     and journal.py already follow.

Deletion is a plain os.remove(). Deliberately: NIST SP 800-88r2 does NOT
recommend overwrite passes on SSDs — wear-levelling and remapped blocks mean the
passes never reach all physical cells — and names cryptographic erase as the
method for SSDs and virtual storage, which full-disk encryption already provides.
A shred loop would be cargo cult that also wears the disk.
"""

import logging
import os
import re
import shutil
import time
from typing import List, Optional

from src.utils.config_reader import get_config_value, initialize_config
from src.waitlist.errors import WaitlistConfigError, WaitlistStepError

#: Accepted document types. VFS states PNG/JPG/PDF only.
ALLOWED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".pdf")

#: VFS rejects anything larger. Checked HERE so a bad file fails with our error
#: before a browser is launched, rather than as an opaque portal rejection.
MAX_BYTES = 2 * 1024 * 1024

#: First bytes of each accepted format. An extension is a claim, not a check —
#: a .png that is really something else should fail before it reaches a portal.
_MAGIC = {
    b"\x89PNG\r\n\x1a\n": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"%PDF": ".pdf",
}

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: Document kinds. The stored filename is fixed per kind, so a client's real
#: name never lands in a path that could surface in a log or a stack trace.
PASSPORT = "passport_bio"


def root() -> str:
    """The directory holding all client documents.

    [waitlist] documents_root, else a per-user application-data directory —
    NEVER the project directory, so git cannot see documents regardless of
    ignore rules.
    """
    initialize_config()
    configured = (get_config_value("waitlist", "documents_root", "") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "vfs-bot", "documents")
    # Linux/EC2: /var/lib when writable (a service account's home may not be),
    # else fall back to the user's own data directory.
    system = os.path.join("/var", "lib", "vfs-bot", "documents")
    if os.access(os.path.dirname(system), os.W_OK):
        return system
    return os.path.join(os.path.expanduser("~"), ".local", "share",
                        "vfs-bot", "documents")


def dir_for(registrant_id: str) -> str:
    """The directory holding one client's documents.

    One directory per client, so erasing a client is a single rmtree — which is
    what makes a GDPR Article 17 request a one-liner rather than a search.
    """
    registrant_id = (registrant_id or "").strip().lower()
    if not _ID_RE.match(registrant_id):
        # Also blocks traversal: a client id is the only thing interpolated into
        # this path, so constraining it constrains the path.
        raise WaitlistConfigError(
            f"Invalid client id '{registrant_id}' for a document path.")
    return os.path.join(root(), registrant_id)


def path_for(registrant_id: str, kind: str = PASSPORT) -> Optional[str]:
    """The stored document of this kind for a client, or None if absent.

    The extension is whatever was stored, so this searches rather than assuming.
    """
    directory = dir_for(registrant_id)
    for extension in ALLOWED_EXTENSIONS:
        candidate = os.path.join(directory, f"{kind}{extension}")
        if os.path.isfile(candidate):
            return candidate
    return None


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #

def _sniff(path: str) -> Optional[str]:
    """The extension implied by the file's magic bytes, or None if unknown."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    for magic, extension in _MAGIC.items():
        if head.startswith(magic):
            return extension
    return None


def validate(path: str, label: str = "document") -> str:
    """Checks a file is an acceptable document. Returns its normalised extension.

    Raises WaitlistStepError with an actionable message — these are all things a
    human can fix, and finding out here costs a second rather than a failed run.
    """
    if not path:
        raise WaitlistStepError(f"{label}: no path given.")
    if not os.path.isfile(path):
        raise WaitlistStepError(
            f"{label}: no file at '{path}'. Use an absolute path — a relative "
            "one resolves against the working directory, which differs between "
            "a manual run and a scheduled one.")

    extension = os.path.splitext(path)[1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise WaitlistStepError(
            f"{label}: '{extension or 'no extension'}' is not accepted. VFS "
            f"takes {', '.join(ALLOWED_EXTENSIONS)} only.")

    size = os.path.getsize(path)
    if size > MAX_BYTES:
        raise WaitlistStepError(
            f"{label}: {size / 1024 / 1024:.1f} MB exceeds the "
            f"{MAX_BYTES / 1024 / 1024:.0f} MB portal limit. Resize or "
            "re-export it.")
    if size == 0:
        raise WaitlistStepError(f"{label}: the file is empty.")

    sniffed = _sniff(path)
    if sniffed is None:
        raise WaitlistStepError(
            f"{label}: '{path}' is not a readable PNG, JPG or PDF — the "
            "extension says one thing and the contents another.")
    # .jpg/.jpeg share magic bytes; treat them as one.
    if {sniffed, extension} - {".jpg", ".jpeg"} and sniffed != extension:
        raise WaitlistStepError(
            f"{label}: named '{extension}' but the contents are '{sniffed}'. "
            "Re-save it in the format the name claims.")
    return extension


# --------------------------------------------------------------------------- #
# Store / fetch / delete                                                       #
# --------------------------------------------------------------------------- #

def _harden(path: str, is_dir: bool = False) -> None:
    """Restricts a document path to its owner.

    Effective on Linux/EC2. On Windows os.chmod only touches the read-only bit —
    the POSIX mode does NOT map to an ACL — so this is close to a no-op there.
    That is acceptable rather than hidden: the default root on Windows is under
    %LOCALAPPDATA%, already outside other non-admin users' reach. Say so plainly
    instead of calling chmod and assuming it did something.
    """
    try:
        os.chmod(path, 0o700 if is_dir else 0o600)
    except OSError as e:
        logging.debug(f"Could not tighten permissions on {path}: {e}")


def store(registrant_id: str, source_path: str, kind: str = PASSPORT) -> str:
    """Copies a document into the managed store and returns its new path.

    Use this to take a file the operator dropped somewhere ad hoc and bring it
    under the retention rules. A document already inside the store is left alone.
    """
    extension = validate(source_path, label=f"{kind} for '{registrant_id}'")
    if extension == ".jpeg":
        extension = ".jpg"

    directory = dir_for(registrant_id)
    os.makedirs(directory, exist_ok=True)
    _harden(directory, is_dir=True)

    destination = os.path.join(directory, f"{kind}{extension}")
    if os.path.abspath(source_path) == os.path.abspath(destination):
        return destination

    # Remove any other-extension copy of the same kind, so a client never ends
    # up with two passports and an ambiguous "which one is current".
    for existing in ALLOWED_EXTENSIONS:
        stale = os.path.join(directory, f"{kind}{existing}")
        if stale != destination and os.path.isfile(stale):
            os.remove(stale)

    shutil.copy2(source_path, destination)
    _harden(destination)
    logging.info(f"Stored {kind} for '{registrant_id}' "
                 f"({os.path.getsize(destination) / 1024:.0f} KB).")
    return destination


def resolve(registrant_id: str, value: str, kind: str = PASSPORT) -> str:
    """The path to use for an upload, given whatever the client file said.

    A client file may point at a document two ways:
      * an explicit path — used as given (and validated), so an operator can
        keep documents wherever they like, and
      * the sentinel "managed" — look it up in the managed store instead.

    Either way the file is validated before a browser sees it.
    """
    value = (value or "").strip()
    if value.lower() in ("managed", "store", "@store"):
        path = path_for(registrant_id, kind)
        if not path:
            raise WaitlistStepError(
                f"No stored {kind} for '{registrant_id}'. Add one with: "
                f"python -m src.waitlist documents add --registrant "
                f"{registrant_id} --file <path>")
        validate(path, label=f"stored {kind} for '{registrant_id}'")
        return path

    validate(value, label=f"{kind} for '{registrant_id}'")
    return value


def delete_for(registrant_id: str, reason: str = "") -> int:
    """Deletes every document held for a client. Returns how many were removed.

    Called when a registration is confirmed successful — the document has served
    its only purpose and keeping it is pure liability. Plain os.remove: see the
    module docstring on why an overwrite loop would be theatre.
    """
    directory = dir_for(registrant_id)
    if not os.path.isdir(directory):
        return 0

    removed = 0
    try:
        for name in os.listdir(directory):
            target = os.path.join(directory, name)
            if os.path.isfile(target):
                os.remove(target)
                removed += 1
        os.rmdir(directory)
    except OSError as e:
        # Never fail a run over cleanup — but say so loudly, because a document
        # left behind is exactly what the retention sweep exists to catch.
        logging.warning(
            f"Could not fully remove documents for '{registrant_id}': {e}. "
            "The retention sweep will retry on the next run.")

    if removed:
        logging.info(f"Deleted {removed} document(s) for '{registrant_id}'"
                     + (f" — {reason}" if reason else "") + ".")
    return removed


def purge_older_than(days: float, dry_run: bool = False) -> List[str]:
    """Deletes documents older than `days`. Returns the paths removed.

    The backstop that makes "we delete after use" true rather than aspirational:
    a crashed or abandoned run must not leave a passport scan on disk forever.
    Runs at the start of every invocation, so it needs no scheduling of its own.
    """
    base = root()
    if not os.path.isdir(base):
        return []

    cutoff = time.time() - days * 86400
    removed = []
    for client in sorted(os.listdir(base)):
        directory = os.path.join(base, client)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            try:
                if not os.path.isfile(path) or os.path.getmtime(path) >= cutoff:
                    continue
                if not dry_run:
                    os.remove(path)
                removed.append(path)
            except OSError as e:
                logging.warning(f"Could not purge {path}: {e}")
        try:
            if not dry_run and not os.listdir(directory):
                os.rmdir(directory)
        except OSError:
            pass

    if removed:
        verb = "would delete" if dry_run else "deleted"
        logging.info(f"Retention sweep {verb} {len(removed)} document(s) older "
                     f"than {days:g} day(s).")
    return removed


def inventory() -> List[dict]:
    """Every document currently held, for `documents list`.

    Deliberately reports AGE — the operator's question is "what is still on disk
    that should not be", and age is what answers it.
    """
    base = root()
    if not os.path.isdir(base):
        return []

    now = time.time()
    items = []
    for client in sorted(os.listdir(base)):
        directory = os.path.join(base, client)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            items.append({
                "registrant_id": client,
                "kind": os.path.splitext(name)[0],
                "path": path,
                "size_kb": stat.st_size / 1024,
                "age_days": (now - stat.st_mtime) / 86400,
            })
    return items
