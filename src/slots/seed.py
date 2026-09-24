"""Backfills the slot database from the bot's log files.

Re-runnable by design: every insert is idempotent, so running this twice — or
running it over days the live recorder already covered — stores nothing new.
That matters because the natural way to use it is a cron line that re-seeds the
last day or two to pick up anything a crashed run never wrote.

Records are fed through `store.record_check`, the same entry point the live path
uses, so a backfilled row is indistinguishable from a live one except for its
`source` column.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from src.slots import logreader
from src.slots.store import SlotStore


def seed(store: SlotStore, paths: List[str], *,
         tz: Optional[timezone] = None,
         since: Optional[datetime] = None) -> Dict[str, int]:
    """Imports every check in `paths`. Returns a summary for the CLI."""
    stats = {"files": 0, "runs": 0, "checks": 0, "stored": 0, "duplicates": 0,
             "unmapped": 0}
    open_runs: Dict[str, int] = {}
    pending_meta: Dict[str, dict] = {}

    for path in paths:
        stats["files"] += 1
        for record in logreader.read_file(path, tz):
            ts = record.get("ts")
            if since and ts and ts < since:
                continue
            kind = record["kind"]
            route = record.get("route")
            if not route:
                continue

            if kind == "run_start":
                run_id = store.start_run(route, started_at=ts, source="backfill")
                if run_id:
                    open_runs[route] = run_id
                    stats["runs"] += 1
                pending_meta.pop(route, None)

            elif kind == "run_meta":
                meta = {k: v for k, v in record.items()
                        if k in ("account", "proxy")}
                run_id = open_runs.get(route)
                if run_id:
                    store.update_run_meta(run_id, **meta)
                else:
                    pending_meta.setdefault(route, {}).update(meta)

            elif kind == "check":
                stats["checks"] += 1
                check_id = store.record_check(
                    route, record["message"], label=record["label"], ts=ts,
                    run_id=open_runs.get(route), source="backfill",
                    occurrence=record.get("occurrence"),
                    occurrence_total=record.get("occurrence_total"))
                if check_id:
                    stats["stored"] += 1
                else:
                    # Either already present, or a label we refuse to guess at.
                    # Both are non-events for the caller; the split is only for
                    # the summary, and unmapped_labels records the detail.
                    stats["duplicates"] += 1

            elif kind == "run_end":
                run_id = open_runs.pop(route, None)
                if run_id:
                    store.finish_run(run_id, record["status"], finished_at=ts)

    row = store.conn.execute("SELECT COUNT(*) AS n FROM unmapped_labels").fetchone()
    stats["unmapped"] = row["n"] if row else 0
    return stats


def seed_recent(store: SlotStore, days: int = 7, *,
                pattern: str = logreader.LOG_GLOB,
                tz: Optional[timezone] = None) -> Dict[str, int]:
    """Seeds the last `days` days of logs (today counts as day 1)."""
    paths = logreader.log_files(pattern, days=days)
    if not paths:
        logging.warning(f"Slot seed: no log files matched '{pattern}'.")
        return {"files": 0, "runs": 0, "checks": 0, "stored": 0,
                "duplicates": 0, "unmapped": 0}
    since = datetime.now().astimezone() - timedelta(days=days)
    return seed(store, paths, tz=tz, since=since)
