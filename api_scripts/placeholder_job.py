"""Placeholder job — stands in for the real waitlist run.

It does exactly what a real job does from the API's point of view: prints to
stdout over time, honours argv, and exits with a status code. That is enough to
verify the whole chain (web app -> ngrok -> FastAPI -> subprocess) end to end
without touching a VFS account.

Swap it for the real thing by pointing VFSAPI_JOB_COMMAND at the waitlist CLI:
    $env:VFSAPI_JOB_COMMAND = '["python","-m","src.waitlist","run"]'
The API appends --route/--registrant/--combo/--dry-run|--live itself, so the
configured command is just the prefix.

Exit codes:
    0  success
    1  simulated failure   (pass --fail to test the API's failure path)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone


def log(message: str) -> None:
    """Print with a timestamp and flush, so a tailed log stays live."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def main() -> int:
    """Simulate a job. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="placeholder_job.py",
        description="Fake waitlist job used to test the webhook end to end.",
    )
    # These mirror the real waitlist CLI so the API's argv construction is
    # exercised exactly as it will be in production.
    parser.add_argument("--route", help="e.g. AE-DEU")
    parser.add_argument("--registrant", help="Client id.")
    parser.add_argument("--combo", help="Centre/category combination.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Simulate only.")
    mode.add_argument("--live", action="store_true", help="Real run.")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation.")
    parser.add_argument("--fail", action="store_true", help="Exit 1, to test errors.")
    parser.add_argument(
        "--duration", type=float, default=10.0, help="Seconds to run (default 10)."
    )
    args = parser.parse_args()

    log("=" * 60)
    log("PLACEHOLDER JOB STARTED")
    log(f"  pid          : {os.getpid()}")
    log(f"  triggered by : {os.environ.get('VFS_TRIGGERED_BY', 'manual')}")
    log(f"  route        : {args.route or '(all)'}")
    log(f"  registrant   : {args.registrant or '(all)'}")
    log(f"  combo        : {args.combo or '(all)'}")
    log(f"  mode         : {'LIVE' if args.live else 'DRY-RUN'}")
    log("=" * 60)

    # Confirm the secret never leaked into the child environment.
    if "VFSAPI_SECRET_TOKEN" in os.environ:
        log("WARNING: the API secret is visible to this child process!")
    else:
        log("OK: API secret is not present in the job environment.")

    steps = max(1, int(args.duration))
    for i in range(1, steps + 1):
        log(f"working... step {i}/{steps}")
        time.sleep(args.duration / steps)

    if args.fail:
        log("Simulated failure requested (--fail). Exiting 1.")
        return 1

    log("PLACEHOLDER JOB COMPLETED SUCCESSFULLY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
