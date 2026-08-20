"""Browser-free, structured validation of a client payload.

WHY THIS EXISTS
---------------
`registrant._validate()` and `__main__._check_client()` already know how to
validate a client, but neither shape suits an API:

  * `_validate` RAISES on the first problem. A web form wants every field error
    at once, not a game of whack-a-mole where each save reveals one more.
  * `_check_client` PRINTS to stdout and returns a count. An API needs the
    problems themselves, with enough structure to attach each one to a form
    field.

So this module re-expresses the same rules as pure functions returning
`Problem` records. The checks are deliberately kept equivalent to the CLI's —
if the two ever disagree, `python -m src.waitlist check` is the source of truth
and this file is the bug.

Nothing here launches a browser or touches the network, so it is safe to call
inside a request handler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.waitlist.errors import WaitlistConfigError

# Severity levels.
ERROR = "error"      # the client cannot run until this is fixed
WARNING = "warning"  # the client can run, but this is probably not intended


@dataclass
class Problem:
    """One validation finding, addressable to a form field where possible."""

    field: str            # payload key ("route", "combos", "passport_number"), or ""
    message: str          # human-readable, actionable
    severity: str = ERROR
    hint: str = ""        # optional follow-up (e.g. the list of valid combos)

    def to_dict(self) -> Dict[str, Any]:
        out = {"field": self.field, "message": self.message,
               "severity": self.severity}
        if self.hint:
            out["hint"] = self.hint
        return out


@dataclass
class Readiness:
    """Whether a ROUTE can accept waitlist registrations at all.

    Distinct from client validation: a perfectly valid client is still unusable
    if their route has no waitlist config, or it is disabled. Reported
    separately so the API can say "your data is fine, this route is not live"
    rather than blaming the payload.
    """

    route: str
    ready: bool
    problems: List[Problem] = field(default_factory=list)
    combos: List[str] = field(default_factory=list)   # valid combo labels

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route": self.route,
            "ready": self.ready,
            "problems": [p.to_dict() for p in self.problems],
            "combos": self.combos,
        }


# Keys that are metadata/targeting rather than form data. Mirrors
# registrant._META_KEYS; re-derived here so this module has no import cycle.
_META_KEYS = frozenset({
    "route", "combos", "enabled", "account", "account_password", "proxy",
})

_ROUTE_RE = re.compile(r"^[A-Z]{2}-[A-Z]{2,4}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# A value that looks like a page selector means the data/structure split has
# been broken (selectors belong in config/waitlist/<ROUTE>.json).
_SELECTOR_RE = re.compile(r"formcontrolname|mat-select|css=|xpath=|<[a-z]+ ", re.I)


def _normalise(label: str) -> str:
    """Collapse whitespace and case, as registrant._normalise does."""
    return " ".join(str(label).split()).strip().lower()


# --------------------------------------------------------------------------- #
# Route readiness                                                              #
# --------------------------------------------------------------------------- #


def route_readiness(route: str) -> Readiness:
    """Can `route` accept waitlist registrations right now?

    Checks the five conditions that must ALL hold, and reports each failure
    separately so the caller learns which one to fix:

      1. a login URL in config/vfs_urls.ini
      2. config/routes/<ROUTE>.json exists, with combinations
      3. config/waitlist/<ROUTE>.json exists and parses
      4. that config has "enabled": true
      5. it declares exactly one committing step

    Never raises — a broken config is reported, not thrown.
    """
    route = str(route or "").strip().upper()
    problems: List[Problem] = []
    combos: List[str] = []

    if not _ROUTE_RE.match(route):
        return Readiness(route=route, ready=False, problems=[Problem(
            field="route",
            message=f"'{route}' is not a valid route key. Use the form 'AE-CHE'.",
        )])

    # 1. login URL
    from src.utils.config_reader import get_config_value
    if not get_config_value("vfs-url", route, ""):
        problems.append(Problem(
            field="route",
            message=f"No login URL for {route} in config/vfs_urls.ini "
                    "(the route is commented out or absent).",
            hint="Uncomment the route there to make it live.",
        ))

    # 2. slot-check combinations
    source, _, dest = route.partition("-")
    try:
        from src.utils.route_schema import get_route_schema
        from src.vfs_bot.slot_check import combo_label
        schema = get_route_schema(source, dest) or {}
        combinations = (schema.get("slot_check") or {}).get("combinations") or []
        combos = [combo_label(c) for c in combinations if not c.get("disabled")]
        if not combos:
            problems.append(Problem(
                field="route",
                message=f"config/routes/{route}.json defines no enabled "
                        "combinations, so there is nothing to wait for.",
            ))
    except Exception as exc:                       # noqa: BLE001 — report, don't crash
        problems.append(Problem(
            field="route",
            message=f"Could not read config/routes/{route}.json: {exc}",
        ))

    # 3-5. waitlist page config
    try:
        from src.waitlist import config as waitlist_config
        waitlist_config.get(route)                 # raises on a bad/missing config
        if not waitlist_config.is_enabled(route):
            problems.append(Problem(
                field="route",
                message=f"config/waitlist/{route}.json has \"enabled\": false — "
                        "registration is switched off for this route.",
                hint="Set it to true once the route's page mapping is trusted.",
            ))
    except WaitlistConfigError as exc:
        problems.append(Problem(
            field="route",
            message=f"Waitlist config problem for {route}: {exc}",
        ))
    except Exception as exc:                       # noqa: BLE001
        problems.append(Problem(
            field="route",
            message=f"Could not read config/waitlist/{route}.json: {exc}",
        ))

    return Readiness(
        route=route,
        ready=not any(p.severity == ERROR for p in problems),
        problems=problems,
        combos=combos,
    )


# --------------------------------------------------------------------------- #
# Client payload validation                                                    #
# --------------------------------------------------------------------------- #


def validate_payload(registrant_id: str, data: Dict[str, Any]) -> List[Problem]:
    """Shape-check a client payload. Returns EVERY problem found.

    Mirrors `registrant._validate`, but accumulates instead of raising, so a web
    form can show all field errors in one pass. Pure — no file or network I/O.
    """
    problems: List[Problem] = []

    if not isinstance(data, dict):
        return [Problem(field="", message="Payload must be a JSON object.")]

    # --- id -------------------------------------------------------------
    if not registrant_id or not _ID_RE.match(str(registrant_id)):
        problems.append(Problem(
            field="client_id",
            message=f"Client id {registrant_id!r} must be lowercase letters, "
                    "digits, underscore or hyphen, starting with a letter or "
                    "digit — it becomes the filename.",
        ))

    # --- route ----------------------------------------------------------
    raw_route = data.get("route")
    if isinstance(raw_route, (list, tuple)):
        problems.append(Problem(
            field="route",
            message="\"route\" must be a single route, not a list — one client "
                    "file targets ONE route.",
            hint="For a second country, create a second client "
                 "(e.g. <id>-ita). Note that duplicates their passport number.",
        ))
        raw_route = None

    route = str(raw_route or "").strip().upper()
    if not route:
        problems.append(Problem(
            field="route",
            message="Missing \"route\". Each client targets exactly one route, "
                    "e.g. \"AE-CHE\".",
        ))
    elif not _ROUTE_RE.match(route):
        problems.append(Problem(
            field="route",
            message=f"\"{route}\" is not a valid route key. Use the form "
                    "'AE-CHE' (as in config/vfs_urls.ini).",
        ))

    # --- combos ---------------------------------------------------------
    combos = data.get("combos")
    if not combos:
        problems.append(Problem(
            field="combos",
            message="Missing \"combos\". List the combination label(s) this "
                    "client is waiting on, exactly as they appear in "
                    f"config/routes/{route or '<ROUTE>'}.json.",
        ))
    elif not isinstance(combos, list):
        problems.append(Problem(
            field="combos",
            message="\"combos\" must be a list of combination labels.",
        ))
    else:
        seen = set()
        for combo in combos:
            if not isinstance(combo, str) or not combo.strip():
                problems.append(Problem(
                    field="combos",
                    message="Every entry in \"combos\" must be a non-empty "
                            "combination label.",
                ))
                continue
            key = _normalise(combo)
            if key in seen:
                problems.append(Problem(
                    field="combos",
                    message=f"\"{combo}\" is listed twice in \"combos\".",
                ))
            seen.add(key)

    # --- account pin (optional, all-or-nothing) -------------------------
    account = data.get("account")
    password = data.get("account_password")
    if account is not None:
        if not isinstance(account, str) or "@" not in account:
            problems.append(Problem(
                field="account",
                message="\"account\" must be the VFS login email address; "
                        f"got {account!r}.",
            ))
        if not password or not str(password).strip():
            problems.append(Problem(
                field="account_password",
                message="\"account\" is set but \"account_password\" is missing. "
                        "Waitlist accounts are a separate pool from "
                        "config/credentials.local.ini, so the password must be "
                        "supplied with the account.",
                hint="Omit BOTH keys to use the shared [waitlist] account.",
            ))
    elif password is not None:
        problems.append(Problem(
            field="account",
            message="\"account_password\" is set but \"account\" is not. Add the "
                    "account email, or drop the password.",
        ))

    # --- form data ------------------------------------------------------
    payload = {k: v for k, v in data.items()
               if k not in _META_KEYS and not k.startswith("_")}
    if not payload:
        problems.append(Problem(
            field="",
            message="No form data. Add the fields the route's form needs "
                    "(first_name, passport_number, ...).",
        ))

    for key, value in payload.items():
        if isinstance(value, (list, tuple)):
            problems.append(Problem(
                field=key,
                message=f"Field '{key}' is a list. Form fields must be single "
                        "values — use separate fields (e.g. address_line_1 / "
                        "address_line_2) instead.",
            ))
            continue
        if isinstance(value, dict):
            problems.append(Problem(
                field=key,
                message=f"Field '{key}' is an object. Form fields must be "
                        "single values.",
            ))
            continue
        if value is not None and not isinstance(value, (str, int, float, bool)):
            problems.append(Problem(
                field=key,
                message=f"Field '{key}' has unsupported type "
                        f"{type(value).__name__}. Use text, numbers or true/false.",
            ))
            continue
        if isinstance(value, str) and _SELECTOR_RE.search(value):
            problems.append(Problem(
                field=key,
                message=f"Field '{key}' looks like a page SELECTOR. Client data "
                        "holds values only — selectors belong in "
                        f"config/waitlist/{route or '<ROUTE>'}.json.",
            ))

    return problems


def check_combos(route: str, combos: List[str],
                 known: Optional[List[str]] = None) -> List[Problem]:
    """Every combo must exist in config/routes/<ROUTE>.json.

    Without this the run cannot select the dropdowns, and the client waits
    forever for a combination that does not exist.
    """
    problems: List[Problem] = []
    if known is None:
        known = route_readiness(route).combos
    known_lower = {_normalise(k) for k in known}

    for combo in combos or []:
        if not isinstance(combo, str):
            continue
        if _normalise(combo) not in known_lower:
            problems.append(Problem(
                field="combos",
                message=f"\"{combo}\" is not a combination of "
                        f"config/routes/{route}.json.",
                hint=f"Available: {'; '.join(known) or 'none'}",
            ))
    return problems


def check_templates(route: str, data: Dict[str, Any],
                    combo: str = "") -> List[Problem]:
    """Does the client's data satisfy every {{placeholder}} the route needs?

    This is what catches "the form asks for date_of_birth and this client has
    none" BEFORE a browser is ever launched.
    """
    problems: List[Problem] = []
    try:
        from src.waitlist import config as waitlist_config
        from src.waitlist import context as ctx
        from src.waitlist.register import _all_templates
        from src.waitlist.registrant import Registrant

        cfg = waitlist_config.get(route)
        person = Registrant(data.get("_id", "candidate"), dict(data))
        combos = data.get("combos") or []
        context = ctx.build(person, route=route,
                            combo=combo or (combos[0] if combos else ""))
        for unresolved in ctx.validate(_all_templates(cfg), context):
            problems.append(Problem(
                field="",
                message=f"Unresolved placeholder: {unresolved}",
                hint="Add the missing field to this client's data.",
            ))
    except WaitlistConfigError as exc:
        problems.append(Problem(field="route", message=str(exc)))
    except Exception as exc:                       # noqa: BLE001
        problems.append(Problem(
            field="",
            message=f"Could not check placeholders for {route}: {exc}",
        ))
    return problems


def precheck_client(registrant_id: str, data: Dict[str, Any]) -> List[Problem]:
    """Full browser-free pre-flight for one client payload.

    Runs, in order: shape validation, route readiness, combo existence, and
    placeholder resolution. Later stages are skipped when an earlier one has
    already made them meaningless (e.g. no point checking combos against a
    route that does not exist).

    Returns every problem found. An empty list means this client would run.
    """
    problems = validate_payload(registrant_id, data)

    route = str(data.get("route") or "").strip().upper()
    if not route or not _ROUTE_RE.match(route):
        return problems                      # nothing further is meaningful

    readiness = route_readiness(route)
    problems.extend(readiness.problems)

    combos = data.get("combos")
    if isinstance(combos, list) and readiness.combos:
        problems.extend(check_combos(route, combos, known=readiness.combos))

    # Placeholders can only be checked against a waitlist config that loaded.
    if not any(p.field == "route" for p in readiness.problems):
        problems.extend(check_templates(route, data))

    return problems
