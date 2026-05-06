"""Core data models — normalized issue records and run state.

These are the stable shapes that flow through Symphony regardless of the
underlying tracker. SPEC.md Section 4.1.1 fixes the issue field set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class RunState(str, Enum):
    """Symphony's *internal* scheduler state — independent from tracker state."""

    UNCLAIMED = "unclaimed"
    CLAIMED = "claimed"
    RUNNING = "running"
    RETRY_QUEUED = "retry_queued"
    RELEASED = "released"  # Terminal — agent reached handoff or done


class TerminalReason(str, Enum):
    """Why a run reached a terminal state. Distinct reasons matter for retry logic."""

    HANDOFF = "handoff"  # Reached workflow-defined handoff state (e.g. Human Review)
    TRACKER_TERMINAL = "tracker_terminal"  # Issue moved to Done/Cancelled externally
    AGENT_FINISHED = "agent_finished"  # Agent ended its session naturally
    MAX_TURNS = "max_turns"  # Hit agent.max_turns cap
    STALL_TIMEOUT = "stall_timeout"  # No agent event within stall_timeout_ms
    USER_INPUT_REQUIRED = "user_input_required"  # SPEC: hard failure (high-trust default)
    ERROR = "error"  # Subprocess failure, transport error, etc.
    ABORTED = "aborted"  # Operator cancelled via dashboard. Skips retry queue.


@dataclass(frozen=True)
class Issue:
    """Normalized issue record — SPEC.md Section 4.1.1 fields.

    Frozen so that orchestrator state stays sane when issues are passed around.
    Use Issue.with_state(...) to produce an updated copy after reconciliation.
    """

    id: str  # Stable internal ID (UUID for Linear, "owner/repo#42" for GitHub)
    identifier: str  # Human ticket key — "MT-625", "#42"
    title: str
    description: str
    priority: int  # 1 = highest. SPEC sorts ascending.
    state: str  # Tracker-reported state name — "Todo", "In Progress", etc.
    branch_name: str | None  # Suggested branch (Linear gives this; GitHub: derived)
    url: str
    labels: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)  # IDs of blocking issues
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def with_state(self, new_state: str) -> "Issue":
        from dataclasses import replace
        return replace(self, state=new_state, updated_at=datetime.now(timezone.utc))

    def to_template_context(
        self, attempt: int, hints: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """SPEC: convert keys to strings, preserve nested structures, expose `attempt`.

        `hints` is an opt-in dashboard extension: a list of operator-supplied
        prompt supplements (each dict carries id/author/content/created_at).
        Defaults to [] so existing templates that don't reference {{ hints }}
        keep rendering unchanged.
        """
        return {
            "issue": {
                "id": self.id,
                "identifier": self.identifier,
                "title": self.title,
                "description": self.description,
                "priority": self.priority,
                "state": self.state,
                "branch_name": self.branch_name or "",
                "url": self.url,
                "labels": list(self.labels),
                "blocked_by": list(self.blocked_by),
            },
            "attempt": attempt,
            "hints": list(hints) if hints else [],
        }


@dataclass
class Workspace:
    """Filesystem workspace assigned to one issue identifier.

    SPEC hard invariants:
      1. Subprocess cwd MUST equal workspace_path
      2. workspace_path normalized absolute MUST sit inside workspace.root
      3. Workspace key MUST be sanitized to [A-Za-z0-9._-]
    """

    issue_id: str
    issue_identifier: str
    path: str  # Absolute path on disk
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RunAttempt:
    """One execution attempt for one issue."""

    issue_id: str
    attempt_number: int  # 1-indexed
    state: RunState = RunState.UNCLAIMED
    started_at: datetime | None = None
    ended_at: datetime | None = None
    terminal_reason: TerminalReason | None = None
    last_event_at: datetime | None = None  # For stall detection
    session_id: str | None = None  # Claude session ID, for resume
    turns_consumed: int = 0
    error_message: str | None = None
    retry_after: datetime | None = None  # For RETRY_QUEUED state
    paused_until: datetime | None = None  # Dashboard cooperative pause sentinel
    cost_usd: float = 0.0  # Cumulative — summed from TURN_COMPLETED.cost_usd

    def is_active(self) -> bool:
        return self.state in (RunState.CLAIMED, RunState.RUNNING)

    def is_terminal(self) -> bool:
        return self.state == RunState.RELEASED

    def mark_event(self) -> None:
        """Update last_event_at — called on every agent event for stall detection."""
        self.last_event_at = datetime.now(timezone.utc)
