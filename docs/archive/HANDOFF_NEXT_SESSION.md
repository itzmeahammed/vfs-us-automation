# Prompt for the next session

Copy everything between the lines into a fresh Claude Code session.

---

I'm building a VFS Global visa-appointment automation system. Read these first,
in order — they are current and were written for exactly this handoff:

1. `PHASES.md` — one-page overview of all phases and the workflow
2. `MEMORY.md` — confirmed facts, decisions already made, traps
3. `TASKS.md` — what's done and what's next
4. `keepInMind.md` — how to run and operate the inbox watcher

**Where things stand:** registration onto the waitlist works in production. The
email watcher (`src/inbox/`) is built and running against real mailboxes. The
booking foundations (`src/booking/` — lifecycle, identity, config loader) are
built and tested but drive nothing yet. **1155 tests pass, 0 failures**
(measured 2026-09-14; coverage: inbox 91%, waitlist 59%, booking 55%).

**Before you trust a green suite:** install `requirements-api.txt` AND `httpx`
and `pytest-cov`. Without them 56 API tests report as collection ERRORS, not
failures, and the suite reads green while they never run. That is exactly how a
422 regression in `src/api/clients.py` stayed hidden.

**There is already a read-only probe:** `python -m src.booking probe -sc AE -dc GRC
--email X --password Y --keep-open`. It logs in, reads the dashboard, matches a
client to a row, and stops — it clicks nothing. A live run on 2026-09-03 proved
login and dashboard navigation work; it found 0 cards because that account held
no applications on that route. Use it to verify selectors before writing anything.

**The next piece of work is the booking runner** — the code that clicks `Book Now`
and walks the pages after it.

It has been blocked on one thing: the HTML selectors for those 5–6 pages. I'm
supplying those now.

Before you write any of it, please confirm you've understood these, because they
were expensive to establish and are easy to get backwards:

- **The slot is NOT held and NOT burned.** It stays in the public pool while the
  flow is walked. Abandoning mid-flow is safe — nothing is consumed, the client
  keeps their waitlist entry and invitation. The risk is losing a race to another
  applicant, which is a *performance* concern, not a safety one. `slot_gone` is a
  normal, expected outcome.
- **Payment is the only real commit**, and it opens in a new window or tab.
  `config/booking/_default.json` currently puts `commits: true` on `select_slot`
  only because validation requires exactly one committing step and the payment
  step doesn't exist yet. Move it when you add payment.
- **Identity resolution is exact, not fuzzy.** The dashboard's "Group Reference
  Number" is the same value the registration confirmation email calls the
  "Unique Reference Number". Store it at registration, use it to pick the row.
  Names only narrow the shortlist.
- **Ambiguity aborts.** If two candidates match equally well, book neither.
  Booking the wrong client is unrecoverable; missing an invitation is a bad day.
- **`config/booking/_default.json` is a guess** — four steps, no payment step. It
  is wrong in shape as well as in selectors. Rewrite it against the real pages
  rather than patching it.

How I'd like you to work:

- Follow the existing conventions closely. `src/waitlist/` and `src/inbox/` are
  the house style: declarative JSON config per country, pure functions for
  anything that must be correct, comments that explain *why* rather than *what*.
- Test as you go, and tell me the **coverage number**, not just the test count.
  (Earlier in this project "58 tests passing" hid 33% coverage with two modules
  at zero. Don't let that happen again.)
- Don't enable any route until its flow has actually been walked.
- If something I tell you conflicts with what's in the docs, say so rather than
  silently picking one.

Here are the selectors: [PASTE THEM]

---

## If you're instead continuing without selectors

Swap the last paragraphs for one of these:

**To set up the scheduled watcher** (20 min, independent of everything else):

> Set up a Windows Task Scheduler entry running `python -m src.inbox watch --once`
> hourly, following the existing `run_task.ps1` / `setup_task.ps1` pattern used by
> the slot checker. The watcher currently only runs when invoked manually.

**To add a country's email config:**

> Here are real VFS emails from [COUNTRY]: [PASTE]. Add
> `config/inbox/AE-XXX.json` following the existing Greece and Italy configs.
> Note the validity window differs per country (36h for GRC/NLD, 48h for ITA) and
> the country discriminator is the portal URL in the body — without it, one
> country's email matches another's matcher and inherits the wrong deadline.

**To do the SQLite migration** (deferred, not urgent):

> Migrate `state/waitlist_journal.jsonl` to SQLite as described in TASKS.md
> Phase 3. Keep the public API of `src/waitlist/journal.py` unchanged — nine call
> sites across the API, waitlist and inbox depend on it. Import existing rows as
> history; note they are not uniformly populated (one has `account: ""` and a
> hand-written reference in `reason`).
