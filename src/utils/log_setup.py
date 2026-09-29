"""One logging setup for every entry point, with a run_id on every line.

    ═════════════════ THE TWO SINKS, AND WHY BOTH ═════════════════

    console   human format, unchanged   an operator watching a run
    JSONL     one object per line       a machine answering "what happened?"

The JSONL file is the new part and the reason this module exists. A text log is
only greppable for strings a human guessed in advance; a JSONL log with a
`run_id` on every record can be queried:

    jq 'select(.run_id=="6abba6b53f34")' logs/app.jsonl

That is the difference between "I think this is the right run" and knowing.

    ═════════════════ WHY A FILTER, NOT A FORMATTER ═════════════════

The run_id is injected by a logging.Filter rather than looked up inside the
formatter. Filters run once per record and mutate it, so BOTH handlers see the
id — the console formatter can show it too, and any future handler inherits it
for free. Doing it in a formatter would bind the id to one sink.

Redaction is deliberately left where it already lives (waitlist/redaction.py
installs a filter on the root logger). This module must not duplicate it: two
redaction filters would be two places to update when a new secret appears, and
the one that got missed would be the one that leaked.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from src.utils.run_context import run_id

#: Everything a JSON record carries beyond the message. Kept explicit rather
#: than dumping record.__dict__, which would include unserialisable args and
#: leak whatever a caller happened to attach.
_STANDARD = (
    "name", "levelname", "pathname", "lineno", "funcName",
    "created", "msecs", "thread", "process",
)


class RunIdFilter(logging.Filter):
    """Stamps every record with the current run id. Never rejects a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        # getattr guard: a record may already carry an explicit run_id from a
        # caller logging on behalf of a different run (the API supervisor
        # reporting a child's outcome). Theirs wins.
        if not getattr(record, "run_id", None):
            record.run_id = run_id()
        return True


class JsonLinesFormatter(logging.Formatter):
    """One JSON object per record, newline-delimited.

    Never raises. A formatter that throws takes the log line AND, in some
    handler configurations, the calling frame's error handling with it — so a
    value that will not serialise is stringified rather than fatal.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "run_id": getattr(record, "run_id", ""),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        # Caller-attached extras, e.g. log.info("...", extra={"route": "AE-NOR"}).
        # This is what makes the log queryable by route or client rather than
        # only by free text.
        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_") or key in payload:
                continue
            # `message` and `asctime` are set ON THE RECORD by any formatter
            # that ran before this one (the console handler's). They duplicate
            # payload["msg"] and payload["ts"], so they are dropped here rather
            # than shipped twice in every record.
            if key in ("msg", "args", "exc_info", "exc_text", "stack_info",
                       "levelno", "relativeCreated", "module", "filename",
                       "threadName", "processName", "taskName",
                       "message", "asctime"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:                                    # noqa: BLE001
            return json.dumps({"ts": payload["ts"], "level": record.levelname,
                               "run_id": payload["run_id"],
                               "msg": "<unserialisable log record>"})


def setup(
    *,
    verbose: bool = False,
    text_log: Optional[str] = None,
    json_log: str = os.path.join("logs", "app.jsonl"),
    console: bool = True,
) -> str:
    """Configure the root logger. Returns the run id now in force.

    Idempotent per process in the sense that matters: it REPLACES the root
    handlers rather than adding to them, so an entry point that calls this
    after something else called basicConfig does not double-log.

    Args:
        verbose: DEBUG instead of INFO.
        text_log: optional human-readable file, e.g. logs/booking.log. Kept
            because existing runbooks tell people to tail it.
        json_log: the queryable log. This is the one to keep.
        console: write to stderr as well.
    """
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)

    run_filter = RunIdFilter()
    handlers: List[logging.Handler] = []

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%H:%M:%S"))
        handlers.append(stream)

    for path, formatter in (
        (text_log, logging.Formatter(
            "%(asctime)s | %(run_id)s | %(levelname)-8s | %(message)s",
            datefmt="%H:%M:%S")),
        (json_log, JsonLinesFormatter()),
    ):
        if not path:
            continue
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            file_handler = logging.FileHandler(path, encoding="utf-8")
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)
        except OSError as exc:
            # A log we cannot open must not stop a run — but say so on stderr,
            # because a silent loss of the JSONL log is a silent loss of the
            # only queryable record.
            print(f"(could not open log {path}: {exc})", file=sys.stderr)

    for handler in handlers:
        handler.addFilter(run_filter)
        root.addHandler(handler)

    return run_id()
