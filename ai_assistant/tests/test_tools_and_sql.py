import json
import sqlite3
import time

import pytest

from ai_assistant.redact import Redactor
from ai_assistant.sqlite_tool import ReadOnlySQLite, register_sqlite_tool
from ai_assistant.tools import ToolError, ToolRegistry


def call(reg, name, args, **kw):
    return reg.call(name, json.dumps(args), redactor=Redactor(salt="t"),
                    default_timeout_s=kw.get("timeout", 5), max_chars=kw.get("max_chars", 5000))


@pytest.fixture
def reg():
    r = ToolRegistry()

    @r.tool(description="echo", parameters={"type": "object", "properties": {
        "n": {"type": "integer", "minimum": 1, "maximum": 10},
        "kind": {"type": "string", "enum": ["a", "b"]},
        "day": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}}, "required": ["n"]})
    def echo(n, kind="a", day=None):
        return {"n": n, "kind": kind, "who": "ops@example.com", "password": "pw"}

    @r.tool(description="slow")
    def slow():
        time.sleep(3)

    @r.tool(description="boom")
    def boom():
        raise ToolError("nothing for that day")

    @r.tool(description="big")
    def big():
        return "x" * 10_000

    @r.tool(description="tuple keys")
    def grid():
        return {(0, 9): 1}
    return r


def test_valid_call_is_redacted(reg):
    res = call(reg, "echo", {"n": "3"})
    body = json.loads(res.content)
    assert res.ok and body["n"] == 3
    assert body["password"] == "[redacted]" and body["who"].startswith("acct-")


@pytest.mark.parametrize("args,needle", [
    ({}, "missing required"), ({"n": 0}, ">= 1"), ({"n": 11}, "<= 10"),
    ({"n": 1, "kind": "c"}, "one of"), ({"n": 1, "zzz": 1}, "unknown argument"),
    ({"n": True}, "must be integer"), ({"n": 1, "day": "15/10/2026"}, "does not match"),
])
def test_bad_arguments_are_refused(reg, args, needle):
    res = call(reg, "echo", args)
    assert not res.ok and needle in res.content


def test_unknown_tool_lists_alternatives(reg):
    res = call(reg, "nope", {})
    assert not res.ok and "echo" in res.content


def test_timeout_and_tool_error_and_truncation(reg):
    assert "timed out" in call(reg, "slow", {}, timeout=0.3).content
    assert "nothing for that day" in call(reg, "boom", {}).content
    res = call(reg, "big", {}, max_chars=100)
    assert res.truncated and len(res.content) < 300 and "narrower" in res.content
    assert json.loads(call(reg, "grid", {}).content) == {"0,9": 1}


def test_pseudonym_in_args_reaches_tool_as_real_address():
    r = ToolRegistry()
    seen = {}

    @r.tool(description="x", parameters={"type": "object", "properties": {"who": {"type": "string"}}})
    def who(who):
        seen["who"] = who
        return {"who": who}
    red = Redactor(salt="z")
    token = red.text("ops@example.com")
    out = r.call("who", json.dumps({"who": token}), redactor=red, default_timeout_s=2, max_chars=500)
    assert seen["who"] == "ops@example.com"
    assert "ops@example.com" not in out.content          # and it goes back masked


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE runs(id INTEGER PRIMARY KEY, route TEXT, mb REAL);"
                      "INSERT INTO runs(route, mb) VALUES ('AE-NOR', 1.5), ('AE-DEU', 2.0);")
    con.commit()
    con.close()
    return str(path)


def test_sql_reads_and_aggregates(db):
    out = ReadOnlySQLite(db).execute("SELECT route, round(sum(mb),1) AS mb FROM runs GROUP BY 1 ORDER BY 1")
    assert out["columns"] == ["route", "mb"] and out["rows"] == [["AE-DEU", 2.0], ["AE-NOR", 1.5]]


@pytest.mark.parametrize("q", [
    "DELETE FROM runs", "UPDATE runs SET mb=0", "DROP TABLE runs", "PRAGMA writable_schema=1",
    "ATTACH DATABASE 'x.db' AS x", "SELECT load_extension('evil')",
    "WITH t AS (SELECT 1) INSERT INTO runs(route) SELECT 'x' FROM t",
    "SELECT 1; DELETE FROM runs", "CREATE TEMP TABLE z(a)",
])
def test_sql_refuses_everything_but_reading(db, q):
    with pytest.raises(ToolError):
        ReadOnlySQLite(db).execute(q)
    assert sqlite3.connect(db).execute("SELECT count(*) FROM runs").fetchone()[0] == 2


def test_sql_row_cap_and_deadline(db):
    capped = ReadOnlySQLite(db, max_rows=1).execute("SELECT * FROM runs")
    assert capped["row_count"] == 1 and capped["truncated"]
    slow = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
            "SELECT count(*) FROM c")
    with pytest.raises(ToolError, match="exceeded"):
        ReadOnlySQLite(db, timeout_s=0.3).execute(slow)


def test_sql_tool_description_carries_schema(db):
    r = ToolRegistry()
    register_sqlite_tool(r, db)
    desc = r.schemas()[0]["function"]["description"]
    assert "CREATE TABLE runs" in desc
