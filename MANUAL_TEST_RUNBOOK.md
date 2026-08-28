# Manual test runbook — verifying the whole flow end to end

Work top to bottom. Each section is independently checkable, and each tells you
**what you should see** and **what it means if you don't**.

Everything here is safe: the system ships **parked**, and Sections 0–7 cannot
submit a registration to VFS no matter what you do. Only Section 8 changes that,
and it is deliberately last.

**Set this once per terminal:**

```powershell
cd C:\Users\Universal\Documents\mufaddal\vfs-malta-slot-checker
$token = (Select-String -Path .env.api -Pattern '^VFSAPI_SECRET_TOKEN=(.+)$').Matches[0].Groups[1].Value.Trim()
$H = @{ 'X-Webhook-Secret-Token' = $token }
```

---

## Section 0 — Baseline: confirm you are parked

**Before touching anything, confirm nothing can register.** If this section is
wrong, stop and fix it before continuing.

```powershell
python -m src.waitlist status
```

**Expect the first line:**

```
Waitlist registration: DISABLED · DRY RUN (nothing submitted) · caps 3/run, 20/day
```

`DISABLED` is the master kill switch (`[waitlist] register_enabled`). While it
says DISABLED, a run walks the whole flow and reports what it *would* do, then
stops. That is the state you want for every section below except Section 8.

> **Note:** settings come from `config/config.ini` **plus** `config/config.local.ini`
> (gitignored, overrides the first). Trust `status` over reading the ini by eye —
> it prints the merged, effective values.

---

## Section 1 — The API is alive

```powershell
python -m src.api
```

Leave it running in this terminal. Expect:

```
Webhook API v1.0.0 listening on http://127.0.0.1:8000 (docs=off, single_flight=True)
```

**In a second terminal:**

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

**Expect:** `status : ok`

| If instead | Means |
|---|---|
| `FATAL: webhook API configuration is invalid` | `VFSAPI_SECRET_TOKEN` missing or under 32 chars. It lives in `.env.api`. |
| Connection refused | Server not running, or it crashed — read the first terminal. |

---

## Section 2 — Auth actually protects the API

This is the one that must never fail. The token is the real security boundary.

```powershell
# 2a. No token -> MUST be refused
try { Invoke-RestMethod http://127.0.0.1:8000/clients } catch { "refused: $($_.Exception.Response.StatusCode.value__)" }

# 2b. Wrong token -> MUST be refused
try { Invoke-RestMethod http://127.0.0.1:8000/clients -Headers @{'X-Webhook-Secret-Token'='wrong'} } catch { "refused: $($_.Exception.Response.StatusCode.value__)" }

# 2c. Correct token -> works
Invoke-RestMethod http://127.0.0.1:8000/clients -Headers $H
```

**Expect:** 2a and 2b print `refused: 401`. 2c returns your client list.

> If 2a ever returns data, **stop and do not open a tunnel.** That would mean the
> API answers unauthenticated requests.

---

## Section 3 — Read the system's current posture

```powershell
Invoke-RestMethod http://127.0.0.1:8000/status -Headers $H | Select-Object -ExpandProperty posture
```

**Expect (today):**

```
PARKED — [waitlist] register_enabled is false, so nothing can register.
Detection and notification still work.
```

The four postures, in ascending order of risk:

| Posture | Meaning |
|---|---|
| `PARKED` | Master switch off. Nothing registers. **Where you are now.** |
| `MANUAL` | Registration allowed, but only when *you* trigger it. |
| `AUTO (DRY RUN)` | Slot bot fires the waitlist bot automatically, but stops before submitting. |
| `AUTO (LIVE)` | Fully autonomous, real submissions. |

Route readiness:

```powershell
(Invoke-RestMethod http://127.0.0.1:8000/status -Headers $H).routes |
  Format-Table route, ready, clients
```

**Expect today:** `AE-CHE` and `AE-NLD` ready; the rest not ready (either no
clients, or `"enabled": false` in `config/waitlist/<ROUTE>.json`). That is normal
— a route is only "ready" once you deliberately turn it on.

---

## Section 4 — Validate config and client data (no browser)

Fast, offline, and catches most real problems before a browser is ever launched.

```powershell
python -m src.waitlist check --route AE-CHE
```

**Expect:**

```
✓ Route config for AE-CHE: 4 step(s); commit step = 'review_pay'
    review_pay: 2 field(s)   ← COMMITS (point of no return)
✓ Client 'ahmed': 28 field(s), 1 combo(s)
    ✓ every {{placeholder}} resolves
✓ 3 client(s) ready for AE-CHE.
```

The line that matters is **`every {{placeholder}} resolves`** — it proves the
client's data can fill every field the route's form needs. A missing field here
becomes a half-filled form on the real portal.

`[DISABLED — runs will skip them]` next to a client is informational, not an
error: parked clients are validated but not run.

---

## Section 5 — Client lifecycle through the API

This is exactly what travnooker.com will do.

### 5a. What combos may this route accept?

```powershell
Invoke-RestMethod http://127.0.0.1:8000/routes/AE-CHE/readiness -Headers $H
```

**Expect:** `ready: True`, combos `Abu Dhabi - SCHENGEN`, `Dubai - SCHENGEN`.

Your web app should populate its dropdown from this, never from a hardcoded list
— the labels must match the portal exactly.

### 5b. Create a client

```powershell
$body = @{
  client_id = 'runbook-test'; route = 'AE-CHE'
  combos = @('Dubai - SCHENGEN')
  account = 'you@example.com'; account_password = 'the-vfs-password'
  first_name = 'TEST'; last_name = 'USER'; nationality = 'India'
  passport_number = 'A1234567'
  phone_country_code = '971'; phone_number = '501234567'
  email = 'test@example.com'
  address_line_1 = 'FLAT 101'; address_line_2 = 'DUBAI'
} | ConvertTo-Json

Invoke-RestMethod http://127.0.0.1:8000/clients -Method POST -Headers $H `
  -ContentType 'application/json' -Body $body
```

**Expect:** `201`, with this message:

```
Client created. It is PARKED (enabled=false) — call POST /clients/runbook-test/enable to arm it.
```

**Creating is never arming.** A bug in the web app must not be able to arm a
fleet of clients for live registration.

> `combos` is a **preference order**, not a shopping list — see Section 7.

### 5c. Read it back — secrets must NOT come out

```powershell
Invoke-RestMethod http://127.0.0.1:8000/clients/runbook-test -Headers $H |
  Select-Object -ExpandProperty client
```

**Expect:** `has_account_password : True`, but **no `account_password` field**,
and `passport_number` masked (`A1*****67`). If you ever see a real password in a
response, that is a serious bug — report it.

### 5d. Arm it, then park it again

```powershell
Invoke-RestMethod http://127.0.0.1:8000/clients/runbook-test/enable  -Method POST -Headers $H
Invoke-RestMethod http://127.0.0.1:8000/clients/runbook-test/disable -Method POST -Headers $H
```

`enable` **refuses** a client that would not run — arming something broken just
moves the failure to 3am. To see that, try enabling a client with a bad combo:
you get `422` and a `problems[]` list naming each fault.

### 5e. Clean up

```powershell
Invoke-RestMethod http://127.0.0.1:8000/clients/runbook-test -Method DELETE -Headers $H
```

---

## Section 6 — Trigger a run and follow it

```powershell
$body = @{ route='AE-CHE'; dry_run=$true; reason='runbook test' } | ConvertTo-Json
$r = Invoke-RestMethod http://127.0.0.1:8000/trigger/waitlist -Method POST -Headers $H `
       -ContentType 'application/json' -Body $body
$r.job.job_id
```

**Expect:** returns in well under a second with `202` and a `job_id`. The call is
non-blocking — the browser work happens in a background process.

```powershell
# Poll until it stops being 'running' (a real run takes 2-5 minutes)
do {
  Start-Sleep 10
  $j = Invoke-RestMethod "http://127.0.0.1:8000/jobs/$($r.job.job_id)" -Headers $H
  $j.status
} while ($j.status -eq 'running')

$j | Select-Object status, outcome, exit_code, needs_attention, log_file
$j.results
```

### Reading the outcome

| `status` | Meaning |
|---|---|
| `succeeded` | The run completed. Read `results[]` for what happened per client. |
| `slots_available` | **Better than success** — a real bookable slot appeared, so it stopped instead of waitlisting. Tell the client to *book*. |
| `failed` | The run errored. `log_file` has the traceback. |

**`needs_attention: true` is the one to take seriously.** It means a submit is
unresolved — it may or may not have landed on VFS. Never retry it automatically;
check the VFS account by hand, then record what you found:

```powershell
python -m src.waitlist resolve --route AE-CHE --combo "Dubai - SCHENGEN" `
  --registrant <id> --status success
```

### Idempotency — what protects you from a double-submit

```powershell
$k = @{ 'X-Webhook-Secret-Token'=$token; 'Idempotency-Key'='my-unique-key-1' }
Invoke-RestMethod http://127.0.0.1:8000/trigger/waitlist -Method POST -Headers $k `
  -ContentType 'application/json' -Body $body
# run the exact same command again
```

**Expect: the same `job_id` both times.** Your web app retrying a request whose
response it never saw gets the original job back instead of starting a second
registration run.

A trigger with a *different* key while a job is running returns `409` —
single-flight. Two concurrent runs would corrupt the journal and double-book
accounts.

---

## Section 7 — The guards (why nothing registers by accident)

Eight gates run before anything is submitted, cheapest and most decisive first:

| # | Gate |
|---|---|
| 1 | master kill switch (`register_enabled`) |
| 2 | route config enabled |
| 3 | client enabled |
| 4 | client actually wants this combo |
| 5 | a dangling journal entry needs a human |
| 6 | already registered for this exact route+combo+client |
| 6b | **one entry per client per route** |
| 7 | per-run cap (`max_per_run`) |
| 8 | per-day cap (`max_per_day`) |

To see the verdict for one client without launching a browser, save this as
`check_guard.py` and run `python check_guard.py ahmed`:

```python
import sys
sys.path.insert(0, ".")
from src.utils.config_reader import initialize_config
initialize_config()
from src.waitlist import guards, registrant as R

who = sys.argv[1]
r = R.load(who)
v = guards.check(r.route, r.combos[0], r)
print("ALLOW" if v.allowed else "BLOCK")
print(v.reason)
```

**Expect today:**

```
BLOCK
waitlist registration is switched off ([waitlist] register_enabled = false)
```

That is gate 1 — the master switch, short-circuiting before anything else runs.

### The guard worth understanding: one entry per client, per route

A client's `combos` list is a **preference order** ("whichever opens first"),
**not** a list to register for all of. One person wants one appointment. Two
entries hold two slots for one need, deny one to somebody else, and risk VFS
voiding both as duplicates.

See the full history:

```powershell
python -m src.waitlist journal --all
```

**Expect today:** `ahmed` holds a **successful** AE-CHE entry
(`ref SWDB79923880977`, 2026-08-11). He is therefore **blocked** from
registering again on that route.

> **This is correct behaviour, not a bug.** If you want to test with him, either
> use a different client, or cancel that entry on the portal and record it:
>
> ```powershell
> python -m src.waitlist resolve --route AE-CHE --combo "Dubai - SCHENGEN" `
>   --registrant ahmed --status failed
> ```

### Anything stuck needing a human?

```powershell
python -m src.waitlist journal
```

**Expect:** `Nothing needs attention.` Anything listed here blocks that
route/combo/client until you resolve it.

---

## Section 8 — Going live, one switch at a time

**Only after Sections 0–7 pass.** Change **one** switch, test, then the next.
Edit `config/config.local.ini` (gitignored), not `config/config.ini`.

| Stage | Change | Posture becomes | What it proves |
|---|---|---|---|
| **1** | *(nothing — you are here)* | `PARKED` | Whole chain runs; everything reports `skipped`. |
| **2** | `register_enabled = true`, keep `dry_run = true` | `MANUAL` | Fills the real form on the real portal, stops before the commit click. |
| **3** | `dry_run = false` | `MANUAL` (live) | **Real submissions.** Start with ONE client on ONE route. |
| **4** | `auto_trigger_enabled = true` | `AUTO` | Slot bot fires the waitlist bot by itself. |

After each change, re-check the posture:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/status -Headers $H | Select-Object -ExpandProperty posture
```

> **At Stage 3 the system submits real registrations against real VFS accounts.**
> `max_per_day` (currently 20) is your ceiling if something misbehaves — consider
> lowering it to 1 or 2 for the first live day.

---

## Section 9 — The tunnel (optional; needed for remote testing)

Blocked until you register an ngrok authtoken — see [ngrok/README.md](ngrok/README.md).

```powershell
.\ngrok\start_tunnel.ps1
```

It refuses to start if the API is down, prints the public URL, and asserts that
an unauthenticated request is refused. Then verify **from a different machine**:

```powershell
python ngrok/test_remote.py https://YOUR-URL.ngrok-free.app
```

**Expect:** `RESULT: 7 passed, 0 failed`.

Loopback working proves nothing about reachability — run this from elsewhere.

---

## Section 10 — Outbound webhooks (bot → your app)

Currently **off** (`webhook.enabled = false`, no URL set). To test, add to
`config/config.local.ini`:

```ini
[webhook]
enabled = true
url = https://your-app.example.com/webhooks/vfs
secret = <a 64-char hex secret, DIFFERENT from VFSAPI_SECRET_TOKEN>
```

Ping it:

```powershell
python -c "import sys; sys.path.insert(0,'.'); from src.utils.config_reader import initialize_config; initialize_config(); from src.utils import webhook; print(webhook.send_test_ping())"
```

Your app **must** verify the `X-VFS-Signature` HMAC over the **raw body bytes** —
otherwise anyone who learns the URL can post a fake "registration confirmed".
Reference implementation is in `API_TUNNEL_SETUP.md`, Section 3.3.

Check nothing is undelivered:

```powershell
python -c "import sys; sys.path.insert(0,'.'); from src.utils import webhook; print(webhook.deadletter_count(), 'undelivered')"
```

**Expect:** `0 undelivered`.

---

## Troubleshooting

| Symptom | Cause / what to do |
|---|---|
| `LoginFormNotReadyError: Login form never appeared within 15s` | **Cloudflare blocked that proxy exit IP.** Environmental and transient, not a wiring fault. Retry; if it persists, rotate the proxy pool entry. |
| Job `failed`, `exit_code: 1` | Read `log_file` from the job record — the child's full stdout/stderr is there. |
| Everything says `skipped` | Expected while `register_enabled = false`. That is Stage 1 working correctly. |
| `already registered ... (ref ...)` | Guard 6b. That client already holds an entry on that route. Correct behaviour. |
| `409` on trigger | A job is already running (single-flight). `GET /jobs` to see it. |
| `503` on trigger | The run lock is held by the scheduled slot checker. Wait for it. |
| `422` with `problems[]` | Validation. Each entry names the exact field and the fix. |
| Garbled dashes in terminal output | Your terminal's decoding, not the API. Read via `Invoke-RestMethod` rather than piping raw bytes. |
