"""Tools the model may call, and the guard rails around calling them.

A tool is a plain Python function plus a JSON-schema for its arguments:

    registry = ToolRegistry()

    @registry.tool(
        description="Daily totals for the last N days.",
        parameters={"type": "object",
                    "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 90}},
                    "required": []})
    def get_trend(days: int = 14):
        return {...}

Every call goes through `ToolRegistry.call`, which:
  * rejects unknown tools and validates arguments against the schema (types,
    required, enum, min/max, maxLength) — the model's arguments are untrusted
    input like any other;
  * runs the function on a worker thread under a deadline;
  * JSON-encodes the result, redacts it, and caps its size;
  * turns every failure into a short error the model can read and recover from,
    never a stack trace and never an exception that kills the answer.

Tools must be READ-ONLY. The registry cannot enforce that; it is the contract.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as _dt
import decimal
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .redact import Redactor

_JSON_TYPES = {
    "string": str, "integer": int, "number": (int, float), "boolean": bool,
    "array": list, "object": dict,
}


class ToolError(Exception):
    """Raise from a tool to send the model a clean, intentional error message."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., Any]
    timeout_s: Optional[float] = None

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


@dataclass
class ToolResult:
    name: str
    args: dict
    ok: bool
    content: str          # what the model sees (redacted, capped)
    ms: int
    chars: int            # size before capping
    truncated: bool


def _default(o: Any):
    if isinstance(o, (_dt.datetime, _dt.date, _dt.time)):
        return o.isoformat()
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if isinstance(o, bytes):
        return f"<{len(o)} bytes>"
    if hasattr(o, "keys") and hasattr(o, "__getitem__"):   # sqlite3.Row & co.
        return {k: o[k] for k in o.keys()}
    return str(o)


def jsonable(value: Any) -> Any:
    """Make dict keys JSON-safe (tuples -> 'a,b') recursively."""
    if isinstance(value, dict):
        return {(",".join(map(str, k)) if isinstance(k, tuple) else
                 k if isinstance(k, (str, int, float, bool)) or k is None else str(k)):
                jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def _validate(schema: dict, args: dict) -> Tuple[Optional[dict], Optional[str]]:
    props = schema.get("properties", {}) or {}
    clean: Dict[str, Any] = {}
    for key in args:
        if key not in props:
            return None, f"unknown argument '{key}'. Allowed: {sorted(props) or 'none'}"
    for key in schema.get("required", []) or []:
        if args.get(key) is None:
            return None, f"missing required argument '{key}'"
    for key, spec in props.items():
        if key not in args or args[key] is None:
            continue
        v = args[key]
        typ = spec.get("type")
        if typ == "integer" and isinstance(v, float) and v.is_integer():
            v = int(v)
        if typ == "number" and isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                pass
        if typ == "integer" and isinstance(v, str) and v.strip().lstrip("-").isdigit():
            v = int(v)
        py = _JSON_TYPES.get(typ)
        if py and (not isinstance(v, py) or (typ in ("integer", "number") and isinstance(v, bool))):
            return None, f"argument '{key}' must be {typ}"
        if "enum" in spec and v not in spec["enum"]:
            return None, f"argument '{key}' must be one of {spec['enum']}"
        if "minimum" in spec and v < spec["minimum"]:
            return None, f"argument '{key}' must be >= {spec['minimum']}"
        if "maximum" in spec and v > spec["maximum"]:
            return None, f"argument '{key}' must be <= {spec['maximum']}"
        if "maxLength" in spec and isinstance(v, str) and len(v) > spec["maxLength"]:
            return None, f"argument '{key}' is longer than {spec['maxLength']} chars"
        if "pattern" in spec and isinstance(v, str) and not re.fullmatch(spec["pattern"], v):
            return None, f"argument '{key}'={v!r} does not match {spec['pattern']}"
        clean[key] = v
    return clean, None


class ToolRegistry:
    def __init__(self, max_workers: int = 4):
        self._tools: Dict[str, Tool] = {}
        self._pool = cf.ThreadPoolExecutor(max_workers=max_workers,
                                           thread_name_prefix="assistant-tool")

    # ---- registration ------------------------------------------------------
    def add(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' registered twice")
        if not tool.name.replace("_", "").isalnum() or len(tool.name) > 64:
            raise ValueError(f"bad tool name '{tool.name}'")
        params = tool.parameters or {"type": "object", "properties": {}}
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        tool.parameters = params
        self._tools[tool.name] = tool
        return tool

    def tool(self, name: Optional[str] = None, *, description: str,
             parameters: Optional[dict] = None, timeout_s: Optional[float] = None):
        def deco(fn):
            self.add(Tool(name or fn.__name__, description.strip(), parameters or {}, fn,
                          timeout_s))
            return fn
        return deco

    def names(self) -> List[str]:
        return list(self._tools)

    def schemas(self) -> List[dict]:
        return [t.schema() for t in self._tools.values()]

    # ---- calling -----------------------------------------------------------
    def call(self, name: str, raw_args: str, *, redactor: Redactor,
             default_timeout_s: float, max_chars: int) -> ToolResult:
        t0 = time.monotonic()

        def done(ok: bool, payload: Any, args: dict) -> ToolResult:
            if isinstance(payload, str):
                text = redactor.text(payload)
            else:
                # Round-trip through JSON first so rows, dates and tuple keys are
                # plain data by the time the redactor walks them — an object it
                # cannot see inside is an object it cannot scrub.
                plain = json.loads(json.dumps(jsonable(payload), default=_default))
                text = json.dumps(redactor.obj(plain), ensure_ascii=False,
                                  separators=(",", ":"))
            size = len(text)
            truncated = size > max_chars
            if truncated:
                text = (text[:max_chars] + f"\n…[truncated: {size - max_chars} more chars. "
                        "Call again with narrower arguments (fewer days, a route, a limit).]")
            return ToolResult(name, args, ok, text, int((time.monotonic() - t0) * 1000),
                              size, truncated)

        tool = self._tools.get(name)
        if tool is None:
            return done(False, {"error": f"no tool named '{name}'",
                                "available": self.names()}, {})
        try:
            args = json.loads(raw_args or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            return done(False, {"error": f"arguments are not valid JSON: {e}"}, {})
        # The model addresses accounts by pseudonym; the tool needs the real one.
        args = redactor.restore_obj(args)
        clean, err = _validate(tool.parameters, args)
        if err:
            return done(False, {"error": err}, args)

        timeout = tool.timeout_s or default_timeout_s
        fut = self._pool.submit(tool.func, **clean)
        try:
            result = fut.result(timeout=timeout)
        except cf.TimeoutError:
            fut.cancel()
            return done(False, {"error": f"tool timed out after {timeout:.0f}s; "
                                         "try narrower arguments"}, clean)
        except ToolError as e:
            return done(False, {"error": str(e)}, clean)
        except Exception as e:                      # a bug in a tool: say what, not where
            return done(False, {"error": f"{type(e).__name__}: {str(e)[:300]}"}, clean)
        return done(True, result, clean)

    def close(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
