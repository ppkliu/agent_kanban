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

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..models import Issue, RunAttempt, RunState, TerminalReason
from .mode import (
    DEFAULT_MODE,
    MODE_LABEL_PREFIX,
    Mode,
    is_valid_mode,
)
from .repo_inspect import inspect_repo as fs_inspect_repo
from .stage import Stage as TranslatedStage, derive_stage
from .task_result import derive_result

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

TaskStatus = Literal[
    "pending",
    "running",
    "done",
    "failed",
    "cancelled",
    "blocked_for_human",  # Phase D3 — agent declared [HUMAN_REQUIRED]
]
TaskStage = Literal[
    "queued",
    "running",
    "exploring_codebase",
    "modifying_files",
    "running_tests",
    "summarising",
    "done",
    "failed",
    "cancelled",
    "blocked_for_human",  # Phase D3
]


class RepoOut(BaseModel):
    id: str
    name: str
    default_mode: Literal["build"] = "build"
    allowed_modes: list[Literal["plan", "build", "review"]] = Field(
        default_factory=lambda: ["plan", "build", "review"]
    )


class ListReposOut(BaseModel):
    repos: list[RepoOut]


class SubTaskSpec(BaseModel):
    """One child of a Path A subtask decomposition. The caller supplies the
    pre-decomposed list; Symphony creates 1 parent + N children with the
    dependency graph stitched at issue-ingestion time."""

    task: str = Field(min_length=1, max_length=16_000)
    depends_on: Optional[list[int]] = Field(
        default=None,
        description=(
            "Sibling indices (0-based) in the parent's `subtasks` array that "
            "must reach a terminal state before this subtask is dispatched. "
            "Only forward references are allowed (idx < this subtask's "
            "position); the server translates indices to real task_ids."
        ),
    )
    mode: Optional[Literal["plan", "build", "review"]] = Field(
        default=None,
        description="Per-child tool whitelist mode. Inherits parent default if unset.",
    )
    files_hint: Optional[list[str]] = Field(
        default=None,
        description="Per-child file focus list. Independent of the parent's hint.",
    )


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
    mode: Optional[Literal["plan", "build", "review"]] = Field(
        default=None,
        description=(
            "Hard tool whitelist for this task. plan = read+grep+bash (no edit); "
            "build = full toolset (default); review = read+grep only. Enforced "
            "via runner allowed_tools, not just prompt suggestion."
        ),
    )
    idempotency_key: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        description=(
            "Optional client-supplied key for safe retry. Same key within "
            "1 hour returns the original task_id without creating a duplicate. "
            "Critical for upstream LLM retries on network blips."
        ),
    )
    parent_task_id: Optional[str] = Field(
        default=None,
        max_length=128,
        description=(
            "If set, the new task is a child of this parent task_id. Encoded "
            "as a 'parent:<id>' label on the Issue so the orchestrator can "
            "gate dispatch when the parent ends in a non-success terminal "
            "state. Use this to model graph-shaped workflows where a high-"
            "level goal fans out into a tree of subtasks."
        ),
    )
    depends_on: Optional[list[str]] = Field(
        default=None,
        description=(
            "Optional list of sibling task_ids that must reach a terminal "
            "state before this task is dispatched. Maps directly to "
            "Issue.blocked_by and is consumed by the existing dispatch gate. "
            "Use this to express dependency order between subtasks under a "
            "shared parent."
        ),
    )
    subtasks: Optional[list[SubTaskSpec]] = Field(
        default=None,
        max_length=50,
        description=(
            "Phase D2a Path A — caller-supplied decomposition. When set, the "
            "outer `task` becomes the parent issue and each entry becomes a "
            "child issue auto-linked with `parent:<id>` label and (if given) "
            "translated sibling `depends_on`. A single `idempotency_key` "
            "covers the whole batch — replays return the original parent id. "
            "Cap: 50 subtasks per submission. Mutually exclusive with "
            "`decompose=true`."
        ),
    )
    decompose: bool = Field(
        default=False,
        description=(
            "Phase D2b Path B — Symphony-side decomposition. When true, the "
            "parent is submitted with `mode=plan` and a `decompose-pending:1` "
            "label; once the plan-mode runner finishes, the backend parses "
            "its final assistant message as a JSON subtask array and creates "
            "the child issues itself (mirrors the Path A fan-out). Mutually "
            "exclusive with explicit `subtasks=[...]`. Disable globally with "
            "env `SYMPHONY_DECOMPOSE_ENABLED=0` for cost-sensitive deploys."
        ),
    )
    project_id: Optional[str] = Field(
        default=None,
        max_length=128,
        description=(
            "Phase E1 multi-project — bind the task to a project so the "
            "dashboard can filter / route by it. Encoded as a `project:<id>` "
            "label on the Issue (same pattern as `parent:` / `trace:` / "
            "`mode:`). Children inherit the parent's project. If omitted, "
            "the auto-created `default` project is used so existing callers "
            "keep working unchanged. Archived projects are rejected (400)."
        ),
    )


class SubmitTaskOut(BaseModel):
    task_id: str
    status: Literal["pending"] = "pending"
    trace_id: str = Field(
        description=(
            "32-hex W3C trace_id bound to this task. Echoed back so upstream "
            "agents can correlate logs across processes. Extracted from the "
            "caller's 'traceparent' HTTP header (W3C TraceContext) when "
            "present; otherwise a fresh trace_id is generated."
        ),
    )


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


class TestRunOut(BaseModel):
    passed: int
    failed: int
    framework: Optional[str] = None


class TaskResultOut(BaseModel):
    task_id: str
    status: Literal["done", "failed", "cancelled"]
    summary: str
    files_changed: list[FileChange] = Field(default_factory=list)
    tests_run: Optional[TestRunOut] = None
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


class ResolveHumanBlockIn(BaseModel):
    task_id: str
    resolution_hint: str = Field(
        min_length=1,
        max_length=8000,
        description=(
            "Operator's response to the agent's escalation. Recorded as a "
            "hint and injected into the next attempt's prompt so the agent "
            "sees what the human decided."
        ),
    )
    author: str = Field(
        default="human-resolver",
        max_length=64,
        description="Display name for the recorded hint (audit trail).",
    )


class ResolveHumanBlockOut(BaseModel):
    ok: bool
    task_id: str
    hint_id: int
    re_queued: bool


class ListTasksIn(BaseModel):
    parent_task_id: Optional[str] = Field(
        default=None,
        description=(
            "If set, only return tasks whose 'parent:<id>' label matches. "
            "Pass the parent's task_id to enumerate its direct children "
            "(siblings of one parent). Unset = return every Tool API task."
        ),
    )
    status: Optional[TaskStatus] = Field(
        default=None,
        description=(
            "If set, only return tasks whose derived status matches. Useful "
            "for an upstream agent that wants to scan 'what's still running' "
            "or 'what failed' without polling each task_id individually."
        ),
    )
    project_id: Optional[str] = Field(
        default=None,
        description=(
            "Phase E1 — if set, only return tasks whose 'project:<id>' "
            "label matches. Combine with `parent_task_id` to walk one "
            "parent's children within a project."
        ),
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=500,
        description="Cap on number of tasks returned. Default 100, max 500.",
    )


class TaskSummaryOut(BaseModel):
    task_id: str
    title: str
    status: TaskStatus
    parent_task_id: Optional[str] = None
    project_id: Optional[str] = None
    depends_on: list[str] = Field(default_factory=list)
    mode: Optional[Literal["plan", "build", "review"]] = None
    created_at: datetime


class ListTasksOut(BaseModel):
    tasks: list[TaskSummaryOut]


class WorkflowResultIn(BaseModel):
    task_id: str = Field(
        description=(
            "Parent task id. Use the `task_id` returned by "
            "`submit_coding_task` (Path A) or by the decompose-pending "
            "parent (Path B). The rollup walks one level — direct "
            "children only — since both fan-out paths produce flat "
            "subtask graphs."
        ),
    )


class WorkflowChildResult(BaseModel):
    task_id: str
    title: str
    status: TaskStatus
    terminal_reason: Optional[str] = None
    summary: Optional[str] = None
    cost_usd: float = 0.0
    duration_ms: Optional[int] = None


class WorkflowAggregate(BaseModel):
    cost_usd: float = 0.0
    duration_ms: Optional[int] = None
    files_changed: list[FileChange] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per-status counts across parent + children. Keys are the same "
            "literals as TaskStatus. Useful for upstream agents to render "
            "a progress bar without iterating the children list."
        ),
    )


class WorkflowParentResult(BaseModel):
    task_id: str
    title: str
    status: TaskStatus
    terminal_reason: Optional[str] = None
    summary: Optional[str] = None
    cost_usd: float = 0.0
    duration_ms: Optional[int] = None


class WorkflowResultOut(BaseModel):
    task_id: str
    status: TaskStatus = Field(
        description=(
            "Rolled-up status across parent + children. Precedence: "
            "`blocked_for_human` > `failed` > `running` (incl. pending) > "
            "`cancelled` (only when ALL are cancelled) > `done` (only when "
            "ALL are done). Callers can poll the same endpoint until "
            "status terminalises rather than juggling two endpoints."
        ),
    )
    trace_id: Optional[str] = None
    detailed_url: str
    parent: WorkflowParentResult
    children: list[WorkflowChildResult] = Field(default_factory=list)
    aggregate: WorkflowAggregate


class InspectRepoIn(BaseModel):
    repo: str = Field(description="Repo id returned by list_repos")


class AgentsDocOut(BaseModel):
    filename: str
    content: str


class InspectRepoOut(BaseModel):
    repo: str
    agents_md: Optional[AgentsDocOut] = None
    structure: str = ""
    languages: list[str] = Field(default_factory=list)
    file_count: int = 0
    default_mode: Literal["build"] = "build"
    allowed_modes: list[Literal["plan", "build", "review"]] = Field(
        default_factory=lambda: ["plan", "build", "review"]
    )


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


_DONE_REASONS = {
    TerminalReason.AGENT_FINISHED,
    TerminalReason.HANDOFF,
    TerminalReason.TRACKER_TERMINAL,
}

# W3C TraceContext: ``00-<32hex trace_id>-<16hex parent_id>-<2hex flags>``.
# Symphony doesn't emit spans yet, so only the trace_id portion is load-bearing.
_TRACEPARENT_RE = re.compile(
    r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$",
    re.IGNORECASE,
)
_TRACE_LABEL_PREFIX = "trace:"
_PARENT_LABEL_PREFIX = "parent:"
_PROJECT_LABEL_PREFIX = "project:"
_DECOMPOSE_PENDING_LABEL = "decompose-pending:1"


def _parse_or_generate_trace_id(traceparent: str | None) -> str:
    """Extract the trace_id from a W3C ``traceparent`` header, or mint a
    fresh 32-hex id if the header is missing or malformed."""
    if traceparent:
        m = _TRACEPARENT_RE.match(traceparent.strip())
        if m:
            return m.group(1).lower()
    return secrets.token_hex(16)


def _extract_trace_id_from_labels(labels: list[str]) -> str | None:
    for label in labels:
        if label.startswith(_TRACE_LABEL_PREFIX):
            return label[len(_TRACE_LABEL_PREFIX):]
    return None


def _extract_parent_id_from_labels(labels: list[str]) -> str | None:
    for label in labels:
        if label.startswith(_PARENT_LABEL_PREFIX):
            return label[len(_PARENT_LABEL_PREFIX):]
    return None


def _extract_project_id_from_labels(labels: list[str]) -> str | None:
    for label in labels:
        if label.startswith(_PROJECT_LABEL_PREFIX):
            return label[len(_PROJECT_LABEL_PREFIX):]
    return None


def _rollup_status(statuses: list[TaskStatus]) -> TaskStatus:
    """Phase D5 — collapse a list of per-task statuses into one rollup.

    Precedence (most actionable wins): blocked_for_human > failed >
    running (incl. pending) > cancelled (only when ALL are cancelled) >
    done (only when ALL are done). Empty input degenerates to ``pending``
    so callers see a stable shape before any work has been observed."""
    if not statuses:
        return "pending"
    if "blocked_for_human" in statuses:
        return "blocked_for_human"
    if "failed" in statuses:
        return "failed"
    if "running" in statuses or "pending" in statuses:
        return "running"
    if all(s == "cancelled" for s in statuses):
        return "cancelled"
    if all(s == "done" for s in statuses):
        return "done"
    # Mixed terminal states (e.g. one cancelled + one done): degrade to
    # ``failed`` so the operator sees that the workflow did not fully
    # succeed.
    return "failed"


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
    """Map RunAttempt → (status, fallback_stage). The fallback stage is only
    used when no event in the attempt's stream implies a finer-grained stage
    (see stage.derive_stage)."""
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
    if reason == TerminalReason.NEEDS_HUMAN:
        return "blocked_for_human", "blocked_for_human"
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


def _make_task_issue(body: SubmitTaskIn, trace_id: str) -> Issue:
    """Generate an Issue ingestible by InMemoryTracker.add().

    Encodes the requested ``mode`` (if any) as a ``mode:<value>`` label,
    the W3C ``trace_id`` as a ``trace:<32hex>`` label, and (if set) the
    ``parent_task_id`` as a ``parent:<id>`` label so the orchestrator can
    read all three off ``issue.labels`` at dispatch time without threading
    extra positional args through the runner chain. ``depends_on`` maps
    straight to ``Issue.blocked_by`` so the existing dispatch gate handles
    dependency ordering with no orchestrator changes.
    """
    task_id = f"tsk_{secrets.token_hex(8)}"
    description = body.task
    if body.files_hint:
        description = f"{body.task}\n\nFocus on: {', '.join(body.files_hint)}"
    # Phase D2b — Path B parent gets the decomposition system prompt
    # prepended so the plan-mode runner sees the right "emit a JSON
    # subtask array" instructions, plus the `decompose-pending:1` marker
    # label that triggers the post-finalize hook to parse + fan out.
    effective_mode = body.mode
    extra_labels: list[str] = []
    if body.decompose:
        from .decompose import DECOMPOSITION_SYSTEM_PROMPT  # noqa: PLC0415
        description = f"{DECOMPOSITION_SYSTEM_PROMPT}\n\n---\n{description}"
        effective_mode = "plan"
        extra_labels.append(_DECOMPOSE_PENDING_LABEL)
    labels = ["tool-api", f"{_TRACE_LABEL_PREFIX}{trace_id}"]
    if effective_mode is not None:
        labels.append(f"{MODE_LABEL_PREFIX}{effective_mode}")
    if body.parent_task_id:
        labels.append(f"{_PARENT_LABEL_PREFIX}{body.parent_task_id}")
    if body.project_id:
        labels.append(f"{_PROJECT_LABEL_PREFIX}{body.project_id}")
    labels.extend(extra_labels)
    blocked_by = list(body.depends_on) if body.depends_on else []
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
        labels=labels,
        blocked_by=blocked_by,
        created_at=now,
        updated_at=now,
    )


def _fan_out_children(
    *,
    parent_id: str,
    specs: list[dict[str, Any]],
    tracker: Any,
    trace_id: str,
    project_id: str | None,
    repo: str,
) -> list[str]:
    """Create N child issues under a parent, mirroring the Path A loop.

    Each ``spec`` is a plain dict with ``task`` (str, required),
    ``depends_on`` (list[int] of sibling indices, optional), ``mode``
    (one of plan/build/review, optional), and ``files_hint`` (list[str],
    optional). Sibling indices are translated to real task_ids using the
    creation order. Children inherit the supplied ``trace_id`` and
    ``project_id`` so per-project filtering keeps working downstream.

    Returns the list of created child task_ids. Tracker.add is invoked
    one child at a time so partial fan-out is observable if a later add
    raises (the previous children stay in the tracker).
    """
    child_task_ids: list[str] = []
    for spec in specs:
        depends_on_ids = [
            child_task_ids[idx] for idx in (spec.get("depends_on") or [])
        ]
        child_body = SubmitTaskIn(
            task=spec["task"],
            repo=repo,
            files_hint=spec.get("files_hint"),
            mode=spec.get("mode"),
            parent_task_id=parent_id,
            depends_on=depends_on_ids,
            project_id=project_id,
        )
        child_issue = _make_task_issue(child_body, trace_id=trace_id)
        tracker.add(child_issue)
        child_task_ids.append(child_issue.id)
    return child_task_ids


def _tracker_kind(state: "DashboardAppState") -> str:
    return state.orch.workflow.config.tracker_kind


def _decompose_enabled() -> bool:
    """Phase D2b kill switch. Default = enabled. Set
    ``SYMPHONY_DECOMPOSE_ENABLED=0`` (or ``false`` / ``no``) on the server
    to refuse all ``decompose=true`` submissions with a 400.
    """
    import os
    raw = os.environ.get("SYMPHONY_DECOMPOSE_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _submit_rate_limit_per_minute() -> int:
    """Read the global submit-rate-limit env var. 0 (or unset, or unparseable)
    means unlimited. Read every request so operators can re-tune without
    restarting the dashboard."""
    import os
    raw = os.environ.get("SYMPHONY_SUBMIT_RATE_LIMIT_PER_MINUTE", "0")
    try:
        n = int(raw)
    except ValueError:
        return 0
    return n if n > 0 else 0


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
    "/inspect_repo",
    response_model=InspectRepoOut,
    summary="Read AGENTS.md + tree summary + language breakdown for a repo",
    description=(
        "Cheap, read-only — no agent is spawned. Returns the repo's AGENTS.md "
        "(or CONTRIBUTING.md / README.md fallback), a short tree summary, and "
        "a language breakdown. **Call this before submit_coding_task for any "
        "non-trivial work** so your task description matches the project's "
        "conventions. Tree depth and entry count are capped to keep the "
        "response token-light."
    ),
)
def post_inspect_repo(body: InspectRepoIn, request: Request) -> InspectRepoOut:
    state = _state(request)
    cfg = state.orch.workflow.config

    # Phase A only knows one repo. Validate the id matches.
    if cfg.tracker_kind == "memory":
        valid_id = "memory"
    else:
        valid_id = f"{cfg.tracker_kind}:{cfg.tracker_repo or 'unknown'}"
    if body.repo != valid_id:
        raise HTTPException(
            status_code=400,
            detail=f"unknown repo {body.repo!r}; call list_repos for valid ids",
        )

    workspace_root = Path(cfg.workspace_root).expanduser()
    inspection = fs_inspect_repo(workspace_root, repo_id=body.repo)

    agents_out: AgentsDocOut | None = None
    if inspection.agents_md is not None:
        agents_out = AgentsDocOut(
            filename=inspection.agents_md.filename,
            content=inspection.agents_md.content,
        )
    return InspectRepoOut(
        repo=inspection.repo,
        agents_md=agents_out,
        structure=inspection.structure,
        languages=inspection.languages,
        file_count=inspection.file_count,
    )


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

    # Phase D2b — `decompose=true` validation.
    if body.decompose:
        if body.subtasks:
            raise HTTPException(
                status_code=400,
                detail=(
                    "subtasks=[...] and decompose=true are mutually exclusive; "
                    "use one or the other (caller-supplied list vs Symphony "
                    "plan-mode runner)"
                ),
            )
        if _decompose_enabled() is False:
            raise HTTPException(
                status_code=400,
                detail=(
                    "decomposition disabled by operator "
                    "(SYMPHONY_DECOMPOSE_ENABLED=0)"
                ),
            )

    # Phase E1 — resolve project_id: explicit, or auto-default. Archived
    # projects reject new tasks (400). The same project_id is propagated to
    # child subtasks via the fan-out path so the whole graph shares it.
    if hasattr(state.bridge, "ensure_default_project"):
        if body.project_id is None:
            body.project_id = state.bridge.ensure_default_project()
        else:
            proj = state.bridge.get_project(body.project_id)
            if proj is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"unknown project_id {body.project_id!r}; "
                        f"POST /api/v1/projects to create one first"
                    ),
                )
            if proj.get("archived_at") is not None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"project {body.project_id!r} is archived and does "
                        f"not accept new tasks"
                    ),
                )

    # Idempotency short-circuit: if the caller sent a key we've seen before
    # within TTL, return the original task_id instead of creating a duplicate.
    # Runs BEFORE the rate limit check — replaying a client retry shouldn't
    # cost the caller their quota. The trace_id returned is the one the
    # *original* task was created with, so the caller's log correlation
    # continues to point at the same task across retries.
    if body.idempotency_key and hasattr(state.bridge, "lookup_idempotency_key"):
        prior = state.bridge.lookup_idempotency_key(body.idempotency_key)
        if prior is not None:
            prior_trace: str | None = None
            tracker_obj = state.orch.tracker
            if hasattr(tracker_obj, "all"):
                for issue in tracker_obj.all():
                    if issue.id == prior:
                        prior_trace = _extract_trace_id_from_labels(issue.labels)
                        break
            return SubmitTaskOut(
                task_id=prior,
                trace_id=prior_trace or secrets.token_hex(16),
            )

    trace_id = _parse_or_generate_trace_id(request.headers.get("traceparent"))

    # Global rate limit (Phase C quota). Unset / 0 = unlimited.
    cap = _submit_rate_limit_per_minute()
    if cap > 0 and hasattr(state.bridge, "count_submits_since"):
        from datetime import datetime, timedelta, timezone
        window_seconds = 60
        since = (
            datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        ).isoformat()
        recent = state.bridge.count_submits_since(since)
        if recent >= cap:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"submit rate limit exceeded: {recent}/{cap} in last "
                    f"{window_seconds}s — wait and retry"
                ),
                headers={"Retry-After": str(window_seconds)},
            )

    tracker = state.orch.tracker
    if not hasattr(tracker, "add"):  # pragma: no cover — defence in depth
        raise HTTPException(
            status_code=500,
            detail="tracker does not support runtime task ingestion",
        )

    # Phase D2a Path A — caller-supplied decomposition: create 1 parent issue
    # + N children, all sharing one trace_id and linked via parent:<id> +
    # translated sibling depends_on. Falls through to single-task path when
    # `subtasks` is absent or empty.
    if body.subtasks:
        # Validate forward-only sibling references before we mutate any state.
        for i, spec in enumerate(body.subtasks):
            for idx in spec.depends_on or []:
                if idx < 0 or idx >= i:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"subtasks[{i}].depends_on[{idx}] must reference "
                            f"a prior sibling (0..{i - 1}); forward-only "
                            f"references prevent cycles"
                        ),
                    )

        parent_issue = _make_task_issue(body, trace_id=trace_id)
        tracker.add(parent_issue)

        _fan_out_children(
            parent_id=parent_issue.id,
            specs=[
                {
                    "task": s.task,
                    "depends_on": s.depends_on,
                    "mode": s.mode,
                    "files_hint": s.files_hint,
                }
                for s in body.subtasks
            ],
            tracker=tracker,
            trace_id=trace_id,
            project_id=body.project_id,
            repo=body.repo,
        )

        if body.idempotency_key and hasattr(state.bridge, "record_idempotency_key"):
            state.bridge.record_idempotency_key(body.idempotency_key, parent_issue.id)
        if hasattr(state.bridge, "record_submit"):
            state.bridge.record_submit(parent_issue.id)

        return SubmitTaskOut(task_id=parent_issue.id, trace_id=trace_id)

    issue = _make_task_issue(body, trace_id=trace_id)
    tracker.add(issue)

    # Persist the idempotency mapping AFTER the issue is in the tracker, so
    # we never advertise a task_id whose underlying issue doesn't exist.
    if body.idempotency_key and hasattr(state.bridge, "record_idempotency_key"):
        state.bridge.record_idempotency_key(body.idempotency_key, issue.id)

    # Log the submit for the rate-limiter's window count + ops observability.
    if hasattr(state.bridge, "record_submit"):
        state.bridge.record_submit(issue.id)

    return SubmitTaskOut(task_id=issue.id, trace_id=trace_id)


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
    status, fallback_stage = _derive_status(att)
    # For active attempts, sniff the most recent events to surface a finer
    # stage label (Phase B). Terminal attempts keep their status-derived stage.
    stage: TaskStage = fallback_stage
    if status == "running" and att is not None:
        events = state.bridge.fetch_events(
            body.task_id, attempt_number=att.attempt_number, limit=200
        )
        stage = derive_stage(events, fallback_stage)  # type: ignore[arg-type]
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

    fallback_summary = _build_summary_phase_a(last)
    events = state.bridge.fetch_events(
        body.task_id, attempt_number=last.get("attempt_number"), limit=2000
    )
    derived = derive_result(last, events, fallback_summary=fallback_summary)
    files_out = [
        FileChange(path=fc.path, change=fc.change)  # type: ignore[arg-type]
        for fc in derived.files_changed
    ]
    tests_out: TestRunOut | None = None
    if derived.tests_run is not None:
        tests_out = TestRunOut(
            passed=derived.tests_run.passed,
            failed=derived.tests_run.failed,
            framework=derived.tests_run.framework,
        )

    return TaskResultOut(
        task_id=body.task_id,
        status=outer_status,
        summary=derived.summary,
        files_changed=files_out,
        tests_run=tests_out,
        blockers=derived.blockers,
        follow_ups=derived.follow_ups,
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


@tool_router.post(
    "/resolve_human_block",
    response_model=ResolveHumanBlockOut,
    summary="Unblock a task the agent escalated via [HUMAN_REQUIRED]",
    description=(
        "Operator resolution for a task whose agent ended in "
        "`terminal_reason: needs_human`. Records the supplied resolution "
        "as a hint (visible to the next attempt's prompt) and force-retries "
        "the attempt so the agent picks up where it left off, this time "
        "with the human's answer in context. Returns 404 if the task "
        "doesn't exist; 409 if the task is not in the `blocked_for_human` "
        "state (caller used the wrong endpoint)."
    ),
)
def post_resolve_human_block(
    body: ResolveHumanBlockIn, request: Request
) -> ResolveHumanBlockOut:
    state = _state(request)
    att = state.orch._attempts.get(body.task_id)  # noqa: SLF001
    if att is None:
        raise HTTPException(
            status_code=404,
            detail=f"no attempt for task_id {body.task_id!r}",
        )
    if att.state != RunState.RELEASED or att.terminal_reason != TerminalReason.NEEDS_HUMAN:
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {body.task_id!r} is not blocked_for_human "
                f"(state={att.state.value}, terminal_reason="
                f"{att.terminal_reason.value if att.terminal_reason else None})"
            ),
        )
    hint_id = state.bridge.add_hint(
        body.task_id, body.author, body.resolution_hint
    )
    re_queued = state.orch.force_retry(body.task_id)
    # Phase D3 telemetry: the hint we just recorded with author=
    # "human-resolver" is what `symphony_human_resolutions_total` counts —
    # no extra side table needed.
    return ResolveHumanBlockOut(
        ok=True,
        task_id=body.task_id,
        hint_id=hint_id,
        re_queued=re_queued,
    )


@tool_router.post(
    "/list_tasks",
    response_model=ListTasksOut,
    summary="Enumerate Tool API tasks with optional parent + status filter",
    description=(
        "Returns a flat list of Tool API tasks (those carrying the "
        "`tool-api` label) sorted by created_at ascending. Useful for an "
        "upstream agent to walk a parent's child graph (`parent_task_id`), "
        "scan only-failed tasks (`status=failed`), or page through recent "
        "submissions. This is a read-only endpoint and does not spawn an "
        "agent or mutate state."
    ),
)
def post_list_tasks(body: ListTasksIn, request: Request) -> ListTasksOut:
    state = _state(request)
    tracker = state.orch.tracker
    if not hasattr(tracker, "all"):  # pragma: no cover — defence in depth
        return ListTasksOut(tasks=[])

    summaries: list[TaskSummaryOut] = []
    parent_filter = body.parent_task_id
    status_filter = body.status
    project_filter = body.project_id
    mode_prefix = MODE_LABEL_PREFIX

    for issue in tracker.all():
        if "tool-api" not in issue.labels:
            continue
        issue_parent = _extract_parent_id_from_labels(issue.labels)
        if parent_filter is not None and issue_parent != parent_filter:
            continue
        issue_project = _extract_project_id_from_labels(issue.labels)
        if project_filter is not None and issue_project != project_filter:
            continue
        att = state.orch._attempts.get(issue.id)  # noqa: SLF001
        derived_status, _ = _derive_status(att)
        if status_filter is not None and derived_status != status_filter:
            continue
        issue_mode: Optional[str] = None
        for label in issue.labels:
            if label.startswith(mode_prefix):
                issue_mode = label[len(mode_prefix):]
                break
        summaries.append(
            TaskSummaryOut(
                task_id=issue.id,
                title=issue.title,
                status=derived_status,
                parent_task_id=issue_parent,
                project_id=issue_project,
                depends_on=list(issue.blocked_by),
                mode=issue_mode,  # type: ignore[arg-type]
                created_at=issue.created_at,
            )
        )

    summaries.sort(key=lambda s: s.created_at)
    return ListTasksOut(tasks=summaries[: body.limit])


def _task_result_fields(
    issue: Issue, att: Optional[RunAttempt], bridge
) -> tuple[TaskStatus, Optional[str], Optional[str], float, Optional[int], list, list, list]:
    """Read one task's per-attempt history + derive (status, terminal_reason,
    summary, cost_usd, duration_ms, files_changed, blockers, follow_ups).
    Used by the workflow rollup; mirrors what ``get_task_result`` returns for
    a single task without going through HTTP/pydantic."""
    status, _ = _derive_status(att)
    history = bridge.list_attempt_history(issue.id) if bridge is not None else []
    if not history:
        return status, None, None, 0.0, None, [], [], []
    last = history[-1]
    reason = last.get("terminal_reason")
    started = last.get("started_at")
    ended = last.get("ended_at")
    duration_ms: Optional[int] = None
    if started and ended:
        try:
            duration_ms = _ms_between(
                datetime.fromisoformat(started), datetime.fromisoformat(ended)
            )
        except Exception:  # noqa: BLE001
            duration_ms = None
    fallback = _build_summary_phase_a(last)
    events = bridge.fetch_events(
        issue.id, attempt_number=last.get("attempt_number"), limit=2000
    )
    derived = derive_result(last, events, fallback_summary=fallback)
    return (
        status,
        reason,
        derived.summary,
        float(last.get("cost_usd") or 0.0),
        duration_ms,
        list(derived.files_changed),
        list(derived.blockers),
        list(derived.follow_ups),
    )


@tool_router.post(
    "/get_workflow_result",
    response_model=WorkflowResultOut,
    summary="Aggregate one parent task + its direct children into one rollup",
    description=(
        "Phase D5. Walks the parent task + its direct children (one level — "
        "both Path A and D2b produce flat subtask graphs) and returns a single "
        "rollup: parent + children summaries, per-status counts, summed cost, "
        "wall-clock duration, deduplicated files_changed / blockers / "
        "follow_ups. Status precedence: blocked_for_human > failed > running "
        "(incl. pending) > cancelled (only when ALL are cancelled) > done "
        "(only when ALL are done). Partial rollups are returned — callers "
        "poll this same endpoint until `status` terminalises rather than "
        "juggling two endpoints. 404 only when `task_id` is unknown."
    ),
)
def post_get_workflow_result(
    body: WorkflowResultIn, request: Request
) -> WorkflowResultOut:
    state = _state(request)
    tracker = state.orch.tracker
    bridge = state.bridge

    parent = next(
        (i for i in tracker.all() if i.id == body.task_id), None
    ) if hasattr(tracker, "all") else None
    if parent is None:
        raise HTTPException(
            status_code=404,
            detail=f"task_id not found: {body.task_id}",
        )

    parent_att = state.orch._attempts.get(parent.id)  # noqa: SLF001
    (
        parent_status,
        parent_reason,
        parent_summary,
        parent_cost,
        parent_duration_ms,
        parent_files,
        parent_blockers,
        parent_followups,
    ) = _task_result_fields(parent, parent_att, bridge)

    parent_out = WorkflowParentResult(
        task_id=parent.id,
        title=parent.title,
        status=parent_status,
        terminal_reason=parent_reason,
        summary=parent_summary,
        cost_usd=parent_cost,
        duration_ms=parent_duration_ms,
    )

    children_out: list[WorkflowChildResult] = []
    agg_files_by_path: dict[str, FileChange] = {
        fc.path: FileChange(path=fc.path, change=fc.change)  # type: ignore[arg-type]
        for fc in parent_files
    }
    agg_blockers: list[str] = list(parent_blockers)
    agg_followups: list[str] = list(parent_followups)
    starts: list[str] = []
    ends: list[str] = []
    total_cost = parent_cost

    history = bridge.list_attempt_history(parent.id) if bridge is not None else []
    if history:
        last = history[-1]
        if last.get("started_at"):
            starts.append(last["started_at"])
        if last.get("ended_at"):
            ends.append(last["ended_at"])

    if hasattr(tracker, "all"):
        for child in tracker.all():
            child_parent = _extract_parent_id_from_labels(child.labels)
            if child_parent != parent.id:
                continue
            child_att = state.orch._attempts.get(child.id)  # noqa: SLF001
            (
                c_status,
                c_reason,
                c_summary,
                c_cost,
                c_duration_ms,
                c_files,
                c_blockers,
                c_followups,
            ) = _task_result_fields(child, child_att, bridge)
            children_out.append(
                WorkflowChildResult(
                    task_id=child.id,
                    title=child.title,
                    status=c_status,
                    terminal_reason=c_reason,
                    summary=c_summary,
                    cost_usd=c_cost,
                    duration_ms=c_duration_ms,
                )
            )
            for fc in c_files:
                agg_files_by_path[fc.path] = FileChange(
                    path=fc.path, change=fc.change,  # type: ignore[arg-type]
                )
            for b in c_blockers:
                if b not in agg_blockers:
                    agg_blockers.append(b)
            for f in c_followups:
                if f not in agg_followups:
                    agg_followups.append(f)
            total_cost += c_cost
            ch_history = bridge.list_attempt_history(child.id) if bridge is not None else []
            if ch_history:
                last = ch_history[-1]
                if last.get("started_at"):
                    starts.append(last["started_at"])
                if last.get("ended_at"):
                    ends.append(last["ended_at"])

    children_out.sort(key=lambda c: c.task_id)

    statuses: list[TaskStatus] = [parent_status] + [c.status for c in children_out]
    rollup = _rollup_status(statuses)
    counts: dict[str, int] = {}
    for s in statuses:
        counts[s] = counts.get(s, 0) + 1

    duration_ms: Optional[int] = None
    if starts and ends:
        try:
            duration_ms = _ms_between(
                min(datetime.fromisoformat(s) for s in starts),
                max(datetime.fromisoformat(e) for e in ends),
            )
        except Exception:  # noqa: BLE001
            duration_ms = None

    return WorkflowResultOut(
        task_id=parent.id,
        status=rollup,
        trace_id=_extract_trace_id_from_labels(parent.labels),
        detailed_url=_public_url(state, parent.id),
        parent=parent_out,
        children=children_out,
        aggregate=WorkflowAggregate(
            cost_usd=total_cost,
            duration_ms=duration_ms,
            files_changed=list(agg_files_by_path.values()),
            blockers=agg_blockers,
            follow_ups=agg_followups,
            counts=counts,
        ),
    )
