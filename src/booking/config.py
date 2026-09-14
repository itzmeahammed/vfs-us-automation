"""Load config/booking/<ROUTE>.json — the per-country booking flow.

Mirrors src/waitlist/config.py deliberately, down to the function names: same
"extends" inheritance, same merge-steps-by-name, same load-time validation, same
cache. Somebody who has edited a waitlist route file already knows how to edit a
booking one, and the two can be reasoned about together.

WHAT DIFFERS FROM THE WAITLIST CONFIG
-------------------------------------
1. Steps carry a "type". Registration steps are all "fill a form and submit";
   booking adds genuinely different kinds of page — find a row on a dashboard,
   assert an identity, pick a date from a calendar — so the engine dispatches on
   type instead of running one procedure.

2. The committing step is LATER and DIFFERENT. In registration the point of no
   return is the review/pay submit. In booking it is the SLOT PICK: that is
   where a slot leaves the pool. The "exactly one step must be marked commits"
   rule carries over unchanged; only which step carries the flag differs.

3. An "identity" block. Registration knows who it is registering — the caller
   said so. Booking has to work it out from a dashboard, so the confidence rules
   are configuration rather than code. See src/booking/identity.py.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from src.booking.errors import BookingConfigError

BOOKING_DIR = os.path.join("config", "booking")

#: Step types the engine can dispatch. A config naming anything else is refused
#: at LOAD time — a typo must not surface as "nothing happened" mid-flow.
STEP_TYPES = frozenset({
    "dashboard_resume",   # find the waitlisted application, open it
    "identity_assert",    # verify the opened application is the right client
    "form",               # fill fields and submit (the waitlist behaviour)
    "slot_pick",          # choose a date/time — THE COMMITTING STEP
    "confirm",            # read the confirmation, capture the reference
})

#: Steps that may carry "commits": true. Marking anything else is almost
#: certainly a mistake, and a mistake here is the expensive kind.
COMMITTABLE_TYPES = frozenset({"slot_pick", "form", "confirm"})

_cache: Dict[str, Optional[Dict[str, Any]]] = {}
_resolved: Dict[str, Dict[str, Any]] = {}


# --------------------------------------------------------------------------- #
# Loading                                                                      #
# --------------------------------------------------------------------------- #

def _load_file(key: str) -> Optional[Dict[str, Any]]:
    """Reads one booking config by key; None if absent.

    A malformed file RAISES rather than being skipped. Booking must never
    proceed against a half-understood page description — the same rule
    waitlist/config.py holds, and for the same reason.
    """
    if key in _cache:
        return _cache[key]

    path = os.path.join(BOOKING_DIR, f"{key}.json")
    if not os.path.isfile(path):
        _cache[key] = None
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise BookingConfigError(f"Could not read booking config '{path}': {e}") from e

    _cache[key] = data
    return data


def _merge_step(parent: dict, child: dict) -> dict:
    """Merges one child step over its parent, ONE LEVEL DEEP into nested objects.

    Deeper than the waitlist's shallow merge, because booking steps carry nested
    config blocks — "row", "calendar", "submit" — and a route usually needs to
    override ONE key inside one of them.

    A shallow merge replaces the whole block, which is silent and wrong: Greece
    narrowing `row.reference_pattern` to the GRC prefix would have dropped the
    inherited `row.container` and `row.open` selectors with it, leaving a step
    that cannot find or click anything. (A real bug, caught by a test.)

    Deliberately one level only. Deeper merging makes "where did this value come
    from?" genuinely hard to answer from the files, and nothing here needs it.
    To replace a nested block outright rather than merge into it, remove the
    step and add it back under the same name.
    """
    merged = dict(parent)
    for key, value in child.items():
        existing = merged.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            nested = dict(existing)
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def _merge_steps(parent: List[dict], child: List[dict]) -> List[dict]:
    """Merges child steps over parent steps BY NAME, preserving parent order.

    Identical semantics to waitlist/config._merge_steps: repeat a name to
    override just the keys given, "remove": true to drop one, "before"/"after"
    to position a new one. Position matters — steps run in order, so a portal
    with an extra page in the MIDDLE would otherwise have it appended after the
    committing step and never reached.
    """
    by_name = {s.get("name"): dict(s) for s in parent or []}
    order = [s.get("name") for s in parent or []]

    for step in child or []:
        name = step.get("name")
        if not name:
            raise BookingConfigError('Every booking step needs a unique "name".')

        if step.get("remove"):
            by_name.pop(name, None)
            order = [n for n in order if n != name]
            continue

        if name in by_name:
            by_name[name] = _merge_step(by_name[name], step)
        elif step.get("after") or step.get("before"):
            anchor = step.get("after") or step.get("before")
            if anchor not in order:
                raise BookingConfigError(
                    f"Step '{name}' is positioned relative to '{anchor}', which "
                    f"is not a step in this flow. Known steps: "
                    f"{', '.join(n for n in order if n) or 'none'}."
                )
            by_name[name] = dict(step)
            at = order.index(anchor)
            order.insert(at + 1 if step.get("after") else at, name)
        else:
            by_name[name] = dict(step)
            order.append(name)

    return [by_name[n] for n in order if n in by_name]


def _merge(parent: Dict[str, Any], child: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(parent)
    for key, value in child.items():
        if key == "extends":
            continue
        if key == "steps":
            merged["steps"] = _merge_steps(parent.get("steps", []), value)
        elif key in ("identity", "confirmation") and isinstance(value, dict):
            base = dict(parent.get(key) or {})
            base.update(value)
            merged[key] = base
        else:
            merged[key] = value
    return merged


def _resolve(config: Dict[str, Any], seen: List[str]) -> Dict[str, Any]:
    parent_key = config.get("extends")
    if not parent_key:
        return config
    if parent_key in seen:
        raise BookingConfigError(
            f"Cyclic 'extends' in booking configs: {' -> '.join(seen + [parent_key])}"
        )
    parent = _load_file(parent_key)
    if parent is None:
        raise BookingConfigError(
            f"Booking config extends missing parent '{parent_key}' "
            f"(expected {os.path.join(BOOKING_DIR, parent_key + '.json')})."
        )
    return _merge(_resolve(parent, seen + [parent_key]), config)


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #

def _validate(route: str, config: Dict[str, Any]) -> None:
    """Refuses a config that could not be executed safely.

    Everything checked here is checked at LOAD time, before a browser exists,
    because the alternative is discovering it half-way through a flow that has
    already touched the client's account.
    """
    steps = config.get("steps")
    if not steps:
        raise BookingConfigError(f"'{route}': defines no \"steps\" — nothing to do.")
    if not isinstance(steps, list):
        raise BookingConfigError(f"'{route}': \"steps\" must be a list.")

    names: List[str] = []
    committing: List[str] = []

    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise BookingConfigError(f"'{route}': step {index} must be an object.")

        name = step.get("name")
        if not name:
            raise BookingConfigError(f"'{route}': step {index} is missing \"name\".")
        if name in names:
            raise BookingConfigError(f"'{route}': duplicate step name '{name}'.")
        names.append(name)

        step_type = step.get("type")
        if not step_type:
            raise BookingConfigError(
                f"'{route}' step '{name}': missing \"type\". One of: "
                f"{', '.join(sorted(STEP_TYPES))}."
            )
        if step_type not in STEP_TYPES:
            raise BookingConfigError(
                f"'{route}' step '{name}': unknown type '{step_type}'. "
                f"Valid: {', '.join(sorted(STEP_TYPES))}."
            )

        fields = step.get("fields") or []
        if not isinstance(fields, list):
            raise BookingConfigError(
                f"'{route}' step '{name}': \"fields\" must be a list."
            )

        if step.get("commits"):
            committing.append(name)
            if step_type not in COMMITTABLE_TYPES:
                raise BookingConfigError(
                    f"'{route}' step '{name}': a '{step_type}' step cannot be the "
                    f"commit point — it changes nothing at VFS. Committable "
                    f"types: {', '.join(sorted(COMMITTABLE_TYPES))}."
                )

    # The property the whole safety design rests on. Without exactly one, either
    # nothing is journalled as committed (so a taken slot leaves no trace), or
    # two steps both claim to be the point of no return and the write-ahead
    # marker lands in the wrong place.
    if not committing:
        raise BookingConfigError(
            f"'{route}': no step is marked \"commits\": true. Exactly one step "
            f"must be flagged as the point of no return — for booking that is "
            f"normally the slot_pick, where the slot actually leaves the pool."
        )
    if len(committing) > 1:
        raise BookingConfigError(
            f"'{route}': {len(committing)} steps are marked \"commits\": true "
            f"({', '.join(committing)}). Exactly one is allowed."
        )

    # An identity_assert AFTER the commit verifies nothing that can still be
    # undone: the whole value of click-then-check is that the check happens while
    # backing out is still free.
    commit_at = names.index(committing[0])
    for index, step in enumerate(steps):
        if step.get("type") == "identity_assert" and index > commit_at:
            raise BookingConfigError(
                f"'{route}' step '{step['name']}': an identity_assert must come "
                f"BEFORE the committing step '{committing[0]}'. Verifying after "
                f"the commit cannot prevent a wrong booking."
            )

    identity = config.get("identity")
    if identity is not None and not isinstance(identity, dict):
        raise BookingConfigError(f"'{route}': \"identity\" must be an object.")


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def get(route: str) -> Dict[str, Any]:
    """The fully resolved, validated booking config for a route."""
    key = (route or "").upper()
    if key in _resolved:
        return _resolved[key]

    raw = _load_file(key)
    if raw is None:
        raise BookingConfigError(
            f"No booking config for route '{route}' "
            f"(expected {os.path.join(BOOKING_DIR, key + '.json')})."
        )

    config = _resolve(raw, [key])
    _validate(key, config)
    _resolved[key] = config
    return config


def steps_for(route: str) -> List[Dict[str, Any]]:
    """Just the step list."""
    return get(route).get("steps", [])


def commit_step_name(route: str) -> str:
    """The name of the step that is the point of no return.

    Validation guarantees exactly one, so this cannot return None for a config
    that loaded.
    """
    for step in steps_for(route):
        if step.get("commits"):
            return step["name"]
    raise BookingConfigError(f"'{route}': no committing step (should be unreachable).")


def identity_policy(route: str) -> Dict[str, Any]:
    """The identity-matching rules, with SAFE defaults for anything unset.

    The defaults are the strict ones on purpose: a route that says nothing about
    identity gets exact-match-or-refuse, not a permissive guess.
    """
    policy = dict(get(route).get("identity") or {})
    policy.setdefault("min_confidence", "exact")
    policy.setdefault("require_unique_match", True)
    policy.setdefault("verify_on_detail_page", True)
    policy.setdefault("on_ambiguous", "abort")
    return policy


def is_enabled(route: str) -> bool:
    """Whether this route may run at all. Absent = disabled."""
    try:
        return bool(get(route).get("enabled"))
    except BookingConfigError:
        return False


def configured_routes() -> List[str]:
    """Every route with a booking config, excluding _-prefixed base files."""
    if not os.path.isdir(BOOKING_DIR):
        return []
    return [
        name[: -len(".json")].upper()
        for name in sorted(os.listdir(BOOKING_DIR))
        if name.endswith(".json") and not name.startswith("_")
    ]


def check() -> List[str]:
    """Validates every configured route. Returns problems (empty = all good)."""
    problems = []
    for route in configured_routes():
        try:
            get(route)
        except BookingConfigError as e:
            problems.append(f"{route}: {e}")
    return problems


def clear_cache() -> None:
    """Drops the caches so edited files are re-read (tests use this)."""
    _cache.clear()
    _resolved.clear()
