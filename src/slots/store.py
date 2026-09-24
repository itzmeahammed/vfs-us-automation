"""The only writer of the slot database.

Two layers, deliberately:

  * `SlotStore` — explicit, raises on error, takes a path. Tests and the seeder
    use this.
  * module-level `start_run` / `finish_run` / `record_check` — the bot's
    interface. Same operations against a process-wide store, wrapped so that a
    locked, missing or corrupt database can NEVER fail a route. Recording
    history is a bonus; checking slots is the job. The logs remain the source of
    truth, and a lost row can be recovered from them by the seeder.

Idempotency is in the schema, not in the callers: `checks` is UNIQUE on
`(combo_id, ts_utc)` and inserts use OR IGNORE, so re-running the seeder over
logs that overlap what the live path already wrote stores nothing new.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Optional

from src.slots import db, events, parse, registry

DEFAULT_DB_PATH = os.path.join("state", "slots.db")

# One warning per process per failure kind — a broken database must not turn the
# log into a wall of identical stack traces during a 10-route run.
_warned: set = set()


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logging.warning(message)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class SlotStore:
    """Read/write access to one slot database."""

    def __init__(self, conn: sqlite3.Connection, db_path: str = DEFAULT_DB_PATH):
        self.conn = conn
        self.db_path = db_path

    @classmethod
    def open(cls, db_path: str = DEFAULT_DB_PATH) -> "SlotStore":
        return cls(db.connect(db_path), db_path)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "SlotStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----- combinations ----------------------------------------------------

    def sync_combos(self, routes_dir: str = registry.ROUTES_DIR) -> Dict[str, int]:
        """Brings `combos` in line with config/routes. Safe to call repeatedly."""
        return registry.sync(self.conn, routes_dir)

    def combo_id_for(self, route: str, combo: dict) -> int:
        """The id for a structured combination, inserting it if it's new.

        Insert-if-new matters because the live path must never drop a reading:
        if a route file gains a combination mid-day, the check still lands, keyed
        by the same normalised identity the next `sync_combos` will use — so it
        updates that row rather than creating a twin.
        """
        centre = (combo.get("centre") or "").strip()
        category = (combo.get("category") or "").strip()
        sub_category = (combo.get("sub_category") or "").strip()
        city = registry._city(centre)
        key = registry.combo_key(route, city, category, sub_category)

        row = self.conn.execute(
            "SELECT id FROM combos WHERE combo_key = ?", (key,)
        ).fetchone()
        if row:
            return row["id"]

        source_code, dest_code = registry._route_codes(route)
        cur = self.conn.execute(
            "INSERT INTO combos (combo_key, route, source_code, dest_code,"
            " country_name, centre, city, category, sub_category, visa_type,"
            " purpose, config_label, enabled, in_config)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)",
            (key, route.upper(), source_code, dest_code,
             registry.DESTINATION_NAMES.get(dest_code, dest_code),
             centre, city, category, sub_category,
             registry.visa_type(category, sub_category),
             registry.purpose(category, sub_category),
             (combo.get("label") or "").strip()),
        )
        return cur.lastrowid

    # ----- runs ------------------------------------------------------------

    def start_run(self, route: str, *, started_at: Optional[datetime] = None,
                  account: Optional[str] = None, proxy: Optional[str] = None,
                  source: str = "live") -> Optional[int]:
        """Opens a run row and returns its id."""
        started = started_at or datetime.now(timezone.utc)
        source_code, dest_code = registry._route_codes(route)
        started_utc = _iso(started.astimezone(timezone.utc))
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO runs (route, source_code, dest_code,"
            " started_at_utc, status, account_masked, proxy_label, source)"
            " VALUES (?, ?, ?, ?, 'RUNNING', ?, ?, ?)",
            (route.upper(), source_code, dest_code, started_utc,
             account, proxy, source),
        )
        self.conn.commit()
        if cur.lastrowid and cur.rowcount:
            return cur.lastrowid
        row = self.conn.execute(
            "SELECT id FROM runs WHERE route = ? AND started_at_utc = ? AND source = ?",
            (route.upper(), started_utc, source),
        ).fetchone()
        return row["id"] if row else None

    def update_run_meta(self, run_id: Optional[int], *, account: Optional[str] = None,
                        proxy: Optional[str] = None) -> None:
        """Fills in the account/IP a run used, which the log reveals after it starts."""
        if not run_id:
            return
        sets, params = [], []
        if account:
            sets.append("account_masked = ?")
            params.append(account)
        if proxy:
            sets.append("proxy_label = ?")
            params.append(proxy)
        if not sets:
            return
        params.append(run_id)
        self.conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", params)

    def finish_run(self, run_id: Optional[int], status: str, *,
                   attempts: Optional[int] = None, error: Optional[str] = None,
                   finished_at: Optional[datetime] = None) -> None:
        if not run_id:
            return
        finished = finished_at or datetime.now(timezone.utc)
        self.conn.execute(
            "UPDATE runs SET status = ?, attempts = ?, error = ?, finished_at_utc = ?"
            " WHERE id = ?",
            (status, attempts, (error or "")[:500] or None,
             _iso(finished.astimezone(timezone.utc)), run_id),
        )
        self.conn.commit()

    # ----- checks ----------------------------------------------------------

    def _previous_state(self, combo_id: int, before_utc: str) -> Optional[dict]:
        """The combination's last informative check before `before_utc`.

        Bounded by `before_utc` rather than 'the latest row' so a backfill that
        inserts older checks still produces correct transitions.
        """
        row = self.conn.execute(
            "SELECT id, outcome FROM checks"
            " WHERE combo_id = ? AND ts_utc < ? AND outcome IN (?, ?, ?)"
            " ORDER BY ts_utc DESC LIMIT 1",
            (combo_id, before_utc, parse.SLOT, parse.WAITLIST, parse.NONE),
        ).fetchone()
        if not row:
            return None
        dates = {
            r["applicants"]: r["slot_date"]
            for r in self.conn.execute(
                "SELECT applicants, slot_date FROM slot_dates WHERE check_id = ?",
                (row["id"],),
            )
        }
        return {"outcome": row["outcome"], "dates": dates, "id": row["id"]}

    def record_check(self, route: str, message: str, *,
                     combo: Optional[dict] = None, label: Optional[str] = None,
                     ts: Optional[datetime] = None, run_id: Optional[int] = None,
                     source: str = "live", occurrence: Optional[int] = None,
                     occurrence_total: Optional[int] = None) -> Optional[int]:
        """Stores one combination reading. Returns the check id, or None if skipped.

        Give it either `combo` (the structured dict from the route file — the
        live path always has this) or `label` (all a log line carries). A label
        that cannot be resolved to a known combination is parked in
        `unmapped_labels` and the reading is dropped: a guess would corrupt a
        country's numbers, and the log line stays on disk to be re-imported once
        the route file is fixed.

        `occurrence` / `occurrence_total` come from the log reader and place the
        reading within its run, which is what lets an older log's bare centre
        label ('Abu Dhabi', two categories) be attributed at all. The live path
        never needs them: it passes `combo` and knows exactly what it read.
        """
        route = (route or "").upper()
        when = ts or datetime.now().astimezone()
        if when.tzinfo is None:
            when = when.astimezone()
        local = when
        utc = when.astimezone(timezone.utc)

        if combo is not None:
            combo_id = self.combo_id_for(route, combo)
        else:
            combo_id = registry.resolve(self.conn, route, label or "",
                                        occurrence=occurrence,
                                        occurrence_total=occurrence_total)
            if not combo_id:
                registry.record_unmapped(self.conn, route, label or "", _iso(utc))
                self.conn.commit()
                _warn_once(
                    f"unmapped:{route}:{label}",
                    f"Slot history: '{label}' on {route} matches no combination in "
                    "config/routes — reading not stored (fix the route file, then re-seed).",
                )
                return None

        outcome, dates = parse.parse_message(message)
        offset = local.utcoffset()
        offset_min = int(offset.total_seconds() // 60) if offset else 0
        ts_utc = _iso(utc)
        date_local = local.date().isoformat()

        cur = self.conn.execute(
            "INSERT OR IGNORE INTO checks (combo_id, run_id, ts_utc, ts_local,"
            " tz_offset_min, date_local, hour_local, weekday, outcome,"
            " error_reason, raw_message, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (combo_id, run_id, ts_utc, _iso(local), offset_min, date_local,
             local.hour, local.weekday(), outcome,
             parse.error_reason(message) if outcome == parse.ERROR else None,
             (message or "").strip(), source),
        )
        if not cur.rowcount:
            return None                      # already stored — nothing to redo
        check_id = cur.lastrowid

        if dates:
            self.conn.executemany(
                "INSERT OR REPLACE INTO slot_dates (check_id, applicants, slot_date, lead_days)"
                " VALUES (?, ?, ?, ?)",
                [(check_id, count, slot_date,
                  parse.lead_days(slot_date, date_local) or 0)
                 for count, slot_date in sorted(dates.items())],
            )

        self._record_events(combo_id, check_id, local, ts_utc, outcome, dates)

        self.conn.execute(
            "UPDATE combos SET first_seen_utc = MIN(COALESCE(first_seen_utc, ?), ?),"
            " last_seen_utc = MAX(COALESCE(last_seen_utc, ?), ?) WHERE id = ?",
            (ts_utc, ts_utc, ts_utc, ts_utc, combo_id),
        )
        self.conn.commit()
        return check_id

    def _record_events(self, combo_id: int, check_id: int, local: datetime,
                       ts_utc: str, outcome: str, dates: Dict[int, str]) -> None:
        prev = self._previous_state(combo_id, ts_utc)
        transitions = events.diff(prev, {"outcome": outcome, "dates": dates})
        if not transitions:
            return
        gap_hours = None
        if prev:
            prev_ts = self.conn.execute(
                "SELECT ts_utc FROM checks WHERE id = ?", (prev["id"],)
            ).fetchone()
            if prev_ts:
                delta = (datetime.fromisoformat(ts_utc)
                         - datetime.fromisoformat(prev_ts["ts_utc"]))
                gap_hours = round(delta.total_seconds() / 3600.0, 3)

        self.conn.executemany(
            "INSERT OR IGNORE INTO events (combo_id, check_id, ts_utc, ts_local,"
            " hour_local, weekday, kind, prev_outcome, new_outcome, prev_date,"
            " new_date, delta_days, gap_hours)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(combo_id, check_id, ts_utc, _iso(local), local.hour, local.weekday(),
              e["kind"], e["prev_outcome"], e["new_outcome"], e["prev_date"],
              e["new_date"], e["delta_days"], gap_hours) for e in transitions],
        )

    # ----- housekeeping ----------------------------------------------------

    def counts(self) -> Dict[str, int]:
        """Row counts per table — used by the seeder's summary and by tests."""
        out = {}
        for table in ("combos", "runs", "checks", "slot_dates", "events",
                      "unmapped_labels"):
            row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            out[table] = row["n"]
        return out


# ===== Process-wide, failure-isolated API (what the bot calls) =============

_store: Optional[SlotStore] = None
_store_failed = False
# The run currently in progress. Held here rather than threaded through
# vfs_bot's call chain: the slot checker is four calls deep from the supervisor
# and has no business carrying a database id through browser code. One process
# runs one route at a time (the run lock guarantees it), so a module-level
# current run is accurate, and a stale one can only cost a check its run link —
# never a wrong one, because it is cleared in a `finally`.
_current_run: Optional[int] = None


def set_current_run(run_id: Optional[int]) -> None:
    global _current_run
    _current_run = run_id


def current_run() -> Optional[int]:
    return _current_run


def db_path() -> str:
    """Database path: VFS_SLOTS_DB, then `[slots] db_path`, then state/slots.db.

    The env override exists so a one-off (or a test suite) can point the bot at
    a scratch copy without editing config — the same escape hatch VFS_PROXY and
    VFS_BOT_CONFIG_PATH already give.
    """
    env = os.environ.get("VFS_SLOTS_DB")
    if env:
        return env
    try:
        from src.utils.config_reader import get_config_value
        return get_config_value("slots", "db_path", DEFAULT_DB_PATH) or DEFAULT_DB_PATH
    except Exception:
        return DEFAULT_DB_PATH


def enabled() -> bool:
    """Whether the bot records history. `[slots] enabled = false` turns it off."""
    try:
        from src.utils.config_reader import get_config_value
        value = get_config_value("slots", "enabled", "true")
        return str(value).strip().lower() not in ("false", "0", "no", "off")
    except Exception:
        return True


def get_store() -> Optional[SlotStore]:
    """The process-wide store, or None if it can't be opened (already warned)."""
    global _store, _store_failed
    if _store is not None or _store_failed:
        return _store
    if not enabled():
        _store_failed = True
        return None
    try:
        _store = SlotStore.open(db_path())
        _store.sync_combos()
    except Exception as e:
        _store_failed = True
        _warn_once("open", f"Slot history disabled for this run — cannot open "
                           f"'{db_path()}': {e}")
        return None
    return _store


def reset() -> None:
    """Drops the cached store (tests, and after a config change)."""
    global _store, _store_failed
    if _store is not None:
        _store.close()
    _store = None
    _store_failed = False
    _warned.clear()


def start_run(route: str, **kwargs) -> Optional[int]:
    store = get_store()
    if not store:
        return None
    try:
        return store.start_run(route, **kwargs)
    except Exception as e:
        _warn_once("start_run", f"Slot history: could not open a run row: {e}")
        return None


def update_run_meta(run_id: Optional[int], **kwargs) -> None:
    if run_id is None:
        return
    store = get_store()
    if not store:
        return
    try:
        store.update_run_meta(run_id, **kwargs)
        store.conn.commit()
    except Exception as e:
        _warn_once("update_run_meta", f"Slot history: could not update run {run_id}: {e}")


def finish_run(run_id: Optional[int], status: str, **kwargs) -> None:
    if run_id is None:
        return
    store = get_store()
    if not store:
        return
    try:
        store.finish_run(run_id, status, **kwargs)
    except Exception as e:
        _warn_once("finish_run", f"Slot history: could not close run {run_id}: {e}")


def record_check(route: str, message: str, **kwargs) -> Optional[int]:
    store = get_store()
    if not store:
        return None
    kwargs.setdefault("run_id", current_run())
    try:
        return store.record_check(route, message, **kwargs)
    except Exception as e:
        _warn_once("record_check",
                   f"Slot history: could not record a check on {route}: {e}")
        return None
