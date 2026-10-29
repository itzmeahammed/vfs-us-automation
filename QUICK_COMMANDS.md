# Quick commands

Day-to-day commands only. The full reference is [COMMANDS.md](COMMANDS.md); for failures, see [RUNBOOK.md](RUNBOOK.md).

Run everything in **PowerShell from the project root**, after activating the venv once per terminal:

```powershell
cd C:\Users\ASRAB\Documents\VFS\vfs-malta-slot-checker
.\.venv\Scripts\Activate.ps1
```

---

## 1. What has to be running

| Service | Needed? | How it runs | What it does |
|---|---|---|---|
| **Slot checker** (supervisor) | ✅ Yes | Scheduled task `VFS Slot Checker`, every 30 min | Checks slots on every route, sends Telegram, and **starts auto-book** when a slot lands inside a waiting request's dates |
| **API** | ✅ Yes, for the web app | You start it (terminal 1) | Web app backend: manual Book and pay, jobs, logs. Also runs the **inbox watcher** (VFS invitation emails) |
| **ngrok tunnel** | ✅ Yes, for the web app | You start it (terminal 2) | Exposes the API at `https://tribute-groggy-yam.ngrok-free.dev` |


Before any real payment, check once:
- the card is set (`python -m src.payment card`)
- the SMS relay is online (`python -m src.otp_relay devices`)

### Restart after a code change

**One command. Use this one.**

```powershell
.\restart.ps1
```

Replaces the API process, waits for `/v1/health`, leaves the tunnel alone, and
reports whether the slot checker is enabled.

**Why a script and not `-StartApi`:** the API loads everything at import and
serves jobs from memory, so a code change is invisible until the process is
replaced. `start_tunnel.ps1 -StartApi` does NOT do that - it only starts an API
when none is answering, so against a healthy old process it prints
`[ok] API healthy` and changes nothing. On 2026-10-28 four fixes sat unserved
for a day for exactly that reason.

**Why it leaves ngrok running:** `ngrok/ngrok.yml` reserves no domain, so the
free tier issues a NEW public URL on every ngrok restart - and that URL is the
webhook the web app calls. Restarting the API under the existing tunnel keeps
the URL. Only pass `-Tunnel` when you want a new one:

```powershell
.\restart.ps1 -Tunnel      # also restarts ngrok; PRINTS A NEW PUBLIC URL
```

It refuses to restart while a booking is in flight, because booking jobs are
children of the API: one killed after the Pay click leaves a payment nobody can
account for. Answer `yes` to override.

### Start everything from cold

```powershell
# Terminal 1: API (keep open). Reads .env.api. Restart it after any code change.
python -m src.api

# Terminal 2: ngrok tunnel (refuses to start if the API is down)
.\ngrok\start_tunnel.ps1
#   or start the API and tunnel together:
.\ngrok\start_tunnel.ps1 -StartApi

# Slot checker: already scheduled. Make sure it is enabled:
Enable-ScheduledTask  -TaskName "VFS Slot Checker"
Get-ScheduledTaskInfo -TaskName "VFS Slot Checker" | Format-List LastRunTime, LastTaskResult, NextRunTime
```

Once it is up, `.\restart.ps1` is the only command you need for a code change.

### Run the slot checker now

```powershell
Start-ScheduledTask -TaskName "VFS Slot Checker"   # same path as the schedule
python -m src.supervisor                           # all routes, in this terminal
python -m src.supervisor -sc AE -dc NOR            # one route
python -m src.supervisor -sc AE -dc NOR --local    # one route, no proxy
```

### Stop

```powershell
Disable-ScheduledTask -TaskName "VFS Slot Checker"  # pause the slot checker (and auto-book)
# API / ngrok: Ctrl+C in their terminals, or, when restart.ps1 started
# the API (it runs hidden, so there is no terminal to Ctrl+C):
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'src.api' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

---

## 2. Booking per route

There are three levels:

| Level | Flags | Effect |
|---|---|---|
| **Probe** | `probe` | Logs in and reads the dashboard. Changes nothing. |
| **Walk** | `--walk` | Walks the booking pages and stops **before** payment. Reversible. |
| **Commit** | `--walk --commit --yes` | **Books and pays with the company card. No undo.** |

`--request <id>` supplies the applicant, account, combo and dates from `config/booking_requests/<id>.json`. List the ids with `python -m src.booking requests`.

### AE → Norway (NOR)
```powershell
python -m src.booking probe -sc AE -dc NOR
python -m src.booking probe -sc AE -dc NOR --request trav-nook-ae-nor-8dcf93 --walk
python -m src.booking probe -sc AE -dc NOR --request trav-nook-ae-nor-8dcf93 --walk --commit --yes
```

### AE → Hungary (HUN): Unified Checkout payment
```powershell
python -m src.booking probe -sc AE -dc HUN
python -m src.booking probe -sc AE -dc HUN --request trav-nook-ae-hun-af0da4 --walk
python -m src.booking probe -sc AE -dc HUN --request trav-nook-ae-hun-af0da4 --walk --commit --yes --capture full
```
On the first live HUN payment, `--capture full` saves the card form to `runs/AE-HUN/`, so the card-field selectors can be confirmed.

### AE → Sweden (SWE)
```powershell
python -m src.booking probe -sc AE -dc SWE
python -m src.booking probe -sc AE -dc SWE --request test-test-ae-swe-78e8da --walk
python -m src.booking probe -sc AE -dc SWE --request test-test-ae-swe-78e8da --walk --commit --yes
```

### AE → France (FRA) / Netherlands (NLD) / Greece (GRC)
These routes have no stored requests yet. Create one in the web app, then use its id:
```powershell
python -m src.booking probe -sc AE -dc FRA
python -m src.booking probe -sc AE -dc FRA --request <request-id> --walk
python -m src.booking probe -sc AE -dc FRA --request <request-id> --walk --commit --yes
# Same pattern with -dc NLD and -dc GRC
```

### AE → Switzerland (CHE)
CHE is disabled in `config/booking/AE-CHE.json`. Only probe and walk work.

### Test the full flow without paying: stop before the last payment button

Runs the whole flow (login, slot, details, card, **VFS Pay click**), then stops at the bank's 3-D Secure screen. The SMS code is read and logged **masked** (e.g. `4****0`). It is **never typed**, and the bank's submit is **never clicked**. The log shows:

`TEST MODE: Stopping before last payment button (OTP submit) for testing.`

Command line only; the API and web app cannot turn it on.

```powershell
python -m src.booking probe -sc AE -dc NOR --request trav-nook-ae-nor-8dcf93 --walk --commit --yes --stop-before-otp-submit
python -m src.booking probe -sc AE -dc HUN --request trav-nook-ae-hun-af0da4 --walk --commit --yes --stop-before-otp-submit
python -m src.booking probe -sc AE -dc SWE --request test-test-ae-swe-78e8da --walk --commit --yes --stop-before-otp-submit
```

What to expect:
- The appointment is **held unpaid** until VFS releases it, usually within about 30 minutes. Until then the account may show "booking in progress".
- The request goes back to `waiting`. The payment journal records the outcome `test_stopped`, not "unknown".
- ⚠️ **If the bank shows no 3-D Secure screen, the Pay click itself completes the payment.** The log then says `TEST MODE: no 3-D Secure screen appeared`.

### Useful extra flags

| Flag | Use |
|---|---|
| `--to select_slot` | Stop after this step |
| `--capture full` | Save the DOM of every page (mapping or debugging) |
| `--keep-open` | Leave the browser open at the end |
| `--proxy-url ""` | Force your local IP (no proxy) |
| `--email x --password y` | Force a specific VFS account |
| `-v` | Debug logs |

---

## 3. Waitlist registration (only when you run it)

```powershell
python -m src.waitlist status                       # clients per route
python -m src.waitlist check  --route AE-CHE        # validate config (no browser)
python -m src.waitlist doctor --route AE-CHE        # do the selectors still match VFS? types nothing
python -m src.waitlist run    --route AE-CHE        # dry run (unless config says otherwise)
python -m src.waitlist run    --route AE-CHE --live # real registration
python -m src.waitlist run    --registrant <client-id> --live
python -m src.waitlist journal                      # entries needing attention
```

---

## 4. Handy checks

### Status

```powershell
python -m src.booking requests                      # every request: status, dates, attempts, last error
python -m src.booking requests --status needs_attention
python -m src.booking status                        # routes configured for booking
python -m src.booking check                         # validate config/booking/*.json (offline)
```

### Money: run after any payment or crash

```powershell
python -m src.payment status                        # UNANSWERED or declined payments
python -m src.payment card                          # card loads? (masked; charges nothing)
python -m src.otp_relay devices                     # SMS relay SIM online?
python -m src.otp_relay watch --minutes 60          # recent bank SMS (codes masked)
```

### Accounts and proxies

```powershell
python -m src.utils.account_health                          # benched or disabled accounts, and why
python -m src.utils.account_health clear <email>            # re-enable after fixing
python -m src.utils.account_health clear <email> AE-NOR     # one route only
```

### Logs

```powershell
Get-Content .\app.log -Wait                         # slot checker, live
Get-Content .\task_runner.log -Tail 20              # last scheduled ticks + exit codes
Get-Content .\logs\booking.log -Tail 80             # manual booking runs
ls .\logs\booking_auto\ | sort LastWriteTime | select -Last 5   # auto-book runs
ls .\logs\api_jobs\     | sort LastWriteTime | select -Last 5   # web-app jobs
Select-String .\logs\app.jsonl -Pattern "<run_id>"  # everything one run wrote
```

### Tests

```powershell
python -m pytest -q
```
