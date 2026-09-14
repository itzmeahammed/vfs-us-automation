# Post-Invitation Booking — Design Proposal

**Status:** proposal, not implemented. Written 2026-08-31.
**Scope:** what happens after VFS emails a waitlisted client "Slots available for
booking an appointment", up to (but excluding) payment.

> Companion to [ARCHITECTURE.md](ARCHITECTURE.md) (the slot checker) and
> `src/waitlist/__init__.py` (the registration half). This document assumes both.
> Task breakdown: [TASKS.md](TASKS.md). Cross-session context: [MEMORY.md](MEMORY.md).

---

## 1. The feature in one paragraph

A client is registered on a VFS waitlist (this already works — `src/waitlist/`).
Days later VFS emails the account: *"Appointment slots ... are now available for
booking ... This link will only be valid for 48 hours."* The link is the ordinary
portal login URL — no token, no deep link. Someone must then log in, find that
client's waitlisted application on the dashboard, open it, and complete the
remaining booking steps. This feature does that unattended, for any country,
driven entirely by configuration.

---

## 2. The three facts that drive the whole design

Everything below follows from these. They are stated up front because if any one
of them turns out to be wrong, the design changes.

### 2.1 The invitation email carries no identifier

Observed (AE-ITA):

```
Subject: Slots available for booking an appointment
Dear IRINA KONOVALOVA,
Appointment slots for your travel from UAE to Italy for the Tourist visa
visa category are now available for booking.
We request you to Click Here https://services.vfsglobal.com/are/en/ita/login
This link will only be valid for 48 hours ...
```

The link is `config/vfs_urls.ini`'s `AE-ITA` login URL. The only discriminators
are **the greeting name** and **the visa category**, both in free prose that will
be worded differently by every country.

**Consequence:** the email is a *trigger*, not an instruction. It says "something
on this (account, route) is bookable". Identity is resolved elsewhere.

### 2.2 The dashboard probably does not show the reference number

The registration confirmation gives a reference (`SWDB80350743950` for AE-CHE,
`ITD125298020335` for AE-ITA) and the journal stores it. But the dashboard list
of waitlisted applications likely does **not** display it.

**Consequence:** there is no exact key to join on. Identity resolution is fuzzy
text matching on the applicant name, which is safety-critical (see §4).

### 2.3 48 hours, not 48 seconds

This is emphatically **not** the slot-checker's race. There is time to queue,
to space logins out, to retry tomorrow, to ask a human.

**Consequence:** correctness beats latency everywhere. A design that is slower
but never books the wrong person is strictly better. Reacting to an email with an
immediate login storm across every client on an account is the *wrong* move — it
is exactly the pattern that gets accounts blocked.

---

## 3. What already exists (and is reused unchanged)

This feature is **not** a new engine. `src/waitlist/` already contains one:

| Existing piece | Reused for |
|---|---|
| `config.py` — `extends`, merge-steps-by-name, `commits` validation | the booking step list, verbatim |
| `fields.py` — 8-widget registry, label-based location, `if_present` | every form on every booking page |
| `context.py` — `{{placeholder\|modifier}}` resolution | every value typed into those forms |
| `register.py` — page gates, click ladder, `_await_enabled`, dwell | walking the booking pages |
| `journal.py` — write-ahead, fsync, dedup, dangling detection | the booking commit boundary |
| `guards.py` — kill switches, caps, per-day limits | booking-specific caps |
| `accounts.py` — account resolution, capacity, proxy pinning | unchanged |
| `runner.py` — Chrome launch, login gauntlet, no-retry policy | the booking run |
| `doctor.py --walk` | validating booking configs before a live run |
| `redaction.py`, `webhook.py`, `notify.py` | unchanged |

**The step engine is the asset.** A booking flow is a second `steps` array. The
work is in the *new* concerns: watching a mailbox, resolving identity safely, and
extending the journal into a longer lifecycle.

---

## 4. Identity resolution — the hard part

### 4.1 Why this is safety-critical

`accounts.py` supports several clients per account (`max_clients_per_account`,
`one_client_per_account_combo`). So this is reachable:

> Account `x@y.com` has Ahmed **and** Fatima waitlisted on AE-ITA / Tourist.
> The dashboard shows two rows differing only by applicant name.

Booking Fatima's slot under Ahmed's passport is unrecoverable, costs a real
client a real appointment, and may burn the account. **A missed 48-hour window is
a bad day; a mis-booking is a disaster.** The code must encode that asymmetry.

### 4.2 The resolution ladder

Four independent sources, none of which has to be reliable on its own:

```
1. JOURNAL     (account, route) -> candidate clients + their references
                 offline, free, no parsing. Narrows the field.

2. EMAIL       "Dear IRINA KONOVALOVA" -> narrows candidates further
                 a HINT. Per-country regex. May fail; that is tolerated.

3. DASHBOARD   rows matching a candidate by normalised name
                 ground truth for what exists.

4. DETAIL PAGE reference / passport read AFTER clicking the row
                 THE VERIFICATION. Converts a fuzzy match into a checked one.
```

Step 4 is the load-bearing idea and the thing the current architecture lacks:

> **You do not need to identify correctly on the first click, provided you can
> verify before committing.** Click, assert identity, back out if wrong.

Nothing on the detail page commits anything (the commit boundary is later, on the
slot-pick step). So a wrong click is free, as long as it is *detected*.

### 4.3 Name normalisation — a pure function

`journal._normalise()` already collapses whitespace and lowercases. Names need
more, in `src/booking/identity.py`:

- strip diacritics (accented forms → plain ASCII)
- drop honorifics (Mr, Mrs, Ms, Dr)
- drop punctuation and collapse whitespace
- **sort tokens** so `KONOVALOVA IRINA` == `IRINA KONOVALOVA`
- optionally ignore middle names when one side omits them

Pure, ~40 lines, fully testable without a browser. Highest leverage code in the
feature.

### 4.4 Confidence, not a boolean

```python
class Confidence:
    EXACT   = "exact"    # all normalised tokens equal
    STRONG  = "strong"   # surname + first initial, no other candidate matches
    WEAK    = "weak"     # partial / substring
    NONE    = "none"
```

Rules, in `config/booking/_default.json` so they are tunable per route:

```json
"identity": {
  "min_confidence": "exact",
  "require_unique_match": true,
  "verify_on_detail_page": true,
  "on_ambiguous": "abort"
}
```

**`require_unique_match` is the important one.** If two dashboard rows score
equally, book **neither** — screenshot, alert, stop. This must be a hard code
path, not a configurable-away convenience.

### 4.5 The pessimistic default

Design for "no reference anywhere". If the reference *does* turn out to be on the
detail page, `verify_on_detail_page` gets an exact assertion and everything gets
safer — the design only simplifies. Never the reverse.

### 4.6 The cheapest mitigation is configuration

If ambiguity exists only because clients share accounts, the fix is not code:
make `one_client_per_account_combo` the **default for any route with booking
enabled**. Costs more VFS accounts; removes the dangerous case entirely.

---

## 5. The mailbox watcher

### 5.1 Why a new module

`src/utils/otp_email.py` does IMAP already, but it is one-shot, called
synchronously mid-login, and hardcoded to OTP search text. The watcher is a
different shape: long-lived, multi-mailbox, multi-pattern, with durable
seen-state. Reuse the IMAP connection helpers; do not extend the OTP module.

### 5.2 Ship it observational first

**Phase 1 is a listener that triggers nothing.** It classifies every VFS email it
sees across all waitlist accounts, logs it, and Telegrams a digest. Run it for a
few weeks and you have a real corpus for *every* country you serve — instead of
designing twelve matchers from one Italian sample.

This mirrors how `config/waitlist/_default.json` was written: extracted from a
route proven end to end, "deliberately NOT written up front".

### 5.3 Config shape — `config/inbox/<ROUTE>.json`

```json
{
  "extends": "_default",
  "matchers": [
    {
      "name": "waitlist_invitation",
      "classify": "invitation",
      "subject_contains": ["Slots available for booking"],
      "from_contains": ["vfshelpline.com"],
      "body_contains": ["are now available for booking"],
      "extract": {
        "applicant_name": "Dear\\s+([A-Z][A-Z\\s-]+),",
        "category": "for the\\s+(.+?)\\s+visa category"
      },
      "validity_hours": 48
    },
    {
      "name": "waitlist_confirmation",
      "classify": "confirmation",
      "subject_contains": ["Successfully Added to Waitlist"],
      "extract": {
        "applicant_name": "Dear\\s+([A-Z][A-Z\\s-]+),",
        "reference": "Unique Reference Number is\\s+([A-Z0-9]+)"
      }
    }
  ]
}
```

Every extraction is **optional**. A matcher that classifies but extracts nothing
still fires the trigger — §4's ladder does not depend on it.

### 5.4 The confirmation matcher pays for itself immediately

`Successfully Added to Waitlist` carries the reference (`ITD125298020335`) and
the name. That is an **independent second source** for a datum the browser
sometimes fails to capture.

Today, a registration that submits and then loses the page is journalled
`UNKNOWN` and blocks that triple until a human checks the portal. With the
watcher, most of those resolve themselves. **This is worth building even if the
booking feature is never finished.**

### 5.5 Open question — whose mailbox?

Registrant files carry a client `email` distinct from the VFS `account`. Italy's
invitation went to the account holder. If some portal mails the *applicant*
address instead, the watcher needs client mailbox credentials, which you do not
have. Needs confirming per country; until then, watch account mailboxes only and
rely on §5.6 as the safety net.

### 5.6 Polling fallback — never depend on email alone

Any account holding a `waiting` journal row gets its dashboard checked every
`poll_hours` regardless of email. This covers: unparseable wording, mail
delivered elsewhere, a watcher that was down, spam filtering. With 48 hours of
slack, a 6-hourly poll is ample and costs little bandwidth.

**Email makes it fast; polling makes it correct.** Both, always.

---

## 6. Journal: one lifecycle, not two tools

### 6.1 The decision

A client's record moves through **one** state machine:

```
registered -> waiting -> invited -> booking -> booked
                            |          |
                            |          +-> booking_unknown  (needs a human)
                            +-> expired (48h lapsed)
```

Not a separate booking store. `journal.py` is already an append-only event log
keyed by `(route, combo, registrant_id)` whose readers "always take the LATEST
row per triple" — that *is* a state machine. Two stores could disagree about
whether a client is booked; reconciling them is a bug class not worth owning.

### 6.2 Two independent commit boundaries on one row

`Status.COMMITTED_STATES` currently means "this triple is spoken for". Once
`success` means "registered, awaiting invitation", the row carries **two**
separate commitments:

| Boundary | Set by | Means |
|---|---|---|
| registration | `review_pay` submit | on the waitlist; do not re-register |
| booking | booking commit step | appointment booked; do not re-book |

So `Status` splits per phase, and the dangling-entry gate must ask *which phase*
before blocking. Contained, but it is the one genuinely fiddly refactor.

### 6.3 Move to SQLite here

`journal.py`'s docstring already predicted this exact moment:

> *"the read-check-write in `blocking_entry()` is not atomic, so it is only safe
> while there is exactly ONE writer at a time ... If registration ever needs
> genuine PARALLELISM, swap this for SQLite."*

The watcher is a **third** writer (alongside supervisor and runner) and it is
long-lived — it cannot politely take the global run lock for hours. That breaks
the single-writer invariant the JSONL design rests on.

SQLite gives ACID, real locking, and dedup as a `UNIQUE` constraint rather than a
hope. The public API is already narrow (`append` / `blocking_entry` / `dangling` /
`update_status`) precisely so this swap touches one file.

**Migrate, don't discard:** existing JSONL rows import as history. Note the real
data is not uniformly populated — one row has `"account": ""` and a null
reference with a hand-written `WL-77231` in `reason`. Journal data is a **hint
that may be missing**, never a guaranteed key. Another reason the dashboard is
authoritative.

### 6.4 Account is stored masked

Rows carry `"account": "mu***@travnook.com"`. Good for redaction, but it cannot
be joined on. Add a stable non-reversible `account_id` (e.g. a salted hash) for
joining, keeping the mask for display. Do **not** start storing plaintext
addresses in the journal.

---

## 7. Booking flow — the step list

### 7.1 New step types

Three, on top of the existing form machinery:

| Type | Purpose | Commits? |
|---|---|---|
| `dashboard_resume` | find the waitlisted application, open it | no |
| `identity_assert` | verify the opened application is the right client | no |
| `slot_pick` | choose a date/time from the offered slots | **yes** |

`identity_assert` is new and load-bearing (§4.2 step 4). `slot_pick` is a genuine
new widget — a calendar, unlike anything `fields.py` handles today.

### 7.2 Shape — `config/booking/<ROUTE>.json`

```json
{
  "extends": "_default",
  "enabled": false,

  "steps": [
    {
      "name": "dashboard_resume",
      "type": "dashboard_resume",
      "url_contains": "dashboard",
      "row": {
        "container": "app-appointment-card, .application-card",
        "match_by": ["applicant_name", "category"],
        "open": { "role": "button", "name": "Continue booking" }
      }
    },
    {
      "name": "verify_identity",
      "type": "identity_assert",
      "assert_any_of": [
        { "field": "reference",       "value": "{{journal.vfs_reference}}" },
        { "field": "passport_number", "value": "{{passport_number}}" },
        { "field": "applicant_name",  "value": "{{first_name}} {{last_name}}" }
      ],
      "on_mismatch": "abort"
    },
    {
      "name": "appointment_details",
      "url_contains": "application-detail",
      "fields": [],
      "submit": { "role": "button", "name": "Continue" }
    },
    {
      "name": "select_slot",
      "type": "slot_pick",
      "commits": true,
      "strategy": "{{booking.slot_strategy}}",
      "date_window": {
        "from": "{{booking.date_from}}",
        "to":   "{{booking.date_to}}"
      }
    },
    {
      "name": "review_pay",
      "url_contains": "review-pay",
      "scroll_to_bottom": true,
      "fields": [],
      "submit": { "role": "button", "name": "Confirm" },
      "stop_before_submit": "{{booking.stop_before_payment}}"
    }
  ],

  "confirmation": {
    "url_contains": "confirmation",
    "success_text": ["appointment", "confirmed"],
    "reference_pattern": "\\b([A-Z]{2,4}\\d{9,})\\b"
  }
}
```

Note `assert_any_of` — with reference, passport and name as alternatives, the
config adapts to whatever a given portal actually shows without a code change.

### 7.3 `commits` moves

In registration, `review_pay` is the point of no return. In booking it is
**`slot_pick`** — that is where a slot is taken from the pool. `config.py`'s
"exactly one step must be marked commits" validation carries over unchanged;
only which step carries the flag differs.

### 7.4 ANSWERED (2026-09-02): there is no hold, and no burn either

> **The slot is NOT reserved while the flow is walked, and abandoning does not
> destroy it. It simply stays in the public pool, where somebody else may take
> it first.**

Confirmed by the user, who has walked the flow manually. The consequences run
the opposite way from the pessimistic reading:

| | |
|---|---|
| **Abandoning mid-flow is SAFE** | Nothing was consumed. The client keeps their waitlist entry and their invitation, and can retry while the window is open. |
| **`slot_gone` is the normal loss** | Losing a race to another applicant — exactly what `BookingStatus.SLOT_GONE` already models as retryable and non-alarming. |
| **Speed is a PERFORMANCE constraint** | Every second between opening the flow and completing payment is a second a competitor can take the slot. Not a safety constraint. |
| **Payment is still the only commit** | Nothing is confirmed until it completes, so a booking that stops short has no value — but it also does no harm. |

**What this retracts.** An earlier draft of this section read "if it burns,
booking must be atomic through payment" and proposed a separate `consumes` flag
for the slot pick. Both are withdrawn: nothing is consumed before payment, so
one commit boundary (payment) remains correct and `dry_run` stays meaningful up
to it.

**The real shape of the flow** (from the user's manual walk): after `Book Now`
there are **5–6 steps** — select a date, select a payment method, skip some
optional pages — and then payment, which **opens in a new window or tab**. Only
after payment completes is the booking confirmed.

---

## 8. Folder structure

Additive, following existing conventions:

```
src/
  inbox/                        NEW - the mailbox watcher
    __init__.py
    watcher.py                  long-lived IMAP loop (IDLE, poll fallback)
    matcher.py                  declarative classify + extract (PURE)
    seen.py                     durable UID state, restart-safe
    __main__.py                 python -m src.inbox watch|test|replay

  booking/                      NEW - the post-invitation flow
    __init__.py
    config.py                   config/booking/<ROUTE>.json (mirrors waitlist/config.py)
    identity.py                 name normalisation + confidence (PURE)
    resolve.py                  the §4.2 ladder (PURE, given inputs)
    steps/
      dashboard_resume.py
      identity_assert.py
      slot_pick.py
    runner.py                   browser orchestration (mirrors waitlist/runner.py)
    queue.py                    invitation work queue, paced + capped

  waitlist/
    journal.py                  MODIFIED - SQLite, lifecycle states, account_id
    result.py                   MODIFIED - booking statuses
    guards.py                   MODIFIED - booking caps and kill switches
    fields.py                   MODIFIED - + calendar widget

config/
  inbox/_default.json           NEW  shared matchers
  inbox/<ROUTE>.json            NEW  per-country email wording
  booking/_default.json         NEW  shared step skeleton
  booking/<ROUTE>.json          NEW  per-country booking steps

state/
  bookings.db                   NEW  SQLite (gitignored)
  inbox_seen.json               NEW  watcher UID state (gitignored)

tests/
  test_identity.py              NEW  normalisation + confidence + ambiguity
  test_inbox_matcher.py         NEW  against saved .eml fixtures
  test_booking_resolve.py       NEW  the ladder, table-driven
  test_journal_lifecycle.py     NEW  state transitions, dedup, dangling
  fixtures/emails/              NEW  real VFS emails, redacted
  fixtures/pages/               NEW  saved dashboard/detail HTML
```

**Testability discipline** (the codebase already holds this line —
`slot_check.cascade_steps()` is pure "so it's testable without Playwright"):
`identity.py`, `resolve.py`, `matcher.py` and the journal state machine must be
**pure functions over plain data**. They are the parts that must not be wrong,
and they are the parts you cannot iterate on against the live site — every real
run costs an account and possibly a client's slot.

---

## 9. Phasing

Each phase ends somewhere shippable. Phases 1–3 touch nothing on VFS.

| # | Phase | Deliverable | Risk |
|---|---|---|---|
| 0 | **Recon** | ✅ **Mostly answered** — no hold/no burn (§7.4), the dashboard shows both reference and name (§4.5). **Still needed: the HTML selectors for the 5–6 pages after `Book Now`.** The user is supplying them. | none |
| 1 | **Watcher, observational** | `src/inbox/`, classify + log + Telegram digest. Triggers nothing. Builds the multi-country email corpus. | none |
| 2 | **Confirmation reconciliation** | Use `Successfully Added to Waitlist` to fill missing references and auto-resolve `UNKNOWN` rows. Immediate standalone value. | low |
| 3 | **Journal lifecycle** | SQLite migration, lifecycle states, two commit boundaries, `account_id`. Pure-function tests. | low |
| 4 | **Identity resolution** | `identity.py` + `resolve.py` against saved fixtures. No browser. | low |
| 5 | **Dashboard resume, read-only** | Log in, find the application, open it, assert identity, screenshot, **stop**. Real runs, zero mutation. | low |
| 6 | **Booking steps to the boundary** | Walk the forms up to `slot_pick`, stop before it. Validates every selector against production. | low |
| 7 | **Slot pick** | The committing step. Shape depends entirely on Phase 0's hold-or-burn answer. | **high** |
| 8 | **Payment** | Separate effort, independently gated. | highest |
| 9 | **Autonomy** | Watcher triggers the queue; queue paces runs under existing caps. | medium |

Phases 1–6 are most of the work and none of it can book the wrong person.

---

## 10. Things that will bite

- **Slot gone between invitation and booking.** First-come-first-served, 48h
  window, many invitees. Needs to be a *normal, expected, non-alarming* outcome —
  not a failure in the summary.
- **Session expiry mid-flow.** Booking takes minutes. Post-commit, an expiry is a
  committed-state error, never a retry.
- **The 48h clock.** Store `invite_expires_at`; expire the row deliberately and
  report it rather than letting it rot in `invited`.
- **Login storms.** N clients invited on one account at once must be **serialised
  in one session**, not N logins. Account health is tuned for read-only checks;
  booking touches accounts far harder and likely needs its own caps.
- **The label trap, again.** `autotrigger.py` documents that `slot_check.result_label()`
  deliberately ignores the route file's `label`, which is what clients put in
  `combos[]`, and that a naive match silently matches nothing on AE-NLD. The same
  trap exists between an email's category prose and a combo label. Reuse
  `resolve_combo_label()`; do not re-derive it.
- **Bandwidth.** A booking run is far longer than a slot check. Re-baseline
  expected MB. `block_resource_types` stays empty (it breaks Turnstile).
- **Per-country divergence is larger here.** Registration steps are similar
  across portals; booking pages diverge more. Expect `_default.json` to carry
  less than it does for registration, and each route to override more. That is
  what the design is for.
- **`_merge_steps` shallow merge.** Overriding one key of a step restates the
  step. Tolerable for registration's 4 steps; worth a deep merge before booking
  configs grow.
- **ToS.** Registration already mutates VFS state; booking appointments and
  eventually paying is a further step again. A deliberate decision, not an
  accidental one.

---

## 11. Open questions

### Answered (2026-09-02)

1. ✅ **Hold or burn?** — **Neither.** No reservation; abandoning is safe; the
   risk is losing a race. See §7.4.
2. ✅ **Does the dashboard show the reference?** — **Yes**, as "Group Reference
   Number", and it is the SAME value the confirmation email calls the "Unique
   Reference Number". Identity resolution is therefore exact. See §4.5.
3. ✅ **Does the dashboard row show the applicant name?** — **Yes**, under
   "Applicants:". Confirmed by screenshot.
4. ✅ **Which mailbox receives the invitation?** — the VFS **account** address,
   for Italy, Greece and Netherlands. Unconfirmed for other countries.

### Still open

5. ⏳ **The HTML selectors for the 5–6 pages after `Book Now`.** The single
   remaining blocker for the booking runner. The user is supplying them.
6. ⏳ **How payment behaves in a new window/tab.** It opens separately; the
   handling is deliberately deferred until the earlier steps work.
7. ⬜ **Should booking-enabled routes force one client per account?** (§4.6) —
   much less pressing now that the reference makes identity exact, but still a
   cheap way to remove the ambiguous case entirely.
