#!/usr/bin/env bash
#
# EC2 entrypoint for the VFS Malta slot checker — invoke this from cron hourly.
#
# It wraps the self-healing supervisor with:
#   * xvfb-run — a fresh virtual X display per run (real/headed Chrome needs a
#                display; -a auto-picks a free display number) torn down after.
#
# The supervisor itself launches & kills Chrome and retries on failure, so this
# script stays thin.
#
# Crontab (hourly):
#   0 * * * * /opt/vfs-malta-slot-checker/run_ec2.sh >> /opt/vfs-malta-slot-checker/app.log 2>&1
#
# Prerequisites on the box:
#   sudo apt update
#   sudo apt install -y xvfb google-chrome-stable   # or chromium
#   python3 -m venv .venv && . .venv/bin/activate
#   pip install -r requirements.txt
#   python -m playwright install chromium            # Playwright client deps
#
set -euo pipefail

# Resolve the project dir (this script's location) so cron's CWD doesn't matter.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Prefer the venv python if present, else system python3.
if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="python3"
fi

# OVERLAP GUARD: owned by PYTHON, not by this script. src/supervisor.py flocks
# the same /tmp/vfs-slot-checker.lock itself (src/utils/runlock.py) and skips the
# tick if another browser-driving run holds it.
#
# This script used to flock it here, before exec'ing the child - which deadlocked
# once the supervisor started taking it too: the parent held the lock, so the
# child could never get it and every tick skipped while this script still printed
# "Run finished". Do not reintroduce a lock here.
#
# Python has to be the owner rather than this script, because this script cannot
# see a waitlist run started by the API or by hand - and those drive a browser too.

echo "[$(date '+%F %T')] Starting VFS slot-check run (xvfb + supervisor)..."

# xvfb-run -a : fresh auto-numbered virtual display for headed Chrome.
# Pass a reasonable screen size so the page renders at a desktop viewport.
xvfb-run -a --server-args="-screen 0 1280x1024x24" \
  "$PYTHON" -m src.supervisor "$@"

echo "[$(date '+%F %T')] Run finished."
