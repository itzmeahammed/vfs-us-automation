"""Per-request context: a request id, who acted, and the audit trail.

REQUEST ID. Every response carries X-Request-ID (the caller's, when it sent a
sane one, else a fresh one), and every /v1 error body repeats it. A web app
logging that id can hand it over and the matching server lines are one grep.

X-ACTOR. The API has ONE shared token, so on its own it cannot say WHICH
sales agent armed a booking that later spent money. The web app knows; it
sends `X-Actor: <agent email or id>` and the audit log records it. This is
attribution, not authentication — the token still decides who may call.

AUDIT LOG. Every mutating request (POST, PUT, PATCH, DELETE) appends one line
to state/audit.jsonl: when, request id, actor, method, path, status, caller
ip. Never the body — bodies carry passports and passwords. state/, not logs/:
a retention sweep must never delete the record of who spent money.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import Request

log = logging.getLogger("vfs.api.audit")

REQUEST_ID_HEADER = "X-Request-ID"
ACTOR_HEADER = "X-Actor"
_SAFE = re.compile(r"^[A-Za-z0-9._@:+\-]{1,80}$")
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

AUDIT_FILE = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")),
    "state", "audit.jsonl")
_write_lock = threading.Lock()


def request_id_for(request: Request) -> str:
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    return supplied if _SAFE.match(supplied) else secrets.token_hex(8)


def actor_for(request: Request) -> str:
    """The X-Actor header if it is a sane token, else 'unknown'. Logged, so
    it is constrained: no spaces, no newlines, at most 80 characters."""
    supplied = request.headers.get(ACTOR_HEADER, "").strip()
    return supplied if _SAFE.match(supplied) else "unknown"


def audit(request: Request, status: int) -> None:
    """Append one line for a mutating request. Never raises."""
    if request.method not in MUTATING:
        return
    entry: dict[str, Any] = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "request_id": getattr(request.state, "request_id", ""),
        "actor": getattr(request.state, "actor", "unknown"),
        "method": request.method,
        "path": request.url.path,
        "status": status,
        "ip": (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
               or (request.client.host if request.client else "")),
    }
    try:
        os.makedirs(os.path.dirname(AUDIT_FILE), exist_ok=True)
        with _write_lock, open(AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.warning("Could not write the audit log: %s", exc)


def read_audit(limit: int = 100, actor: str = "", path_prefix: str = "") -> list:
    """Most recent first."""
    if not os.path.exists(AUDIT_FILE):
        return []
    with open(AUDIT_FILE, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    out = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if actor and row.get("actor") != actor:
            continue
        if path_prefix and not str(row.get("path", "")).startswith(path_prefix):
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out
