# PHASES — the waitlist-to-appointment pipeline

One page. What each phase does, how the pieces fit, and what is actually done.

Detail lives elsewhere: [BOOKING_DESIGN.md](BOOKING_DESIGN.md) (why),
[TASKS.md](TASKS.md) (what next), [keepInMind.md](keepInMind.md) (how to run it).

---

## The whole workflow

```
        ┌─────────────────────────────────────────────────────────────┐
        │  CLIENT SIGNS UP                                            │
        └──────────────────────────┬──────────────────────────────────┘
                                   ▼
   ①  src/waitlist/    register them on the VFS waitlist       ✅ WORKS TODAY
                       └─ journal row: success + reference
                                   ▼
                       ┌───────────────────────┐
                       │  days or weeks pass   │
                       └───────────┬───────────┘
                                   ▼
   ②  VFS emails "Slots available for booking"   ── valid 36h (GRC, NLD)
                                   │                        48h (ITA)
                                   ▼
   ③  src/inbox/       see the email, know who it names      ✅ BUILT
                       └─ name + category + deadline
                                   ▼
   ④  src/booking/     log in, find their row, book it       🔶 FOUNDATIONS
                       └─ journal row: booked + reference
                                   ▼
   ⑤  payment                                                ⬜ NOT STARTED
```

**The join that makes it work.** The confirmation email's *Unique Reference
Number* is the same value the dashboard shows as *Group Reference Number*
(confirmed for Greece). So:

```
registration → store reference     invitation → names a client
                     └──────────────────┬──────────────┘
                                        ▼
                          dashboard row selected EXACTLY
```

Names only narrow the shortlist. The reference does the selecting. That is what
turns "probably the right person" into "provably the right person".

---

## Phase 1 — the mailbox watcher ✅

**`src/inbox/`** · 91% coverage · running against real mailboxes

**Job:** know when VFS says something happened, without anyone reading email.

```
IMAP, read-only  →  search since the last pass  →  skip what's been read
                 →  classify each new message   →  report
```

Classifies into: **invitation** (name, category, deadline), **confirmation**
(name, reference), **cancellation**, **otp**, **other**. Per-country rules live
in `config/inbox/<ROUTE>.json` — adding a country is one JSON file.

**It triggers nothing.** Reads mail, writes two local files. Deliberately: it is
collecting real per-country wording before anything acts on it.

| Design choice | Why |
|---|---|
| Read-only IMAP | A human opening the mailbox finds it as VFS left it |
| Time narrows, UIDs decide | Clocks skew and mail arrives out of order — a timestamp is an unsafe test |
| One bad mailbox never stops the others | Your Gmail account fails auth; the other three read fine |

**Live run found:** 312 messages, 123 VFS. Corrected four wrong assumptions —
the real sender is `donotreply@` not the signature address; 36h ≠ 48h across
countries; a catch-all was shadowing other routes; OTPs (111 of 123) were
burying the digest.

---

## Phase 2 — reconciliation ✅

**`src/inbox/reconcile.py`** · dry-run by default

**Job:** answer the question a human currently logs into VFS to answer.

A registration that submits then loses the page journals `unknown` and blocks
that client until someone checks the portal. But VFS already emailed the answer.

```
confirmation email  →  match name to a client (exact, normalised)
                    →  unknown/pending → success + reference
                    →  success w/o ref → backfill
                    →  append, never rewrite
```

**Refuses rather than guesses.** Exact match only. If one confirmation could
belong to two clients it settles **neither**. A wrong reconciliation marks a
client registered when they aren't — they are then never retried.

*Works but unexercised: no confirmations have arrived and no rows are dangling.*

---

## Phase 3 — the lifecycle 🔶

**`src/booking/lifecycle.py`** · 47 tests · pure functions

**Job:** one record for the whole journey, not two stores that can disagree.

```
registered ──▶ waiting ──▶ invited ──▶ booking ──▶ booked
                              │           │
                              │           ├─▶ booking_unknown   needs a human
                              │           └─▶ slot_gone         normal! retry
                              ├─▶ expired      window lapsed
                              └─▶ cancelled    entry cancelled at VFS
```

**The subtle part — two commit boundaries on one row:**

```python
is_committed("success", PHASE_REGISTRATION)  # True   already waitlisted
is_committed("success", PHASE_BOOKING)       # False  but NOT booked
```

A client sits registration-committed for weeks while not booking-committed at
all. One flag would have to pick one meaning and be wrong about the other.

**What the machine refuses** is the interesting half: `booked` is terminal;
`invited` cannot jump to `booked` (it must pass through `booking` so the
write-ahead marker is always on disk first); `booking_unknown` is human-only.

`slot_gone` is **not a failure** — first-come-first-served with many invitees
makes it expected, and it must read as normal so real failures stay visible.

---

## Phase 4 — identity 🔶

**`src/booking/identity.py`** · 45 tests · pure functions

**Job:** decide which client a row belongs to — the decision that must not be wrong.

> Booking one client's appointment under another's passport is unrecoverable and
> costs a real person a real slot. Missing an invitation is a bad day.
> **The code encodes that asymmetry: when in doubt, do nothing.**

```
reference match?  →  EXACT, short-circuits — nothing else considered
       else
name match?       →  scored: exact / strong / weak / none
       └─ two candidates tie?  →  RESOLVE NEITHER
```

Then **click-then-check**: opening a dashboard row commits nothing, so a wrong
click is free *provided it is detected*. `verify()` is that detection — any one
agreeing field passes, but a field present on both sides that **disagrees** is
fatal. Silence tolerated; contradiction never.

Normalisation handles the real cases: token order (`KONOVALOVA IRINA` =
`IRINA KONOVALOVA`), diacritics, honorifics, and apostrophes-deleted-but-
hyphens-split (`O'BRIEN` = `OBrien`, `AL-FARSI` = `Al Farsi`).

---

## Phase 5 — the booking flow 🔶

**`src/booking/config.py`** · 28 tests · `config/booking/<ROUTE>.json`

**Job:** describe each country's booking pages as data, not code.

Same engine shape as the waitlist configs — `extends`, merge-by-name,
load-time validation — plus step **types**, because booking has genuinely
different kinds of page:

| Type | Does | Commits? |
|---|---|---|
| `dashboard_resume` | find the waitlisted row, open it | no |
| `identity_assert` | verify it's the right client | no |
| `form` | fill fields, submit | no |
| `slot_pick` | choose a date — **the point of no return** | **yes** |
| `confirm` | read the reference | no |

**Validation refuses** at load time, before a browser exists: no committing
step, two committing steps, a non-committable type marked `commits`, or an
`identity_assert` placed *after* the commit (it would verify nothing that could
still be undone).

**Nested blocks merge one level deep.** A real bug: Greece narrowing
`row.reference_pattern` silently dropped the inherited `row.container` and
`row.open`, leaving a step that could not find or click anything.

---

## Where things actually stand

| Phase | Code | Proven against reality |
|---|---|---|
| ① registration | ✅ works | ✅ real registrations |
| ③ watcher | ✅ built | 🔶 3 countries configured, no live invitation seen yet |
| ② reconcile | ✅ built | ⬜ nothing to reconcile yet |
| ③ lifecycle | ✅ built | ⬜ not yet driving anything |
| ④ identity | ✅ built | 🔶 dashboard format from a screenshot |
| ⑤ booking config | 🔶 skeleton | ⬜ **pages never walked** |
| ⑤ booking probe | ✅ built | 🔶 login + dashboard proven live; cards untested (no account had any) |
| ⑤ booking runner | ⬜ | ⬜ |
| ⑥ payment | ⬜ | ⬜ |

**All booking routes are `enabled: false`.** Nothing can run.

### The probe — proving login + dashboard before writing the runner

`src/booking/probe.py` · `python -m src.booking probe -sc AE -dc GRC`

**READ-ONLY. It logs in, reads the dashboard, reports, and stops.** It does not
click "Book Now", submit anything, or change VFS state. `--keep-open` leaves the
authenticated session up so the pages after that button can be inspected by hand
— which is how the missing selectors get captured.

Built before the runner because it isolates the risk: login, dashboard
navigation, card selectors and identity matching can all be proven while the
booking pages are still unknown.

**First live run (2026-09-03, AE-GRC):** logged in successfully, reached
`/grc/dashboard`, found 0 cards. **Correct** — that account holds no Greek
applications. The card *parsing* is separately proven by 23 offline tests against
the exact text from the dashboard screenshot.

Two fixes came out of that run:

- **`_login_and_reach_dashboard()`** — the probe was reusing registration's login
  helper, which clicks "Start New Booking" and lands on Appointment Details, one
  page *past* the dashboard. It then navigated back. Now `_login()` takes a
  `reach_booking` flag; the registration and slot-check paths are untouched.
- **Empty-result diagnosis** — "0 cards" has two causes needing opposite fixes
  (an empty account vs. a wrong selector). The probe now reads the page's own
  wording to say which, instead of leaving it to guesswork.

### The one thing blocking the rest

**The HTML selectors for the 5–6 pages after `Book Now`.** The user is supplying
them. `config/booking/_default.json` currently describes those pages from
guesswork — it has four steps and no payment step, so it is wrong in *shape* as
well as in selectors. It should be rewritten against the real thing, not patched.

Everything else the recon needed is now answered:

| Question | Answer |
|---|---|
| Does an unpaid slot hold or burn? | **Neither.** No reservation. Abandoning is safe; the risk is losing a race. |
| Does the dashboard show the reference? | **Yes** — and it is the same value the confirmation email carries. |
| Does the dashboard row show the name? | **Yes**, under "Applicants:". |
| Which mailbox gets the invitation? | The VFS **account** address (ITA, GRC, NLD). |

### What the real flow looks like

From the user walking it manually:

```
Book Now  →  5–6 steps  →  payment (NEW WINDOW/TAB)  →  confirmed
             select date
             select payment method
             skip some optional pages
```

**No hold.** The slot stays in the public pool the whole time — someone else can
take it at any point. Three consequences:

- **Abandoning mid-flow is safe.** Nothing is consumed. The client keeps their
  waitlist entry and invitation, and can retry while the window is open.
- **`slot_gone` is a lost race**, not damage — exactly what the lifecycle already
  models as normal and retryable.
- **Speed is a performance concern, not a safety one.**

**Payment stays the only commit**, and it is on the critical path: a booking that
stops short is worthless, though it is also harmless.

> **Retracted:** an earlier draft said the slot "burns", that stopping before
> payment was unsafe, and that booking had to be atomic through payment. All
> three are wrong. `_default.json`'s `commits` flag currently sits on
> `select_slot` only because config validation requires exactly one and the
> payment step does not exist yet — **move it to payment when that step is added.**

---

## Commands

```powershell
# Watcher (Phase 1-2)
python -m src.inbox check                  # validate configs      offline
python -m src.inbox test                   # matchers vs fixtures  offline
python -m src.inbox status                 # config + liveness
python -m src.inbox watch --once           # one pass, read-only
python -m src.inbox reconcile              # dry run
python -m src.inbox reconcile --apply      # ← the only writing command

# Booking (Phase 3-5)
python -m src.booking check                # validate configs      offline
python -m src.booking status               # steps + commit point  offline
python -m src.booking probe -sc AE -dc GRC --email X --password Y
python -m src.booking probe -sc AE -dc GRC --email X --password Y --keep-open
#   ^ READ-ONLY: logs in, reads the dashboard, clicks nothing.
#     --keep-open leaves the session up to capture selectors by hand.

# Everything
python -m pytest -q
```

---

## Design rules that hold across every phase

1. **Ambiguity aborts.** Never guess which client.
2. **Exactly one commit boundary per flow**, enforced at config load.
3. **Post-commit is never retryable** — a submit that may have landed must never
   be replayed.
4. **Pure functions for anything that must not be wrong** — lifecycle, identity,
   matching. They hold no I/O, so they are tested exhaustively offline, including
   adversarial cases that are hard to produce live and unaffordable to get wrong.
5. **Configuration, not code.** A new country is a JSON file.
6. **Observe before acting.** Ship read-only, collect real data, then automate.
