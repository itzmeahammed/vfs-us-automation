# Handover — VFS Norway booking bot

## What this is

A Playwright bot that books VFS visa appointments end to end: log in → pick
centre/category → fill applicant details → pick a slot → services → review →
**pay with a real card** → confirmation.

- `src/booking/` — the walk, the probe, the CLI, route configs
- `src/payment/` — gateway, card, journal, config. Country-agnostic by design:
  CyberSource is the same portal across VFS countries.
- `src/waitlist/` — the older invitation flow; `register.py` still owns the
  shared click/field/page-gate helpers the booking walk uses.

Route configs are JSON (`config/booking/AE-NOR.json`), client records are
gitignored JSON holding PII (`config/registrants/*.json`).

## Where the work stands

**AE-NOR is fully mapped and every step has run live.** As of 2026-09-29 a run
reached the payment gateway, filled all 14 fields, and submitted. Every
CyberSource selector worked on first contact.

| step | state |
|---|---|
| start_booking → review_pay | proven live |
| payment (gateway, card, submit) | proven live; VFS declined the card |
| confirmation | **never reached** — no successful payment yet |

AE-CHE and AE-GRC remain unwalked and `enabled: false`.

`1686 passed, 1 skipped`. The skip reads a sample DOM from `captured/`, which
the operator deleted; it guards nothing right now.

## Running it

```bash
export VFS_CARD_NUMBER="..." VFS_CARD_EXPIRY="MM/YY" VFS_CARD_CVN="..."

PYTHONIOENCODING=utf-8 python -m src.booking probe \
  -sc AE -dc NOR --entry new \
  --combo "Norway Visa Application Center - Dubai - Tourist" \
  --registrant mufaddal-nor --proxy-url "" \
  --walk --commit -v
```

Drop `--commit` for a dry run that stops armed at Pay Online and charges
nothing. `--capture full` adds per-page DOM when mapping a new country.
`--keep-open` is reconnaissance only — it holds the session waiting for a human
and must never be set on a scheduled run.

Card values come from the environment only. `card.py` refuses to load them from
a file, because `config/` is committed and a secret committed once is not
removed by deleting it.

## Things that will bite you

**VFS blocks an account after ~3 logins in a short window (429001).** Every
invocation is a fresh login. One run, then diagnose from artifacts — never
retry blind. This has cost two live invitations historically.

**`force=True` does not enable a disabled button.** It dispatches a click at
the coordinates; a disabled `<button>` swallows it and Playwright still reports
success. `_click` now refuses disabled controls and reports *why* (reading the
page's own `mat-error`, empty required inputs, unticked boxes). Never reintroduce
force into that ladder. The one legitimate force is `walk._click_slot`, where an
overlay covers a control known to be live.

**A VFS modal whose only button says "Continue" can mean stop.** Seen live:
*"We have received your booking request and your payment is under process."* The
dismisser's keyword list matched it, and clicking Continue would have booked and
charged a second time. `BLOCKING_PHRASES` in `turnstile.py` raises
`BlockingDialogError` **before** the dismiss loop — that ordering is the safety
property. Those phrases live in code, not config, deliberately: a config file is
something an operator edits to make a run go through.

**A blocking modal makes the previous step's submit fail silently,** so the
symptom lands one step later as a page-gate timeout. Same shape as the
force-click bug: cause and symptom in different places.

**Never write `page.content()` on the payment gateway.** A filled card form
returns the PAN and CVN inside `input value=""`. The payment path writes PNGs
only; `tests/test_capture_modes.py` asserts `_capture_html` appears nowhere in
`runner.py` (comments stripped first, since that file documents the rule).

**`PaymentDeclined` subclasses `PaymentSubmitted`, never `PaymentError`.**
Anything catching `PaymentError` to retry would otherwise retry a payment that
may have taken money. A test pins the hierarchy because it looks like something
a refactor would "tidy up".

**`get_by_text(exact=False)` is a substring match but still case-sensitive.**
That surprising pairing burned a 45s gate. Page gates now accept a list and
match case-insensitively with `\s+` between words; every phrase must still be
absent from the previous page, enforced per phrase.

## Open items

- **Commit everything.** Nothing from 2026-09-29 is in git.

- **`config/registrants/mufaddal-nor.json` has changed identity repeatedly** —
  it currently reads `mukram@travnook.com` / `ahammedL yousef` /
  `S0580172`, while the 12:51 run used athul's account with mufaddal's
  passport. Confirm whose booking is actually intended before the next run; a
  mismatched account/email means the VFS confirmation lands in an unwatched
  inbox.
- ~~Switch `strategy` to `"in_range"`~~ — **replaced by a stricter rule.**
  A booking now REQUIRES `date_from`/`date_to` on the client record; no window
  means the run is refused before Chrome opens, never a fallback to
  "earliest". The route-level `strategy` is ignored. An agent who genuinely
  wants the soonest slot writes `"slot_strategy": "earliest"` explicitly.
  **All three existing client records currently fail this** — run
  `python -m src.booking check` to list them.

- `inbox reconcile --apply` writes the journal without the run lock.
- Two clients' form email ≠ account email, so invitations land unwatched.
- `store.backfill_timestamps()` is called from nowhere.
- AE-CHE and AE-GRC unwalked and disabled.

## Working agreements

- Diagnose from artifacts, not reruns — the session and the login are the
  scarce resources.
- Screenshots on failure; DOM only under `--capture full`.
- The payment journal is fsync'd **before** the irreversible click. Its absence
  is proof nothing was submitted.
- `python -m src.payment status` after any payment run, and after any crash.
