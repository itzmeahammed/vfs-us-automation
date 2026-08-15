"""Client profiles — ONE file per client, in config/registrants/<id>.json.

Each file is completely self-contained: it says WHICH route and WHICH
combinations the client is waiting on, plus the data to type into the form. So
adding a client is exactly one new file, and removing one is exactly one
deletion — there is no second folder to keep in sync.

    {
      "route": "AE-CHE",                      <- one route per file
      "enabled": true,
      "combos": ["Dubai - SCHENGEN"],

      "first_name": "Ahmed",                  <- data the form needs
      "passport_number": "A1234567",
      ...
    }

ONE FILE = ONE ROUTE, deliberately. A client who wants two countries gets two
files (ahmed-che.json, ahmed-ita.json). The cost is that their passport number
then lives in two places — keep them in step if you edit one.

The hard rule this module enforces:

    a client file contains DATA and TARGETING — never SELECTORS.

Where things are on the page lives in config/waitlist/<ROUTE>.json. The two are
joined by field NAME via {{placeholders}} (see context.py), which is what lets
one route config serve every client.

These files hold PII (passport number, date of birth, phone, address) so they are
gitignored; only the .example template is committed.
"""

import glob
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from src.waitlist.errors import WaitlistConfigError

REGISTRANT_DIR = os.path.join("config", "registrants")

#: Keys that control TARGETING rather than being form data. They are stripped
#: from the data context, so they can never be typed into a field by accident.
#: "account"/"account_password" are the VFS LOGIN, never form content — keeping
#: them out of the context also stops a password reaching a page field.
_META_KEYS = ("route", "combos", "enabled", "id", "notes",
              "account", "account_password", "proxy")

#: Fields whose values must never reach a log file or a Telegram message.
SENSITIVE_FIELDS = (
    "passport_number",
    "date_of_birth",
    "dob",
    "email",
    "phone_number",
    "national_id",
    "address_line_1",
    "address_line_2",
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ROUTE_RE = re.compile(r"^[A-Z]{2}-[A-Z]{2,4}$")

_MISSING = object()


class Registrant:
    """One client: their route, their combinations, and their form data."""

    def __init__(self, registrant_id: str, data: Dict[str, Any]):
        self.id = registrant_id

        self.route = str(data.get("route", "")).strip().upper()
        self.enabled = bool(data.get("enabled", True))
        self.combos = [str(c).strip() for c in (data.get("combos") or []) if str(c).strip()]
        self.notes = data.get("notes", "")

        # The VFS account this client's waitlist entries live under. Optional —
        # falls back to [waitlist] account. See accounts.resolve() for the full
        # order and for why the hourly rotation is deliberately not used.
        self.account = str(data.get("account", "")).strip()
        self.account_password = str(data.get("account_password", "")).strip()

        # Optional exit-IP pin for this client, e.g. "user:pass@host:port".
        # Omit to let accounts.resolve_proxy() pick from config/proxylist.txt.
        self.proxy = str(data.get("proxy", "")).strip()

        # Only non-meta keys become form data, so {{route}} always means the
        # run's route and can never be shadowed by this file.
        self.data = {k: v for k, v in (data or {}).items() if k not in _META_KEYS}
        self._flat = _flatten(self.data)

    # -- access ------------------------------------------------------------ #

    def get(self, key: str, default: Any = None) -> Any:
        """Look up a field by dotted path, with underscore/dot equivalence."""
        if key in self._flat:
            return self._flat[key]
        for alt in (key.replace(".", "_"), key.replace("_", ".")):
            if alt in self._flat:
                return self._flat[alt]
        return default

    def has(self, key: str) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def keys(self) -> List[str]:
        """Every addressable field name (dotted), for error messages."""
        return sorted(self._flat)

    def as_context(self) -> Dict[str, Any]:
        """The flat mapping used to resolve {{placeholders}}."""
        return dict(self._flat)

    def wants(self, combo: str) -> bool:
        """True if this client is waiting on `combo` (case/space-insensitive)."""
        want = _normalise(combo)
        return any(_normalise(c) == want for c in self.combos)

    # -- display ----------------------------------------------------------- #

    def label(self) -> str:
        """A safe, non-PII display name for logs and Telegram."""
        name = f"{self.get('first_name') or ''} {self.get('last_name') or ''}".strip()
        return f"{self.id} ({name})" if name else self.id

    def __repr__(self) -> str:  # never dump PII into a traceback
        return (f"<Registrant {self.id} route={self.route} "
                f"combos={len(self.combos)} fields={len(self._flat)}>")


def _normalise(text: str) -> str:
    """Collapses whitespace runs and lowercases — matches journal._normalise, so
    a label lands on the same identity everywhere."""
    return " ".join((text or "").split()).lower()


def _flatten(data: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """Flattens one level of nesting into dotted keys, keeping scalars as-is."""
    out: Dict[str, Any] = {}
    for key, value in (data or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, prefix=f"{path}."))
        else:
            out[path] = value
    return out


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #

def _validate(registrant_id: str, data: Dict[str, Any]) -> None:
    """Shape checks. Raises WaitlistConfigError with an actionable message."""
    if not isinstance(data, dict):
        raise WaitlistConfigError(
            f"Client '{registrant_id}': file must contain a JSON object.")

    # --- targeting ---
    # The list check comes FIRST: "route": ["AE-CHE", "AE-ITA"] is the natural
    # mistake to make, and it deserves the message that explains the one-file-
    # one-route rule rather than a generic "not a valid route key".
    if isinstance(data.get("route"), (list, tuple)):
        raise WaitlistConfigError(
            f"Client '{registrant_id}': \"route\" must be a single route, not a "
            "list — one file targets ONE route. For a second country create a "
            f"second file (e.g. {registrant_id}-ita.json). Note that duplicates "
            "their passport number, so keep both in step if you edit one.")

    route = str(data.get("route", "")).strip().upper()
    if not route:
        raise WaitlistConfigError(
            f"Client '{registrant_id}': missing \"route\". Each client file "
            "targets exactly ONE route, e.g. \"route\": \"AE-CHE\".")
    if not _ROUTE_RE.match(route):
        raise WaitlistConfigError(
            f"Client '{registrant_id}': \"route\": \"{route}\" is not a valid "
            "route key. Use the form 'AE-CHE' (as in config/vfs_urls.ini).")

    combos = data.get("combos")
    if not combos:
        raise WaitlistConfigError(
            f"Client '{registrant_id}': missing \"combos\". List the "
            "combination label(s) this client is waiting on, exactly as they "
            f"appear in config/routes/{route}.json.")
    if not isinstance(combos, list):
        raise WaitlistConfigError(
            f"Client '{registrant_id}': \"combos\" must be a list of "
            "combination labels.")
    for combo in combos:
        if not isinstance(combo, str) or not combo.strip():
            raise WaitlistConfigError(
                f"Client '{registrant_id}': every entry in \"combos\" must be a "
                "non-empty combination label.")
    seen = set()
    for combo in combos:
        key = _normalise(combo)
        if key in seen:
            raise WaitlistConfigError(
                f"Client '{registrant_id}': \"{combo}\" is listed twice in "
                "\"combos\".")
        seen.add(key)

    # --- account pin (optional, but all-or-nothing) ---
    # Waitlist accounts are a separate pool from config/credentials.local.ini,
    # so a pinned account MUST carry its own password — there is nowhere else to
    # look it up.
    account = data.get("account")
    password = data.get("account_password")
    if account is not None:
        if not isinstance(account, str) or "@" not in account:
            raise WaitlistConfigError(
                f"Client '{registrant_id}': \"account\" must be the VFS login "
                f"email address; got {account!r}.")
        if not password or not str(password).strip():
            raise WaitlistConfigError(
                f"Client '{registrant_id}': \"account\" is set but "
                "\"account_password\" is missing. Waitlist accounts are separate "
                "from config/credentials.local.ini, so the password must be "
                "given here. Omit BOTH keys to use the shared "
                "[waitlist] account instead.")
    elif password is not None:
        raise WaitlistConfigError(
            f"Client '{registrant_id}': \"account_password\" is set but "
            "\"account\" is not. Add the account email, or drop the password.")

    # --- form data ---
    payload = {k: v for k, v in data.items() if k not in _META_KEYS}
    if not payload:
        raise WaitlistConfigError(
            f"Client '{registrant_id}': no form data. Add the fields the route's "
            "form needs (first_name, passport_number, ...).")

    for key, value in _flatten(payload).items():
        if isinstance(value, (list, tuple)):
            raise WaitlistConfigError(
                f"Client '{registrant_id}': field '{key}' is a list. Form fields "
                "must be single values — use separate fields (e.g. "
                "address_line_1 / address_line_2) instead.")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise WaitlistConfigError(
                f"Client '{registrant_id}': field '{key}' has unsupported type "
                f"{type(value).__name__}. Use text, numbers or true/false.")
        # A selector in a client file means the data/structure split has been
        # broken — catch it loudly rather than letting it half-work.
        if isinstance(value, str) and re.search(
                r"formcontrolname|mat-select|css=|xpath=|<[a-z]+ ", value, re.I):
            raise WaitlistConfigError(
                f"Client '{registrant_id}': field '{key}' looks like a page "
                "SELECTOR. Client files hold data only — selectors belong in "
                f"config/waitlist/{route}.json.")


# --------------------------------------------------------------------------- #
# Loading                                                                      #
# --------------------------------------------------------------------------- #

def path_for(registrant_id: str) -> str:
    return os.path.join(REGISTRANT_DIR, f"{registrant_id}.json")


def load(registrant_id: str) -> Registrant:
    """Loads and validates one client file by id (the filename stem)."""
    registrant_id = (registrant_id or "").strip()
    if not _ID_RE.match(registrant_id):
        raise WaitlistConfigError(
            f"Invalid client id '{registrant_id}' — use lowercase letters, "
            "digits, '-' and '_' only (it must match the filename).")

    path = path_for(registrant_id)
    if not os.path.isfile(path):
        available = ", ".join(available_ids()) or "none found"
        raise WaitlistConfigError(
            f"No client file at {path}. Available: {available}.")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise WaitlistConfigError(f"Could not read {path}: {e}") from e

    data.pop("id", None)  # the filename is the id; ignore any in-file duplicate
    _validate(registrant_id, data)
    person = Registrant(registrant_id, data)

    # Register this client's PII for log scrubbing IMMEDIATELY — before any
    # caller can log a field value or a library can echo one back in an error.
    # Doing it here rather than at each call site means no loader can forget.
    try:
        from src.waitlist import redaction
        redaction.register(person)
    except Exception as e:                                   # never block a load
        logging.debug(f"Could not register redaction values: {e}")

    logging.debug(
        f"Loaded client '{registrant_id}': route={person.route}, "
        f"{len(person.combos)} combo(s), {len(person.keys())} field(s)."
    )
    return person


def available_ids() -> List[str]:
    """Every client id with a file on disk (examples and _private excluded)."""
    if not os.path.isdir(REGISTRANT_DIR):
        return []
    ids = []
    for path in sorted(glob.glob(os.path.join(REGISTRANT_DIR, "*.json"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if not name.startswith("_"):
            ids.append(name)
    return ids


def load_all(skip_invalid: bool = False) -> List[Registrant]:
    """Every client file on disk.

    skip_invalid=True logs and skips a malformed file rather than raising — used
    by `status`, so one bad file cannot hide the whole roster. The run path
    leaves it False: a broken client file must stop a run, not be silently
    ignored.
    """
    people = []
    for registrant_id in available_ids():
        try:
            people.append(load(registrant_id))
        except WaitlistConfigError as e:
            if not skip_invalid:
                raise
            logging.error(f"Skipping invalid client file: {e}")
    return people


def for_route(route: str, include_disabled: bool = False) -> List[Registrant]:
    """
    Every client waiting on `route`, in filename order.

    This is how a run finds its work: there is no separate targets file, so the
    roster IS the set of client files whose "route" matches.
    """
    want = (route or "").strip().upper()
    people = [p for p in load_all() if p.route == want]
    if include_disabled:
        return people
    return [p for p in people if p.enabled]


def redaction_values(registrant: Registrant) -> List[str]:
    """The literal values that must be scrubbed from logs for this client.

    Passed to the logging filter so a passport number can never appear in
    app.log, the daily archive, or a Telegram error message.
    """
    values = []
    for field in SENSITIVE_FIELDS:
        value = registrant.get(field)
        if isinstance(value, (str, int)) and len(str(value)) >= 4:
            values.append(str(value))
    return values
