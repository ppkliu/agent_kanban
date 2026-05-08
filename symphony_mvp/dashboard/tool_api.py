"""Coding Service Tool API — Phase A (5 of 6 endpoints).

Exposes a small RESTful surface designed for upstream LLM agents to consume
via OpenAI / Anthropic function calling. Each endpoint maps onto existing
orchestrator + bridge infrastructure; nothing here owns scheduling state.

See ``docs/design/coding-service-tool-api.md`` §11 for the phased roadmap.
Phase A intentionally skips: ``inspect_repo``, per-task ``mode``, idempotency
keys, multi-repo pool — they're tracked as Phase B / C in
``docs/todolist/post-mvp-gaps.md``.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..models import Issue, RunAttempt, RunState, TerminalReason

if TYPE_CHECKING:  # pragma: no cover
    from .server import DashboardAppState

# Imported lazily inside handlers to avoid a circular import on the auth dep.
_REQUIRE_AUTH = None


def _auth_dep(request: Request) -> None:
    """Lazy delegate to server._require_auth so this module doesn't import it
    at top level (server.py imports this module via include_router)."""
    global _REQUIRE_AUTH
    if _REQUIRE_AUTH is None:
        from .server import _require_auth as ra
        _REQUIRE_AUTH = ra
    _REQUIRE_AUTH(request)


tool_router = APIRouter(
    prefix="/api/v1/tools",
    dependencies=[Depends(_auth_dep)],
    tags=["coding-service-tool-api"],
)


# --------------------------------------------------------------------------- #
# Schemas                                                                     #
# --------------------------------------------------------------------------- #

TaskStatus = Literal["pending", "running", "done", "failed", "cancelled"]
TaskStage = Literal["queued", "running", "done", "failed", "cancelled"]


class RepoOut(BaseModel):
    id: str
    name: str
    default_mode: Literal["build"] = "build"
    allowed_modes: list[Literal["plan", "build", "review"]] = Field(
        default_factory=lambda: ["plan", "build", "review"]
    )


class ListReposOut(BaseModel):
    repos: list[RepoOut]


class SubmitTaskIn(BaseModel):
    task: str = Field(
        min_length=1,
        max_length=16_000,
        description=(
            "Self-contained task description with all context. "
            "Bad: 'fix the login bug'. "
            "Good: 'In src/auth/login.ts the rate limiter resets per request "
            "instead of per IP — fix the logic and add a regression test.'"
        ),
    )
    repo: str = Field(
        description=(
            "Registered repo identifier returned by list_repos. "
            "Phase A only supports the in-memory tracker ('memory')."
        )
    )
    files_hint: list[str] | None = Field(
        default=None,
        description="Optional file paths to focus on. Helps the agent skip exploration.",
    )


class SubmitTaskOut(BaseModel):
    task_id: str
    status: Literal["pending"] = "pending"


class CheckStatusIn(BaseModel):
    task_id: str


class CheckStatusOut(BaseModel):
    task_id: str
    status: TaskStatus
    stage: TaskStage
    progress: float = Field(ge=0.0, le=1.0)
    current_turn: int = 0
    max_turns: int
    started_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    next_poll_after_ms: int = 5000


class GetResultIn(BaseModel):
    task_id: str


class FileChange(BaseModel):
    path: str
    change: Literal["added", "modified", "deleted"]


class TaskResultOut(BaseModel):
    task_id: str
    status: Literal["done", "failed", "cancelled"]
    summary: str
    files_changed: list[FileChange] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    detailed_url: str
    cost_usd: float = 0.0
    duration_ms: Optional[int] = None
    terminal_reason: Optional[str] = None


class CancelTaskIn(BaseModel):
    task_id: str
    reason: Optional[str] = None


class CancelTaskOut(BaseModel):
    ok: bool
    was_running: bool
    task_id: str


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


_DONE_REASONS = {
    TerminalReason.AGENT_FINISHED,
    TerminalReason.HANDOFF,
    TerminalReason.TRACKER_TERMINAL,
}


def _state(request: Request) -> "DashboardAppState":
    return request.app.state.symphony


def _public_url(state: "DashboardAppState", task_id: str) -> str:
    """Best-effort dashboard URL for the operator to inspect a task in detail.

    Reads PUBLIC_DASHBOARD_URL env if set; falls back to a localhost URL using
    the orchestrator's known port. Never blocks on network."""
    import os
    base = os.environ.get("PUBLIC_DASHBOARD_URL", "http://localhost:17957").rstrip("/")
    return f"{base}/issues/{task_id}"


def _derive_status(att: Optional[RunAttempt]) -> tuple[TaskStatus, TaskStage]:
    """Map RunAttempt → (status, stage). Phase A keeps stage simple."""
    if att is None:
        return "pending", "queued"
    if att.state == RunState.UNCLAIMED:
        return "pending", "queued"
    if att.state in (RunState.CLAIMED, RunState.RUNNING, RunState.RETRY_QUEUED):
        return "running", "running"
    # RELEASED branch
    reason = att.terminal_reason
    if reason == TerminalReason.ABORTED:
        return "cancelled", "cancelled"
    if reason in _DONE_REASONS:
        return "done", "done"
    return "failed", "failed"


def _derive_progress(att: Optional[RunAttempt], max_turns: int) -> float:
    if att is None:
        return 0.0
    if att.state == RunState.RELEASED:
        return 1.0
    if max_turns <= 0:
        return 0.0
    return min(1.0, att.turns_consumed / max_turns)


def _ms_between(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() * 1000)


def _build_summary_phase_a(history_row: dict[str, Any]) -> str:
    """Phase A summary template. Phase B will replace this with proper
    last-assistant-text extraction from event_records."""
    reason = history_row.get("terminal_reason") or "unknown"
    turns = history_row.get("turns_consumed") or 0
    cost = history_row.get("cost_usd") or 0.0
    state = history_row.get("state") or "released"
    err = history_row.get("error_message")
    parts = [
        f"Task reached {state} after {turns} turn(s); "
        f"terminal reason: {reason}; cost ${cost:.4f}."
    ]
    if err:
        parts.append(f"Error: {err}")
    return " ".join(parts)


def _make_task_issue(body: SubmitTaskIn) -> Issue:
    """Generate an Issue ingestible by InMemoryTracker.add()."""
    task_id = f"tsk_{secrets.token_hex(8)}"
    description = body.task
    if body.files_hint:
        description = f"{body.task}\n\nFocus on: {', '.join(body.files_hint)}"
    now = datetime.now(timezone.utc)
    return Issue(
        id=task_id,
        identifier=task_id,
        title=body.task[:80],
        description=description,
        priority=1,
        state="open",
        branch_name=None,
        url=f"opencode-tool-api://{task_id}",
        labels=["tool-api"],
        blocked_by=[],
        created_at=now,
        updated_at=now,
    )


def _tracker_kind(state: "DashboardAppState") -> str:
    return state.orch.workflow.config.tracker_kind


# --------------------------------------------------------------------------- #
# Endpoints                                                                   #
# --------------------------------------------------------------------------- #


@tool_router.post(
    "/list_repos",
    response_model=ListReposOut,
    summary="List repos this Coding Service can operate on",
    description=(
        "Returns the set of repos the Symphony orchestrator is bound to. "
        "Phase A reports a single repo derived from the active workflow's "
        "tracker config. Call this before submit_coding_task if you don't "
        "know what to pass for `repo`."
    ),
)
def post_list_repos(request: Request) -> ListReposOut:
    state = _state(request)
    cfg = state.orch.workflow.config
    if cfg.tracker_kind == "memory":
        repo = RepoOut(id="memory", name="symphony in-memory tracker")
    else:
        repo_id = f"{cfg.tracker_kind}:{cfg.tracker_repo or 'unknown'}"
        repo = RepoOut(id=repo_id, name=cfg.tracker_repo or repo_id)
    return ListReposOut(repos=[repo])


@tool_router.post(
    "/submit_coding_task",
    response_model=SubmitTaskOut,
    summary="Delegate a coding task and immediately return a task_id (fire-and-check)",
    description=(
        "Creates a new task and returns a task_id within ~1s. Use "
        "check_task_status to poll progress; do NOT wait synchronously. "
        "Phase A only supports the in-memory tracker (`repo='memory'`); "
        "submitting to a GitHub-backed Symphony will return 400 since this "
        "service cannot create real GitHub issues from the tool API."
    ),
)
def post_submit_coding_task(
    body: SubmitTaskIn, request: Request
) -> SubmitTaskOut:
    state = _state(request)
    kind = _tracker_kind(state)
    if kind != "memory":
        raise HTTPException(
            status_code=400,
            detail=(
                f"submit_coding_task is only supported for tracker_kind='memory' "
                f"in Phase A; current tracker is {kind!r}"
            ),
        )
    if body.repo != "memory":
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown repo {body.repo!r}; call list_repos for valid ids"
            ),
        )

    issue = _make_task_issue(body)
    tracker = state.orch.tracker
    if not hasattr(tracker, "add"):  # pragma: no cover — defence in depth
        raise HTTPException(
            status_code=500,
            detail="tracker does not support runtime task ingestion",
        )
    tracker.add(issue)
    return SubmitTaskOut(task_id=issue.id)


@tool_router.post(
    "/check_task_status",
    response_model=CheckStatusOut,
    summary="Poll a task's progress",
    description=(
        "Cheap to call. Respects next_poll_after_ms hint. Stage values are "
        "intentionally coarse in Phase A (queued / running / done / "
        "cancelled / failed); finer-grained stages arrive in Phase B."
    ),
)
def post_check_task_status(
    body: CheckStatusIn, request: Request
) -> CheckStatusOut:
    state = _state(request)
    orch = state.orch
    att = orch._attempts.get(body.task_id)  # noqa: SLF001 — same pattern as existing routes
    status, stage = _derive_status(att)
    cfg = orch.workflow.config
    progress = _derive_progress(att, cfg.max_turns)

    started_at = att.started_at if att else None
    duration_ms: Optional[int] = None
    if started_at and att and att.ended_at:
        duration_ms = _ms_between(started_at, att.ended_at)
    elif started_at and att and att.is_active():
        duration_ms = _ms_between(started_at, datetime.now(timezone.utc))

    return CheckStatusOut(
        task_id=body.task_id,
        status=status,
        stage=stage,
        progress=progress,
        current_turn=att.turns_consumed if att else 0,
        max_turns=cfg.max_turns,
        started_at=started_at,
        duration_ms=duration_ms,
        next_poll_after_ms=2_000 if status == "running" else 30_000,
    )


@tool_router.post(
    "/get_task_result",
    response_model=TaskResultOut,
    summary="Fetch the structured result of a completed task",
    description=(
        "Only callable when status ∈ {done, failed, cancelled}. Returns a "
        "summary, structured file-change list (empty in Phase A — populated "
        "in Phase B), and a `detailed_url` for the operator dashboard. The "
        "full event transcript is intentionally NOT included to avoid "
        "context pollution upstream."
    ),
)
def post_get_task_result(
    body: GetResultIn, request: Request
) -> TaskResultOut:
    state = _state(request)
    history = state.bridge.list_attempt_history(body.task_id)
    if not history:
        # No attempt has finalised yet — caller should poll status instead.
        raise HTTPException(
            status_code=409,
            detail=(
                "task has no finalised attempt yet; call check_task_status "
                "until status is done|failed|cancelled"
            ),
        )

    last = history[-1]
    reason = last.get("terminal_reason")
    if reason == TerminalReason.ABORTED.value:
        outer_status: Literal["done", "failed", "cancelled"] = "cancelled"
    elif reason in {r.value for r in _DONE_REASONS}:
        outer_status = "done"
    else:
        outer_status = "failed"

    started_at = last.get("started_at")
    ended_at = last.get("ended_at")
    duration_ms: Optional[int] = None
    if started_at and ended_at:
        try:
            sa = datetime.fromisoformat(started_at)
            ea = datetime.fromisoformat(ended_at)
            duration_ms = _ms_between(sa, ea)
        except Exception:  # noqa: BLE001
            duration_ms = None

    return TaskResultOut(
        task_id=body.task_id,
        status=outer_status,
        summary=_build_summary_phase_a(last),
        files_changed=[],  # Phase B
        blockers=[],
        follow_ups=[],
        detailed_url=_public_url(state, body.task_id),
        cost_usd=float(last.get("cost_usd") or 0.0),
        duration_ms=duration_ms,
        terminal_reason=reason,
    )


@tool_router.post(
    "/cancel_task",
    response_model=CancelTaskOut,
    summary="Abort a running task",
    description=(
        "Idempotent — calling on an already-completed task returns "
        "was_running=false. Use when a task is taking too long and you'd "
        "rather retry with files_hint, or when the upstream user "
        "interrupts."
    ),
)
def post_cancel_task(
    body: CancelTaskIn, request: Request
) -> CancelTaskOut:
    state = _state(request)
    was_running = state.orch.abort(
        body.task_id, message=body.reason or "operator cancelled via tool api"
    )
    return CancelTaskOut(ok=True, was_running=was_running, task_id=body.task_id)
