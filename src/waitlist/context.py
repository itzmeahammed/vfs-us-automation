"""{{placeholder}} resolution — the join between route config and registrant data.

This module is the entire contract between the two config folders:

    config/waitlist/<ROUTE>.json   says WHERE:  "selector": "input[formcontrolname='passportNumber']"
                                   and WHAT:    "value": "{{passport_number}}"
    config/registrants/<id>.json   supplies:    "passport_number": "A1234567"

Route files therefore never contain personal data, and registrant files never
contain selectors. Adding a country is a new file in config/waitlist/; adding a
client is a new file in config/registrants/. Neither touches Python.

Deliberately a PURE module — no `page`, no I/O, no globals — so every resolution
rule is unit-testable without Playwright (the same discipline slot_check.py
applies to cascade_steps()).

Available namespaces inside {{ }}:

    {{first_name}}            registrant field (bare name — the common case)
    {{registrant.first_name}} the same, explicit
    {{route}} {{source}} {{dest}}   'AE-CHE' / 'AE' / 'CHE'
    {{combo}} {{centre}} {{category}} {{sub_category}}
    {{today}} {{today+30d}}   dates, ISO by default
    {{index}} {{index1}}      applicant number within a repeating block (0- / 1-based)

Modifiers, applied left to right after a '|':

    {{first_name|upper}}      UPPER / lower / title case
    {{dob|date:%d/%m/%Y}}     reformat an ISO date for the portal's expected format
    {{phone_number|digits}}   strip everything but digits
    {{middle_name|default:-}} substitute when the field is missing or empty
"""

import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Tuple

from src.waitlist.errors import WaitlistConfigError

#: {{ name | modifier:arg | modifier }}
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

#: {{today}} / {{today+30d}} / {{today-7d}}
_TODAY_RE = re.compile(r"^today(?:\s*([+-])\s*(\d+)\s*([dwmy]))?$", re.I)

_MISSING = object()


# --------------------------------------------------------------------------- #
# Building the context                                                         #
# --------------------------------------------------------------------------- #

def build(registrant, route: str = "", combo: str = "",
          combo_parts: Dict[str, Any] = None, index: int = 0,
          extra: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Assembles the flat mapping that {{placeholders}} resolve against.

    Registrant fields are exposed BOTH bare ({{first_name}}) and namespaced
    ({{registrant.first_name}}); the namespaced form wins on a collision with a
    route/combo key, so a registrant with a field called 'route' can still be
    addressed unambiguously.
    """
    source, _, dest = (route or "").partition("-")
    parts = combo_parts or {}

    context: Dict[str, Any] = {}
    if registrant is not None:
        fields = registrant.as_context()
        context.update(fields)
        for key, value in fields.items():
            context[f"registrant.{key}"] = value
        context["registrant.id"] = registrant.id

    context.update({
        "route": route,
        "source": source,
        "dest": dest,
        "combo": combo,
        "centre": parts.get("centre", ""),
        "category": parts.get("category", ""),
        "sub_category": parts.get("sub_category", ""),
        "index": index,
        "index1": index + 1,
    })
    if extra:
        context.update(extra)
    return context


# --------------------------------------------------------------------------- #
# Modifiers                                                                    #
# --------------------------------------------------------------------------- #

def _mod_date(value: Any, arg: str) -> str:
    """Reformats an ISO-ish date into the portal's expected format."""
    if value in (None, ""):
        return ""
    if isinstance(value, (date, datetime)):
        parsed = value
    else:
        text = str(value).strip()
        parsed = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise WaitlistConfigError(
                f"Could not read '{value}' as a date. Use ISO format "
                "(YYYY-MM-DD) in registrant files."
            )
    return parsed.strftime(arg or "%d/%m/%Y")


_MODIFIERS = {
    "upper": lambda v, a: str(v).upper(),
    "lower": lambda v, a: str(v).lower(),
    "title": lambda v, a: str(v).title(),
    "strip": lambda v, a: str(v).strip(),
    "digits": lambda v, a: re.sub(r"\D", "", str(v)),
    "date": _mod_date,
    "default": lambda v, a: (a or "") if v in (None, "") else v,
    # Zero-pad a number, e.g. {{index1|pad:2}} -> '01'
    "pad": lambda v, a: str(v).rjust(int(a or 2), "0"),
}


def _apply_modifiers(value: Any, modifiers: List[Tuple[str, str]], token: str) -> Any:
    for name, arg in modifiers:
        func = _MODIFIERS.get(name)
        if func is None:
            raise WaitlistConfigError(
                f"Unknown modifier '{name}' in {{{{{token}}}}}. Available: "
                + ", ".join(sorted(_MODIFIERS))
            )
        value = func(value, arg)
    return value


def _parse_token(token: str) -> Tuple[str, List[Tuple[str, str]]]:
    """'dob|date:%d/%m/%Y' -> ('dob', [('date', '%d/%m/%Y')])"""
    parts = token.split("|")
    name = parts[0].strip()
    modifiers = []
    for raw in parts[1:]:
        mod, _, arg = raw.strip().partition(":")
        modifiers.append((mod.strip().lower(), arg))
    return name, modifiers


def _lookup(name: str, context: Dict[str, Any]) -> Any:
    """Resolves a bare name against the context, including the {{today}} forms."""
    if name in context:
        return context[name]

    match = _TODAY_RE.match(name)
    if match:
        sign, amount, unit = match.groups()
        result = date.today()
        if amount:
            days = int(amount) * {"d": 1, "w": 7, "m": 30, "y": 365}[unit.lower()]
            result += timedelta(days=days if sign == "+" else -days)
        return result.isoformat()

    return _MISSING


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def resolve(template: Any, context: Dict[str, Any], where: str = "") -> Any:
    """
    Substitutes every {{placeholder}} in `template` against `context`.

    A template that is EXACTLY one placeholder returns the raw value with its
    type intact ({{index1}} -> 2, not "2"); anything else returns a string.

    Raises WaitlistConfigError naming `where` if a placeholder has no value —
    a silently blank passport field is far worse than a loud failure.
    """
    if not isinstance(template, str):
        return template

    match = _PLACEHOLDER_RE.fullmatch(template.strip())
    if match:
        name, modifiers = _parse_token(match.group(1))
        value = _lookup(name, context)
        if value is _MISSING:
            raise WaitlistConfigError(_missing_message(name, context, where))
        return _apply_modifiers(value, modifiers, match.group(1))

    def _replace(m):
        name, modifiers = _parse_token(m.group(1))
        value = _lookup(name, context)
        if value is _MISSING:
            raise WaitlistConfigError(_missing_message(name, context, where))
        return str(_apply_modifiers(value, modifiers, m.group(1)))

    return _PLACEHOLDER_RE.sub(_replace, template)


def _missing_message(name: str, context: Dict[str, Any], where: str) -> str:
    location = f" (in {where})" if where else ""
    available = ", ".join(sorted(k for k in context if "." not in k)[:25])
    return (
        f"Placeholder {{{{{name}}}}}{location} has no value. Add '{name}' to the "
        f"registrant profile, or correct the name in the waitlist config. "
        f"Available fields: {available}"
    )


def placeholders_in(template: Any) -> List[str]:
    """Every placeholder NAME used in a template (modifiers stripped)."""
    if not isinstance(template, str):
        return []
    return [_parse_token(t)[0] for t in _PLACEHOLDER_RE.findall(template)]


def validate(templates: List[Tuple[str, Any]], context: Dict[str, Any]) -> List[str]:
    """
    Dry-checks a batch of (where, template) pairs against a context and returns a
    list of human-readable problems (empty when all resolve).

    Called BEFORE the browser is launched so a missing passport number costs a
    second, not a half-finished registration.
    """
    problems = []
    for where, template in templates:
        try:
            resolve(template, context, where=where)
        except WaitlistConfigError as e:
            problems.append(str(e))
    return problems
