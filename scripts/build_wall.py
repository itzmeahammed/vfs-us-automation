"""Build the wall board — the big-screen view of the slot history.

    python -m scripts.build_wall                 # -> reports/slot_wall.html
    python -m scripts.build_wall --open          # and open it fullscreen-ready

The page reloads itself every two minutes, and the bot rewrites it after every
run, so a screen left on this URL stays current with no one touching it.
"""

import argparse
import os
import sys
import webbrowser

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.slots import query, wall  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=query.DEFAULT_DAYS,
                    help=f"window in days (default: {query.DEFAULT_DAYS})")
    ap.add_argument("--db", default=None, help="database path (default: state/slots.db)")
    ap.add_argument("--out", default=None, help="output HTML path")
    ap.add_argument("--open", action="store_true", dest="open_after",
                    help="open the wall in the default browser")
    args = ap.parse_args()

    path = wall.build(db_path=args.db, output=args.out, days=args.days)
    print(f"wall written: {path}  ({os.path.getsize(path) / 1024:.0f} KB, "
          f"last {args.days} days)")
    if args.open_after:
        webbrowser.open(f"file://{os.path.abspath(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
