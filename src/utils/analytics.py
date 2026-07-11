"""Analytics dashboard for the VFS slot checker.

Parses app.log + account_health.json + the account/route/proxy config into one
consolidated view so you can see and manage everything at a glance:
accounts, routes, health/cooldowns, IPs used, slot hits, and failure patterns.

    python -m src.utils.analytics            # print the dashboard
    python -m src.utils.analytics --log other.log --write report.txt

Read-only: it never changes state (use account_health for that).
"""

import argparse
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime

from src.utils.config_reader import initialize_config, get_config_section
from src.utils import account_health, credentials, proxy_pool

LOG_FILE = "app.log"

_TS = r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"
RE_TS = re.compile(_TS)
RE_RUN = re.compile(_TS + r".*Running (\d+) route")
RE_CRED = re.compile(_TS + r".*Using credential \d+/\d+ available for (\S+) at hour \d+: (\S+)")
RE_PROXY = re.compile(_TS + r".*proxyseller ip used here : (\S+)")
RE_SLOT = re.compile(r"-> Earliest available slot .*?: *(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})")
RE_OUTCOME = re.compile(_TS + r".*Route (\S+?) (OK|FAILED|RESTRICTED|LOCKED|BLOCKED|SKIPPED|PAUSED)\.")
RE_BENCH = re.compile(r"Account (\S+) benched (\d+)h .*?[—-] (.+?)\.")
RE_DISABLE = re.compile(r"Account (\S+) DISABLED .*?[—-] (.+?)\.")


def _mask_map() -> dict:
    """masked-handle -> [short account names] (masks collide, e.g. ha*** = 2)."""
    out = defaultdict(list)
    for email, _pw, _routes in credentials._load_pool():
        out[credentials.mask(email)].append(email.split("@")[0])
    return out


def _name(masked: str, mm: dict) -> str:
    names = mm.get(masked)
    if not names:
        return masked.split("@")[0] if masked else "-"
    return "/".join(names)


def parse_log(path: str) -> dict:
    """Parse the log into route-run records + health events."""
    records = []          # {ts, route, account, status, slots:[dates], proxy}
    bench_events = []      # (ts, masked, hours/None, reason)
    cur = {"route": None, "account": None, "proxy": None, "slots": []}
    ticks = 0
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return {"records": [], "bench_events": [], "ticks": 0}
    with f:
        for line in f:
            if RE_RUN.search(line):
                ticks += 1
            m = RE_CRED.search(line)
            if m:
                cur = {"route": m.group(2), "account": m.group(3),
                       "proxy": None, "slots": []}
                continue
            m = RE_PROXY.search(line)
            if m:
                cur["proxy"] = m.group(2)
                continue
            m = RE_SLOT.search(line)
            if m:
                cur["slots"].append(m.group(1))
                continue
            m = RE_OUTCOME.search(line)
            if m:
                ts, route, status = m.group(1), m.group(2), m.group(3)
                account = cur["account"] if cur["route"] == route else None
                records.append({
                    "ts": ts, "route": route, "status": status,
                    "account": account,
                    "proxy": cur["proxy"] if cur["route"] == route else None,
                    "slots": list(cur["slots"]) if cur["route"] == route else [],
                })
                cur = {"route": None, "account": None, "proxy": None, "slots": []}
                continue
            m = RE_BENCH.search(line)
            if m:
                ts = RE_TS.search(line)
                bench_events.append((ts.group(1) if ts else "", m.group(1),
                                     m.group(2) + "h", m.group(3)))
                continue
            m = RE_DISABLE.search(line)
            if m:
                ts = RE_TS.search(line)
                bench_events.append((ts.group(1) if ts else "", m.group(1),
                                     "DISABLED", m.group(2)))
    return {"records": records, "bench_events": bench_events, "ticks": ticks}


def _hr(title):
    return f"\n── {title} " + "─" * max(2, 56 - len(title))


def build_report(path: str) -> str:
    mm = _mask_map()
    data = parse_log(path)
    recs = data["records"]
    out = []

    # Header
    span = ""
    if recs:
        span = f"{recs[0]['ts'][:16]}  ->  {recs[-1]['ts'][:16]}"
    out.append("=" * 64)
    out.append("  VFS SLOT CHECKER — ANALYTICS")
    out.append(f"  generated {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  "
               f"log span {span}  |  route-runs {len(recs)}")
    out.append("=" * 64)

    # Overview
    status_tot = defaultdict(int)
    for r in recs:
        status_tot[r["status"]] += 1
    total = len(recs) or 1
    out.append(_hr("OVERVIEW"))
    out.append("  " + "  ".join(f"{s}:{status_tot[s]}" for s in
               ("OK", "FAILED", "RESTRICTED", "LOCKED", "BLOCKED", "SKIPPED", "PAUSED")
               if status_tot[s]))
    slot_runs = sum(1 for r in recs if r["slots"])
    out.append(f"  runs that found a slot: {slot_runs}  |  scheduler ticks parsed: {data['ticks']}")

    # Health now
    out.append(_hr("ACCOUNT HEALTH (now)"))
    snap = account_health.snapshot()
    now = time.time()
    disabled = [e for e, r in snap.items() if r.get("disabled")]
    cooling = [(e, r) for e, r in snap.items()
               if not r.get("disabled") and r.get("cooldown_until", 0) > now]
    if disabled:
        out.append("  [DISABLED — needs manual clear]:")
        for e in disabled:
            out.append(f"     {credentials.mask(e)}  ({snap[e].get('last_reason')})")
    if cooling:
        out.append("  [COOLDOWN]:")
        for e, r in sorted(cooling, key=lambda x: x[1]["cooldown_until"]):
            until = datetime.fromtimestamp(r["cooldown_until"]).strftime("%m-%d %H:%M")
            out.append(f"     {credentials.mask(e):24} until {until}  ({r.get('last_reason')})")
    if not disabled and not cooling:
        out.append("  all accounts healthy.")

    # Per route
    out.append(_hr("PER ROUTE"))
    routes = [k.upper() for k in (get_config_section("vfs-url") or {})]
    by_route = defaultdict(list)
    for r in recs:
        by_route[r["route"]].append(r)
    out.append(f"  {'route':10} {'runs':>4} {'OK':>3} {'RESTR':>5} {'other':>5} "
               f"{'avail':>6}  last / latest slot")
    for route in routes:
        rr = by_route.get(route, [])
        ok = sum(1 for x in rr if x["status"] == "OK")
        restr = sum(1 for x in rr if x["status"] in ("RESTRICTED", "LOCKED"))
        other = len(rr) - ok - restr
        avail = len(credentials._available(credentials._load_pool(), route))
        elig = len(credentials.eligible_emails(route))
        last = rr[-1] if rr else None
        laststr = f"{last['status']}@{last['ts'][11:16]}" if last else "-"
        slots = [d for x in rr for d in x["slots"]]
        slotstr = f" | slot {slots[-1]}" if slots else ""
        flag = "  <-- ALL BENCHED" if elig and avail == 0 else ""
        out.append(f"  {route:10} {len(rr):>4} {ok:>3} {restr:>5} {other:>5} "
                   f"{avail:>3}/{elig:<2} {laststr}{slotstr}{flag}")

    # Per account
    out.append(_hr("PER ACCOUNT"))
    by_acct = defaultdict(lambda: {"used": 0, "OK": 0, "RESTR": 0, "other": 0,
                                   "routes": set(), "last": ""})
    for r in recs:
        if not r["account"]:
            continue
        a = by_acct[r["account"]]
        a["used"] += 1
        a["routes"].add(r["route"].replace("AE-", ""))
        a["last"] = r["ts"][5:16]
        if r["status"] == "OK":
            a["OK"] += 1
        elif r["status"] in ("RESTRICTED", "LOCKED", "BLOCKED"):
            a["RESTR"] += 1
        else:
            a["other"] += 1
    out.append(f"  {'account':16} {'routes':14} {'used':>4} {'OK':>3} {'RESTR':>5} "
               f"{'health':>18}")
    hstate = {}
    for e, r in snap.items():
        if r.get("disabled"):
            hstate[credentials.mask(e)] = "DISABLED"
        elif r.get("cooldown_until", 0) > now:
            hstate[credentials.mask(e)] = "cool->" + datetime.fromtimestamp(
                r["cooldown_until"]).strftime("%m-%d %H:%M")
    for masked, a in sorted(by_acct.items(), key=lambda x: -x[1]["used"]):
        out.append(f"  {_name(masked, mm):16} {','.join(sorted(a['routes'])):14} "
                   f"{a['used']:>4} {a['OK']:>3} {a['RESTR']:>5} "
                   f"{hstate.get(masked, 'healthy'):>18}")

    # Recent runs
    out.append(_hr("RECENT ROUTE-RUNS (last 14)"))
    out.append(f"  {'time':16} {'route':8} {'account':12} {'status':10} {'slots':5} proxy")
    for r in recs[-14:]:
        out.append(f"  {r['ts'][5:16]:16} {r['route']:8} "
                   f"{_name(r['account'] or '', mm)[:12]:12} {r['status']:10} "
                   f"{len(r['slots']):>5} {r['proxy'] or 'local'}")

    # Insights
    out.append(_hr("INSIGHTS"))
    insights = []
    for route in routes:
        elig = len(credentials.eligible_emails(route))
        avail = len(credentials._available(credentials._load_pool(), route))
        if elig and avail == 0:
            insights.append(f"  [!] {route}: ALL {elig} account(s) benched -> route is PAUSED (no runs).")
        elif elig and avail == 1:
            insights.append(f"  [!] {route}: only 1 of {elig} accounts available — one more bench pauses it.")
    # accounts restricted a lot
    for masked, a in by_acct.items():
        if a["RESTR"] >= 3 and a["RESTR"] > a["OK"]:
            insights.append(f"  [!] {_name(masked, mm)}: restricted {a['RESTR']}x (>OK {a['OK']}) — VFS is flagging it.")
    if disabled:
        insights.append(f"  [!] {len(disabled)} account(s) DISABLED — fix creds & run: "
                        f"python -m src.utils.account_health clear <email>")
    out.append("\n".join(insights) if insights else "  nothing notable — all routes have available accounts.")

    out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="VFS slot-checker analytics dashboard.")
    ap.add_argument("--log", default=LOG_FILE, help="log file to parse (default app.log)")
    ap.add_argument("--write", metavar="FILE", help="also write the report to FILE")
    args = ap.parse_args()

    initialize_config()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    report = build_report(args.log)
    print(report)
    if args.write:
        with open(args.write, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"(written to {args.write})")


if __name__ == "__main__":
    main()
