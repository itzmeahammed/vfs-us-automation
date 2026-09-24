"""Backfill the slot database from the bot's log files.

    python -m scripts.seed_from_logs                 # last 7 days
    python -m scripts.seed_from_logs --days 30
    python -m scripts.seed_from_logs --all           # every log on disk
    python -m scripts.seed_from_logs --db /tmp/x.db --dry-run

Safe to re-run: nothing is stored twice.
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.slots import logreader, seed, store as store_mod  # noqa: E402
from src.slots.store import SlotStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7,
                    help="how many days of logs to import (default: 7)")
    ap.add_argument("--all", action="store_true", help="import every log file")
    ap.add_argument("--db", default=None, help="database path (default: state/slots.db)")
    ap.add_argument("--logs", default=logreader.LOG_GLOB, help="log file glob")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, but roll back instead of committing")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    db_path = args.db or store_mod.db_path()
    with SlotStore.open(db_path) as store:
        combo_stats = store.sync_combos()
        print(f"combinations from config/routes: {combo_stats['inserted']} new, "
              f"{combo_stats['updated']} updated, {combo_stats['retired']} retired")

        if args.all:
            paths = logreader.log_files(args.logs)
            stats = seed.seed(store, paths)
        else:
            stats = seed.seed_recent(store, days=args.days, pattern=args.logs)

        if args.dry_run:
            store.conn.rollback()
            print("(dry run — rolled back)")

        print(f"log files read : {stats['files']}")
        print(f"route runs     : {stats['runs']}")
        print(f"checks found   : {stats['checks']}")
        print(f"  stored       : {stats['stored']}")
        print(f"  already there: {stats['duplicates']}")

        if stats["unmapped"]:
            print(f"\n{stats['unmapped']} label(s) could not be matched to "
                  "config/routes — not stored:")
            for row in store.conn.execute(
                    "SELECT route, label, hits FROM unmapped_labels"
                    " ORDER BY hits DESC LIMIT 20"):
                print(f"  {row['route']:8} x{row['hits']:<5} {row['label']}")
            print("Fix the route file (or add the centre), then re-run to import them.")

        counts = store.counts()
        print(f"\ndatabase now: {counts['checks']} checks, {counts['slot_dates']} "
              f"quoted dates, {counts['events']} events, across "
              f"{counts['combos']} combinations ({db_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
