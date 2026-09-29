# Waitlist Automation — API-Driven Client Onboarding & Auto-Trigger

**Status:** ALL PHASES COMPLETE (0-5) · 606 tests passing
**Created:** 2026-08-19
**Depends on:** [API_TUNNEL_SETUP.md](API_TUNNEL_SETUP.md) (the local webhook API — built and passing 31/31 checks)

---

## 1. The flow you described

```
Web app                                                          Your machine
   │
   │ 1. POST /clients  {route, combos, personal data, account}
   ├──────────────────────────────────────────────────────────▶  validate + write
   │                                                             config/registrants/<id>.json
   │ ◀──── 201 {client_id, warnings[]} ──────────────────────────┘
   │
   │                          2. supervisor's hourly slot check runs
   │                             detects "waitlist open" for AE-CHE / Dubai - SCHENGEN
   │                                             │
   │                          3. auto-trigger ───┘
   │                             fires the waitlist runner for the
   │                             clients waiting on that route+combo
   │                                             │
   │                          4. registration commits ──▶ Telegram
   │ ◀──── 5. outbound webhook: registration confirmed ────────────┘
   │        POST <your app>/webhooks/waitlist
```

**This is achievable, and most of the hard parts already exist.** The registration
engine, journal, dedup, guards, caps, and Telegram are all built and battle-tested.
What is missing is the connective tissue: writing client files from an API payload,
an event when a waitlist opens, and an outbound callback to your app.

---

## 2. What already exists vs. what must be built

### Already built (do not rebuild)

| Capability | Where |
|---|---|
| Registration engine (login → combo → fill → commit) | `src/waitlist/runner.py:346` `run_registration()` |
| Terminal states + result objects | `src/waitlist/result.py:13` `Status`, `:36` `WaitlistResult` |
| Write-ahead journal, dedup on `(route, combo, registrant_id)` | `src/waitlist/journal.py:110` `blocking_entry()` |
| Safety guards (6 gates) + per-run/per-day caps | `src/waitlist/guards.py:85` |
| Account resolution + capacity limits | `src/waitlist/accounts.py:84`, `:197` |
| Registrant validation, **already I/O-free** | `src/waitlist/registrant.py:157` `_validate(id, dict)` |
| Waitlist-open detection (read-only) | `src/waitlist/detect.py:157` `is_offered()` |
| Telegram send | `src/utils/telegram.py:57` `send_message()`, `:62` `send_error()` |
| Inbound authenticated webhook + background jobs | `src/api/` (31/31 checks passing) |

### Must be built

| # | Gap | Why it matters |
|---|---|---|
| ~~G1~~ | ~~No client-file writer~~ — **closed in Phase 1.** `src/waitlist/store.py`. | Closed. |
| ~~G2~~ | ~~No "waitlist opened" event~~ — **closed in Phase 3.** `src/waitlist/autotrigger.py`, hooked at `supervisor.py` after the browser closes. | Closed. |
| ~~G3~~ | ~~No outbound HTTP~~ — **closed in Phase 4.** | Closed. |
| ~~G4~~ | ~~Validation prints, not returns~~ — **closed in Phase 1.** `src/waitlist/validate.py`. | Closed. |
| ~~G5~~ | ~~`job_command` is the placeholder~~ — **closed in Phase 2.1.** Now runs the real waitlist CLI. | Closed. |
| ~~G6~~ | ~~Journal assumes a single writer~~ — **closed in Phase 0.** `src/utils/runlock.py` serialises every browser-driving run. | Closed. |
| ~~G7~~ | ~~CDP port / config-key collision~~ — **resolved by G6's lock**, not a separate fix. `kill_stale_bot_chrome()` already frees the port from crashed runs; only concurrent *live* runs collided. | Closed by Phase 0.2. |
| G8 | **No account auto-allocation.** `accounts.resolve` is a 3-source manual lookup. | Every API-created client needs an account, or they all share one. |

### Already fixed during this design pass

Two bugs in the API would have blocked this flow outright. Both are fixed and covered by regression checks in [api_scripts/smoke_test.py](api_scripts/smoke_test.py):

- **Combo labels were rejected.** `TriggerRequest.combo` used a slug regex, but real labels are `"Dubai - SCHENGEN"` — spaces and dashes. Every real combo would have 422'd.
- **`AE-MT` was rejected.** The route regex demanded exactly 3 destination letters; `config/routes/AE-MT.json` is real. Now `^[A-Z]{2}-[A-Z]{2,4}$`, matching `registrant._ROUTE_RE`.

---

## 3. Cases and decisions you need to think through

This is the part you asked for — the things that will bite.

### 3.1 Identity and duplicates

- **What makes a client unique?** Filename is the id today (`ahmed-deu.json`). If your web app has its own user ids, you need a mapping. Recommendation: `<appuserid>-<route-lowercase>.json`, e.g. `u10432-che.json`, so it's derivable in both directions and one user can hold several routes.
- **Same person, two routes** = two files today, which duplicates their passport number. If they edit their passport in your app, **both files must update** or they silently diverge.
- **Re-submitting the same client.** Is `POST /clients` create-only (409 on conflict) or upsert? Recommendation: create-only, with an explicit `PUT /clients/{id}` for updates, so an accidental double-submit from a flaky network never overwrites live data.
- **Editing a client mid-registration.** If a registration is `pending` in the journal, an edit must be refused — the data being typed into the portal is in flight.

### 3.2 The trigger — where it fires, and how often

Two candidate hook points, and they are **not** equivalent:

| Hook | Granularity | Trade-off |
|---|---|---|
| `slot_check.py:462` | Per **combo** — exactly which one opened | Fires mid-slot-check, inside the browser context. Cannot launch a second browser safely from here. |
| `supervisor.py:630` | Per **route** — only a count (`outcome["waitlist"]`) | Clean boundary, browser already closed. But you lose which combo opened. |

**Recommendation: fire at `supervisor.py:630`, but enrich `_outcome()` to carry the combo labels, not just the count.** That gives you clean sequencing *and* combo granularity. `_outcome()` at `supervisor.py:183` already receives the full `slots` list; it just discards the detail, so no call-signature change is needed.

> **But the labels it carries are not the labels clients use.** `result_label()` deliberately ignores the route file's `"label"` field, and for AE-NLD the two diverge completely. See [§7.2](#72-the-label-trap-do-not-skip-this) — this is the one part of the connection that fails *silently*.

Other decisions:
- **Debounce.** A waitlist can stay open for hours; the checker runs twice an hour. Without a cooldown you re-trigger every 30 minutes. The journal's `blocking_entry` prevents double *registration*, but you'd still launch a browser each time. Reuse the `waitlist_cooldown` pattern from `notify.py`.
- **Do you trigger immediately, or queue?** Immediately is simpler but means a slot-check tick can take 10+ extra minutes. Queuing needs a worker.
- **What if it opens for a route with no waiting clients?** Cheap check — do it *before* launching a browser.

### 3.3 Concurrency — the highest-risk area

`journal.py:19-28` says outright: *"this bot is run ON DEMAND, so there is exactly one writer... If waitlist registration is ever moved onto the scheduler (concurrent runs), swap this for SQLite with a partial UNIQUE INDEX."*

**Your design does exactly what that warning describes.** Three writers become possible:
1. The scheduled slot-check (`run_task.ps1`, twice hourly)
2. The auto-trigger firing the waitlist runner
3. A manual `python -m src.waitlist run`

Plus `runner.py:437` writes a **global** config key (`browser.cdp_url`) and uses a fixed CDP port — so two concurrent runs fight over the same Chrome debugging port.

**Mitigation, in order of preference:**
- **A cross-process lock (mutex/lockfile) covering *any* browser-driving run.** `run_task.ps1` already uses a global mutex for the scheduler — extend that same mutex to the waitlist runner. Simplest correct fix.
- The API's `single_flight` only protects API-initiated jobs — it does **not** know about the scheduler. Necessary but not sufficient.
- SQLite journal migration if you ever want genuine parallelism. Bigger job; probably unnecessary if you take the lock.

### 3.4 Safety — this thing books real appointments

`register_enabled` defaults to **False**, `max_per_run` to 1, `max_per_day` to 5. These exist for good reason. Decide deliberately:

- **Does an API-created client default to `enabled: true`?** Recommendation: **no.** Create parked, require an explicit activation call. A bug in your web app should not be able to mass-register.
- **Dry-run first.** Consider forcing the first run for any new client to be a dry run, with live registration only after a clean dry-run result.
- **Who is accountable for a wrong booking?** The commit click is the point of no return (`register.py`). `unknown` status means *submitted, outcome unconfirmed* — a human must resolve it (`python -m src.waitlist resolve`). Your app needs to surface that state, not hide it.
- **PII.** `config/registrants/*.json` holds passport numbers and DOB, and is gitignored with a pre-commit hook backstop. An API that accepts this data over a tunnel means **passport numbers now travel over the network.** They must never be logged — `src/waitlist/redaction.py` exists; the API must use it.

### 3.5 Failure and delivery

- **Outbound webhook retries.** Your app will be down sometimes. Needs retry with backoff and a dead-letter log, or you lose confirmations silently.
- **Webhook authenticity.** Your app must verify the callback is really from the bot — HMAC-sign the body with a shared secret (mirror of the inbound token).
- **Ordering.** Telegram and your app may receive the same event out of order. Include a monotonic sequence or timestamp.
- **`SlotsAvailable`.** `runner.py:50` raises this when a real slot banner appears — meaning the client should *book*, not waitlist. Your app needs to handle this as a distinct, and much better, outcome.
- **Partial success.** One client succeeds, the next fails. Results are per `(client, combo)`; the callback must reflect that, not a single run-level boolean.

### 3.6 Config prerequisites the API cannot fix

A route only accepts waitlist registration when **all** of these hold (§8 of the map):
1. Uncommented in `config/vfs_urls.ini`
2. `config/routes/<ROUTE>.json` exists with the named combos
3. `config/waitlist/<ROUTE>.json` exists **and** `"enabled": true`
4. Exactly one step flagged `"commits": true`
5. `[waitlist] register_enabled = true`

Only **5 of 12** routes have a waitlist config today (`AE-CHE, AE-DEU, AE-FRA, AE-ITA, AE-NLD`), and `AE-DEU` is `"enabled": false` pending a **known centre bug** — both "Dubai" rows carry the Abu Dhabi centre string, so a Dubai client would be waitlisted at the wrong city.

**`POST /clients` must reject a route that is not registration-ready, and say precisely which of the 5 conditions failed.** Otherwise your app happily creates clients that can never register.

---

## 4. Task breakdown

Phases are ordered so each is independently shippable and testable. **Phase 0 is a prerequisite for everything and should not be skipped.**

---

### Phase 0 — Concurrency safety (blocking) — ✅ COMPLETE

> Do this first. Every later phase increases the chance of concurrent runs, and
> the journal explicitly does not tolerate them.

- [x] **0.1** ~~Extract the global mutex~~ **DONE** — [src/utils/runlock.py](src/utils/runlock.py). Reuses the *existing* names (`Global\VfsSlotChecker` on Windows, `/tmp/vfs-slot-checker.lock` on POSIX), so `run_task.ps1` and `run_ec2.sh` need no change. Re-entrant in-process, exclusive between processes.
- [x] **0.2** **DONE** — `supervisor.main()` uses `on_busy="skip"` + exit 0 (an overlapping tick is normal, matching the scheduler); `runner.run_registration()` uses `on_busy="raise"` (a deliberate registration must be *told* it did not run). The lock is taken after validation but before Chrome starts, so a bad config still fails fast.
- [x] **0.3** ~~Make the CDP port dynamic~~ **Not needed — verified.** `ChromeProcess.start()` already calls `kill_stale_bot_chrome()` (`chrome_launcher.py:263`), which kills any bot Chrome by profile prefix and frees port 9222 before launching. The port only collides between *concurrent live* runs, which 0.2 now prevents. The same reasoning covers the global `set_config_value("browser","cdp_url")` write at `runner.py:437`: it is only unsafe with two runs in flight. **G7 was overstated in the original analysis.**
- [x] **0.4** **DONE** — [tests/test_runlock.py](tests/test_runlock.py), 11 tests: cross-process exclusion (real child processes, not threads), crash recovery, re-entrancy, and a PowerShell-interop regression guard. Verified live: a held lock made the real supervisor skip and exit 0 with no browser launched; it ran normally once free. Full suite: **508 passed**.
- [x] **0.5** **DONE** — `journal.py`'s header now names the lock and every entry point that takes it, and states the SQLite migration is only needed for genuine parallelism (several runs at once), not for the current take-turns model.

---

### Phase 1 — Client file creation via API — ✅ COMPLETE

- [x] **1.1** **DONE** — [src/waitlist/validate.py](src/waitlist/validate.py) `validate_payload()`. Accumulates instead of raising; each `Problem` carries `field`/`message`/`severity`/`hint` so the web app can attach errors to form inputs.
- [x] **1.2** **DONE** — [src/waitlist/store.py](src/waitlist/store.py): `create()` / `update()` / `delete()` / `set_enabled()` / `list_ids()`. Atomic (temp + fsync + `os.replace`); `create()` refuses to overwrite. **Note:** chmod 0600 is a no-op on Windows — protection there is the directory ACL, same as the existing hand-written files. Documented in the module header.
- [x] **1.3** **DONE** — `validate.route_readiness()`. Verified live: AE-CHE ready with 2 combos; AE-DEU correctly not-ready ("enabled": false); AE-MT not-ready (no login URL). Also returns the valid combo labels, so a signup form can populate its dropdown from `GET /routes/{route}/readiness`.
- [x] **1.4** **DONE** — `validate.precheck_client()`, plus `check_combos()` and `check_templates()`. Disabled combinations are excluded, so a client cannot queue for one the slot checker never checks.
- [x] **1.5** **DONE** — [src/api/clients.py](src/api/clients.py). 201 / 409 / 422 with `problems[]` at the TOP level of the body. Fixed a real bug found here: the error handler stringified a structured `detail`, handing the web app an unparseable Python repr.
- [x] **1.6** **DONE** — all four, plus `GET /routes/{route}/readiness`. Redaction is structural (`_public_view()`), not text-based: secrets dropped entirely, PII masked (`Z9****43`). `redaction.scrub()` is for log TEXT and was the wrong tool for response bodies.
- [x] **1.7** **DONE** — clients are created PARKED (`enabled: false`) unless explicitly armed, and `/enable` refuses a client that would not run.
- [x] **1.8** **DONE** — [tests/test_api_clients.py](tests/test_api_clients.py), 28 tests. Full suite **536 passed**. Verified live against the running server, and `python -m src.waitlist check --registrant <id>` validates an API-created client end to end.

---

### Phase 2 — Wire the API to the real waitlist runner — ✅ COMPLETE

- [x] **2.1** **DONE** — the default is now `[python, -m, src.waitlist, run, --json]`. The placeholder stays available via `$env:VFSAPI_JOB_COMMAND` and the smoke test still uses it (31/31). Verified live: the API logs `Job command: ... -m src.waitlist run --json` at startup.
- [x] **2.2** **DONE** — verified live end to end: a trigger produced `... run --json --registrant ahmed --dry-run` and the run reached the real VFS portal. Combo labels with spaces survive intact (regression check in the smoke test).
- [x] **2.3** **DONE** — added `--json` to the waitlist CLI, emitting a marker-delimited, redaction-scrubbed result block; `JobManager._attach_results()` parses it into `results[]`, `outcome`, and `needs_attention`. Verified live: `GET /jobs/{id}` returned `ahmed | Dubai - SCHENGEN | skipped` with the reason, not just exit 0. Parsing is best-effort — a missing or malformed block never changes the job's status.
- [x] **2.4** **DONE** — CLI exit 2 (`SlotsAvailable`) maps to a dedicated `slots_available` job status, explicitly NOT a failure: a bookable slot means the run correctly declined to waitlist. A `pending`/`unknown` result sets `needs_attention: true` with guidance to resolve it by hand — never to retry, which risks a duplicate registration.
- [x] **2.5** **DONE** — the two are complementary and both needed: `single_flight` rejects a second API trigger with 409 before spawning; the Phase 0 runlock additionally excludes the scheduler and manual runs, which the API cannot see. A trigger racing a scheduled slot check now fails with a clear message instead of corrupting the journal.

---

### Phase 3 — The "waitlist opened" event — ✅ COMPLETE

> See [§7 — The slot-bot → waitlist-bot connection](#7--the-slot-bot--waitlist-bot-connection)
> for the full design rationale behind these tasks. Read it before starting:
> three of the constraints below are non-obvious and one (3.1) is a real trap.

- [x] **3.1** **Map `result_label` → client combo label.** `_outcome()` receives `slot_results` as `(result_label, message)` tuples, and `result_label` (`slot_check.py:334`) *deliberately ignores* the route file's `"label"` — the exact string clients put in their `combos[]`. For AE-NLD these diverge completely (see §7.2). Build `resolve_combo(route, result_label) -> combo_label | None` that maps back via the route file's combination dicts. **A naive string match silently matches nothing for AE-NLD.**
- [x] **3.2** Enrich `supervisor._outcome()` (`:183`) with `waitlist_combos: list[str]` (client-facing labels, post-3.1), alongside the existing `waitlist` count. No call-signature change needed — `slots` already carries what's required.
- [x] **3.3** Add the emission point at `supervisor.py:630`, after `outcomes.append` — **not** inside `slot_check`. Browser must be closed first (§7.1).
- [x] **3.4** Build the matcher: given `(route, combo)`, find clients via `registrant.for_route()` that are `enabled`, list that combo, and have no `journal.blocking_entry()`. **Return early if empty — do not launch a browser.** This is the common case and must stay cheap.
- [x] **3.5** **Group the matched clients by resolved account** (`accounts.resolve` per client). One run is one login (`runner.py:399`), so N accounts = N sequential runs. Emit one trigger per account group.
- [x] **3.6** Debounce per `(route, combo)`, reusing the `waitlist_cooldown` pattern from `notify.py`. A waitlist stays open for hours; the checker runs twice hourly.
- [x] **3.7** Auto-trigger: invoke the runner per account group, respecting Phase 0's lock, `register_enabled`, `max_per_run`, `max_per_day`.
- [x] **3.8** Handle the two run-aborting outcomes (§7.4): `SlotsAvailable` → report as a *better* outcome and re-trigger the remaining clients; `WaitlistCommittedError` → halt, surface for human resolution, **do not retry**.
- [x] **3.9** **Kill switch**: `[waitlist] auto_trigger_enabled`, defaulting to **False**. Must be disable-able without a code change.
- [x] **3.10** Tests: label mapping for AE-NLD *and* AE-CHE, opened-with-no-clients (asserts no browser launch), opened-with-clients, multi-account grouping, debounce suppression, cap enforcement, `SlotsAvailable` mid-plan, kill switch.

---

### Phase 4 — Outbound webhook to your app — ✅ COMPLETE

- [x] **4.1** **DONE** — [src/utils/webhook.py](src/utils/webhook.py), stdlib `urllib` only (no new bot dependency), never raises, returns a `DeliveryResult`.
- [x] **4.2** **DONE** — `X-VFS-Signature: sha256=<hmac>` over the RAW body bytes, compared constant-time. `verify_signature()` is the reference implementation; §3.3 of [API_TUNNEL_SETUP.md](API_TUNNEL_SETUP.md) has the Node/Express version with the raw-body caveat. Verified against a real HTTP server, not a mock.
- [x] **4.3** **DONE** — retries ~1s/4s/10s on 5xx, timeouts and connection errors; 4xx (except 408/429) is permanent and not retried. Exhausted deliveries append to `state/webhook_deadletter.jsonl` (fsync'd), never dropped.
- [x] **4.4** **DONE** — all five plus `test.ping`, in a versioned envelope (`version`, `event`, `sequence`, `sent_at`, `data`). `pending`/`unknown` routes to `registration.needs_attention` so it can never be mistaken for an ordinary success or failure.
- [x] **4.5** **DONE** — scrubbed at the serialisation boundary, so nested values are covered. Verified live: a passport number and client email in a `reason` string arrived as `[redacted]` while `vfs_reference` survived intact. If scrubbing ever produced invalid JSON, a minimal event is sent instead — failing to deliver beats leaking.
- [x] **4.6** **DONE** — `notify_registered()` posts the webhook *and* keeps Telegram. One difference by design: the web app is told about `skipped` outcomes too (its user is waiting on an answer), while Telegram stays quiet on those.
- [x] **4.7** **DONE** — [tests/test_webhook.py](tests/test_webhook.py), 24 tests against a real local HTTP server (a mock would agree with a broken signature). Full suite **571 passed**.

---

### Phase 5 — Operations — ✅ COMPLETE

- [x] **5.1** `GET /status` — routes ready, clients waiting, journal state, last check time, caps consumed today.
- [x] **5.2** Surface `journal.dangling()` (`:125`) — `pending`/`unknown` rows needing human resolution — in both the API and Telegram. **These are the dangerous ones.**
- [x] **5.3** Expose `python -m src.waitlist resolve` equivalently via API, so your app can clear a stuck row.
- [x] **5.4** Alert when a route's readiness *changes* (e.g. someone disables a waitlist config), so clients don't sit waiting on a dead route.
- [x] **5.5** Runbook: what to do on `unknown`, on a wrong-centre booking, on a stuck lock.
- [ ] **5.6** Fix the **known AE-DEU centre bug** (both Dubai rows carry the Abu Dhabi centre) before enabling that route — it is currently the only thing keeping `AE-DEU` waitlisting off.

---

## 5. Open questions for you

**Decided 2026-08-19:**

- **Accounts (Q2): the web app supplies per-client VFS credentials.** Each client payload carries `account` + `account_password`. This scales past `max_clients_per_account`, but means VFS passwords now travel over the tunnel and land in the client file — so Phase 1 must treat them as secrets (never logged, never echoed back, file mode 0600) and clients sharing an account still register in one sequential run.
- **Auto-register (Q3): automatic, but DRY-RUN only at first.** Phase 3 auto-triggers and walks the whole flow, stopping before the commit click. Live registration is a separate, deliberate flip once the dry runs look right.

- **Multi-tenant (Q4): YES — several of your users can queue for the same route+combo.** Safe as designed, *because* each client brings their own VFS account (Q2): `accounts.capacity_verdict` compares clients **within one account**, so two tenants on the same combination never collide. Verified — two clients on `Dubai - SCHENGEN` with different accounts produce two separate runs, two logins, run sequentially under the global lock. Leave `one_client_per_account_combo` at its default (warn-only); it only matters if you ever put two tenants on ONE account.

- **Web app (Q5): https://www.travnooker.com/.** A hosted app, so its egress IP depends on the host and may not be stable. ngrok IP-allowlisting is paid-tier anyway; the `X-Webhook-Secret-Token` remains the real access control. Set `[webhook] url` to `https://www.travnooker.com/<your-callback-path>`.

- **Retention (Q6): PERMANENT — client files are kept indefinitely.** No sweep to build; current behaviour already matches. Note what this means: `config/registrants/*.json` holds passport numbers, dates of birth **and VFS account passwords**, kept for as long as the file exists. Uploaded identity DOCUMENTS still auto-delete on success (`journal.py` → `_delete_documents_on_success`) plus a 30-day backstop (`document_retention_days`) — that is separate and unchanged. Deletion is therefore a deliberate act: `DELETE /clients/{id}` when a client is done with you.

---

### Residual risk accepted with permanent retention

Not blocking, but stated so the choice is on the record:

- The files are gitignored, with a pre-commit hook as a second line of defence.
- On Windows they inherit the directory ACL (owner + Administrators); `chmod 0600` is a no-op there. On EC2 the 0600 applies.
- Anything permanent grows: a machine backup, a disk image, or a support copy carries every passport number you have ever held. Whole-disk encryption is the cheap mitigation if this machine is ever off your desk.

---

## 6. Suggested order — ALL COMPLETE

> Phases 0, 1, 2, 4, 3 and 5 are done, in that order. **606 tests pass.**
> See [SYSTEM_GUIDE.md](SYSTEM_GUIDE.md) for how it all fits together.
>
> **The system ships PARKED.** `register_enabled=false` and
> `auto_trigger_enabled=false`, so nothing registers until you deliberately
> switch it on. `GET /status` tells you the current posture in one sentence.

### Original plan

Ship in this sequence — each step is independently useful, and the risky parts come only after the safety work:

1. **Phase 0** — concurrency lock. *Non-negotiable prerequisite.*
2. **Phase 1** — client creation. Immediately useful even while triggering stays manual.
3. **Phase 2** — real runner wiring. Now your app can trigger a real (dry-run) registration.
4. **Phase 4** — outbound webhook. Close the loop before automating the trigger, so you can *see* what would have happened.
5. **Phase 3** — auto-trigger. The riskiest piece, done last, behind a default-off kill switch.
6. **Phase 5** — operations.

A reasonable first milestone is **Phases 0 + 1 + 2 with `dry_run` forced true**: your web app can create clients and trigger real dry runs, with nothing able to commit a booking. That validates the entire chain at zero risk.

---

## 7. The slot-bot → waitlist-bot connection

The design detail behind Phase 3. **The one-by-one client loop you want already
exists** — `runner.run_registration()` at [runner.py:479](src/waitlist/runner.py#L479)
is a `for person, combo_label in plan:` loop with per-client error isolation: a
`WaitlistConfigError` on client 3 records a `FAILED` result and continues to
client 4. You are not building a queue. You are building the *trigger* into an
engine that already queues correctly.

### 7.1 Why the trigger cannot fire from inside the slot check

The tempting hook is [slot_check.py:462](src/vfs_bot/slot_check.py#L462) — the
exact moment `detect.is_offered()` returns true, with per-combo granularity.
**Do not hook there.** Three independent reasons, any one of them fatal:

| # | Reason | Evidence |
|---|---|---|
| 1 | **Wrong account.** The slot checker runs on a *rotating* slot-check account. Waitlist accounts are a deliberately separate pool — nothing is read from `credentials.local.ini`. A waitlist entry *belongs to* the account that created it, so registering on the slot-check account creates an entry the client can never see or cancel. | [accounts.py:14-18](src/waitlist/accounts.py#L14-L18) |
| 2 | **Wrong egress/profile.** A waitlist run resolves its own proxy and a Chrome profile keyed to `resolved.email`. Reusing the slot-check session gets neither. | [runner.py:432](src/waitlist/runner.py#L432) |
| 3 | **Reentrancy.** You would be launching a second Chrome from inside a live page context, mid-slot-check, colliding on the same fixed CDP port. | [runner.py:437](src/waitlist/runner.py#L437) |

The existing shim [src/vfs_bot/waitlist.py](src/vfs_bot/waitlist.py) already
encodes this: it deliberately does **not** re-export `register`, so the
always-on slot-check path is never "one attribute access away from a mutating
call."

**Conclusion: the browser session cannot be reused. A waitlist run is always a
fresh login.** That is a correctness requirement, not an optimisation — and it
is why the trigger fires *after* the route's browser has closed.

### 7.2 The label trap (do not skip this)

`_outcome()` receives `slot_results` as `(result_label, message)` tuples. But
`result_label()` ([slot_check.py:334](src/vfs_bot/slot_check.py#L334))
**deliberately ignores** the route file's `"label"` field — and that field is
exactly what clients name in their `combos[]` and what `runner._combo_parts()`
matches against.

Measured divergence on the real config:

| Route | Client writes in `combos[]` | `result_label()` produces |
|---|---|---|
| AE-CHE | `Dubai - SCHENGEN` | `Dubai - SCHENGEN` ✅ identical |
| AE-NLD | `Dubai - Tourist Visa` | `Netherlands Visa application center- Dubai - Tourist Visa - Tourist Purpose` ❌ |
| AE-NLD | `Abu Dhabi - Schengen Visa` | `Netherlands Visa Application Center-Abu Dhabi - Schengen Visa - Schengen Short Stay` ❌ |

**A naive string match works for AE-CHE and silently matches nothing for every
AE-NLD client.** That failure mode is the worst kind: it looks like "no clients
waiting", so nothing fires, no error is raised, and the route appears healthy.

Task 3.1 exists to close this: map back through the route file's combination
dicts rather than comparing label strings.

### 7.3 One run is one login — group by account

[runner.py:399-411](src/waitlist/runner.py#L399-L411) is explicit:

> *"Every client in this plan must resolve to the SAME account, because one run
> is one login. Mixed pins are a config error, caught here before Chrome starts."*

So "clients in line, one by one" holds **only within an account**. Five clients
on `AE-CHE / Dubai - SCHENGEN` pinned across three VFS accounts is **three
separate runs, three logins, three browsers** — and sequentially, because of the
concurrency constraint in Phase 0.

This makes account allocation (gap G8, open question 2) a throughput ceiling,
not a detail: it decides how many clients you can actually serve per open
window, and `max_clients_per_account` caps it further.

### 7.4 Two behaviours that abort the whole run

Both are correct, and both will surprise you if your app assumes a run processes
every client:

**A real slot stops everything.** [runner.py:466](src/waitlist/runner.py#L466) —
if a bookable slot banner appears for any combo, `SlotsAvailable` is raised and
the run halts, *including clients queued behind it*. The reasoning is sound
(book it, don't queue for it), but one lucky combo suspends the others'
waitlisting. Your app should treat this as a **better** outcome, notify the
client to book, and re-trigger the remainder.

**An ambiguous submit stops everything.** `WaitlistCommittedError` → journalled
as `unknown`, run halted. With one submit outstanding, the safe move is to touch
nothing else on that account. This needs a human (`python -m src.waitlist
resolve`). **Your app must surface it, never auto-retry it** — a retry risks a
duplicate registration on an entry that may already exist.

### 7.5 The resulting flow

```
slot check finds waitlist for a combo
        │
        ▼
record (route, result_label) in the outcome         ← 3.2
        │
        ▼
route finishes, browser closes                      ← clean boundary (7.1)
        │
        ▼
map result_label → client combo label               ← 3.1  (the trap, 7.2)
        │
        ▼
match: enabled clients on (route, combo),
       no blocking journal entry                    ← 3.4
        │
        ├── none ──▶ stop. no browser launched.     ← the cheap common case
        │
        ▼ some
group matched clients by resolved account           ← 3.5  (one run = one login)
        │
        ▼
for each account group, sequentially:
    acquire the global run lock                     ← Phase 0
    run_registration()  ──▶  one-by-one client loop ← already exists
        │
        ├─ SlotsAvailable ────▶ halt; report; re-trigger the rest   ← 3.8
        ├─ WaitlistCommittedError ─▶ halt; human resolution needed  ← 3.8
        └─ per-client results ─▶ Telegram + outbound webhook (Ph. 4)
```

### 7.6 What this means for sequencing

The connection itself is **small** — an enrichment to `_outcome`, a label
mapper, a matcher, an account grouper, and a call. The engine underneath is
already built and hardened.

What makes Phase 3 the last thing to ship is not its size but its blast radius:
it is the step that lets the system book real appointments with no human in the
loop. Everything it depends on (the lock, the label mapping, the callback so you
can *see* what it would have done) should be working and observed first — which
is exactly the order in §6.
