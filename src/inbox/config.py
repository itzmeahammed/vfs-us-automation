"""Load config/inbox/<ROUTE>.json — the per-country email matchers.

Mirrors src/waitlist/config.py deliberately: same "extends" inheritance, same
merge-by-name, same load-time validation, same cache. A country's email wording
is configuration in exactly the way its page structure is, and someone who has
edited one of these files should find the other already familiar.

Matchers merge BY NAME, like waitlist steps:

    * repeating a name overrides just the keys given (the rest survive)
    * a new name is appended, or positioned with "before"/"after"
    * "remove": true drops an inherited matcher

ORDER MATTERS. classify() takes the first match, so a general matcher inherited
from _default sits below the specific ones a route adds — which is what the
default append does. Use "before" only when a route genuinely needs to pre-empt
an inherited matcher.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from src.inbox.matcher import MatcherConfigError, validate_matchers

INBOX_DIR = os.path.join("config", "inbox")

#: Parsed files by key. Cleared by clear_cache(); tests rely on that.
_cache: Dict[str, Optional[Dict[str, Any]]] = {}
_resolved: Dict[str, Dict[str, Any]] = {}


def _load_file(key: str) -> Optional[Dict[str, Any]]:
    """Reads one inbox config by key; None if the file is absent.

    A malformed file RAISES rather than being skipped. Silently ignoring a
    broken matcher file would mean classifying nothing for that country and
    looking exactly like 'no mail arrived' — the failure this package exists to
    prevent. Same reasoning as waitlist/config.py.
    """
    if key in _cache:
        return _cache[key]
    path = os.path.join(INBOX_DIR, f"{key}.json")
    if not os.path.isfile(path):
        _cache[key] = None
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise MatcherConfigError(f"Could not read inbox config '{path}': {e}") from e
    _cache[key] = data
    return data


def _merge_matchers(parent: List[dict], child: List[dict]) -> List[dict]:
    """Merges child matchers over parent ones BY NAME, preserving parent order."""
    by_name = {m.get("name"): dict(m) for m in parent or []}
    order = [m.get("name") for m in parent or []]

    for entry in child or []:
        name = entry.get("name")
        if not name:
            raise MatcherConfigError('Every inbox matcher needs a unique "name".')
        if entry.get("remove"):
            by_name.pop(name, None)
            order = [n for n in order if n != name]
            continue
        if name in by_name:
            merged = dict(by_name[name])
            merged.update(entry)          # child keys win, parent keys survive
            by_name[name] = merged
        elif entry.get("after") or entry.get("before"):
            anchor = entry.get("after") or entry.get("before")
            if anchor not in order:
                raise MatcherConfigError(
                    f"Matcher '{name}' is positioned relative to '{anchor}', "
                    f"which is not a matcher here. Known: {', '.join(order) or 'none'}."
                )
            by_name[name] = dict(entry)
            at = order.index(anchor)
            order.insert(at + 1 if entry.get("after") else at, name)
        else:
            by_name[name] = dict(entry)
            order.append(name)

    return [by_name[n] for n in order if n in by_name]


def _merge(parent: Dict[str, Any], child: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(parent)
    for key, value in child.items():
        if key == "extends":
            continue
        if key == "matchers":
            merged["matchers"] = _merge_matchers(parent.get("matchers", []), value)
        else:
            merged[key] = value
    return merged


def _resolve(config: Dict[str, Any], seen: List[str]) -> Dict[str, Any]:
    parent_key = config.get("extends")
    if not parent_key:
        return config
    if parent_key in seen:
        raise MatcherConfigError(
            f"Cyclic 'extends' in inbox configs: {' -> '.join(seen + [parent_key])}"
        )
    parent = _load_file(parent_key)
    if parent is None:
        raise MatcherConfigError(
            f"Inbox config extends missing parent '{parent_key}' "
            f"(expected {os.path.join(INBOX_DIR, parent_key + '.json')})."
        )
    return _merge(_resolve(parent, seen + [parent_key]), config)


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def get(route: str) -> Dict[str, Any]:
    """The fully resolved, validated inbox config for a route.

    Raises MatcherConfigError if the route has no config or the config is bad.
    """
    key = (route or "").upper()
    if key in _resolved:
        return _resolved[key]

    raw = _load_file(key)
    if raw is None:
        raise MatcherConfigError(
            f"No inbox config for route '{route}' "
            f"(expected {os.path.join(INBOX_DIR, key + '.json')})."
        )

    config = _resolve(raw, [key])
    matchers = config.get("matchers")
    if not matchers:
        raise MatcherConfigError(f"'{route}': inbox config defines no \"matchers\".")
    validate_matchers(matchers, where=f"config/inbox/{key}.json")

    _resolved[key] = config
    return config


def matchers_for(route: str) -> List[Dict[str, Any]]:
    """Just the matcher list for a route."""
    return get(route).get("matchers", [])


def configured_routes() -> List[str]:
    """Every route with an inbox config, excluding _-prefixed base files."""
    if not os.path.isdir(INBOX_DIR):
        return []
    out = []
    for name in sorted(os.listdir(INBOX_DIR)):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        out.append(name[: -len(".json")].upper())
    return out


def all_matchers() -> Dict[str, List[Dict[str, Any]]]:
    """Every configured route's matchers, for classify_all().

    A broken config for ONE country must not blind the watcher to every other,
    so a failing route is skipped here rather than raising. `check()` is the
    place that reports such a file loudly.
    """
    import logging

    out: Dict[str, List[Dict[str, Any]]] = {}
    for route in configured_routes():
        try:
            out[route] = matchers_for(route)
        except MatcherConfigError as e:
            logging.error(f"Inbox config for {route} is unusable, skipping it: {e}")
    return out


def check() -> List[str]:
    """Validates every configured route. Returns a list of problems (empty = OK).

    The offline preflight: `python -m src.inbox check` before trusting a run.
    """
    problems = []
    for route in configured_routes():
        try:
            get(route)
        except MatcherConfigError as e:
            problems.append(f"{route}: {e}")
    return problems


def clear_cache() -> None:
    """Drops the caches so edited config files are re-read (tests use this)."""
    _cache.clear()
    _resolved.clear()
