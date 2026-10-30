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

    # An operator pause sits on top of all of the above: the config may be
    # fine and the route still be stopped for today.
    from src.utils import route_pause
    paused = route_pause.problem(route, route_pause.WAITLIST)
    if paused:
        problems.append(Problem(
            field="route", message=paused[0].upper() + paused[1:] + ".",
            hint=f"Lift it with PATCH /v1/routes/{route}/flows/waitlist "
                 "{\"enabled\": true}.",
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

    The VFS account and its portal password are NOT included: they are metadata
    about which login to use, not form data, and the caller supplies them
    separately. The MAILBOX password is - see `mailbox_password_field`.
    """
    try:
        from src.waitlist import config as waitlist_config

        cfg = waitlist_config.get(route)
    except Exception:                                  # noqa: BLE001
        # A route with no usable config has no fields to describe. The caller
        # already reports WHY via route_readiness().problems.
        return []
    out = _fields_from_steps(cfg.get("steps"), set())
    if not out:
        # A route with no form has no account to read mail for either. An
        # unknown route must report NOTHING, not a lone password box:
        # readiness().problems is what explains why, and a form built from one
        # field would look like a route that almost works.
        return out
    mailbox = mailbox_password_field(route)
    if mailbox:
        out.append(mailbox)
    return out


def mailbox_password_field(route: str) -> Optional[Dict[str, Any]]:
    """The mailbox-password field for a waitlist form. Every route, always.

    *** EVERY ROUTE, NOT JUST THE OTP ONES. ***

    Registering is only half the job. VFS then drives the whole lifecycle by
    email - the "Successfully Added to Waitlist" confirmation, and above all the
    INVITATION that says a slot is bookable for the next 36-48 hours. The inbox
    watcher reads that over IMAP from the account the client registered under,
    and `invite_booking` turns it into a booking. A mailbox nobody can sign in
    to means the invitation is never seen and the registration quietly leads
    nowhere.

    This field was originally scoped to routes with an `email_otp` step, i.e.
    France, because that is where a MID-BOOKING code has to be read. That was
    too narrow: the invitation arrives on every route. On 2026-10-29 an AE-NLD
    client was registered under an account the watcher could not open, and no
    form had ever asked for its password (see `route_needs_mailbox`, which is
    still the right question for the OTP step itself and the wrong one here).

    *** ASKED EVEN WHEN THE CREDENTIALS POOL ALREADY HOLDS ONE. ***

    It briefly skipped the field for an address the pool knows, on the grounds
    that making an agent retype a known value invites the wrong value. That
    hides the one field the whole invitation chain hangs on, and it makes the
    form depend on server-side credential state an agent cannot see: the same
    route renders differently per account, for a reason the page cannot explain.
    So the box is always offered and left OPTIONAL, and whether it HAD to be
    filled is decided at submit by `check_mailbox_password`, which is where the
    account is actually known.
    """
    return _field(
        "account_email_password", "Mailbox password (Roundcube)", "password",
        "account",
        "The password for this account's MAILBOX, not the portal - NOT the "
        "portal password. After registering, the bot signs in to this mailbox "
        "to catch the VFS invitation email, which is what lets it book the "
        "appointment. Without it the registration still lands, but the "
        "invitation is never seen. Can be left blank only for a company "
        "account the bot already holds a mailbox password for.",
        required=False)


#: Placeholders the bot fills from the route and the chosen combo
#: (context.build), so they are never something a client supplies.
_CONTEXT_KEYS = frozenset({
    "route", "source", "dest", "combo", "centre", "category", "sub_category",
    "index", "index1",
})


def _fields_from_steps(steps: Any, seen: set) -> List[Dict[str, Any]]:
    """One entry per client placeholder in `steps`, in form order. `seen` is
    shared by the caller so a field asked for twice is described once."""
    out: List[Dict[str, Any]] = []
    for step in steps or []:
        if step.get("disabled"):
            continue
        for spec in step.get("fields") or []:
            value = spec.get("value")
            if not isinstance(value, str):
                continue          # a literal (e.g. a checkbox `true`) — not client data
            for name, filt in _placeholders(value):
                if name in seen or name in _CONTEXT_KEYS:
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


def _field(name: str, label: str, kind: str, step: str, notes: str,
           required: bool = True) -> Dict[str, Any]:
    return {"name": name, "label": label, "kind": kind, "required": required,
            "step": step, "notes": notes, "options": [],
            "options_status": OPTIONS_NOT_A_CHOICE, "options_captured_at": ""}


def required_fields_live(route: str) -> List[Dict[str, Any]]:
    """What a LIVE-booking client must send for this route, as required_fields()
    is for the waitlist. Three sources, in the order a form should ask:

      the date window     date_from / date_to — nothing else chooses a date
      the booking pages   config/booking/<ROUTE>.json, the pages the bot fills
      the payment page    the gateway's billing fields (config/payment/)

    It is the mirror of booking.requests.precheck(): everything that would
    answer "Required for booking/payment" is advertised here, so a form built
    from this list is one POST /clients accepts.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    try:
        from src.booking import config as booking_config

        # The live entry first, so its labels win; then any other entry the
        # route has, because check_booking_templates() demands those too.
        modes = sorted(booking_config.entry_modes(route),
                       key=lambda m: m != booking_config.ENTRY_NEW)
        if booking_config.ENTRY_NEW not in modes:
            return out               # no live flow here: nothing to describe
        step_fields: List[Dict[str, Any]] = []
        for mode in modes:
            step_fields.extend(
                _fields_from_steps(booking_config.steps_for(route, mode), seen))
    except Exception:                                  # noqa: BLE001
        return out

    window = ("Inclusive, YYYY-MM-DD. The bot books the earliest slot inside "
              "date_from..date_to and nothing outside it.")
    out.append(_field("date_from", "Earliest date", "date", "booking_window", window))
    out.append(_field("date_to", "Latest date", "date", "booking_window",
                      window + " Send the same date twice for a single day."))

    # The account the agent books under, asked for ONLY where the booking
    # itself needs the mailbox: France's flow stops on an emailed OTP that the
    # bot reads from that account's inbox. On every other route the account is
    # optional (the shared pool covers it), so advertising these everywhere
    # would put two password boxes on forms that have no use for them.
    try:
        from src.booking import requests as booking_requests

        if booking_requests.route_needs_mailbox(route):
            domain = booking_requests.booking_account_domain()
            on_domain = f" Must be an @{domain} address." if domain else ""
            out.append(_field(
                "account", "VFS account", "email", "account",
                "The VFS account to book under — the agent's own is fine."
                + on_domain +
                " This route asks for an emailed OTP, so the bot has to sign "
                "in to this address's mailbox to read it."))
            out.append(_field(
                "account_password", "VFS portal password", "password",
                "account",
                "The password for visa.vfsglobal.com. Stored, never returned."))
            out.append(_field(
                "account_email_password", "Email (Roundcube) password",
                "password", "account",
                "The MAILBOX password for the same address — NOT the portal "
                "password above. The bot reads the emailed OTP with it. "
                "Verify it before submitting: a wrong one is only discovered "
                "after the applicant's details have been sent to VFS, and "
                "that attempt is then spent. Stored, never returned."))
    except Exception:                                  # noqa: BLE001
        pass

    out.extend(step_fields)

    try:
        from src.payment import config as payment_config

        billing = payment_config.load().get("billing_fields") or []
    except Exception:                                  # noqa: BLE001
        billing = []
    for spec in billing:
        name = spec.get("value_key") or spec.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        entry = _field(name, name.replace("_", " ").title(), "text", "payment",
                       "Billing detail for the card payment page.",
                       required=bool(spec.get("required")))
        if name == "country_code":
            # NOT ADVERTISED AT ALL. The billing country is fixed to
            # BILLING_COUNTRY by booking.requests.normalise(), so a form that
            # renders this field can only send something that is overwritten —
            # and three different web-app spellings of it reached the live
            # payment page before it was fixed (2026-10-23).
            continue
        out.append(entry)
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


def check_invitation_email(data: Dict[str, Any]) -> List[Problem]:
    """Warn when the form's "email" differs from the VFS account address.

    WHY THIS MATTERS, AND WHY IT IS EASY TO MISS
    --------------------------------------------
    VFS sends the "Slots available for booking" invitation to the EMAIL TYPED
    INTO THE WAITLIST FORM on /your-details — not to the account the
    registration was made under. Those are separate fields in a client file and
    nothing has ever forced them to agree.

    The inbox watcher reads ACCOUNT mailboxes (src/inbox/watcher.py:
    mailbox_accounts). So when they differ, the invitation lands in a mailbox
    nobody is watching, whose password we do not have, and the client's booking
    window closes unnoticed.

    The failure is completely silent: no error, no missing row, no exception —
    just an email that never arrives anywhere we look. That is precisely the
    class of problem worth catching at validation time.

    A WARNING, NOT AN ERROR
    -----------------------
    Deliberately non-blocking. A client may legitimately want confirmations at
    their own address, and existing clients were created before this rule
    existed — 4 of the 5 in this repo currently differ. Refusing them outright
    would break working registrations to enforce a preference. The warning says
    what will happen; the operator decides.

    The alternative — silently rewriting the client's email to the account
    address — was rejected: it changes data the client supplied, and the person
    who typed it deserves to be told rather than overruled.
    """
    problems: List[Problem] = []

    email = str(data.get("email") or "").strip().lower()
    account = str(data.get("account") or "").strip().lower()

    # No account set means the shared [waitlist] account is used, and the client
    # file cannot know which that is. Nothing to compare.
    if not email or not account:
        return problems

    if email != account:
        problems.append(Problem(
            field="email",
            severity=WARNING,
            message=(
                f"The form email ({email}) differs from the VFS account "
                f"({account}). VFS sends the 'slots available' invitation to "
                f"the form email, and the inbox watcher only reads ACCOUNT "
                f"mailboxes — so this client's invitation would arrive "
                f"somewhere nothing is watching."),
            hint=(
                f"Set \"email\" to {account} so the invitation lands in a "
                f"mailbox the watcher reads. Keep them different only if you "
                f"will check {email} by hand — the booking window is 36-48 "
                f"hours."),
        ))

    return problems


#: Pulls the field name out of a resolver diagnostic like
#: "Placeholder {{passport_number}} (in step 'your_details' ...) has no value...".
_PLACEHOLDER_NAME_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)")


def _missing_field_name(unresolved: str) -> str:
    """The bare field name from a placeholder diagnostic, for a short message."""
    match = _PLACEHOLDER_NAME_RE.search(str(unresolved))
    return match.group(1) if match else str(unresolved).strip()


def check_booking_templates(route: str, data: Dict[str, Any],
                            combo: str = "") -> List[Problem]:
    """Does the client's data satisfy every {{placeholder}} a BOOKING needs?

    The waitlist and the booking want different things from a client, and a
    client can legitimately need only one of them:

      waitlist only   VFS has no open slots, so the client registers and waits
                      for an invitation. Name and email are enough.
      booking only    slots are open, so there is nothing to wait for — but the
                      booking form wants passport number and phone, which a
                      waitlist registration never asked for.

    check_templates answers the first question and was the only one asked, so a
    client with no passport number read as fully ready and then failed on page 2
    of 7 — with a live session open and a login spent. This answers the second.

    Reported as WARNINGS, not errors: a waitlist-only client is a legitimate
    client, not a broken one. The warning says what they cannot do yet; the
    operator decides whether that matters.
    """
    problems: List[Problem] = []
    try:
        from src.booking import config as booking_config
        from src.waitlist import context as ctx
        from src.waitlist.register import _all_templates
        from src.waitlist.registrant import Registrant

        if route not in booking_config.configured_routes():
            return problems          # nothing to book on this route yet

        person = Registrant(data.get("_id", "candidate"), dict(data))
        combos = data.get("combos") or []
        # The centre/category/sub-category come from the COMBO, not the client,
        # so they are supplied here rather than reported as missing fields.
        context = ctx.build(
            person, route=route, combo=combo or (combos[0] if combos else ""),
            combo_parts={"centre": "?", "category": "?", "sub_category": "?"})

        seen = set()
        for mode in booking_config.entry_modes(route):
            steps = booking_config.steps_for(route, mode)
            for unresolved in ctx.validate(
                    _all_templates({"steps": steps}), context):
                if unresolved in seen:
                    continue
                seen.add(unresolved)
                # Report the FIELD, not the resolver's full diagnostic: that
                # message ends with a dump of every available key, which is
                # right for an error a developer is debugging and noise in a
                # warning an operator reads down a list of clients.
                name = _missing_field_name(unresolved)
                problems.append(Problem(
                    field=name,
                    message=f"Not bookable yet: no {name}.",
                    hint="Add this field to book for this client. They can "
                         "still join the waitlist without it.",
                    severity=WARNING,
                ))
    except Exception as exc:                       # noqa: BLE001
        problems.append(Problem(
            field="",
            message=f"Could not check booking fields for {route}: {exc}",
            hint="The booking config may be unreadable.",
            severity=WARNING,
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

    # Independent of the route and of any config loading, so it runs before the
    # early return below — a client with a bad route still deserves to be told
    # their invitation would go somewhere unwatched.
    problems.extend(check_invitation_email(data))

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

    # WHAT THE CLIENT CAN DO, not just whether they are valid. A client may need
    # only the waitlist, only a booking, or both, and the two are answered by
    # DIFFERENT configs — so this sits OUTSIDE the waitlist guard above.
    # Nesting it there meant a route whose waitlist is switched off (Norway:
    # slots are open, so there is nothing to wait for) never had its booking
    # fields checked at all — which is exactly the booking-only case.
    problems.extend(check_booking_templates(route, data))

    problems.extend(check_mailbox_password(data))
    problems.extend(check_phone(data))

    return problems


#: The only dialling code served. Every route is AE-*, every centre is in the
#: UAE, and every client reaches VFS on a UAE number - so a number that is not
#: one is a typo, not a foreign client. Serving a second country means extending
#: this rule deliberately rather than discovering it mid-run.
UAE_DIALLING_CODE = "971"
#: Digits in the NUMBER box, with +971 already chosen in the box beside it.
#: 55 602 4553 -> "556024553".
UAE_SUBSCRIBER_DIGITS = 9


def check_phone(data: Dict[str, Any]) -> List[Problem]:
    """Is this a UAE subscriber number, with no country code inside it?

    *** THE PORTAL ASKS FOR THE CODE SEPARATELY. ***

    Every route's form splits "Contact number" into two controls: the dialling
    code, then the number. The bot fills them from `phone_country_code` and
    `phone_number`, so a `+971` carried inside the number is typed in ON TOP of
    the code already chosen - the portal receives 971 and then +9715445792.

    Nothing checked this. On 2026-10-30 request osama-ahammed-ae-fra-b71d1c80
    held `phone_country_code: "971"` with `phone_number: "+9715445792"`, and the
    only reason it was not booked against an unreachable contact number is that
    the run was cancelled for an unrelated reason. Strip the prefix and 7 digits
    remain, so that number was wrong twice over and nothing said so.

    ERRORS, not warnings. The cost of refusing is an agent retyping a field
    while they are still looking at the form; the cost of allowing it is an
    appointment booked - and paid for - against a number VFS cannot reach, which
    no amount of later correction undoes.
    """
    number = str(data.get("phone_number") or "").strip()
    code = str(data.get("phone_country_code") or "").strip().lstrip("+")

    problems: List[Problem] = []
    if code and code != UAE_DIALLING_CODE:
        problems.append(Problem(
            field="phone_country_code",
            message=(f"\"{code}\" is not a UAE dialling code. Only "
                     f"+{UAE_DIALLING_CODE} numbers can be used."),
            hint="Every centre is in the UAE and VFS calls a UAE number."))

    if not number:
        # Whether the field is REQUIRED is asked by the form definition; this
        # judges a value that is there. Otherwise every route with no phone
        # field would report a phone problem.
        return problems

    compact = re.sub(r"[\s()\-.]", "", number)

    if compact.startswith("+"):
        problems.append(Problem(
            field="phone_number",
            message=(f"\"{number}\" starts with '+'. The number box takes the "
                     "subscriber number only - the country code is chosen in "
                     "the box beside it."),
            hint=(f"Put the code in \"phone_country_code\" (currently "
                  f"\"{code or 'empty'}\") and the 9 digits here: "
                  f"+971 55 602 4553 is code 971, number 556024553.")))
        return problems

    if not compact.isdigit():
        problems.append(Problem(
            field="phone_number",
            message=(f"\"{number}\" is not a plain number. Digits only - the "
                     "bot types this straight into the portal."),
            hint="Spaces, brackets, dashes and dots are fine; letters are not."))
        return problems

    if compact.startswith(UAE_DIALLING_CODE) and len(compact) > UAE_SUBSCRIBER_DIGITS:
        # Only when it cannot be a real number on its own: a UAE mobile never
        # starts 971, so this is unambiguous rather than a guess about intent.
        problems.append(Problem(
            field="phone_number",
            message=(f"\"{number}\" repeats the country code "
                     f"{UAE_DIALLING_CODE}, which is already selected in the "
                     "box before it."),
            hint=f"This field should be \"{compact[len(UAE_DIALLING_CODE):]}\"."))
        compact = compact[len(UAE_DIALLING_CODE):]   # judge the length on what is left

    if len(compact) != UAE_SUBSCRIBER_DIGITS:
        problems.append(Problem(
            field="phone_number",
            message=(f"\"{compact}\" is {len(compact)} digit"
                     f"{'' if len(compact) == 1 else 's'}; a UAE number is "
                     f"{UAE_SUBSCRIBER_DIGITS}."),
            hint="With +971 chosen beside it, 55 602 4553 is \"556024553\"."))

    return problems


def check_mailbox_password(data: Dict[str, Any]) -> List[Problem]:
    """Can the bot open the mailbox this client will register under?

    Registering is only half the job: VFS sends the INVITATION by email, the
    inbox watcher reads it over IMAP, and `invite_booking` turns it into a
    booking. An account nobody can sign in to means the invitation is never
    seen and the registration leads nowhere - silently, weeks later, with the
    client's window already closed.

    Two sources satisfy it, in the order `inbox.watcher.mailbox_accounts` tries
    them: the client's own `account_email_password`, else the credentials
    pool's `email_password` for the same address. A company account needs
    nothing typed; an agent using their own account has to supply it.

    *** NEVER the VFS portal password. *** They were one value until
    2026-10-16 and assuming they still match is a bug, not a shortcut.

    Checked HERE rather than in the form list, because the form is rendered
    before anyone has chosen an account - `mailbox_password_field` offers the
    box, this decides whether it had to be filled.
    """
    account = str(data.get("account") or "").strip()
    if not account:
        # No pinned account: the shared pool supplies both the login and its
        # mailbox password, so there is nothing for an agent to get wrong.
        return []
    if str(data.get("account_email_password") or "").strip():
        return []

    try:
        from src.utils import credentials as cred_mod
        if cred_mod.email_password_for(account, required=False):
            return []
    except Exception:                                       # noqa: BLE001
        # Treat an unreadable pool as "no password there": demanding the field
        # is recoverable, assuming one exists is not.
        pass

    # WARNING, not ERROR, and the distinction is the whole design.
    #
    # An error blocks the write, and a blocked write means the client is never
    # registered at all. A waitlist PLACE is worth more than the automation on
    # top of it: an agent who does not have the Roundcube password to hand must
    # still be able to take the place now and have a human watch the mailbox.
    # Registered-but-unwatched is a worse outcome than registered-and-watched
    # and a better one than not registered, so this says so and does not refuse.
    #
    # It is surfaced three times over, because it is easy to ignore once: here
    # in the client's `problems`, in the runner's log before a live submit, and
    # in inbox.watcher.mailbox_accounts when the pass skips the mailbox.
    return [Problem(
        field="account_email_password",
        severity=WARNING,
        message=(f"{account} has no MAILBOX password, so the bot cannot read "
                 "the VFS invitation email for this client - it will be "
                 "registered, but the invitation has to be caught by hand."),
        hint=("This is the Roundcube/IMAP password for the account's mailbox "
              "- NOT the VFS portal password. VFS announces a bookable slot by "
              "email and the bot reads that mailbox to catch it, so without "
              "this the client is registered but the invitation is never "
              "seen."))]
