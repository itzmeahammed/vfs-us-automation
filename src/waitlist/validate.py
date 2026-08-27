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
from typing import Any, Dict, List, Optional, Tuple

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


def _placeholders(template: str) -> List[Tuple[str, str]]:
    """Every {{placeholder}} in a value template, as (name, first_filter).

    Parsing is DELEGATED to context.py — the module that actually resolves these
    at fill time — rather than matched with a second pattern of our own. A local
    regex here had drifted from it and silently dropped fields: it allowed a bare
    `|filter` only, so `{{passport_expiry|date:%d/%m/%Y}}` (a filter WITH an
    argument) matched nothing at all. required_fields() then omitted the field
    from the form spec, while check_templates() — which uses the real parser —
    still demanded it, so a web app that rendered exactly the advertised fields
    got its client rejected for a field it was never told to ask for.

    Chained filters (`{{x|digits|upper}}`) parse correctly too; only the FIRST
    is returned, since the filter is used solely to pick a human-readable note.
    """
    from src.waitlist import context as ctx

    out: List[Tuple[str, str]] = []
    for token in ctx._PLACEHOLDER_RE.findall(template):
        name, modifiers = ctx._parse_token(token)
        out.append((name, modifiers[0][0] if modifiers else ""))
    return out

# How a route-config widget maps to something a web form can render. The API
# contract is deliberately generic ("text", "select", "file") rather than
# leaking Angular Material widget names to the client.
_WIDGET_KIND = {
    "text": "text",
    "mat-select": "select",
    "select": "select",
    "checkbox": "checkbox",
    "file": "file",
    "date": "date",
}

# Filters that tell you something about the VALUE the portal expects, so the
# web app can validate before submitting rather than after a browser run.
_FILTER_HINT = {
    "upper": "Submitted in UPPER CASE.",
    "lower": "Submitted in lower case.",
    "title": "Submitted in Title Case.",
    "digits": "Digits only — punctuation and spaces are stripped.",
}


def required_fields(route: str) -> List[Dict[str, Any]]:
    """The client-data fields THIS route's form actually asks for.

    This is the answer to "what should my signup form render?". Without it a
    web app has to hardcode a field list, which is wrong per route and silently
    breaks when a portal changes: AE-CHE wants nine fields including address
    lines, AE-NLD wants seven, and AE-ITA wants a passport SCAN. A hardcoded
    form would submit a client that can never register, and the failure would
    only surface minutes into a browser run.

    Derived from the route's own step definitions — the same config the bot
    fills from — so it cannot drift from what the portal is really asked for.

    Returns one entry per distinct field, in the order the form asks for them:

        {"name", "label", "kind", "required", "step", "notes"}

    `required=False` marks a field the portal only shows in some renders
    (`if_present`), so the web app can present it as optional rather than
    blocking signup on it.

    Account credentials are NOT included: they are metadata about which VFS
    login to use, not form data, and the caller supplies them separately.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    try:
        from src.waitlist import config as waitlist_config

        cfg = waitlist_config.get(route)
    except Exception:                                  # noqa: BLE001
        # A route with no usable config has no fields to describe. The caller
        # already reports WHY via route_readiness().problems.
        return out

    for step in cfg.get("steps") or []:
        if step.get("disabled"):
            continue
        for spec in step.get("fields") or []:
            value = spec.get("value")
            if not isinstance(value, str):
                continue          # a literal (e.g. a checkbox `true`) — not client data
            for name, filt in _placeholders(value):
                if name in seen:
                    continue
                seen.add(name)
                notes = []
                if spec.get("if_present"):
                    notes.append("The portal only shows this on some renders.")
                if filt and filt in _FILTER_HINT:
                    notes.append(_FILTER_HINT[filt])
                # The portal sometimes puts SEVERAL inputs under one caption —
                # "Contact number" covers both the country code and the number,
                # split by `index`. Rendering two identically-labelled boxes
                # would be unusable, so fall back to the field name, which is
                # already descriptive.
                label = spec.get("label") or ""
                if spec.get("index") is not None or not label:
                    label = name.replace("_", " ").title()

                options, options_status = _options_of(spec)
                out.append({
                    "name": name,
                    "label": label,
                    "kind": _WIDGET_KIND.get(str(spec.get("widget")), "text"),
                    # if_present fields are optional BY DEFINITION: the form
                    # that would demand them is not always rendered.
                    "required": bool(spec.get("required", True))
                                and not spec.get("if_present"),
                    "step": step.get("name", ""),
                    "notes": " ".join(notes),
                    "options": options,
                    "options_status": options_status,
                    "options_captured_at": spec.get("options_captured_at") or "",
                })
    return out


#: Field kinds whose value must come from a fixed list on the portal. A "select"
#: with no options is the case this whole mechanism exists for: the API used to
#: say "render a dropdown" without saying what goes in it, so a web app either
#: rendered an empty control or hardcoded a guess — and the guess was only found
#: wrong minutes into a browser run, when the option could not be clicked.
_CHOICE_KINDS = {"select"}

#: options_status values, and what a web app should do with each:
#:   "known"       - options[] is a list observed on the real portal. Render a
#:                   dropdown from it; POST /clients rejects anything else.
#:   "unknown"     - this field IS a dropdown but its options have not been
#:                   captured yet. Render free text with a warning; the value
#:                   cannot be checked until someone harvests the list.
#:   "not_a_choice"- a free-text/date/file field. options[] is empty and
#:                   meaningless; render by "kind" as before.
OPTIONS_KNOWN = "known"
OPTIONS_UNKNOWN = "unknown"
OPTIONS_NOT_A_CHOICE = "not_a_choice"


def _options_of(spec: Dict[str, Any]) -> Tuple[List[str], str]:
    """The allowed values for a field spec, and how much we trust that list.

    Returns ([], "not_a_choice") for anything that is not a dropdown, so the
    caller never has to special-case widget names.

    An "options" list in the config is treated as OBSERVED — the harvester
    writes it from the live portal's own overlay, and its sibling
    "options_captured_at" records when. It is deliberately NOT inferred or
    defaulted: a guessed list is worse than an admitted gap, because it makes a
    wrong value look validated.
    """
    kind = _WIDGET_KIND.get(str(spec.get("widget")), "text")
    if kind not in _CHOICE_KINDS:
        return [], OPTIONS_NOT_A_CHOICE

    raw = spec.get("options")
    if not isinstance(raw, list) or not raw:
        return [], OPTIONS_UNKNOWN

    options = [str(o).strip() for o in raw if str(o).strip()]
    if not options:
        return [], OPTIONS_UNKNOWN
    return options, OPTIONS_KNOWN


def check_choices(route: str, data: Dict[str, Any]) -> List[Problem]:
    """Rejects a client value that is not one of a dropdown's observed options.

    This is the point of harvesting the lists. Without it, `"nationality":
    "Lebanese"` sails through creation and only fails deep inside a live run,
    when get_by_role("option", name="Lebanese") matches nothing on a portal
    whose actual entry is "Lebanon" — after a login, a Turnstile solve and a
    committed form step.

    Only fields with a "known" list are checked. A dropdown nobody has
    harvested yet cannot be validated, so it passes through untouched rather
    than blocking on a list we do not have: rejection is earned per field by
    observing the portal, never assumed.
    """
    problems: List[Problem] = []
    for spec in required_fields(route):
        if spec.get("options_status") != OPTIONS_KNOWN:
            continue
        name = spec["name"]
        if name not in data:
            continue                      # absence is check_templates()'s job
        value = str(data.get(name) or "").strip()
        if not value:
            continue
        options = spec["options"]
        if value in options:
            continue

        # Case-only mismatches are the common near-miss ("male" for "Male"), so
        # they get a pointed message rather than the full list.
        near = [o for o in options if o.lower() == value.lower()]
        if near:
            problems.append(Problem(
                field=name,
                message=f"{value!r} must match the portal's option exactly: "
                        f"{near[0]!r}.",
                hint="The portal matches this value exactly, including case.",
            ))
            continue

        problems.append(Problem(
            field=name,
            message=f"{value!r} is not an option for "
                    f"{spec.get('label') or name!r} on {route}.",
            hint=f"Valid options: {'; '.join(options)}",
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

    # Placeholders and dropdown values can only be checked against a waitlist
    # config that loaded.
    if not any(p.field == "route" for p in readiness.problems):
        problems.extend(check_templates(route, data))
        problems.extend(check_choices(route, data))

    return problems
