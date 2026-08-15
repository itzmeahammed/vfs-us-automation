# Waitlist Registration — Task List

Status of the waitlist-registration feature (`src/waitlist/`). The slot checker
is unchanged and unaffected — this is additive.

**Legend:** `[x]` done · `[ ]` not started · `[~]` partial / blocked

---

## Phase 1 — Foundation ✅ COMPLETE

### Package structure
- [x] `src/waitlist/` package, separate from `src/vfs_bot/`
- [x] `detect.py` — READ-ONLY detection, moved from `vfs_bot/waitlist.py`
- [x] `notify.py` — Telegram reporting, moved
- [x] `src/vfs_bot/waitlist.py` reduced to a re-export shim
- [x] **Verified**: slot-check path imports none of the registration stack
      (no playwright, no pydantic, no `register`/`accounts`/`doctor`)

### Configuration model
- [x] `config/waitlist/<ROUTE>.json` — WHERE things are (selectors, steps)
- [x] `config/registrants/<client>.json` — ONE client, self-contained
- [x] `{{placeholder}}` resolution joins the two by field NAME (`context.py`)
- [x] Modifiers: `|upper` `|lower` `|title` `|digits` `|date:` `|default:` `|pad:`
- [x] `extends` inheritance for route configs (cycle-safe)
- [x] Validation rejects a selector appearing in a client file
- [x] Targeting keys (`route`/`combos`/`enabled`/`account*`/`proxy`) stripped
      from form data, so a password can never be typed into a page field

### Safety
- [x] Commit boundary declared in JSON (`"commits": true`)
- [x] `WaitlistCommittedError` **outside** the `RetryableError` hierarchy —
      the supervisor can never auto-retry a submit
- [x] Write-ahead journal, `fsync`'d before the committing click
- [x] Dedup on `(route, combo, client)`, whitespace/case-insensitive
- [x] Dangling-entry gate: unresolved attempts block that triple
- [x] Four independent opt-ins before anything submits
- [x] `register_enabled = false` and `dry_run = true` by default
- [x] Per-run and per-day caps

### Accounts
- [x] Separate pool — nothing read from `credentials.local.ini`
- [x] Resolution: `--email` → client file → `[waitlist] account`
- [x] **No fallback to the hourly rotation** (deliberate: a waitlist entry
      belongs to the account that created it)
- [x] Account+password all-or-nothing, validated at load
- [x] Several clients may share one account
- [x] Capacity knobs: `max_clients_per_account`, `one_client_per_account_combo`

### Proxy
- [x] Pool read from `config/proxylist.txt` (shared with the slot checker)
- [x] Stable per-account pinning — `[waitlist] accounts` order, else a hash
- [x] Explicit pin beats the `[proxy] enabled` master switch
- [x] Usage meter logged at end of every run (browser MB + billed proxy MB)
- [x] Silent when the browser never ran (no misleading "0.0 MB")

### Operability
- [x] CLI: `status` `check` `run` `doctor` `journal` `resolve`
- [x] `check` validates config + data with **no browser**
- [x] `doctor` probes live selectors without typing/ticking/submitting
- [x] `doctor --walk` reaches later pages (advances form, never commits)
- [x] Log redaction — PII scrubbed from log, archive, console, Telegram
- [x] Telegram OFF by default for waitlist runs
- [x] `SlotsAvailable` stops the run when a real slot exists
- [x] PII gitignored (`config/registrants/*.json`), template committed

### Tests
- [x] **263 passing** (was 20 failing before — broken `.venv`, now fixed)
- [x] `test_waitlist_context.py` — placeholders, modifiers, client validation
- [x] `test_waitlist_config.py` — route config, inheritance, journal, dedup
- [x] `test_waitlist_accounts.py` — resolution, capacity, proxy pinning
- [x] `test_waitlist_detect.py` — checkbox detection across portal variants
- [x] `test_waitlist_redaction.py` — PII scrubbing
- [x] `test_waitlist_doctor.py` — probe logic
- [x] Existing `test_waitlist*.py` still pass via the shim

---

## Phase 2 — Switzerland end-to-end ✅ COMPLETE

**Proven with a real registration on 2026-08-11: `SWDB79923880977`.**
All four steps executed, the write-ahead `pending` row was written before the
commit, and the confirmation page was matched and its reference captured.

- [x] `config/waitlist/AE-CHE.json` — 4 steps mapped from real HTML
- [x] Label-based field addressing (this portal emits **no**
      `formcontrolname`, only positional `mat-input-N` ids)
- [x] Split phone field via `index: 0/1` under one "Contact number" caption
- [x] Duplicate-id consent checkboxes matched by label + index
- [x] 20s dwell before Save (portal demands 18s)
- [x] Checkbox found without `formcontrolname`; button is **Continue**
- [x] `config/registrants/ahmed.json` filled in

- [x] Step 3 `details_summary` — after Save the URL STAYS on `/your-details`
      and only the content changes, so it is gated on text not URL
- [x] `confirmation` block — `/confirmation`, "Your Appointment is Waitlisted",
      reference pattern capturing `SWDB…`
- [x] **One live registration**, confirmed on the portal

### Bugs found and fixed while proving it
- [x] `login()` ran the full slot check → split into `authenticate()` /
      `start_booking()` / `start_slot_check()`; ~45s and a stray Telegram
      notice removed per run
- [x] `if_present` was unreachable (Playwright's `TimeoutError` matched the
      later `except Exception`) → replaced with a 1.5s presence probe
- [x] Consent-checkbox label wraps an `<a>`; clicking its centre opened the
      T&Cs in a new tab → click ladder now targets the native input first and
      re-reads state after every strategy
- [x] Save enables BEFORE the 30s countdown expires but does nothing → wait
      the countdown *and* poll the button

### Open questions
- [x] **Can one account hold two entries for the same combo?** YES →
      `one_client_per_account_combo` stays `false` (warn-only)
- [x] **Does the combo still show in slot checks?** Depends on availability
- [ ] **Is a waitlist registration cancellable?** If yes, much of the
      paranoia can be relaxed. If no, `verify` becomes essential.
- [ ] **What happens when a slot opens?** The confirmation page says "check
      your dashboard occasionally and emails for slot availability" — which
      suggests VFS does NOT reliably push. If someone must poll, there is a
      whole missing phase (monitoring waitlisted entries) not yet designed.

---

## Phase 3 — Scale to 10+ countries 📋 NOT STARTED

- [ ] Country #2 — repeat the Phase-2 loop (recon → config → doctor → dry → live)
- [ ] Country #3, then batch the rest once the pattern is proven
- [ ] Extract genuinely shared structure into `config/waitlist/_default.json`
      *(deliberately deferred: with one route there is nothing to compare)*
- [ ] Add widgets only as real portals demand them (datepicker, file upload…)
- [ ] Per-country fixture HTML in `tests/fixtures/waitlist/` for offline work

---

## Phase 4 — Verification 🔍 NOT STARTED (needed by ~country 3)

**The biggest remaining architectural gap.** The journal records what the bot
*believes*; nothing ever checks VFS. It is currently unfalsifiable.

- [ ] `python -m src.waitlist verify --account <email>` — log in, read the
      account's real waitlist entries, compare against the journal
- [ ] Auto-resolve `unknown` entries found on the portal
- [ ] Flag journal-says-success-but-portal-says-no (the case nothing can
      currently detect)
- [ ] Needs: what VFS's "my waitlist entries" page looks like — comes free
      from the Phase-2 live run

---

## Phase 5 — Hardening 🛡️ LOW PRIORITY

- [ ] `chmod 600` on `config/registrants/*.json`
- [ ] Encryption at rest for client files *(judgement call — plaintext on EC2
      is the current risk)*
- [ ] SQLite journal *(only if this ever leaves single-writer manual use;
      JSONL is genuinely fine today)*
- [ ] Prune stale journal entries
- [ ] Update `README.md` / `COMMANDS.md` with the waitlist commands

---

## Deliberately NOT doing

Recorded so they are not re-litigated:

- **Scheduling it** — runs on demand, by design
- **A web UI** — the CLI is faster for a single operator
- **Microservices / queues / containers** — one operator, ~10 clients
- **More `{{}}` modifiers** — only three of seven are used
- **Route-config inheritance right now** — needs 4–5 routes before the shared
  parts are visible rather than guessed

---

## Right now

```powershell
# 1. Do the selectors still match the live page?
python -m src.waitlist doctor --route AE-CHE --walk

# 2. Walk all four steps, stop before Confirm
python -m src.waitlist run --route AE-CHE

# 3. Only after both look right
python -m src.waitlist run --route AE-CHE --live
```

Step 1 gives the two missing URLs. Everything in Phase 2 is blocked on it.

**Note:** `[proxy] enabled = false` in `config/config.ini` and there is no
`config/config.local.ini`, so runs currently egress from **this machine's own
IP**. Create `config/config.local.ini` with `[proxy] enabled = true` to use
`config/proxylist.txt`.
