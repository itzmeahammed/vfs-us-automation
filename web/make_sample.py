"""Regenerate `web/payload.sample.json` — the fixture the verify harness renders.

Real readings, the real builder, one adjustment: every check's timestamp is
shifted forward so the newest lands a few minutes ago.

*** THE SHIFT IS WHAT MAKES IT A USABLE FIXTURE, AND IT IS NOT A FAKE. ***

A card only shows an appointment date when its reading is under
`query.BOOKABLE_WITHIN_HOURS` old — a board promising a date in 88px type may
not do it off a stale reading. The checker runs 09:00–24:00, so a snapshot taken
at any other hour has no bookable card at all, and `verify/render.jsx` asserts
there is at least one (`bookable > 0`): the empty state and the headline are
mutually exclusive branches and the fixture has to exercise both. Taking the
snapshot straight from `state/slots.db` therefore depends on being run inside a
window that is open maybe a third of the day.

So the rows are real — the same centres, visa types, dates and outcomes VFS
actually returned, through `wall.build_payload` — read as if the last cycle had
just finished. Nothing is invented and no number is edited; only the clock moves.

    python web/make_sample.py
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.slots import db, store, wall                        # noqa: E402

OUT = os.path.join("web", "payload.sample.json")
#: Where the newest reading lands after the shift. Comfortably inside
#: BOOKABLE_WITHIN_HOURS, and not so fresh that "Seen 0m" reads as a stopped clock.
FRESH_MINUTES = 4


def _shift(stamp, delta: timedelta):
    """Move one stored timestamp, keeping the exact format it was stored in."""
    if not stamp:
        return stamp
    return (datetime.fromisoformat(stamp) + delta).isoformat(timespec="seconds")


def main(days: int = 7) -> None:
    source = store.db_path()
    work = os.path.join(tempfile.mkdtemp(prefix="board-sample-"), "slots.db")
    shutil.copy2(source, work)

    conn = sqlite3.connect(work)
    newest = conn.execute("SELECT MAX(ts_utc) FROM checks").fetchone()[0]
    if not newest:
        raise SystemExit(f"{source} holds no checks — nothing to sample.")
    target = datetime.now(timezone.utc) - timedelta(minutes=FRESH_MINUTES)
    delta = target - datetime.fromisoformat(newest)
    seconds = int(delta.total_seconds())

    # Shifted in Python, not with SQLite's datetime(): that returns
    # '2026-10-29 07:09:19' for an input of '2026-10-29T07:09:19+00:00', losing
    # both the T and the offset, and the readers then parse a naive datetime and
    # fail against an aware `now`.
    #
    # Both clocks move, or `ts_local` drifts away from `ts_utc` and the by-day
    # buckets stop lining up with the readings in them.
    for table in ("checks", "events"):
        # Newest first. `checks` is unique on (combo_id, ts_utc) and the shift
        # is forward, so a row moved before its later neighbour lands on a slot
        # that neighbour has not vacated yet. Descending, every target is free.
        rows = conn.execute(
            f"SELECT id, ts_utc, ts_local FROM {table}"
            " ORDER BY ts_utc DESC").fetchall()
        conn.executemany(
            f"UPDATE {table} SET ts_utc = ?, ts_local = ? WHERE id = ?",
            [(_shift(utc, delta), _shift(local, delta), rid)
             for rid, utc, local in rows])
    conn.commit()
    conn.close()

    read = db.connect(work, read_only=True)
    try:
        payload = wall.build_payload(read, days=days)
    finally:
        read.close()

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    cards = payload["views"][0]["cards"]
    centres = sum(len(c.get("centres", [])) for v in payload["views"]
                  for c in v.get("cards", []))
    print(f"{OUT}: {os.path.getsize(OUT) // 1024} KB, "
          f"shifted {seconds / 3600:+.1f}h, "
          f"{sum(1 for c in cards if c['has_date'])} bookable card(s), "
          f"{centres} centre row(s).")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 7)
