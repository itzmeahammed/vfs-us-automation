"""SQLite connection + migrations for the slot history.

Every other module in `src.slots` goes through `connect()`; nothing else in the
project opens the database file directly.

Concurrency: the bot is the only writer and writes a handful of rows per minute,
while the dashboard only reads. WAL mode plus a busy timeout is therefore
enough — a reader never blocks the bot, and two writers (a run plus a manual
seed) queue instead of failing.

Migrations are forward-only and numbered. `schema.sql` is migration 1 and is
applied with IF NOT EXISTS throughout, so pointing this at an existing database
is a no-op. To change the schema, append to MIGRATIONS — never edit a shipped
entry, because databases in the field have already applied it.
"""

import logging
import os
import sqlite3
from typing import Callable, List, Tuple

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

# Version 1 is schema.sql, loaded from disk; later migrations are callables so
# they can inspect the database and stay idempotent.
def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column
               for row in conn.execute(f"PRAGMA table_info({table})"))


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """ALTER TABLE ADD COLUMN, but only when the column isn't already there.

    A column added in a later migration is ALSO written into schema.sql, so that
    a fresh database gets the current shape in one step. That means the ALTER
    must be a no-op on a database created from today's schema.sql, and only do
    real work on one created before the column existed. Every migration here has
    to be safe to run against both.
    """
    if not _has_column(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# Each entry: (version, callable taking the connection).
_LATER_MIGRATIONS: List[Tuple[int, Callable[[sqlite3.Connection], None]]] = [
    # 2: what a combination is FOR (tourist / business / any purpose), so the
    # board can be filtered the way an agent thinks about a client. Derived from
    # the category text; existing rows are filled in by the next registry.sync().
    (2, lambda conn: (
        _add_column(conn, "combos", "purpose", "TEXT NOT NULL DEFAULT 'any'"),
        conn.execute("CREATE INDEX IF NOT EXISTS idx_combos_purpose ON combos(purpose)"),
    )),
    # 3: a combination's position in its route file, so a log line carrying only
    # a bare centre can be attributed by position (the bot checks in config
    # order). Filled in by the next registry.sync(); 0 until then, which is the
    # same as "unknown" and simply leaves those labels unresolved.
    (3, lambda conn: (
        _add_column(conn, "combos", "config_order", "INTEGER NOT NULL DEFAULT 0"),
    )),
    # 4: `checks.run_id` was only ever read one row at a time. Coverage asks the
    # opposite question in bulk — which runs produced no readings at all — and
    # without an index that is a scan of every check for every run.
    (4, lambda conn: (
        conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_run ON checks(run_id)"),
    )),
]


def _schema_sql() -> str:
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        return f.read()


def connect(db_path: str, *, read_only: bool = False) -> sqlite3.Connection:
    """Opens (and migrates) the slot database, returning a configured connection.

    The caller owns the connection and should close it — or use it as a context
    manager for transaction scope, which is what store.py does.
    """
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=15.0, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    # WAL: readers (dashboard) never block the writer (the bot).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    # NORMAL is the right trade for derived data: survives process crashes, and
    # the logs remain the ultimate source of truth if a power cut loses the tail.
    conn.execute("PRAGMA synchronous=NORMAL")
    if not read_only:
        migrate(conn)
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return (row["v"] or 0) if row else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Applies any migrations the database hasn't seen. Returns the new version."""
    version = _current_version(conn)

    if version < 1:
        conn.executescript(_schema_sql())
        conn.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (1)")
        version = 1
        logging.debug("Slot database initialised at schema version 1.")

    for target, apply in _LATER_MIGRATIONS:
        if target <= version:
            continue
        apply(conn)
        conn.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (target,))
        version = target
        logging.info(f"Slot database migrated to schema version {target}.")

    conn.commit()
    return version
