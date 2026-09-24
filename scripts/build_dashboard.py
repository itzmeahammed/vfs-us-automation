"""Build the agent slot dashboard from the slot database.

    python -m scripts.build_dashboard                  # -> reports/slot_dashboard.html
    python -m scripts.build_dashboard --days 14
    python -m scripts.build_dashboard --open           # and open it in a browser
"""

import argparse
import os
import sys
import webbrowser

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.slots import dashboard, query  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=query.DEFAULT_DAYS,
                    help=f"window in days (default: {query.DEFAULT_DAYS})")
    ap.add_argument("--db", default=None, help="database path (default: state/slots.db)")
    ap.add_argument("--out", default=None, help="output HTML path")
    ap.add_argument("--open", action="store_true", dest="open_after",
                    help="open the page in the default browser when done")
    args = ap.parse_args()

    path = dashboard.build(db_path=args.db, output=args.out, days=args.days)
    size_kb = os.path.getsize(path) / 1024.0
    print(f"dashboard written: {path}  ({size_kb:.0f} KB, last {args.days} days)")
    if args.open_after:
        webbrowser.open(f"file://{os.path.abspath(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
