"""Per-route waitlist page configuration — WHERE things are (never WHAT to type).

One JSON file per route in config/waitlist/<ROUTE>.json, describing the pages the
waitlist flow walks and the controls on them. It is kept SEPARATE from
config/routes/<ROUTE>.json on purpose:

  * config/routes/  is loaded on EVERY run and drives the always-on slot check.
    A broken waitlist file must never be able to break slot checking.
  * config/waitlist/ is loaded only when registering, and will be edited
    constantly while each portal is mapped out.

Like route schemas, a file may "extends" another so shared structure lives in
_default.json and each country overrides only what differs.

Shape (every key optional unless marked):

{
  "extends": "_default",
  "description": "...",
  "enabled": true,
  "checkbox": "mat-checkbox[formcontrolname='agreeToWaitlist']",
  "steps": [                                   // REQUIRED: pages, in order
    {
      "name": "appointment_details",           // REQUIRED, unique
      "url_contains": "application-detail",    // page gate before acting
      "settle_seconds": 0,                     // wait BEFORE filling the fields
      "dwell_seconds": 0,                      // wait AFTER filling, before submit
      "commits": false,                        // true => point of no return
      "fields": [ ... ],                       // see fields.py
      "submit": { "role": "button", "name": "Submit" }
    }

Note the two waits are separate on purpose. settle_seconds covers portals that
gate on how long the page was open before it was touched (and gives Angular time
to wire up its validators); dwell_seconds is the portal's own stated minimum
before its submit button enables. A route may set either, both, or neither.
  ],
  "confirmation": {
    "url_contains": "review",
    "success_text": ["successfully"],
    "reference_pattern": "([A-Z]{2,4}-\\\\d{6,})"
  }
}
"""

import glob
import json
import logging
import os
from typing import Any, Dict, List, Optional

from src.waitlist.errors import WaitlistConfigError

WAITLIST_DIR = os.path.join("config", "waitlist")
DEFAULT_KEY = "_default"

_cache: Dict[str, Optional[Dict[str, Any]]] = {}


# --------------------------------------------------------------------------- #
# Loading + inheritance                                                        #
# --------------------------------------------------------------------------- #

def _load_file(key: str) -> Optional[Dict[str, Any]]:
    """Loads and caches one waitlist config JSON by key; None if absent."""
    if key in _cache:
        return _cache[key]
    path = os.path.join(WAITLIST_DIR, f"{key}.json")
    if not os.path.isfile(path):
        _cache[key] = None
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        # Unlike the slot-check schema loader, a bad waitlist file RAISES rather
        # than logging and continuing — registration must never proceed against a
        # half-understood page description.
        raise WaitlistConfigError(f"Could not read waitlist config '{path}': {e}") from e
    _cache[key] = data
    return data


def _merge_steps(parent_steps: List[dict], child_steps: List[dict]) -> List[dict]:
    """Merges child steps over parent steps BY NAME, preserving parent order.

    A child step with an existing name is merged field-by-field over the parent's
    (so a route can override just `dwell_seconds` without restating the fields).
    `"remove": true` drops an inherited step entirely.

    A NEW step is appended by default, but may be positioned explicitly with
    `"after": "<step>"` or `"before": "<step>"`. Position matters: steps run in
    order, so a portal with an extra page in the MIDDLE of the flow (Italy's OTP
    sits between the details summary and review-pay) would otherwise have it
    appended after the committing step and never reached.
    """
    by_name = {s.get("name"): dict(s) for s in parent_steps or []}
    order = [s.get("name") for s in parent_steps or []]
    for child in child_steps or []:
        name = child.get("name")
        if not name:
            raise WaitlistConfigError("Every waitlist step needs a unique \"name\".")
        if child.get("remove"):
            by_name.pop(name, None)
            order = [n for n in order if n != name]
            continue
        if name in by_name:
            merged = dict(by_name[name])
            merged.update(child)          # child keys win, parent keys survive
            by_name[name] = merged
        elif child.get("after") or child.get("before"):
            anchor = child.get("after") or child.get("before")
            if anchor not in order:
                raise WaitlistConfigError(
                    f"Step '{name}' is positioned relative to '{anchor}', which "
                    f"is not a step in this flow. Known steps: "
                    f"{', '.join(order) or 'none'}.")
            by_name[name] = dict(child)
            at = order.index(anchor)
            order.insert(at + 1 if child.get("after") else at, name)
        else:
            by_name[name] = dict(child)
            order.append(name)
    return [by_name[n] for n in order if n in by_name]


def _merge(parent: Dict[str, Any], child: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(parent)
    for key, value in child.items():
        if key == "extends":
            continue
        if key == "steps":
            merged["steps"] = _merge_steps(parent.get("steps", []), value)
        elif key == "confirmation" and isinstance(value, dict):
            base = dict(parent.get("confirmation") or {})
            base.update(value)
            merged["confirmation"] = base
        else:
            merged[key] = value
    return merged


def _resolve(config: Dict[str, Any], seen: List[str]) -> Dict[str, Any]:
    parent_key = config.get("extends")
    if not parent_key:
        return config
    if parent_key in seen:
        raise WaitlistConfigError(
            f"Cyclic 'extends' in waitlist configs: {' -> '.join(seen + [parent_key])}"
        )
    parent = _load_file(parent_key)
    if parent is None:
        raise WaitlistConfigError(
            f"Waitlist config extends missing parent '{parent_key}' "
            f"(expected {os.path.join(WAITLIST_DIR, parent_key + '.json')})."
        )
    return _merge(_resolve(parent, seen + [parent_key]), config)


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #

def _validate(route: str, config: Dict[str, Any]) -> None:
    steps = config.get("steps")
    if not steps:
        raise WaitlistConfigError(
            f"Waitlist config for '{route}' defines no \"steps\" — nothing to do."
        )
    if not isinstance(steps, list):
        raise WaitlistConfigError(f"'{route}': \"steps\" must be a list.")

    names = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise WaitlistConfigError(f"'{route}': step {index} must be an object.")
        name = step.get("name")
        if not name:
            raise WaitlistConfigError(f"'{route}': step {index} is missing \"name\".")
        if name in names:
            raise WaitlistConfigError(f"'{route}': duplicate step name '{name}'.")
        names.append(name)

        fields = step.get("fields") or []
        if not isinstance(fields, list):
            raise WaitlistConfigError(f"'{route}' step '{name}': \"fields\" must be a list.")

    # Exactly the property the whole safety design rests on: SOMETHING must be
    # marked as the point of no return, or nothing would ever be journalled as
    # committed. Absent an explicit flag we cannot guess, so we refuse.
    if not any(step.get("commits") for step in steps):
        raise WaitlistConfigError(
            f"'{route}': no step is marked \"commits\": true. Exactly one step "
            "must be flagged as the point of no return (the one whose submit "
            "actually registers the waitlist) so it can be journalled correctly."
        )


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def get(route: str) -> Dict[str, Any]:
    """
    Returns the fully resolved, validated waitlist config for a route
    (e.g. 'AE-CHE'), falling back to _default.json when the route has no file.

    Raises WaitlistConfigError if neither exists or the result is invalid — a
    registration must never run against a config we don't understand.
    """
    key = (route or "").strip().upper()
    config = _load_file(key)
    if config is None:
        config = _load_file(DEFAULT_KEY)
        if config is None:
            raise WaitlistConfigError(
                f"No waitlist config for '{key}' and no '{DEFAULT_KEY}.json' "
                f"default in {WAITLIST_DIR}/. Create "
                f"{os.path.join(WAITLIST_DIR, key + '.json')} describing this "
                "portal's waitlist pages."
            )
        resolved = dict(config)
    else:
        resolved = _resolve(config, seen=[key])

    _validate(key, resolved)
    return resolved


def is_enabled(route: str) -> bool:
    """True if this route's waitlist config exists and is not switched off.

    A missing config is 'not enabled' rather than an error, so callers can ask
    cheaply without a try/except.
    """
    try:
        return bool(get(route).get("enabled", True))
    except WaitlistConfigError:
        return False


def checkbox_selector(route: str) -> Optional[str]:
    """The route's waitlist checkbox override, or None for the default."""
    try:
        return get(route).get("checkbox")
    except WaitlistConfigError:
        return None


def commit_step_name(route: str) -> str:
    """The name of the step flagged \"commits\": true."""
    for step in get(route)["steps"]:
        if step.get("commits"):
            return step["name"]
    raise WaitlistConfigError(f"'{route}': no committing step.")  # _validate prevents this


def configured_routes() -> List[str]:
    """Every route with a waitlist config file (the _default excluded)."""
    if not os.path.isdir(WAITLIST_DIR):
        return []
    routes = []
    for path in sorted(glob.glob(os.path.join(WAITLIST_DIR, "*.json"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if not name.startswith("_"):
            routes.append(name.upper())
    return routes


def clear_cache() -> None:
    """Drops the file cache (used by tests and by the config editor after a save)."""
    _cache.clear()
    logging.debug("Waitlist config cache cleared.")
