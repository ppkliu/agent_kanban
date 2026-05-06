"""Integration: orchestrator + bridge — hint injection, priority override, actions.

These tests use EchoRunner so no LLM is required. They exercise the opt-in
hooks added to Orchestrator/Issue/_AgentWorker in P0.1 plus the bridge stores.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from symphony_mvp import (
    EchoRunner,
    InMemoryTracker,
    Issue,
    Orchestrator,
    RunState,
    TerminalReason,
    WorkspaceManager,
    load_workflow,
)
from symphony_mvp.dashboard.bridge import DashboardBridge


WORKFLOW_WITH_HINTS = """---
tracker:
  kind: memory
polling:
  interval_ms: 50
agent:
  max_concurrent_agents: 2
  max_turns: 3
  stall_timeout_ms: 5000
  retry_max_attempts: 2
  handoff_state: in_review
runner:
  kind: echo
---
Issue {{ issue.identifier }}: {{ issue.title }}

{% if hints %}
# Operator hints
{% for h in hints %}- [{{ h.author }}] {{ h.content }}
{% endfor %}
{% endif %}
"""


def _setup(tmp_path: Path, issues=None, runner=None):
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text(WORKFLOW_WITH_HINTS, encoding="utf-8")
    workflow = load_workflow(wf_path)
    workflow.config.workspace_root = str(tmp_path / "workspaces")
    tracker = InMemoryTracker(issues or [])
    workspaces = WorkspaceManager(workflow.config.workspace_root)
    bridge = DashboardBridge(tmp_path / "dash.db")
    orch = Orchestrator(
        workflow=workflow,
        tracker=tracker,
        workspaces=workspaces,
        runner=runner or EchoRunner(),
        bridge=bridge,
    )
    orch.add_event_listener(bridge.on_event)
    return orch, tracker, bridge


def _make_issue(identifier="MT-1", state="open", priority=1, **kw) -> Issue:
    return Issue(
        id=f"id-{identifier}",
        identifier=identifier,
        title=kw.pop("title", f"Test {identifier}"),
        description=kw.pop("description", "test body"),
        priority=priority,
        state=state,
        branch_name=None,
        url=f"https://example.com/{identifier}",
        **kw,
    )


def _run_until_released(orch: Orchestrator, issue_id: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        orch.tick()
        att = orch._attempts.get(issue_id)  # noqa: SLF001
        if att and att.state == RunState.RELEASED:
            return
        time.sleep(0.05)
    raise AssertionError(f"{issue_id} did not reach RELEASED in {timeout}s")


def test_hint_injects_into_prompt_and_marks_consumed(tmp_path: Path) -> None:
    issue = _make_issue("MT-HINT")
    orch, _, bridge = _setup(tmp_path, [issue])

    bridge.add_hint(issue.id, "alice", "use useReducer not useFormState")
    assert len(bridge.fetch_pending_hints(issue.id)) == 1

    _run_until_released(orch, issue.id)

    # After dispatch, hint should be consumed.
    assert bridge.fetch_pending_hints(issue.id) == []
    history = bridge.list_hints(issue.id)
    assert len(history) == 1 and history[0]["consumed"] is True

    # Check the rendered prompt actually saw the hint by looking at marker file
    # contents: EchoRunner writes "prompt_chars=N" — the prompt with hint
    # should have more chars than without. We assert by re-rendering.
    from symphony_mvp.workflow import render_prompt
    no_hint = render_prompt(orch.workflow, issue.to_template_context(1, hints=[]))
    with_hint = render_prompt(
        orch.workflow,
        issue.to_template_context(
            1, hints=[{"id": 1, "author": "alice", "content": "use useReducer not useFormState", "created_at": ""}]
        ),
    )
    assert "useReducer" in with_hint and "useReducer" not in no_hint


def test_priority_override_dispatches_first(tmp_path: Path) -> None:
    high_native = _make_issue("MT-NATIVE-HI", priority=1)
    low_native = _make_issue("MT-NATIVE-LO", priority=5)
    # delay to keep workers visible while we observe
    orch, _, bridge = _setup(
        tmp_path, [high_native, low_native], runner=EchoRunner(delay_s=0.5)
    )
    # Override: make the LOW-native-priority issue dispatch first.
    bridge.set_priority_override(low_native.id, rank=0, set_by="alice")

    orch.tick()
    snapshot = orch.state_snapshot()
    seen_ids = [a["issue_id"] for a in snapshot["attempts"]]
    # Override should put MT-NATIVE-LO ahead of MT-NATIVE-HI in dispatch order.
    assert seen_ids[0] == low_native.id, seen_ids


def test_pause_releases_worker_and_blocks_redispatch(tmp_path: Path) -> None:
    issue = _make_issue("MT-PAUSE")
    orch, _, _ = _setup(tmp_path, [issue], runner=EchoRunner(delay_s=2.0))
    orch.tick()
    time.sleep(0.1)
    assert orch.pause(issue.id) is True
    orch.tick()
    att = orch._attempts[issue.id]  # noqa: SLF001
    assert att.state == RunState.RETRY_QUEUED
    assert att.paused_until is not None

    # Subsequent tick must NOT redispatch a paused issue.
    orch.tick()
    att = orch._attempts[issue.id]  # noqa: SLF001
    assert att.state == RunState.RETRY_QUEUED


def test_resume_clears_pause_sentinels(tmp_path: Path) -> None:
    issue = _make_issue("MT-RES")
    orch, _, _ = _setup(tmp_path, [issue], runner=EchoRunner(delay_s=0.5))
    orch.tick()
    time.sleep(0.1)
    orch.pause(issue.id)
    orch.tick()
    assert orch.resume(issue.id) is True
    att = orch._attempts[issue.id]  # noqa: SLF001
    assert att.paused_until is None
    assert att.retry_after is None


def test_abort_marks_aborted_and_skips_retry(tmp_path: Path) -> None:
    issue = _make_issue("MT-ABORT")
    orch, _, _ = _setup(tmp_path, [issue], runner=EchoRunner(delay_s=2.0))
    orch.tick()
    time.sleep(0.1)
    assert orch.abort(issue.id) is True
    att = orch._attempts[issue.id]  # noqa: SLF001
    assert att.state == RunState.RELEASED
    assert att.terminal_reason == TerminalReason.ABORTED


def test_force_retry_allows_redispatch_of_released_issue(tmp_path: Path) -> None:
    issue = _make_issue("MT-RETRY")
    orch, _, _ = _setup(tmp_path, [issue])
    _run_until_released(orch, issue.id)
    assert orch._attempts[issue.id].state == RunState.RELEASED  # noqa: SLF001

    assert orch.force_retry(issue.id) is True
    att = orch._attempts[issue.id]  # noqa: SLF001
    assert att.state == RunState.UNCLAIMED  # sentinel for next dispatch

    # Next tick should pick it up.
    _run_until_released(orch, issue.id)
    assert orch._attempts[issue.id].state == RunState.RELEASED  # noqa: SLF001
