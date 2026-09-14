# TASKS — Post-Invitation Booking

Task breakdown for the feature designed in [BOOKING_DESIGN.md](BOOKING_DESIGN.md).
Read that first — it explains *why*. This file is *what to do, in what order*.

**Status legend:** `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked

**Last updated:** 2026-09-03 — **Phases 1–5 foundations built and tested**
(984 tests passing). See [PHASES.md](PHASES.md) for the one-page overview.

**Phase 0 (recon) is still open and now blocks the booking runner.** The booking
configs describe pages nobody has opened in a browser; they ship `enabled: false`.

---

## How to use this file

1. Work phases **in order**. Each ends somewhere shippable and testable.
2. **Phase 0 gates everything.** Its three answers change the design of phases
   4–7. Do not write booking code before it is done.
3. Tick tasks as you go and update *Last updated*. Add discovered work rather
   than silently doing it — the next session reads this file, not your memory.
4. Anything that must survive across sessions goes in [MEMORY.md](MEMORY.md).

**Two rules that override convenience:**

- Never let a run book the wrong person. Ambiguity → abort, alert, stop.
- Never write browser-driving code for a page you have not first captured as a
  fixture. Every live run costs an account and possibly a client's slot.

---

## Phase 0 — Recon 🟢 MOSTLY ANSWERED (2026-09-02)

- [x] **Q1: does an unpaid selected slot HOLD or BURN?** → **NEITHER.** There is
      no reservation. The slot stays in the public pool while the 5–6 steps are
      walked, and the booking is confirmed only when payment completes.
      Abandoning mid-flow is **safe** — nothing consumed, the client keeps their
      waitlist entry and invitation — but a competitor may take the slot at any
      moment. Speed is a **performance** constraint, not a safety one.
- [x] **Q2: does the dashboard show the reference number?** → **YES**, as
      "Group Reference Number - GRC127086415238", and it is the **same value**
      the confirmation email calls the "Unique Reference Number". Identity
      resolution is therefore **exact**, not fuzzy.
- [x] **Q3: does the dashboard row show the applicant name?** → **YES**, under
      "Applicants:". Confirmed by screenshot.
- [x] **Q5: which mailbox receives the invitation?** → the VFS **account**
      address, for ITA, GRC and NLD.

**The real flow, from the user's manual walk:**

```
Book Now  →  5–6 steps  →  payment (NEW WINDOW/TAB)  →  confirmed
             select date · select payment method · skip optional pages
```

### Built to support the recon

- [x] **`src/booking/probe.py` + `python -m src.booking probe`** — READ-ONLY.
      Logs in, reads the dashboard, matches a client to a row, stops. Clicks
      nothing. `--keep-open` leaves the authenticated session up so the pages
      after "Book Now" can be captured by hand.
- [x] **`_login_and_reach_dashboard()`** in `src/waitlist/runner.py`. The probe
      was reusing registration's helper, which clicks "Start New Booking" and
      lands one page PAST the dashboard, then navigated back — a wasted page
      load of metered proxy. Both now wrap `_login(reach_booking=...)`;
      registration and slot-check paths untouched.
- [x] Empty-result diagnosis: "0 cards" means either an empty account or a wrong
      selector, which need opposite fixes. The probe reads the page's own
      wording to say which.

**Live run 2026-09-03 (AE-GRC, `mufaddal@travnook.com`):** login ✅, reached
`/grc/dashboard` ✅, 0 cards — **correct**, that account holds no Greek
applications. Card parsing is proven by 23 offline tests against the real
screenshot text; it has simply never met a live card.

- [ ] **Re-run the probe against an account that HAS a waitlist entry** — the
      only way to verify the card container selector. Try `-dc CHE`, where the
      journal shows real registrations.

### Switzerland: the first route built from REAL DOM (2026-09-03)

- [x] **`config/booking/AE-CHE.json`** — every selector captured from a live
      invited account (`switzerland_booking/switzerland.html`), not guessed.
      Notable: Switzerland skips Appointment Details entirely ("Book Now" goes
      straight to the calendar), and the confirmation step is REMOVED rather
      than guessed — a guessed step at the END of a flow would read some other
      page as success.
- [x] **`src/booking/walk.py`** — picks a date and a time, continues through
      `/services`. Refuses to submit the committing step. 20 tests asserted
      against the real captured HTML.
- [x] **`probe --walk [--to STEP]`** — clicks "Book Now" and walks. Reversible:
      the slot is never held, so abandoning costs only the attempt.

**⚠️ `date-availiable` — THREE i's.** VFS's own typo in their markup. Matching
the correct spelling finds nothing and the run reports no slots while the page
shows four. A test guards the config against a well-meaning "fix".

**Other real selectors:** `data-date="YYYY-MM-DD"` on each cell (exact, far
better than day numbers); slot times in `td[id^='tv']`; the radio is
`tabindex=-1` so the LABEL must be clicked; Continue is `disabled` until both a
date and a time are chosen.

**Live run 2026-09-03 (AE-CHE, `mufaddal@travnook.com`):** login ✅ including
Turnstile, dashboard ✅, **0 cards — correct.** That account's CHE entries
(`SWDB80350743950`) were cancelled; the "Waitlist cancellation" emails found on
2026-09-02 name exactly that reference. The walk's safety guard then refused to
guess which row to open, as designed.

- [ ] **Walk needs an account with a LIVE invited entry.** The calendar and slot
      selectors are proven against captured HTML but have never been driven on a
      live page.
- [ ] **Capture the pages after `/che/services`.** The user reports 5-6 steps;
      two are known. Then move `commits` to the payment step and enable.

---

### The form email must match the account (2026-09-03)

- [x] **`validate.check_invitation_email()`** + shown by `python -m src.waitlist
      check`. VFS sends the invitation to the email typed on `/your-details`,
      but the watcher reads ACCOUNT mailboxes — a mismatch means the invitation
      is never seen and the 36-48h window closes silently. 14 tests.
- [ ] **Fix the 3 flagged clients** (`ahmed-nld`, `test-che`,
      `trav-nook-ae-cze-cb9115`) — set their `email` to their account address,
      or accept checking those inboxes by hand.

---

### Still outstanding

- [ ] **THE BLOCKER: HTML selectors for the 5–6 pages after `Book Now`.**
      The user is supplying them. `config/booking/_default.json` currently has
      four guessed steps and **no payment step** — it is wrong in shape as well
      as in selectors, so rewrite it against the real thing rather than patching.
- [ ] Save HTML fixtures → `tests/fixtures/pages/` as the selectors arrive
- [ ] Q4 (nice to have): does the dashboard row carry an id in the DOM even when
      not displayed? Less important now that the visible reference is exact.

> ⚠️ **Retracted claim.** An earlier version of this file said the slot burns and
> that booking had to be atomic through payment. That is **wrong** — nothing is
> consumed before payment. `_default.json`'s `commits` flag sits on `select_slot`
> only because validation requires exactly one committing step and the payment
> step does not exist yet. **Move it to payment when that step is added.**

---

## Phase 1 — Mailbox watcher, observational only 🟢 **BUILT**

Triggers nothing. Builds the multi-country email corpus that makes every later
matcher evidence-based instead of guesswork.

**New:** `src/inbox/`

- [x] `matcher.py` — declarative classify + extract. **PURE FUNCTION** over an
      email dict. No IMAP, no I/O.
- [x] `config.py` — `extends` + merge-by-name + load-time validation, mirroring
      `waitlist/config.py`
- [x] `seen.py` — durable UID state (`state/inbox_seen.json`), restart-safe,
      atomic write, UIDVALIDITY reset detection
- [x] `watcher.py` — multi-mailbox IMAP loop, **read-only** (never marks mail
      seen). Reuses `otp_email.py`'s parsing approach without extending it.
- [x] `report.py` — digest, redacted, to log + Telegram summary channel
- [x] `__main__.py` — `check | status | test | watch | reconcile`
- [x] `config/inbox/_default.json` + `config/inbox/AE-ITA.json` (from the real
      emails in BOOKING_DESIGN §2.1)
- [x] `[inbox]` settings section + `config/config.ini` documentation
- [x] **Tests — 170 across 6 files, 85% line coverage of `src/inbox/`**

  | File | Tests | Covers |
  |---|---|---|
  | `test_inbox_matcher.py` | 30 | classification, extraction, ordering, validation |
  | `test_inbox_seen.py` | 23 | high-water + tail, gaps, UIDVALIDITY, persistence |
  | `test_inbox_watcher.py` | 43 | MIME parsing, IMAP failure handling, `run_pass` |
  | `test_inbox_config.py` | 21 | `extends`, merge-by-name, fail-loud contract |
  | `test_inbox_report.py` | 21 | digest sections, the 48h clock, **redaction** |
  | `test_inbox_cli.py` | 18 | every command, exit codes, dry-run safety |

  Per-module coverage: `config` 91%, `report` 92%, `reconcile` 91%, `seen` 89%,
  `watcher` 82%, `__main__` 83%, `matcher` 79%.

- [x] **`[otp] imap_host` configured** — `mail.travnook.com`, verified
      connecting. The watcher now runs against real mailboxes.
- [x] `config/inbox/AE-GRC.json` + `AE-NLD.json` — **3 countries configured**
- [ ] **Run observationally for 2+ weeks. Collect wording for every country.**

**Definition of done:** the watcher has seen and correctly classified real
invitation + confirmation emails from at least 3 countries. *(Italy, Greece and
Netherlands are configured from real emails the user supplied, and verified
against saved fixtures. No INVITATION has yet been read live from a mailbox —
the sample accounts hold none. Appointment-confirmed and cancellation emails
HAVE been read live.)*

**Audit note (2026-09-01):** an initial claim of "58 tests passing" was true but
misleading — coverage was 33%, with `seen.py` at 8% and `report.py`/`__main__.py`
at 0%. The table above is the state after closing those gaps. Untested paths
remaining are defensive branches (unreachable-in-practice `except` clauses) and
the `watch()` sleep loop.

**Decision made during build:** no IMAP IDLE. It means holding a connection open
per mailbox and reconnecting on every server timeout — real complexity bought
for latency that does not matter against a 48-hour deadline. Revisit only if
that deadline shrinks.

---

## Phase 2 — Confirmation reconciliation 🟢 **BUILT**

Standalone value — worth shipping even if booking never happens.

- [x] Parse `Successfully Added to Waitlist` → reference + applicant name
- [x] Match to journal rows lacking `vfs_reference`; backfill it
- [x] Auto-resolve `UNKNOWN`/`PENDING` rows where the email proves it landed
- [x] Conservative name matching — exact on normalised form, **ambiguity
      resolves nothing**
- [x] Dry-run by default; `--apply` to write
- [x] Append-only writes, preserving the audit trail
- [x] `tests/test_inbox_reconcile.py` — 28 tests, mostly about *refusing*
- [ ] Telegram when a dangling row resolves itself *(currently logged only)*

**Why now:** today a submit that loses the page journals `UNKNOWN` and blocks
that triple until a human checks the portal. This removes most of that toil.

**Bug found and fixed during build:** `normalise_name` spaced apostrophes,
splitting `O'BRIEN` into two tokens so it never matched `OBrien`. Apostrophes
are now deleted; hyphens still split (`AL-FARSI` → `al farsi`), because portals
render those inconsistently. See the test for the reasoning.

---

## Phases 3–5 — status after 2026-09-02

Built as **new modules under `src/booking/`**, leaving the existing waitlist code
untouched. That was a deliberate change of approach: the original plan modified
`journal.py` / `result.py` / `guards.py` in place, but nine call sites across the
API, waitlist and inbox depend on them, and the registration half must keep
working regardless. Extending beside them costs one extra import and removes the
whole blast radius.

- [x] **Phase 3 — lifecycle** (`src/booking/lifecycle.py`, 47 tests). States,
      transitions, the two independent commit boundaries, deadline arithmetic.
      Pure functions.
- [x] **Phase 4 — identity** (`src/booking/identity.py`, 45 tests). Name
      normalisation, confidence scoring, resolve-or-refuse, click-then-verify.
      Pure functions.
- [x] **Phase 5 — booking config** (`src/booking/config.py`, 28 tests, plus
      `config/booking/*.json`). Step types, inheritance, load-time validation of
      the commit boundary.
- [x] `src/booking/errors.py` — taxonomy split at the commit boundary.

**Still to do for these phases:**

- [ ] **SQLite migration.** Deferred deliberately: the JSONL journal's
      single-writer invariant still holds (the inbox watcher only reads it), so
      the migration is not yet forced. It becomes necessary when the booking
      runner writes concurrently with a scheduled watcher.
- [ ] **Wire the lifecycle into the journal.** The states exist and are tested
      but nothing writes them yet — that arrives with the runner.
- [ ] **The booking runner** (`src/booking/runner.py`) and step handlers.
      **Blocked on Phase 0** — see below.

**Bug found and fixed during build:** booking steps merge by name, but a shallow
merge replaced nested blocks wholesale. Greece narrowing `row.reference_pattern`
silently dropped the inherited `row.container` and `row.open`, leaving a step
that could not find or click anything. `_merge_step` now merges one level deep.

---

## Phase 3 (original plan) — Journal lifecycle + SQLite 🟡

The one genuinely fiddly refactor. `journal.py`'s docstring predicted it.

- [ ] Design the schema; `UNIQUE` index makes dedup a guarantee, not a hope
- [ ] Migrate `state/waitlist_journal.jsonl` → `state/bookings.db` (import as
      history, do not discard). **Note real rows are not uniformly populated —
      one has `account: ""` and a hand-written `WL-77231` in `reason`.**
- [ ] Lifecycle states: `registered → waiting → invited → booking → booked`,
      plus `expired` and `booking_unknown`
- [ ] **Split the commit boundary per phase.** `Status.COMMITTED_STATES`
      currently means one thing; it must now answer "spoken for *for which
      phase*". Registration and booking commit independently on one row.
- [ ] Add `account_id` (salted hash) for joining — keep the mask for display.
      Do **not** store plaintext addresses.
- [ ] `invite_expires_at` (email received + `validity_hours`)
- [ ] Keep the public API narrow: `append` / `blocking_entry` / `dangling` /
      `update_status`
- [ ] `tests/test_journal_lifecycle.py` — transitions, dedup, dangling gate

**Why SQLite now:** the watcher is a *third* writer and is long-lived, so it
cannot take the global run lock. That breaks the single-writer invariant the
JSONL design rests on.

---

## Phase 4 — Identity resolution 🟡

No browser. Pure functions against Phase 0's fixtures. **This is the code that
must not be wrong.**

- [ ] `src/booking/identity.py` — normalisation: strip diacritics, drop
      honorifics, drop punctuation, **sort tokens**, optional middle-name
      tolerance. PURE.
- [ ] Confidence scoring: `EXACT` / `STRONG` / `WEAK` / `NONE`
- [ ] `src/booking/resolve.py` — the 4-step ladder (journal → email → dashboard
      → detail page). PURE, given inputs.
- [ ] **`require_unique_match` as a hard code path.** Two equal candidates →
      book neither. Not configurable away.
- [ ] `config/booking/_default.json` `identity` block
- [ ] `tests/test_identity.py` — table-driven, including the adversarial cases:
      two clients same surname; name order reversed; middle name on one side;
      accented spellings
- [ ] `tests/test_booking_resolve.py` — ambiguity **must** abort

---

## Phase 5 — Dashboard resume, read-only 🟡

First real runs. Zero mutation on VFS.

- [ ] `src/booking/config.py` — mirrors `waitlist/config.py` (`extends`,
      merge-by-name, validation)
- [ ] `src/booking/steps/dashboard_resume.py` — locate + open the application
- [ ] `src/booking/steps/identity_assert.py` — verify, `on_mismatch: abort`
- [ ] `src/booking/runner.py` — mirrors `waitlist/runner.py`: Chrome, login
      gauntlet, **no retries**, takes the global run lock
- [ ] Log in → find → open → assert → screenshot → **stop**
- [ ] Extend `doctor.py` to validate booking configs (`--walk`)
- [ ] Verify against ≥2 countries

---

## Phase 6 — Booking steps up to the commit boundary 🟡

- [ ] Walk every form page up to (not including) `slot_pick`
- [ ] Reuse `fields.py` widgets; add per-route field configs
- [ ] `config/booking/AE-ITA.json` (and the second recon country)
- [ ] Stop before the committing step; screenshot; journal `booking` state
- [ ] Confirm every selector against production

**Definition of done:** the bot reliably reaches the slot picker for 2 countries
and stops there, with nothing committed.

---

## Phase 7 — Slot pick (THE COMMITTING STEP) 🔴

**Shape depends entirely on Phase 0 Q1.** Do not start before it is answered.

- [ ] Calendar widget in `fields.py` (month nav, availability, disabled dates)
- [ ] `src/booking/steps/slot_pick.py`
- [ ] Selection strategy: `earliest` / `date_window` / `preferred_days`
- [ ] Write-ahead journal + fsync before the committing click (mirror
      `register.py` exactly)
- [ ] `WaitlistCommittedError` equivalent — **never** a `RetryableError`
- [ ] Unconditional screenshots around the commit
- [ ] `SlotGoneError` as a **normal, expected, non-alarming** outcome
- [ ] Guards: `booking.enabled` (default off), per-day caps, dry-run

---

## Phase 8 — Payment 🔴

Deliberately separate. Fully automatic per the user's decision, but built and
gated independently of everything above.

- [ ] Scope after Phase 7 is proven
- [ ] Independent kill switch, default off
- [ ] Amount assertion against an expected fee — abort on mismatch
- [ ] Never retry a submit; ambiguous outcome → journal `unknown`, alert, stop
- [ ] Separate verification path that re-reads the account's booking list

---

## Phase 9 — Autonomy 🟡

- [ ] `src/booking/queue.py` — invitation work queue, paced and capped
- [ ] Watcher fires the queue (like `autotrigger.py` does for registration)
- [ ] **Serialise N invited clients on one account into ONE session** — never N
      logins
- [ ] Polling fallback: any `waiting` row's dashboard checked every `poll_hours`
      regardless of email
- [ ] Booking-specific account-health caps (read-only thresholds are wrong here)
- [ ] Expire `invited` rows past `invite_expires_at`; report, don't rot
- [ ] Outbound webhooks for booking events (reuse `utils/webhook.py`)
- [ ] API endpoints (mirror `src/api/clients.py`)

---

## Cross-cutting (do alongside, not after)

- [ ] `.gitignore`: `state/bookings.db`, `state/inbox_seen.json`,
      `config/inbox/*.local.json`
- [ ] Redaction: every new PII path through `waitlist/redaction.py`
- [ ] Re-baseline expected proxy MB for a booking run
- [ ] Update `ARCHITECTURE.md`, `GLOSSARY.md`, `COMMANDS.md`
- [x] ~~Fix `_merge_steps` shallow merge~~ — **not a real issue.** Verified in
      `waitlist/config.py`: it already merges field-by-field and supports
      `remove` / `before` / `after`. `src/inbox/config.py` mirrors it.
- [ ] Reuse `autotrigger.resolve_combo_label()` for email-category → combo
      matching; **do not re-derive it** (see the label trap)

---

## Discovered work (found by the first live pass, 2026-09-02)

- [x] **`appointment_confirmed` matcher** — found by a live pass on
      2026-09-03 over `mohammed@travnook.com`. VFS confirming a BOOKED
      appointment: "Your <Country> Visa Appointment is Confirmed!" carrying
      "Appointment Reference Group URN - GRC127086415238", plus a plainer
      "Appointment Confirmation" wording carrying "Unique Reference Number".
      Six seen across GRC/DEU/CZH/NOR.
      **`GRC127086415238 / MICHEL EL KHOURY` is exactly row [0] of the dashboard
      screenshot** — independent proof the email and dashboard references are the
      same value.
- [ ] **Consume it.** When the booking runner exists, this email settles a
      `booking_unknown` row without a human logging in — the booking-side
      counterpart to what `reconcile.py` already does for registrations.
      Classified `other` today because nothing reads it yet.
- [x] **`[inbox] mailboxes`** — watch a mailbox that has no client file (a
      shared inbox, or an account being trialled). `email:password`,
      comma-separated, in `config.local.ini`.
- [x] **`runner.shutdown()`** — Playwright's driver must disconnect BEFORE
      Chrome is killed, or Node reports an unhandled `EPIPE: broken pipe` after
      the run. All four call sites use it. 7 tests.

- [ ] **Act on "Waitlist cancellation" emails.** VFS mails when an entry is
      cancelled, naming the same reference the registration confirmation carries
      (`SWDB79923880977`). **It means a journal row marked `success` is no longer
      true** — that client is off the waitlist and needs re-registering. Three
      such emails are already in the mailboxes. Currently recognised and its
      reference captured, but **nothing acts on it**. Natural home: Phase 3's
      lifecycle (a `cancelled` state) plus a reconcile rule.
- [ ] **Fix or drop the Gmail waitlist account.** `na***@gmail.com` fails IMAP
      auth against `mail.travnook.com` — it is a Gmail address on a non-Gmail
      host. Config, not code; that mailbox is currently unwatched.

**Corrections the live pass forced** (both already applied):

- The real sender is `donotreply@vfshelpline.com` / `donotreply@vfsglobal.com`,
  **not** the `info.italyuae@` address printed in the email signature. The
  original `AE-ITA.json` pinned the signature address and would have matched
  nothing.
- Added `vfs_otp` matcher: 111 of 123 VFS emails are OTPs, and left in the
  catch-all they buried everything else in the digest.

---

## Decisions still open

Tracked here so no session silently picks one. See BOOKING_DESIGN §11.

| # | Question | Blocks | Answer |
|---|---|---|---|
| 1 | Unpaid slot: hold or burn? | Phase 7 | ✅ **Neither** — no hold, nothing consumed, risk is losing a race |
| 2 | Reference on dashboard? | Phase 4 | ✅ **Yes** — same value as the confirmation email |
| 3 | Applicant name on dashboard row? | Phase 4 | ✅ **Yes** — under "Applicants:" |
| 4 | Which mailbox gets the invitation? | Phase 1 | ✅ The VFS **account** address (ITA, GRC, NLD) |
| 5 | Force one client per account for booking routes? | Phase 4 | ⬜ undecided — much less pressing now identity is exact |
| 6 | **Selectors for the 5–6 post-`Book Now` pages** | **the runner** | ⏳ **user is supplying** |
| 7 | How does payment behave in a new window/tab? | payment | ⏳ deferred until earlier steps work |
