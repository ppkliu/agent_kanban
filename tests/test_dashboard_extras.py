"""Tests for B1-B8 backend extensions: attempt history, FSM transitions,
events query, attempts query, workspace browse, broadcast WS messages, CORS."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from symphony_mvp import (
    EchoRunner,
    InMemoryTracker,
    Issue,
    Orchestrator,
    RunState,
    WorkspaceManager,
    load_workflow,
)
from symphony_mvp.agent_runner import AgentEvent, AgentEventKind
from symphony_mvp.dashboard.bridge import DashboardBridge
from symphony_mvp.dashboard.server import create_app


WORKFLOW_TEMPLATE = """---
tracker:
  kind: memory
polling:
  interval_ms: 50
agent:
  max_concurrent_agents: 2
  max_turns: 3
  retry_max_attempts: 2
  handoff_state: in_review
runner:
  kind: echo
---
Issue {{ issue.identifier }}: {{ issue.title }}
"""


def _make_issue(identifier="MT-1") -> Issue:
    return Issue(
        id=f"id-{identifier}",
        identifier=identifier,
        title=f"Title {identifier}",
        description="body",
        priority=1,
        state="open",
        branch_name=None,
        url=f"https://example.com/{identifier}",
    )


@pytest.fixture()
def app_ctx(tmp_path: Path):
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(WORKFLOW_TEMPLATE, encoding="utf-8")
    workflow = load_workflow(wf)
    workflow.config.workspace_root = str(tmp_path / "ws")
    tracker = InMemoryTracker([_make_issue("MT-1")])
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
    yield {"orch": orch, "bridge": bridge, "tracker": tracker, "client": client, "wf_path": wf, "workflow": workflow, "tmp": tmp_path}
    bridge.close()


def _run_until_released(orch, issue_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        orch.tick()
        att = orch._attempts.get(issue_id)  # noqa: SLF001
        if att and att.state == RunState.RELEASED:
            return
        time.sleep(0.05)
    raise AssertionError(f"{issue_id} did not reach RELEASED in {timeout}s")


# ----------------- B1: attempt_history persistence -----------------

def test_attempt_history_recorded_on_release(app_ctx) -> None:
    orch, bridge, client = app_ctx["orch"], app_ctx["bridge"], app_ctx["client"]
    _run_until_released(orch, "id-MT-1")

    history = bridge.list_attempt_history("id-MT-1")
    assert len(history) == 1
    row = history[0]
    assert row["state"] == "released"
    assert row["terminal_reason"] == "agent_finished"
    assert row["turns_consumed"] >= 1


def test_attempts_endpoint_returns_history(app_ctx) -> None:
    orch, client = app_ctx["orch"], app_ctx["client"]
    _run_until_released(orch, "id-MT-1")
    r = client.get("/api/v1/issues/id-MT-1/attempts")
    assert r.status_code == 200
    body = r.json()
    assert body["current"] is None  # already released
    assert len(body["history"]) == 1


# ----------------- B2: FSM transitions broadcast -----------------

def test_fsm_transitions_observed_via_bridge(app_ctx) -> None:
    orch, bridge = app_ctx["orch"], app_ctx["bridge"]
    seen: list[tuple[str, str, str]] = []
    bridge.add_transition_subscriber(
        lambda iid, frm, to: seen.append((iid, frm, to))
    )
    _run_until_released(orch, "id-MT-1")
    states = [(frm, to) for (_, frm, to) in seen]
    # Expect at least UNCLAIMED→CLAIMED, CLAIMED→RUNNING, ?→RELEASED
    assert ("unclaimed", "claimed") in states
    assert ("claimed", "running") in states
    assert any(to == "released" for (_, to) in states)


# ----------------- B3: cumulative cost on attempt -----------------

def test_cost_accumulates_from_turn_completed(app_ctx) -> None:
    orch, bridge = app_ctx["orch"], app_ctx["bridge"]
    # Inject a synthetic TURN_COMPLETED with cost via the worker event hook.
    # First run a normal attempt so an attempt exists.
    orch.tick()
    time.sleep(0.05)
    # Manually push a couple of cost events:
    orch._on_worker_event(  # noqa: SLF001
        "id-MT-1",
        AgentEvent.now(AgentEventKind.TURN_COMPLETED, cost_usd=0.012),
    )
    orch._on_worker_event(  # noqa: SLF001
        "id-MT-1",
        AgentEvent.now(AgentEventKind.TURN_COMPLETED, cost_usd=0.008),
    )
    att = orch._attempts["id-MT-1"]  # noqa: SLF001
    assert abs(att.cost_usd - 0.020) < 1e-9


# ----------------- B4: events query endpoint -----------------

def test_events_endpoint_returns_persisted(app_ctx) -> None:
    orch, client = app_ctx["orch"], app_ctx["client"]
    _run_until_released(orch, "id-MT-1")
    r = client.get("/api/v1/issues/id-MT-1/events")
    assert r.status_code == 200
    body = r.json()
    kinds = [e["kind"] for e in body["events"]]
    assert "turn_completed" in kinds
    assert "done" in kinds


def test_events_endpoint_filters_by_attempt(app_ctx) -> None:
    orch, client, bridge = app_ctx["orch"], app_ctx["client"], app_ctx["bridge"]
    _run_until_released(orch, "id-MT-1")
    # Add a synthetic event for attempt 99 to verify filtering
    bridge.record_attempt_number("id-MT-1", 99)
    bridge.on_event(
        "id-MT-1",
        AgentEvent.now(AgentEventKind.MESSAGE_DELTA, text="from-attempt-99"),
    )
    r = client.get("/api/v1/issues/id-MT-1/events?attempt_number=99")
    assert r.status_code == 200
    events = r.json()["events"]
    assert all(e["attempt_number"] == 99 for e in events)
    assert any(e["data"].get("text") == "from-attempt-99" for e in events)


# ----------------- B5/B6: workspace browse + file preview -----------------

def test_workspace_endpoint_lists_files(app_ctx) -> None:
    orch, client = app_ctx["orch"], app_ctx["client"]
    _run_until_released(orch, "id-MT-1")  # creates workspace + marker
    r = client.get("/api/v1/issues/id-MT-1/workspace")
    assert r.status_code == 200
    body = r.json()
    assert body["exists"] is True
    paths = [e["path"] for e in body["entries"]]
    assert "agent_ran.txt" in paths


def test_workspace_file_preview_returns_lines(app_ctx) -> None:
    orch, client = app_ctx["orch"], app_ctx["client"]
    _run_until_released(orch, "id-MT-1")
    r = client.get(
        "/api/v1/issues/id-MT-1/workspace/file",
        params={"path": "agent_ran.txt"},
    )
    assert r.status_code == 200
    body = r.json()
    assert any("prompt_chars=" in line for line in body["lines"])


def test_workspace_file_rejects_path_traversal(app_ctx) -> None:
    orch, client = app_ctx["orch"], app_ctx["client"]
    _run_until_released(orch, "id-MT-1")
    r = client.get(
        "/api/v1/issues/id-MT-1/workspace/file",
        params={"path": "../../etc/passwd"},
    )
    assert r.status_code == 400


# ----------------- B7: WS broadcast on config / workflow -----------------

def test_websocket_broadcasts_config_changed(app_ctx) -> None:
    client = app_ctx["client"]
    with client.websocket_connect("/api/v1/events") as ws:
        client.patch("/api/v1/config", json={"max_concurrent_agents": 9})
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "config_changed"
        assert msg["config"]["max_concurrent_agents"] == 9


def test_websocket_broadcasts_workflow_reloaded(app_ctx) -> None:
    client = app_ctx["client"]
    new_content = WORKFLOW_TEMPLATE.replace(
        "max_concurrent_agents: 2", "max_concurrent_agents: 4"
    )
    with client.websocket_connect("/api/v1/events") as ws:
        client.put("/api/v1/workflow", json={"content": new_content})
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "workflow_reloaded"
        assert msg["ok"] is True
        assert msg["config"]["max_concurrent_agents"] == 4


# ----------------- B8: CORS middleware -----------------

def test_cors_allows_localhost_dev_origin(app_ctx) -> None:
    client = app_ctx["client"]
    r = client.options(
        "/api/v1/state",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    # FastAPI's CORS middleware returns 200 for valid preflight
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_issue_detail_includes_all_attempts_field(app_ctx) -> None:
    orch, client = app_ctx["orch"], app_ctx["client"]
    _run_until_released(orch, "id-MT-1")
    r = client.get("/api/v1/issues/id-MT-1")
    assert r.status_code == 200
    body = r.json()
    assert "all_attempts" in body
    assert len(body["all_attempts"]) >= 1


# ----------------- Emergency stop (abort_all) -----------------

def test_abort_all_returns_empty_when_nothing_active(app_ctx) -> None:
    orch = app_ctx["orch"]
    aborted = orch.abort_all()
    assert aborted == []


def test_abort_all_aborts_running_attempts(app_ctx, tmp_path: Path) -> None:
    """Stage attempts as RUNNING via direct insertion, then trigger abort_all."""
    from symphony_mvp.models import RunAttempt, RunState as RS

    orch = app_ctx["orch"]
    now = datetime.now(timezone.utc)
    orch._attempts["id-MT-1"] = RunAttempt(  # noqa: SLF001
        issue_id="id-MT-1",
        attempt_number=1,
        state=RS.RUNNING,
        started_at=now,
        last_event_at=now,
    )
    orch._attempts["id-MT-2"] = RunAttempt(  # noqa: SLF001
        issue_id="id-MT-2",
        attempt_number=1,
        state=RS.CLAIMED,
        started_at=now,
        last_event_at=now,
    )
    orch._attempts["id-MT-3"] = RunAttempt(  # noqa: SLF001
        issue_id="id-MT-3",
        attempt_number=1,
        state=RS.RETRY_QUEUED,
        retry_after=now,
    )

    aborted = orch.abort_all(message="e2e kill switch")
    assert set(aborted) == {"id-MT-1", "id-MT-2"}
    # RETRY_QUEUED is preserved (no live worker to terminate)
    assert orch._attempts["id-MT-3"].state == RS.RETRY_QUEUED  # noqa: SLF001
    # Idempotent: second call has nothing left to abort
    assert orch.abort_all() == []


def test_emergency_stop_endpoint(app_ctx) -> None:
    from symphony_mvp.models import RunAttempt, RunState as RS

    orch, client = app_ctx["orch"], app_ctx["client"]
    now = datetime.now(timezone.utc)
    orch._attempts["id-MT-1"] = RunAttempt(  # noqa: SLF001
        issue_id="id-MT-1",
        attempt_number=1,
        state=RS.RUNNING,
        started_at=now,
        last_event_at=now,
    )

    r = client.post("/api/v1/emergency_stop", json={"message": "operator panic"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["aborted_count"] == 1
    assert body["aborted_ids"] == ["id-MT-1"]


def test_emergency_stop_endpoint_idempotent_when_idle(app_ctx) -> None:
    client = app_ctx["client"]
    r = client.post("/api/v1/emergency_stop", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["aborted_count"] == 0
    assert body["aborted_ids"] == []
