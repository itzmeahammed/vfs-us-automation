"""What we watched, what we missed, and what a tighter cadence would cost.

    python -m scripts.slot_coverage                 # last 30 days
    python -m scripts.slot_coverage --days 60
    python -m scripts.slot_coverage --target 10     # aim for a 10-minute cadence
    python -m scripts.slot_coverage --json          # machine-readable

Read-only: it opens the database, prints, and changes nothing. Every figure is
derived from the readings, so re-running it after a change in behaviour gives a
different — and current — answer. No country, route or centre is named in the
logic; the groupings are the regimes `coverage` infers.
"""

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.slots import coverage, schedule, store as store_mod  # noqa: E402


def _bar(fraction, width=18):
    if fraction is None:
        return " " * width
    filled = int(round(max(0.0, min(1.0, fraction)) * width))
    return "#" * filled + "." * (width - filled)


def _pct(value):
    return f"{value * 100:.0f}%" if value is not None else "-"


def _mins(hours):
    return f"{hours * 60:.0f}m" if hours else "-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=coverage.DEFAULT_DAYS,
                    help=f"window in days (default: {coverage.DEFAULT_DAYS})")
    ap.add_argument("--db", default=None, help="database path")
    ap.add_argument("--target", type=float, default=15.0,
                    help="cadence in minutes to price up (default: 15)")
    ap.add_argument("--budget", type=float, default=None,
                    help="readings per hour to allocate (default: what we achieve now)")
    ap.add_argument("--resolution", type=float,
                    default=coverage.DEFAULT_RESOLUTION_HOURS,
                    help="hours after a reading still counted as watched (default: 1)")
    ap.add_argument("--json", action="store_true", help="dump the numbers as JSON")
    args = ap.parse_args()

    path = args.db or store_mod.db_path()
    if not os.path.exists(path):
        print(f"No slot database at {path}.")
        return 1

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    profile = coverage.profile(conn, args.days, resolution_hours=args.resolution)
    summary = coverage.summary(conn, args.days)
    head = schedule.capacity(conn, args.days)
    plan = schedule.plan(conn, args.days, readings_per_hour=args.budget,
                         profile_rows=profile)
    gain = schedule.expected_gain(conn, args.days, plan_rows=plan["combinations"],
                                  profile_rows=profile)
    prices = [schedule.required_budget(conn, args.days, target_interval_min=t,
                                       profile_rows=profile)
              for t in (5.0, 10.0, 15.0, 20.0, 30.0)]

    if args.json:
        print(json.dumps({"summary": summary, "capacity": head, "profile": profile,
                          "plan": plan, "expected_gain": gain,
                          "prices": prices}, indent=2, default=str))
        return 0

    print(f"\nWINDOW  last {summary['days']} days, "
          f"closed on {', '.join(summary['closed_days']) or 'nothing'}")
    print(f"        {summary['combinations_watched']} combinations watched, "
          f"{summary['episodes']} open episodes, "
          f"{summary['episodes_seen_once']} of them seen exactly once")
    print(f"        {summary['observed_runs_without_readings']} of "
          f"{summary['observed_runs']} runs the bot performed produced no readings")
    recovered = summary['runs'] - summary['observed_runs']
    if recovered > 0:
        unattributable = (summary['runs_without_readings']
                          - summary['observed_runs_without_readings'])
        print(f"        ({recovered:,} further runs were recovered from logs; "
              f"{unattributable:,} of those carried no attributable readings, "
              f"which is a limit of the logs, not wasted bot time)")
    print(f"\nCOVERAGE  {_pct(summary['coverage'])} of open hours within "
          f"{args.resolution:g}h of a reading   [{_bar(summary['coverage'])}]")
    print(f"        {summary['covered_hours']:,.0f}h watched, "
          f"{summary['blind_hours']:,.0f}h blind")

    clock = summary.get("clock") or {}
    if clock.get("watched_hours"):
        print()
        print(f"THE CLOCK  {clock['watched_hours']} of 24 local hours watched "
              f"  [{_bar(clock['clock_coverage'])}]")
        for row in clock["hours"]:
            rate = row["openings_per_1k"]
            if row["blind"]:
                shown = f"{row['readings']:>7,}   {'never watched':<13}"
            else:
                shown = (f"{row['readings']:>7,}   {rate:>5.1f}/1k     "
                         f"{'#' * min(40, int((rate or 0) * 3))}")
            print(f"  {row['hour']:02d}:00 {shown}")
        if clock["blind_hours"]:
            hours = clock["blind_hours"]
            print(f"        blind: {hours[0]:02d}:00-{hours[-1] + 1:02d}:00 "
                  f"({len(hours)}h). Openings run at "
                  f"{clock['openings_per_1k']:g} per 1k readings in the hours we "
                  f"do watch.")
            if clock.get("rate_varies") is False:
                print("        Those watched hours vary no more than chance would "
                      "produce, so there is no evidence any hour is quiet -- and "
                      "none at all for the blind ones.")
            print(f"        Watching the whole clock would make "
                  f"{clock['uplift_if_uniform']}x as many openings visible, IF "
                  f"releases are uniform in time.")

    if head.get("sustained_per_hour"):
        print(f"\nCAPACITY  {head['achieved_per_hour']}/h achieved  vs  "
              f"{head['sustained_per_hour']}/h if never idle")
        print(f"        busy {_pct(head['busy_share'])} of the clock, "
              f"idle {_pct(head['idle_share'])}  [{_bar(head['busy_share'])}]")
        print(f"        {_pct(head['wasted_share_of_run_time'])} of observed run time went to "
              f"runs that produced nothing")

    print("\nREGIMES  " + "  ".join(f"{k}={v}" for k, v in
                                    sorted(summary["by_regime"].items())))
    if summary["combinations_with_unresolved_episodes"]:
        print(f"        {summary['combinations_with_unresolved_episodes']} combination(s) "
              "have episodes too short to measure at this cadence — their miss rate "
              "is not identifiable from this data")

    print(f"\nPER COMBINATION ({len(profile)} known)")
    print(f"  {'route':8} {'city':12} {'visa type':24} {'regime':13} "
          f"{'cover':>6} {'eps':>4} {'once':>5} {'gap':>7} {'plan':>7}")
    planned = {r["combo_id"]: r for r in plan["combinations"]}
    order = {coverage.FLASH: 0, coverage.INTERMITTENT: 1, coverage.SPARSE: 2,
             coverage.PERSISTENT: 3, coverage.WAITLIST_ONLY: 4,
             coverage.DORMANT: 5, coverage.UNSEEN: 6}
    for row in sorted(profile, key=lambda r: (order.get(r["regime"], 9),
                                              -(r["episodes"] or 0))):
        if not row["readings"] and row["regime"] == coverage.UNSEEN:
            continue
        mine = planned.get(row["combo_id"])
        if not row["enabled"] or not row["in_config"]:
            target = "off"          # switched off in config, so never polled
        elif mine and mine.get("interval_minutes"):
            target = f"{mine['interval_minutes']:.0f}m"
        else:
            target = "-"
        print(f"  {row['route']:8} {row['city'][:12]:12} {row['visa_type'][:24]:24} "
              f"{row['regime']:13} {_pct(row['coverage']):>6} {row['episodes']:>4} "
              f"{row['episodes_seen_once']:>5} {_mins(row['median_gap_hours']):>7} {target:>7}")

    print(f"\nPLAN AT TODAY'S BUDGET  {plan['budget_per_hour']}/h "
          f"({plan['min_interval_minutes']:g}-{plan['max_interval_minutes']:g} min bounds)"
          + ("  DOES NOT FIT" if plan["unmet"] else ""))
    det, con = gain["detection"], gain["confirmation"]
    print(f"        detection group    {det['before']} -> {det['after']} "
          f"expected catches ({det['ratio']}x over {det['combinations']} combinations)")
    print(f"        confirmation group {con['before']} -> {con['after']} "
          f"({con['ratio']}x) — deliberately given up")
    if det["ratio"] is not None and det["ratio"] < 1.2:
        print("        Reallocation alone changes little: the budget, not its "
              "distribution, is the constraint.")

    print("\nWHAT A TIGHTER CADENCE COSTS")
    print(f"  {'cadence':>9} {'needed/h':>9} {'capacity/h':>11} {'workers':>8}")
    for price in prices:
        print(f"  {price['target_interval_minutes']:>8.0f}m {price['required_per_hour']:>9.1f} "
              f"{str(price['sustained_per_hour']):>11} {str(price['workers_needed']):>8}")

    chosen = schedule.required_budget(conn, args.days,
                                      target_interval_min=args.target,
                                      profile_rows=profile)
    verdict = ("reachable without another worker" if chosen["reachable_without_new_workers"]
               else f"needs about {chosen['workers_needed']} workers")
    print(f"\n  At {args.target:g} minutes on {chosen['detection_combinations']} "
          f"detection combinations: {chosen['required_per_hour']}/h — {verdict}.")

    cadence = summary_cadence(profile)
    if cadence:
        print(f"\nIF A RELEASE LASTS ... (at the current {cadence * 60:.0f}m typical cadence)")
        print(f"  {'duration':>9} {'caught':>8} {'true per 100 seen':>19}")
        for row in coverage.detection_sensitivity(cadence):
            print(f"  {row['duration_minutes']:>8}m {_pct(row['catch_rate']):>8} "
                  f"{str(row['implied_true_per_100_seen']):>19}")
        print("  The catch rate is arithmetic, not a guess — but the duration is "
              "unknown, which is why it is a table and not one number.")

    conn.close()
    return 0


def summary_cadence(profile):
    """The typical achieved cadence across combinations that produce episodes."""
    values = [schedule.achieved_interval_hours(r) for r in profile if r["episodes"]]
    values = [v for v in values if v]
    if not values:
        return None
    values.sort()
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


if __name__ == "__main__":
    raise SystemExit(main())
