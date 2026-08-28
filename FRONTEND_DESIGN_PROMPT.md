# Frontend design prompt — VFS waitlist admin

Six parts. Paste **Part 0 first**, then work through the rest one at a time —
each builds on the previous answer. Do not paste all six at once; the point is
to settle the layout before anything is built.

Everything factual below was verified against the running API on 2026-08-21.

---

## PART 0 — Context (paste this first)

I am building an admin frontend for a bot that puts visa applicants on VFS
Global waitlists. I want you to design the layout and information architecture
before any code is written.

### What the system does

A client wants a visa appointment (say, UAE → Switzerland, route code
`AE-CHE`). Appointments are usually unavailable, but VFS sometimes opens a
**waitlist**. Two bots:

- **Slot bot** — scheduled, watches for openings, sends Telegram alerts. Never
  registers anyone.
- **Waitlist bot** — logs into a real VFS account, fills a 4-step form, submits.
  **Irreversible from the bot's side**: undoing it means logging into the VFS
  portal by hand and cancelling.

A local FastAPI service wraps the bot. My web app talks to that API.

### The three things that make this UI unusual

1. **Actions are irreversible and consume a scarce resource.** A registration
   takes a real waitlist place on a real government-contractor portal. A
   mis-click cannot be undone in-app. The UI must make the dangerous action
   visibly different from the safe one, every time.

2. **Nothing is instant.** A run drives a real browser through Cloudflare and a
   4-step form — 60 to 180 seconds. The API returns `202` with a `job_id`
   immediately; the UI polls. There is no synchronous "did it work?".

3. **Some outcomes need a human, and must never be retried automatically.** If
   a submit is in flight when the process dies, the result is `pending` or
   `unknown` — it may or may not have registered. Auto-retrying could
   double-register someone. These must be surfaced as a blocking task, not a
   toast that scrolls away.

### Who uses it

**Me (admin)** — I manage every client, every route, all the safety switches,
and trigger runs. Full control.

That is the primary audience. Assume a knowledgeable operator, not a novice —
but one who will be tired at 3am and must not be able to fire a live
registration by muscle memory.

### For this part

Don't design anything yet. Read the above and tell me:
- What you think the riskiest interaction in this UI is, and why
- What information an operator needs to see *before* they can safely act
- Any question you need answered before designing the layout

---

## PART 1 — The data you have to work with

Every endpoint below is real and verified. Auth is a single shared secret in
the `X-Webhook-Secret-Token` header on every call except `/health`.

### Read

| Endpoint | Returns |
|---|---|
| `GET /health` | `{status, version, server_time}` — no auth |
| `GET /status` | The whole system state in one call (below) |
| `GET /status/dangling` | Registrations needing a human |
| `GET /clients` | `{count, clients:[{client_id, route, combos[], enabled}]}` |
| `GET /clients/{id}` | `{client_id, client{}, runnable, problems[]}` |
| `GET /routes/{route}/readiness` | `{route, ready, combos[], problems[]}` |
| `GET /jobs` | `{count, active_job_id, jobs[]}` |
| `GET /jobs/{id}` | One job + per-client `results[]` |
| `GET /jobs/{id}/logs` | Raw log text |

`GET /status` payload:

```json
{
  "posture": "MANUAL — registration is enabled but the auto-trigger is off…",
  "switches": {
    "register_enabled": true, "dry_run": true,
    "auto_trigger_enabled": false, "auto_trigger_dry_run": true,
    "max_per_run": 3, "max_per_day": 20
  },
  "routes": [{"route":"AE-CHE","ready":true,
              "combos":["Abu Dhabi - SCHENGEN","Dubai - SCHENGEN"],
              "clients":3,"problems":[]}],
  "clients_total": 2,
  "dangling": [],
  "needs_attention": false,
  "webhook_configured": false,
  "undelivered_webhooks": 0,
  "degraded": []
}
```

A job record:

```json
{"job_id":"418da69e","status":"succeeded","outcome":"completed",
 "exit_code":0,"needs_attention":false,"pid":1234,
 "started_at":"…","finished_at":"…","log_file":"…","payload":{"route":"AE-CHE"},
 "results":[{"registrant_id":"test-che","combo":"Dubai - SCHENGEN",
             "status":"dry_run","reason":"…","vfs_reference":null,
             "screenshots":["…png"]}]}
```

### Write

| Endpoint | Notes |
|---|---|
| `POST /clients` | Creates **parked**. Returns `422` + `problems[]` on bad data. |
| `PUT /clients/{id}` | `409` if a registration is in flight |
| `DELETE /clients/{id}` | Does **not** cancel an existing VFS entry |
| `POST /clients/{id}/enable` | Refuses (`422`) if the client would not run |
| `POST /clients/{id}/disable` | |
| `POST /trigger/waitlist` | `202` + `job_id`. `409` if a job is running. Accepts `Idempotency-Key`. |
| `POST /jobs/{id}/cancel` | |
| `POST /status/resolve` | Record what a human found for a dangling entry |

Also available when enabled: `GET /docs` (Swagger UI), `GET /redoc`, and
`GET /openapi.json` — the full machine-readable schema, useful for generating
a typed client.

Trigger body — **`dry_run` defaults to `true`**; live requires sending
`false` explicitly:

```json
{"route":"AE-CHE","registrant":"test-che","dry_run":true,"reason":"…"}
```

### Constraints that shape the UI

- **Rate limit: 20 requests/min per IP.** A naive 5s poll of 3 endpoints =
  36/min and rate-limits itself. Budget your polling.
- **Single-flight**: only one job at a time; a second trigger gets `409`.
- **Secrets never come back.** `account_password` is never returned; passport
  and email come back masked (`A1****67`). You get
  `has_account_password: true` and nothing more.
- **Webhooks** (optional, bot → your app): `waitlist.opened`,
  `registration.succeeded`, `registration.failed`,
  `registration.needs_attention`, `slots.available`. HMAC-signed. These let
  you avoid polling entirely for state changes.

### For this part

Tell me which screens/views this data implies, and what belongs on each.
Not visual design yet — just the information architecture. Flag anything you
need that the API does not currently expose.

---

## PART 2 — The domain rules the UI must encode

These are not suggestions. Getting them wrong in the UI causes real harm.

### The four safety switches

Server-side config (`config/config.local.ini`), **not** editable via the API
today:

| Switch | Meaning |
|---|---|
| `register_enabled` | Master kill switch. False ⇒ nothing can ever be submitted. |
| `dry_run` | Rehearse: walk the whole flow, stop before the committing click. |
| `auto_trigger_enabled` | Let the slot bot start waitlist runs by itself. |
| `auto_trigger_dry_run` | Same idea as `dry_run`, for auto-started runs. |

**The non-obvious part — `dry_run` and `auto_trigger_dry_run` do NOT stack.**
Exactly one applies, depending on who started the run:

| Run started by | Governed by |
|---|---|
| A human (CLI or API) | `dry_run`, or a per-request override |
| The auto-trigger | `auto_trigger_dry_run` |

So `dry_run = true` does **not** protect you from an auto-triggered run. And a
per-run flag overrides the config default entirely — a `--live` run submits for
real even while config says `dry_run = true`. **This has already caused one
unintended real registration on my account.**

The UI must therefore never display "dry run mode" as a blanket reassurance.
It must say what *this specific action* will do.

### Posture

`GET /status` collapses the switches into one word: `PARKED`, `MANUAL`,
`AUTO (DRY RUN)`, `AUTO (LIVE)` — ascending risk. This is the single most
important thing on the screen.

### One entry per client per route

A client's `combos` list is a **preference order**, not a shopping list:
`["Dubai - SCHENGEN", "Abu Dhabi - SCHENGEN"]` means *"whichever opens first"*.
One person gets **one** waitlist entry per route. Two entries would hold two
places for one need and risk VFS voiding both.

The UI must present combos as ranked preference, never as multi-select
"register me for all of these".

### Created ≠ armed

`POST /clients` always creates **parked** (`enabled: false`). Arming is a
separate deliberate call. Do not design a create-form that silently arms.

### Outcome statuses

| Status | Reached VFS? |
|---|---|
| `skipped` | No — a gate declined |
| `dry_run` | No — rehearsed and stopped |
| `failed` | No — failed before the commit |
| `success` | **Yes** — has a `vfs_reference` |
| `pending` | **Maybe** — submit was in flight |
| `unknown` | **Maybe** — submitted, result unread |

`pending` and `unknown` ⇒ `needs_attention: true`. **Never offer a retry
button on these.** The only correct action is: check the VFS portal manually,
then record what was found. Treat them as blocking work items.

Also: `slots_available` as a job status means a *real bookable appointment*
appeared, so the run stopped without waitlisting. That is a **better** outcome
than success and should read as good news with a different call to action
("tell the client to book now").

### Caps

`max_per_run` (3) resets each invocation, so it does not limit daily
throughput. `max_per_day` (20) is the real ceiling.

### For this part

Show me how each rule surfaces in the UI. Specifically:
- How does the operator tell a dry run from a live one *at the moment of
  clicking*?
- How do `needs_attention` items demand attention without being dismissable
  noise?
- How are `combos` presented so "preference order" is self-evident?

---

## PART 3 — Layout

Now design it. Give me:

1. **Navigation** — what are the top-level destinations, and why that split?
2. **Each screen** — ASCII wireframe or clear structural description. What is
   above the fold, what is progressive disclosure.
3. **The dashboard** — what does the operator see in the first two seconds?
4. **State handling** — loading, empty, error, and the "job running" state
   that lasts three minutes.

Constraints:
- Desktop-first; I use this on a laptop. Should not break on a phone.
- Dark and light both.
- No heavy chrome. Density over decoration — this is an operations tool.
- The posture indicator must be visible on every screen.

Do not write code yet. I want to agree the layout first.

---

## PART 4 — The dangerous paths

Design these four flows in detail. These are where harm happens.

1. **Triggering a live run.** From intent to confirmation to watching it. What
   friction exists, and where? How does the operator know, at the moment of
   the final click, that this one is real?

2. **A job running for three minutes.** What does the operator see? Can they
   leave and come back? What if they close the tab? How is a `needs_attention`
   result surfaced the instant it appears?

3. **Resolving a dangling entry.** The operator must check the VFS portal
   manually, then record `success` or `failed`. Design the flow so recording
   the wrong answer is hard. The API endpoint is `POST /status/resolve`.

4. **Creating and arming a client.** Two deliberate steps. `POST /clients`
   returns `422` with a structured `problems[]` array naming each faulty field
   — design the error presentation around that, not a generic banner.

For each: what could a tired operator do wrong, and what in the design stops
them?

---

## PART 5 — What to build, in order

Give me a build order with reasoning:

- What is the minimum that is genuinely useful on day one?
- What can wait?
- Where should I use webhooks instead of polling, given the 20 req/min limit?
- What does the API need to expose that it does not today?
- What would you *not* build, and why?

Be opinionated. I would rather have a small tool that is correct than a large
one that lets me make expensive mistakes.

---

## Ground rules for whoever answers this

- **Correctness over polish.** This tool spends real waitlist places.
- **Do not design a switch panel that flips the safety switches remotely**
  unless you argue for it explicitly. A remote button that turns on live
  registration is the most dangerous control in the system.
- Say plainly when I am asking for something that would make the tool less
  safe.
- If you need to verify how something behaves, the API is at
  `http://127.0.0.1:8000` and the repo is the source of truth. Do not guess.
