"""End-to-end orchestrator tests using EchoRunner + InMemoryTracker.

These prove the orchestrator handles the full FSM lifecycle without external
dependencies — no LLM calls, no GitHub API, no Linear.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from symphony_mvp import (
    AgentEventKind,
    EchoRunner,
    InMemoryTracker,
    Issue,
    Orchestrator,
    RunState,
    TerminalReason,
    WorkspaceManager,
    load_workflow,
)


WORKFLOW_TEMPLATE = """---
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
"""


def _setup(tmp_path: Path, runner=None, issues=None):
    wf_path = tmp_path / "WORKFLOW.md"
    wf_path.write_text(WORKFLOW_TEMPLATE, encoding="utf-8")
    workflow = load_workflow(wf_path)
    workflow.config.workspace_root = str(tmp_path / "workspaces")
    tracker = InMemoryTracker(issues or [])
    workspaces = WorkspaceManager(workflow.config.workspace_root)
    runner = runner or EchoRunner()
    orch = Orchestrator(
        workflow=workflow, tracker=tracker, workspaces=workspaces, runner=runner
    )
    return orch, tracker, workspaces


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
    raise AssertionError(
        f"Issue {issue_id} did not reach RELEASED in {timeout}s"
    )


def test_dispatch_completes_via_echo(tmp_path: Path) -> None:
    issue = _make_issue("MT-1")
    orch, tracker, _ = _setup(tmp_path, issues=[issue])

    _run_until_released(orch, issue.id)

    att = orch._attempts[issue.id]  # noqa: SLF001
    assert att.state == RunState.RELEASED
    assert att.terminal_reason == TerminalReason.AGENT_FINISHED
    assert att.turns_consumed >= 1

    # SPEC invariant #1: agent ran with cwd == workspace_path.
    # EchoRunner writes "agent_ran.txt" in cwd; verify it landed in the workspace.
    ws_dir = Path(orch.workflow.config.workspace_root) / "MT-1"
    assert (ws_dir / "agent_ran.txt").exists()


def test_concurrency_cap_respected(tmp_path: Path) -> None:
    issues = [_make_issue(f"MT-{i}", priority=i) for i in range(1, 6)]
    # delay_s ensures workers are still running when we observe.
    orch, _, _ = _setup(tmp_path, runner=EchoRunner(delay_s=0.5), issues=issues)

    orch.tick()
    time.sleep(0.05)  # Let worker threads start
    active = sum(
        1 for a in orch._attempts.values()  # noqa: SLF001
        if a.state in (RunState.CLAIMED, RunState.RUNNING)
    )
    assert active <= orch.workflow.config.max_concurrent_agents


def test_priority_sorting(tmp_path: Path) -> None:
    """Higher priority (lower number) dispatches first when slots are scarce."""
    high = _make_issue("MT-HI", priority=1)
    low = _make_issue("MT-LO", priority=5)
    orch, _, _ = _setup(tmp_path, runner=EchoRunner(delay_s=0.5), issues=[low, high])
    # max_concurrent_agents is 2 in template; both should dispatch but order matters
    # Make sure the high-pri one is in the snapshot first.
    orch.tick()
    snapshot = orch.state_snapshot()
    seen_ids = [a["issue_id"] for a in snapshot["attempts"]]
    assert seen_ids[0].endswith("MT-HI"), seen_ids


def test_external_terminal_state_releases(tmp_path: Path) -> None:
    """Tracker says issue is now Done; reconcile must release it."""
    # Slow runner so it's still running when we transition externally
    issue = _make_issue("MT-DONE")
    orch, tracker, _ = _setup(
        tmp_path, runner=EchoRunner(delay_s=2.0), issues=[issue]
    )
    orch.tick()
    time.sleep(0.1)
    tracker.transition(issue.id, "closed")
    orch.tick()
    att = orch._attempts[issue.id]  # noqa: SLF001
    assert att.terminal_reason == TerminalReason.TRACKER_TERMINAL


def test_handoff_state_releases(tmp_path: Path) -> None:
    issue = _make_issue("MT-REVIEW")
    orch, tracker, _ = _setup(
        tmp_path, runner=EchoRunner(delay_s=2.0), issues=[issue]
    )
    orch.tick()
    time.sleep(0.1)
    tracker.transition(issue.id, "in_review")
    orch.tick()
    att = orch._attempts[issue.id]  # noqa: SLF001
    assert att.terminal_reason == TerminalReason.HANDOFF


def test_blocked_by_holds_dispatch(tmp_path: Path) -> None:
    blocker = _make_issue("MT-BLOCK", state="open")
    blocked = _make_issue(
        "MT-WAIT", state="open", blocked_by=[blocker.id]
    )
    orch, _, _ = _setup(tmp_path, issues=[blocker, blocked])

    orch.tick()
    # Only the unblocked one should be dispatched
    att_block = orch._attempts.get(blocker.id)  # noqa: SLF001
    att_wait = orch._attempts.get(blocked.id)  # noqa: SLF001
    assert att_block is not None
    assert att_wait is None  # didn't dispatch


def test_state_snapshot_serializable(tmp_path: Path) -> None:
    import json
    issue = _make_issue("MT-SNAP")
    orch, _, _ = _setup(tmp_path, issues=[issue])
    orch.tick()
    snap = orch.state_snapshot()
    # Should round-trip through JSON
    s = json.dumps(snap, default=str)
    assert "MT-SNAP" in s
    parsed = json.loads(s)
    assert "attempts" in parsed
