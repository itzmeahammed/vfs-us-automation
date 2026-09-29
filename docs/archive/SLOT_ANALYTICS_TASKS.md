# Slot Analytics — build plan

Turn the slot checker's throwaway log lines into a permanent, queryable history,
then a dashboard sales agents use to tell a client **which Schengen country to
apply for, and how soon they'll get an appointment**.

Long game: enough clean history to *predict* openings. Every decision below is
made so the data is model-ready from day one — nothing is aggregated away, and
the raw banner text is always kept so features can be re-derived later.

**Storage:** SQLite at `state/slots.db` (WAL), append-forever.
**Source of truth for what exists:** `config/routes/*.json` — combinations come
from there, so a country/centre/category can never be duplicated by a portal
renaming its dropdown text.

---

## Phase 1 — Storage foundation  ✅

The database, the writer, and the two pure parsers. No behaviour change to the
bot yet — this phase is only libraries + tests.

- [x] **1.1** `src/slots/schema.sql` — tables, indexes, `schema_version`.
- [x] **1.2** `src/slots/db.py` — connection factory (WAL, foreign keys, row
      factory, busy timeout), forward-only migration runner.
- [x] **1.3** `src/slots/parse.py` — banner text -> `(outcome, {applicants: date})`.
      Shared by the live path AND the log seeder so they can never drift.
- [x] **1.4** `src/slots/registry.py` — read `config/routes/*.json`, build the
      canonical combination list, upsert into `combos`. Identity is the
      normalised `(route, centre, category, sub_category)` key, never the
      display label. Resolves a log label back to a combo.
- [x] **1.5** `src/slots/events.py` — compare a check with the combo's previous
      one, emit `opened` / `closed` / `date_moved` / `reappeared`.
- [x] **1.6** `src/slots/store.py` — the only module that writes: `start_run`,
      `finish_run`, `record_check`, `sync_combos`. Idempotent
      (`UNIQUE(combo_id, ts_utc)`), never raises into the caller.
- [x] **1.7** Tests: parse, registry/dedup, events, store idempotency.

## Phase 2 — Seed from the last 7 days of logs  ✅

- [x] **2.1** `src/slots/logreader.py` — walk `logs/app-*.log`, pair
      `Checking slot for: <label>` with its `-> <message>` (including the
      continuation lines that carry no timestamp), and attach each to the route
      run it happened in.
- [x] **2.2** Resolve labels -> combos via the registry. Anything unresolved
      lands in `unmapped_labels` for review instead of inventing a combo.
- [x] **2.3** `scripts/seed_from_logs.py --days 7` — idempotent, re-runnable,
      prints a summary of what it stored.
- [x] **2.4** Tests against real log fixtures.

## Phase 3 — Live recording  ✅

- [x] **3.1** `slot_check.py`: one `record_check` call where the result is
      already logged.
- [x] **3.2** `supervisor.py`: `start_run` before the route runs,
      `finish_run` after its status line.
- [x] **3.3** `[slots]` config section (`enabled`, `db_path`, `timezone`).
- [x] **3.4** Failure isolation — a DB error can never fail a route. Tests
      prove a broken DB leaves the run untouched.

## Phase 4 — Query layer (the sales numbers)  ✅

`src/slots/query.py`, one function per question an agent actually asks:

- [x] **4.1** Per country/centre: latest earliest-slot date, when it was seen.
- [x] **4.2** Typical wait — median/min lead days over the window.
- [x] **4.3** Availability — share of days a slot was seen, per-day sparkline.
- [x] **4.4** Waitlist availability (the fallback pitch).
- [x] **4.5** Openings — how many, and the weekday/hour they cluster in.
- [x] **4.6** `best_bet` ranking score, with its inputs exposed so the page can
      explain *why* a country ranks where it does.
- [x] **4.7** Tests on a seeded fixture DB.

## Phase 5 — Agent dashboard  ✅

- [x] **5.1** `src/slots/dashboard.py` + template -> single self-contained HTML.
- [x] **5.2** Ranking table: country, city, status, next appointment, typical
      wait, soonest seen, last slot seen (date), date drift, best-bet score.
      Every heading carries a tooltip explaining what the number is.
- [x] **5.3** Drill-down per country: every centre/category, its own numbers.
- [x] ~~**5.4** Copy-ready pitch line per country.~~ Built, then removed at the
      owner's request — agents write their own wording.
- [x] **5.5** Best-time-to-check grid (weekday x hour).
- [x] **5.6** `scripts/build_dashboard.py`, auto-refresh after each run.
- [x] **5.7** Activity feed: openings AND earliest dates jumping closer. Openings
      alone showed days of silence while the board was busy — the countries worth
      selling never "open" because they never close.
- [x] **5.8** Visa-type tabs (Tourist / Business), schema v2 `combos.purpose`.
      An any-purpose type (SCHENGEN, Short Stay, ShortStay) counts for BOTH tabs:
      Sweden, Switzerland, Germany, Greece, Italy and the Netherlands have nothing
      else, so excluding those would empty the tabs of the best countries.
- [x] **5.9** Waitlist tab — its own board (open now / offered on / real slots too /
      last seen), because none of the slot columns mean anything for a sign-up.
      Ignores the visa-type split: a waitlist covers the country.

### Where Phases 1-5 landed (16 Sep 2026)

- `state/slots.db` seeded from the last 7 days of logs: **3,021 checks**, 1,267
  quoted dates, 91 events, 43 combinations, **0 unmapped labels** — every label
  in the logs matched a combination in `config/routes`.
- Live recording is wired in and failure-isolated; `reports/slot_dashboard.html`
  rebuilds itself at the end of every run.
- 72 tests across `tests/test_slots_*.py`.

What the first week of data says: Norway and Sweden are open on essentially
every check at ~2 days out; France is always open but ~20 days out and slipping
~4 days a day; Hungary opens occasionally (4 openings, all 18:00-22:00);
Switzerland, Czechia, Germany, Greece, Italy and the Netherlands are
waitlist-only. Malta and Luxembourg are switched off in `config/vfs_urls.ini`,
so they show as *not checked* rather than as having no slots.

- [x] **5.10** Wall board (`src/slots/wall.py`) — the card-wall mockup's exact
      look (`Card wall (your concept)-html/Main.dc.html`, left untouched), with
      the dashboard's content: Tourist / Business / Waitlist tabs on the
      subtitle line (clickable, and rotating every 20 s on an unattended
      screen), each card's footer carrying Wait / Soonest / Seen (date) / Drift,
      no week strip. 1920x1080 scaled to any screen; rewritten after every run.

## Phase 6 — Prediction (later, once history accumulates)

- [ ] **6.1** Feature table: rolling rates, gaps since last opening, weekday/hour.
- [ ] **6.2** Baseline model — probability a combo opens in the next 24h.
- [ ] **6.3** Lead-time forecast + confidence shown on the dashboard.
- [ ] **6.4** Backtesting harness so a model is never shipped unmeasured.

---

## Data captured per check

| Field | Why it's kept |
|---|---|
| `ts_utc`, `ts_local`, `hour_local`, `weekday` | Time-of-day patterns; UTC keeps ordering honest |
| `outcome` (`slot`/`waitlist`/`none`/`error`) | The label a model predicts |
| `raw_message` | Re-derive anything later; VFS wording changes |
| `slot_dates[applicants]` + `lead_days` | Different answer for 1, 2, 3 applicants |
| `run_id` -> account, proxy, status | Tells "checked, nothing there" from "never checked" |
| `events` | Openings are the thing worth predicting |
