"""Reads slot results back out of the bot's own log files.

The bot has been logging every check for months; this turns that text into the
same records the live path writes, so the dashboard has real history on day one
instead of waiting a week to become useful.

What it looks for, in the order a run produces it:

    ########## Route 8/10: AE-NOR ##########          <- a run starts
    Using credential 4/8 available for AE-NOR ...: pa***@travnook.com
    proxyseller ip used here : 86.97.65.29:10005 ...
    Checking slot for: Norway ... - Dubai - Tourist   <- a combination
      -> Earliest available slot for 1 Applicants is : 16-09-2026
    Earliest available slot for 2 Applicants is : 21-09-2026   <- continuation
    Route AE-NOR OK.                                  <- the run ends

Two details the format forces:

  * A result's extra banners are logged as CONTINUATION lines with no timestamp
    of their own (they're part of one multi-line message). They must be glued
    back on, or every multi-applicant reading loses all but its first date.
  * Log timestamps carry no timezone. They are the bot machine's local clock —
    Gulf time in practice — so the local zone is applied unless one is passed.

Pure-ish: `iter_events` takes any iterable of lines, so the tests drive it with
strings rather than files.
"""

import glob
import logging
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Iterator, List, Optional

LOG_GLOB = os.path.join("logs", "app-*.log")

_TS = r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+\]"

RE_LINE = re.compile(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+\]")
RE_ROUTE_START = re.compile(_TS + r".*#{5,} Route \d+/\d+: ([A-Z]{2}-[A-Z]{2,4}) #{5,}")
RE_CREDENTIAL = re.compile(_TS + r".*Using credential .*? for ([A-Z]{2}-[A-Z]{2,4}).*?: (\S+)")
RE_PROXY = re.compile(_TS + r".*ip used here *: (\S+)")
RE_CHECKING = re.compile(_TS + r".*\[slot_check\.py:\d+\] *Checking slot for: (.+?)\s*$")
RE_RESULT = re.compile(_TS + r".*\[slot_check\.py:\d+\] *-> (.*)$")
RE_ROUTE_END = re.compile(_TS + r".*Route ([A-Z]{2}-[A-Z]{2,4}) ([A-Z]+)\.\s*$")

# Statuses the supervisor prints; anything else is ignored as noise.
_STATUSES = {"OK", "FAILED", "SKIPPED", "PAUSED", "BLOCKED", "RESTRICTED",
             "LOCKED", "GEO"}


def _parse_ts(text: str, tz: Optional[timezone]) -> datetime:
    """Log stamp -> aware datetime, in the bot machine's zone unless told otherwise."""
    naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=tz) if tz else naive.astimezone()


def iter_events(lines: Iterable[str], tz: Optional[timezone] = None) -> Iterator[dict]:
    """Yields `run_start`, `run_end` and `check` records in log order.

    A `check` is only emitted once its result line arrives, so a run cut off
    mid-combination (process killed, disk full) yields no half-record.

    Checks are held until their run closes, then released together, each
    carrying `occurrence` (which repeat of its label this is, 1-based) and
    `occurrence_total` (how many times that label appeared in the run). Older
    logs wrote only the centre, so one label can stand for several
    combinations, and position within the run is the only thing that tells them
    apart — but position is only meaningful against the total, which is not
    known until the run has finished. Run order is preserved: a run's checks are
    still emitted after its `run_start` and before its `run_end`.
    """
    route: Optional[str] = None
    pending_label: Optional[str] = None
    pending_ts: Optional[datetime] = None
    result: Optional[dict] = None       # a result still collecting continuations
    buffered: List[dict] = []           # this run's checks, awaiting their totals

    def flush():
        nonlocal result
        if result:
            out, result = result, None
            return out
        return None

    def close_run() -> List[dict]:
        """Numbers the buffered checks and hands them over."""
        nonlocal buffered
        if not buffered:
            return []
        totals = Counter(c["label"] for c in buffered)
        seen: Dict[str, int] = {}
        for check in buffered:
            label = check["label"]
            seen[label] = seen.get(label, 0) + 1
            check["occurrence"] = seen[label]
            check["occurrence_total"] = totals[label]
        out, buffered = buffered, []
        return out

    for raw in lines:
        line = raw.rstrip("\n")

        # Continuation of a multi-line result (extra applicant banners).
        if result is not None and not RE_LINE.match(line):
            if line.strip():
                result["message"] += "\n" + line.strip()
            continue

        done = flush()
        if done:
            buffered.append(done)

        m = RE_ROUTE_START.search(line)
        if m:
            # A run with no 'Route X OK.' line (killed mid-route) still releases
            # what it collected, and it is numbered against that partial run —
            # which is exactly why resolve() checks the total before trusting a
            # position.
            for check in close_run():
                yield check
            route = m.group(2)
            yield {"kind": "run_start", "route": route,
                   "ts": _parse_ts(m.group(1), tz)}
            pending_label = None
            continue

        m = RE_CREDENTIAL.search(line)
        if m:
            route = m.group(2)
            yield {"kind": "run_meta", "route": route, "account": m.group(3)}
            continue

        m = RE_PROXY.search(line)
        if m and route:
            yield {"kind": "run_meta", "route": route, "proxy": m.group(2)}
            continue

        m = RE_CHECKING.search(line)
        if m:
            pending_ts = _parse_ts(m.group(1), tz)
            pending_label = m.group(2).strip()
            continue

        m = RE_RESULT.search(line)
        if m and pending_label:
            # The result's own stamp is when the banner was READ; that is the
            # observation time, not when the dropdown selection started.
            result = {"kind": "check", "route": route, "label": pending_label,
                      "ts": _parse_ts(m.group(1), tz), "message": m.group(2).strip()}
            pending_label, pending_ts = None, None
            continue

        m = RE_ROUTE_END.search(line)
        if m and m.group(3) in _STATUSES:
            for check in close_run():
                yield check
            yield {"kind": "run_end", "route": m.group(2), "status": m.group(3),
                   "ts": _parse_ts(m.group(1), tz)}
            pending_label = None
            continue

    done = flush()
    if done:
        buffered.append(done)
    for check in close_run():
        yield check


def log_files(pattern: str = LOG_GLOB, days: Optional[int] = None,
              today: Optional[datetime] = None) -> list:
    """Log paths, oldest first, optionally limited to the last `days` days.

    Filtering is by the date in the filename (`app-2026-09-14.log`), which is
    exact and costs nothing — no need to open a file to find out it's too old.
    """
    paths = sorted(glob.glob(pattern))
    if not days:
        return paths

    now = today or datetime.now()
    cutoff = (now - timedelta(days=days - 1)).date()
    kept = []
    for path in paths:
        m = re.search(r"(\d{4}-\d\d-\d\d)", os.path.basename(path))
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date >= cutoff:
            kept.append(path)
    return kept


def read_file(path: str, tz: Optional[timezone] = None) -> Iterator[dict]:
    """Streams one log file's records. Unreadable files are logged and skipped."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for record in iter_events(f, tz):
                yield record
    except OSError as e:
        logging.error(f"Slot seed: cannot read '{path}': {e}")
