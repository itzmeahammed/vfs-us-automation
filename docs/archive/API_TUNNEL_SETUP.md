# Local Trigger API + ngrok Tunnel — Setup Guide

A remote web app calls a public ngrok URL → ngrok forwards to `127.0.0.1:8000`
on your machine → FastAPI checks the shared secret → a background subprocess
runs the job → the HTTP call returns immediately with a `job_id`.

```
  Web app  ──HTTPS──▶  ngrok edge  ──tunnel──▶  127.0.0.1:8000  ──spawn──▶  job
           X-Webhook-Secret-Token              (loopback only)      (background)
```

> **A note on the brief.** You asked for Cloudflare Quick Tunnels *and* ngrok.
> This guide uses **ngrok**, per your answer. One correction to the premise
> worth stating plainly: **Cloudflare Quick Tunnels have no native
> authentication or IP restriction at all** — that only exists on *named*
> tunnels with Cloudflare Access, which needs an account and a domain. So if
> you ever switch, do not assume the tunnel is protecting you. In this design
> the FastAPI token is the real access control either way, and the tunnel's own
> auth is a second layer on top. See [Appendix A](#appendix-a--if-you-switch-to-cloudflare) for the Cloudflare variant.

---

## What was created

| Path | Purpose |
|---|---|
| [src/api/config.py](src/api/config.py) | Typed settings, `VFSAPI_` env prefix. Refuses to start without a strong token. |
| [src/api/security.py](src/api/security.py) | Constant-time token check + per-IP rate limiting. |
| [src/api/jobs.py](src/api/jobs.py) | Non-blocking subprocess spawn, tracking, timeout, cancel. |
| [src/api/schemas.py](src/api/schemas.py) | Strictly-typed request/response models. |
| [src/api/main.py](src/api/main.py) | The FastAPI app, endpoints, error handling, headers. |
| [src/api/__main__.py](src/api/__main__.py) | `python -m src.api` entry point. |
| [api_scripts/placeholder_job.py](api_scripts/placeholder_job.py) | The placeholder job the API triggers today. |
| [api_scripts/smoke_test.py](api_scripts/smoke_test.py) | 31-check end-to-end verification. |
| [src/api/clients.py](src/api/clients.py) | Client CRUD + route readiness (Phase 1). |
| [src/waitlist/validate.py](src/waitlist/validate.py) | Browser-free structured validation. |
| [src/waitlist/store.py](src/waitlist/store.py) | Atomic client-file writes. |
| [src/utils/webhook.py](src/utils/webhook.py) | Signed outbound callbacks (Phase 4). |
| [src/utils/runlock.py](src/utils/runlock.py) | Global run lock (Phase 0). |
| [requirements-api.txt](requirements-api.txt) | `pip install -r` for the API only. |

`.env.api`, `logs/api_jobs/`, and `ngrok*.yml` are gitignored — secrets and job
logs never enter version control.

---

# SECTION 1 — Local API Wrapper

## 1.1 Install

The bot's own dependencies are untouched; the API's live in a separate file so
you only install them on the machine running the webhook.

```powershell
cd C:\path\to\vfs-malta-slot-checker

# Use the project venv if you have one
.\.venv\Scripts\Activate.ps1

pip install -r requirements-api.txt
```

That resolves to:

```powershell
pip install "fastapi>=0.110,<1" "uvicorn[standard]>=0.29,<1" "pydantic-settings>=2.0,<3"
```

To run the smoke test you also need `httpx` (TestClient's HTTP layer):

```powershell
pip install httpx
```

## 1.2 Generate the shared secret

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

A 64-character hex string. Set it for the current session:

```powershell
$env:VFSAPI_SECRET_TOKEN = "paste_the_64_char_token_here"
```

Persist it for your user (survives reboots — needed for the service setup):

```powershell
[Environment]::SetEnvironmentVariable("VFSAPI_SECRET_TOKEN", "paste_token_here", "User")
```

Or put it in `.env.api` at the repo root (already gitignored):

```ini
VFSAPI_SECRET_TOKEN=paste_the_64_char_token_here
```

The server **refuses to start** if this is missing, shorter than 32 characters,
or a placeholder like `changeme`. That is deliberate: a webhook about to be
published to the internet with no auth is worse than no webhook.

## 1.3 Run the server

```powershell
python -m src.api
```

Expected output:

```
Webhook API v1.0.0 listening on http://127.0.0.1:8000 (docs=off, single_flight=True)
Job command: C:\...\python.exe -m src.waitlist run --json
```

Alternative (equivalent):

```powershell
uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

## 1.4 Verify

```powershell
python api_scripts/smoke_test.py
```

This starts the app in-process and runs 31 checks — auth rejection, injection-
shaped input, a real spawned subprocess, single-flight, timeouts, headers.
Verified passing on this repo:

```
RESULT: 31 passed, 0 failed
```

The wider suite (`python -m pytest -q`) covers the rest: **571 passed**.

Notably it asserts the trigger returns in **~0.03 s** for a job that runs 2 s —
proof the endpoint is genuinely non-blocking, not just described as such.

## 1.5 Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | — | Liveness. Says nothing sensitive. |
| `POST` | `/trigger/waitlist` | ✅ | Spawn the job. Returns `202` + `job_id` immediately. |
| `GET` | `/jobs` | ✅ | Recent job history. |
| `GET` | `/jobs/{job_id}` | ✅ | One job's status. |
| `POST` | `/jobs/{job_id}/cancel` | ✅ | Kill a running job and its children. |
| `POST` | `/clients` | ✅ | Create a client file from the web app's data. |
| `GET` | `/clients` | ✅ | List clients (`?route=` filters). |
| `GET` | `/clients/{id}` | ✅ | One client, redacted, plus whether it would run. |
| `PUT` | `/clients/{id}` | ✅ | Update a client (refused mid-registration). |
| `DELETE` | `/clients/{id}` | ✅ | Delete a client. |
| `POST` | `/clients/{id}/enable` | ✅ | Arm a client (refuses if it would not run). |
| `POST` | `/clients/{id}/disable` | ✅ | Park a client. |
| `GET` | `/routes/{route}/readiness` | ✅ | Can this route register, and its valid combos. |

You said the endpoint list is not final — adding one is a single decorated
function in [src/api/main.py](src/api/main.py); put
`dependencies=[Depends(require_token)]` on it and it inherits the whole
security stack.

### Trigger request body

Every field is optional:

```json
{
  "route": "AE-DEU",
  "registrant": "client_001",
  "combo": "dubai-tourism",
  "dry_run": true,
  "reason": "triggered from admin panel"
}
```

`dry_run` defaults to **`true`**. You must send `false` explicitly for a real
run — the safe direction is the default.

### Test it

```powershell
$token = $env:VFSAPI_SECRET_TOKEN

# Health — no token
curl.exe http://127.0.0.1:8000/health

# Trigger
curl.exe -X POST http://127.0.0.1:8000/trigger/waitlist `
  -H "X-Webhook-Secret-Token: $token" `
  -H "Content-Type: application/json" `
  -d '{\"route\":\"AE-DEU\",\"dry_run\":true}'

# Expect 401
curl.exe -i -X POST http://127.0.0.1:8000/trigger/waitlist `
  -H "Content-Type: application/json" -d '{}'
```

Response to a successful trigger (`202 Accepted`):

```json
{
  "accepted": true,
  "message": "Job started in the background.",
  "job": {
    "job_id": "0f7d70f18fff0fed",
    "status": "running",
    "pid": 24184,
    "started_at": "2026-08-19T16:36:52.104332+00:00",
    "log_file": "C:\\...\\logs\\api_jobs\\job-20260819-163652-0f7d70f18fff0fed.log"
  }
}
```

## 1.6 The job it runs

**It already runs the real waitlist CLI** — `python -m src.waitlist run --json`.
The API appends `--route` / `--registrant` / `--combo` / `--dry-run|--live`
itself, so the configured value is only the command *prefix*.

`dry_run` defaults to **true**, so a trigger cannot submit a registration unless
it explicitly sends `dry_run: false` — and `[waitlist] register_enabled` (false
by default) still gates it behind that.

To run the placeholder instead (to exercise the plumbing without touching VFS):

```powershell
$env:VFSAPI_JOB_COMMAND = '["python","api_scripts/placeholder_job.py"]'
```

`GET /jobs/{id}` reports per-client outcomes parsed from the run:

```json
{
  "status": "succeeded",
  "outcome": "completed",
  "needs_attention": false,
  "results": [
    {"registrant_id": "ahmed", "combo": "Dubai - SCHENGEN",
     "status": "skipped", "reason": "registration is switched off"}
  ]
}
```

`status: "slots_available"` means a real bookable slot appeared, so the run
stopped without waitlisting — a **better** outcome than success.
`needs_attention: true` means a submit is unresolved and a human must check the
VFS account; never retry it automatically.

## 1.7 Security decisions, and why

| Control | Rationale |
|---|---|
| **`hmac.compare_digest`** for the token | `==` short-circuits on the first wrong byte, leaking how many leading characters were correct. Constant-time comparison costs nothing. |
| **Identical 401 for missing/malformed/wrong** | Saying "token present but incorrect" confirms the attacker found the right header name. Free reconnaissance for no benefit. |
| **`create_subprocess_exec` with an argv list** | No shell is ever involved, so there is no string for `;` or `&&` to hide in. Injection is structurally impossible, not filtered against. |
| **Regex-validated `route`/`registrant`/`combo`** | Defence in depth. Even though argv is safe, a value that reaches a CLI flag is constrained to a known shape. |
| **`extra="forbid"` on the request model** | An unexpected field is a `422`, not silently ignored — a caller cannot plant a key hoping a later version honours it. |
| **Binds `127.0.0.1` only, validated** | The tunnel is the only public edge. A non-loopback host raises at startup rather than quietly exposing the API to your LAN. |
| **`/docs`, `/redoc`, `/openapi.json` disabled** | An exposed schema hands over the complete API surface. Set `VFSAPI_ENABLE_DOCS=1` locally when you need them. |
| **Secret stripped from the child env** | The job never needs it; a subprocess that leaks its environment into a log cannot leak the token. The smoke test asserts this. |
| **Output to a file, not a pipe** | An unread pipe fills its OS buffer and deadlocks a chatty child — and the Playwright bot is very chatty. |
| **Single-flight** | Two concurrent waitlist runs would corrupt the journal and double-book accounts. Second trigger gets `409`. |
| **Job timeout + process group kill** | A hung Playwright run is killed rather than pinning single-flight forever. The group kill takes Chrome with it instead of orphaning it. |
| **Per-IP rate limiting** | Blunts a flood from someone who has the URL. Checked *before* the token comparison. |
| **64 KB body cap** | Checked from `Content-Length` before the body is read. |
| **Generic `500` body** | Tracebacks go to the server log, never to the caller. |

---

# SECTION 2 — ngrok Tunnel Setup

## 2.1 Install

**Option A — winget (recommended):**

```powershell
winget install ngrok.ngrok
```

**Option B — Chocolatey:**

```powershell
choco install ngrok
```

**Option C — manual:** download from <https://ngrok.com/download>, unzip
`ngrok.exe` to e.g. `C:\ngrok\`, and add that folder to `PATH`:

```powershell
[Environment]::SetEnvironmentVariable(
  "PATH", $env:PATH + ";C:\ngrok", "User")
```

Verify:

```powershell
ngrok version
```

## 2.2 Authenticate

1. Create a free account at <https://dashboard.ngrok.com/signup>
2. Copy your token from <https://dashboard.ngrok.com/get-started/your-authtoken>
3. Register it (writes to `%LOCALAPPDATA%\ngrok\ngrok.yml`):

```powershell
ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN_HERE
```

Confirm where the config lives:

```powershell
ngrok config check
```

## 2.3 Start the tunnel

With the API already running on port 8000:

```powershell
ngrok http 8000
```

You get:

```
Forwarding    https://abc123-45-67-89.ngrok-free.app -> http://localhost:8000
```

That HTTPS URL is what your web app calls:

```
POST https://abc123-45-67-89.ngrok-free.app/trigger/waitlist
```

**Free-tier caveat:** the subdomain is random and **changes every restart**.
Your web app needs the URL as configurable, not hardcoded. A paid static domain
fixes this:

```powershell
ngrok http 8000 --domain=your-reserved-name.ngrok-free.app
```

### The free-tier browser warning

Free ngrok injects an interstitial HTML warning page on browser requests. It
breaks API clients that don't expect HTML. Suppress it by sending this header
from your web app (alongside the secret token):

```
ngrok-skip-browser-warning: true
```

## 2.4 Restricting who can reach the tunnel

This is the part to be precise about, because the free tier is more limited
than it appears.

### What free ngrok actually gives you

Traffic Policy is available on the free tier and runs **at the ngrok edge** —
requests are rejected before they ever reach your machine. Create
`ngrok-policy.yml` next to your config:

```yaml
# ngrok-policy.yml — enforced at the ngrok edge, before traffic reaches you.
on_http_request:
  # 1) Only allow the endpoints that actually exist.
  - expressions:
      - "!(req.url.path.startsWith('/trigger/') ||
           req.url.path.startsWith('/jobs') ||
           req.url.path == '/health')"
    actions:
      - type: custom-response
        config:
          status_code: 404
          body: '{"error":"not_found"}'
          headers:
            content-type: application/json

  # 2) Require the shared secret at the EDGE as well as in the app.
  #    Defence in depth: a bad token never reaches your machine at all.
  - expressions:
      - "req.url.path != '/health'"
      - "!('x-webhook-secret-token' in req.headers)"
    actions:
      - type: custom-response
        config:
          status_code: 401
          body: '{"error":"unauthorized"}'
          headers:
            content-type: application/json

  # 3) Reject anything that is not JSON POST/GET.
  - expressions:
      - "req.method != 'GET' && req.method != 'POST'"
    actions:
      - type: deny
        config:
          status_code: 405
```

Run with it:

```powershell
ngrok http 8000 --traffic-policy-file="C:\ngrok\ngrok-policy.yml"
```

### IP restriction — paid only

IP restrictions are a **paid** ngrok feature. If your plan includes it, add to
the policy:

```yaml
on_tcp_connect:
  - actions:
      - type: restrict-ips
        config:
          # Your web app's egress IPs. Everything else is dropped at the edge.
          allow:
            - 203.0.113.45/32
            - 198.51.100.0/24
```

Get your web app's egress IP from its host (Vercel, Render, AWS all publish
these). **Verify the ranges before locking yourself out.**

### Basic auth — paid only

```powershell
ngrok http 8000 --basic-auth "webhookuser:a-long-random-password"
```

Free-tier users get `ERR_NGROK_313` here. Not a problem: your
`X-Webhook-Secret-Token` already does this job, and does it better — basic auth
credentials travel in a base64 header that is trivially decoded from any log
that captures headers.

### The honest summary

| Control | Free | Paid |
|---|:--:|:--:|
| HTTPS at the edge | ✅ | ✅ |
| Traffic Policy (path/header/method filtering) | ✅ | ✅ |
| Static domain | ❌ | ✅ |
| IP allowlist | ❌ | ✅ |
| Basic auth | ❌ | ✅ |
| **App-level token (this project)** | ✅ | ✅ |

**On the free tier the FastAPI token is your real security boundary.** The
Traffic Policy is a genuine second layer, but do not let it lull you: keep the
token strong, keep it out of git, and rotate it if it is ever pasted anywhere.

## 2.5 Running ngrok persistently as a Windows service

So it survives a closed terminal, a logout, and a reboot.

### Step 1 — Put both tunnel and config in one file

Edit `%LOCALAPPDATA%\ngrok\ngrok.yml`:

```yaml
version: "3"
agent:
  authtoken: YOUR_NGROK_AUTHTOKEN_HERE
  log: C:\ngrok\ngrok.log
  log_level: info

endpoints:
  - name: vfs-webhook
    url: https://your-reserved-name.ngrok-free.app   # omit on free tier
    upstream:
      url: 8000
    traffic_policy:
      file: C:\ngrok\ngrok-policy.yml
```

Validate it:

```powershell
ngrok config check
```

### Step 2 — Install the service (run PowerShell **as Administrator**)

```powershell
ngrok service install --config "$env:LOCALAPPDATA\ngrok\ngrok.yml"
ngrok service start
```

Verify and control:

```powershell
Get-Service ngrok
Restart-Service ngrok
Stop-Service ngrok
Get-Content C:\ngrok\ngrok.log -Tail 30 -Wait
```

Remove it:

```powershell
ngrok service stop
ngrok service uninstall
```

> **Config path gotcha.** The service runs as `LOCAL SYSTEM`, which has a
> *different* `%LOCALAPPDATA%` than your user. Always pass `--config` with an
> absolute path, as above, or the service starts with no authtoken and fails.

### Step 3 — Make the API itself persistent too

The tunnel is useless if the API behind it is down. Register the FastAPI server
as a scheduled task that starts at boot. This repo already uses Task Scheduler
(see [setup_task.ps1](setup_task.ps1)), so this matches the existing pattern.

Run **as Administrator**:

```powershell
$repo = "C:\path\to\vfs-malta-slot-checker"
$py   = "$repo\.venv\Scripts\python.exe"   # or (Get-Command python).Source

$action = New-ScheduledTaskAction `
    -Execute $py `
    -Argument "-m src.api" `
    -WorkingDirectory $repo

$trigger = New-ScheduledTaskTrigger -AtStartup

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U `
    -RunLevel Limited          # least privilege: it does not need admin

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)   # never auto-kill a long server

Register-ScheduledTask -TaskName "VFS-Webhook-API" `
    -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Local FastAPI webhook that triggers VFS jobs."
```

Because the task runs as *your* user, it inherits the `VFSAPI_SECRET_TOKEN` you
set with `[Environment]::SetEnvironmentVariable(..., "User")`. Confirm:

```powershell
Start-ScheduledTask -TaskName "VFS-Webhook-API"
Get-ScheduledTask -TaskName "VFS-Webhook-API" | Get-ScheduledTaskInfo
curl.exe http://127.0.0.1:8000/health
```

### Step 4 — Confirm the whole chain

```powershell
# 1. API up locally
curl.exe http://127.0.0.1:8000/health

# 2. ngrok service up
Get-Service ngrok
curl.exe https://YOUR-NGROK-URL.ngrok-free.app/health

# 3. Full authenticated trigger through the tunnel
curl.exe -X POST https://YOUR-NGROK-URL.ngrok-free.app/trigger/waitlist `
  -H "X-Webhook-Secret-Token: $env:VFSAPI_SECRET_TOKEN" `
  -H "ngrok-skip-browser-warning: true" `
  -H "Content-Type: application/json" `
  -d '{\"route\":\"AE-DEU\",\"dry_run\":true}'

# 4. Watch the job run
Get-ChildItem "$repo\logs\api_jobs" | Sort-Object LastWriteTime -Desc |
  Select-Object -First 1 | Get-Content -Wait
```

---

## Calling it from your web app

```javascript
// Server-side ONLY. This token must never reach a browser bundle —
// anyone with it can run jobs on the machine.
const res = await fetch(`${process.env.VFS_WEBHOOK_URL}/trigger/waitlist`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-Webhook-Secret-Token": process.env.VFS_WEBHOOK_SECRET,
    "ngrok-skip-browser-warning": "true",
  },
  body: JSON.stringify({ route: "AE-DEU", dry_run: true }),
});

if (res.status === 202) {
  const { job } = await res.json();
  console.log("Job started:", job.job_id);   // poll GET /jobs/{id} for the outcome
} else if (res.status === 409) {
  console.warn("A job is already running.");
}
```

---

## Configuration reference

Every setting is a `VFSAPI_`-prefixed env var, or a line in `.env.api`.

| Variable | Default | Meaning |
|---|---|---|
| `VFSAPI_SECRET_TOKEN` | *(required)* | Shared secret. Min 32 chars. |
| `VFSAPI_HOST` | `127.0.0.1` | Bind address. Loopback enforced. |
| `VFSAPI_PORT` | `8000` | Bind port. |
| `VFSAPI_JOB_COMMAND` | placeholder | argv list, as JSON. |
| `VFSAPI_JOB_TIMEOUT_SECONDS` | `3600` | Kill a job after this long. |
| `VFSAPI_SINGLE_FLIGHT` | `true` | Refuse a second concurrent job. |
| `VFSAPI_RATE_LIMIT_REQUESTS` | `20` | Requests per window, per IP. |
| `VFSAPI_RATE_LIMIT_WINDOW_SECONDS` | `60` | Window length. |
| `VFSAPI_JOB_HISTORY_LIMIT` | `100` | Finished jobs kept in memory. |
| `VFSAPI_ENABLE_DOCS` | *(unset)* | `1` re-enables `/docs`. Local only. |
| `VFSAPI_LOG_LEVEL` | `INFO` | Server log level. |

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `FATAL: webhook API configuration is invalid` | `VFSAPI_SECRET_TOKEN` unset or under 32 chars. Generate one (§1.2). |
| `401` when you sent a token | Header name must be exactly `X-Webhook-Secret-Token`. Check for a trailing newline from copy-paste. |
| `409 conflict` | A job is already running. `GET /jobs` to see it; `POST /jobs/{id}/cancel` to stop it. |
| `422` on a valid-looking route | Route must be `AA-BBB` — two letters, dash, three letters. |
| HTML instead of JSON | Free ngrok interstitial. Send `ngrok-skip-browser-warning: true`. |
| ngrok service starts then dies | It runs as `LOCAL SYSTEM` with a different `%LOCALAPPDATA%`. Pass `--config` with an absolute path. |
| Tunnel is up, `502` from ngrok | The API is down. `curl http://127.0.0.1:8000/health`. |
| URL changed after restart | Free-tier behaviour. Reserve a domain, or make the URL configurable in the web app. |
| Job starts then instantly fails | Read the `log_file` path from the trigger response — the child's stdout/stderr is all there. |

---

## Appendix A — If you switch to Cloudflare

Quick Tunnels need no account and no domain:

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:8000
```

You get a random `https://<random-words>.trycloudflare.com` URL.

**What you must know before relying on it:**

- Quick Tunnels have **no authentication, no IP restriction, and no access
  control of any kind.** Anyone with the URL reaches your machine. The FastAPI
  token is doing 100% of the work.
- The URL changes on every restart, and there is no way to reserve one.
- There is no uptime guarantee — Cloudflare positions them for testing only.

To get real edge access control you need a **named tunnel** (free Cloudflare
account + a domain on Cloudflare):

```powershell
cloudflared tunnel login
cloudflared tunnel create vfs-webhook
cloudflared tunnel route dns vfs-webhook webhook.yourdomain.com

# config.yml
#   tunnel: <TUNNEL-UUID>
#   credentials-file: C:\Users\<you>\.cloudflared\<UUID>.json
#   ingress:
#     - hostname: webhook.yourdomain.com
#       service: http://127.0.0.1:8000
#     - service: http_status:404

cloudflared service install
```

Then, in the Cloudflare Zero Trust dashboard, add an **Access application** on
`webhook.yourdomain.com` with a **service-token** policy. Your web app sends:

```
CF-Access-Client-Id: <id>.access
CF-Access-Client-Secret: <secret>
```

That is genuine edge authentication — and it is the *only* Cloudflare
configuration that provides it. Named tunnels are free; the domain is the cost.

---

# SECTION 3 — Outbound Webhooks (bot → your app)

Sections 1–2 let your app *push* to the bot. This is the return path: the bot
POSTs to your app when something happens, so you never poll.

## 3.1 Configure

Generate a signing secret — **different** from `VFSAPI_SECRET_TOKEN` (that one
authenticates inbound requests; this one proves outbound ones came from the bot):

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Put it in `config/config.local.ini` (gitignored), **not** `config/config.ini`:

```ini
[webhook]
enabled = true
url = https://your-app.example.com/webhooks/vfs
secret = <the 64-char hex secret>
timeout_seconds = 10.0
```

Verify the wiring with a harmless ping:

```powershell
python -c "import sys; sys.path.insert(0,'.'); from src.utils.config_reader import initialize_config; initialize_config(); from src.utils import webhook; print(webhook.send_test_ping())"
```

## 3.2 What arrives

```http
POST /webhooks/vfs
Content-Type: application/json
X-VFS-Signature: sha256=<hmac>
X-VFS-Event: registration.succeeded
X-VFS-Delivery: 12-1787150470
```
```json
{
  "version": 1,
  "event": "registration.succeeded",
  "sequence": 12,
  "sent_at": "2026-08-19T18:41:02.104332+00:00",
  "data": {
    "route": "AE-CHE",
    "combo": "Dubai - SCHENGEN",
    "registrant_id": "u10432-che",
    "status": "success",
    "vfs_reference": "WL-99887",
    "reason": ""
  }
}
```

### Events

| Event | Meaning | What your app should do |
|---|---|---|
| `waitlist.opened` | A waitlist opened for these combos | Inform waiting clients |
| `registration.succeeded` | Client is on the waitlist | Confirm, store `vfs_reference` |
| `registration.failed` | Not registered (includes `skipped`) | Show `reason`; safe to retry later |
| `registration.needs_attention` | **Submit is pending/unknown** | **Escalate to a human. Never auto-retry** — the entry may already exist |
| `slots.available` | A real bookable slot exists | Tell the client to **book**, not wait |
| `test.ping` | Wiring check | Ignore |

> `status: "skipped"` is delivered too — your client is waiting on an answer,
> and "we checked, here's why nothing happened" is a real answer. Telegram
> stays quieter on purpose; it only pings when a human is needed.

## 3.3 Verify the signature — REQUIRED

Without this, anyone who learns your callback URL can post a fake
"registration confirmed". Compute HMAC-SHA256 over the **raw request body**
(not a re-serialised object) and compare in constant time.

```javascript
import crypto from "node:crypto";

// IMPORTANT: needs the RAW body. In Express use
// express.raw({ type: "application/json" }) on this route, NOT express.json().
export function verifyVfsSignature(rawBody, header, secret) {
  const expected = "sha256=" +
    crypto.createHmac("sha256", secret).update(rawBody).digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(header || "");
  // timingSafeEqual throws on a length mismatch, so check that first.
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

app.post("/webhooks/vfs",
  express.raw({ type: "application/json" }),
  (req, res) => {
    if (!verifyVfsSignature(req.body, req.get("X-VFS-Signature"),
                            process.env.VFS_WEBHOOK_SECRET)) {
      return res.status(401).end();          // 4xx = permanent, not retried
    }
    const evt = JSON.parse(req.body.toString("utf8"));

    // Return 2xx FAST. Slow handlers burn the bot's retry budget; do the real
    // work on a queue.
    res.status(200).end();
    void handleVfsEvent(evt);
  });
```

Python reference implementation: `webhook.verify_signature()` in
[src/utils/webhook.py](src/utils/webhook.py).

## 3.4 Delivery semantics

- **Retries:** ~1s, 4s, 10s on 5xx, timeouts, and connection errors.
- **No retry on 4xx** (except 408/429) — a request you refused won't succeed later.
- **Dead-letter:** exhausted deliveries append to `state/webhook_deadletter.jsonl`
  rather than vanishing. Check it with:
  ```powershell
  python -c "import sys; sys.path.insert(0,'.'); from src.utils import webhook; print(webhook.deadletter_count(), 'undelivered')"
  ```
- **Ordering:** `sequence` is monotonic per bot process; `sent_at` is UTC ISO.
  Treat a lower sequence than one you've already handled as out-of-order.
- **Idempotency:** use `X-VFS-Delivery` to dedupe — a retry after your app
  processed but failed to respond will deliver the same event twice.
- **PII is scrubbed** before sending (passport numbers, emails → `[redacted]`).
  Identify clients by `registrant_id`, which is stable and safe.
- **A failed webhook never fails a registration.** If your app is down, the
  registration still happened — check Telegram and the dead-letter log.
