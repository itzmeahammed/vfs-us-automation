import json
import logging
import os
from typing import Any, Dict, Optional

# Per-route flow schemas live here as <SOURCE>-<DEST>.json (e.g. AE-LU.json).
# A `_default.json` provides the shared base flow used by any route without its
# own file (e.g. DE / IT / MT, which don't diverge from the standard portal).
SCHEMA_DIR = os.path.join("config", "routes")
DEFAULT_KEY = "_default"

_cache: Dict[str, Dict[str, Any]] = {}


def _load_file(key: str) -> Optional[Dict[str, Any]]:
    """Loads and caches a single schema JSON by key; returns None if absent/invalid."""
    if key in _cache:
        return _cache[key]

    path = os.path.join(SCHEMA_DIR, f"{key}.json")
    if not os.path.isfile(path):
        _cache[key] = None
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _cache[key] = data
        return data
    except (OSError, json.JSONDecodeError) as e:
        logging.error(f"Could not read route schema '{path}': {e}")
        _cache[key] = None
        return None


def clear_cache() -> None:
    """Drops the file cache so edited schemas are re-read."""
    _cache.clear()


def get_route_schema(source_code: str, dest_code: str) -> Dict[str, Any]:
    """
    Returns the flow schema for a route (e.g. AE-LU), falling back to the shared
    `_default.json` when the route has no file of its own.

    A route file may set `"extends": "_default"` (or another key) to inherit and
    override only the steps it changes, so a portal that just adds one step does
    not have to restate the entire flow.

    Returns an empty dict if neither the route file nor the default exists.
    """
    key = f"{source_code}-{dest_code}"
    schema = _load_file(key)
    if schema is None:
        schema = _load_file(DEFAULT_KEY)
        if schema is None:
            logging.warning(
                f"No route schema for '{key}' and no '{DEFAULT_KEY}.json' default."
            )
            return {}
        return schema

    return _resolve(schema, seen=[key])


def _resolve(schema: Dict[str, Any], seen: list) -> Dict[str, Any]:
    """
    Resolves the `extends` chain (child overrides parent), guarding against
    cycles. `IN-LU` -> `AE-LU` -> `_default` resolves bottom-up so the deepest
    parent is built first, then each child merged over it.
    """
    parent_key = schema.get("extends")
    if not parent_key:
        return schema
    if parent_key in seen:
        logging.error(
            f"Cyclic 'extends' in route schemas: {' -> '.join(seen + [parent_key])}"
        )
        return schema
    parent = _load_file(parent_key)
    if parent is None:
        logging.warning(f"Schema extends missing parent '{parent_key}'.")
        return schema
    resolved_parent = _resolve(parent, seen + [parent_key])
    return _merge(resolved_parent, schema)


def _merge(parent: Dict[str, Any], child: Dict[str, Any]) -> Dict[str, Any]:
    """
    Shallow-merges a child schema over a parent. `steps` are merged by step
    `name`: a child step with the same name replaces the parent's; new child
    steps are appended in order. All other keys are taken from the child if
    present, else the parent.
    """
    merged = dict(parent)
    for k, v in child.items():
        if k == "extends":
            continue
        if k == "steps":
            merged["steps"] = _merge_steps(parent.get("steps", []), v)
        else:
            merged[k] = v
    return merged


def _merge_steps(parent_steps, child_steps):
    by_name = {s.get("name"): dict(s) for s in parent_steps}
    order = [s.get("name") for s in parent_steps]
    for cs in child_steps:
        name = cs.get("name")
        if name in by_name:
            by_name[name] = cs
        else:
            by_name[name] = cs
            order.append(name)
    return [by_name[n] for n in order]
