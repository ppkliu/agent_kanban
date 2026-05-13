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

-- Submit log for the Tool API global rate limiter (Phase C quota).
-- One row per successful submit_coding_task call. The submit handler counts
-- rows whose submitted_at is within the rate-limit window (default 60s) and
-- returns 429 when the env-configured per-minute cap is exceeded. Rows are
-- retained for ~1 day for ops debugging and reaped by a periodic cleanup.
CREATE TABLE IF NOT EXISTS submit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT    NOT NULL,
    submitted_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_submit_log_submitted
    ON submit_log (submitted_at);

-- Idempotency keys for Tool API submit_coding_task (Phase C).
-- Lets upstream LLM agents safely retry submits on network blips —
-- the same key within TTL returns the original task_id rather than
-- creating a duplicate task. Expired rows are reaped opportunistically
-- by the orchestrator's existing tick-level cleanup paths.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT    PRIMARY KEY,
    task_id     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_idempotency_expires
    ON idempotency_keys (expires_at);

-- In-flight attempt snapshot — one row per non-released attempt.
-- Lets the orchestrator hydrate _attempts on restart so retry queue + session
-- continuity survive a process bounce. Released attempts are deleted from this
-- table (their history lives in attempt_history above).
CREATE TABLE IF NOT EXISTS attempts_state (
    issue_id        TEXT    PRIMARY KEY,
    attempt_number  INTEGER NOT NULL,
    state           TEXT    NOT NULL,
    started_at      TEXT,
    last_event_at   TEXT,
    session_id      TEXT,
    turns_consumed  INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0,
    error_message   TEXT,
    retry_after     TEXT,
    paused_until    TEXT,
    updated_at      TEXT    NOT NULL
);
