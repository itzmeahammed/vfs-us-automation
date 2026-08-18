check this project, this project checks slot booking on vfsglobal website. right now it logs in, goes to dashboard and checks for each combination and then logs out.
the next big phase is to do exact work and after checking for a combination, if slots found,  continue to book the appointment, slot check is on step-1. i need to go to end till step-6, making a payment.

how can i move to next phase, discuess the architecture, design, folder structure, alll phases in this big upgrade, talk to about eveything i need to take into consideration. the project should be completely dynamic, configurable and easy to maintain and easy to make changes. noting hardcoded.



Phase 2: Slot-check → Full Booking (Steps 1–6 + Payment)
What you have today (and it's good)
Your codebase is already well-factored for this jump. The pieces that matter:

supervisor.py — per-route orchestration, fresh Chrome per attempt, account health, IP rotation, retry classification. This is your transaction manager already.
vfs_bot.py — login gauntlet (Turnstile → creds → OTP → dashboard). Ends at slot_check.run_slot_check().
route_schema.py — JSON-per-route with extends inheritance and a _merge_steps function that already merges a steps array by name — and nothing in the codebase uses steps yet. That's a hook the previous author left for exactly this phase.
settings.py — typed pydantic config, INI + env layered.
Per-route selectors override in JSON (vfs_bot.py:42-59) — the "no hardcoding" pattern is established.
The critical architectural gap: today the bot is idempotent and read-only. waitlist.py documents this explicitly — "never ticks the checkbox." Every retry, every relaunch, every IP rotation is free because nothing is mutated on VFS's side. Booking breaks that assumption completely, and that single fact drives most of the design below.

1. The core problem: booking is not retryable
Right now supervisor.run() catches RetryableError and relaunches the browser. If that happens after you've submitted applicant details at Step 3, a retry means:

a second half-created application on the VFS account
possibly a held/reserved slot you can't release
at Step 6, a double payment
So the first design decision is: the flow must be split into a retryable prefix and a non-retryable committed suffix, with an explicit point of no return.


┌─────────────── RETRYABLE (idempotent) ────────────────┐┌── COMMITTED ──┐
login → turnstile → OTP → dashboard → step1 slot found   │ step2..6 + pay
                                                         ↑
                                              POINT OF NO RETURN
                                        (slot selected / hold acquired)
Everything left of that line: current retry logic applies unchanged. Everything right of it: no blind retries — only resume-from-checkpoint, guided by state read back from the page. The distinction needs to be a first-class concept, e.g. a new CommittedFlowError(Exception) that is explicitly not a subclass of RetryableError, and a supervisor branch that treats it as "human must look at this" (Telegram alert with screenshots + the booking journal path).

2. Architecture: a declarative step engine
Right now the flow is imperative Python (login() calls _pass_turnstile() calls...). For 6 steps × N routes × per-portal variations, that becomes unmaintainable — and violates your "nothing hardcoded" requirement.

Recommended: a step-engine driven by the route JSON steps array — the hook already in route_schema.py.

The layering

supervisor.py          "which route, which account, which IP, retry policy"
   ↓
BookingSession         NEW: owns the point-of-no-return + journal + resume
   ↓
StepEngine             NEW: iterates schema["steps"], dispatches to handlers
   ↓
step handlers          NEW: one module per step type (form_fill, calendar,
                            applicant_list, review, payment, ...)
   ↓
actions/ primitives    NEW: fill / select / click / upload / assert / wait
   ↓                        (generic, selector-driven, retry-aware)
dom_utils, turnstile   EXISTING, unchanged
The engine itself stays small (~200 lines). It does: resolve step → look up handler by "type" → pass the step config + a context object → record the outcome to the journal → advance or halt.

What a step looks like in JSON

{
  "steps": [
    {
      "name": "appointment_details",
      "type": "slot_check",
      "url_contains": "application-detail",
      "on_no_slot": "abort"
    },
    {
      "name": "applicant_details",
      "type": "form",
      "url_contains": "applicant-details",
      "fields": [
        { "control": "firstName",   "value": "{{applicant.first_name}}" },
        { "control": "nationality", "value": "{{applicant.nationality}}", "widget": "mat-select" },
        { "control": "dob",         "value": "{{applicant.dob}}", "widget": "datepicker",
          "format": "dd/MM/yyyy" },
        { "control": "passportNo",  "value": "{{applicant.passport.number}}" }
      ],
      "continue": { "role": "button", "name": "Continue" }
    },
    {
      "name": "select_slot",
      "type": "calendar",
      "commits": true,
      "strategy": "{{booking.slot_strategy}}",
      "date_window_days": "{{booking.date_window_days}}"
    },
    {
      "name": "review",
      "type": "review",
      "assert_contains": ["{{applicant.passport.number}}", "{{combo.centre}}"]
    },
    {
      "name": "payment",
      "type": "payment",
      "provider": "{{payment.provider}}",
      "dry_run": "{{booking.dry_run}}"
    }
  ]
}
Two things to notice:

"commits": true — the engine marks the point of no return declaratively. No code change needed when a portal reorders its steps.
{{...}} templating — the resolver pulls from a merged context (applicant profile + route + combo + settings). This is what makes it actually dynamic rather than "configurable in theory."
Widget abstraction (important)
Your select_mat_dropdown() in slot_check.py:26 is already the right idea — it hides Angular Material's async option-loading behind a clean call. Generalize it into an actions/widgets.py registry:

widget	handles
text	plain input, your existing fill_field
mat-select	your existing dropdown logic
datepicker	mat-datepicker overlay, month/year navigation
radio / checkbox	mat-radio-group, mat-checkbox
file	passport/photo uploads (set_input_files)
autocomplete	type-then-pick-option
phone	country-code + number composites
A new portal quirk = a new widget entry or a selector override in JSON, never a change to step logic. This is the single highest-leverage abstraction in the whole upgrade.

3. Applicant data: the new configuration domain
This is genuinely new — slot-checking needed no personal data. Booking needs a lot: names, DOB, passport numbers/expiry, nationality, phone, address, travel dates, sometimes documents.

Design:


config/
  applicants/
    _schema.json           # field definitions + validation rules (declarative)
    profile-ahmed.json     # one file per person
    profile-fatima.json
  bookings/
    AE-MT.json             # WHICH profiles to book on WHICH combos, priority
A booking target ties it together:


{
  "route": "AE-MT",
  "enabled": true,
  "targets": [
    {
      "label": "Ahmed - Dubai Tourism",
      "combo": "Dubai - Short Stay - Tourism",
      "applicants": ["profile-ahmed"],
      "slot_strategy": "earliest",
      "date_window": { "from": "2026-09-01", "to": "2026-11-30" },
      "max_price": 450,
      "priority": 1
    }
  ]
}
Key points:

Never in git. These files are PII — passport numbers, DOB. config/applicants/ and config/bookings/ go into .gitignore with .example templates committed, matching how credentials.local.ini already works.
Consider encryption at rest (cryptography.fernet, key from env/OS keyring). On an EC2 box holding a dozen people's passport data, plaintext JSON is a real liability. At minimum: chmod 600, and never log applicant fields — add a log redaction filter to main.py's logger setup, keyed off the applicant schema's field names.
Validate before launching a browser. Pydantic models mirroring _schema.json. A missing passport expiry should fail in 50 ms at startup, not at Step 4 with a half-created booking. This is exactly the pattern settings.py already uses.
Extend the config UI. You already have credentials_editor.py (a stdlib local web server, 127.0.0.1-only, backup-then-write, validate-on-save). An applicant_editor.py following the same pattern is the natural home — and the _schema.json can drive the form fields, so adding a field to the schema adds it to the UI for free.
4. Booking journal + idempotency (the safety net)
You need a durable record of what was attempted, written before each committing action. This is what turns "we crashed mid-booking, no idea what happened" into "resume or alert with specifics."


state/bookings/
  2026-07-30T14-22-11_AE-MT_ahmed.json

{
  "booking_id": "uuid",
  "route": "AE-MT", "account": "a***@x.com", "applicants": ["profile-ahmed"],
  "started_at": "...", "committed_at": "...",
  "current_step": "payment",
  "status": "payment_pending",
  "steps": [
    { "name": "appointment_details", "status": "ok", "at": "...",
      "data": { "slot": "05-08-2026" } },
    { "name": "select_slot", "status": "ok", "committed": true, "at": "..." }
  ],
  "vfs_reference": "APP-12345678",
  "payment": { "provider": "...", "amount": 380, "currency": "AED",
               "attempt_id": "...", "status": "submitted" },
  "screenshots": ["...png"]
}
Rules:

Write-ahead: journal the intent before clicking a committing button, flush to disk (os.fsync), then click. If you crash between, you at least know you might have committed.
On startup, scan for dangling journals (status not terminal). Alert loudly and refuse to auto-book that account/route again until a human resolves it. Half the value of the journal is preventing the second bad booking.
De-duplication key: (account, applicant_passport, combo, slot_date). Before booking, check journals — if a matching successful booking exists, skip. Prevents "the cron fired twice and booked twice."
Storage: your waitlist_cooldown.py docstring already flags JSON-file state as needing SQLite once there are concurrent writers. Booking is that moment. A single SQLite file gives you ACID + real locking with zero infra, and lets you enforce the dedup key as an actual UNIQUE constraint rather than hoping the read-check-write races don't bite. I'd go SQLite from day one here, and migrate the cooldown/health state onto it opportunistically.
5. Payment (Step 6) — the highest-risk surface
Be blunt about this one. Payment is where a bug costs real money, and where VFS/the PSP is most likely to change under you.

Design for a hard gate:


[booking]
enabled = false          ; master kill switch — off by default
dry_run = true           ; walk steps 1..6, stop BEFORE final submit
max_bookings_per_run = 1
max_bookings_per_day = 3
require_confirmation = telegram   ; none | telegram
confirmation_timeout_seconds = 300

[payment]
enabled = false          ; SECOND, independent switch
provider = <psp>
max_amount = 500
currency = AED
Recommended progression — do not skip stages:

dry_run: fill every step through 6, screenshot each, stop before final submit. Run this for a week. Validates all selectors and data mapping with zero commitment.
require_confirmation = telegram: bot reaches the payment page, sends you a screenshot + amount + slot details, waits for an approve/reject reply (needs a Telegram inbound poller — new; today telegram.py is send-only). Human in the loop for the money step.
Fully autonomous — only after (2) has worked repeatedly and you've set max_amount.
Other payment considerations:

Amount assertion is mandatory. Read the amount off the page, compare against max_amount and against an expected fee for that combo. Abort if it doesn't match — this catches both VFS price changes and you landing on the wrong page.
Never put card data in config files. Options, in order of preference: (a) a saved card on the VFS account, so no card entry is automated; (b) OS keyring / AWS Secrets Manager, fetched at runtime, never logged, never written to journal; (c) a virtual/single-use card with a hard limit — genuinely the best risk control if your issuer supports it.
3DS / OTP on payment is likely. Your otp_flow.py + otp_service.py pattern is reusable, but a bank 3DS challenge may arrive by SMS, not email — which may mean a manual-approval hand-off is the only honest answer. Design for "pause and ask the human" as a supported outcome, not a failure.
Never retry a payment submit. On ambiguous outcome (timeout, page died) the ONLY correct action is: journal it as unknown, alert, and stop. Then a separate verification path re-logs-in and reads the account's booking list to determine what actually happened. This is standard payment-systems practice and it matters here.
Screenshot every payment page unconditionally, regardless of screenshots_enabled. You want the evidence.
6. Proposed folder structure
Keeping your existing conventions, adding rather than restructuring:


src/
  supervisor.py                 MODIFIED: booking outcomes, dangling-journal gate
  settings.py                   MODIFIED: + Booking, Payment, Applicant sections
  vfs_bot/
    vfs_bot.py                  MODIFIED: run() ends by handing to the engine
    slot_check.py               MODIFIED: becomes the "slot_check" step handler
    errors.py                   MODIFIED: + CommittedFlowError, PaymentError,
                                            SlotGoneError, ValidationError
    engine/
      __init__.py
      step_engine.py            NEW  iterate schema["steps"], dispatch, journal
      context.py                NEW  {{...}} resolution, merged scope
      registry.py               NEW  step type -> handler map
    steps/
      __init__.py
      slot_check.py             NEW  wraps existing logic as a step
      form.py                   NEW  generic declarative form filler
      applicants.py             NEW  add/repeat applicant blocks
      calendar.py               NEW  date+time slot picking (COMMITTING)
      documents.py              NEW  uploads
      review.py                 NEW  assertions before commit
      payment.py                NEW  the gated money step
      confirmation.py           NEW  read reference no., save PDF/screenshot
    actions/
      __init__.py
      widgets.py               NEW  widget registry (mat-select, datepicker, ...)
      forms.py                 NEW  fill-a-field-set primitive
      asserts.py               NEW  declarative page assertions
  booking/
    __init__.py
    session.py                 NEW  BookingSession: commit point, resume, dedup
    journal.py                 NEW  write-ahead journal (SQLite)
    applicant.py               NEW  pydantic applicant models + loader
    targets.py                 NEW  booking target loader + priority
    guards.py                  NEW  kill switches, rate limits, amount checks
    verify.py                  NEW  post-hoc "did it actually book?" checker
  utils/
    telegram_inbound.py        NEW  approval polling (stage 2 above)
    secrets.py                 NEW  keyring/env secret access, redaction

config/
  routes/AE-MT.json            MODIFIED: gains a "steps" array
  applicants/                  NEW (gitignored) + .example
  bookings/                    NEW (gitignored) + .example
state/
  bookings.db                  NEW (gitignored) SQLite journal
tests/
  test_step_engine.py          NEW
  test_context_resolution.py   NEW
  test_journal_dedup.py        NEW
  test_guards.py               NEW
  test_calendar_pick.py        NEW
  fixtures/                    NEW saved HTML of each VFS step
Testability note: your cascade_steps() is deliberately a pure function so it's testable without Playwright — slot_check.py:9-13 says so explicitly. Apply the same discipline aggressively here: context resolution, step sequencing, date-window filtering, guard evaluation, and dedup logic should all be pure functions. Then save real HTML fixtures of steps 2–6 and test handlers against them offline. You cannot iterate on booking logic against the live site — every run costs an account and possibly money.

7. Implementation phases
I'd sequence it like this. Each phase ends somewhere shippable.

#	Phase	Deliverable	Risk
0	Recon	Manually walk steps 1–6 with --keep-open, save HTML + screenshots of every step, catalogue every field and widget per portal. Do not write code first.	none
1	Step engine	Engine + context + registry; port existing slot-check to a slot_check step. Behaviour identical to today, all tests green.	low
2	Applicant data	Schema, pydantic models, loaders, .example files, redaction filter, applicant_editor.py UI.	low
3	Journal	SQLite journal, BookingSession, dedup, dangling-journal startup gate, CommittedFlowError + supervisor branch.	low
4	Steps 2–3 (pre-commit)	Form/applicant handlers. Still no commit — abort before slot selection. Real runs are safe.	low
5	Step 4: slot pick	The committing step. dry_run stops right before. First time you have a real point of no return.	high
6	Step 5: review	Assertions, screenshot, abort-on-mismatch.	low
7	Step 6: payment	Dry-run → Telegram approval → autonomous. Guards + amount checks.	highest
8	Verification & recovery	verify.py reads the account's booking list; resume/reconcile dangling journals.	medium
9	Hardening	Rate limits, per-account booking caps, reporting/receipts, ops runbook.	low
Phases 1–4 are safe to build and run against production without ever committing anything — that's most of the work, de-risked.

8. Things that will bite you (worth planning for now)
Slot disappears between check and book. Extremely likely — you find a slot at Step 1, by Step 4 someone else took it. Needs SlotGoneError as a normal, expected, non-alarming outcome that gracefully abandons and reports. Don't let it look like a failure in the summary.
Session timeout mid-flow. Steps 2–6 with data entry take minutes. VFS sessions expire. Your is_session_expired() detection exists — but post-commit, a session expiry is a CommittedFlowError, not a retry.
Time pressure vs. Turnstile. Slots vanish in seconds on busy routes. Your current login path can take 90 s+ through Turnstile. If booking needs to be fast, you may eventually want a warm authenticated session pool — but don't design for that yet; measure first.
Multi-applicant forms repeat field blocks with indexed control names (applicants[0].firstName). Your widget/field config needs an index-templating notion ({{index}}) from the start, or you'll retrofit it painfully.
Bandwidth. Six steps + uploads on a metered proxy is far more than a slot check. Your persist_cache and metering are already there — just re-baseline the per-run MB expectation, and note that block_resource_types is even riskier now (images may be meaningful on document-upload steps).
Account safety. Booking touches accounts far more heavily than checking. account_safety thresholds tuned for read-only checks are probably wrong for booking; consider separate booking-specific caps.
Portal divergence. Steps 2–6 vary much more between countries than Step 1 does. The extends + per-route steps override in route_schema.py is the right mechanism — but expect _default.json to hold maybe 70% and each route to override more than you'd like. That's fine; that's what the design is for. (Note: _default.json doesn't currently exist — worth creating in Phase 1.)
_merge_steps has a latent quirk: both branches of its if name in by_name do the same assignment (route_schema.py:104-108). Harmless today, but you'll want deep merge for steps (override one field of a step without restating all of it) once steps carry real config. Worth fixing in Phase 1.
Legal/ToS. Automating bookings and payments on VFS is a materially different posture from read-only checking, and near-certainly against their terms. Your own docstrings currently say "This project never books anything" — that line was deliberate. Not my call to make, but you should make it knowingly rather than by accident, and the code comments should stop claiming otherwise.
My top recommendations, condensed
Do Phase 0 (recon) before any code. Six steps × ten portals of unknown field structure — designing handlers blind guarantees rework.
Make the commit boundary a first-class concept ("commits": true in schema, CommittedFlowError, supervisor branch). This is the one thing that, if you skip it, will cause a genuinely bad outcome.
SQLite for the journal from day one. Don't start with JSON files; you need the UNIQUE constraint to make dedup real.
Two independent kill switches (booking.enabled, payment.enabled), both defaulting off, plus dry_run and max_amount.
HTML fixtures + pure functions. You cannot afford to iterate against the live site.
The widget registry is where your "nothing hardcoded" goal is actually won or lost. Invest there.
Want me to drill into any one of these? The two I'd suggest next are the step-engine + context contract (exact JSON schema and handler interface, which locks in the whole shape) or Phase 0's recon checklist (precisely what to capture from each step so the schema design isn't guesswork).