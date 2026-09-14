# MEMORY — Post-Invitation Booking

**Durable context for any session working on this feature.** Read this before
[TASKS.md](TASKS.md); read [BOOKING_DESIGN.md](BOOKING_DESIGN.md) for the full
reasoning.

**Last updated:** 2026-09-14 — Phases 1–5 foundations built
(`src/inbox/` + `src/booking/`, **1155 tests passing, measured**). Recon answered;
Switzerland's booking flow built from real DOM.
Start with [PHASES.md](PHASES.md) for the one-page picture.

> **Maintenance rule:** when a recon answer arrives or a design decision is made,
> record it here in the same commit as the code. This file is what stops the next
> session re-deriving — or worse, re-litigating — settled ground.

---

## Read these first, in this order

| File | Why |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The slot checker. The Cloudflare gates, account health, proxy pool — all reused. |
| `src/waitlist/__init__.py` | The registration half's docstring: the read-only / mutating split, and why. |
| `config/waitlist/_default.json` | The declarative step engine, extensively commented. **This is the pattern to follow.** |
| [BOOKING_DESIGN.md](BOOKING_DESIGN.md) | The design for this feature. |
| [TASKS.md](TASKS.md) | What to do next. |

---

## The feature, in three sentences

A client is registered on a VFS waitlist (already works). Days later VFS emails
the account "Slots available for booking an appointment" with a **48-hour**
window and a link that is just the ordinary portal login URL. This feature logs
in, finds that client's waitlisted application on the dashboard, and completes
the booking — unattended, for any country, driven entirely by config.

---

## Confirmed facts

### The two VFS emails (AE-ITA, real, 2026-08)

**Registration confirmation** — `Successfully Added to Waitlist`
- Carries `Dear <APPLICANT NAME>,` and `Your Unique Reference Number is ITD125298020335.`
- **Valuable:** an independent second source for the reference, which the browser
  sometimes fails to capture. Powers Phase 2.

**Invitation** — `Slots available for booking an appointment`
- `Dear <APPLICANT NAME>,` … `for the <CATEGORY> visa visa category` (the doubled
  word "visa visa" is in the real email — do not "fix" it in a matcher)
- Link: `https://services.vfsglobal.com/are/en/ita/login` — **the plain login URL**,
  already in `config/vfs_urls.ini`. **No token, no deep link.**
- `This link will only be valid for 48 hours`
- Sent to the **VFS account** address (confirmed for Italy only)

⚠️ **This is one country's wording.** Every other country will differ. That is
exactly why Phase 1 ships observational-only — to collect the real corpus before
writing matchers.

### The manual flow after an invitation

Log in normally → dashboard → click the waitlisted application → complete the
remaining steps. One dashboard row per waitlisted application. **The steps differ
per country** — hence fully declarative config.

### Reference number formats seen

| Route | Format | Source |
|---|---|---|
| AE-CHE | `SWDB79923880977`, `SWDB80350743950` | confirmation page → journal |
| AE-ITA | `ITD125298020335` | waitlist confirmation email |
| AE-GRC | `GRC127086415238` | dashboard **and** appointment-confirmed email |
| AE-DEU | `DEU72044101933` | appointment-confirmed email |
| AE-CZE | `CZH124459430502` | appointment-confirmed email |
| AE-NOR | `NOR126162488430` | appointment-confirmed email |
| AE-CHE | `WL-77231` | hand-verified, in `reason`, **not** in `vfs_reference` |

**Do not assume a single pattern.** The existing `reference_pattern` regex
`\b([A-Z]{4}\d{9,})\b` would not match `WL-77231`, and prefixes run 3–4 letters.

### The appointment-confirmed email (found live 2026-09-03)

VFS confirming a **booked appointment** — the end of the journey, and the natural
counterpart to the waitlist confirmation. Two wordings in one mailbox:

```
Subject: Your Greece Visa Appointment is Confirmed!
  Appointment Reference Group URN - GRC127086415238
  Dear MICHEL EL KHOURY Greetings from VFS Global.

Subject: Appointment Confirmation
  ... Unique Reference Number NOR126162488430 on 08-09-2026 at 11:45 ...
```

**Why it matters:** `GRC127086415238 / MICHEL EL KHOURY` is *exactly* row [0] of
the dashboard screenshot. Independent proof that VFS's mail and the dashboard
carry the same reference — the join the whole identity design rests on.

When the booking runner exists, this email settles a `booking_unknown` row
without a human logging in, exactly as `reconcile.py` already does for
registrations. Classified `other` for now because nothing consumes it yet.

**Three greeting shapes now, all real — assume nothing:**

| Email | Greeting |
|---|---|
| invitation / waitlist confirmation | `Dear NAME,` — comma |
| cancellation | `Dear NAME Your appointment...` — no comma |
| appointment confirmed | `Dear NAME Greetings from VFS Global` — no comma |

### Journal reality (`state/waitlist_journal.jsonl`, 12 rows)

- Append-only JSONL; readers take the **latest row per `(route, combo, registrant_id)`**
- `account` is stored **masked** (`mu***@travnook.com`) — **cannot be joined on**
- One row has `account: ""` and `vfs_reference: null` with `WL-77231` only in prose
- **Journal data is a hint that may be missing, never a guaranteed key**
- Only AE-CHE has real successes; AE-CZE has dry runs only

---

## Decisions made (do not re-litigate)

### 1. The email is a trigger, not an instruction

It says "something on this (account, route) is bookable". **The dashboard is
authoritative.** Every parsing rule not written is a per-country rule not
maintained. A missed/unparseable email degrades into the polling fallback.

### 2. Identity resolution is a four-source ladder with verification

```
journal (candidates) → email name (hint) → dashboard (ground truth)
                                         → detail page (VERIFY before commit)
```

**The key insight:** you don't need to identify correctly on the first click,
provided you verify before committing. Click, assert, back out if wrong. Nothing
on the detail page commits anything.

### 3. Ambiguity aborts. Always.

Several clients may share one VFS account (`max_clients_per_account`). Two rows
differing only by name is reachable. **A missed 48-hour window is a bad day;
booking the wrong person is a disaster.** `require_unique_match` is a hard code
path, never configurable away.

### 4. One lifecycle, not two tools

`registered → waiting → invited → booking → booked` on **one** journal row.
Rejected: a separate booking store. Two stores can disagree about whether a
client is booked; reconciling them is a bug class not worth owning.

Consequence: one row now carries **two independent commit boundaries**
(registration and booking). The dangling gate must ask *which phase*.

### 5. SQLite at Phase 3

The watcher is a **third** writer and long-lived, so it cannot take the global
run lock. That breaks the single-writer invariant JSONL rests on. `journal.py`'s
own docstring called this exact moment.

### 6. Ship the watcher observational first

Classify + log + Telegram, trigger nothing, for 2+ weeks. Mirrors how
`config/waitlist/_default.json` was written — from a proven route, "deliberately
NOT written up front". Designing 12 matchers from one Italian sample is guesswork.

### 7. Correctness over latency, everywhere

48 hours is not a race. Queue, pace, serialise. Reacting to an email with an
immediate login storm across every client on an account is exactly what gets
accounts blocked.

### 8. Payment is a separate, later, independently-gated effort

User's explicit decision: fully automatic, no human in the loop — but built after
booking is proven, behind its own kill switch.

---

## Recon answers (2026-09-02) — these settled the design

| # | Question | Answer |
|---|---|---|
| **1** | Does an unpaid slot HOLD or BURN? | **NEITHER.** No reservation at all. The slot stays in the public pool while the 5–6 steps are walked; the booking is confirmed only when payment completes. **Abandoning mid-flow is safe** — nothing consumed, the client keeps their waitlist entry and invitation — but a competitor may take it at any moment. |
| **2** | Reference on the dashboard? | **YES** — "Group Reference Number - GRC127086415238", and it is the **same value** the confirmation email calls the "Unique Reference Number". Identity resolution is **exact**. |
| **3** | Applicant name on the row? | **YES**, under "Applicants:". |
| 4 | Which mailbox gets the invitation? | The VFS **account** address (ITA, GRC, NLD confirmed). |

### The real booking flow

```
Book Now  →  5–6 steps  →  payment (NEW WINDOW/TAB)  →  confirmed
             select date · select payment method · skip optional pages
```

⚠️ **Retracted claim, do not reinstate.** An earlier draft said the slot "burns",
that stopping before payment was unsafe, and that booking had to be atomic
through payment. **All three are wrong.** Nothing is consumed before payment.
`config/booking/_default.json`'s `commits` flag sits on `select_slot` only
because validation requires exactly one and the payment step does not exist yet
— **move it to payment when that step is added.**

## Still open

| # | Question | Status |
|---|---|---|
| **6** | **Selectors for the 5–6 pages after `Book Now`** | ⏳ **THE BLOCKER** — user is supplying |
| 7 | Payment in a new window/tab — how to drive it | ⏳ deferred until earlier steps work |
| 5 | Force one client per account on booking routes? | ⬜ much less pressing now identity is exact |

---

## What is built (2026-09-01)

`src/inbox/` — the mailbox watcher and reconciler. **Observational: it reads
mail and the journal, and triggers nothing.** 58 tests, all offline.

```
matcher.py     PURE. email -> Match. Substring conditions + optional regex
               extraction. First match wins, so config order is load-bearing.
config.py      config/inbox/<ROUTE>.json, extends + merge-by-name (mirrors
               waitlist/config.py)
seen.py        durable UID high-water mark + tail, UIDVALIDITY reset detection
watcher.py     multi-mailbox IMAP, READ-ONLY (never marks mail seen)
report.py      redacted digest -> log + Telegram SUMMARY channel
reconcile.py   settles unknown/pending journal rows from confirmation emails
__main__.py    check | status | test | watch | reconcile
```

**Commands that need no network:** `check`, `test`. That is where matcher work
happens — save a country's real mail into `tests/fixtures/emails/` and iterate.

**Blocked on config:** `[otp] imap_host` is blank, so `watch` cannot run yet.
`status` resolves 4 real mailboxes from the registrant files, so the rest is
wired.

**Test state:** 170 tests, 85% line coverage of `src/inbox/`, all offline. Full
suite 806 passing. (The `fastapi` collection errors are pre-existing — that
package is in `requirements-api.txt` but not installed in `.venv`.)

**Measuring coverage here:** there is no `pytest-cov` in this venv. Use stdlib
`trace` rather than adding a dependency:

```python
tracer = trace.Trace(count=1, trace=0, ignoredirs=[sys.prefix])
tracer.runfunc(lambda: pytest.main([...]))
```

### Decisions taken while building

- **No IMAP IDLE.** A held-open connection per mailbox plus reconnect handling
  is real complexity, bought for latency that is irrelevant against a 48-hour
  deadline. Poll every 5 minutes instead. Revisit only if that deadline shrinks.
- **The digest goes to the SUMMARY Telegram channel, not the success channel.**
  The success channel's value is that it only fires when a bookable slot is
  found; mail digests would destroy that. Same reasoning as `[waitlist]
  telegram_enabled` defaulting off.
- **Only invitations push to Telegram.** Everything else sits in the log for
  whoever is reviewing matchers — a message per poll would train the reader to
  ignore the channel.
- **`reconcile` is dry-run by default.** It writes a *registered* record; a
  wrong one means a client is never retried and silently misses their
  appointment. `--apply` is explicit.
- **Reconciliation refuses ambiguity.** Exact match on the normalised name only
  — no initials, no substrings, no edit distance. Two clients matching one
  confirmation settles neither and says so.

### The EPIPE bug and `shutdown()`

Playwright's Node driver talks to Python over a pipe. `_login()` starts a
Playwright instance and stashes it on the bot (it must outlive that function),
but **nothing ever stopped it** — so killing Chrome left Node writing into a dead
socket and reporting an unhandled `EPIPE: broken pipe` *after* the run finished.

`runner.shutdown(bot, chrome)` fixes it, and **order is the whole fix**:

```
1. playwright.stop()   the driver disconnects cleanly
2. chrome.close()      the process tree goes
```

Both steps are independent — a driver that will not stop must never prevent
Chrome being killed, because a surviving Chrome holds the CDP port and the next
run cannot start. All four call sites (registration, doctor, probe) use it.
7 tests in `tests/test_runner_shutdown.py`.

**A bug inside the fix, worth remembering:** a bulk find-and-replace of
`chrome.close()` also replaced the one *inside* `shutdown()`, making it
infinitely recursive so Chrome was never killed. It surfaced only as a
`maximum recursion depth exceeded` warning that was easy to dismiss as test
noise. There is now a test asserting `chrome.close()` is reached exactly once.

### THE INVITATION GOES TO THE FORM EMAIL, NOT THE ACCOUNT

The user's find, 2026-09-03, and it invalidates an assumption the watcher was
built on.

VFS sends "Slots available for booking" to the **email typed into the waitlist
form** on `/your-details` — not to the VFS account the registration was made
under. Those are separate fields in a client file (`email` vs `account`) and
nothing ever forced them to agree.

The inbox watcher reads **account** mailboxes. So when they differ, the
invitation lands in a mailbox nobody watches, whose password we do not have, and
the 36-48h window closes **silently** — no error, no missing row, nothing to
notice.

**4 of the 5 real clients in this repo differ.** All would have missed theirs.

`validate.check_invitation_email()` now flags it, and `python -m src.waitlist
check` prints it per client. Two deliberate choices:

- **A WARNING, not an error.** Existing clients predate the rule; erroring would
  break working registrations to enforce a preference.
- **The client's data is never rewritten.** Silently setting `email = account`
  changes what the client supplied; the person who typed it deserves to be told
  rather than overruled.

Compared against the **resolved** account, not the raw field — that is what
caught `mufaddal-calcuttawala`, which has no `account` of its own but resolves to
the shared `[waitlist]` account and differs from *that*.

### Watching a mailbox with no client file

`[inbox] mailboxes = email:password, ...` in `config.local.ini`. Client files
remain the normal source; this is the escape hatch for a shared inbox or an
account being trialled. An explicit entry wins over the same address resolved
from a client file.

### Bug found by a test, worth remembering

`normalise_name` originally replaced all punctuation with a space, which split
`O'BRIEN` into `o brien` so it never matched `OBrien`. Apostrophes are now
deleted (they sit *inside* one name); hyphens still split (`AL-FARSI` →
`al farsi`) because portals render those inconsistently and either half may be
dropped. Relevant to UAE names specifically.

### Correction to an earlier claim

An earlier note said `_merge_steps` in `waitlist/config.py` was a shallow merge
needing fixing. **It is not** — it already merges field-by-field and supports
`remove` / `before` / `after`. `src/inbox/config.py` mirrors it deliberately.

---

## Traps — each already cost someone

### The label trap (documented in `autotrigger.py`)

`slot_check.result_label()` **deliberately ignores** the route file's `label`
field — which is exactly what clients put in `combos[]`. For AE-NLD they diverge
completely:

```
client combos[] : "Dubai - Tourist Visa"
result_label()  : "Netherlands Visa application center- Dubai - Tourist Visa - Tourist Purpose"
```

A naive match works for AE-CHE and silently matches **nothing** for AE-NLD —
looking exactly like "no clients waiting". Use `resolve_combo_label()`. The same
trap exists between an email's category prose and a combo label.

### Never enable `block_resource_types`

Blocking images/media/fonts shifts the Turnstile checkbox the coordinate-click
targets **and** makes the load pattern look suspicious. It is empty by design.

### The countdown gate

VFS gates save buttons behind a countdown. **The button enables BEFORE the timer
expires, but clicking early silently does nothing.** Hence `dwell_seconds` *and*
`_await_enabled` — belt and braces. Expect the same on booking pages.

### Volatile selectors

VFS's `<app-dynamic-form>` emits **no** `formcontrolname` and only positional ids
(`mat-input-3`) that shift when a field is added. **Address fields by their
visible label.** Ids on consent checkboxes are sometimes duplicated.

### `_merge_steps` is a shallow merge

Overriding one key of a step means restating the step. Fine for registration's 4
steps; fix before booking configs grow.

### Post-commit is never retryable

`WaitlistCommittedError` is deliberately **not** a `RetryableError` so the
supervisor's relaunch logic can never pick it up. Preserve this in booking.

---

## Architecture cheat-sheet

### Reused unchanged

`config.py` (extends + merge-by-name + `commits` validation) · `fields.py`
(8 widgets, label-based, `if_present`) · `context.py` (`{{placeholder|modifier}}`)
· `register.py` (page gates, click ladder, dwell) · `journal.py` (write-ahead,
fsync, dedup) · `guards.py` · `accounts.py` · `runner.py` · `doctor.py --walk` ·
`redaction.py` · `webhook.py` · `notify.py`

**The step engine already exists. Booking is a second `steps` array, not a new engine.**

### New modules — what exists TODAY

```
src/inbox/          ✅ BUILT, running   91% coverage (measured 2026-09-14)
  matcher.py        PURE  email -> classification
  config.py               config/inbox/<ROUTE>.json
  seen.py                 UID high-water mark, incremental scan
  watcher.py              multi-mailbox IMAP, READ-ONLY
  report.py               redacted digest
  reconcile.py            settles unknown journal rows from confirmations

src/booking/        🔶 FOUNDATIONS      121 tests, 87% coverage
  lifecycle.py      PURE  states, transitions, TWO commit boundaries
  identity.py       PURE  normalisation, confidence, resolve-or-refuse, verify
  config.py               config/booking/<ROUTE>.json, load-time validation
  errors.py               taxonomy split at the commit boundary
  probe.py          ✅    READ-ONLY dashboard reader — login proven live
  __main__.py             check | status | probe
  ⬜ runner.py            NOT WRITTEN — blocked on selectors
  ⬜ steps/               NOT WRITTEN — blocked on selectors
```

**The probe exists so the runner isn't written blind.** It logs in, reads the
dashboard, matches a client to a row, and stops — clicking nothing. That proves
login, dashboard navigation, card selectors and identity matching while the
booking pages are still unknown. `--keep-open` leaves the authenticated session
up so the pages after "Book Now" can be captured by hand.

**First live run (2026-09-03, AE-GRC, `mufaddal@travnook.com`):** login worked,
reached `/grc/dashboard`, found **0 cards — correctly**, because that account
holds no Greek applications. Card parsing is proven separately by 23 offline
tests against the real screenshot text.

**Two login helpers now, and the distinction matters:**

```python
_login_and_reach_dashboard(bot, url)          # existing applications  ← probe
_login_and_reach_appointment_page(bot, url)   # Start New Booking      ← registration
```

Both wrap `_login(bot, url, reach_booking=...)`. The probe originally used the
second, which lands one page PAST the dashboard and then had to navigate back —
a wasted page load of metered proxy traffic. Registration and slot-check paths
are unchanged.

**Design decision worth keeping:** `src/booking/` was built **beside** the
waitlist code, not by modifying it. The original plan changed `journal.py`,
`result.py` and `guards.py` in place, but **nine call sites** across the API,
waitlist and inbox depend on them and the registration half must keep working.
Extending beside costs one import and removes the whole blast radius.

**SQLite deferred, not dropped.** The journal's single-writer invariant still
holds — the watcher only READS it. It becomes necessary when the booking runner
writes while a scheduled watcher runs.

### The purity discipline

The codebase already holds this line — `slot_check.cascade_steps()` is pure "so
it's testable without Playwright". Apply it to `identity.py`, `resolve.py`,
`matcher.py` and the journal state machine. **They are the parts that must not be
wrong, and the parts you cannot iterate on against the live site** — every real
run costs an account and possibly a client's slot.

### Config layout

```
config/waitlist/<ROUTE>.json    WHERE things are (registration) — exists
config/registrants/<id>.json    ONE CLIENT: data + targeting — exists (PII, gitignored)
config/inbox/<ROUTE>.json       email wording per country — NEW
config/booking/<ROUTE>.json     booking steps per country — NEW
```

Route configs hold **structure**; registrant files hold **data**. Joined by field
name: a route writes `{{passport_number}}`, the client file supplies it. **That
split is what lets one route config serve every client — preserve it.**

---

## Commands

```powershell
# Existing
& .venv\Scripts\python.exe -m src.waitlist status
& .venv\Scripts\python.exe -m src.waitlist check --route AE-CHE
& .venv\Scripts\python.exe -m src.waitlist doctor --route AE-CHE --walk
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc ITA --keep-open   # recon
& .venv\Scripts\python.exe -m pytest -q

# Planned
& .venv\Scripts\python.exe -m src.inbox watch
& .venv\Scripts\python.exe -m src.inbox test --route AE-ITA
& .venv\Scripts\python.exe -m src.booking run --route AE-ITA --registrant <id> --dry-run
```
