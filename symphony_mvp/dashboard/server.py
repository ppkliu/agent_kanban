"""FastAPI app for the Symphony dashboard.

The app is constructed via :func:`create_app(orchestrator, bridge, workflow_path)`
so tests can inject fakes without spinning up a real orchestrator. The CLI
entrypoint in ``__main__.py`` builds the real wiring.

Per spec §5 (REST + WebSocket API), routes implemented here:
  GET    /api/v1/state             — state snapshot, 5-column kanban
  GET    /api/v1/issues/{id}       — single issue detail + history
  POST   /api/v1/issues/{id}/hint  — attach operator hint
  POST   /api/v1/issues/{id}/pause
  POST   /api/v1/issues/{id}/resume
  POST   /api/v1/issues/{id}/abort
  POST   /api/v1/issues/{id}/retry
  POST   /api/v1/priority          — drag-reorder
  PATCH  /api/v1/config            — runtime knobs (whitelist)
  GET    /api/v1/workflow          — current WORKFLOW.md text + parsed config
  PUT    /api/v1/workflow          — write + reload (last-known-good)
  WS     /api/v1/events            — live event stream

Bearer token: when env ``DASHBOARD_API_KEY`` is set (or ``api_key`` is passed
to :func:`create_app`), every REST route and the WebSocket require
``Authorization: Bearer <key>``. When unset, the dashboard runs open (suitable
for localhost dev only).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..agent_runner import AgentEvent
from ..models import Issue, RunAttempt, RunState
from ..orchestrator import Orchestrator
from ..workflow import Workflow, load_workflow, render_prompt
from .bridge import DashboardBridge

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App state container
# ---------------------------------------------------------------------------


class DashboardAppState:
    """Stuff the FastAPI ``app.state`` carries.

    Held as a small dataclass-ish object so route handlers can grab references
    via ``request.app.state.symphony``.
    """

    def __init__(
        self,
        orchestrator: Orchestrator,
        bridge: DashboardBridge,
        *,
        workflow_path: Optional[Path] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.orch = orchestrator
        self.bridge = bridge
        self.workflow_path = Path(workflow_path) if workflow_path else None
        self.api_key = api_key
        self.last_workflow_load_error: Optional[str] = None
        # Generic broadcast hooks (config_changed / workflow_reloaded).
        # The WS handler installs an entry; PATCH/PUT routes call broadcast().
        self._broadcasters: list[Any] = []

    def add_broadcaster(self, fn: Any) -> None:
        self._broadcasters.append(fn)

    def remove_broadcaster(self, fn: Any) -> None:
        try:
            self._broadcasters.remove(fn)
        except ValueError:
            pass

    def broadcast(self, payload: dict[str, Any]) -> None:
        for fn in list(self._broadcasters):
            try:
                fn(payload)
            except Exception:  # noqa: BLE001
                logger.exception("broadcaster raised")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _require_auth(request: Request) -> None:
    """Bearer token check. No-op when api_key is unset (open mode)."""
    state: DashboardAppState = request.app.state.symphony
    if not state.api_key:
        return
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = auth.split(" ", 1)[1].strip()
    if token != state.api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid bearer token"
        )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class HintIn(BaseModel):
    author: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=8000)


class HintOut(BaseModel):
    id: int
    consumed: bool


class PauseIn(BaseModel):
    reason: Optional[str] = None


class AbortIn(BaseModel):
    message: Optional[str] = None


class EmergencyStopIn(BaseModel):
    message: Optional[str] = None


class PriorityReorderIn(BaseModel):
    ordered_issue_ids: list[str]
    set_by: str = Field(default="dashboard", max_length=64)
    ttl_hours: float = Field(default=24.0, ge=0)
    reason: Optional[str] = "drag-reorder"


class ConfigPatchIn(BaseModel):
    """Whitelist of runtime knobs operators can change without editing WORKFLOW.md."""

    max_concurrent_agents: Optional[int] = Field(default=None, ge=1, le=200)
    polling_interval_ms: Optional[int] = Field(default=None, ge=100, le=3_600_000)


class WorkflowPutIn(BaseModel):
    content: str


# ---------------------------------------------------------------------------
# Helpers — converting orchestrator/bridge state into dashboard JSON
# ---------------------------------------------------------------------------


def _issue_dict(issue: Issue) -> dict[str, Any]:
    return {
        "id": issue.id,
        "identifier": issue.identifier,
        "title": issue.title,
        "description": issue.description,
        "priority": issue.priority,
        "state": issue.state,
        "branch_name": issue.branch_name,
        "url": issue.url,
        "labels": list(issue.labels),
        "blocked_by": list(issue.blocked_by),
        "created_at": issue.created_at.isoformat() if issue.created_at else None,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
    }


def _attempt_dict(att: RunAttempt) -> dict[str, Any]:
    return {
        "issue_id": att.issue_id,
        "attempt_number": att.attempt_number,
        "state": att.state.value,
        "started_at": att.started_at.isoformat() if att.started_at else None,
        "ended_at": att.ended_at.isoformat() if att.ended_at else None,
        "terminal_reason": att.terminal_reason.value if att.terminal_reason else None,
        "last_event_at": att.last_event_at.isoformat() if att.last_event_at else None,
        "session_id": att.session_id,
        "turns_consumed": att.turns_consumed,
        "cost_usd": float(getattr(att, "cost_usd", 0.0) or 0.0),
        "error_message": att.error_message,
        "retry_after": att.retry_after.isoformat() if att.retry_after else None,
        "paused_until": att.paused_until.isoformat() if att.paused_until else None,
    }


def _config_dict(workflow: Workflow) -> dict[str, Any]:
    """Sanitised config for API responses — NEVER includes tracker.api_key."""
    cfg = workflow.config
    return {
        "tracker_kind": cfg.tracker_kind,
        "tracker_repo": cfg.tracker_repo,
        # tracker_api_key intentionally OMITTED — see spec §11 DoD #9
        "active_states": list(cfg.active_states),
        "terminal_states": list(cfg.terminal_states),
        "polling_interval_ms": cfg.polling_interval_ms,
        "workspace_root": cfg.workspace_root,
        "max_concurrent_agents": cfg.max_concurrent_agents,
        "max_turns": cfg.max_turns,
        "stall_timeout_ms": cfg.stall_timeout_ms,
        "retry_max_attempts": cfg.retry_max_attempts,
        "handoff_state": cfg.handoff_state,
        "runner_kind": cfg.runner_kind,
        "runner_model": cfg.runner_model,
    }


def _column_for(att: Optional[RunAttempt]) -> str:
    """Map an attempt to one of the five kanban columns."""
    if att is None:
        return "pending"
    if att.state == RunState.UNCLAIMED:
        return "pending"
    if att.state == RunState.CLAIMED:
        return "claimed"
    if att.state == RunState.RUNNING:
        return "running"
    if att.state == RunState.RETRY_QUEUED:
        return "retry_queued"
    if att.state == RunState.RELEASED:
        return "released"
    return "pending"


def _build_state_snapshot(
    orch: Orchestrator, bridge: DashboardBridge
) -> dict[str, Any]:
    cfg = orch.workflow.config
    columns: dict[str, list[dict[str, Any]]] = {
        "pending": [],
        "claimed": [],
        "running": [],
        "retry_queued": [],
        "released": [],
    }
    overrides = bridge.get_priority_overrides()

    # Active issues from tracker (read-only) — these are anything still open.
    try:
        active_issues = orch.tracker.fetch_active(cfg.active_states)
    except Exception:  # noqa: BLE001
        logger.exception("tracker.fetch_active failed in /state; returning empty")
        active_issues = []
    issues_by_id = {i.id: i for i in active_issues}

    # Snapshot attempts under lock to keep things consistent.
    with orch._lock:  # noqa: SLF001
        attempts = dict(orch._attempts)  # noqa: SLF001

    seen: set[str] = set()
    for iid, att in attempts.items():
        seen.add(iid)
        col = _column_for(att)
        issue = issues_by_id.get(iid)
        entry: dict[str, Any] = {
            "issue": _issue_dict(issue) if issue else {"id": iid},
            "attempt": _attempt_dict(att),
            "queue_rank": overrides.get(iid),
        }
        columns[col].append(entry)

    # Active tracker issues that don't have an attempt yet → Pending.
    for iid, issue in issues_by_id.items():
        if iid in seen:
            continue
        columns["pending"].append(
            {
                "issue": _issue_dict(issue),
                "attempt": None,
                "queue_rank": overrides.get(iid),
            }
        )

    # Stable ordering: pending uses (override-rank, priority, created_at, id).
    columns["pending"].sort(
        key=lambda e: (
            e.get("queue_rank") if e.get("queue_rank") is not None else 1_000_000,
            (e["issue"].get("priority") or 999),
            e["issue"].get("created_at") or "",
            e["issue"].get("identifier") or "",
        )
    )

    totals = {
        "active_workers": sum(
            1 for a in attempts.values() if a.state in (RunState.CLAIMED, RunState.RUNNING)
        ),
        "released_today": sum(
            1 for a in attempts.values() if a.state == RunState.RELEASED
        ),
    }

    return {
        "tick_at": datetime.now(timezone.utc).isoformat(),
        "config": _config_dict(orch.workflow),
        "columns": columns,
        "totals": totals,
    }


# ---------------------------------------------------------------------------
# REST routes
# ---------------------------------------------------------------------------

api_router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(_require_auth)],
)


@api_router.get("/state")
def get_state(request: Request) -> dict[str, Any]:
    state: DashboardAppState = request.app.state.symphony
    return _build_state_snapshot(state.orch, state.bridge)


@api_router.get("/issues/{issue_id}")
def get_issue(issue_id: str, request: Request) -> dict[str, Any]:
    state: DashboardAppState = request.app.state.symphony
    orch = state.orch
    bridge = state.bridge

    with orch._lock:  # noqa: SLF001
        att = orch._attempts.get(issue_id)  # noqa: SLF001
        att_dict = _attempt_dict(att) if att else None

    # Try to find the issue in tracker (best-effort; tracker is read-only).
    try:
        active = orch.tracker.fetch_active(orch.workflow.config.active_states)
        issue = next((i for i in active if i.id == issue_id), None)
    except Exception:  # noqa: BLE001
        issue = None

    rendered_preview: Optional[str] = None
    if issue is not None and att is not None:
        try:
            hints = bridge.fetch_pending_hints(issue_id)
            ctx = issue.to_template_context(att.attempt_number, hints=hints)
            rendered_preview = render_prompt(orch.workflow, ctx)
        except Exception as e:  # noqa: BLE001
            rendered_preview = f"(render preview failed: {e})"

    workspace_path: Optional[str] = None
    if issue is not None:
        try:
            workspace_path = str(orch.workspaces.path_for(issue.identifier))
        except Exception:  # noqa: BLE001
            workspace_path = None

    return {
        "issue": _issue_dict(issue) if issue else {"id": issue_id},
        "current_attempt": att_dict,
        "all_attempts": bridge.list_attempt_history(issue_id),
        "hints": bridge.list_hints(issue_id),
        "rendered_prompt_preview": rendered_preview,
        "workspace_path": workspace_path,
        "tracker_url": issue.url if issue else None,
    }


@api_router.post("/issues/{issue_id}/hint", response_model=HintOut)
def post_hint(issue_id: str, body: HintIn, request: Request) -> HintOut:
    state: DashboardAppState = request.app.state.symphony
    hid = state.bridge.add_hint(issue_id, body.author, body.content)
    return HintOut(id=hid, consumed=False)


@api_router.post("/issues/{issue_id}/pause")
def post_pause(
    issue_id: str, body: PauseIn, request: Request
) -> dict[str, Any]:
    state: DashboardAppState = request.app.state.symphony
    ok = state.orch.pause(issue_id)
    if not ok:
        raise HTTPException(status_code=404, detail="no active attempt for issue")
    return {"ok": True, "reason": body.reason}


@api_router.post("/issues/{issue_id}/resume")
def post_resume(issue_id: str, request: Request) -> dict[str, Any]:
    state: DashboardAppState = request.app.state.symphony
    ok = state.orch.resume(issue_id)
    if not ok:
        raise HTTPException(status_code=404, detail="issue is not paused")
    return {"ok": True}


@api_router.post("/issues/{issue_id}/abort")
def post_abort(
    issue_id: str, body: AbortIn, request: Request
) -> dict[str, Any]:
    state: DashboardAppState = request.app.state.symphony
    ok = state.orch.abort(issue_id, message=body.message or "operator aborted")
    if not ok:
        raise HTTPException(status_code=404, detail="no abortable attempt for issue")
    return {"ok": True}


@api_router.post("/issues/{issue_id}/retry")
def post_retry(issue_id: str, request: Request) -> dict[str, Any]:
    state: DashboardAppState = request.app.state.symphony
    ok = state.orch.force_retry(issue_id)
    if not ok:
        raise HTTPException(
            status_code=404, detail="issue not in RELEASED state"
        )
    return {"ok": True}


@api_router.post("/emergency_stop")
def post_emergency_stop(
    body: EmergencyStopIn, request: Request
) -> dict[str, Any]:
    """Operator-triggered kill switch: abort every active attempt at once.

    Returns the list of issue_ids that were aborted (may be empty if nothing
    was running). Idempotent — clicking the button twice will only report
    aborts on the first call.
    """
    state: DashboardAppState = request.app.state.symphony
    aborted = state.orch.abort_all(
        message=body.message or "operator emergency stop"
    )
    return {"ok": True, "aborted_count": len(aborted), "aborted_ids": aborted}


@api_router.post("/priority")
def post_priority(body: PriorityReorderIn, request: Request) -> dict[str, Any]:
    state: DashboardAppState = request.app.state.symphony
    state.bridge.reorder_pending(
        body.ordered_issue_ids,
        set_by=body.set_by,
        ttl_hours=body.ttl_hours,
        reason=body.reason,
    )
    return {"ok": True, "overrides": state.bridge.get_priority_overrides()}


@api_router.patch("/config")
def patch_config(body: ConfigPatchIn, request: Request) -> dict[str, Any]:
    state: DashboardAppState = request.app.state.symphony
    cfg = state.orch.workflow.config
    changed: dict[str, Any] = {}
    if body.max_concurrent_agents is not None:
        cfg.max_concurrent_agents = body.max_concurrent_agents
        changed["max_concurrent_agents"] = cfg.max_concurrent_agents
    if body.polling_interval_ms is not None:
        cfg.polling_interval_ms = body.polling_interval_ms
        changed["polling_interval_ms"] = cfg.polling_interval_ms
    if changed:
        state.broadcast(
            {"type": "config_changed", "config": _config_dict(state.orch.workflow)}
        )
    return {"ok": True, "changed": changed, "config": _config_dict(state.orch.workflow)}


@api_router.get("/issues/{issue_id}/events")
def get_issue_events(
    issue_id: str,
    request: Request,
    attempt_number: Optional[int] = Query(default=None),
    limit: int = Query(default=5000, ge=1, le=20000),
) -> dict[str, Any]:
    state: DashboardAppState = request.app.state.symphony
    events = state.bridge.fetch_events(
        issue_id, attempt_number=attempt_number, limit=limit
    )
    return {"issue_id": issue_id, "attempt_number": attempt_number, "events": events}


@api_router.get("/issues/{issue_id}/attempts")
def get_issue_attempts(issue_id: str, request: Request) -> dict[str, Any]:
    """Combine finalised history with the live current attempt."""
    state: DashboardAppState = request.app.state.symphony
    history = state.bridge.list_attempt_history(issue_id)
    current: Optional[dict[str, Any]] = None
    with state.orch._lock:  # noqa: SLF001
        att = state.orch._attempts.get(issue_id)  # noqa: SLF001
        if att is not None and att.state != RunState.RELEASED:
            current = _attempt_dict(att)
    return {
        "issue_id": issue_id,
        "current": current,
        "history": history,
    }


_MAX_PREVIEW_LINES = 200
_MAX_PREVIEW_BYTES = 256 * 1024


@api_router.get("/issues/{issue_id}/workspace")
def get_issue_workspace(issue_id: str, request: Request) -> dict[str, Any]:
    """List files in the workspace, one level recursive. No write paths exposed."""
    state: DashboardAppState = request.app.state.symphony
    orch = state.orch
    try:
        active = orch.tracker.fetch_active(orch.workflow.config.active_states)
        issue = next((i for i in active if i.id == issue_id), None)
    except Exception:  # noqa: BLE001
        issue = None
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not in active set")
    try:
        ws_path = orch.workspaces.path_for(issue.identifier)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"workspace: {e}")
    if not ws_path.exists():
        return {"workspace_path": str(ws_path), "exists": False, "entries": []}
    entries: list[dict[str, Any]] = []
    for top in sorted(ws_path.iterdir()):
        rel = top.name
        if top.is_dir():
            entries.append({"path": rel, "is_dir": True})
            try:
                for child in sorted(top.iterdir()):
                    entries.append(
                        {
                            "path": f"{rel}/{child.name}",
                            "is_dir": child.is_dir(),
                            "size": (
                                child.stat().st_size if child.is_file() else None
                            ),
                        }
                    )
            except OSError:
                pass
        else:
            entries.append(
                {
                    "path": rel,
                    "is_dir": False,
                    "size": top.stat().st_size,
                }
            )
    return {
        "workspace_path": str(ws_path),
        "exists": True,
        "entries": entries,
    }


@api_router.get("/issues/{issue_id}/workspace/file")
def get_issue_workspace_file(
    issue_id: str,
    request: Request,
    path: str = Query(..., min_length=1, max_length=512),
) -> dict[str, Any]:
    """Read-only file preview, capped at 200 lines / 256 KiB.

    Path-traversal guard: resolve relative to workspace_path; reject if the
    resolved real path doesn't sit inside the workspace.
    """
    state: DashboardAppState = request.app.state.symphony
    orch = state.orch
    try:
        active = orch.tracker.fetch_active(orch.workflow.config.active_states)
        issue = next((i for i in active if i.id == issue_id), None)
    except Exception:  # noqa: BLE001
        issue = None
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not in active set")
    try:
        ws_path = orch.workspaces.path_for(issue.identifier).resolve()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"workspace: {e}")
    candidate = (ws_path / path).resolve()
    try:
        candidate.relative_to(ws_path)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes workspace")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    size = candidate.stat().st_size
    truncated = False
    try:
        with open(candidate, "rb") as f:
            raw = f.read(_MAX_PREVIEW_BYTES + 1)
        if len(raw) > _MAX_PREVIEW_BYTES:
            raw = raw[:_MAX_PREVIEW_BYTES]
            truncated = True
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if len(lines) > _MAX_PREVIEW_LINES:
            lines = lines[:_MAX_PREVIEW_LINES]
            truncated = True
        return {
            "path": path,
            "size": size,
            "truncated": truncated,
            "lines": lines,
        }
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"read error: {e}")


@api_router.get("/workflow")
def get_workflow(request: Request) -> dict[str, Any]:
    state: DashboardAppState = request.app.state.symphony
    if not state.workflow_path or not state.workflow_path.exists():
        raise HTTPException(status_code=404, detail="workflow path not configured")
    return {
        "path": str(state.workflow_path),
        "content": state.workflow_path.read_text(encoding="utf-8"),
        "config": _config_dict(state.orch.workflow),
        "last_load_error": state.last_workflow_load_error,
    }


@api_router.put("/workflow")
def put_workflow(body: WorkflowPutIn, request: Request) -> dict[str, Any]:
    """Write content to the workflow file and hot-reload.

    Per spec §2.1 / §12: failure to parse the new file MUST keep the previous
    in-memory config alive. We write the file optimistically, attempt a load,
    and on failure restore the prior file content + return 422.
    """
    state: DashboardAppState = request.app.state.symphony
    if not state.workflow_path:
        raise HTTPException(status_code=404, detail="workflow path not configured")
    prior = (
        state.workflow_path.read_text(encoding="utf-8")
        if state.workflow_path.exists()
        else None
    )
    state.workflow_path.write_text(body.content, encoding="utf-8")
    try:
        new_workflow = load_workflow(state.workflow_path)
    except ValueError as e:
        # Roll the file back so disk + in-memory both reflect last-known-good.
        if prior is not None:
            state.workflow_path.write_text(prior, encoding="utf-8")
        state.last_workflow_load_error = str(e)
        return JSONResponse(
            status_code=422,
            content={"ok": False, "error": str(e), "kept_previous": True},
        )
    state.orch.workflow = new_workflow
    state.last_workflow_load_error = None
    state.broadcast(
        {"type": "workflow_reloaded", "ok": True, "config": _config_dict(new_workflow)}
    )
    return {
        "ok": True,
        "config": _config_dict(state.orch.workflow),
    }


# ---------------------------------------------------------------------------
# WebSocket — implemented in W1.6 (bound below)
# ---------------------------------------------------------------------------


def _ws_authorized(websocket: WebSocket, api_key: Optional[str]) -> bool:
    if not api_key:
        return True
    auth = websocket.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    return auth.split(" ", 1)[1].strip() == api_key


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:7957",
    "http://127.0.0.1:7957",
]


def create_app(
    orchestrator: Orchestrator,
    bridge: DashboardBridge,
    *,
    workflow_path: Optional[Path | str] = None,
    api_key: Optional[str] = None,
    cors_origins: Optional[list[str]] = None,
) -> FastAPI:
    """Build the dashboard FastAPI app bound to a live orchestrator + bridge.

    The orchestrator's ``bridge`` attribute should already be set to ``bridge``
    by the caller — that's what makes hint injection / priority overrides
    actually take effect during dispatch.
    """
    app = FastAPI(
        title="Symphony Dashboard",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or DEFAULT_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    state = DashboardAppState(
        orchestrator,
        bridge,
        workflow_path=Path(workflow_path) if workflow_path else None,
        api_key=api_key if api_key is not None else os.environ.get("DASHBOARD_API_KEY") or None,
    )
    app.state.symphony = state

    app.include_router(api_router)

    # Coding Service Tool API — POST /api/v1/tools/* for upstream LLM agents.
    # See docs/design/coding-service-tool-api.md.
    from .tool_api import tool_router  # noqa: PLC0415 — keep import scoped to setup
    app.include_router(tool_router)

    @app.websocket("/api/v1/events")
    async def events_ws(
        websocket: WebSocket,
        filter: Optional[str] = Query(default=None),
    ) -> None:
        if not _ws_authorized(websocket, state.api_key):
            await websocket.close(code=4401)
            return
        await websocket.accept()

        # Parse filter: ?filter=issue:MT-1,MT-3
        allowed_ids: Optional[set[str]] = None
        if filter and filter.startswith("issue:"):
            allowed_ids = {x.strip() for x in filter[len("issue:"):].split(",") if x.strip()}

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)

        def _on_event(issue_id: str, event: AgentEvent) -> None:
            if allowed_ids is not None and issue_id not in allowed_ids:
                return
            payload = {
                "type": "agent_event",
                "issue_id": issue_id,
                "event": {
                    "kind": event.kind.value,
                    "timestamp": event.timestamp.isoformat(),
                    "data": event.data,
                },
            }
            try:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except Exception:  # noqa: BLE001
                pass

        def _on_transition(issue_id: str, frm: str, to: str) -> None:
            if allowed_ids is not None and issue_id not in allowed_ids:
                return
            payload = {
                "type": "fsm_transition",
                "issue_id": issue_id,
                "from": frm,
                "to": to,
            }
            try:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except Exception:  # noqa: BLE001
                pass

        def _on_broadcast(payload: dict[str, Any]) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except Exception:  # noqa: BLE001
                pass

        bridge.add_event_subscriber(_on_event)
        bridge.add_transition_subscriber(_on_transition)
        state.add_broadcaster(_on_broadcast)
        try:
            # Replay recent ring-buffer events for context on connect.
            with orchestrator._lock:  # noqa: SLF001
                attempt_ids = list(orchestrator._attempts.keys())  # noqa: SLF001
            for iid in attempt_ids:
                if allowed_ids is not None and iid not in allowed_ids:
                    continue
                for ev in bridge.get_recent_events(iid, limit=100):
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "agent_event",
                                "issue_id": iid,
                                "event": {
                                    "kind": ev.kind.value,
                                    "timestamp": ev.timestamp.isoformat(),
                                    "data": ev.data,
                                },
                                "replay": True,
                            },
                            default=str,
                        )
                    )

            while True:
                msg = await queue.get()
                await websocket.send_text(json.dumps(msg, default=str))
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("WebSocket loop crashed")
        finally:
            bridge.remove_event_subscriber(_on_event)
            bridge.remove_transition_subscriber(_on_transition)
            state.remove_broadcaster(_on_broadcast)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True}

    # Prometheus scrape endpoint. NO auth by convention — protect via
    # reverse-proxy if the dashboard is internet-facing. Reads all data
    # from in-memory orchestrator state + SQLite; zero hot-path overhead.
    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        from .metrics import format_prometheus  # noqa: PLC0415 — scoped import
        return format_prometheus(state.orch, state.bridge)

    # Mount frontend dist if it exists. We resolve relative to the package so
    # `python -m symphony_mvp.dashboard` from any cwd still serves the SPA.
    pkg_root = Path(__file__).resolve().parents[2]
    dist_dir = pkg_root / "frontend" / "dist"
    if dist_dir.is_dir():
        # SPA fallback: any unmatched GET goes to index.html so client-side
        # routing keeps working. /api/* and /healthz are matched first.
        index_html = dist_dir / "index.html"
        app.mount(
            "/assets",
            StaticFiles(directory=str(dist_dir / "assets")),
            name="assets",
        )

        @app.get("/")
        def _index() -> FileResponse:
            return FileResponse(index_html)

        @app.get("/{full_path:path}")
        def _spa_fallback(full_path: str) -> FileResponse:
            candidate = dist_dir / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_html)

    return app
