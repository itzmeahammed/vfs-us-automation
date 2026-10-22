"""The inbox watcher's heartbeat: one small JSON file that says what it is doing.

Written by every pass (src/inbox/service.py) and read by GET /v1/inbox/status,
by the supervisor (to skip its own pass while the API's watcher is live), and
by anyone with `cat state/inbox_watcher.json`. It is a status surface, not a
ledger: losing it costs nothing but a stale dashboard.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STATE_DIR = os.path.join(REPO_ROOT, "state")
FILE = os.path.join(STATE_DIR, "inbox_watcher.json")

RUNNING = "running"
IDLE = "idle"
ERROR = "error"
DISABLED = "disabled"
STOPPED = "stopped"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read() -> Dict[str, Any]:
    """The last heartbeat, or {} when none was ever written."""
    try:
        with open(FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write(**fields: Any) -> Dict[str, Any]:
    """Merge `fields` into the heartbeat and stamp it. Never raises."""
    data = read()
    data.update({k: v for k, v in fields.items() if v is not None})
    now = time.time()
    data["updated_epoch"] = now
    data["updated_at"] = _iso(now)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".inbox_watcher-",
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, FILE)
    except OSError as exc:
        log.warning("Could not write the inbox heartbeat: %s", exc)
    return data


def age_seconds(data: Optional[Dict[str, Any]] = None) -> Optional[float]:
    data = read() if data is None else data
    stamp = data.get("updated_epoch")
    if not stamp:
        return None
    return max(0.0, time.time() - float(stamp))


def is_live(max_age_seconds: Optional[float] = None,
            data: Optional[Dict[str, Any]] = None) -> bool:
    """True when a watcher process has reported recently enough to be trusted
    to run the next pass itself.

    "Recently" defaults to two poll intervals plus a pass's worth of slack, so
    one slow or failed pass does not make the supervisor pile in on top. A
    heartbeat in the STOPPED or DISABLED state is never live, however fresh.
    """
    data = read() if data is None else data
    if not data or data.get("state") in (STOPPED, DISABLED):
        return False
    age = age_seconds(data)
    if age is None:
        return False
    if max_age_seconds is None:
        try:
            from src.settings import settings
            poll = int(settings().inbox.poll_seconds or 300)
        except Exception:                                   # noqa: BLE001
            poll = 300
        max_age_seconds = 2 * poll + 120
    return age <= max_age_seconds
