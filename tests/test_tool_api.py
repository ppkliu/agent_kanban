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
    # Phase B: summary now comes from the agent's own MESSAGE_DELTA stream
    # (EchoRunner emits "Wrote <path>") rather than the Phase A template.
    # Just assert it's a non-empty string here.
    assert isinstance(body["summary"], str) and body["summary"]
    assert body["detailed_url"].endswith(f"/issues/{task_id}")
    # Phase B: files_changed is derived from TOOL_CALL events. EchoRunner
    # doesn't emit any tool calls, so it's still empty for this runner.
    assert body["files_changed"] == []
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


# --------------------------------------------------------------------------- #
# Phase B — stage translation in check_task_status                            #
# --------------------------------------------------------------------------- #


def test_check_status_uses_stage_translator_for_active_attempts(app_ctx) -> None:
    """Inject a TOOL_CALL(edit) event and verify the API returns
    stage='modifying_files' instead of the plain 'running' fallback."""
    from datetime import datetime, timezone
    from symphony_mvp.agent_runner import AgentEvent, AgentEventKind

    orch = app_ctx["orch"]
    bridge = app_ctx["bridge"]
    submit = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "stage check", "repo": "memory"},
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
    bridge.record_attempt_number(task_id, 1)
    bridge.on_event(
        task_id,
        AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            timestamp=now,
            data={"tool": "edit", "input": {"path": "src/x.py"}},
        ),
    )

    r = app_ctx["client"].post(
        "/api/v1/tools/check_task_status", json={"task_id": task_id}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["stage"] == "modifying_files"


# --------------------------------------------------------------------------- #
# Phase B — files_changed + tests_run derivation in get_task_result           #
# --------------------------------------------------------------------------- #


def test_get_task_result_files_changed_from_injected_events(app_ctx) -> None:
    """Inject a finalised attempt + edit/write tool calls; verify the
    Phase B result derivation surfaces them in files_changed."""
    from datetime import datetime, timedelta, timezone
    from symphony_mvp.agent_runner import AgentEvent, AgentEventKind

    bridge = app_ctx["bridge"]
    task_id = "tsk_phase_b"
    now = datetime.now(timezone.utc)

    bridge.record_attempt_number(task_id, 1)
    bridge.on_event(
        task_id,
        AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            timestamp=now - timedelta(seconds=5),
            data={"tool": "write", "tool_use_id": "w1", "input": {"path": "src/new.py"}},
        ),
    )
    bridge.on_event(
        task_id,
        AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            timestamp=now - timedelta(seconds=4),
            data={"tool": "edit", "tool_use_id": "e1", "input": {"path": "src/old.py"}},
        ),
    )
    bridge.on_event(
        task_id,
        AgentEvent(
            kind=AgentEventKind.MESSAGE_DELTA,
            timestamp=now - timedelta(seconds=3),
            data={"text": "Implemented the change. Consider adding more tests."},
        ),
    )

    finished = RunAttempt(
        issue_id=task_id,
        attempt_number=1,
        state=RunState.RELEASED,
        started_at=now - timedelta(seconds=10),
        ended_at=now,
        terminal_reason=TerminalReason.AGENT_FINISHED,
        turns_consumed=2,
        cost_usd=0.0042,
    )
    bridge.record_finalised_attempt(finished)

    r = app_ctx["client"].post(
        "/api/v1/tools/get_task_result", json={"task_id": task_id}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    paths = {f["path"]: f["change"] for f in body["files_changed"]}
    assert paths == {"src/new.py": "added", "src/old.py": "modified"}
    assert "Implemented the change" in body["summary"]
    assert any("more tests" in s for s in body["follow_ups"])


# --------------------------------------------------------------------------- #
# Phase B — inspect_repo                                                      #
# --------------------------------------------------------------------------- #


def test_inspect_repo_returns_agents_md_and_tree(app_ctx, tmp_path) -> None:
    """Drop an AGENTS.md + a couple source files into the workspace root
    and verify inspect_repo surfaces them."""
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir(exist_ok=True)
    (workspace_root / "AGENTS.md").write_text(
        "Use snake_case for python", encoding="utf-8"
    )
    src = workspace_root / "src"
    src.mkdir()
    (src / "main.py").write_text("print(1)", encoding="utf-8")

    r = app_ctx["client"].post(
        "/api/v1/tools/inspect_repo", json={"repo": "memory"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["repo"] == "memory"
    assert body["agents_md"]["filename"] == "AGENTS.md"
    assert "snake_case" in body["agents_md"]["content"]
    assert "src/" in body["structure"]
    assert "python" in body["languages"]


def test_inspect_repo_rejects_unknown_repo_id(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/tools/inspect_repo", json={"repo": "github:not/configured"}
    )
    assert r.status_code == 400
    assert "unknown repo" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Phase B — per-task mode (plan / build / review)                             #
# --------------------------------------------------------------------------- #

def test_submit_with_mode_writes_mode_label_on_issue(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "audit", "repo": "memory", "mode": "review"},
    )
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    issue = next(i for i in app_ctx["tracker"].all() if i.id == task_id)
    assert "mode:review" in issue.labels
    assert "tool-api" in issue.labels


def test_submit_without_mode_does_not_add_mode_label(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "no mode", "repo": "memory"},
    )
    task_id = r.json()["task_id"]
    issue = next(i for i in app_ctx["tracker"].all() if i.id == task_id)
    assert not any(label.startswith("mode:") for label in issue.labels)


def test_submit_rejects_invalid_mode_value(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "x", "repo": "memory", "mode": "debug"},
    )
    # Pydantic validation kicks in: 422 unprocessable entity
    assert r.status_code == 422


def test_submit_with_each_valid_mode(app_ctx) -> None:
    for mode in ("plan", "build", "review"):
        r = app_ctx["client"].post(
            "/api/v1/tools/submit_coding_task",
            json={"task": "x", "repo": "memory", "mode": mode},
        )
        assert r.status_code == 200, f"mode={mode!r} failed: {r.text}"
        issue = next(
            i for i in app_ctx["tracker"].all() if i.id == r.json()["task_id"]
        )
        assert f"mode:{mode}" in issue.labels


# --------------------------------------------------------------------------- #
# Phase C — idempotency_key (safe retry of submit_coding_task)                #
# --------------------------------------------------------------------------- #

def test_submit_with_same_idempotency_key_returns_same_task_id(app_ctx) -> None:
    """Two submits with the same key should yield the same task_id and create
    only one issue in the tracker."""
    body = {
        "task": "do thing",
        "repo": "memory",
        "idempotency_key": "client-retry-key-1",
    }
    r1 = app_ctx["client"].post("/api/v1/tools/submit_coding_task", json=body)
    r2 = app_ctx["client"].post("/api/v1/tools/submit_coding_task", json=body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["task_id"] == r2.json()["task_id"]
    # Only one issue in the tracker (no duplicate)
    matching = [i for i in app_ctx["tracker"].all() if i.id == r1.json()["task_id"]]
    assert len(matching) == 1


def test_submit_without_idempotency_key_always_creates_new_task(app_ctx) -> None:
    body = {"task": "no key", "repo": "memory"}
    r1 = app_ctx["client"].post("/api/v1/tools/submit_coding_task", json=body)
    r2 = app_ctx["client"].post("/api/v1/tools/submit_coding_task", json=body)
    assert r1.json()["task_id"] != r2.json()["task_id"]


def test_submit_with_different_idempotency_keys_creates_separate_tasks(
    app_ctx,
) -> None:
    r1 = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "a", "repo": "memory", "idempotency_key": "key-a"},
    )
    r2 = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "b", "repo": "memory", "idempotency_key": "key-b"},
    )
    assert r1.json()["task_id"] != r2.json()["task_id"]


def test_submit_rate_limit_returns_429_when_cap_exceeded(app_ctx, monkeypatch) -> None:
    """With SYMPHONY_SUBMIT_RATE_LIMIT_PER_MINUTE=2, the 3rd submit in a
    60-second window should be rejected with 429 + Retry-After."""
    monkeypatch.setenv("SYMPHONY_SUBMIT_RATE_LIMIT_PER_MINUTE", "2")
    client = app_ctx["client"]

    for i in range(2):
        r = client.post(
            "/api/v1/tools/submit_coding_task",
            json={"task": f"under-limit {i}", "repo": "memory"},
        )
        assert r.status_code == 200, f"submit {i} unexpectedly failed: {r.text}"

    over = client.post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "over-limit", "repo": "memory"},
    )
    assert over.status_code == 429
    assert over.headers.get("retry-after") == "60"
    assert "rate limit" in over.json()["detail"].lower()


def test_submit_rate_limit_unset_means_unlimited(app_ctx, monkeypatch) -> None:
    """Default (env var unset) should bypass the rate limit entirely."""
    monkeypatch.delenv("SYMPHONY_SUBMIT_RATE_LIMIT_PER_MINUTE", raising=False)
    client = app_ctx["client"]
    # 10 submits in quick succession should all succeed
    for i in range(10):
        r = client.post(
            "/api/v1/tools/submit_coding_task",
            json={"task": f"no-limit {i}", "repo": "memory"},
        )
        assert r.status_code == 200, f"submit {i} unexpectedly failed: {r.text}"


def test_idempotency_replay_does_not_count_against_rate_limit(
    app_ctx, monkeypatch
) -> None:
    """A retried submit with the same idempotency_key short-circuits BEFORE
    the rate limit check — replays don't burn quota."""
    monkeypatch.setenv("SYMPHONY_SUBMIT_RATE_LIMIT_PER_MINUTE", "1")
    client = app_ctx["client"]

    # First submit succeeds (quota consumed)
    r1 = client.post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "x", "repo": "memory", "idempotency_key": "replay-test"},
    )
    assert r1.status_code == 200

    # Idempotency replay — same key — should NOT trigger 429
    r2 = client.post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "x", "repo": "memory", "idempotency_key": "replay-test"},
    )
    assert r2.status_code == 200
    assert r2.json()["task_id"] == r1.json()["task_id"]

    # But a fresh submit (no key, no idempotency) IS blocked at cap=1
    r3 = client.post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "fresh", "repo": "memory"},
    )
    assert r3.status_code == 429


def test_idempotency_short_circuit_skips_label_mutation(app_ctx) -> None:
    """If we resubmit with same key but different mode, the original task
    is returned unchanged — we never mutate an existing issue."""
    r1 = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={
            "task": "x", "repo": "memory",
            "idempotency_key": "key-c", "mode": "build",
        },
    )
    task_id = r1.json()["task_id"]
    issue = next(i for i in app_ctx["tracker"].all() if i.id == task_id)
    assert "mode:build" in issue.labels

    # Re-submit with mode=review under same key — should NOT change anything
    r2 = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={
            "task": "x", "repo": "memory",
            "idempotency_key": "key-c", "mode": "review",
        },
    )
    assert r2.json()["task_id"] == task_id
    issue_after = next(i for i in app_ctx["tracker"].all() if i.id == task_id)
    # Original mode:build label preserved; no mode:review added
    assert "mode:build" in issue_after.labels
    assert "mode:review" not in issue_after.labels


# --------------------------------------------------------------------------- #
# W3C trace_id propagation (partial close on Tracing/Metrics gap)             #
# --------------------------------------------------------------------------- #

import re as _re_for_trace_tests

_TRACE_ID_RE = _re_for_trace_tests.compile(r"^[0-9a-f]{32}$")


def test_submit_returns_trace_id_in_response(app_ctx) -> None:
    """Without a traceparent header, the response still carries a fresh
    32-hex trace_id so callers can use it for log correlation."""
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "x", "repo": "memory"},
    )
    assert r.status_code == 200
    trace_id = r.json()["trace_id"]
    assert _TRACE_ID_RE.match(trace_id), f"trace_id {trace_id!r} not 32-hex"


def test_submit_extracts_trace_id_from_traceparent_header(app_ctx) -> None:
    """A valid W3C traceparent header threads its trace_id through the
    response — the upstream caller's trace context is preserved."""
    upstream_trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    traceparent = f"00-{upstream_trace}-00f067aa0ba902b7-01"
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "x", "repo": "memory"},
        headers={"traceparent": traceparent},
    )
    assert r.status_code == 200
    assert r.json()["trace_id"] == upstream_trace


def test_submit_with_malformed_traceparent_generates_fresh_trace(app_ctx) -> None:
    """Garbage traceparent → fall back to a fresh trace_id rather than 4xx.
    Header isn't a contract field, so we degrade gracefully."""
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "x", "repo": "memory"},
        headers={"traceparent": "not-a-traceparent"},
    )
    assert r.status_code == 200
    trace_id = r.json()["trace_id"]
    assert _TRACE_ID_RE.match(trace_id)


def test_submit_writes_trace_label_on_issue(app_ctx) -> None:
    """The trace_id is encoded as a ``trace:<32hex>`` label on the Issue so
    every event in the orchestrator's stream can be correlated back."""
    upstream_trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "x", "repo": "memory"},
        headers={"traceparent": f"00-{upstream_trace}-00f067aa0ba902b7-01"},
    )
    task_id = r.json()["task_id"]
    issue = next(i for i in app_ctx["tracker"].all() if i.id == task_id)
    assert f"trace:{upstream_trace}" in issue.labels


def test_uppercase_traceparent_is_normalised_to_lowercase(app_ctx) -> None:
    """W3C spec allows uppercase hex; we lower-case for stable correlation
    keys (matching common observability stacks like Tempo / Honeycomb)."""
    upper = "4BF92F3577B34DA6A3CE929D0E0E4736"
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "x", "repo": "memory"},
        headers={"traceparent": f"00-{upper}-00F067AA0BA902B7-01"},
    )
    assert r.status_code == 200
    assert r.json()["trace_id"] == upper.lower()


def test_idempotency_replay_returns_original_trace_id(app_ctx) -> None:
    """A retried submit (same idempotency_key) returns the trace_id of the
    *original* task, not a fresh one — so the caller's log correlation
    keeps pointing at the same task across retries."""
    upstream_trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    r1 = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "x", "repo": "memory", "idempotency_key": "trace-replay"},
        headers={"traceparent": f"00-{upstream_trace}-00f067aa0ba902b7-01"},
    )
    assert r1.status_code == 200
    assert r1.json()["trace_id"] == upstream_trace

    # Retry with a *different* upstream trace — idempotency must still bind
    # the response to the *original* task's trace_id, not the new request's.
    different = "11111111111111111111111111111111"
    r2 = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "x", "repo": "memory", "idempotency_key": "trace-replay"},
        headers={"traceparent": f"00-{different}-00f067aa0ba902b7-01"},
    )
    assert r2.status_code == 200
    assert r2.json()["task_id"] == r1.json()["task_id"]
    assert r2.json()["trace_id"] == upstream_trace


# --------------------------------------------------------------------------- #
# Phase D1 — subtask graph foundation (parent_task_id + depends_on + list)    #
# --------------------------------------------------------------------------- #

def test_submit_with_parent_task_id_encodes_label(app_ctx) -> None:
    """parent_task_id is written as a `parent:<id>` label so the orchestrator
    can read it off issue.labels at dispatch time (same pattern as mode:/
    trace:)."""
    parent_resp = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "parent goal", "repo": "memory"},
    )
    parent_id = parent_resp.json()["task_id"]

    child_resp = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "child step", "repo": "memory", "parent_task_id": parent_id},
    )
    assert child_resp.status_code == 200
    child_id = child_resp.json()["task_id"]
    child = next(i for i in app_ctx["tracker"].all() if i.id == child_id)
    assert f"parent:{parent_id}" in child.labels


def test_submit_with_depends_on_populates_blocked_by(app_ctx) -> None:
    """depends_on maps straight to Issue.blocked_by — the existing dispatch
    gate then enforces ordering with zero orchestrator changes."""
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={
            "task": "step C",
            "repo": "memory",
            "depends_on": ["tsk_aaa", "tsk_bbb"],
        },
    )
    task_id = r.json()["task_id"]
    issue = next(i for i in app_ctx["tracker"].all() if i.id == task_id)
    assert issue.blocked_by == ["tsk_aaa", "tsk_bbb"]


def test_list_tasks_filters_by_parent_task_id(app_ctx) -> None:
    """Only children of one parent come back when parent_task_id is set."""
    parent = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "P", "repo": "memory"},
    ).json()["task_id"]
    other_parent = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "P2", "repo": "memory"},
    ).json()["task_id"]
    for label in ("c1", "c2"):
        app_ctx["client"].post(
            "/api/v1/tools/submit_coding_task",
            json={"task": label, "repo": "memory", "parent_task_id": parent},
        )
    app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "other-child", "repo": "memory", "parent_task_id": other_parent},
    )

    r = app_ctx["client"].post(
        "/api/v1/tools/list_tasks", json={"parent_task_id": parent}
    )
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert len(tasks) == 2
    titles = {t["title"] for t in tasks}
    assert titles == {"c1", "c2"}
    for t in tasks:
        assert t["parent_task_id"] == parent


def test_list_tasks_filters_by_status(app_ctx) -> None:
    """status filter only returns tasks whose derived status matches."""
    for n in range(3):
        app_ctx["client"].post(
            "/api/v1/tools/submit_coding_task",
            json={"task": f"queued-{n}", "repo": "memory"},
        )
    # All freshly-submitted tasks have status="pending" (no attempt yet).
    r = app_ctx["client"].post(
        "/api/v1/tools/list_tasks", json={"status": "pending"}
    )
    assert r.status_code == 200
    assert len(r.json()["tasks"]) == 3
    # No task should be in 'done' yet.
    r2 = app_ctx["client"].post(
        "/api/v1/tools/list_tasks", json={"status": "done"}
    )
    assert r2.json()["tasks"] == []


def test_list_tasks_returns_summary_shape(app_ctx) -> None:
    """The returned summaries carry the fields needed for kanban grouping:
    task_id, title, status, parent_task_id, depends_on, mode, created_at."""
    parent = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "kanban-parent", "repo": "memory"},
    ).json()["task_id"]
    app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={
            "task": "kanban-child",
            "repo": "memory",
            "parent_task_id": parent,
            "depends_on": [parent],
            "mode": "build",
        },
    )

    r = app_ctx["client"].post("/api/v1/tools/list_tasks", json={})
    rows = r.json()["tasks"]
    child = next(t for t in rows if t["title"] == "kanban-child")
    assert child["parent_task_id"] == parent
    assert child["depends_on"] == [parent]
    assert child["mode"] == "build"
    assert child["status"] == "pending"
    assert "created_at" in child


# --------------------------------------------------------------------------- #
# Phase D2a — Path A: caller-supplied subtask list                            #
# --------------------------------------------------------------------------- #

def test_submit_with_subtasks_creates_parent_and_linked_children(app_ctx) -> None:
    """`subtasks=[a, b, c]` produces 4 issues: 1 parent + 3 children. Each
    child carries a `parent:<id>` label pointing at the parent; the parent
    itself has no parent label."""
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={
            "task": "Build TODO API",
            "repo": "memory",
            "subtasks": [
                {"task": "data model"},
                {"task": "CRUD endpoints"},
                {"task": "integration tests"},
            ],
        },
    )
    assert r.status_code == 200
    parent_id = r.json()["task_id"]

    all_issues = app_ctx["tracker"].all()
    parent = next(i for i in all_issues if i.id == parent_id)
    children = [
        i for i in all_issues if f"parent:{parent_id}" in i.labels
    ]
    assert len(children) == 3
    assert not any(lbl.startswith("parent:") for lbl in parent.labels)
    assert {c.title for c in children} == {
        "data model", "CRUD endpoints", "integration tests"
    }


def test_subtask_sibling_depends_on_translates_to_real_task_ids(app_ctx) -> None:
    """Sibling indices in spec.depends_on get rewritten to actual sibling
    task_ids on the resulting Issue.blocked_by, so the existing dispatch
    gate handles ordering with no orchestrator changes."""
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={
            "task": "umbrella",
            "repo": "memory",
            "subtasks": [
                {"task": "first"},
                {"task": "second", "depends_on": [0]},
                {"task": "third", "depends_on": [0, 1]},
            ],
        },
    )
    parent_id = r.json()["task_id"]
    children = sorted(
        [i for i in app_ctx["tracker"].all() if f"parent:{parent_id}" in i.labels],
        key=lambda i: i.created_at,
    )
    a, b, c = children
    assert b.blocked_by == [a.id]
    assert c.blocked_by == [a.id, b.id]


def test_subtasks_inherit_parent_trace_id(app_ctx) -> None:
    """Every issue (parent + all children) shares one trace_id so log
    correlation across the whole subgraph points at the same trace."""
    upstream = "4bf92f3577b34da6a3ce929d0e0e4736"
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        headers={"traceparent": f"00-{upstream}-00f067aa0ba902b7-01"},
        json={
            "task": "umbrella",
            "repo": "memory",
            "subtasks": [{"task": "a"}, {"task": "b"}],
        },
    )
    assert r.json()["trace_id"] == upstream
    parent_id = r.json()["task_id"]
    label = f"trace:{upstream}"
    for issue in app_ctx["tracker"].all():
        if issue.id == parent_id or f"parent:{parent_id}" in issue.labels:
            assert label in issue.labels


def test_subtasks_idempotency_replay_returns_parent_id(app_ctx) -> None:
    """Same idempotency_key replay returns the original parent_id and does
    NOT create duplicate children — one key covers the whole batch."""
    body = {
        "task": "umbrella",
        "repo": "memory",
        "idempotency_key": "batch-1",
        "subtasks": [{"task": "x"}, {"task": "y"}],
    }
    r1 = app_ctx["client"].post("/api/v1/tools/submit_coding_task", json=body)
    r2 = app_ctx["client"].post("/api/v1/tools/submit_coding_task", json=body)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["task_id"] == r2.json()["task_id"]
    parent_id = r1.json()["task_id"]
    children = [
        i for i in app_ctx["tracker"].all()
        if f"parent:{parent_id}" in i.labels
    ]
    assert len(children) == 2  # no duplicates from the replay


def test_subtasks_invalid_depends_on_index_returns_400(app_ctx) -> None:
    """Forward-only sibling reference rule: depends_on[idx] must be < own
    index. Out-of-range or self/forward references are rejected before any
    state mutation."""
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={
            "task": "umbrella",
            "repo": "memory",
            "subtasks": [
                {"task": "a"},
                {"task": "b", "depends_on": [5]},
            ],
        },
    )
    assert r.status_code == 400
    assert "depends_on" in r.json()["detail"]
    # No issues should have been created
    titles = {i.title for i in app_ctx["tracker"].all()}
    assert "umbrella" not in titles
    assert "a" not in titles


def test_subtasks_per_child_mode_only_on_child(app_ctx) -> None:
    """Per-child `mode` writes `mode:<value>` on that child only — the
    parent's labels are independent."""
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={
            "task": "umbrella",
            "repo": "memory",
            "subtasks": [
                {"task": "scan", "mode": "review"},
                {"task": "implement", "mode": "build"},
            ],
        },
    )
    parent_id = r.json()["task_id"]
    parent = next(i for i in app_ctx["tracker"].all() if i.id == parent_id)
    children = sorted(
        [i for i in app_ctx["tracker"].all() if f"parent:{parent_id}" in i.labels],
        key=lambda i: i.created_at,
    )
    assert not any(lbl.startswith("mode:") for lbl in parent.labels)
    assert "mode:review" in children[0].labels
    assert "mode:build" in children[1].labels


# --------------------------------------------------------------------------- #
# Phase E1 — project_id label + list_tasks filter + /api/v1/projects REST     #
# --------------------------------------------------------------------------- #

def test_submit_without_project_id_uses_default(app_ctx) -> None:
    """submitting without project_id auto-attaches the `default` project so
    existing single-tenant callers keep working unchanged."""
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "single tenant", "repo": "memory"},
    )
    assert r.status_code == 200
    issue = next(i for i in app_ctx["tracker"].all() if i.id == r.json()["task_id"])
    assert "project:default" in issue.labels


def test_submit_with_project_id_encodes_label(app_ctx) -> None:
    # Create a project first
    pc = app_ctx["client"].post(
        "/api/v1/projects", json={"name": "Alpha"}
    )
    assert pc.status_code == 200
    pid = pc.json()["id"]

    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "scoped", "repo": "memory", "project_id": pid},
    )
    assert r.status_code == 200
    issue = next(i for i in app_ctx["tracker"].all() if i.id == r.json()["task_id"])
    assert f"project:{pid}" in issue.labels


def test_submit_with_unknown_project_id_returns_400(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "x", "repo": "memory", "project_id": "no-such-proj"},
    )
    assert r.status_code == 400
    assert "unknown project_id" in r.json()["detail"]


def test_submit_to_archived_project_returns_400(app_ctx) -> None:
    pc = app_ctx["client"].post("/api/v1/projects", json={"name": "Stale"})
    pid = pc.json()["id"]
    # Archive it
    pat = app_ctx["client"].patch(
        f"/api/v1/projects/{pid}", json={"archived": True}
    )
    assert pat.status_code == 200

    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "x", "repo": "memory", "project_id": pid},
    )
    assert r.status_code == 400
    assert "archived" in r.json()["detail"]


def test_subtasks_inherit_parent_project_id(app_ctx) -> None:
    """A parent submitted with project_id propagates it to every child."""
    pc = app_ctx["client"].post("/api/v1/projects", json={"name": "Graph"})
    pid = pc.json()["id"]

    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={
            "task": "umbrella",
            "repo": "memory",
            "project_id": pid,
            "subtasks": [{"task": "a"}, {"task": "b"}],
        },
    )
    parent_id = r.json()["task_id"]
    children = [
        i for i in app_ctx["tracker"].all() if f"parent:{parent_id}" in i.labels
    ]
    assert len(children) == 2
    for child in children:
        assert f"project:{pid}" in child.labels


def test_list_tasks_filters_by_project_id(app_ctx) -> None:
    """list_tasks ?project_id=X returns only that project's tasks. The
    auto-default project's tasks do NOT leak into a specific project's filter."""
    p_alpha = app_ctx["client"].post(
        "/api/v1/projects", json={"name": "Alpha"}
    ).json()["id"]
    p_beta = app_ctx["client"].post(
        "/api/v1/projects", json={"name": "Beta"}
    ).json()["id"]

    app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "alpha-1", "repo": "memory", "project_id": p_alpha},
    )
    app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "beta-1", "repo": "memory", "project_id": p_beta},
    )
    app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "default-1", "repo": "memory"},
    )

    r = app_ctx["client"].post(
        "/api/v1/tools/list_tasks", json={"project_id": p_alpha}
    )
    titles = [t["title"] for t in r.json()["tasks"]]
    assert titles == ["alpha-1"]
    for t in r.json()["tasks"]:
        assert t["project_id"] == p_alpha


def test_projects_rest_list_and_rename(app_ctx) -> None:
    r0 = app_ctx["client"].get("/api/v1/projects")
    assert r0.status_code == 200
    # default project auto-materialized on first GET
    ids = [p["id"] for p in r0.json()["projects"]]
    assert "default" in ids

    new_id = app_ctx["client"].post(
        "/api/v1/projects", json={"name": "Initial"}
    ).json()["id"]
    assert new_id.startswith("proj_")

    # Rename
    r2 = app_ctx["client"].patch(
        f"/api/v1/projects/{new_id}", json={"name": "Renamed"}
    )
    assert r2.status_code == 200
    assert r2.json()["project"]["name"] == "Renamed"

    # Archive + list filters
    r3 = app_ctx["client"].patch(
        f"/api/v1/projects/{new_id}", json={"archived": True}
    )
    assert r3.status_code == 200
    active = app_ctx["client"].get("/api/v1/projects").json()["projects"]
    assert new_id not in [p["id"] for p in active]
    all_p = app_ctx["client"].get(
        "/api/v1/projects?include_archived=true"
    ).json()["projects"]
    assert new_id in [p["id"] for p in all_p]


def test_projects_patch_unknown_returns_404(app_ctx) -> None:
    r = app_ctx["client"].patch(
        "/api/v1/projects/no-such", json={"name": "x"}
    )
    assert r.status_code == 404
    assert "unknown project" in r.json()["detail"]


def test_create_project_with_explicit_id(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/projects", json={"id": "custom-id-1", "name": "Custom"}
    )
    assert r.status_code == 200
    assert r.json()["id"] == "custom-id-1"
    assert r.json()["name"] == "Custom"


def test_create_project_rejects_bad_id(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/projects",
        json={"id": "has spaces and slashes/", "name": "Bad"},
    )
    # Pydantic pattern validator rejects → 422
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Phase D2b — Symphony-side decomposition (decompose=true Path B)             #
# --------------------------------------------------------------------------- #

def _run_until_released(orch, issue_id: str, timeout: float = 5.0) -> None:
    """Tick the orchestrator until the named issue's attempt is RELEASED."""
    import time
    from symphony_mvp.models import RunState
    deadline = time.time() + timeout
    while time.time() < deadline:
        orch.tick()
        att = orch._attempts.get(issue_id)  # noqa: SLF001
        if att is not None and att.state == RunState.RELEASED:
            # Give the worker thread + transition subscriber a beat to settle.
            time.sleep(0.05)
            return
        time.sleep(0.05)
    raise AssertionError(f"issue {issue_id} did not reach RELEASED in {timeout}s")


def test_submit_with_decompose_and_subtasks_returns_400(app_ctx) -> None:
    """Mutex: decompose=true cannot coexist with caller-supplied subtasks."""
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={
            "task": "Build a TODO API",
            "repo": "memory",
            "decompose": True,
            "subtasks": [{"task": "a"}],
        },
    )
    assert r.status_code == 400
    assert "mutually exclusive" in r.json()["detail"]


def test_submit_with_decompose_kill_switch_returns_400(app_ctx, monkeypatch) -> None:
    """SYMPHONY_DECOMPOSE_ENABLED=0 disables Path B (cost-sensitive deploys)."""
    monkeypatch.setenv("SYMPHONY_DECOMPOSE_ENABLED", "0")
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "Build a TODO API", "repo": "memory", "decompose": True},
    )
    assert r.status_code == 400
    assert "decomposition disabled" in r.json()["detail"]


def test_submit_with_decompose_creates_parent_with_pending_label(app_ctx) -> None:
    """Path B parent has the decompose-pending marker + mode:plan label +
    the decomposition system prompt prepended to the description so the
    plan-mode runner sees the right instructions."""
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "Build a TODO API", "repo": "memory", "decompose": True},
    )
    assert r.status_code == 200
    parent_id = r.json()["task_id"]
    parent = next(i for i in app_ctx["tracker"].all() if i.id == parent_id)
    assert "decompose-pending:1" in parent.labels
    assert "mode:plan" in parent.labels
    assert "JSON array" in parent.description
    assert "Build a TODO API" in parent.description


def test_decompose_hook_creates_children_from_echo_runner_message(app_ctx) -> None:
    """End-to-end: EchoRunner emits a JSON subtask array as its final
    assistant message; the post-finalize hook parses it and creates the
    children with the expected parent:/project: labels inherited."""
    from symphony_mvp.agent_runner import EchoRunner
    orch = app_ctx["orch"]
    orch.runner = EchoRunner(
        final_message='[{"task":"design schema","depends_on":[]},'
                      '{"task":"write tests","depends_on":[0]}]'
    )
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "Build a TODO API", "repo": "memory", "decompose": True},
    )
    parent_id = r.json()["task_id"]
    _run_until_released(orch, parent_id)

    children = [
        i for i in app_ctx["tracker"].all() if f"parent:{parent_id}" in i.labels
    ]
    assert len(children) == 2
    children_sorted = sorted(children, key=lambda i: i.created_at)
    assert children_sorted[0].title.startswith("design schema")
    assert children_sorted[1].title.startswith("write tests")
    # All children inherit the default project (no explicit project_id
    # was set on the parent, so auto-default applies + propagates).
    for c in children:
        assert "project:default" in c.labels
    # Second child depends on the first
    assert children_sorted[1].blocked_by == [children_sorted[0].id]


def test_decompose_hook_emits_error_event_on_malformed_json(app_ctx) -> None:
    """Malformed final message: no children, ERROR event recorded for
    operator visibility."""
    from symphony_mvp.agent_runner import EchoRunner
    orch = app_ctx["orch"]
    orch.runner = EchoRunner(final_message="not even close to JSON")
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "Build a TODO API", "repo": "memory", "decompose": True},
    )
    parent_id = r.json()["task_id"]
    _run_until_released(orch, parent_id)

    children = [
        i for i in app_ctx["tracker"].all() if f"parent:{parent_id}" in i.labels
    ]
    assert children == []

    events = app_ctx["bridge"].fetch_events(parent_id, limit=200)
    error_events = [e for e in events if e["kind"] == "error"]
    assert len(error_events) >= 1
    assert "decomposition parse failed" in error_events[0]["data"]["message"]


# --------------------------------------------------------------------------- #
# Phase D3 — NEEDS_HUMAN escalation                                            #
# --------------------------------------------------------------------------- #

def test_echo_runner_with_human_required_marker_releases_as_needs_human(
    app_ctx,
) -> None:
    """When an agent ends its turn with `[HUMAN_REQUIRED] <reason>`, the
    orchestrator detects the marker and finalises the attempt with
    ``TerminalReason.NEEDS_HUMAN`` (not AGENT_FINISHED)."""
    from symphony_mvp.agent_runner import EchoRunner
    from symphony_mvp.models import TerminalReason
    orch = app_ctx["orch"]
    orch.runner = EchoRunner(
        final_message="I made some progress but [HUMAN_REQUIRED] need scope clarification"
    )
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "ambiguous goal", "repo": "memory"},
    )
    task_id = r.json()["task_id"]
    _run_until_released(orch, task_id)
    att = orch._attempts.get(task_id)  # noqa: SLF001
    assert att is not None
    assert att.terminal_reason == TerminalReason.NEEDS_HUMAN


def test_check_task_status_reports_blocked_for_human(app_ctx) -> None:
    from symphony_mvp.agent_runner import EchoRunner
    orch = app_ctx["orch"]
    orch.runner = EchoRunner(
        final_message="[HUMAN_REQUIRED] need a design decision on caching"
    )
    submit = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "design caching", "repo": "memory"},
    )
    task_id = submit.json()["task_id"]
    _run_until_released(orch, task_id)
    r = app_ctx["client"].post(
        "/api/v1/tools/check_task_status", json={"task_id": task_id}
    )
    body = r.json()
    assert body["status"] == "blocked_for_human"
    assert body["stage"] == "blocked_for_human"


def test_marker_inside_fenced_code_block_does_not_escalate(app_ctx) -> None:
    """Prompt-injection defence: an agent quoting source / docs that
    happen to contain `[HUMAN_REQUIRED]` should NOT trigger escalation."""
    from symphony_mvp.agent_runner import EchoRunner
    from symphony_mvp.models import TerminalReason
    orch = app_ctx["orch"]
    orch.runner = EchoRunner(
        final_message=(
            "I implemented the feature. Here's a code snippet I referenced:\n"
            "```\n"
            "if condition: print('[HUMAN_REQUIRED] in source')\n"
            "```\n"
            "Done."
        )
    )
    submit = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "quote some code", "repo": "memory"},
    )
    task_id = submit.json()["task_id"]
    _run_until_released(orch, task_id)
    att = orch._attempts.get(task_id)  # noqa: SLF001
    assert att.terminal_reason == TerminalReason.AGENT_FINISHED


def test_resolve_human_block_records_hint_and_requeues(app_ctx) -> None:
    """Operator resolution: records a hint visible to the next attempt
    and force-retries so the agent re-runs with the resolution in context."""
    from symphony_mvp.agent_runner import EchoRunner
    from symphony_mvp.models import RunState, TerminalReason
    orch = app_ctx["orch"]
    orch.runner = EchoRunner(
        final_message="[HUMAN_REQUIRED] which DB engine should I use"
    )
    submit = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "build it", "repo": "memory"},
    )
    task_id = submit.json()["task_id"]
    _run_until_released(orch, task_id)
    assert orch._attempts[task_id].terminal_reason == TerminalReason.NEEDS_HUMAN  # noqa: SLF001

    r = app_ctx["client"].post(
        "/api/v1/tools/resolve_human_block",
        json={
            "task_id": task_id,
            "resolution_hint": "use sqlite, not redis",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["re_queued"] is True
    assert body["hint_id"] > 0

    # After force_retry, the attempt is back to UNCLAIMED (sentinel)
    assert orch._attempts[task_id].state == RunState.UNCLAIMED  # noqa: SLF001
    # The hint is recorded with author=human-resolver
    hints = app_ctx["bridge"].list_hints(task_id)
    assert any(
        h["author"] == "human-resolver" and "sqlite" in h["content"]
        for h in hints
    )


def test_resolve_human_block_409_when_task_not_blocked(app_ctx) -> None:
    """resolve_human_block on a task that is NOT in NEEDS_HUMAN should
    409 (caller used the wrong endpoint / state mismatch)."""
    # Submit a normal task and let it finish as AGENT_FINISHED
    from symphony_mvp.agent_runner import EchoRunner
    orch = app_ctx["orch"]
    orch.runner = EchoRunner()  # default: no marker → AGENT_FINISHED
    submit = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "normal task", "repo": "memory"},
    )
    task_id = submit.json()["task_id"]
    _run_until_released(orch, task_id)
    r = app_ctx["client"].post(
        "/api/v1/tools/resolve_human_block",
        json={"task_id": task_id, "resolution_hint": "n/a"},
    )
    assert r.status_code == 409
    assert "not blocked_for_human" in r.json()["detail"]


def test_resolve_human_block_404_when_unknown_task(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/tools/resolve_human_block",
        json={"task_id": "tsk_nope", "resolution_hint": "x"},
    )
    assert r.status_code == 404


def test_needs_human_parent_holds_children_at_dispatch_gate(app_ctx) -> None:
    """D1 + D3 integration: a parent that ends in NEEDS_HUMAN counts as
    a 'parent failure' for the dispatch gate, so any pending children
    stay frozen until the parent is resolved."""
    from symphony_mvp.agent_runner import EchoRunner
    from symphony_mvp.models import TerminalReason
    orch = app_ctx["orch"]
    orch.runner = EchoRunner(
        final_message="[HUMAN_REQUIRED] need approval"
    )
    parent = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "parent", "repo": "memory"},
    ).json()["task_id"]
    _run_until_released(orch, parent)
    assert orch._attempts[parent].terminal_reason == TerminalReason.NEEDS_HUMAN  # noqa: SLF001

    # Submit a child whose parent_task_id points at the blocked parent.
    child_resp = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={
            "task": "child",
            "repo": "memory",
            "parent_task_id": parent,
        },
    )
    child_id = child_resp.json()["task_id"]
    # Run a tick — child must NOT dispatch because parent is in
    # _PARENT_FAILURE_REASONS (NEEDS_HUMAN was just added there in D3).
    orch.tick()
    assert orch._attempts.get(child_id) is None  # noqa: SLF001


def test_decompose_hook_inherits_project_id_from_parent(app_ctx) -> None:
    """Children created by the post-finalize hook carry the parent's
    explicit project_id (not just the auto-default)."""
    from symphony_mvp.agent_runner import EchoRunner
    orch = app_ctx["orch"]
    orch.runner = EchoRunner(
        final_message='[{"task":"a"},{"task":"b"}]'
    )
    proj = app_ctx["client"].post(
        "/api/v1/projects", json={"name": "alpha"}
    ).json()
    r = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={
            "task": "umbrella",
            "repo": "memory",
            "decompose": True,
            "project_id": proj["id"],
        },
    )
    parent_id = r.json()["task_id"]
    _run_until_released(orch, parent_id)
    children = [
        i for i in app_ctx["tracker"].all() if f"parent:{parent_id}" in i.labels
    ]
    assert len(children) == 2
    for c in children:
        assert f"project:{proj['id']}" in c.labels


# --------------------------------------------------------------------------- #
# Phase D5 — get_workflow_result rollup                                       #
# --------------------------------------------------------------------------- #


def _seed_task(
    app_ctx,
    *,
    task_id: str,
    title: str,
    parent_id: str | None,
    terminal_reason: TerminalReason | None,
    cost_usd: float = 0.0,
    error_message: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> Issue:
    """Inject an Issue + (optional) finalised RunAttempt directly into the
    orchestrator + bridge so we can build a parent + children graph
    without running the runner. Mirrors the direct-injection pattern
    used by ``test_get_task_result_marks_aborted_attempt_as_cancelled``."""
    labels = ["tool-api"]
    if parent_id is not None:
        labels.append(f"parent:{parent_id}")
    issue = Issue(
        id=task_id,
        identifier=task_id,
        title=title,
        description="",
        priority=1,
        state="open",
        branch_name=None,
        url="",
        labels=labels,
    )
    app_ctx["tracker"].add(issue)
    if terminal_reason is None:
        return issue
    now = datetime.now(timezone.utc)
    att = RunAttempt(
        issue_id=task_id,
        attempt_number=1,
        state=RunState.RELEASED,
        started_at=started_at or (now - timedelta(seconds=10)),
        ended_at=ended_at or now,
        terminal_reason=terminal_reason,
        turns_consumed=1,
        cost_usd=cost_usd,
        error_message=error_message,
    )
    app_ctx["orch"]._attempts[task_id] = att  # noqa: SLF001
    app_ctx["bridge"].record_finalised_attempt(att)
    return issue


def test_get_workflow_result_404_when_task_id_unknown(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/tools/get_workflow_result",
        json={"task_id": "tsk_does_not_exist"},
    )
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def test_get_workflow_result_parent_only_returns_empty_children(app_ctx) -> None:
    """Parent exists, no children decomposed yet → rollup returns just
    the parent's result with `children=[]`. Status follows the parent."""
    _seed_task(
        app_ctx,
        task_id="tsk_parent_lonely",
        title="lone parent",
        parent_id=None,
        terminal_reason=TerminalReason.AGENT_FINISHED,
        cost_usd=0.5,
    )
    r = app_ctx["client"].post(
        "/api/v1/tools/get_workflow_result",
        json={"task_id": "tsk_parent_lonely"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["parent"]["task_id"] == "tsk_parent_lonely"
    assert body["children"] == []
    assert body["aggregate"]["cost_usd"] == pytest.approx(0.5)
    assert body["aggregate"]["counts"] == {"done": 1}


def test_get_workflow_result_happy_aggregates_parent_and_children(app_ctx) -> None:
    """Parent done + 2 children done → rollup status `done`, cost summed."""
    _seed_task(
        app_ctx,
        task_id="tsk_p_ok",
        title="parent ok",
        parent_id=None,
        terminal_reason=TerminalReason.AGENT_FINISHED,
        cost_usd=0.10,
    )
    _seed_task(
        app_ctx,
        task_id="tsk_c1_ok",
        title="child 1 ok",
        parent_id="tsk_p_ok",
        terminal_reason=TerminalReason.AGENT_FINISHED,
        cost_usd=0.20,
    )
    _seed_task(
        app_ctx,
        task_id="tsk_c2_ok",
        title="child 2 ok",
        parent_id="tsk_p_ok",
        terminal_reason=TerminalReason.AGENT_FINISHED,
        cost_usd=0.30,
    )
    r = app_ctx["client"].post(
        "/api/v1/tools/get_workflow_result",
        json={"task_id": "tsk_p_ok"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert {c["task_id"] for c in body["children"]} == {"tsk_c1_ok", "tsk_c2_ok"}
    assert body["aggregate"]["cost_usd"] == pytest.approx(0.60)
    assert body["aggregate"]["counts"] == {"done": 3}


def test_get_workflow_result_partial_in_flight_returns_running(app_ctx) -> None:
    """Parent done + one child done + one child still running →
    rollup status `running` (caller keeps polling)."""
    _seed_task(
        app_ctx,
        task_id="tsk_p_partial",
        title="parent partial",
        parent_id=None,
        terminal_reason=TerminalReason.AGENT_FINISHED,
    )
    _seed_task(
        app_ctx,
        task_id="tsk_c_done",
        title="finished child",
        parent_id="tsk_p_partial",
        terminal_reason=TerminalReason.AGENT_FINISHED,
    )
    # Second child: tracker entry only, no attempt → status="pending"
    _seed_task(
        app_ctx,
        task_id="tsk_c_pending",
        title="not yet started",
        parent_id="tsk_p_partial",
        terminal_reason=None,
    )
    r = app_ctx["client"].post(
        "/api/v1/tools/get_workflow_result",
        json={"task_id": "tsk_p_partial"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert body["aggregate"]["counts"]["pending"] == 1
    assert body["aggregate"]["counts"]["done"] == 2


def test_get_workflow_result_failure_precedence(app_ctx) -> None:
    """One failed child outweighs many done children → status `failed`."""
    _seed_task(
        app_ctx,
        task_id="tsk_p_failmix",
        title="parent failmix",
        parent_id=None,
        terminal_reason=TerminalReason.AGENT_FINISHED,
    )
    _seed_task(
        app_ctx,
        task_id="tsk_c_ok1",
        title="ok child",
        parent_id="tsk_p_failmix",
        terminal_reason=TerminalReason.AGENT_FINISHED,
    )
    _seed_task(
        app_ctx,
        task_id="tsk_c_err",
        title="errored child",
        parent_id="tsk_p_failmix",
        terminal_reason=TerminalReason.ERROR,
        error_message="boom",
    )
    r = app_ctx["client"].post(
        "/api/v1/tools/get_workflow_result",
        json={"task_id": "tsk_p_failmix"},
    )
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "failed"
    assert body["aggregate"]["counts"]["failed"] == 1


def test_get_workflow_result_block_for_human_outranks_failure(app_ctx) -> None:
    """`blocked_for_human` is the most actionable; it wins even when a
    sibling has failed (operator should address the block first)."""
    _seed_task(
        app_ctx,
        task_id="tsk_p_mix2",
        title="parent mix2",
        parent_id=None,
        terminal_reason=TerminalReason.AGENT_FINISHED,
    )
    _seed_task(
        app_ctx,
        task_id="tsk_c_block",
        title="blocked child",
        parent_id="tsk_p_mix2",
        terminal_reason=TerminalReason.NEEDS_HUMAN,
    )
    _seed_task(
        app_ctx,
        task_id="tsk_c_failed",
        title="failed child",
        parent_id="tsk_p_mix2",
        terminal_reason=TerminalReason.ERROR,
    )
    r = app_ctx["client"].post(
        "/api/v1/tools/get_workflow_result",
        json={"task_id": "tsk_p_mix2"},
    )
    body = r.json()
    assert body["status"] == "blocked_for_human"
    assert body["aggregate"]["counts"]["blocked_for_human"] == 1
    assert body["aggregate"]["counts"]["failed"] == 1


def test_get_workflow_result_response_carries_trace_and_detailed_url(
    app_ctx,
) -> None:
    """Smoke: trace_id label is propagated and a detailed_url is built."""
    submit = app_ctx["client"].post(
        "/api/v1/tools/submit_coding_task",
        json={"task": "rollup link smoke", "repo": "memory"},
    )
    task_id = submit.json()["task_id"]
    r = app_ctx["client"].post(
        "/api/v1/tools/get_workflow_result",
        json={"task_id": task_id},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["detailed_url"].endswith(f"/issues/{task_id}")
    # submit_coding_task always tags the parent with a trace label
    assert body["trace_id"] is not None and len(body["trace_id"]) == 32
