# API: master switches and the inbox watcher

For the web app. Every call needs the header `X-Webhook-Secret-Token: <token>`.
Base URL is the tunnel URL, paths are under `/v1`. Add `X-Actor: <who>` on
writes so `/v1/audit` records who did it.

## 1. Switches (turn flows on and off remotely)

```
GET   /v1/switches
PATCH /v1/switches        body: any subset of the four, e.g. {"invite_booking": true}
```

Response (both calls):

```json
{"waitlist": false, "invite_booking": true, "live_booking": true,
 "test_booking": true, "note": "..."}
```

| Switch | What it allows |
|---|---|
| `waitlist` | registering clients on VFS waitlists |
| `invite_booking` | booking (and paying) when a VFS invitation email arrives |
| `live_booking` | booking and paying a live slot inside a booking request's window |
| `test_booking` | test-mode requests may walk every page and stop before paying |

The PATCH writes `config/config.local.ini`. The API and the inbox watcher pick
it up at their next pass (within `poll_seconds`); the slot-check supervisor
picks it up at its next run (within 30 minutes).

## 2. Inbox watcher

The watcher runs inside the API process and reads the mailbox of every account that holds a waitlist registration
every `poll_seconds` (default 1200, i.e. 20 minutes). It records invitations and appointment
confirmations always, and books from invitations only while `invite_booking`
is on. If the API is down, the slot-check supervisor runs a pass at the end of
each of its runs instead.

### 2.1 One call for a panel

```
GET /v1/inbox/status
```

```json
{
  "server_time": "2026-10-20T13:40:00Z",
  "watcher": {
    "verdict": "live",            // running | live | stale | disabled | never_ran
    "live": true,
    "thread_in_this_process": true,
    "state": "idle",              // running | idle | error | disabled | stopped
    "age_seconds": 41,
    "passes": 312,
    "last_pass_started_at": "...", "last_pass_finished_at": "...",
    "last_pass_seconds": 6.2,
    "last_error": "",
    "next_pass_at": "2026-10-20T13:41:19Z",
    "poll_seconds": 1200, "pid": 3008, "trigger": "api"
  },
  "last_pass": {
    "mailboxes_checked": 13, "mailboxes_failed": [], "messages_seen": 4,
    "matched": 1, "invitations": 1, "waitlist_confirmations": 0,
    "appointment_confirmations": 0, "expiries": 0,
    "acting": true, "bookings_started": ["3f9c…"]
  },
  "switches": {"invite_booking": true, "test_booking": true,
               "background_watch": true, "poll_seconds": 1200},
  "mailboxes": {"count": 1, "watched": ["mu***@travnook.com"], "error": ""},
  "invitations": {"pending": 0, "booking": 1, "booked": 2, "needs_attention": 0,
                  "manual": 1, "expired": 3, "dismissed": 0},
  "confirmations": {"matched": 2, "unmatched": 0, "ambiguous": 0, "latest": [ ... ]}
}
```

How to read `verdict`:

- `running`: a pass is in progress right now.
- `live`: the last heartbeat is recent. The watcher is doing its job.
- `stale`: no heartbeat for more than two poll intervals. The API thread is
  down; the supervisor is the fallback. Check `GET /v1/health/ready`.
- `disabled`: `[inbox] background_watch = false` on the bot machine.
- `never_ran`: no pass has ever completed on this machine.

### 2.2 Regular updates without polling

```
GET /v1/inbox/stream?interval=10        (3..120 seconds, max 1 hour per connection)
```

Server-Sent Events. One `status` event immediately, then one every `interval`
seconds, each carrying the same JSON as `/v1/inbox/status`. Reconnect when you
receive `event: end`.

```js
const es = new EventSource(url, { /* add the token via a proxy or query shim */ });
es.addEventListener("status", e => render(JSON.parse(e.data)));
```

Browsers cannot set headers on `EventSource`; proxy it through your backend
or poll `/v1/inbox/status` every 10 seconds.

### 2.3 Run a pass now

```
POST /v1/inbox/pass
```

Reads every mailbox once and acts exactly as the background watcher would.
Returns the pass summary (200), or 409 when a pass is already running.

### 2.4 Appointment confirmations from email

```
GET /v1/inbox/confirmations?status=matched|unmatched|ambiguous&route=AE-NOR&limit=50
```

Each item is one VFS "Appointment is Confirmed" email and what it settled:

```json
{"key": "a1b2…", "route": "AE-NOR", "account": "mu***@travnook.com",
 "applicant_name": "AHMED KHAN", "reference": "NOR126162488430",
 "status": "matched",
 "settled": [{"kind": "invitation", "key": "3f9c…", "client_id": "ahmed-nor"},
             {"kind": "payment", "run_id": "6ad09e180783e1"}],
 "notes": [], "received_epoch": 1760950000, "created_at": "..."}
```

`matched` means the invitation or booking request is now `booked` with the
VFS reference and any unanswered payment for it is answered. `unmatched` and
`ambiguous` mean a human should look: nothing was changed.

## 3. Where the results show up

| What | Endpoint |
|---|---|
| Invitations and their outcome | `GET /v1/booking/invitations` |
| Booking requests (live flow) | `GET /v1/clients?flow=live`, `GET /v1/clients/{id}/timeline` |
| Payments still unanswered | `GET /v1/booking/payments/unanswered` |
| Waitlist attempts, including failed logins | `GET /v1/clients/{id}/timeline`, `GET /v1/waitlist/status` |
