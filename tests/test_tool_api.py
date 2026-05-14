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
