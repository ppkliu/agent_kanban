-- Symphony Dashboard SQLite schema.
--
-- The orchestrator's in-memory state is the source of truth (per SPEC §3).
-- This file ONLY persists:
--   1. event_records       — agent event history for replay tab
--   2. hints               — operator-supplied prompt supplements
--   3. priority_overrides  — drag-to-reorder overrides for dispatch sort
--
-- All three tables are dashboard-private. Removing the dashboard.db file
-- does not affect orchestrator scheduling correctness — it only loses
-- replay history and pending hints.

CREATE TABLE IF NOT EXISTS event_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id        TEXT    NOT NULL,
    attempt_number  INTEGER NOT NULL,
    kind            TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    data_json       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_records_issue_time
    ON event_records (issue_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_event_records_attempt
    ON event_records (issue_id, attempt_number);

CREATE TABLE IF NOT EXISTS hints (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id     TEXT    NOT NULL,
    author       TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    consumed     INTEGER NOT NULL DEFAULT 0,
    consumed_at  TEXT,
    consumed_attempt INTEGER
);

CREATE INDEX IF NOT EXISTS idx_hints_issue_pending
    ON hints (issue_id, consumed);

CREATE TABLE IF NOT EXISTS priority_overrides (
    issue_id    TEXT    PRIMARY KEY,
    rank        INTEGER NOT NULL,
    set_by      TEXT    NOT NULL,
    set_at      TEXT    NOT NULL,
    expires_at  TEXT,
    reason      TEXT
);

-- Finalised attempt snapshots (one row per RELEASED attempt).
-- Lets the dashboard render run-history tables and Replay tab even after
-- the orchestrator has dropped the attempt record from its in-memory map.
CREATE TABLE IF NOT EXISTS attempt_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id        TEXT    NOT NULL,
    attempt_number  INTEGER NOT NULL,
    state           TEXT    NOT NULL,
    started_at      TEXT,
    ended_at        TEXT,
    terminal_reason TEXT,
    last_event_at   TEXT,
    session_id      TEXT,
    turns_consumed  INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0,
    error_message   TEXT,
    UNIQUE (issue_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS idx_attempt_history_issue
    ON attempt_history (issue_id, attempt_number);
