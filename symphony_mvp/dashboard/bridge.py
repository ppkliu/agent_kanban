"""DashboardBridge — connects Orchestrator events to dashboard subscribers + storage.

Per spec §6, this class is the ONLY place that persists Symphony state outside
the orchestrator's memory. Three responsibilities:

  1. Subscribe to Orchestrator.add_event_listener and:
        a) maintain a per-issue ring buffer for fast WS replay
        b) write to SQLite for long-term replay history
        c) fan out to live WebSocket subscribers
  2. Hint store — operator-supplied prompt supplements that get injected
     on the next attempt and are marked consumed afterwards.
  3. Priority override store — operator drag-to-reorder of pending issues.

Symphony's tracker stays read-only at all times. None of these stores write
anything back to GitHub / Linear / etc.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ..agent_runner import AgentEvent

logger = logging.getLogger(__name__)

EventSubscriber = Callable[[str, AgentEvent], None]
TransitionSubscriber = Callable[[str, str, str], None]

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class DashboardBridge:
    """Bridges Orchestrator events to dashboard subscribers + persistence.

    Thread-safety: every public method takes ``self._lock``. SQLite connection
    is opened with ``check_same_thread=False`` because orchestrator workers
    publish from worker threads while FastAPI subscribers consume from async
    handlers.
    """

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        ring_size: int = 500,
    ) -> None:
        self.db_path = str(db_path)
        self.ring_size = ring_size
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self._lock = threading.RLock()
        self._rings: dict[str, deque[AgentEvent]] = {}
        self._event_subs: list[EventSubscriber] = []
        self._transition_subs: list[TransitionSubscriber] = []
        # Tracks the current attempt number per issue so on_event can stamp
        # records correctly. Updated externally via record_attempt_number().
        self._current_attempt: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def _init_schema(self) -> None:
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        with self._db:
            self._db.executescript(sql)

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # Subscriber API (used by FastAPI WebSocket handler)
    # ------------------------------------------------------------------ #
    def add_event_subscriber(self, fn: EventSubscriber) -> None:
        with self._lock:
            self._event_subs.append(fn)

    def remove_event_subscriber(self, fn: EventSubscriber) -> None:
        with self._lock:
            try:
                self._event_subs.remove(fn)
            except ValueError:
                pass

    def add_transition_subscriber(self, fn: TransitionSubscriber) -> None:
        with self._lock:
            self._transition_subs.append(fn)

    def remove_transition_subscriber(self, fn: TransitionSubscriber) -> None:
        with self._lock:
            try:
                self._transition_subs.remove(fn)
            except ValueError:
                pass

    # ------------------------------------------------------------------ #
    # Event ingestion (called from Orchestrator worker threads)
    # ------------------------------------------------------------------ #
    def record_attempt_number(self, issue_id: str, attempt_number: int) -> None:
        """Orchestrator/dispatch tells the bridge which attempt is currently
        active for this issue, so events get tagged with the right number."""
        with self._lock:
            self._current_attempt[issue_id] = attempt_number

    def on_event(self, issue_id: str, event: AgentEvent) -> None:
        """Listener registered with ``Orchestrator.add_event_listener``.

        Drops nothing — even on persistence error we still fan out so the
        live UI keeps working.
        """
        with self._lock:
            ring = self._rings.setdefault(
                issue_id, deque(maxlen=self.ring_size)
            )
            ring.append(event)
            attempt_n = self._current_attempt.get(issue_id, 0)
            subscribers = list(self._event_subs)
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO event_records "
                    "(issue_id, attempt_number, kind, timestamp, data_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        issue_id,
                        attempt_n,
                        event.kind.value,
                        event.timestamp.isoformat(),
                        json.dumps(event.data, default=str),
                    ),
                )
        except Exception:  # noqa: BLE001
            logger.exception("event persist failed for %s", issue_id)

        for sub in subscribers:
            try:
                sub(issue_id, event)
            except Exception:  # noqa: BLE001
                logger.exception("event subscriber raised")

    def notify_fsm_transition(
        self, issue_id: str, from_state: str, to_state: str
    ) -> None:
        with self._lock:
            subscribers = list(self._transition_subs)
        for sub in subscribers:
            try:
                sub(issue_id, from_state, to_state)
            except Exception:  # noqa: BLE001
                logger.exception("transition subscriber raised")

    def get_recent_events(
        self, issue_id: str, limit: int = 100
    ) -> list[AgentEvent]:
        with self._lock:
            ring = self._rings.get(issue_id)
            if not ring:
                return []
            tail = list(ring)
            return tail[-limit:]

    # ------------------------------------------------------------------ #
    # Hint store
    # ------------------------------------------------------------------ #
    def add_hint(self, issue_id: str, author: str, content: str) -> int:
        ts = _now_iso()
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO hints (issue_id, author, content, created_at, consumed) "
                "VALUES (?, ?, ?, ?, 0)",
                (issue_id, author, content, ts),
            )
            return int(cur.lastrowid)

    def fetch_pending_hints(self, issue_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, author, content, created_at FROM hints "
                "WHERE issue_id = ? AND consumed = 0 ORDER BY id",
                (issue_id,),
            ).fetchall()
        return [
            {"id": r[0], "author": r[1], "content": r[2], "created_at": r[3]}
            for r in rows
        ]

    def mark_hints_consumed(
        self, hint_ids: Iterable[int], *, attempt_number: int | None = None
    ) -> None:
        ids = list(hint_ids)
        if not ids:
            return
        ts = _now_iso()
        placeholders = ",".join("?" * len(ids))
        with self._lock, self._db:
            self._db.execute(
                f"UPDATE hints SET consumed = 1, consumed_at = ?, "
                f"consumed_attempt = ? WHERE id IN ({placeholders})",
                (ts, attempt_number, *ids),
            )

    def list_hints(self, issue_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, author, content, created_at, consumed, "
                "consumed_at, consumed_attempt FROM hints "
                "WHERE issue_id = ? ORDER BY id DESC",
                (issue_id,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "author": r[1],
                "content": r[2],
                "created_at": r[3],
                "consumed": bool(r[4]),
                "consumed_at": r[5],
                "consumed_attempt": r[6],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # Priority override store
    # ------------------------------------------------------------------ #
    def set_priority_override(
        self,
        issue_id: str,
        rank: int,
        *,
        set_by: str,
        ttl_hours: float = 24.0,
        reason: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=ttl_hours) if ttl_hours > 0 else None
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO priority_overrides "
                "(issue_id, rank, set_by, set_at, expires_at, reason) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(issue_id) DO UPDATE SET "
                "rank=excluded.rank, set_by=excluded.set_by, "
                "set_at=excluded.set_at, expires_at=excluded.expires_at, "
                "reason=excluded.reason",
                (
                    issue_id,
                    int(rank),
                    set_by,
                    now.isoformat(),
                    expires.isoformat() if expires else None,
                    reason,
                ),
            )

    def clear_priority_override(self, issue_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM priority_overrides WHERE issue_id = ?",
                (issue_id,),
            )

    def get_priority_overrides(self) -> dict[str, int]:
        """Returns active (non-expired) overrides as ``{issue_id: rank}``.

        Lazily prunes expired rows on every read.
        """
        now_iso = _now_iso()
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM priority_overrides "
                "WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now_iso,),
            )
            rows = self._db.execute(
                "SELECT issue_id, rank FROM priority_overrides ORDER BY rank"
            ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def reorder_pending(
        self,
        ordered_issue_ids: list[str],
        *,
        set_by: str,
        ttl_hours: float = 24.0,
        reason: str | None = "drag-reorder",
    ) -> None:
        """Replace overrides for the given issue ids with sequential ranks.

        Issue ids NOT in ``ordered_issue_ids`` keep any prior override they had.
        Use ``clear_priority_override`` to remove an override entirely.
        """
        for rank, iid in enumerate(ordered_issue_ids):
            self.set_priority_override(
                iid, rank, set_by=set_by, ttl_hours=ttl_hours, reason=reason
            )

    # ------------------------------------------------------------------ #
    # Attempt history (finalised snapshots)
    # ------------------------------------------------------------------ #
    def record_finalised_attempt(self, attempt: Any) -> None:
        """Snapshot a RELEASED attempt so history survives in-memory eviction.

        ``attempt`` is duck-typed against ``symphony_mvp.models.RunAttempt``;
        we deliberately accept Any to keep bridge import-light.
        Idempotent on (issue_id, attempt_number) via UNIQUE constraint —
        re-finalising overwrites the row (REPLACE semantics).
        """
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO attempt_history "
                "(issue_id, attempt_number, state, started_at, ended_at, "
                "terminal_reason, last_event_at, session_id, turns_consumed, "
                "cost_usd, error_message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(issue_id, attempt_number) DO UPDATE SET "
                "state=excluded.state, started_at=excluded.started_at, "
                "ended_at=excluded.ended_at, "
                "terminal_reason=excluded.terminal_reason, "
                "last_event_at=excluded.last_event_at, "
                "session_id=excluded.session_id, "
                "turns_consumed=excluded.turns_consumed, "
                "cost_usd=excluded.cost_usd, "
                "error_message=excluded.error_message",
                (
                    attempt.issue_id,
                    attempt.attempt_number,
                    getattr(attempt.state, "value", str(attempt.state)),
                    attempt.started_at.isoformat() if attempt.started_at else None,
                    attempt.ended_at.isoformat() if attempt.ended_at else None,
                    getattr(attempt.terminal_reason, "value", None)
                    if attempt.terminal_reason
                    else None,
                    attempt.last_event_at.isoformat()
                    if attempt.last_event_at
                    else None,
                    attempt.session_id,
                    attempt.turns_consumed or 0,
                    float(getattr(attempt, "cost_usd", 0.0) or 0.0),
                    attempt.error_message,
                ),
            )

    # ------------------------------------------------------------------ #
    # Active attempt persistence (for orchestrator restart recovery)
    # ------------------------------------------------------------------ #
    def persist_attempt(self, attempt: Any) -> None:
        """Snapshot one in-flight attempt to ``attempts_state``.

        Idempotent on ``issue_id`` (PK). Released attempts SHOULD be deleted
        via ``delete_attempt_state`` rather than left in this table; the
        ``attempt_history`` table is the canonical home for finalised rows.

        Duck-typed on ``RunAttempt`` like ``record_finalised_attempt``.
        """
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO attempts_state "
                "(issue_id, attempt_number, state, started_at, last_event_at, "
                "session_id, turns_consumed, cost_usd, error_message, "
                "retry_after, paused_until, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(issue_id) DO UPDATE SET "
                "attempt_number=excluded.attempt_number, "
                "state=excluded.state, "
                "started_at=excluded.started_at, "
                "last_event_at=excluded.last_event_at, "
                "session_id=excluded.session_id, "
                "turns_consumed=excluded.turns_consumed, "
                "cost_usd=excluded.cost_usd, "
                "error_message=excluded.error_message, "
                "retry_after=excluded.retry_after, "
                "paused_until=excluded.paused_until, "
                "updated_at=excluded.updated_at",
                (
                    attempt.issue_id,
                    attempt.attempt_number,
                    getattr(attempt.state, "value", str(attempt.state)),
                    attempt.started_at.isoformat() if attempt.started_at else None,
                    attempt.last_event_at.isoformat()
                    if attempt.last_event_at else None,
                    attempt.session_id,
                    attempt.turns_consumed or 0,
                    float(getattr(attempt, "cost_usd", 0.0) or 0.0),
                    attempt.error_message,
                    attempt.retry_after.isoformat() if attempt.retry_after else None,
                    attempt.paused_until.isoformat()
                    if attempt.paused_until else None,
                    now_iso,
                ),
            )

    def delete_attempt_state(self, issue_id: str) -> None:
        """Remove an issue's row from ``attempts_state``. Called on _release."""
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM attempts_state WHERE issue_id = ?", (issue_id,)
            )

    def load_active_attempts(self) -> list[dict[str, Any]]:
        """Return all rows from ``attempts_state``. Orchestrator hydrates
        ``self._attempts`` from this on init when a bridge is provided."""
        with self._lock:
            rows = self._db.execute(
                "SELECT issue_id, attempt_number, state, started_at, "
                "last_event_at, session_id, turns_consumed, cost_usd, "
                "error_message, retry_after, paused_until "
                "FROM attempts_state"
            ).fetchall()
        cols = (
            "issue_id",
            "attempt_number",
            "state",
            "started_at",
            "last_event_at",
            "session_id",
            "turns_consumed",
            "cost_usd",
            "error_message",
            "retry_after",
            "paused_until",
        )
        return [dict(zip(cols, r)) for r in rows]

    def list_attempt_history(self, issue_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT issue_id, attempt_number, state, started_at, ended_at, "
                "terminal_reason, last_event_at, session_id, turns_consumed, "
                "cost_usd, error_message "
                "FROM attempt_history WHERE issue_id = ? "
                "ORDER BY attempt_number ASC",
                (issue_id,),
            ).fetchall()
        cols = (
            "issue_id",
            "attempt_number",
            "state",
            "started_at",
            "ended_at",
            "terminal_reason",
            "last_event_at",
            "session_id",
            "turns_consumed",
            "cost_usd",
            "error_message",
        )
        return [dict(zip(cols, r)) for r in rows]

    # ------------------------------------------------------------------ #
    # Replay queries
    # ------------------------------------------------------------------ #
    def fetch_events(
        self,
        issue_id: str,
        *,
        attempt_number: int | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        with self._lock:
            if attempt_number is None:
                rows = self._db.execute(
                    "SELECT id, attempt_number, kind, timestamp, data_json "
                    "FROM event_records WHERE issue_id = ? "
                    "ORDER BY id ASC LIMIT ?",
                    (issue_id, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT id, attempt_number, kind, timestamp, data_json "
                    "FROM event_records WHERE issue_id = ? AND attempt_number = ? "
                    "ORDER BY id ASC LIMIT ?",
                    (issue_id, attempt_number, limit),
                ).fetchall()
        return [
            {
                "id": r[0],
                "attempt_number": r[1],
                "kind": r[2],
                "timestamp": r[3],
                "data": json.loads(r[4]),
            }
            for r in rows
        ]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
