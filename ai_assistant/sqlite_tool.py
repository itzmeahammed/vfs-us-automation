"""A read-only SQL tool over a SQLite database — the workhorse for analytics.

Pre-built tools answer the questions you thought of; SQL answers the rest
("which weekday did NOR open most in September?"). Letting a model write SQL
is only acceptable when the database itself refuses everything but reading, so
this does not rely on inspecting the query text. Four independent locks:

  1. the file is opened with `mode=ro` (the OS-level handle cannot write);
  2. `PRAGMA query_only = ON`;
  3. an AUTHORIZER that allows SELECT, column reads and a short list of pure
     functions, and denies everything else — writes, ATTACH, PRAGMA, temp
     tables, load_extension — at prepare time;
  4. a progress handler that aborts any statement past its deadline, and a row
     cap so one query cannot return the whole table.

`execute()` runs exactly one statement; a second one after `;` is an error.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Optional

from .tools import ToolError, ToolRegistry

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
if hasattr(sqlite3, "SQLITE_RECURSIVE"):
    _ALLOWED_ACTIONS.add(sqlite3.SQLITE_RECURSIVE)        # WITH RECURSIVE

#: Functions a query may call. Anything not listed (load_extension, fts/rtree
#: internals, user functions a host registered) is refused.
_ALLOWED_FUNCTIONS = {
    "abs", "avg", "count", "max", "min", "sum", "total", "group_concat", "string_agg",
    "round", "length", "lower", "upper", "trim", "ltrim", "rtrim", "substr", "substring",
    "replace", "instr", "coalesce", "ifnull", "nullif", "iif", "typeof", "cast",
    "date", "time", "datetime", "julianday", "strftime", "unixepoch", "printf", "format",
    "like", "glob", "hex", "quote", "char", "unicode", "sign", "floor", "ceil", "ceiling",
    "mod", "pow", "power", "sqrt", "log", "log10", "ln", "exp",
    "json", "json_extract", "json_array_length", "json_each", "json_type", "json_valid",
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile",
    "lag", "lead", "first_value", "last_value", "nth_value", "random",
}


def _authorizer(action, arg1, arg2, dbname, source):
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() not in _ALLOWED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and (dbname or "main") not in ("main", "temp"):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def schema_text(path: str, max_chars: int = 9_000) -> str:
    """CREATE statements (comments included — they document the columns)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT sql FROM sqlite_master WHERE type IN ('table','view') "
            "AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    finally:
        con.close()
    text = ";\n".join(r[0].strip() for r in rows) + ";"
    return text[:max_chars]


class ReadOnlySQLite:
    def __init__(self, path: str, *, max_rows: int = 200, timeout_s: float = 8.0,
                 max_cell_chars: int = 400):
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        self.path = os.path.abspath(path)
        self.max_rows = max_rows
        self.timeout_s = timeout_s
        self.max_cell_chars = max_cell_chars

    def _connect(self) -> sqlite3.Connection:
        uri = "file:" + self.path.replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
        con.execute("PRAGMA query_only = ON")
        con.set_authorizer(_authorizer)
        return con

    def execute(self, query: str, max_rows: Optional[int] = None) -> dict:
        query = (query or "").strip().rstrip(";").strip()
        if not query:
            raise ToolError("empty query")
        head = query.split(None, 1)[0].lower()
        if head not in ("select", "with", "values"):
            raise ToolError("only SELECT / WITH queries are allowed")
        limit = min(max_rows or self.max_rows, self.max_rows)
        deadline = time.monotonic() + self.timeout_s
        con = self._connect()
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2_000)
        try:
            try:
                cur = con.execute(query)
            except sqlite3.DatabaseError as e:
                msg = str(e)
                if "not authorized" in msg:
                    msg = ("not authorized: only reads of the main database and the "
                           "allowed functions are permitted")
                elif "interrupted" in msg:
                    msg = f"query exceeded {self.timeout_s:.0f}s; add filters or a LIMIT"
                raise ToolError(msg) from None
            cols = [d[0] for d in (cur.description or [])]
            try:
                rows = cur.fetchmany(limit + 1)
            except sqlite3.OperationalError as e:
                if "interrupted" in str(e):
                    raise ToolError(f"query exceeded {self.timeout_s:.0f}s; "
                                    "add filters or a LIMIT") from None
                raise ToolError(str(e)) from None
        finally:
            con.close()
        truncated = len(rows) > limit
        rows = rows[:limit]

        def cell(v):
            if isinstance(v, str) and len(v) > self.max_cell_chars:
                return v[:self.max_cell_chars] + "…"
            if isinstance(v, bytes):
                return f"<{len(v)} bytes>"
            return v
        return {"columns": cols, "rows": [[cell(v) for v in r] for r in rows],
                "row_count": len(rows), "truncated": truncated,
                **({"note": f"only the first {limit} rows; aggregate or add LIMIT"}
                   if truncated else {})}


def register_sqlite_tool(registry: ToolRegistry, path: str, *, name: str = "run_sql",
                         about: str = "", max_rows: int = 200,
                         timeout_s: float = 8.0) -> ReadOnlySQLite:
    """Add a `run_sql` tool whose description carries the live schema."""
    db = ReadOnlySQLite(path, max_rows=max_rows, timeout_s=timeout_s)
    schema = schema_text(path)
    description = (
        "Run ONE read-only SQLite query (SELECT or WITH) and get rows back. Use it for "
        "any analysis the other tools do not cover: grouping, filtering by date/hour/"
        f"weekday, joins. At most {max_rows} rows are returned — aggregate in SQL "
        "rather than fetching raw rows. SQLite dialect: use date()/strftime(), not "
        "NOW() or EXTRACT().\n"
        + (about.strip() + "\n" if about else "")
        + "Schema:\n" + schema)
    registry.tool(
        name, description=description, timeout_s=timeout_s + 2,
        parameters={"type": "object",
                    "properties": {
                        "query": {"type": "string", "maxLength": 4000,
                                  "description": "A single SELECT/WITH statement."},
                        "max_rows": {"type": "integer", "minimum": 1, "maximum": max_rows}},
                    "required": ["query"]},
    )(lambda query, max_rows=None: db.execute(query, max_rows))
    return db
