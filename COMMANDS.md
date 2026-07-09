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

## Preview the credential rotation (which account each hour)

```powershell
& .venv\Scripts\python.exe -c "from src.utils.config_reader import initialize_config as i; i(); from src.utils import credentials as c; [print(f'{h:02d}:00 -> cred{idx}  {em}') for h,idx,em in c.rotation_schedule()]"
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
# See every benched/disabled account and why:
& .venv\Scripts\python.exe -m src.utils.account_health

# After fixing an account (e.g. corrected its password), flag it healthy:
& .venv\Scripts\python.exe -m src.utils.account_health clear <email@travnook.com>

# Clear ALL health records (re-enable everything):
& .venv\Scripts\python.exe -m src.utils.account_health clear-all
```

Tuning lives in `config.ini` under `[account_safety]` (`hard_cooldown_hours`,
`soft_cooldown_hours`, `fail_threshold`, `max_attempts`).

## Log maintenance

`app.log` grows over time — trim it occasionally:

```powershell
Clear-Content .\app.log                  # empty it (keep the file)
```

---

## Notes

- The task runs **only while you are logged in** (a real Chrome window must render).
  A sleeping / shut-down / logged-out PC fires nothing and does **not** catch up.
- Both runs within an hour (`:29` and `:59`) use the **same** rotated VFS account;
  the account advances by clock hour (`cred1` at 06:00, `cred2` at 07:00, ...).
- Telegram: slot reports go to the **success** chat (`TELEGRAM_chat_id`); failure/
  error alerts go to the **summary** chat (`TELEGRAM_SUMMARY_CHAT_ID`).
- Wrong email/password **stops that run immediately** (no retries) and alerts the
  summary chat. An unregistered email just **skips** that portal (no alert).
```


Open a normal PowerShell (not VS Code's): press Win, type PowerShell, Enter. Then:


cd "c:\Users\ASRAB\Documents\VFS\vfs-malta-slot-checker"

# Is it scheduled? Last result (0 = success) and next run time:
Get-ScheduledTaskInfo -TaskName "VFS Slot Checker" | Format-List LastRunTime, LastTaskResult, NextRunTime

# Per-tick history (fired / skipped / exit code):
Get-Content .\task_runner.log -Tail 20

# Last run's detail:
Get-Content .\app.log -Tail 40
Optional — fire one run right now to confirm end-to-end (a Chrome window will open ~2 min):


Start-ScheduledTask -TaskName "VFS Slot Checker"