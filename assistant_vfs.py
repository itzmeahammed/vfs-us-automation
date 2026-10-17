"""The VFS slot-checker's tools for the portable `ai_assistant` package.

This is the ONLY project-specific part of the assistant. It wires the generic
engine to what this project already computes — the editor's stats, account
health, proxy and scheduler views, the run logs, and the slot database — as
read-only tools. Another project copies `ai_assistant/` unchanged and writes
its own version of this file.

Settings: [assistant] in config/config.ini (defaults) / config.local.ini.
The OpenAI key is [openai] api_key unless [assistant] api_key is set.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from typing import Optional

from ai_assistant import (Assistant, AssistantConfig, AssistantHTTP, ToolError,
                          ToolRegistry, register_sqlite_tool)

SYSTEM_CONTEXT = """\
A Windows bot that watches VFS Global visa-appointment availability for applicants in \
the UAE. Each ROUTE is source-destination, e.g. AE-NOR = UAE -> Norway, AE-DEU = Germany, \
AE-MT = Malta. A route has one or more COMBOS (centre city x visa category).

How it runs: a Windows scheduled task fires every 30 minutes inside a daily window; each \
RUN visits every enabled route in its own Chrome. A route can take several ATTEMPTS (retry \
on another proxy IP). Traffic goes through metered residential proxies (Proxy-Seller); \
proxy MB is the main running cost and there is a daily MB cap that pauses routes when spent.

Route outcomes per check: slot (bookable date shown), waitlist, none (no dates), error. \
Run statuses: OK, FAILED, SKIPPED, PAUSED (cap/bench), GEO (IP outside UAE), RESTRICTED.

Accounts: several VFS logins, rotated. Failures add STRIKES to an account on a route; \
at the threshold the account is BENCHED (soft cool-down) on that route, or hard-blocked. \
A route with no healthy account is DARK (cannot run). Logins pass a Cloudflare Turnstile \
check and sometimes an e-mailed OTP.

Data: stats come from the bot's own logs (one file per local day). The slot database \
records every check (`checks`), changes (`events`: opened/closed/date_moved/...), runs, \
and offered dates (`slot_dates`, with lead_days = days from check to appointment). \
Timestamps: *_utc are UTC; ts_local/date_local/hour_local are UAE local time (UTC+4).
"""

#: What the scope gate lets through. Everything else gets the fixed refusal.
SCOPE = (
    "A VFS Global visa-appointment slot-checking bot and its operations: slot "
    "availability and openings per route/country/city/visa type, runs and attempts, "
    "success and failure reasons, logs and errors, VFS accounts (strikes, benches), "
    "proxies and data usage/cost, Cloudflare/OTP issues, the scheduler, its settings, "
    "and forecasts or trends built from that data.")

SUGGESTIONS = (
    "Summarise today: runs, slots found, failures and data used.",
    "Which routes failed most this week, and why?",
    "When do Norway slots usually open (hour and weekday)?",
    "How long will the proxy data last at the current pace?",
    "Which accounts are benched or close to being benched?",
)

LABELS = {
    "get_overview": "overview", "get_day_stats": "day stats", "get_trend": "daily trend",
    "get_route_performance": "route performance", "get_account_health": "account health",
    "get_proxy_usage": "proxy usage", "get_scheduler_status": "scheduler",
    "search_logs": "logs", "get_slot_rankings": "slot rankings",
    "get_slot_heatmap": "slot heatmap", "get_slot_activity": "slot activity",
    "get_slot_forecast": "forecast", "get_watch_coverage": "watch coverage",
    "run_sql": "slot database", "get_config_summary": "settings",
}

#: Config sections the model may see. An ALLOW-list on purpose: the loaded
#: config also holds [credN] logins, tokens and card settings.
_CONFIG_SECTIONS = ("schedule", "proxy", "account_safety", "turnstile", "waitlist",
                    "timeouts", "retry", "bandwidth", "switches", "slots", "browser")
#: ...and within those, keys that hold (or may one day hold) a login or a proxy URL.
_CRED_KEY = re.compile(r"account|password|passwd|proxy$|^proxies|token|secret|key$", re.I)

_END_DAY = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$",
            "description": "Last local date included, YYYY-MM-DD. Omit for the latest "
                           "day with logs (normally today)."}

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_DAY = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$",
        "description": "Local date YYYY-MM-DD. Omit for the latest day with logs."}


def _days_prop(default: int, maximum: int = 90) -> dict:
    return {"type": "integer", "minimum": 1, "maximum": maximum,
            "description": f"How many days back (default {default})."}


def _attempt_digest(mbs) -> dict:
    mbs = [float(x) for x in (mbs or [])]
    if not mbs:
        return {"count": 0}
    return {"count": len(mbs), "total_mb": round(sum(mbs), 1),
            "avg_mb": round(sum(mbs) / len(mbs), 2), "max_mb": round(max(mbs), 1)}


def build_registry(ed) -> ToolRegistry:
    """`ed` is the credentials_editor module (passed in: it may be __main__)."""
    reg = ToolRegistry()
    tool = reg.tool

    def resolve_day(day: Optional[str]) -> str:
        days = ed.available_days()
        if not days:
            raise ToolError("no log files found")
        if day is None:
            return days[0]
        if day not in days:
            raise ToolError(f"no logs for {day}. Days available: {days[-1]} .. {days[0]} "
                            f"({len(days)} days)")
        return day

    def window(days: int, end_day: Optional[str]) -> list:
        """Log days, newest first: `days` of them ending at `end_day` (inclusive)."""
        all_days = ed.available_days()
        start = all_days.index(resolve_day(end_day))
        return all_days[start:start + days]

    def day_digest(day: str) -> dict:
        d = dict(ed.day_stats(day))
        d["attempts"] = _attempt_digest(d.get("attempts"))
        d["browser"] = _attempt_digest(d.get("browser"))
        d["runs"] = _attempt_digest(d.get("runs")) if isinstance(d.get("runs"), list) else d.get("runs")
        d["summary"] = ed._summary(ed.day_stats(day))
        return d

    # ---- operations -------------------------------------------------------
    @tool(description="Right-now snapshot: latest day's totals (runs, slots, success, "
                      "MB), active alerts (dark routes, data cap, low success), scheduler "
                      "state, proxy balance, and healthy accounts per route. Start here "
                      "for 'how is it going' questions.")
    def get_overview():
        p = ed.stats_payload()
        if p.get("error"):
            raise ToolError(p["error"])
        return {"latest_log_day": p["day"], "today": p["today"],
                "alerts": [{"level": k, "message": m} for k, m in p["alerts"]],
                "data_budget": {"used_mb_today": (p.get("budget") or {}).get("used_mb"),
                                "daily_cap_mb": p.get("cap")},
                "accounts": {"enabled": p["accounts_total"],
                             "available": p["accounts_available"]},
                "accounts_per_route": p["pool"],
                "scheduler": ed.scheduler_status(),
                "proxy_package": ed.proxy_traffic()}

    @tool(description="Everything measured on ONE day from the logs: run/attempt counts, "
                      "status counts, failure reasons, per-route results (attempts, ok, "
                      "fail, slot/waitlist/none checks, MB, slot dates), slots found, "
                      "account strikes and restrictions, Turnstile and OTP outcomes, proxy "
                      "ports, MB per host and per hour.",
          parameters={"type": "object", "properties": {"day": _DAY}})
    def get_day_stats(day: Optional[str] = None):
        return day_digest(resolve_day(day))

    @tool(description="One summary row per day (oldest first): runs, attempts, MB, MB per "
                      "attempt, ok/fail, success %, slots, Cloudflare share, paused, "
                      "restricted, first/last activity. For trends and day-to-day "
                      "comparisons. `days` counts back from `end_day` INCLUSIVE.",
          parameters={"type": "object", "properties": {"days": _days_prop(14),
                                                       "end_day": _END_DAY}})
    def get_trend(days: int = 14, end_day: Optional[str] = None):
        ds = window(days, end_day)
        return [ed._summary(ed.day_stats(d)) for d in reversed(ds)]

    @tool(description="Per-route totals across several days, from the logs: attempts, "
                      "ok, fail, success %, slot/waitlist/none/error checks, MB, MB per "
                      "attempt, and the days the route ran. Optional single route. "
                      "`days` counts back from `end_day` INCLUSIVE — for ONE specific "
                      "day (e.g. yesterday) pass days=1 and end_day=that date.",
          parameters={"type": "object", "properties": {
              "days": _days_prop(7, 60),
              "end_day": _END_DAY,
              "route": {"type": "string", "pattern": r"^[A-Z]{2}-[A-Z]{2,3}$",
                        "description": "e.g. AE-NOR. Omit for all routes."}}})
    def get_route_performance(days: int = 7, route: Optional[str] = None,
                              end_day: Optional[str] = None):
        ds = window(days, end_day)
        agg: dict = {}
        for d in ds:
            for code, r in ed.day_stats(d)["routes"].items():
                if route and code != route:
                    continue
                a = agg.setdefault(code, {"attempts": 0, "ok": 0, "fail": 0, "other": 0,
                                          "slot": 0, "waitlist": 0, "none": 0, "error": 0,
                                          "mb": 0.0, "days_run": 0})
                for k in ("attempts", "ok", "fail", "other", "slot", "waitlist", "none",
                          "error"):
                    a[k] += int(r.get(k) or 0)
                a["mb"] += float(r.get("mb") or 0)
                a["days_run"] += 1
        for a in agg.values():
            done = a["ok"] + a["fail"]
            a["success_pct"] = round(100 * a["ok"] / done, 1) if done else None
            a["mb"] = round(a["mb"], 1)
            a["mb_per_attempt"] = round(a["mb"] / a["attempts"], 2) if a["attempts"] else None
        if route and not agg:
            raise ToolError(f"{route} has no runs in the last {days} days")
        return {"period": f"{ds[-1]} .. {ds[0]}" if ds else None,
                "routes": dict(sorted(agg.items(), key=lambda kv: -kv[1]["attempts"]))}

    @tool(description="Account health now: each account's state (healthy / benched / "
                      "disabled), strikes, bench expiry and reason, per-route benches; "
                      "the strike/bench rules; routes with no healthy account.",
          parameters={"type": "object", "properties": {
              "route": {"type": "string", "pattern": r"^[A-Z]{2}-[A-Z]{2,3}$",
                        "description": "Only show this route's benches."}}})
    def get_account_health(route: Optional[str] = None):
        h = ed.health_payload()
        if not h.get("available", True):
            raise ToolError("account health module unavailable")
        now = h.get("now") or 0

        def until(ts):
            if not ts or ts <= now:
                return None
            return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        accounts = []
        for a in h["accounts"]:
            routes = [{"route": r["route"], "strikes": r["strikes"],
                       "benched_until": until(r["until"]) if r["benched"] else None,
                       "reason": r["reason"] or None}
                      for r in a["routes"]
                      if (r["benched"] or r["strikes"]) and (not route or r["route"] == route)]
            accounts.append({"account": a["email"], "enabled": a["enabled"],
                             "state": a["state"], "strikes": a["strikes"],
                             "until": until(a["until"]), "reason": a["reason"] or None,
                             "disabled_reason": a["disabled_reason"] or None,
                             "route_issues": routes})
        return {"rules": h["rules"], "accounts": accounts,
                "orphan_records": len(h.get("orphans") or [])}

    @tool(description="Proxy data: the Proxy-Seller package (GB left, used, limit, "
                      "expiry), MB used per day from the logs, today's MB per hour, the "
                      "daily cap, and runway in days at the recent average.",
          parameters={"type": "object", "properties": {"days": _days_prop(14, 60)}})
    def get_proxy_usage(days: int = 14):
        pkg = ed.proxy_traffic()
        ds = ed.available_days()[:days]
        per_day = [{"day": d, "mb": ed._summary(ed.day_stats(d))["mb"]} for d in reversed(ds)]
        recent = [r["mb"] for r in per_day if r["mb"] > 0][-7:]
        avg = sum(recent) / len(recent) if recent else None
        out = {"package": pkg, "daily_cap_mb": ed._cap_mb(),
               "used_today_ledger_mb": (ed._budget() or {}).get("used_mb"),
               "mb_per_day": per_day,
               "avg_mb_per_day_last7_active": round(avg, 1) if avg else None,
               "today_mb_by_hour": ed.day_stats(ds[0])["hourly"] if ds else {}}
        if pkg.get("ok") and avg:
            out["runway_days_at_avg"] = round(pkg["left_mb"] / avg, 1)
        return out

    @tool(description="Windows scheduled task: state, last/next run, last result code, "
                      "missed runs, sleep/wake settings, plus the last lines of the task "
                      "runner log (start/finish/exit codes). For 'did it run / why not'.")
    def get_scheduler_status():
        st = ed.scheduler_status()
        lines = []
        path = os.path.join(ed.HERE, "task_runner.log")
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = [ln.rstrip() for ln in f.readlines()[-30:]]
        except OSError:
            pass
        st["result_code_meaning"] = {0: "success", 1: "run exited with error",
                                     267009: "running now", 267014: "terminated (stopped "
                                     "or hit the time limit)", 267011: "has not run yet"
                                     }.get(st.get("lastResult"), "see Windows docs")
        return {"task": st, "runner_log_tail": lines}

    # ---- logs -------------------------------------------------------------
    nested_quant = re.compile(r"\([^)]*[+*][^)]*\)\s*[+*{]")

    @tool(description="Search the bot's log lines (newest first). Plain case-insensitive "
                      "text by default; set regex=true for a Python regex. Use for exact "
                      "errors, a route's story on a day, or when something happened. "
                      "Returns matching lines (timestamp, level, message) and the total "
                      "match count.",
          parameters={"type": "object", "properties": {
              "query": {"type": "string", "maxLength": 200,
                        "description": "Text or regex, e.g. 'AE-GRC' or 'Route .* FAILED'."},
              "day": _DAY,
              "days": {"type": "integer", "minimum": 1, "maximum": 14,
                       "description": "Search this many days back from `day` (default 1)."},
              "regex": {"type": "boolean"},
              "level": {"type": "string", "enum": ["ERROR", "WARNING", "INFO"],
                        "description": "Only lines of this level."},
              "max_lines": {"type": "integer", "minimum": 1, "maximum": 150}},
              "required": ["query"]}, timeout_s=25)
    def search_logs(query: str, day: Optional[str] = None, days: int = 1,
                    regex: bool = False, level: Optional[str] = None, max_lines: int = 60):
        if regex:
            if nested_quant.search(query):
                raise ToolError("regex has nested quantifiers; simplify it")
            try:
                pat = re.compile(query, re.I)
            except re.error as e:
                raise ToolError(f"bad regex: {e}")
            match = pat.search
        else:
            needle = query.lower()
            match = lambda s: needle in s.lower()       # noqa: E731
        start = resolve_day(day)
        all_days = ed.available_days()
        idx = all_days.index(start)
        span = all_days[idx: idx + days]
        hits, total = [], 0
        lvl = f"] {level} [" if level else None
        for d in span:
            path = os.path.join(ed.LOG_DIR, f"app-{d}.log")
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except OSError:
                continue
            for ln in reversed(lines):
                if lvl and lvl not in ln:
                    continue
                if match(ln):
                    total += 1
                    if len(hits) < max_lines:
                        hits.append(ln.rstrip()[:320])
        return {"days_searched": span, "total_matches": total,
                "shown": len(hits), "lines_newest_first": hits}

    # ---- slot analytics (state/slots.db) ----------------------------------
    def db_conn():
        ed.health_module()                       # puts the project on sys.path
        from src.slots import db as sdb
        return sdb.connect(db_path, read_only=True)

    def with_db(fn):
        conn = db_conn()
        try:
            return fn(conn)
        finally:
            conn.close()

    @tool(description="Destinations ranked by how bookable they are over the window: "
                      "availability (share of checks with a slot), median/best lead days, "
                      "current outcome and date, openings, score and verdict.",
          parameters={"type": "object", "properties": {
              "days": _days_prop(7, 60),
              "purpose": {"type": "string", "description": "Visa purpose filter, e.g. "
                          "'tourist' or 'business'. Omit for all."}}})
    def get_slot_rankings(days: int = 7, purpose: Optional[str] = None):
        from src.slots import query
        rows = with_db(lambda c: query.rank(c, days, purpose))
        drop = ("combos", "by_day", "sparkline")
        return [{k: v for k, v in r.items() if k not in drop} for r in rows]

    @tool(description="Weekday x local-hour grid. Two DIFFERENT "
                      "measures: `availability` = cells where a slot was already showing "
                      "(share of checks with a slot; `checks` = sample size), and "
                      "`new_openings` = when a slot newly APPEARED (closed -> open). For "
                      "'when do slots open / best time to check' use new_openings; for "
                      "'when can I usually find a slot' use availability. Ignore cells "
                      "with few checks.",
          parameters={"type": "object", "properties": {
              "days": _days_prop(30, 90),
              "route": {"type": "string", "pattern": r"^[A-Z]{2}-[A-Z]{2,3}$"}}})
    def get_slot_heatmap(days: int = 30, route: Optional[str] = None):
        from src.slots import query
        h = with_db(lambda c: query.heatmap(c, days, route=route))
        cells = [{"weekday": _WEEKDAYS[k[0]], "hour": k[1], "checks": v.get("observed"),
                  "share_with_slot": round(v.get("rate") or 0, 3)}
                 for k, v in sorted(h.get("grid", {}).items()) if v.get("observed")]
        opens = sorted(({"weekday": _WEEKDAYS[k[0]], "hour": k[1], "count": v}
                        for k, v in h.get("openings", {}).items()),
                       key=lambda r: -r["count"])
        return {"days": days, "route": route or "all",
                "new_openings": {"total": sum(r["count"] for r in opens), "by_cell": opens},
                "availability": cells}

    @tool(description="What changed recently, newest first: slots opening, closing, dates "
                      "moving earlier/later, waitlists opening — with country, city, visa "
                      "type, dates and time.",
          parameters={"type": "object", "properties": {
              "days": _days_prop(7, 60),
              "limit": {"type": "integer", "minimum": 1, "maximum": 80}}})
    def get_slot_activity(days: int = 7, limit: int = 40):
        from src.slots import query
        return with_db(lambda c: {"openings": query.recent_openings(c, days, limit),
                                  "activity": query.recent_activity(c, days, limit)})

    @tool(description="When each combo is likely to open next — statistics on gaps "
                      "between past releases, not ML. Each row is a date range with a "
                      "confidence word, or a reason there is none (open_now, never_opened, "
                      "waitlist_only, dormant, overdue, ...).",
          parameters={"type": "object", "properties": {"days": _days_prop(30, 120)}})
    def get_slot_forecast(days: int = 30):
        from src.slots import forecast
        return with_db(lambda c: forecast.forecast(c, days))

    @tool(description="How well the bot watches: readings and openings per local hour, "
                      "which hours are BLIND (never or rarely checked), per-combo regime "
                      "and headline coverage figures.",
          parameters={"type": "object", "properties": {"days": _days_prop(7, 60)}})
    def get_watch_coverage(days: int = 7):
        from src.slots import coverage
        return with_db(lambda c: {"summary": coverage.summary(c, days),
                                  "hours": coverage.hour_profile(c, days)})

    # ---- settings ---------------------------------------------------------
    @tool(description="Operational settings (schedule window, runs per hour, proxy on/off, "
                      "data cap, retry and bench rules, live-booking switch) and which "
                      "routes are enabled. Secrets are never included.")
    def get_config_summary():
        ed.health_module()
        from src.utils import config_reader
        cfg = {s: {k: v for k, v in (config_reader.get_config_section(s, {}) or {}).items()
                   if not _CRED_KEY.search(k)}
               for s in _CONFIG_SECTIONS}
        return {"settings": cfg,
                "routes": [{"route": r["code"], "enabled": r["enabled"]}
                           for r in ed.parse_routes()]}

    # ---- free-form SQL ----------------------------------------------------
    ed.health_module()
    try:
        from src.utils.config_reader import get_config_value
        db_path = get_config_value("slots", "db_path", "") or os.path.join("state", "slots.db")
    except Exception:
        db_path = os.path.join("state", "slots.db")
    db_path = os.path.join(ed.HERE, db_path) if not os.path.isabs(db_path) else db_path
    if os.path.isfile(db_path):
        register_sqlite_tool(reg, db_path, about=(
            "Typical joins: checks.combo_id -> combos.id (route, country_name, city, "
            "visa_type); slot_dates.check_id -> checks.id; events.combo_id -> combos.id. "
            "checks.outcome is slot|waitlist|none|error|disabled. Use date_local / "
            "hour_local / weekday (0=Mon) for local-time grouping. Filter source='live' "
            "unless asked about backfill."))
    return reg


def _setting(section: str, key: str, default=""):
    from src.utils.config_reader import get_config_value
    v = get_config_value(section, key, None)
    return default if v is None or str(v).strip() == "" else str(v).strip()


def build(ed) -> Optional[AssistantHTTP]:
    """The mounted assistant, or None when disabled / no key (the page then
    simply has no panel). Raises nothing: a broken assistant must not take the
    editor down with it."""
    if ed.health_module() is None:
        return None
    if _setting("assistant", "enabled", "true").lower() in ("0", "false", "no", "off"):
        return None
    key = _setting("assistant", "api_key") or _setting("openai", "api_key")
    if not key:
        return None
    state = os.path.join(ed.HERE, "state")
    cfg = AssistantConfig(
        api_key=key,
        model=_setting("assistant", "model") or _setting("openai", "model", "gpt-4o-mini"),
        title="VFS Assistant",
        system_context=SYSTEM_CONTEXT,
        scope_description=SCOPE,
        suggestions=SUGGESTIONS,
        daily_token_budget=int(_setting("assistant", "daily_token_budget", "1500000")),
        rate_limit_per_minute=int(_setting("assistant", "rate_limit_per_minute", "12")),
        audit_path=os.path.join(state, "assistant_audit.jsonl"),
        usage_path=os.path.join(state, "assistant_usage.json"),
    )
    return AssistantHTTP(Assistant(cfg, build_registry(ed)), labels=LABELS)
