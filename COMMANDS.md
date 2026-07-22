# Commands cheat-sheet

Practical commands for running and monitoring the VFS slot checker on **Windows**.

- Run all commands in **PowerShell** from the project root:
  `c:\Users\ASRAB\Documents\VFS\vfs-malta-slot-checker`
- Scheduled task name: **`VFS Slot Checker`** (fires at **:29** and **:59** every hour).
- Logs (project root): `app.log` (detailed), `task_runner.log` (per-tick history),
  `task_stderr.log` (early-crash safety net).

---

## Run it now (on demand)

```powershell
# Via the scheduler (same path the schedule uses; respects the overlap lock):
Start-ScheduledTask -TaskName "VFS Slot Checker"

# Or run the wrapper directly in this terminal (see it work live):
.\run_task.ps1
```

## Edit config & credentials (web UI)

Local browser editors (bind to `127.0.0.1` only; each Save writes a timestamped
`.bak-*` backup first). Leave the window running, edit in the browser, click Save.

```powershell
# Settings — edits config/config.local.ini (the real file):  http://127.0.0.1:8766
& .venv\Scripts\python.exe config_editor.py
```

```powershell
# Accounts — edits config/credentials.local.ini:             http://127.0.0.1:8765
& .venv\Scripts\python.exe credentials_editor.py
```

Both open the page automatically; press `Ctrl+C` in the terminal to stop the server.

The accounts editor has two panels: **Accounts** (edit `credentials.local.ini`) and
**Countries / Routes** (toggle which countries run — edits `config/vfs_urls.ini`).

## Add a new country (route)

A route is `AE-<DEST>` (e.g. `AE-ESP` for Spain). End-to-end:

1. **URL** — add/enable it in `config/vfs_urls.ini`. Easiest via the accounts
   editor's **Countries / Routes** panel (Add country → code `AE-ESP`, paste the
   login URL, tick **Run** → Save). Or edit the file directly:
   `AE-ESP = https://visa.vfsglobal.com/are/en/esp/login` (a leading `;` pauses it).

2. **Combinations** — create `config/routes/AE-ESP.json` (copy an existing one like
   `AE-ITA.json`) and set the real **centre / category / sub-category** dropdown
   values. Add `"otp": true` if the portal emails a code — and `"otp_mode": "text"`
   if it's a plain-text code (like Greece), not an image.

3. **Flag & name** — add the country to `DESTINATION_FLAGS` and `DESTINATION_NAMES`
   in `src/utils/telegram_message.py` (e.g. `"ESP": "🇪🇸"` and `"ESP": "Spain"`), so
   Telegram shows 🇪🇸 Spain instead of the raw code.

4. **Assign accounts** — give at least one account this route in its `routes` list
   (accounts editor **Accounts** panel, or `config/credentials.local.ini`).

5. **Verify** — it then runs automatically on the next tick (no `setup_task.ps1`
   needed; that's only for schedule-cadence changes):
   ```powershell
   # confirm it's now in the active route list:
   & .venv\Scripts\python.exe -c "from src.utils.config_reader import initialize_config as i; i(); from src.supervisor import _all_routes; print(['-'.join(r) for r in _all_routes()])"

   # test just this route once (headed, via proxy):
   & .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ESP -v --proxy
   ```

To **pause** a country later, just untick **Run** in the Countries panel (or comment
its line in `vfs_urls.ini`) — the JSON, accounts, and flag stay for when you re-enable it.

## Schedule status — next run, last run, last result

```powershell
Get-ScheduledTaskInfo -TaskName "VFS Slot Checker" |
  Format-List LastRunTime, LastTaskResult, NextRunTime, NumberOfMissedRuns
```

`LastTaskResult = 0` means the last run succeeded. Confirm it's armed + see triggers:

```powershell
Get-ScheduledTask -TaskName "VFS Slot Checker" | Format-List TaskName, State
(Get-ScheduledTask -TaskName "VFS Slot Checker").Triggers |
  ForEach-Object { [PSCustomObject]@{ Start=$_.StartBoundary; Every=$_.Repetition.Interval } }
```

## Check all previous runs (fired / skipped / exit code)

`task_runner.log` records one line per tick — the fastest history.

```powershell
Get-Content .\task_runner.log            # whole history
Get-Content .\task_runner.log -Tail 20   # recent ticks
```

## Last run's detailed log & outcome

```powershell
Get-Content .\app.log -Tail 40           # last run's detail (bottom of file)

# Just the result lines (per-route success/fail, telegram, slots, rotation):
Select-String -Path .\app.log -Pattern "succeeded|FAILED|All routes done|Telegram message sent|Earliest available slot|Invalid credentials|Using credential"
```

## Watch a run live (while it's running)

```powershell
Get-Content .\app.log -Wait              # live tail; Ctrl+C stops watching (not the bot)
```

## If a run crashed early / silently

```powershell
Get-Content .\task_stderr.log            # startup/import/config crashes land here
```

## Preview the credential rotation (which account each run of the day)

Cadence is set in `config.ini` `[schedule]` (`runs_per_hour`, `start_hour`,
`end_hour`) — one place that drives both the task triggers and this spread.
After changing it, **re-run `.\setup_task.ps1`** to update the scheduler.

```powershell
# Which account each run uses for a route (run-of-day rotation):
& .venv\Scripts\python.exe -c "from src.utils.config_reader import initialize_config as i; i(); from src.utils import credentials as c; [print(f'{t} run#{ri:<2} {em}') for t,ri,em in c.rotation_schedule('AE-ITA')]"
```

## Cleanup sanity check (both should be 0 after a run)

```powershell
(Get-ChildItem $env:TEMP -Directory -Filter 'vfs-chrome-profile-*').Count          # leftover profiles
(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
  Where-Object { $_.CommandLine -like '*vfs-chrome-profile-*' }).Count             # leftover bot Chrome
```

## Manage the task

```powershell
Disable-ScheduledTask -TaskName "VFS Slot Checker"     # pause the schedule
Enable-ScheduledTask  -TaskName "VFS Slot Checker"     # resume
Stop-ScheduledTask    -TaskName "VFS Slot Checker"     # kill an in-progress run
.\setup_task.ps1                                       # re-register / apply changes
Unregister-ScheduledTask -TaskName "VFS Slot Checker" -Confirm:$false   # remove it
```

## Account health / circuit breaker

Accounts that get blocked or keep failing are automatically **benched** (skipped
by selection) to protect them from a VFS ban. Two kinds:
- **Cooldown** (auto-clears) — 429001 restricted / 429202 locked, or too many
  consecutive stuck runs. Benched for `hard_cooldown_hours` / `soft_cooldown_hours`.
- **Disabled** (needs YOU) — wrong credentials or 429002 "Access Denied". Stays
  benched until you fix the account and flag it healthy.

```powershell
# See every benched/disabled account, why, and until when:
& .venv\Scripts\python.exe -m src.utils.account_health

# Manually bench an account (e.g. you know VFS restricted it). Hours optional
# (defaults to hard_cooldown_hours):
& .venv\Scripts\python.exe -m src.utils.account_health bench <email@travnook.com> 24

# After fixing an account (e.g. corrected its password), flag it healthy:
& .venv\Scripts\python.exe -m src.utils.account_health clear <email@travnook.com>

# Clear ALL health records (re-enable everything):
& .venv\Scripts\python.exe -m src.utils.account_health clear-all
```

Check what happened & what runs next:

```powershell
# Last run's outcome per route (restrictions, benchings, disables):
Select-String -Path .\app.log -Pattern "RESTRICTED|benched|DISABLED|Route AE-" | Select-Object -Last 10

# Which account each hour ACTUALLY uses for a route (benched accounts excluded).
# Change 'AE-ITA' to any route:
& .venv\Scripts\python.exe -c "from src.utils.config_reader import initialize_config as i; i(); from src.utils import credentials as c; p=c._available(c._load_pool(),'AE-ITA'); [print(f'{h:02d}:00 ->', c._mask(p[(h-6)%len(p)][0])) for h in range(6,24)]"
```

Tuning lives in `config.ini` under `[account_safety]` (`hard_cooldown_hours`,
`soft_cooldown_hours`, `fail_threshold`, `max_attempts`).

## Proxy / IP (per account, shared pool)

The proxy pool lives in `config/proxylist.txt` (gitignored; one
`http://user:pass@host:port` per line — provider CSV also accepted). Each account
is **pinned to one IP** (`account_index mod N`) and uses it across **all routes** —
a stable identity. The bot injects proxy auth via a built-in forwarder (Chrome
ignores `user:pass`) and probes/skips a dead IP by trying the next in the pool.

**Master switch** — `config.ini` `[proxy] enabled = true|false`. Flip per run:
```powershell
& .venv\Scripts\python.exe -m src.supervisor --local    # this run: no proxy (local IP)
& .venv\Scripts\python.exe -m src.supervisor --proxy    # this run: force proxy IPs
```

```powershell
# Show each account's pinned IP (fast, no network):
& .venv\Scripts\python.exe -c "from src.utils.config_reader import initialize_config as i; i(); from src.utils import proxy_pool as pp, credentials as c; [print(f\"{e.split('@')[0]:16} -> {pp.label(pp.account_proxy(e))}\") for e,_,_ in c._load_pool()]"

# Probe every IP in the pool: exit IP + is it alive? (slow — real requests):
& .venv\Scripts\python.exe -c "from src.utils.config_reader import initialize_config as i; i(); from src.utils import proxy_pool as pp; [print(pp.label(u), '->', pp.probe(u) or 'NO EXIT') for u in pp.pool()]"
```

Notes: only IPs in a **VFS-accepted region (UAE)** avoid the 403203 geo-block;
**datacenter** IPs still risk Cloudflare's bot checks (residential is safest).
Egress IPs are logged to `app.log` but **not** shown in Telegram.

## Analytics dashboard (accounts · routes · health · IPs · slots)

One consolidated view built from `app.log` + `account_health.json` + config —
overview, per-route and per-account stats, health/cooldowns, recent runs, and
auto-flagged insights (e.g. "route PAUSED — all accounts benched").

```powershell
& .venv\Scripts\python.exe -m src.utils.analytics                 # print dashboard
& .venv\Scripts\python.exe -m src.utils.analytics --write analytics_report.txt   # also save
```

Read-only (never changes state). To act on what it shows, use the account-health
commands above.

## Log maintenance

`app.log` grows over time — trim it occasionally:

```powershell
Clear-Content .\app.log                  # empty it (keep the file)
```
## Check ITALY with custom proxy

### 1. Create the temporary proxy config

```powershell
Set-Content -Path "$env:TEMP\vfs_proxy_test.ini" -Encoding ascii -Value @("[proxy-routes]","AE-ITA = socks5://47.91.121.127:80")
```

### 2. Point the application to the config

```powershell
$env:VFS_BOT_CONFIG_PATH = "$env:TEMP\vfs_proxy_test.ini"
```

### 3. Run the supervisor

```powershell
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ITA -v
```

### 4. Clean up

```powershell
Remove-Item Env:VFS_BOT_CONFIG_PATH
```


## Notes

- The task runs **only while you are logged in** (a real Chrome window must render).
  A sleeping / shut-down / logged-out PC fires nothing and does **not** catch up.
- Both runs within an hour (`:29` and `:59`) use the **same** rotated VFS account;
  the account advances by clock hour (`cred1` at 06:00, `cred2` at 07:00, ...).
- Telegram: slot reports go to the **success** chat (`TELEGRAM_chat_id`); failure/
  error alerts go to the **summary** chat (`TELEGRAM_SUMMARY_CHAT_ID`).
- Wrong email/password or a 429002 block **disables that account** (alert sent)
  until you fix it and flag it healthy. A 429001/429202 block **benches it**
  for the configured cooldown. An unregistered email just **skips** that portal.
- Run from a normal PowerShell (Win key → type PowerShell → Enter), starting with
  `cd "c:\Users\ASRAB\Documents\VFS\vfs-malta-slot-checker"`.

---

## Ad-hoc: force one run through a specific proxy

Bypass the pool and pin a single run to one proxy with `--proxy-url`. Supports
`http://` and `socks5://`. **Replace `USER:PASS@HOST:PORT` with real values — do
not commit real proxy credentials into this file.**

```powershell
# HTTP proxy:
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ITA -v --proxy-url http://USER:PASS@HOST:PORT

# SOCKS5 proxy:
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ITA -v --proxy-url socks5://USER:PASS@HOST:PORT
```

## Preview full rotation (route · account · time · IP)

Every route's per-hour account schedule, its login URL, and the **pinned exit
IP:port** each account egresses from (4th column). Probes each account's proxy
**once** (cached) — so it makes a few live requests and takes a moment; a dead or
auth-failed pin shows as `DEAD:<port>`, and `(no proxy)` means the pool is
empty / disabled.

```powershell
& .venv\Scripts\python.exe -c @"
from src.utils.config_reader import initialize_config, get_config_value
initialize_config()
from src.utils import credentials as c
from src.utils import proxy_pool as p
from src.supervisor import _all_routes
_ip = {}
def exit_ip(em, r):
    url = p.account_proxy(em, r)
    if not url:
        return '(no proxy)'
    if em not in _ip:
        host, port, *_ = p.parse(url)
        _ip[em] = f'{p.probe(url) or \"DEAD\"}:{port}'
    return _ip[em]
for s, d in _all_routes():
    r = f'{s}-{d}'
    print(f'=== {r}   {get_config_value(\"vfs-url\", r)} ===')
    for t, ri, em in c.rotation_schedule(r):
        print(f'  {t}   run#{ri:<2}  {em:<28}  {exit_ip(em, r)}')
    print()
"@
```

Want it **instant** (no network, shows the pinned endpoint `host:port` instead of
the live IP)? Swap the `exit_ip(em, r)` call for `p.label(p.account_proxy(em, r))`.

& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ITA -v --proxy-url http://travnookmarketing:JJmaqyo8yc@151.242.128.49:50100
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ITA -v --proxy-url http://travnookmarketing:JJmaqyo8yc@151.244.143.160:50100
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ITA -v --proxy-url http://travnookmarketing:JJmaqyo8yc@151.244.143.243:50100
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ITA -v --proxy-url http://travnookmarketing:JJmaqyo8yc@151.244.143.32:50100
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ITA -v --proxy-url http://travnookmarketing:JJmaqyo8yc@151.244.143.64:50100


& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ITA -v --proxy-url socks5://travnookmarketing:JJmaqyo8yc@151.242.128.49:50101
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ITA -v --proxy-url socks5://travnookmarketing:JJmaqyo8yc@151.244.143.160:50101
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ITA -v --proxy-url socks5://travnookmarketing:JJmaqyo8yc@151.244.143.243:50101
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ITA -v --proxy-url socks5://travnookmarketing:JJmaqyo8yc@151.244.143.32:50101
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ITA -v --proxy-url socks5://travnookmarketing:JJmaqyo8yc@151.244.143.64:50101




## command to fetch all line with Browser traffic this route

```powershell
findstr /N /C:"Browser traffic this route" app.log

```