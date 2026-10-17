"""Who may ask, how often, and how much it may cost.

The assistant spends money on every question and reads operational data, so
the HTTP endpoint is treated as an attack surface even on 127.0.0.1:

* A web page you visit in the same browser can POST to localhost (CSRF) and a
  hostile DNS name can be rebound to 127.0.0.1. `check_request` refuses both:
  the Host header must be on the allow-list, an Origin (when sent) must match
  it, Sec-Fetch-Site must be same-origin, and the body must be JSON — which a
  cross-site form cannot send without a CORS preflight that this server never
  answers.
* `RateLimiter` caps questions per minute; `UsageLedger` caps tokens per day
  and survives restarts; a semaphore caps concurrent answers.
* `AuditLog` records each question's metadata (not the answer) for review.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import os
import threading
import time
from collections import deque
from typing import Mapping, Optional
from urllib.parse import urlsplit


def _hostname(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("["):                       # [::1]:8765
        return value[1:value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def check_request(method: str, headers: Mapping[str, str], client_ip: str, *,
                  allowed_hosts, loopback_only: bool = True) -> Optional[str]:
    """None when the request may proceed, else the reason to refuse it."""
    if loopback_only:
        try:
            if not ipaddress.ip_address(client_ip.split("%")[0]).is_loopback:
                return "only local clients may use the assistant"
        except ValueError:
            return "unrecognised client address"
    allowed = {h.lower() for h in allowed_hosts}
    host = _hostname(headers.get("Host", ""))
    if host not in allowed:
        return "host not allowed"
    origin = headers.get("Origin")
    if origin and origin != "null":
        if _hostname(urlsplit(origin).netloc) not in allowed:
            return "cross-origin request refused"
    elif origin == "null":
        return "cross-origin request refused"
    site = (headers.get("Sec-Fetch-Site") or "").lower()
    if site and site not in ("same-origin", "none"):
        return "cross-site request refused"
    if method == "POST":
        ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return "content-type must be application/json"
    return None


class RateLimiter:
    """Sliding one-minute window, process-wide."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits = deque()
        self._lock = threading.Lock()

    def allow(self) -> Optional[float]:
        """None if allowed, else seconds until the next slot frees up."""
        if self.per_minute <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            while self._hits and now - self._hits[0] >= 60:
                self._hits.popleft()
            if len(self._hits) >= self.per_minute:
                return 60 - (now - self._hits[0])
            self._hits.append(now)
            return None


class UsageLedger:
    """Tokens spent per local day, optionally persisted to a small JSON file."""

    def __init__(self, daily_budget: int, path: Optional[str] = None):
        self.daily_budget = daily_budget
        self.path = path
        self._lock = threading.Lock()
        self._day = self._today()
        self._used = 0
        self._load()

    @staticmethod
    def _today() -> str:
        return _dt.date.today().isoformat()

    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == self._day:
                self._used = int(data.get("tokens") or 0)
        except (OSError, ValueError, TypeError):
            pass

    def _save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"date": self._day, "tokens": self._used}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _roll(self):
        today = self._today()
        if today != self._day:
            self._day, self._used = today, 0

    def remaining(self) -> Optional[int]:
        with self._lock:
            self._roll()
            if self.daily_budget <= 0:
                return None
            return max(0, self.daily_budget - self._used)

    def add(self, tokens: int):
        with self._lock:
            self._roll()
            self._used += max(0, int(tokens or 0))
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            self._roll()
            return {"date": self._day, "tokens": self._used,
                    "budget": self.daily_budget or None}


class AuditLog:
    """Append-only JSONL, rotated at `max_bytes` (one previous file kept)."""

    def __init__(self, path: Optional[str], max_bytes: int = 5_000_000):
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def write(self, record: dict):
        if not self.path:
            return
        record = {"ts": _dt.datetime.now().astimezone().isoformat(timespec="seconds"), **record}
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
                if os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass
