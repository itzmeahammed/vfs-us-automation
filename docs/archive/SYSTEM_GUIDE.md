# How It All Works — A Guided Tour

**Read this to understand the system.** It follows one client, Priya, from the
moment she signs up on your web app to the moment she is on a VFS waitlist —
naming every file, function and decision along the way.

- [Part 1: The two bots](#part-1-the-two-bots)
- [Part 2: Priya signs up](#part-2-priya-signs-up)
- [Part 3: The waitlist opens](#part-3-the-waitlist-opens)
- [Part 4: The registration run](#part-4-the-registration-run)
- [Part 5: Telling your app](#part-5-telling-your-app)
- [Part 6: The safety system](#part-6-the-safety-system)
- [Part 7: Design decisions and why](#part-7-design-decisions-and-why)
- [Part 8: When things go wrong](#part-8-when-things-go-wrong)
- [Part 9: Turning it on](#part-9-turning-it-on)
- [Part 10: File map](#part-10-file-map)

---

## Part 1: The two bots

There are **two separate programs** here, and understanding why they are
separate explains most of the architecture.

### The slot bot (always running)

Runs twice an hour from Windows Task Scheduler ([run_task.ps1](run_task.ps1)).
For each route it opens Chrome, logs in, and reads the appointment page:

```
supervisor.main()                    src/supervisor.py
  └─ run_all_routes()                every route in config/vfs_urls.ini
       └─ run(source, dest)          one route, fresh Chrome
            └─ run_slot_check()      src/vfs_bot/slot_check.py
                 └─ for each combo: read the slot banner
```

Three outcomes per combination:

| What it sees | Meaning |
|---|---|
| A date banner | **Slots available** — book it |
| Nothing at all | No availability |
| A waitlist checkbox | **Waitlist open** — you can queue |

That third case is the interesting one. [slot_check.py:462](src/vfs_bot/slot_check.py#L462):

```python
message = (waitlist.as_result() if waitlist.is_offered(page)
           else "No slot message shown (no availability?).")
```

Note `is_offered()` is **read-only**. It looks for the checkbox; it never ticks
it. The slot bot's job is to observe.

### The waitlist bot (on demand)

A separate program that logs in and actually *fills in the form*:

```
runner.run_registration()            src/waitlist/runner.py:346
  ├─ validate config + roster        (before any browser starts)
  ├─ resolve the account             one run = one login
  ├─ launch Chrome
  └─ for person, combo in plan:      the one-by-one queue
       ├─ select the combo
       ├─ is a real slot there?      → SlotsAvailable, stop everything
       ├─ is the waitlist offered?   → if not, skip
       ├─ account at capacity?       → if so, skip
       └─ register.register()        ← fills the form, ticks, submits
```

### Why they cannot share a browser

This is the single most important architectural fact.

The slot bot is logged in on a **rotating slot-check account**. Waitlist
accounts are a deliberately **separate pool** —
[accounts.py:14-18](src/waitlist/accounts.py#L14-L18) reads nothing from
`credentials.local.ini`.

A VFS waitlist entry **belongs to the account that created it**. If the slot bot
registered Priya using its own rotating account, her entry would sit in an
account she can never see or cancel. It would be worse than useless.

So: **a waitlist run is always a fresh login.** Not an optimisation — a
correctness requirement. Everything downstream follows from it.

The codebase already encoded this before I touched it. [src/vfs_bot/waitlist.py](src/vfs_bot/waitlist.py)
is a shim that re-exports the read-only detection helpers and *deliberately
does not export* `register`, so the always-on path is never one attribute
access away from a mutating call.

---

## Part 2: Priya signs up

Priya wants a Swiss visa appointment in Dubai. She fills in your web app's form.

### Step 1 — Your app asks what's available

```http
GET /routes/AE-CHE/readiness
X-Webhook-Secret-Token: <secret>
```
```json
{
  "route": "AE-CHE",
  "ready": true,
  "combos": ["Abu Dhabi - SCHENGEN", "Dubai - SCHENGEN"],
  "problems": []
}
```

Your form populates its dropdown from `combos`. This matters: those strings must
match the route config **exactly**, so letting the user free-type would
guarantee failures.

Behind it, [validate.route_readiness()](src/waitlist/validate.py) checks five
things that must **all** hold:

1. A login URL in `config/vfs_urls.ini`
2. `config/routes/AE-CHE.json` exists with combinations
3. `config/waitlist/AE-CHE.json` exists and parses
4. That config has `"enabled": true`
5. It declares exactly one committing step

Only **2 of 10** live routes pass today. AE-DEU fails on #4 — it is disabled
pending a known bug where both "Dubai" rows carry the **Abu Dhabi** centre
string, which would waitlist a Dubai client in the wrong city.

Try a route that isn't ready and you get told exactly why:

```json
{
  "route": "AE-DEU", "ready": false,
  "problems": [{"field": "route",
    "message": "config/waitlist/AE-DEU.json has \"enabled\": false — registration is switched off for this route.",
    "hint": "Set it to true once the route's page mapping is trusted."}]
}
```

### Step 2 — Your app creates the client

```http
POST /clients
X-Webhook-Secret-Token: <secret>
```
```json
{
  "client_id": "priya-che",
  "route": "AE-CHE",
  "combos": ["Dubai - SCHENGEN"],
  "account": "priya.waitlist@example.com",
  "account_password": "her-vfs-password",
  "first_name": "PRIYA", "last_name": "SHARMA",
  "nationality": "India",
  "passport_number": "M4455667",
  "date_of_birth": "1993-07-22",
  "phone_country_code": "971", "phone_number": "509998888",
  "email": "priya@example.com",
  "address_line_1": "VILLA 12", "address_line_2": "JUMEIRAH, DUBAI"
}
```

Four validation layers run before anything is written
([clients.py](src/api/clients.py) → [validate.py](src/waitlist/validate.py)):

| Layer | Catches |
|---|---|
| `validate_payload()` | Missing route, bad combos list, account without password, a field that looks like a CSS selector |
| `route_readiness()` | The route can't register |
| `check_combos()` | `"Dubai - SCHENGEN"` isn't a real combination |
| `check_templates()` | The form needs `date_of_birth` and this client has none |

**All problems come back at once** — a web form should not play whack-a-mole:

```json
{
  "error": "client_invalid",
  "problems": [
    {"field": "account_password", "message": "\"account\" is set but \"account_password\" is missing."},
    {"field": "combos", "message": "\"Dubai - Tourism\" is not a combination of config/routes/AE-CHE.json.",
     "hint": "Available: Abu Dhabi - SCHENGEN; Dubai - SCHENGEN"}
  ]
}
```

That structure is why `field` exists: your app attaches each message to the
right input.

### Step 3 — What gets written

[store.create()](src/waitlist/store.py) writes
`config/registrants/priya-che.json` **atomically** — temp file, `fsync`,
`os.replace`. A crash mid-write leaves the old file intact, never a half-written
one. These hold passport numbers; a truncated one is not recoverable.

Note the response:

```json
{
  "created": true,
  "enabled": false,
  "message": "Client created. It is PARKED (enabled=false) — call POST /clients/priya-che/enable to arm it.",
  "client": {
    "passport_number": "M4****67",
    "has_account_password": true
  }
}
```

Two deliberate things:

**Created ≠ armed.** `enabled: false` by default. A bug in your web app must not
be able to arm a fleet of clients for live registration.

**Secrets go in, never out.** The password is stored (the run needs it to log
in) but no endpoint ever returns it. The passport is masked to `M4****67` —
recognisable, not usable. [`_public_view()`](src/api/clients.py) is the single
choke point; if a field ever leaks it leaks there and nowhere else.

### Step 4 — Arming her

```http
POST /clients/priya-che/enable
```

This **re-runs the full pre-flight** and refuses if anything is wrong. Arming
something broken just moves the failure to 3am.

---

## Part 3: The waitlist opens

Two days later, at 14:29, the scheduled slot check runs.

### The moment of detection

[slot_check.py:462](src/vfs_bot/slot_check.py#L462) finds the waitlist checkbox
for `Dubai - SCHENGEN`. The result list gets:

```python
("Dubai - SCHENGEN", "WAITLIST — no slots; waitlist sign-up available")
```

### Why the trigger does NOT fire here

It would be so easy to call `register()` right here. It would also be wrong,
for three independent reasons — any one fatal:

| # | Problem |
|---|---|
| 1 | **Wrong account.** We're logged in as the rotating slot-check account. Priya's entry would belong to it. |
| 2 | **Wrong egress.** A waitlist run resolves its own proxy and a Chrome profile keyed to *her* account. |
| 3 | **Reentrancy.** Launching a second Chrome from inside a live page context, colliding on port 9222. |

### Where it does fire

The browser closes, the route finishes, and *then*
[supervisor.py](src/supervisor.py) fires:

```python
outcomes.append(outcome)

# ---- Auto-trigger hook (Phase 3) ----
if outcome.get("waitlist_combos"):
    try:
        from src.waitlist import autotrigger
        autotrigger.handle_waitlist_opened(f"{source}-{dest}",
                                           outcome["waitlist_combos"])
    except Exception as e:
        logging.exception(f"Auto-trigger failed (non-fatal): {e}")
```

Clean boundary. Browser closed. And wrapped, so a fault in the new code can
never break the slot check that has run reliably for months.

### The label trap — the subtlest bug in this system

`_outcome()` carries labels from `slot_check.result_label()`. Clients name the
route file's `"label"` field. **These are not the same string.**

Measured against the real config:

| Route | Client writes | Supervisor reports |
|---|---|---|
| AE-CHE | `Dubai - SCHENGEN` | `Dubai - SCHENGEN` ✅ |
| AE-NLD | `Dubai - Tourist Visa` | `Netherlands Visa application center- Dubai - Tourist Visa - Tourist Purpose` ❌ |

`result_label()` **deliberately ignores** the `"label"` field
([slot_check.py:334](src/vfs_bot/slot_check.py#L334)) because reports want the
visa type, and the label is often just the centre name.

A naive `if client_combo == reported_label` works for AE-CHE and **matches
nothing for every AE-NLD client**. And here is why that is dangerous rather than
merely broken: finding no match looks *identical* to "nobody is waiting". No
error. No alert. The route reports healthy. Priya's Dutch equivalent silently
never gets registered.

[`resolve_combo_label()`](src/waitlist/autotrigger.py) fixes it by walking the
route's combination dicts, rebuilding the result-style label for each, and
returning the client-facing label of whichever matches. Verified round-trip:

```
AE-NLD:  "Netherlands Visa application center- Dubai - Tourist Visa - Tourist Purpose"
      →  "Dubai - Tourist Visa"          →  finds client 'ahmed-nld' ✓
```

### Deciding what to run

[`plan_for()`](src/waitlist/autotrigger.py) makes a decision without launching
anything:

```
1. resolve_combo_label()   "Dubai - SCHENGEN"        ← the trap, handled
2. find_waiting_clients()  enabled? combo matches?
                           no blocking journal entry?
       └─ none → STOP. No browser. (the common case)
3. group_by_account()      one run = one login
```

**Step 3 exists because of a hard constraint.**
[runner.py:399](src/waitlist/runner.py#L399):

> *"Every client in this plan must resolve to the SAME account, because one run
> is one login."*

Five clients across three accounts = **three separate runs, three logins**. This
is your real throughput ceiling when a window opens, and why account allocation
matters more than it looks.

### Multi-tenant: two of your users, same combination

travnooker.com is multi-tenant, so two of your users may want
`AE-CHE / Dubai - SCHENGEN` at the same time. That works — **because each client
brings their own VFS account**:

```
tenant-a  →  a@example.com   ─┐
                              ├─ 2 account groups → 2 runs, 2 logins, sequential
tenant-b  →  b@example.com   ─┘
```

Two independent mechanisms keep them apart:

- **Grouping.** `group_by_account()` splits them, so they never end up in one
  mixed run (which `runner.py` rejects as a config error).
- **The journal key** is `(route, combo, registrant_id)` — the client id is part
  of it, so tenant-a being registered never blocks tenant-b.

If two tenants ever *shared* one VFS account, they would correctly become a
single run (one login), and `capacity_verdict()` would warn that the account
already holds an entry for that combination. Leave
`one_client_per_account_combo` at its warn-only default unless you start
sharing accounts.

### Debouncing

A waitlist stays open for hours; we check twice an hour. Without a cooldown
we'd launch a browser every 30 minutes. The journal prevents double
*registration*, but not the wasted run. So `(route, combo)` gets a cooldown
window, reusing the same pattern as the existing Telegram notice.

---

## Part 4: The registration run

The plan says: run `priya-che` on `Dubai - SCHENGEN`.

### Gates before the browser opens

```
auto_trigger_enabled ──false──▶ nothing happens (SHIPPED DEFAULT)
        │true
debounce ────────────on cooldown──▶ skip
        │
clients waiting? ────none──▶ skip (no browser!)
        │
account resolvable? ─no──▶ skip
        │
        ▼
   acquire the global run lock          ← Phase 0
        │
   run_registration()
        │
register_enabled ────false──▶ every client SKIPPED (SHIPPED DEFAULT)
        │
max_per_run / max_per_day ──exceeded──▶ skip
```

### The run lock

Three programs can now want a browser: the scheduled slot check, an
auto-triggered waitlist run, and you running one by hand.
[journal.py](src/waitlist/journal.py) states plainly that its append-only
read-check-write is only safe with **one writer**.

[runlock.py](src/utils/runlock.py) enforces that. The clever part is that it
reuses the **existing** lock names:

```
Windows   Global\VfsSlotChecker        ← the same mutex run_task.ps1 already takes
POSIX     /tmp/vfs-slot-checker.lock   ← the same file run_ec2.sh already flocks
```

Zero changes to either shell script, and they now exclude Python correctly.
Verified live — a held lock made the real supervisor skip and exit 0:

```
runlock.py:292  Another browser-driving run is already in progress...
supervisor.py   skipping this tick.
exit code: 0    ← no browser, no account struck
```

Two different behaviours, deliberately:

| Caller | On busy | Why |
|---|---|---|
| `supervisor.main()` | `skip`, exit 0 | An overlapping tick is normal; the next one is 30 min away |
| `run_registration()` | `raise` | A registration is deliberate — the caller must be *told* it didn't happen |

### Inside the run

Then the one-by-one loop at
[runner.py:479](src/waitlist/runner.py#L479) — which **already existed**. You
were never missing a queue; you were missing the trigger into it.

Per client, per combo:

```python
parts = _combo_parts(combo_label, route)     # label → dropdown values
_select_combo(page, parts, combo_label)

banner = slot_check.read_slot_message(page)
if banner:
    raise SlotsAvailable(combo_label, banner)   # ← stops EVERYTHING

if not detect.is_offered(page, checkbox):
    → SKIPPED "no waitlist offered"

allowed, why = accounts.capacity_verdict(...)
if not allowed:
    → SKIPPED

result = register_mod.register(...)          # ← the point of no return
notify.notify_registered(result)
```

### Two behaviours that will surprise you

**A real slot aborts the whole run.** If a bookable slot appears,
`SlotsAvailable` is raised and everything stops — *including clients queued
behind Priya*. That is correct (book it, don't queue for it) but it means one
lucky combination suspends the others. Your app should treat this as a **better**
outcome and re-trigger the rest.

**An ambiguous submit also stops everything.** `WaitlistCommittedError` →
journalled as `unknown`, run halted. With one submit outstanding, the safe move
is to touch nothing else on that account.

### The journal — why Priya can't be registered twice

Every attempt is written to `state/waitlist_journal.jsonl` **before** the
committing click (write-ahead), keyed on `(route, combo, registrant_id)`:

```json
{"route":"AE-CHE","combo":"Dubai - SCHENGEN","registrant_id":"priya-che","status":"pending",...}
{"route":"AE-CHE","combo":"Dubai - SCHENGEN","registrant_id":"priya-che","status":"success","vfs_reference":"WL-77231",...}
```

Append-only; the latest row per triple wins. `blocking_entry()` treats
`pending`, `success` and `unknown` as blocking.

**Why `pending` blocks:** if the process is killed mid-submit, we don't know
whether VFS received it. Retrying could produce two appointments for one person.
So it stays blocked until a human checks — see
[Part 8](#part-8-when-things-go-wrong).

---

## Part 5: Telling your app

Registration succeeded. Two channels fire, deliberately not identical.

### Telegram (out-of-band)

Your existing alerting. Skips `SKIPPED` outcomes — its value is that it only
pings when something needs attention.

### The webhook (your app)

[webhook.py](src/utils/webhook.py) POSTs:

```http
POST https://your-app.example.com/webhooks/vfs
X-VFS-Signature: sha256=8cec2a8d...
X-VFS-Event: registration.succeeded
X-VFS-Delivery: 12-1787150470
```
```json
{
  "version": 1,
  "event": "registration.succeeded",
  "sequence": 12,
  "sent_at": "2026-08-19T14:37:02Z",
  "data": {
    "route": "AE-CHE", "combo": "Dubai - SCHENGEN",
    "registrant_id": "priya-che",
    "status": "success", "vfs_reference": "WL-77231"
  }
}
```

Unlike Telegram, **`skipped` outcomes are delivered too** — Priya is waiting on
an answer, and "we checked, here's why nothing happened" is a real answer.

### The signature — do not skip this

Without verification, anyone who learns your callback URL can POST a fake
"registration confirmed". Your app must verify HMAC-SHA256 over the **raw body**:

```javascript
// NOTE: express.raw, NOT express.json — a re-serialised body will not verify.
app.post("/webhooks/vfs", express.raw({type: "application/json"}), (req, res) => {
  const expected = "sha256=" + crypto
    .createHmac("sha256", process.env.VFS_WEBHOOK_SECRET)
    .update(req.body).digest("hex");
  const a = Buffer.from(expected), b = Buffer.from(req.get("X-VFS-Signature") || "");
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return res.status(401).end();

  res.status(200).end();          // ACK fast; do the work on a queue
  void handleVfsEvent(JSON.parse(req.body.toString("utf8")));
});
```

Verified end to end against a real HTTP server — a mock would happily agree with
a broken signature.

### Events

| Event | What your app should do |
|---|---|
| `waitlist.opened` | Tell waiting clients a window opened |
| `registration.succeeded` | Confirm; store `vfs_reference` |
| `registration.failed` | Show the reason; safe to retry later |
| `registration.needs_attention` | **Escalate to a human. Never auto-retry.** |
| `slots.available` | Tell the client to **book**, not wait |

### PII never leaves

Payloads pass through `redaction.scrub()`. Verified live:

```
reason as delivered : registered with passport [redacted] for [redacted]
passport leaked?    : False
vfs_reference       : WL-77231     ← useful data survives
```

If scrubbing ever produced invalid JSON, a *minimal* event is sent instead.
Failing to deliver beats leaking a passport number.

### Delivery guarantees

Retries ~1s/4s/10s on 5xx and timeouts. **4xx is not retried** (except 408/429)
— a request your app refused won't succeed on attempt three. Exhausted
deliveries append to `state/webhook_deadletter.jsonl` rather than vanishing.

**A failed webhook never fails a registration.** If your app is down, Priya is
still registered.

---

## Part 6: The safety system

Six independent gates stand between "a waitlist opened" and "a client is
registered". This is intentional layering: any single one being wrong stops the
system safely.

| # | Gate | Default | Stops |
|---|---|---|---|
| 1 | `auto_trigger_enabled` | **false** | Anything firing automatically |
| 2 | `auto_trigger_dry_run` | **true** | Auto-runs from committing |
| 3 | `register_enabled` | **false** | Any registration at all |
| 4 | `dry_run` | **true** | Any submit |
| 5 | `max_per_run` / `max_per_day` | 1 / 5 | Runaway volume |
| 6 | Client `enabled` | **false** on create | That client participating |

Plus structural protections that aren't switches:

- **The run lock** — no two browser runs at once
- **The journal** — no double registration
- **`single_flight`** — no two API-triggered jobs at once
- **Account capacity** — no over-loading one VFS account

Check where you stand:

```http
GET /status
```
```json
{
  "posture": "PARKED — [waitlist] register_enabled is false, so nothing can register. Detection and notification still work.",
  "switches": {"register_enabled": false, "auto_trigger_enabled": false, ...},
  "needs_attention": false,
  "undelivered_webhooks": 0
}
```

`posture` exists so you don't have to reason about four booleans. It reports
one of: **PARKED**, **MANUAL**, **AUTO (DRY RUN)**, or **AUTO (LIVE)** — the
last saying explicitly "will REGISTER real clients without a human in the loop."

---

## Part 7: Design decisions and why

### Why `asyncio.create_subprocess_exec`, not `subprocess.run`

You asked for `subprocess`. Plain `subprocess.run` blocks the event loop — the
API would freeze for the 10 minutes a Playwright run takes.

But the bigger reason is security. `create_subprocess_exec` takes an **argv
list** straight to the OS:

```python
await asyncio.create_subprocess_exec(*command, ...)   # command is a LIST
```

There is no shell, so there is no string for `;` or `&&` to hide in. Command
injection is **structurally impossible**, not filtered against. A client name of
`x; rm -rf /` arrives as one harmless argument. (It's also rejected at
validation — defence in depth.)

### Why output goes to a file, not a pipe

An unread pipe fills its OS buffer and **deadlocks** the child. The Playwright
bot is very chatty. A file has no such limit.

### Why the API binds 127.0.0.1 only

The tunnel is the public edge. Binding `0.0.0.0` would additionally expose it to
your whole network for no benefit. The setting *validates* this — a
non-loopback host raises at startup rather than quietly exposing you.

### Why `hmac.compare_digest` for the token

`==` short-circuits on the first differing byte, leaking how many leading
characters were right. Constant-time comparison costs nothing.

Relatedly, all three auth failures (missing / malformed / wrong) return an
**identical** 401. Saying "token present but incorrect" confirms an attacker
found the right header name — free reconnaissance.

### Why the docs endpoints are disabled

`/docs` and `/openapi.json` publish your complete API surface. Off by default;
`VFSAPI_ENABLE_DOCS=1` for local work.

### Why validation collects instead of raising

`registrant._validate()` raises on the first problem — right for a CLI, wrong
for a web form. `validate_payload()` re-expresses the same rules as pure
functions returning `Problem` records. The rules are equivalent; if they ever
disagree, `python -m src.waitlist check` is the source of truth.

### Why the marker-delimited JSON block

The run's own logging shares stdout, so the log isn't parseable as JSON. Hence:

```
---VFS-RESULT-JSON-BEGIN---
{"outcome": "completed", "results": [...]}
---VFS-RESULT-JSON-END---
```

The API slices between markers. Best-effort by design — a missing or malformed
block leaves the exit-code-derived status untouched. **A parsing bug must never
turn a successful registration into a failed job.**

---

## Part 8: When things go wrong

### `needs_attention` — the one that matters

A `pending` or `unknown` journal row means a submit went out and we don't know
whether it landed. Priya is **blocked from re-registering** — correct, since a
retry could create two real appointments, but it means she is silently parked.

```http
GET /status/dangling
```
```json
[{"route": "AE-CHE", "combo": "Dubai - SCHENGEN",
  "registrant_id": "priya-che", "status": "unknown",
  "reason": "confirmation could not be read"}]
```

**Fix it by looking at the actual VFS portal**, then recording what you found:

```http
POST /status/resolve
{"route": "AE-CHE", "combo": "Dubai - SCHENGEN",
 "registrant_id": "priya-che", "status": "success",
 "reason": "verified WL-77231 on the portal"}
```

Only `success` or `failed` are accepted — resolving replaces ambiguity with a
checked fact. **Check first:** marking a real registration `failed` lets the bot
register her again.

### Common situations

| Symptom | Cause | Fix |
|---|---|---|
| `409` on trigger | A job is already running | Wait, or `POST /jobs/{id}/cancel` |
| Trigger runs but everything is `skipped` | `register_enabled = false` | Expected while parked — check `GET /status` |
| Waitlist opens, nothing fires | `auto_trigger_enabled = false` | Expected default |
| Client created but never runs | Created parked | `POST /clients/{id}/enable` |
| `422` on a valid-looking combo | Doesn't match the route file | `GET /routes/{r}/readiness` for valid labels |
| Supervisor exits 0 immediately | Run lock held | Normal — another run is in flight |
| `undelivered_webhooks > 0` | Your app was down | Read `state/webhook_deadletter.jsonl` |

### Reading a run

Every triggered job writes `logs/api_jobs/job-<timestamp>-<id>.log` — the child's
full stdout/stderr. `GET /jobs/{id}` gives you the path.

---

### Data retention: permanent

Client files are kept **indefinitely** — there is no sweep, and deletion is a
deliberate act:

```http
DELETE /clients/priya-che
```

Know what that means: `config/registrants/*.json` holds passport numbers, dates
of birth **and VFS account passwords**, for as long as the file exists. They are
gitignored with a pre-commit hook behind that, and on EC2 they are mode 0600 —
but on Windows `chmod` is a no-op and protection is the directory ACL.

Uploaded identity **documents** are different and still clean themselves up:
deleted on successful registration, with a 30-day backstop
(`document_retention_days`).

The practical consequence: any backup or disk image of this machine carries
every passport number you have ever held. If the machine ever leaves your desk,
whole-disk encryption is the cheap mitigation.

---

## Part 9: Turning it on

**The system ships parked.** Escalate one step at a time, and check `GET /status`
between each.

### Stage 1 — Manual dry runs (safe)

Nothing to change. Create clients, arm them, trigger by hand:

```http
POST /trigger/waitlist  {"registrant": "priya-che", "dry_run": true}
```

Watch `GET /jobs/{id}` → `results[]`. Everything reports `skipped` because
`register_enabled` is false. **This proves the whole chain without touching a
real registration.**

### Stage 2 — Manual live registration

```ini
[waitlist]
register_enabled = true      ; was false
dry_run = false              ; was true
```

Now a trigger with `"dry_run": false` **registers for real**. Do one client,
verify on the portal, confirm the webhook arrived.

### Stage 3 — Auto-trigger, dry run

```ini
auto_trigger_enabled = true    ; was false
auto_trigger_dry_run = true    ; leave true
```

The slot checker now fires runs by itself, but they stop before committing.
**Watch this for several real waitlist openings.** Confirm the right clients are
picked — especially on AE-NLD, where the label mapping matters.

### Stage 4 — Fully automatic

```ini
auto_trigger_dry_run = false
```

`GET /status` should now say **AUTO (LIVE)**. Only do this once Stage 3 has
picked the right people, repeatedly.

### Before Stage 3, fix AE-DEU

`config/routes/AE-DEU.json` has both "Dubai" rows carrying the **Abu Dhabi**
centre string. Enabling that route before fixing it would waitlist Dubai clients
in the wrong city.

---

## Part 10: File map

### New (this work)

| File | Purpose |
|---|---|
| [src/utils/runlock.py](src/utils/runlock.py) | Global run lock; reuses the existing shell mutex |
| [src/utils/webhook.py](src/utils/webhook.py) | Signed outbound callbacks, retry, dead-letter |
| [src/waitlist/validate.py](src/waitlist/validate.py) | Structured, browser-free validation |
| [src/waitlist/store.py](src/waitlist/store.py) | Atomic client-file writes |
| [src/waitlist/autotrigger.py](src/waitlist/autotrigger.py) | The slot-bot → waitlist-bot connection |
| [src/api/](src/api/) | FastAPI app: clients, triggers, jobs, status |
| [api_scripts/](api_scripts/) | Placeholder job + smoke test |

### Modified

| File | Change |
|---|---|
| [src/supervisor.py](src/supervisor.py) | Run lock; `waitlist_combos` in the outcome; auto-trigger hook |
| [src/waitlist/runner.py](src/waitlist/runner.py) | Takes the run lock around browser work |
| [src/waitlist/notify.py](src/waitlist/notify.py) | Posts webhooks alongside Telegram |
| [src/waitlist/__main__.py](src/waitlist/__main__.py) | `--json` structured output |
| [src/waitlist/journal.py](src/waitlist/journal.py) | Single-writer note now names the lock |
| [src/settings.py](src/settings.py) | `[webhook]` section; auto-trigger switches |

### Untouched (deliberately)

`register.py`, `guards.py`, `accounts.py`, `detect.py`, `context.py`,
`fields.py`, `redaction.py` — the registration engine was already correct. This
work built *around* it.

### Tests

**606 passing.** The ones that encode real bugs:

| File | Guards against |
|---|---|
| [test_autotrigger.py](tests/test_autotrigger.py) | The AE-NLD label trap |
| [test_runlock.py](tests/test_runlock.py) | Concurrent runs; PowerShell interop |
| [test_webhook.py](tests/test_webhook.py) | Signature forgery; PII leaks |
| [test_api_clients.py](tests/test_api_clients.py) | Password appearing in any response |
| [test_api_job_results.py](tests/test_api_job_results.py) | Parsing a verbatim real run log |

---

## The shortest possible summary

1. Your app **creates clients** through an authenticated local API, tunnelled via ngrok.
2. The **slot bot** notices a waitlist opening during its normal twice-hourly check.
3. After its browser closes, the **auto-trigger** maps the combo label, finds
   waiting clients, groups them by VFS account, and fires the waitlist bot.
4. The **waitlist bot** takes a global lock, logs in once per account, and
   registers clients one at a time.
5. Outcomes reach **Telegram and your app**, signed and PII-scrubbed.
6. **Six gates and a journal** make sure none of that happens until you decide
   it should — and that nobody is ever registered twice.
