# Prompt: client activity feed for the web app

Paste everything below the line to whoever builds the web app.

---

Build a **client activity feed**: for one client, show everything the VFS booking bot has done, newest first, each line with a timestamp. It must update when the bot does something, without the user refreshing.

## The API

Base URL: `https://tribute-groggy-yam.ngrok-free.dev`
Every request needs the header `X-Webhook-Secret-Token: <token>`. A missing or wrong token returns 401.

### Read the feed

```
GET /v1/clients/{client_id}/timeline?since=<seq>&limit=50&offset=0
```

| Param | Meaning |
|---|---|
| `since` | Return only entries newer than this `seq`. Omit or `0` for the whole history. |
| `limit` | 1–200, default 50. |
| `offset` | For paging through one response. |

Response:

```json
{
  "items": [
    { "id": "req:23", "seq": 23, "at": "2026-10-24T06:31:16Z",
      "source": "booking_request", "event": "attempt_failed",
      "detail": "VFS did not respond in time.", "run_id": "6adb1aa3d6b455" },
    { "id": "job:6adb1aa3d6b455", "at": "2026-10-24T06:28:19Z",
      "source": "job", "event": "failed", "job_id": "6adb1aa3d6b455",
      "run_id": "6adb1aa3d6b455", "finished_at": "2026-10-24T06:31:20Z",
      "logs_url": "/v1/jobs/6adb1aa3d6b455/logs",
      "started_by": "auto-book (scheduler)",
      "detail": "VFS did not respond in time." }
  ],
  "total": 2, "limit": 50, "offset": 0, "next_offset": null
}
```

Rules you can rely on:

- **`id` is stable and unique.** De-duplicate on it. Never render the same `id` twice.
- **`at` is always UTC**, `YYYY-MM-DDTHH:MM:SSZ`. Convert to the user's timezone for display; the UAE is UTC+4.
- **`seq` only goes up**, and only entries with `source: "booking_request"` have it. Store the highest `seq` you have seen per client and send it back as `since`. Do not use `at` as a cursor — it has second resolution, so two events in one second are indistinguishable.
- **Items are newest first.** Page with `next_offset` until it is `null`.
- `detail` is always one plain sentence, safe to show a sales agent. It never contains a stack trace or a selector.

### Get notified instead of polling

The bot POSTs to your webhook URL on **every** change to a client, within a second or two of it happening:

```json
{ "version": 1, "event": "booking.updated", "sequence": 412,
  "sent_at": "2026-10-24T06:31:16.482Z",
  "data": { "request_id": "acme-ae-nor-0001", "route": "AE-NOR",
            "combo": "Norway Visa Application Center - Dubai - Tourist",
            "status": "waiting", "enabled": true, "attempts": 3,
            "seq": 23, "at": "2026-10-24T06:31:16Z",
            "event": "attempt_failed",
            "detail": "VFS did not respond in time.",
            "run_id": "6adb1aa3d6b455" } }
```

Headers: `X-VFS-Event: booking.updated`, `X-VFS-Delivery: <id>`, and
`X-VFS-Signature: sha256=<hmac>` — an HMAC-SHA256 of the **raw request body** using the shared webhook secret. Verify it with a constant-time compare and reject anything that fails. Respond `2xx` quickly; the bot retries three times over ~15 seconds, then gives up.

**Use both.** The push is speed; the timeline is truth:

1. On a push, append `data` to that client's feed if `data.seq` is higher than the highest you hold.
2. If `data.seq` is more than one above your highest, you missed a push — call the timeline with `since=<your highest>` and backfill.
3. When a user opens a client, call the timeline with `since=<your highest>` anyway. It returns `[]` when there is nothing new, so this is cheap.
4. A separate `booking.succeeded` / `booking.needs_attention` webhook also fires for those two outcomes. They duplicate what `booking.updated` already carries — use them for alerting, not for the feed, or you will show the event twice.

## What to render

`status` is the client's current state. Show it as a badge:

| status | Meaning | Tone |
|---|---|---|
| `waiting` | Armed, waiting for VFS to offer a date in the window | neutral |
| `booking` | A run is in progress right now | in progress |
| `booked` | Booked and paid | success |
| `needs_attention` | **A person must check the VFS account.** A payment may have been taken | danger |
| `expired` | The whole date window is in the past | muted |

A client with `enabled: false` is parked: show it as paused whatever the status.

`event` is what happened in that entry:

| event | Show as |
|---|---|
| `created` | Request created |
| `enabled` / `disabled` | Armed / paused |
| `edited` | Details changed |
| `skipped` | **Matched a slot but did not book — `detail` says why** (card problem, daily cap reached, another booking running). Nothing was spent |
| `attempt_started` | Attempt started. Carries `run_id`, and `seen_date` — the date VFS was offering that triggered it |
| `attempt_failed` | Attempt failed before payment. It will be tried again |
| `booked` | Booked and paid. Carries `appointment_date`, `appointment_time`, and a reference where VFS gave one |
| `needs_attention` | Stopped, needs a person |
| `resolved` | A person settled it |
| `test_passed` / `test_failed` / `test_stopped` | A test run, which never pays |
| `expired` | The window passed |
| `confirmed_by_email` | VFS's confirmation email arrived |

Entries with `source: "job"` are the run itself — `event` is `running`, `succeeded`, `failed`, `cancelled` or `timed_out`. Treat them as a sub-line of the attempt with the same `run_id`, not as separate activity.

### The run's log

Where an entry has `logs_url`, that run printed a log:

```
GET /v1/jobs/{job_id}/logs?lines=200
→ { "job_id": "...", "lines": ["06:28:21 | INFO | === Booking COMMIT: AE-NOR ===", ...],
    "line_count": 42, "truncated": false, "log_available": true }
```

Already filtered for display — progress lines plus one sentence per problem. Offer it behind a "view log" toggle. `log_available: false` means the log was pruned; say so rather than showing an empty box.

## Things to get right

- **Never show `needs_attention` as a failure the user can retry themselves.** It means a booking may exist and a card may have been charged. The only safe action is `POST /v1/clients/{id}/resolve` after someone has checked the VFS account.
- `attempts` is the lifetime count including manual runs. Don't present it as "attempts remaining".
- A `skipped` entry is **not** a failure. Nothing was attempted and nothing was spent.
- Long gaps between entries are normal: the bot checks every 30 minutes and only acts when VFS offers a date the client agreed to.
- Timestamps come from the bot's server. Render relative time ("12 minutes ago") with the absolute UTC value in a tooltip.
