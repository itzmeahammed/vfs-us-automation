# VFS Slot Checker — API Implementation Guide

This document covers every endpoint in the VFS Local Trigger API. Use it to build the web app backend that consumes this API.

## Connection & Authentication

| Setting | Value |
|---------|-------|
| Base URL | `http://127.0.0.1:8000` (reached through your tunnel) |
| Auth header | `X-Webhook-Secret-Token: <your-64-char-hex-token>` |
| Auth method | Constant-time HMAC comparison; same opaque 401 for missing, malformed, or wrong tokens |
| Rate limit | 20 req/min per IP, 200 req/min global (both configurable) |
| Body limit | 64 KB for JSON endpoints; 2 MB + 64 KB for document uploads |
| Content-Type | `application/json` for all requests and responses (except document upload: `multipart/form-data`) |

Every endpoint except `GET /health` requires the auth header.

A 429 response includes a `Retry-After` header (seconds).

---

## Endpoint Reference

### Meta

#### `GET /health`
**Auth: None** — unauthenticated liveness probe.

**Response** `200`
```json
{
  "status": "ok",
  "version": "1.0.0",
  "server_time": "2026-09-25T10:30:00+00:00"
}
```

---

### Routes

#### `GET /routes`
List all configured routes with readiness summary and client count.

**Response** `200`
```json
{
  "count": 8,
  "routes": [
    {
      "route": "AE-CHE",
      "ready": true,
      "combos": ["Dubai - SCHENGEN", "Abu Dhabi - SCHENGEN"],
      "clients": 2,
      "problems": []
    },
    {
      "route": "AE-DEU",
      "ready": false,
      "combos": [],
      "clients": 0,
      "problems": ["No waitlist page mapping for AE-DEU."]
    }
  ]
}
```

#### `GET /routes/{route}/readiness`
Detailed readiness for one route — combos, form fields, and problems.

**Response** `200`
```json
{
  "route": "AE-CHE",
  "ready": true,
  "combos": ["Dubai - SCHENGEN"],
  "fields": [
    {
      "name": "first_name",
      "label": "First Name",
      "kind": "text",
      "required": true,
      "step": "applicant_details",
      "notes": "",
      "options": [],
      "options_status": "not_a_choice",
      "options_captured_at": ""
    },
    {
      "name": "nationality",
      "label": "Nationality",
      "kind": "select",
      "required": true,
      "step": "applicant_details",
      "notes": "",
      "options": ["India", "Pakistan", "Lebanon"],
      "options_status": "known",
      "options_captured_at": "2026-09-20T14:00:00"
    }
  ],
  "problems": []
}
```
Use `fields` to render a dynamic signup form. Send the values as top-level keys in `POST /clients`.

For fields where `options_status` is `"known"`, render a dropdown and only accept values from `options[]`. For `"unknown"`, render free text and warn the user.

---

### Clients

#### `POST /clients`
Create a new client. Created **parked** (`enabled: false`) by default.

**Request**
```json
{
  "client_id": "u10432-che",
  "route": "AE-CHE",
  "combos": ["Dubai - SCHENGEN"],
  "enabled": false,
  "account": "user@example.com",
  "account_password": "secret123",
  "first_name": "John",
  "last_name": "Doe",
  "nationality": "India",
  "passport_number": "A1234567"
}
```
- `client_id`: lowercase slug, 1-64 chars (letters, digits, underscore, hyphen)
- `route`: e.g. `AE-CHE` (2 letters, dash, 2-4 letters)
- `combos`: at least one label from `/routes/{route}/readiness`
- Extra keys (first_name, nationality, etc.) are the form fields from the route's `fields[]`
- `account_password` is stored 0600 and **never returned** by any endpoint

**Response** `201`
```json
{
  "client_id": "u10432-che",
  "created": true,
  "enabled": false,
  "message": "Client created. It is PARKED (enabled=false) — call POST /clients/u10432-che/enable to arm it.",
  "client": { "...redacted view..." },
  "warnings": []
}
```

#### `GET /clients`
List clients with optional enrichment.

**Query params:**
| Param | Type | Description |
|-------|------|-------------|
| `route` | string | Filter by route id |
| `enabled` | bool | Only armed (true) or parked (false) |
| `runnable` | bool | Only clients that would/wouldn't run now |
| `include` | string | Comma-separated: `status`, `journal`, `all` |

- `include=status` adds `runnable` and `problem_count` per client
- `include=journal` adds `last_status`, `last_run_at`, `vfs_reference`, `run_count`
- `include=all` enables both

**Response** `200`
```json
{
  "count": 2,
  "clients": [
    {
      "client_id": "u10432-che",
      "route": "AE-CHE",
      "combos": ["Dubai - SCHENGEN"],
      "enabled": true,
      "created_at": "2026-09-10T08:00:00",
      "updated_at": "2026-09-20T14:00:00",
      "enabled_at": "2026-09-15T10:00:00",
      "runnable": true,
      "problem_count": 0,
      "last_status": "success",
      "last_run_at": "2026-09-20T14:30:00",
      "vfs_reference": "SWDB79918334684",
      "run_count": 3
    }
  ],
  "included": ["journal", "status"]
}
```

#### `GET /clients/{client_id}`
One client with secrets stripped, PII masked, and pre-flight results.

**Response** `200`
```json
{
  "client_id": "u10432-che",
  "client": {
    "client_id": "u10432-che",
    "route": "AE-CHE",
    "combos": ["Dubai - SCHENGEN"],
    "enabled": true,
    "first_name": "John",
    "last_name": "Doe",
    "passport_number": "A1***67",
    "has_account_password": true
  },
  "runnable": true,
  "problems": []
}
```

#### `PUT /clients/{client_id}`
**Replace** a client entirely. Fields you don't send are **removed**.

Same body as `POST /clients` (minus `client_id` in body — it's in the URL).

#### `PATCH /clients/{client_id}`
Partially update. Only fields you send are changed. Omitted fields keep their current value.

**Request** (example: change phone only)
```json
{ "phone_number": "+971501234567" }
```

#### `DELETE /clients/{client_id}`
Delete a client and their documents.

**Response** `200`
```json
{ "client_id": "u10432-che", "deleted": true, "documents_removed": 1 }
```

#### `POST /clients/{client_id}/enable`
Arm a client for registration. Refuses if pre-flight finds blocking problems.

#### `POST /clients/{client_id}/disable`
Park a client without deleting data.

#### `GET /clients/{client_id}/journal`
Full registration history for one client, newest first.

**Response** `200`
```json
{
  "client_id": "u10432-che",
  "count": 3,
  "rows": [
    {
      "route": "AE-CHE",
      "combo": "Dubai - SCHENGEN",
      "registrant_id": "u10432-che",
      "status": "success",
      "vfs_reference": "SWDB79918334684",
      "account": "user@example.com",
      "reason": "registered successfully",
      "started_at": "2026-09-20T14:30:00",
      "finished_at": "2026-09-20T14:32:00"
    },
    {
      "route": "AE-CHE",
      "combo": "Dubai - SCHENGEN",
      "registrant_id": "u10432-che",
      "status": "dry_run",
      "vfs_reference": null,
      "reason": "dry run — reached review page",
      "started_at": "2026-09-18T10:00:00",
      "finished_at": "2026-09-18T10:02:00"
    }
  ]
}
```

#### `POST /clients/{client_id}/documents`
Upload a document (passport bio page). `multipart/form-data`.

**Form fields:**
- `file`: PNG, JPG, or PDF, max 2 MB
- `kind`: `passport_bio` (only kind today)

**Response** `201`
```json
{
  "client_id": "u10432-che",
  "kind": "passport_bio",
  "stored": true,
  "size_bytes": 245760,
  "extension": ".png",
  "message": "Document stored. Set this client's file field to \"managed\"..."
}
```

#### `GET /clients/{client_id}/documents`
List documents held for a client (metadata only, never the file).

#### `DELETE /clients/{client_id}/documents`
Remove all documents for a client.

---

### Trigger

#### `POST /trigger/waitlist`
Spawn a waitlist registration job in the background.

**Request**
```json
{
  "route": "AE-CHE",
  "registrant": "u10432-che",
  "combo": "Dubai - SCHENGEN",
  "dry_run": true,
  "reason": "Testing the flow"
}
```
All fields are optional. `dry_run` defaults to `true` — send `false` explicitly for a live run.

**Headers:**
- `Idempotency-Key`: (optional) UUID. Retries with the same key return the original job.

**Response** `202 Accepted`
```json
{
  "accepted": true,
  "message": "Job started in the background.",
  "job": { "...JobResponse..." },
  "replayed": false
}
```

**Error responses:**
- `409`: A job is already running (single-flight)
- `500`: Process could not start

---

### Jobs

#### `GET /jobs`
Recent jobs, most recent first. Supports pagination.

**Query params:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 20 | Max jobs to return (1-100) |
| `offset` | int | 0 | Skip first N jobs |
| `needs_attention` | bool | false | Only jobs with unresolved submits |

**Response** `200`
```json
{
  "count": 2,
  "active_job_id": null,
  "jobs": [
    {
      "job_id": "a1b2c3d4e5f6g7h8",
      "status": "succeeded",
      "command": ["python", "-m", "src.waitlist", "run", "--json", "--route", "AE-CHE"],
      "pid": 12345,
      "started_at": "2026-09-25T10:00:00+00:00",
      "finished_at": "2026-09-25T10:05:00+00:00",
      "exit_code": 0,
      "log_file": "/path/to/log",
      "detail": null,
      "payload": { "route": "AE-CHE", "dry_run": true },
      "outcome": "completed",
      "results": [
        {
          "route": "AE-CHE",
          "combo": "Dubai - SCHENGEN",
          "registrant_id": "u10432-che",
          "status": "dry_run",
          "reason": "dry run completed"
        }
      ],
      "needs_attention": false
    }
  ]
}
```

**Job statuses:** `running`, `succeeded`, `failed`, `timed_out`, `cancelled`, `slots_available`, `unknown`

Note: `slots_available` is a **better** outcome than success — a bookable slot exists, so nothing was waitlisted.

#### `GET /jobs/{job_id}`
Status of one job (same shape as a single item in the jobs list).

#### `GET /jobs/{job_id}/logs`
Tail of a job's log file.

**Query params:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `lines` | int | 200 | Trailing lines to return (1-2000) |

**Response** `200`
```json
{
  "job_id": "a1b2c3d4",
  "lines": ["2026-09-25 10:00:01 | INFO | Starting...", "..."],
  "line_count": 150,
  "truncated": false,
  "log_available": true
}
```

#### `POST /jobs/{job_id}/cancel`
Terminate a running job. Returns `409` if not running.

---

### Status & Operations

#### `GET /status`
Everything an operator needs in one call: switches, route readiness, dangling entries, webhook state.

**Response** `200`
```json
{
  "posture": "AUTO (DRY RUN) — an opening waitlist fires a run that walks the whole flow but stops before submitting.",
  "switches": {
    "register_enabled": true,
    "dry_run": false,
    "auto_trigger_enabled": true,
    "auto_trigger_dry_run": true,
    "max_per_run": 5,
    "max_per_day": 10
  },
  "routes": [
    { "route": "AE-CHE", "ready": true, "combos": ["Dubai - SCHENGEN"], "clients": 1, "problems": [] }
  ],
  "clients_total": 3,
  "dangling": [],
  "needs_attention": false,
  "webhook_configured": true,
  "undelivered_webhooks": 0,
  "degraded": []
}
```

If `degraded` is non-empty, some fields are placeholders, not measurements.

#### `GET /status/dangling`
Journal rows needing human resolution.

**Response** `200`
```json
[
  {
    "route": "AE-CHE",
    "combo": "Dubai - SCHENGEN",
    "registrant_id": "u10432-che",
    "status": "pending",
    "reason": "submit clicked, confirmation page timed out",
    "started_at": "2026-09-24T14:30:00"
  }
]
```

#### `POST /status/resolve`
Record what a human found on the VFS portal for a dangling entry.

**Request**
```json
{
  "route": "AE-CHE",
  "combo": "Dubai - SCHENGEN",
  "registrant_id": "u10432-che",
  "status": "success",
  "reason": "Verified on the portal — reference SWDB79918334684"
}
```
`status` must be `"success"` or `"failed"` — the point is to replace ambiguity with fact.

#### `PATCH /status/switches`
Toggle operational switches. Only the fields you send are changed.

**Request** (any subset)
```json
{
  "register_enabled": true,
  "dry_run": false,
  "auto_trigger_enabled": true,
  "auto_trigger_dry_run": false,
  "max_per_run": 5,
  "max_per_day": 20
}
```

**Response** `200`
```json
{
  "updated": ["auto_trigger_dry_run", "dry_run"],
  "switches": {
    "register_enabled": true,
    "dry_run": false,
    "auto_trigger_enabled": true,
    "auto_trigger_dry_run": false,
    "max_per_run": 5,
    "max_per_day": 20
  },
  "message": "2 switch(es) updated."
}
```

Writes to `config/config.local.ini` and reloads settings immediately. This is how the web app arms/disarms the system without SSH.

---

### Pipeline

#### `GET /pipeline`
Full pipeline snapshot in one call — the dashboard view.

**Response** `200`
```json
{
  "generated_at": 1727259000.0,
  "clients": [
    {
      "id": "u10432-che",
      "name": "John Doe",
      "route": "AE-CHE",
      "combos": ["Dubai - SCHENGEN"],
      "enabled": true,
      "account": "user@example.com",
      "account_source": "client file",
      "form_email": "user@example.com",
      "email_mismatch": false
    }
  ],
  "waitlist_rows": [
    {
      "route": "AE-CHE",
      "combo": "Dubai - SCHENGEN",
      "client": "u10432-che",
      "status": "success",
      "reference": "SWDB79918334684",
      "account": "user@example.com",
      "reason": "",
      "when": 1727259000.0,
      "when_text": "2026-09-25 10:30",
      "is_latest": true,
      "orphaned": false,
      "live": true
    }
  ],
  "waitlist_routes": [
    { "route": "AE-CHE", "enabled": true, "steps": 5 }
  ],
  "booking_routes": [
    {
      "route": "AE-CHE",
      "enabled": false,
      "commit_step": "slot_pick",
      "steps": [
        { "name": "dashboard_resume", "type": "dashboard_resume", "commits": false },
        { "name": "slot_pick", "type": "slot_pick", "commits": true }
      ]
    }
  ],
  "inbox_routes": [
    { "route": "AE-CHE", "hours": 48, "matchers": ["waitlist_invitation", "waitlist_confirmation"] }
  ],
  "mailboxes": [
    { "mailbox": "user@example.com", "last_pass": 1727258000.0, "high_water": "1234" }
  ],
  "health": [],
  "warnings": [],
  "totals": {
    "clients": 1,
    "armed_clients": 1,
    "live_entries": 1,
    "mailboxes": 1,
    "booking_enabled": 0,
    "booking_routes": 1
  }
}
```

This is local file reads only — no browser, no IMAP, no VFS contact.

---

### Config

#### `GET /config`
Read-only dump of operational settings. Secrets are stripped.

**Response** `200`
```json
{
  "schedule": { "runs_per_hour": 3, "start_hour": 6, "end_hour": 23 },
  "timeouts": { "page_load_ms": 60000, "login_wait_ms": 15000, "dashboard_ms": 90000 },
  "retry": { "backoff_seconds": 30, "max_ip_tries": 3 },
  "browser": { "type": "chromium", "headless": true },
  "account_safety": { "hard_cooldown_hours": 12, "soft_cooldown_hours": 2, "fail_threshold": 3 },
  "waitlist": { "register_enabled": true, "dry_run": false, "auto_trigger_enabled": true, "auto_trigger_dry_run": true, "max_per_run": 5, "max_per_day": 10 },
  "webhook": { "enabled": true, "url": "https://yourapp.com/webhook", "timeout_seconds": 10 },
  "inbox": { "poll_seconds": 300, "first_pass_days": 30, "max_per_pass": 200 },
  "bandwidth": { "daily_cap_mb": 500, "warn_at_percent": 80 }
}
```

---

### Webhooks

#### `POST /webhooks/test`
Send a test ping to verify the outbound webhook works.

**Response** `200`
```json
{
  "delivered": true,
  "event": "test.ping",
  "attempts": 1,
  "status_code": 200,
  "error": "",
  "skipped": false
}
```

If webhooks are not configured, `skipped: true` and `delivered: false`.

#### `GET /webhooks/deadletters`
Count of failed webhook deliveries in the dead-letter log.

**Response** `200`
```json
{
  "count": 0,
  "configured": true
}
```

---

### Inbox

#### `POST /inbox/reconcile`
Poll VFS account mailboxes and settle uncertain journal rows using confirmation emails.

**Request**
```json
{
  "dry_run": true
}
```
`dry_run: true` (default) shows what would be settled. `dry_run: false` applies.

**Response** `200`
```json
{
  "mailboxes_checked": 2,
  "mailboxes_failed": [],
  "messages_seen": 15,
  "proposals": [
    {
      "registrant_id": "u10432-che",
      "route": "AE-CHE",
      "combo": "Dubai - SCHENGEN",
      "reference": "SWDB79918334684",
      "new_status": "success",
      "action": "'pending' settled by VFS's confirmation email (ref SWDB79918334684)",
      "blocked_reason": "",
      "will_apply": true
    }
  ],
  "applied": 0,
  "dry_run": true
}
```

**Important:** This opens IMAP connections to real mailboxes. Takes 5-30 seconds. Only call when needed (e.g. when dangling entries exist).

---

### Booking

#### `GET /booking/status`
Booking pipeline overview: configured routes and per-client booking phases.

**Response** `200`
```json
{
  "routes": [
    {
      "route": "AE-CHE",
      "enabled": false,
      "commit_step": "slot_pick",
      "steps": [
        { "name": "dashboard_resume", "type": "dashboard_resume", "commits": false },
        { "name": "identity_assert", "type": "identity_assert", "commits": false },
        { "name": "slot_pick", "type": "slot_pick", "commits": true },
        { "name": "confirm", "type": "confirm", "commits": false }
      ]
    }
  ],
  "clients": [
    {
      "client_id": "u10432-che",
      "route": "AE-CHE",
      "status": "success",
      "vfs_reference": "SWDB79918334684",
      "phase": "registration",
      "needs_attention": false
    }
  ]
}
```

**Booking statuses:** `waiting`, `invited`, `expired`, `booking`, `booking_pending`, `booked`, `booking_unknown`, `slot_gone`, `booking_failed`, `cancelled`

**Phases:** `registration` (on the waitlist) or `booking` (invitation received, booking in progress)

#### `POST /booking/trigger`
Spawn a booking probe as a background job. Same single-flight and idempotency as POST /trigger/waitlist.

**Request**
```json
{
  "route": "AE-CHE",
  "registrant": "u10432-che",
  "dry_run": true,
  "walk": false,
  "reason": "Check dashboard for invitation"
}
```
- `walk: false` (default) reads the dashboard only. `walk: true` clicks "Book Now" and walks the booking pages.
- `dry_run: true` (default) stops before the committing step.

**Headers:**
- `Idempotency-Key`: (optional) UUID for safe retries.

**Response** `202 Accepted`
```json
{
  "accepted": true,
  "message": "Booking probe started in the background.",
  "job": { "...JobResponse..." },
  "replayed": false
}
```

---

### Accounts

#### `GET /accounts/health`
All accounts' circuit-breaker state — which are benched, disabled, or accumulating strikes.

**Response** `200`
```json
{
  "count": 2,
  "accounts": [
    {
      "email": "user@example.com",
      "disabled": false,
      "disabled_reason": "",
      "routes": [
        {
          "route": "AE-NOR",
          "cooldown_until": 1727280000.0,
          "fails": 0,
          "last_reason": "restricted-429001",
          "benched": true
        }
      ]
    },
    {
      "email": "other@example.com",
      "disabled": true,
      "disabled_reason": "bad credentials",
      "routes": []
    }
  ]
}
```

#### `POST /accounts/health/{email}/clear`
Flag an account healthy again.

**Query params:**
| Param | Type | Description |
|-------|------|-------------|
| `route` | string | (optional) Clear only this route. Without it, clears everything. |

**Response** `200`
```json
{ "email": "user@example.com", "route": "AE-NOR", "cleared": true }
```

Returns `404` if no health record exists for the email/route.

#### `POST /accounts/health/{email}/bench`
Manually bench an account on a route.

**Request**
```json
{
  "route": "AE-CHE",
  "hours": 12,
  "reason": "Known rate limit in effect"
}
```

**Response** `200`
```json
{
  "email": "user@example.com",
  "route": "AE-CHE",
  "benched": true,
  "hours": 12,
  "until": 1727323200.0,
  "reason": "Known rate limit in effect"
}
```

---

## Error Responses

Every error returns the same envelope:

```json
{
  "error": "not_found",
  "detail": "No client with id 'xyz'.",
  "status_code": 404
}
```

Validation errors (422) include a `problems` array:

```json
{
  "error": "validation_error",
  "detail": "Request body failed validation.",
  "status_code": 422,
  "problems": [
    { "field": "body.route", "message": "route must look like 'AE-DEU'" }
  ]
}
```

Client validation errors (422) from create/update include structured problems:

```json
{
  "error": "client_invalid",
  "detail": "3 problem(s) with this client.",
  "problems": [
    { "field": "nationality", "message": "Not a valid option...", "severity": "error", "hint": "..." }
  ]
}
```

**Error classes:** `bad_request`, `unauthorized`, `not_found`, `conflict`, `validation_error`, `rate_limited`, `payload_too_large`, `internal_error`, `service_unavailable`

---

## Outbound Webhooks (push from bot to your app)

When enabled (`[webhook] enabled = true` in config), the bot pushes events to your callback URL.

### Events

| Event | When |
|-------|------|
| `waitlist.opened` | A waitlist opened for one or more combos |
| `slots.available` | A real bookable slot appeared |
| `registration.succeeded` | A client was registered successfully |
| `registration.failed` | A registration attempt failed |
| `registration.needs_attention` | A submit outcome is uncertain — needs human verification |
| `test.ping` | Sent by `POST /webhooks/test` |

### Webhook request format

```
POST <your-callback-url>
Content-Type: application/json
X-VFS-Signature: sha256=<hmac-hex>
X-VFS-Event: registration.succeeded
X-VFS-Delivery: 1-1727259000
```

Body:
```json
{
  "version": 1,
  "event": "registration.succeeded",
  "sequence": 42,
  "sent_at": "2026-09-25T10:30:00+00:00",
  "data": { "...event-specific payload..." }
}
```

### Signature verification

Your app MUST verify the signature. Algorithm: HMAC-SHA256 over the raw body bytes, hex-encoded, prefixed `sha256=`.

```python
import hmac, hashlib

def verify(body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())
```

---

## Typical Web App Flows

### 1. Client signup form
```
GET /routes                          → populate route picker
GET /routes/{route}/readiness        → get form fields + combos
POST /clients                        → create client (parked)
POST /clients/{id}/documents         → upload passport (if needed)
POST /clients/{id}/enable            → arm when ready
```

### 2. Dashboard view
```
GET /pipeline                        → full pipeline snapshot
GET /status                          → operational posture + dangling entries
GET /booking/status                  → booking pipeline state
GET /accounts/health                 → which accounts are benched/disabled
GET /config                          → operational settings
```

### 3. Arm/disarm the system
```
PATCH /status/switches               → toggle register_enabled, dry_run, auto_trigger, etc.
```

### 4. Trigger a waitlist run
```
POST /trigger/waitlist               → start job (dry_run: true first)
GET /jobs/{job_id}                   → poll for completion
GET /jobs/{job_id}/logs              → read the output
```

### 5. Trigger a booking probe
```
POST /booking/trigger                → probe dashboard (walk: false) or walk booking pages
GET /jobs/{job_id}                   → poll for completion
```

### 6. Handle dangling entries
```
GET /status/dangling                 → list entries needing resolution
POST /inbox/reconcile                → try auto-settlement from email
POST /status/resolve                 → manual resolution
```

### 7. Client history
```
GET /clients/{id}                    → current state + runnable check
GET /clients/{id}/journal            → full registration history
```

### 8. Account health management
```
GET /accounts/health                 → see which accounts are benched
POST /accounts/health/{email}/clear  → unblock a benched account
POST /accounts/health/{email}/bench  → manually bench an account
```

### 9. Webhook verification
```
POST /webhooks/test                  → verify your callback works
GET /webhooks/deadletters            → check for failed deliveries
```

---

## File Layout

```
src/api/
  __init__.py          Package init, version string
  __main__.py          Entry point: python -m src.api
  main.py              FastAPI app, middleware, exception handlers, core endpoints
  config.py            ApiSettings (pydantic-settings, VFSAPI_* env prefix)
  security.py          Token auth + rate limiting
  schemas.py           All request/response Pydantic models
  jobs.py              Background job manager (spawn, supervise, cancel)
  jobstore.py          Durable JSONL job history
  clients.py           Client CRUD, documents, routes list, route readiness
  status.py            Operational status, dangling entries, resolve, switches toggle
  pipeline.py          GET /pipeline — full pipeline snapshot
  config_view.py       GET /config — read-only settings dump
  webhooks.py          POST /webhooks/test, GET /webhooks/deadletters
  inbox.py             POST /inbox/reconcile
  booking.py           GET /booking/status, POST /booking/trigger
  accounts.py          GET /accounts/health, POST clear/bench
  console.html         Optional admin UI (served at GET /console)
```

---

## Complete Endpoint Summary (28 endpoints)

| Method | Path | Description |
|--------|------|-------------|
| GET | /health | Liveness probe (no auth) |
| GET | /console | Admin UI (no auth, off by default) |
| **Routes** | | |
| GET | /routes | List all routes with readiness summary |
| GET | /routes/{route}/readiness | Form fields, combos, problems for one route |
| **Clients** | | |
| POST | /clients | Create a client |
| GET | /clients | List clients with optional enrichment |
| GET | /clients/{id} | One client (redacted, with pre-flight) |
| PUT | /clients/{id} | Replace a client entirely |
| PATCH | /clients/{id} | Partially update a client |
| DELETE | /clients/{id} | Delete a client and documents |
| POST | /clients/{id}/enable | Arm for registration |
| POST | /clients/{id}/disable | Park without deleting |
| GET | /clients/{id}/journal | Full registration history |
| POST | /clients/{id}/documents | Upload passport scan |
| GET | /clients/{id}/documents | List held documents |
| DELETE | /clients/{id}/documents | Remove all documents |
| **Triggers** | | |
| POST | /trigger/waitlist | Spawn waitlist registration job |
| POST | /booking/trigger | Spawn booking probe job |
| **Jobs** | | |
| GET | /jobs | Recent jobs (paginated) |
| GET | /jobs/{id} | One job's status |
| GET | /jobs/{id}/logs | Tail of job log |
| POST | /jobs/{id}/cancel | Cancel a running job |
| **Status & Ops** | | |
| GET | /status | Full operational status |
| GET | /status/dangling | Entries needing human resolution |
| POST | /status/resolve | Record a human's finding |
| PATCH | /status/switches | Toggle operational switches |
| **Pipeline** | | |
| GET | /pipeline | Full pipeline snapshot |
| **Config** | | |
| GET | /config | Read-only settings dump |
| **Accounts** | | |
| GET | /accounts/health | Circuit-breaker state |
| POST | /accounts/health/{email}/clear | Unblock an account |
| POST | /accounts/health/{email}/bench | Manually bench an account |
| **Webhooks** | | |
| POST | /webhooks/test | Test the outbound webhook |
| GET | /webhooks/deadletters | Failed delivery count |
| **Inbox** | | |
| POST | /inbox/reconcile | Poll mail + settle journal rows |
| **Booking** | | |
| GET | /booking/status | Booking pipeline overview |
