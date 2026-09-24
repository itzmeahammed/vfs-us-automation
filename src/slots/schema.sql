-- Slot history schema (SQLite). Forward-only: never edit a shipped statement,
-- add a new migration in db.py instead.
--
-- Design rules:
--   * Nothing is aggregated away. One row per check, kept forever, with the
--     raw banner text — so any feature can be re-derived later.
--   * A combination's identity comes from config/routes/*.json, never from the
--     portal's display label (VFS renames those; the label is only a hint).
--   * Every timestamp is stored twice: UTC for ordering, local for the
--     weekday/hour patterns an agent actually cares about.

-- One row per thing we check: country + centre + category + sub-category.
-- `combo_key` is the normalised identity and is UNIQUE, so a rename in the
-- portal can never create a second row for the same real-world combination.
CREATE TABLE IF NOT EXISTS combos (
    id              INTEGER PRIMARY KEY,
    combo_key       TEXT    NOT NULL UNIQUE,
    route           TEXT    NOT NULL,          -- 'AE-NOR'
    source_code     TEXT    NOT NULL,          -- 'AE'
    dest_code       TEXT    NOT NULL,          -- 'NOR'
    country_name    TEXT    NOT NULL,          -- 'Norway'
    centre          TEXT    NOT NULL DEFAULT '',  -- raw portal text
    city            TEXT    NOT NULL DEFAULT '',  -- 'Abu Dhabi' (canonical)
    category        TEXT    NOT NULL DEFAULT '',
    sub_category    TEXT    NOT NULL DEFAULT '',
    visa_type       TEXT    NOT NULL DEFAULT '',  -- category/sub-category, de-duped
    purpose         TEXT    NOT NULL DEFAULT 'any',  -- tourist | business | any
    config_label    TEXT    NOT NULL DEFAULT '',  -- label from the route file
    enabled         INTEGER NOT NULL DEFAULT 1,   -- mirrors `disabled` in config
    in_config       INTEGER NOT NULL DEFAULT 1,   -- 0 = retired from config, history kept
    first_seen_utc  TEXT,
    last_seen_utc   TEXT,
    -- Position within its route file, 0-based. The bot checks combinations in
    -- this order, which is what lets a pre-August log line that carries only a
    -- bare centre ('Abu Dhabi', shared by two categories) be attributed by
    -- POSITION instead of by text. See registry.resolve().
    config_order    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_combos_route ON combos(route);
CREATE INDEX IF NOT EXISTS idx_combos_purpose ON combos(purpose);

-- Display labels seen in logs / reports, mapped to the combo they mean. Lets
-- the seeder resolve 'Norway Visa Application Center - Abu Dhabi - Tourist'
-- back to a combo even after the portal renames the centre.
CREATE TABLE IF NOT EXISTS label_aliases (
    label_key   TEXT    NOT NULL,
    route       TEXT    NOT NULL,
    combo_id    INTEGER NOT NULL REFERENCES combos(id) ON DELETE CASCADE,
    origin      TEXT    NOT NULL DEFAULT 'config',   -- config | inferred | manual
    PRIMARY KEY (route, label_key)
);

-- Labels we could NOT map. Never invents a combo — surfaced for a human to fix
-- the route file (or add a manual alias).
CREATE TABLE IF NOT EXISTS unmapped_labels (
    route       TEXT    NOT NULL,
    label       TEXT    NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT,
    last_seen   TEXT,
    PRIMARY KEY (route, label)
);

-- One row per route run. Its purpose is to distinguish "we checked and there
-- was nothing" from "we never checked" — a model trained without that
-- distinction learns from gaps that mean nothing.
CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY,
    route            TEXT    NOT NULL,
    source_code      TEXT    NOT NULL DEFAULT '',
    dest_code        TEXT    NOT NULL DEFAULT '',
    started_at_utc   TEXT    NOT NULL,
    finished_at_utc  TEXT,
    status           TEXT    NOT NULL DEFAULT 'RUNNING',  -- RUNNING|OK|FAILED|...
    attempts         INTEGER,
    account_masked   TEXT,
    proxy_label      TEXT,
    error            TEXT,
    source           TEXT    NOT NULL DEFAULT 'live',     -- live | backfill
    UNIQUE (route, started_at_utc, source)
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at_utc);

-- The fact table: one row per combination read.
CREATE TABLE IF NOT EXISTS checks (
    id            INTEGER PRIMARY KEY,
    combo_id      INTEGER NOT NULL REFERENCES combos(id) ON DELETE CASCADE,
    run_id        INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    ts_utc        TEXT    NOT NULL,
    ts_local      TEXT    NOT NULL,
    tz_offset_min INTEGER NOT NULL DEFAULT 0,
    date_local    TEXT    NOT NULL,          -- 'YYYY-MM-DD', for per-day rollups
    hour_local    INTEGER NOT NULL,          -- 0-23
    weekday       INTEGER NOT NULL,          -- 0=Monday .. 6=Sunday
    outcome       TEXT    NOT NULL,          -- slot | waitlist | none | error | disabled
    error_reason  TEXT,
    raw_message   TEXT    NOT NULL DEFAULT '',
    source        TEXT    NOT NULL DEFAULT 'live',
    -- Idempotency: re-running the seeder over the same logs stores nothing new.
    UNIQUE (combo_id, ts_utc)
);

CREATE INDEX IF NOT EXISTS idx_checks_combo_ts ON checks(combo_id, ts_utc);
CREATE INDEX IF NOT EXISTS idx_checks_ts      ON checks(ts_utc);
CREATE INDEX IF NOT EXISTS idx_checks_date    ON checks(date_local);
CREATE INDEX IF NOT EXISTS idx_checks_outcome ON checks(outcome);
-- Coverage asks which runs produced no readings, which reads this the other way
-- round from the live path.
CREATE INDEX IF NOT EXISTS idx_checks_run     ON checks(run_id);

-- One row per applicant count on a check. VFS quotes a different date for 1, 2
-- and 3 applicants, and a family of four is a different sale from a solo
-- traveller — so this is kept per applicant count, not flattened.
CREATE TABLE IF NOT EXISTS slot_dates (
    check_id    INTEGER NOT NULL REFERENCES checks(id) ON DELETE CASCADE,
    applicants  INTEGER NOT NULL,
    slot_date   TEXT    NOT NULL,            -- 'YYYY-MM-DD'
    lead_days   INTEGER NOT NULL,            -- slot_date - date_local, fixed at read time
    PRIMARY KEY (check_id, applicants)
);

CREATE INDEX IF NOT EXISTS idx_slot_dates_date ON slot_dates(slot_date);

-- Transitions. These are the prediction targets: an 'opened' row is the moment
-- a combination went from nothing to bookable.
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY,
    combo_id      INTEGER NOT NULL REFERENCES combos(id) ON DELETE CASCADE,
    check_id      INTEGER REFERENCES checks(id) ON DELETE CASCADE,
    ts_utc        TEXT    NOT NULL,
    ts_local      TEXT    NOT NULL,
    hour_local    INTEGER NOT NULL,
    weekday       INTEGER NOT NULL,
    kind          TEXT    NOT NULL,          -- opened|closed|date_moved|waitlist_opened|waitlist_closed
    prev_outcome  TEXT,
    new_outcome   TEXT,
    prev_date     TEXT,
    new_date      TEXT,
    delta_days    INTEGER,                   -- new_date - prev_date (date_moved)
    gap_hours     REAL,                      -- since the previous check of this combo
    UNIQUE (combo_id, ts_utc, kind)
);

CREATE INDEX IF NOT EXISTS idx_events_combo ON events(combo_id, ts_utc);
CREATE INDEX IF NOT EXISTS idx_events_kind  ON events(kind, ts_utc);
