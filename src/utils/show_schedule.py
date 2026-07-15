"""Print the run schedule: which URL, which account, at what time.

    python -m src.utils.show_schedule        (or run  .\show_schedule.ps1)

Reflects config.ini [schedule] (runs_per_hour / window), the active routes in
config/vfs_urls.ini, and current account availability (benched accounts are
excluded). Read-only — changes nothing.
"""

from src.utils.config_reader import initialize_config, get_config_value
from src.utils import credentials as c
from src.utils import proxy_pool as pp


def main():
    initialize_config()
    from src.supervisor import _all_routes  # lazy (pulls in the bot)

    rph, sh, eh = c._sched()
    proxy_on = pp.is_enabled()
    now_ri = c.run_index()   # index of the run happening/next right now
    print(f"Schedule: {rph} run(s)/hour, {sh:02d}:00-{eh:02d}:00  "
          f"(benched accounts excluded; proxy {'ON' if proxy_on else 'OFF (local IP)'})")
    print("=" * 72)

    def ip_for(email):
        if not proxy_on:
            return "local"
        return pp.label(pp.account_proxy(email)) or "direct"

    routes = _all_routes()
    if not routes:
        print("No routes active in config/vfs_urls.ini.")
    for s, d in routes:
        r = f"{s}-{d}"
        print(f"\n=== {r}   {get_config_value('vfs-url', r)} ===")
        sched = c.rotation_schedule(r)
        if not sched:
            print("  (no available account)")
        for t, ri, em in sched:
            who = em.split("@")[0] if "@" in em else em
            mark = "  <-- NEXT" if ri == now_ri else ""
            print(f"  {t}   run#{ri:<2}  {who:18}  {ip_for(em)}{mark}")


if __name__ == "__main__":
    main()
