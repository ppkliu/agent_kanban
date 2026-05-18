"""Prometheus text-format metrics for Symphony Dashboard.

Exposed at ``GET /metrics`` (no auth, per Prometheus scraping convention —
protect at the reverse-proxy layer if the dashboard is internet-facing).

Design choices
--------------
- **No external dependency**. We hand-format the Prometheus text protocol
  (https://prometheus.io/docs/instrumenting/exposition_formats/) instead of
  pulling in ``prometheus_client``. The metric set is small and stable.
- **No new instrumentation**. Every counter / gauge is computed at scrape
  time from data we already store (Orchestrator._attempts in memory +
  the bridge's SQLite tables). Zero hot-path overhead.
- **Cardinality is bounded**. The only labelled metrics are state /
  terminal_reason / kind enums, all single-digit cardinality.

For the bigger OpenTelemetry tracing story (spans, propagation), see the
"Tracing / Metrics" entry in docs/todolist/post-mvp-gaps.md — this module
is the minimal-viable subset of that gap.
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from ..models import RunState, TerminalReason

if TYPE_CHECKING:  # pragma: no cover
    from ..orchestrator import Orchestrator
    from .bridge import DashboardBridge


# Stable enum sets so every label combination always appears (Prometheus
# clients deal better with "0" values than with vanishing series).
_RUN_STATES: tuple[str, ...] = tuple(s.value for s in RunState)
_TERMINAL_REASONS: tuple[str, ...] = tuple(r.value for r in TerminalReason)


def _emit_help(lines: list[str], name: str, help_text: str, kind: str) -> None:
    """Append the standard `# HELP` + `# TYPE` preamble for a metric."""
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {kind}")


def _count_history_by_terminal_reason(db: sqlite3.Connection) -> dict[str, int]:
    """Group attempt_history rows by terminal_reason."""
    counts: dict[str, int] = {r: 0 for r in _TERMINAL_REASONS}
    rows = db.execute(
        "SELECT COALESCE(terminal_reason, ''), COUNT(*) "
        "FROM attempt_history GROUP BY terminal_reason"
    ).fetchall()
    for reason, count in rows:
        if reason in counts:
            counts[reason] = int(count)
    return counts


def _count_events_by_kind(db: sqlite3.Connection) -> dict[str, int]:
    """Group event_records rows by kind."""
    rows = db.execute(
        "SELECT kind, COUNT(*) FROM event_records GROUP BY kind"
    ).fetchall()
    return {kind: int(count) for kind, count in rows}


def _count_table(db: sqlite3.Connection, table: str, where: str = "") -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    row = db.execute(sql).fetchone()
    return int(row[0]) if row else 0


def format_prometheus(
    orchestrator: "Orchestrator", bridge: "DashboardBridge"
) -> str:
    """Render the current Symphony metric set as Prometheus exposition text.

    Safe to call repeatedly; all counters are read-only. Returns a bytes-
    ready ``str`` ending with a newline so a typical FastAPI ``PlainTextResponse``
    serves it cleanly.
    """
    lines: list[str] = []

    # ---- attempts by RunState (in-memory snapshot) ---------------------- #
    state_counts: dict[str, int] = {s: 0 for s in _RUN_STATES}
    with orchestrator._lock:  # noqa: SLF001 — read snapshot under lock
        for att in orchestrator._attempts.values():  # noqa: SLF001
            state_counts[att.state.value] = state_counts.get(att.state.value, 0) + 1

    _emit_help(
        lines,
        "symphony_attempts",
        "Current in-memory attempts grouped by Symphony RunState.",
        "gauge",
    )
    for state in _RUN_STATES:
        lines.append(f'symphony_attempts{{state="{state}"}} {state_counts[state]}')

    # ---- finalised attempts by terminal_reason (from SQLite history) ---- #
    # Access the bridge's connection through its public surface; keep it
    # in a critical section so we don't race with a concurrent persist.
    with bridge._lock:  # noqa: SLF001 — same pattern as bridge's own methods
        db = bridge._db  # noqa: SLF001
        history_by_reason = _count_history_by_terminal_reason(db)
        event_count = _count_table(db, "event_records")
        events_by_kind = _count_events_by_kind(db)
        hint_count = _count_table(db, "hints")
        hint_pending_count = _count_table(db, "hints", "consumed = 0")
        priority_override_count = _count_table(db, "priority_overrides")
        active_attempts_state_count = _count_table(db, "attempts_state")
        idempotency_count = _count_table(db, "idempotency_keys")

    _emit_help(
        lines,
        "symphony_attempts_finalised_total",
        "Finalised (RELEASED) attempts grouped by terminal reason.",
        "counter",
    )
    for reason in _TERMINAL_REASONS:
        lines.append(
            f'symphony_attempts_finalised_total{{terminal_reason="{reason}"}} '
            f"{history_by_reason[reason]}"
        )

    # ---- event volume (total + by kind) --------------------------------- #
    _emit_help(
        lines,
        "symphony_events_total",
        "Total agent events recorded by the bridge.",
        "counter",
    )
    lines.append(f"symphony_events_total {event_count}")

    _emit_help(
        lines,
        "symphony_events_by_kind_total",
        "Agent events grouped by AgentEventKind.",
        "counter",
    )
    for kind, count in sorted(events_by_kind.items()):
        lines.append(f'symphony_events_by_kind_total{{kind="{kind}"}} {count}')

    # ---- operator interaction surface ----------------------------------- #
    _emit_help(
        lines, "symphony_hints_total",
        "Operator hints recorded (consumed + pending).", "counter",
    )
    lines.append(f"symphony_hints_total {hint_count}")

    _emit_help(
        lines, "symphony_hints_pending",
        "Operator hints awaiting injection into the next attempt.", "gauge",
    )
    lines.append(f"symphony_hints_pending {hint_pending_count}")

    _emit_help(
        lines, "symphony_priority_overrides",
        "Active drag-to-reorder priority overrides.", "gauge",
    )
    lines.append(f"symphony_priority_overrides {priority_override_count}")

    # ---- persistent retry queue snapshot -------------------------------- #
    _emit_help(
        lines, "symphony_attempts_state_rows",
        "Rows in attempts_state — in-flight attempts that survive restart.",
        "gauge",
    )
    lines.append(f"symphony_attempts_state_rows {active_attempts_state_count}")

    # ---- Tool API idempotency surface ----------------------------------- #
    _emit_help(
        lines, "symphony_idempotency_keys",
        "Outstanding idempotency_keys (may include expired-but-not-cleaned rows).",
        "gauge",
    )
    lines.append(f"symphony_idempotency_keys {idempotency_count}")

    # ---- Phase D3 — NEEDS_HUMAN escalation surface --------------------- #
    # `symphony_attempts_finalised_total{terminal_reason="needs_human"}`
    # already gives the lifetime escalation count via the enum loop above.
    # The two metrics below are operator-facing convenience: how many are
    # waiting on a human RIGHT NOW (gauge) and how many resolutions have
    # been recorded via the resolve_human_block endpoint (counter).
    blocked_for_human_now = sum(
        1
        for att in (
            orchestrator._attempts.values()  # noqa: SLF001
        )
        if att.state == RunState.RELEASED
        and att.terminal_reason == TerminalReason.NEEDS_HUMAN
    )
    _emit_help(
        lines, "symphony_attempts_blocked_for_human",
        "Attempts currently in NEEDS_HUMAN — awaiting a resolve_human_block call.",
        "gauge",
    )
    lines.append(f"symphony_attempts_blocked_for_human {blocked_for_human_now}")

    with bridge._lock:  # noqa: SLF001
        resolved_count = _count_table(
            bridge._db,  # noqa: SLF001
            "hints",
            "author = 'human-resolver'",
        )
    _emit_help(
        lines, "symphony_human_resolutions_total",
        "Hints recorded via Tool API resolve_human_block (lifetime count).",
        "counter",
    )
    lines.append(f"symphony_human_resolutions_total {resolved_count}")

    # Final newline so curl | tail -1 sees a clean cut.
    return "\n".join(lines) + "\n"
