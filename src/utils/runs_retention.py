"""Retention for runs/ — keep the evidence, drop the proof-of-health.

    ═════════════════ WHAT runs/ HOLDS ═════════════════

Two writers put files here, in two shapes (see walk.capture_dir and
diagnostics.SCREENSHOT_DIR):

    runs/<ROUTE>/<run_id>/     the booking walk, plus payment/ under it
    runs/<run_id>/             slot check, login/OTP, waitlist register

The shape says WHO wrote the file, not whether it is worth keeping, so
retention cannot be done by folder. On 2026-10-26 runs/ held 711 MB: the four
route folders were 16 MB of walk output, and the 474 bare folders held
everything that matters — geo blocks, account locks, turnstile failures, and
the waitlist pages that diagnosed the Italy form. Deleting by shape would have
removed the evidence and kept the debris.

    ═════════════════ WHAT IS SAFE TO DELETE ═════════════════

A file is prunable only if it is a MILESTONE: a picture of a run that was going
fine, taken beside a log line that already says the same thing in words. There
are exactly three, and they came to 11,502 files / 548 MB against 113 MB of
everything else.

Everything else is EVIDENCE: a state that cannot be reproduced. A blocked IP, a
locked account, a half-filled waitlist form, a payment page. Those are why this
directory exists.

    ═════════════════ THE ALLOWLIST IS THE SAFETY PROPERTY ═════════════════

MILESTONE_NAMES lists what may be deleted. It is not a list of what to keep.
A screenshot name this module has never seen is KEPT, so adding a new failure
capture upstream can never silently start deleting it — the worst case is that
it is kept too long, which costs disk. The reverse costs the only copy.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import List

log = logging.getLogger(__name__)

RUNS_DIR = "runs"

#: The ONLY screenshots this module may delete — the three written on a run
#: that was going fine. Since 2026-10-26 they are not written at all unless
#: [browser] milestone_screenshots is on; this clears what is already there,
#: and keeps clearing it if someone turns the switch back on.
MILESTONE_NAMES = frozenset({"after_signin", "dashboard", "final"})

#: 20261026_123553_NOR_after_signin.png -> "after_signin". The route code is
#: optional because diagnostics omits it before VfsBot.run() sets one.
_STAMPED = re.compile(
    r"^\d{8}_\d{6}_(?:[A-Z]{2,4}_)?(?P<name>.+)\.png$", re.IGNORECASE)


@dataclass
class PruneReport:
    """What a prune did, or would do. Printed by the CLI, logged on startup."""

    files_removed: int = 0
    bytes_removed: int = 0
    dirs_removed: int = 0
    kept_evidence: int = 0
    kept_recent: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def megabytes(self) -> float:
        return self.bytes_removed / (1024 * 1024)

    def summary(self, dry_run: bool = False) -> str:
        verb = "would remove" if dry_run else "removed"
        return (f"runs/: {verb} {self.files_removed} milestone screenshot(s) "
                f"({self.megabytes:.0f} MB) and {self.dirs_removed} empty "
                f"folder(s); kept {self.kept_evidence} evidence file(s) and "
                f"{self.kept_recent} recent milestone(s).")


def milestone_name(filename: str) -> str:
    """The milestone this filename is, or "" if it is evidence.

    Anything that does not parse as a stamped screenshot is evidence by
    definition — a DOM dump, a payment capture, a file written by hand.
    """
    match = _STAMPED.match(filename)
    if not match:
        return ""
    name = match.group("name")
    return name if name in MILESTONE_NAMES else ""


def prune_runs(root: str = RUNS_DIR, *, max_age_days: int = 7,
               dry_run: bool = False) -> PruneReport:
    """Delete milestone screenshots older than `max_age_days`, then empty dirs.

    Never raises: this runs at startup, and housekeeping must not be able to
    stop the server. `max_age_days <= 0` prunes every milestone regardless of
    age; a recent one is kept because a run that just happened is the one
    someone is most likely to be looking at right now.
    """
    report = PruneReport()
    if not os.path.isdir(root):
        return report

    cutoff = time.time() - max(max_age_days, 0) * 86400

    for directory, _subdirs, files in os.walk(root):
        for filename in files:
            path = os.path.join(directory, filename)
            if not milestone_name(filename):
                report.kept_evidence += 1
                continue
            try:
                stat = os.stat(path)
                if max_age_days > 0 and stat.st_mtime >= cutoff:
                    report.kept_recent += 1
                    continue
                if not dry_run:
                    os.remove(path)
                report.files_removed += 1
                report.bytes_removed += stat.st_size
            except OSError as exc:
                report.errors.append(f"{path}: {exc}")

    report.dirs_removed = _remove_empty_dirs(root, dry_run, report)
    return report


def _remove_empty_dirs(root: str, dry_run: bool, report: PruneReport) -> int:
    """Drop run folders left with nothing in them. Bottom-up, never `root`.

    Most were empty before this module existed: VfsBot creates its run folder
    when Chrome starts, before anything has decided there is evidence to write.
    """
    removed = 0
    for directory, _subdirs, _files in os.walk(root, topdown=False):
        if os.path.abspath(directory) == os.path.abspath(root):
            continue
        try:
            if not os.listdir(directory):
                if not dry_run:
                    os.rmdir(directory)
                removed += 1
        except OSError as exc:
            report.errors.append(f"{directory}: {exc}")
    return removed


def main(argv: List[str] | None = None) -> int:
    """`python -m src.utils.runs_retention [--days N] [--apply]`.

    DRY RUN BY DEFAULT. Deleting the only copy of a failure capture is not
    undoable, so the flag that deletes is the one you have to type.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Prune milestone screenshots from runs/.")
    parser.add_argument("--days", type=int, default=7,
                        help="keep milestones newer than this (default 7; "
                             "0 removes them all)")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete (default: report only)")
    parser.add_argument("--root", default=RUNS_DIR)
    args = parser.parse_args(argv)

    report = prune_runs(args.root, max_age_days=args.days,
                        dry_run=not args.apply)
    print(report.summary(dry_run=not args.apply))
    for problem in report.errors[:10]:
        print(f"  ! {problem}")
    if not args.apply:
        print("  (dry run — add --apply to delete)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
