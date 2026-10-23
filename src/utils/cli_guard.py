"""Entry-point guard: no traceback ever reaches the console.

The console IS an API job's log, which the operator reads. So every
`python -m src.<x>` entry point runs its main() through run():

    sys.exit(cli_guard.run(main))

* stdout/stderr are switched to UTF-8 with errors="replace". The Windows
  console default (cp1252) cannot print a VFS page's "✓", and printing the
  report crashed a finished run on 2026-10-23 (job 9989e42c).
* Any exception that escapes becomes ONE timestamped log line: the VFS
  sentence from public_errors when it is recognised, otherwise UNIDENTIFIED.
  The full traceback goes to logs/crashes.log, never the console.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from datetime import datetime
from typing import Any, Callable

from src.utils.public_errors import UNIDENTIFIED, public_message

CRASH_LOG = os.path.join("logs", "crashes.log")


def utf8_stdio() -> None:
    """Make print() unable to fail on a character the console cannot show."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                   # noqa: BLE001
            pass


def _run_id() -> str:
    try:
        from src.utils.run_context import run_id
        return run_id()
    except Exception:                                       # noqa: BLE001
        return ""


def _save_traceback(exc: BaseException) -> None:
    try:
        os.makedirs(os.path.dirname(CRASH_LOG), exist_ok=True)
        with open(CRASH_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"=== {datetime.now():%Y-%m-%d %H:%M:%S} run={_run_id()} "
                     f"argv={' '.join(sys.argv)}\n")
            fh.write("".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)))
            fh.write("\n")
    except Exception:                                       # noqa: BLE001
        pass


def report_crash(exc: BaseException) -> int:
    """Log one line for an escaped exception; keep the traceback on disk."""
    _save_traceback(exc)
    message = public_message(f"{type(exc).__name__}: {exc}",
                             fallback=UNIDENTIFIED)
    line = f"{message} (details in {CRASH_LOG}, run {_run_id()})"
    try:
        if logging.getLogger().handlers:
            # No exc_info: a plain basicConfig formatter would print it.
            logging.getLogger("vfs.crash").error(line)
        else:
            print(f"{datetime.now():%H:%M:%S} | ERROR    | {line}",
                  file=sys.stderr)
    except Exception:                                       # noqa: BLE001
        pass
    return 1


def _thread_hook(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is not None and not isinstance(args.exc_value, SystemExit):
        report_crash(args.exc_value)


def _sys_hook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    report_crash(exc)


def run(main: Callable[..., Any], *args: Any) -> Any:
    """Run an entry point's main(); an escaped exception exits 1, one line."""
    utf8_stdio()
    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    try:
        return main(*args)
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:                            # noqa: BLE001
        return report_crash(exc)
