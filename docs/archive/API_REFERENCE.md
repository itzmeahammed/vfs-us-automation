# API Reference

Complete reference for the VFS Local Trigger API. Every request and response
below was **captured from the running server** — not written from memory.

- **Base URL (local):** `http://127.0.0.1:8000`
- **Base URL (public):** your ngrok URL, e.g. `https://abc123.ngrok-free.app`
- **Auth:** header `X-Webhook-Secret-Token: <secret>` on every endpoint except `/health`
- **Content type:** `application/json`

**Contents**

| | |
|---|---|
| [Authentication](#authentication) | [Errors](#error-format) |
| [`GET /health`](#get-health) | [`GET /routes/{route}/readiness`](#get-routesrouterreadiness) |
| [`POST /clients`](#post-clients) | [`GET /clients`](#get-clients) |
| [`GET /clients/{id}`](#get-clientsclient_id) | [`PUT /clients/{id}`](#put-clientsclient_id) |
| [`DELETE /clients/{id}`](#delete-clientsclient_id) | [`POST /clients/{id}/enable`](#post-clientsclient_idenable) |
| [`POST /clients/{id}/disable`](#post-clientsclient_iddisable) | [`POST /trigger/waitlist`](#post-triggerwaitlist) |
| [`GET /jobs`](#get-jobs) | [`GET /jobs/{id}`](#get-jobsjob_id) |
| [`POST /jobs/{id}/cancel`](#post-jobsjob_idcancel) | [`GET /status`](#get-status) |
| [`GET /status/dangling`](#get-statusdangling) | [`POST /status/resolve`](#post-statusresolve) |
| [Outbound webhooks](#outbound-webhooks) | [Integration recipe](#integration-recipe) |

---

## Authentication

Every endpoint except `GET /health` requires:

```
X-Webhook-Secret-Token: <your 64-char secret>
```

Generate it once:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

The server refuses to start if the secret is missing or under 32 characters.
Comparison is constant-time, and **all** auth failures (missing, malformed,
wrong) return an identical 401 — confirming a valid header name would be free
reconnaissance for an attacker.

```json
{
  "error": "unauthorized",
  "detail": "Unauthorized: missing or invalid authentication token.",
  "status_code": 401
}
```

**Rate limit:** 20 requests / 60 s per IP → `429` with a `Retry-After` header.

---

## Error format

Every non-2xx response uses one envelope:

```json
{
  "error": "not_found",
  "detail": "No client 'nope'.",
  "status_code": 404
}
```

Client-validation failures add a top-level `problems` array:

```json
{
  "error": "client_invalid",
  "detail": "2 problem(s) with this client.",
  "problems": [
    {
      "field": "account",
      "message": "\"account\" must be the VFS login email address; got 'notanemail'.",
      "severity": "error"
    },
    {
      "field": "combos",
      "message": "\"Nope - Fake\" is not a combination of config/routes/AE-CHE.json.",
      "severity": "error",
      "hint": "Available: Abu Dhabi - SCHENGEN; Dubai - SCHENGEN"
    }
  ],
  "status_code": 422
}
```

`field` maps to your form input. `hint` is safe to show the user. **All problems
are returned at once** — no whack-a-mole.

| Code | Meaning |
|---|---|
| `200` | OK |
| `201` | Client created |
| `202` | Job accepted (spawned, not finished) |
| `400` | Malformed request |
| `401` | Missing/invalid token |
| `404` | No such client or job |
| `409` | Conflict — client exists, job running, or registration in flight |
| `413` | Body over 64 KB |
| `422` | Validation failed — see `problems` |
| `429` | Rate limited |
| `500` | Server error (details in the server log, not the response) |

---

## `GET /health`

Liveness probe. **No auth.** Use it to confirm the tunnel reaches your machine
without handing out the token.

```bash
curl https://YOUR-URL.ngrok-free.app/health
```

```json
{
  "status": "ok",
  "version": "1.0.0",
  "server_time": "2026-08-20T09:14:02.104332+00:00"
}
```

---

## `GET /routes/{route}/readiness`

**Call this before showing a signup form.** Returns whether a route can accept
registrations, and the exact combination labels to populate your dropdown.

Combination labels must match the route config character-for-character, so never
let a user free-type them.

```bash
curl -H "X-Webhook-Secret-Token: $TOKEN" \
  https://YOUR-URL.ngrok-free.app/routes/AE-CHE/readiness
```

**200 — ready**

```json
{
  "route": "AE-CHE",
  "ready": true,
  "combos": ["Abu Dhabi - SCHENGEN", "Dubai - SCHENGEN"],
  "problems": []
}
```

**200 — not ready** (still 200; `ready` is the answer)

```json
{
  "route": "AE-DEU",
  "ready": false,
  "combos": ["Abu Dhabi - Business Visa", "Abu Dhabi - Short Term Visa"],
  "problems": [
    {
      "field": "route",
      "message": "config/waitlist/AE-DEU.json has \"enabled\": false — registration is switched off for this route.",
      "severity": "error",
      "hint": "Set it to true once the route's page mapping is trusted."
    }
  ]
}
```

A route is ready only when **all five** hold: a login URL exists, the route file
has combinations, the waitlist config exists and parses, it is `enabled`, and it
declares exactly one committing step.

> As of 2026-08-20 only **AE-CHE** and **AE-NLD** are ready.

---

## `POST /clients`

Create a client file from your app's signup data.

```bash
curl -X POST https://YOUR-URL.ngrok-free.app/clients \
  -H "X-Webhook-Secret-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "u10432-che",
    "route": "AE-CHE",
    "combos": ["Dubai - SCHENGEN"],
    "account": "client.waitlist@example.com",
    "account_password": "their-vfs-password",
    "first_name": "PRIYA",
    "last_name": "SHARMA",
    "nationality": "India",
    "passport_number": "M4455667",
    "date_of_birth": "1993-07-22",
    "phone_country_code": "971",
    "phone_number": "509998888",
    "email": "priya@example.com",
    "address_line_1": "VILLA 12",
    "address_line_2": "JUMEIRAH, DUBAI",
    "gender": "Female",
    "passport_expiry": "2032-05-05"
  }'
```

### Required fields

| Field | Type | Notes |
|---|---|---|
| `client_id` | string | Becomes the filename. Lowercase slug `[a-z0-9_-]`, max 64. Case is normalised, so `U10432-CHE` → `u10432-che`. Suggested: `<appuserid>-<route>` |
| `route` | string | `AE-CHE` form (2 letters, dash, 2–4 letters) |
| `combos` | string[] | Labels **exactly** as returned by `/readiness` |

### Optional fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | Created **parked**. See below |
| `account` | string | — | VFS login email. All-or-nothing with `account_password` |
| `account_password` | string | — | Stored, **never returned** by any endpoint |

### Form fields

Everything else is passed through as form data (`first_name`, `passport_number`,
…). Field names are free-form because each portal asks for different things —
they are validated against the route's actual `{{placeholder}}` requirements, not
an allow-list.

Rejected: lists, nested objects, and anything resembling a CSS selector (client
files hold data, never page structure).

### 201 Created

```json
{
  "client_id": "demo-che",
  "created": true,
  "enabled": false,
  "message": "Client created. It is PARKED (enabled=false) — call POST /clients/demo-che/enable to arm it.",
  "client": {
    "client_id": "demo-che",
    "route": "AE-CHE",
    "combos": ["Dubai - SCHENGEN"],
    "enabled": false,
    "account": "demo@example.com",
    "first_name": "DEMO",
    "last_name": "USER",
    "nationality": "India",
    "passport_number": "X1****67",
    "date_of_birth": "19******01",
    "phone_number": "50*****67",
    "email": "de************om",
    "has_account_password": true
  }
}
```

> **Two things to notice.**
>
> **Created ≠ armed.** `enabled: false` unless you explicitly send `true`. A bug
> in your app must not be able to arm a fleet of clients for live registration.
>
> **Secrets in, never out.** `account_password` is stored (the run needs it to
> log in) but returned by no endpoint — only `has_account_password: true`. PII
> is masked: `M4455667` → `X1****67`.

### Errors

| Code | Cause |
|---|---|
| `409` | `client_id` already exists — use `PUT` to update |
| `422` | Validation failed; see `problems` |

---

## `GET /clients`

List clients. Optional `?route=AE-CHE` filter.

```bash
curl -H "X-Webhook-Secret-Token: $TOKEN" \
  "https://YOUR-URL.ngrok-free.app/clients?route=AE-CHE"
```

```json
{
  "count": 1,
  "clients": [
    {
      "client_id": "demo-che",
      "route": "AE-CHE",
      "combos": ["Dubai - SCHENGEN"],
      "enabled": false
    }
  ]
}
```

An unreadable client file is skipped rather than failing the whole listing.

---

## `GET /clients/{client_id}`

One client, redacted, **plus whether it would actually run right now**.

```json
{
  "client_id": "demo-che",
  "client": {
    "client_id": "demo-che",
    "route": "AE-CHE",
    "combos": ["Dubai - SCHENGEN"],
    "enabled": false,
    "passport_number": "X1****67",
    "has_account_password": true
  },
  "runnable": true,
  "problems": []
}
```

`runnable` re-runs the full pre-flight. If it is `false`, `problems` says why —
useful for showing "action needed" in your UI.

**404** if unknown.

---

## `PUT /clients/{client_id}`

Update a client. Same body as `POST /clients`; supplied fields replace existing
ones.

**409 if a registration is in flight:**

```json
{
  "error": "conflict",
  "detail": "A registration for 'demo-che' on 'Dubai - SCHENGEN' is in flight (status=pending). Wait for it to resolve, or clear it with `python -m src.waitlist resolve`, before editing.",
  "status_code": 409
}
```

The data being typed into the VFS portal must not change underneath the run.

---

## `DELETE /clients/{client_id}`

```json
{ "client_id": "demo-che", "deleted": true }
```

**Retention is permanent** — nothing expires on its own, so this is the only way
a client file goes away. Call it when a client is done with you: the file holds a
passport number, date of birth, and VFS password.

---

## `POST /clients/{client_id}/enable`

Arm a client for registration.

```json
{
  "client_id": "demo-che",
  "created": false,
  "enabled": true,
  "message": "Client enabled — it will be included in waitlist runs.",
  "client": { "...": "redacted view" }
}
```

**Refuses (422) to arm a client that would not run.** Arming something broken
just moves the failure to 3am.

---

## `POST /clients/{client_id}/disable`

Park a client without deleting their data. Runs skip them until re-enabled.

```json
{
  "client_id": "demo-che",
  "created": false,
  "enabled": false,
  "message": "Client parked — runs will skip them until re-enabled."
}
```

---

## `POST /trigger/waitlist`

Start a waitlist run **in the background**. Returns immediately (~0.03 s) — 202
means *spawned*, never *finished*.

```bash
curl -X POST https://YOUR-URL.ngrok-free.app/trigger/waitlist \
  -H "X-Webhook-Secret-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"registrant": "u10432-che", "dry_run": true}'
```

### Body — every field optional

| Field | Type | Default | Notes |
|---|---|---|---|
| `route` | string | all | e.g. `AE-CHE` |
| `registrant` | string | all | One client id |
| `combo` | string | all | e.g. `Dubai - SCHENGEN` |
| `dry_run` | bool | **`true`** | `false` submits for real |
| `reason` | string | — | Note recorded with the job (max 280) |

> **`dry_run` defaults to `true`.** A trigger cannot submit unless it explicitly
> sends `false`, and `[waitlist] register_enabled` gates it behind that.

### 202 Accepted

```json
{
  "accepted": true,
  "message": "Job started in the background.",
  "job": {
    "job_id": "9062d84f556732e3",
    "status": "running",
    "command": ["python", "-m", "src.waitlist", "run", "--json",
                "--registrant", "u10432-che", "--dry-run"],
    "pid": 24184,
    "started_at": "2026-08-20T09:20:11.104332+00:00",
    "log_file": "C:\\...\\logs\\api_jobs\\job-20260820-092011-9062d84f.log",
    "results": [],
    "needs_attention": false
  }
}
```

Poll `GET /jobs/{job_id}` for the outcome.

**409** if a job is already running (single-flight), or if the scheduled slot
check holds the global run lock.

---

## `GET /jobs`

Recent jobs, newest first. `?limit=20` (max 100).

```json
{
  "count": 2,
  "active_job_id": null,
  "jobs": [ { "job_id": "...", "status": "succeeded", "...": "..." } ]
}
```

---

## `GET /jobs/{job_id}`

**This is where you learn what happened per client.**

```json
{
  "job_id": "9062d84f556732e3",
  "status": "succeeded",
  "exit_code": 0,
  "outcome": "completed",
  "needs_attention": false,
  "started_at": "2026-08-20T09:20:11+00:00",
  "finished_at": "2026-08-20T09:25:47+00:00",
  "log_file": "C:\\...\\logs\\api_jobs\\job-20260820-092011-9062d84f.log",
  "results": [
    {
      "route": "AE-CHE",
      "combo": "Dubai - SCHENGEN",
      "registrant_id": "u10432-che",
      "status": "skipped",
      "account": "de***@example.com",
      "reason": "waitlist registration is switched off ([waitlist] register_enabled = false)",
      "vfs_reference": null
    }
  ]
}
```

### Job status

| Status | Meaning |
|---|---|
| `running` | In progress |
| `succeeded` | Ran to completion — **check `results[]`**, clients may still be skipped |
| `failed` | At least one client failed |
| `slots_available` | **A real bookable slot exists.** Better than success — tell the client to *book*, not queue. The run stopped deliberately |
| `timed_out` | Exceeded `job_timeout_seconds` and was killed |
| `cancelled` | Stopped via the API or shutdown |

### Per-client status in `results[]`

| Status | Meaning |
|---|---|
| `success` | Registered. `vfs_reference` holds the confirmation |
| `dry_run` | Walked the flow, stopped before submitting |
| `skipped` | A guard declined — see `reason` |
| `failed` | Failed before committing; nothing submitted |
| `pending` | **Submit in flight, outcome unseen** |
| `unknown` | **Submitted, confirmation unreadable** |

> **`needs_attention: true`** means a `pending`/`unknown` result. A human must
> check the VFS portal. **Never auto-retry** — the entry may already exist, and
> a retry could book a duplicate appointment. Resolve via
> [`POST /status/resolve`](#post-statusresolve).

---

## `POST /jobs/{job_id}/cancel`

Kill a running job and its child processes (including Chrome). Returns the job
record. **409** if it is not running.

---

## `GET /status`

One call: is the system armed, and is anything stuck?

```json
{
  "posture": "PARKED — [waitlist] register_enabled is false, so nothing can register. Detection and notification still work.",
  "switches": {
    "register_enabled": false,
    "dry_run": true,
    "auto_trigger_enabled": false,
    "auto_trigger_dry_run": true,
    "max_per_run": 1,
    "max_per_day": 5
  },
  "routes": [
    {"route": "AE-CHE", "ready": true,
     "combos": ["Abu Dhabi - SCHENGEN", "Dubai - SCHENGEN"],
     "clients": 1, "problems": []}
  ],
  "clients_total": 6,
  "dangling": [],
  "needs_attention": false,
  "webhook_configured": false,
  "undelivered_webhooks": 0
}
```

`posture` is plain language so you need not reason about four booleans:

| Posture | Meaning |
|---|---|
| `PARKED` | Nothing can register. Detection still works |
| `MANUAL` | Registration enabled, but runs only start when you ask |
| `AUTO (DRY RUN)` | Openings fire runs that stop before submitting |
| `AUTO (LIVE)` | Openings **register real clients with no human in the loop** |

Watch `undelivered_webhooks` — anything above 0 means your app missed events.

---

## `GET /status/dangling`

Journal rows needing a human decision.

```json
[
  {
    "route": "AE-CHE",
    "combo": "Dubai - SCHENGEN",
    "registrant_id": "u10432-che",
    "status": "unknown",
    "reason": "confirmation could not be read",
    "started_at": "2026-08-20T09:25:00"
  }
]
```

Each of these **blocks its client from registering again** — correct, since a
retry could duplicate a real appointment, but it means the client is silently
parked until resolved. Surface these in your admin UI.

---

## `POST /status/resolve`

Record what a human found on the VFS portal, unblocking the client.

```bash
curl -X POST https://YOUR-URL.ngrok-free.app/status/resolve \
  -H "X-Webhook-Secret-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "route": "AE-CHE",
    "combo": "Dubai - SCHENGEN",
    "registrant_id": "u10432-che",
    "status": "success",
    "reason": "verified WL-77231 on the portal"
  }'
```

| Field | Notes |
|---|---|
| `status` | **Only `success` or `failed`.** Resolving replaces ambiguity with a checked fact |
| `reason` | Free text, max 280 |

> **Check the portal first.** Marking a real registration `failed` lets the bot
> register that client again — a duplicate appointment.

```json
{ "resolved": true, "entry": { "...": "the updated journal row" } }
```

---

## Outbound webhooks

The reverse direction: the bot POSTs to **your** app. Configure in
`config/config.local.ini`:

```ini
[webhook]
enabled = true
url = https://www.travnooker.com/webhooks/vfs
secret = <64-char hex, DIFFERENT from the API token>
timeout_seconds = 10.0
```

### What arrives

```http
POST /webhooks/vfs
X-VFS-Signature: sha256=8cec2a8d843bfe8e...
X-VFS-Event: registration.succeeded
X-VFS-Delivery: 12-1787150470
```

```json
{
  "version": 1,
  "event": "registration.succeeded",
  "sequence": 12,
  "sent_at": "2026-08-20T09:37:02.104332+00:00",
  "data": {
    "route": "AE-CHE",
    "combo": "Dubai - SCHENGEN",
    "registrant_id": "u10432-che",
    "status": "success",
    "vfs_reference": "WL-77231"
  }
}
```

### Events

| Event | Your app should |
|---|---|
| `waitlist.opened` | Tell waiting clients a window opened |
| `registration.succeeded` | Confirm; store `vfs_reference` |
| `registration.failed` | Show `reason`; safe to retry later |
| `registration.needs_attention` | **Escalate to a human. Never auto-retry** |
| `slots.available` | Tell the client to **book**, not wait |
| `test.ping` | Ignore (wiring check) |

### Verify the signature — required

Without this, anyone who learns your callback URL can post a fake
"registration confirmed".

```javascript
import crypto from "node:crypto";

// express.raw, NOT express.json — a re-serialised body will not verify.
app.post("/webhooks/vfs",
  express.raw({ type: "application/json" }),
  (req, res) => {
    const expected = "sha256=" + crypto
      .createHmac("sha256", process.env.VFS_WEBHOOK_SECRET)
      .update(req.body).digest("hex");
    const a = Buffer.from(expected);
    const b = Buffer.from(req.get("X-VFS-Signature") || "");
    if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
      return res.status(401).end();     // 4xx = permanent, not retried
    }

    res.status(200).end();              // ACK fast; queue the real work
    void handleVfsEvent(JSON.parse(req.body.toString("utf8")));
  });
```

### Delivery semantics

- Retries ~1 s, 4 s, 10 s on 5xx, timeouts, connection errors
- **4xx is not retried** (except 408/429)
- Exhausted deliveries append to `state/webhook_deadletter.jsonl`
- Dedupe on `X-VFS-Delivery`; order with `sequence`
- **PII is scrubbed** — identify clients by `registrant_id`
- **A failed webhook never fails a registration**

---

## Integration recipe

Minimal correct flow for travnooker.com.

### 1. Signup form

```javascript
const VFS = process.env.VFS_API_URL;
const auth = {
  "X-Webhook-Secret-Token": process.env.VFS_API_TOKEN,
  "Content-Type": "application/json",
  "ngrok-skip-browser-warning": "true",   // free-tier ngrok only
};

// Populate the combination dropdown — never let users free-type these.
const { ready, combos, problems } =
  await fetch(`${VFS}/routes/AE-CHE/readiness`, { headers: auth }).then(r => r.json());

if (!ready) {
  showMessage(problems[0]?.message ?? "This route is not accepting signups.");
}
```

### 2. Create the client

```javascript
const res = await fetch(`${VFS}/clients`, {
  method: "POST", headers: auth,
  body: JSON.stringify({
    client_id: `u${user.id}-che`,      // stable, derived from YOUR user id
    route: "AE-CHE",
    combos: [form.combo],
    account: form.vfsEmail,
    account_password: form.vfsPassword,
    first_name: form.firstName.toUpperCase(),
    // ...remaining form fields
  }),
});

if (res.status === 422) {
  const { problems } = await res.json();
  for (const p of problems) attachFieldError(p.field, p.message, p.hint);
  return;
}
if (res.status === 409) return showMessage("You have already signed up for this route.");
```

### 3. Arm the client

```javascript
// Separate step: created is not armed.
await fetch(`${VFS}/clients/u${user.id}-che/enable`, { method: "POST", headers: auth });
```

### 4. Receive outcomes

```javascript
async function handleVfsEvent({ event, data }) {
  switch (event) {
    case "waitlist.opened":
      return notifyUsers(data.route, data.combos);
    case "registration.succeeded":
      return confirmToUser(data.registrant_id, data.vfs_reference);
    case "slots.available":
      return tellUserToBook(data.route, data.combo, data.banner);
    case "registration.needs_attention":
      return alertStaff(data);           // human check — never auto-retry
    case "registration.failed":
      return recordFailure(data.registrant_id, data.reason);
  }
}
```

### 5. Admin dashboard

```javascript
const status = await fetch(`${VFS}/status`, { headers: auth }).then(r => r.json());

render({
  posture: status.posture,                     // one plain sentence
  stuck: status.dangling,                      // needs human action
  missedEvents: status.undelivered_webhooks,   // >0 means your app missed some
});
```

### Notes

- **`client_id` must be stable and derivable** from your user id — it is the key
  for every later call.
- **Never send the API token to a browser.** Server-side only; anyone with it can
  run jobs on that machine.
- **The ngrok URL changes on restart** on the free tier. Make it configurable.
- **Poll `GET /jobs/{id}`** for triggered runs, or wait for the webhook. `202`
  only means spawned.
