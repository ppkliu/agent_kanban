"""Tests for the Coding Service Tool API (Phase A — 5 endpoints).

Mirrors the harness style of tests/test_dashboard_extras.py: a real
Orchestrator + InMemoryTracker + DashboardBridge + FastAPI TestClient,
and direct injection of RunAttempt rows into orchestrator state when we
need to exercise specific status branches.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from symphony_mvp import (
    EchoRunner,
    InMemoryTracker,
    Issue,
    Orchestrator,
    WorkspaceManager,
    load_workflow,
)
from symphony_mvp.dashboard.bridge import DashboardBridge
from symphony_mvp.dashboard.server import create_app
from symphony_mvp.models import RunAttempt, RunState, TerminalReason


WORKFLOW_TEMPLATE = """---
tracker:
  kind: memory
polling:
  interval_ms: 50
agent:
  max_concurrent_agents: 2
  max_turns: 5
  retry_max_attempts: 2
  handoff_state: in_review
runner:
  kind: echo
---
Tool API task {{ issue.identifier }}: {{ issue.title }}
"""


@pytest.fixture()
def app_ctx(tmp_path: Path):
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(WORKFLOW_TEMPLATE, encoding="utf-8")
    workflow = load_workflow(wf)
    workflow.config.workspace_root = str(tmp_path / "ws")
    tracker = InMemoryTracker()  # empty — Tool API will inject tasks
    workspaces = WorkspaceManager(workflow.config.workspace_root)
    bridge = DashboardBridge(tmp_path / "dash.db")
    orch = Orchestrator(
        workflow=workflow,
        tracker=tracker,
        workspaces=workspaces,
        runner=EchoRunner(),
        bridge=bridge,
    )
    orch.add_event_listener(bridge.on_event)
    app = create_app(orch, bridge, workflow_path=wf, api_key=None)
    client = TestClient(app)
    yield {
        "orch": orch,
        "tracker": tracker,
        "bridge": bridge,
        "client": client,
        "workflow": workflow,
    }
    bridge.close()


# --------------------------------------------------------------------------- #
# list_repos                                                                  #
# --------------------------------------------------------------------------- #

def test_list_repos_returns_memory_for_inmemory_tracker(app_ctx) -> None:
    r = app_ctx["client"].post("/api/v1/tools/list_repos", json={})
    assert r.status_code == 200
    body = r.json()
    assert len(body["repos"]) == 1
    repo = body["repos"][0]
    assert repo["id"] == "memory"
    assert repo["default_mode"] == "build"
    assert "build" in repo["allowed_modes"]


# --------------------------------------------------------------------------- #
# submit_coding_task                                                          #
# --------------------------------------------------------------------------- #

def test_submit_coding_task_creates_issue_and_returns_task_id(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={
            "task": "Add /health endpoint with {status:'ok'} JSON",
            "repo": "memory",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending"
    task_id = body["task_id"]
    assert task_id.startswith("tsk_")

    # Tracker should now contain the issue, ready for orchestrator to pick up
    issues = app_ctx["tracker"].all()
    assert any(i.id == task_id for i in issues)
    issue = next(i for i in issues if i.id == task_id)
    assert "Add /health" in issue.title or "Add /health" in issue.description
    assert "tool-api" in issue.labels


def test_submit_coding_task_rejects_unknown_repo(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "do thing", "repo": "github:not/configured"},
    )
    assert r.status_code == 400
    assert "unknown repo" in r.json()["detail"]


def test_submit_coding_task_includes_files_hint_in_description(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={
            "task": "Refactor login flow",
            "repo": "memory",
            "files_hint": ["src/auth/login.ts", "src/auth/session.ts"],
        },
    )
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    issue = next(i for i in app_ctx["tracker"].all() if i.id == task_id)
    assert "src/auth/login.ts" in issue.description
    assert "src/auth/session.ts" in issue.description


# --------------------------------------------------------------------------- #
# check_task_status                                                           #
# --------------------------------------------------------------------------- #

def test_check_task_status_pending_when_no_attempt_yet(app_ctx) -> None:
    submit = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "test pending", "repo": "memory"},
    )
    task_id = submit.json()["task_id"]
    # Don't tick the orchestrator — attempt should not exist yet
    r = app_ctx["client"].post(
        "/api/v1/tools/check_task_status", json={"task_id": task_id}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending"
    assert body["stage"] == "queued"
    assert body["progress"] == 0.0
    assert body["max_turns"] == 5
    assert body["next_poll_after_ms"] >= 1000


def test_check_task_status_running_when_attempt_active(app_ctx) -> None:
    orch = app_ctx["orch"]
    submit = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "test running", "repo": "memory"},
    )
    task_id = submit.json()["task_id"]
    now = datetime.now(timezone.utc)
    orch._attempts[task_id] = RunAttempt(  # noqa: SLF001
        issue_id=task_id,
        attempt_number=1,
        state=RunState.RUNNING,
        started_at=now - timedelta(seconds=2),
        last_event_at=now,
        turns_consumed=2,
    )

    r = app_ctx["client"].post(
        "/api/v1/tools/check_task_status", json={"task_id": task_id}
    )
    body = r.json()
    assert body["status"] == "running"
    assert body["stage"] == "running"
    assert body["current_turn"] == 2
    assert 0.0 < body["progress"] <= 1.0
    assert body["duration_ms"] is not None and body["duration_ms"] > 0


# --------------------------------------------------------------------------- #
# get_task_result                                                             #
# --------------------------------------------------------------------------- #

def test_get_task_result_409_before_finalisation(app_ctx) -> None:
    submit = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "still running", "repo": "memory"},
    )
    task_id = submit.json()["task_id"]
    r = app_ctx["client"].post(
        "/api/v1/tools/get_task_result", json={"task_id": task_id}
    )
    assert r.status_code == 409
    assert "no finalised attempt" in r.json()["detail"]


def test_get_task_result_returns_done_summary_after_run(app_ctx) -> None:
    """Run a real EchoRunner attempt to completion, then fetch the result."""
    import time
    orch, tracker = app_ctx["orch"], app_ctx["tracker"]
    submit = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "echo and finish", "repo": "memory"},
    )
    task_id = submit.json()["task_id"]

    deadline = time.time() + 5.0
    while time.time() < deadline:
        orch.tick()
        att = orch._attempts.get(task_id)  # noqa: SLF001
        if att and att.state == RunState.RELEASED:
            break
        time.sleep(0.05)
    assert orch._attempts[task_id].state == RunState.RELEASED  # noqa: SLF001

    r = app_ctx["client"].post(
        "/api/v1/tools/get_task_result", json={"task_id": task_id}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == task_id
    assert body["status"] == "done"
    assert "Task reached" in body["summary"]
    assert body["detailed_url"].endswith(f"/issues/{task_id}")
    assert body["files_changed"] == []  # Phase A always empty
    assert body["terminal_reason"] == "agent_finished"


def test_get_task_result_marks_aborted_attempt_as_cancelled(app_ctx) -> None:
    """Direct-inject a finalised ABORTED attempt and verify status mapping."""
    bridge = app_ctx["bridge"]
    task_id = "tsk_aborted"
    now = datetime.now(timezone.utc)
    fake_att = RunAttempt(
        issue_id=task_id,
        attempt_number=1,
        state=RunState.RELEASED,
        started_at=now - timedelta(seconds=10),
        ended_at=now,
        terminal_reason=TerminalReason.ABORTED,
        turns_consumed=1,
        cost_usd=0.001,
        error_message="operator emergency stop",
    )
    bridge.record_finalised_attempt(fake_att)

    r = app_ctx["client"].post(
        "/api/v1/tools/get_task_result", json={"task_id": task_id}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["terminal_reason"] == "aborted"
    assert "operator emergency stop" in body["summary"]


# --------------------------------------------------------------------------- #
# cancel_task                                                                 #
# --------------------------------------------------------------------------- #

def test_cancel_task_aborts_running_attempt(app_ctx) -> None:
    orch = app_ctx["orch"]
    submit = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "to be cancelled", "repo": "memory"},
    )
    task_id = submit.json()["task_id"]
    now = datetime.now(timezone.utc)
    orch._attempts[task_id] = RunAttempt(  # noqa: SLF001
        issue_id=task_id,
        attempt_number=1,
        state=RunState.RUNNING,
        started_at=now,
        last_event_at=now,
    )

    r = app_ctx["client"].post(
        "/api/v1/tools/cancel_task",
        json={"task_id": task_id, "reason": "test cancel"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["was_running"] is True
    # Underlying orchestrator state should be RELEASED with ABORTED reason
    att = orch._attempts[task_id]  # noqa: SLF001
    assert att.state == RunState.RELEASED
    assert att.terminal_reason == TerminalReason.ABORTED


def test_cancel_task_idempotent_on_unknown_or_finished(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/tools/cancel_task",
        json={"task_id": "tsk_does_not_exist"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["was_running"] is False
