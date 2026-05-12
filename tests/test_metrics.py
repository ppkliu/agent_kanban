"""Tests for the Prometheus /metrics endpoint + formatter."""
from __future__ import annotations

from datetime import datetime, timezone
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
from symphony_mvp.agent_runner import AgentEvent, AgentEventKind
from symphony_mvp.dashboard.bridge import DashboardBridge
from symphony_mvp.dashboard.metrics import format_prometheus
from symphony_mvp.dashboard.server import create_app
from symphony_mvp.models import RunAttempt, RunState, TerminalReason


WORKFLOW_TEMPLATE = """---
tracker:
  kind: memory
polling:
  interval_ms: 50
agent:
  max_concurrent_agents: 2
  max_turns: 3
  retry_max_attempts: 1
  handoff_state: in_review
runner:
  kind: echo
---
Metrics test {{ issue.identifier }}: {{ issue.title }}
"""


@pytest.fixture()
def ctx(tmp_path: Path):
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(WORKFLOW_TEMPLATE, encoding="utf-8")
    workflow = load_workflow(wf)
    workflow.config.workspace_root = str(tmp_path / "ws")
    tracker = InMemoryTracker([])
    workspaces = WorkspaceManager(workflow.config.workspace_root)
    bridge = DashboardBridge(tmp_path / "dash.db")
    orch = Orchestrator(
        workflow=workflow, tracker=tracker, workspaces=workspaces,
        runner=EchoRunner(), bridge=bridge,
    )
    orch.add_event_listener(bridge.on_event)
    app = create_app(orch, bridge, workflow_path=wf, api_key=None)
    client = TestClient(app)
    yield {"orch": orch, "bridge": bridge, "client": client}
    bridge.close()


# --------------------------------------------------------------------------- #
# format_prometheus — pure function tests                                     #
# --------------------------------------------------------------------------- #


def test_format_emits_help_and_type_preamble(ctx) -> None:
    text = format_prometheus(ctx["orch"], ctx["bridge"])
    assert "# HELP symphony_attempts " in text
    assert "# TYPE symphony_attempts gauge" in text
    assert "# HELP symphony_attempts_finalised_total " in text
    assert "# TYPE symphony_attempts_finalised_total counter" in text


def test_format_emits_every_run_state_even_when_zero(ctx) -> None:
    text = format_prometheus(ctx["orch"], ctx["bridge"])
    # Every RunState enum value should appear as a labelled gauge line
    for state in ("unclaimed", "claimed", "running", "retry_queued", "released"):
        assert f'symphony_attempts{{state="{state}"}} 0' in text


def test_format_counts_in_memory_attempts(ctx) -> None:
    """Inject 2 RUNNING + 1 CLAIMED attempt and verify counts surface."""
    orch = ctx["orch"]
    now = datetime.now(timezone.utc)
    for i, state in enumerate((RunState.RUNNING, RunState.RUNNING, RunState.CLAIMED)):
        orch._attempts[f"id-{i}"] = RunAttempt(  # noqa: SLF001
            issue_id=f"id-{i}", attempt_number=1, state=state,
            started_at=now, last_event_at=now,
        )
    text = format_prometheus(orch, ctx["bridge"])
    assert 'symphony_attempts{state="running"} 2' in text
    assert 'symphony_attempts{state="claimed"} 1' in text


def test_format_counts_finalised_attempts_by_reason(ctx) -> None:
    bridge = ctx["bridge"]
    now = datetime.now(timezone.utc)
    bridge.record_finalised_attempt(RunAttempt(
        issue_id="id-A", attempt_number=1, state=RunState.RELEASED,
        started_at=now, ended_at=now,
        terminal_reason=TerminalReason.AGENT_FINISHED, turns_consumed=2,
    ))
    bridge.record_finalised_attempt(RunAttempt(
        issue_id="id-B", attempt_number=1, state=RunState.RELEASED,
        started_at=now, ended_at=now,
        terminal_reason=TerminalReason.ABORTED, turns_consumed=1,
    ))
    text = format_prometheus(ctx["orch"], bridge)
    assert 'symphony_attempts_finalised_total{terminal_reason="agent_finished"} 1' in text
    assert 'symphony_attempts_finalised_total{terminal_reason="aborted"} 1' in text
    assert 'symphony_attempts_finalised_total{terminal_reason="max_turns"} 0' in text


def test_format_counts_events_total_and_by_kind(ctx) -> None:
    bridge = ctx["bridge"]
    bridge.record_attempt_number("id-A", 1)
    for kind in (
        AgentEventKind.TOOL_CALL, AgentEventKind.TOOL_CALL,
        AgentEventKind.MESSAGE_DELTA,
    ):
        bridge.on_event(
            "id-A",
            AgentEvent(kind=kind, timestamp=datetime.now(timezone.utc), data={}),
        )
    text = format_prometheus(ctx["orch"], bridge)
    assert "symphony_events_total 3" in text
    assert 'symphony_events_by_kind_total{kind="tool_call"} 2' in text
    assert 'symphony_events_by_kind_total{kind="message_delta"} 1' in text


def test_format_counts_idempotency_keys(ctx) -> None:
    bridge = ctx["bridge"]
    bridge.record_idempotency_key("k1", "tsk_1")
    bridge.record_idempotency_key("k2", "tsk_2")
    text = format_prometheus(ctx["orch"], bridge)
    assert "symphony_idempotency_keys 2" in text


# --------------------------------------------------------------------------- #
# /metrics HTTP endpoint                                                      #
# --------------------------------------------------------------------------- #


def test_metrics_endpoint_returns_text_plain_no_auth_required(ctx) -> None:
    """Prometheus scraping convention — /metrics should be unauthenticated.
    Verify it works even with auth otherwise enabled."""
    r = ctx["client"].get("/metrics")
    assert r.status_code == 200
    # The default response_class=PlainTextResponse should produce text/plain
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    # Core metrics present
    assert "symphony_attempts" in body
    assert "symphony_events_total" in body


def test_metrics_endpoint_skips_auth_even_when_api_key_is_set(tmp_path: Path) -> None:
    """Independent fixture: this time the dashboard requires a bearer token,
    /metrics should STILL be reachable without one (Prometheus convention)."""
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(WORKFLOW_TEMPLATE, encoding="utf-8")
    workflow = load_workflow(wf)
    workflow.config.workspace_root = str(tmp_path / "ws")
    bridge = DashboardBridge(tmp_path / "dash.db")
    orch = Orchestrator(
        workflow=workflow, tracker=InMemoryTracker([]),
        workspaces=WorkspaceManager(workflow.config.workspace_root),
        runner=EchoRunner(), bridge=bridge,
    )
    app = create_app(orch, bridge, workflow_path=wf, api_key="secret-token")
    client = TestClient(app)
    try:
        # Confirm auth IS enforced on a protected route
        r_state = client.get("/api/v1/state")
        assert r_state.status_code == 401

        # /metrics bypasses
        r_metrics = client.get("/metrics")
        assert r_metrics.status_code == 200
        assert "symphony_attempts" in r_metrics.text
    finally:
        bridge.close()
