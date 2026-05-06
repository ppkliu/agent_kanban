"""End-to-end demo — fully self-contained, no LLM/GitHub required.

What you see:
  * 3 fake issues are added to an in-memory tracker
  * Symphony orchestrator picks them up, dispatches workers, runs the EchoRunner
  * The EchoRunner writes a marker file in each workspace
  * One issue is externally transitioned to 'closed' mid-run -> reconcile releases it
  * Another is moved to 'in_review' -> handoff path
  * Final state snapshot printed as JSON

Run with:
    cd symphony-mvp
    python examples/demo_echo.py
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path

# Make the package importable when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from symphony_mvp import (  # noqa: E402
    EchoRunner,
    InMemoryTracker,
    Issue,
    Orchestrator,
    WorkspaceManager,
    load_workflow,
)


WORKFLOW_TEXT = """---
tracker:
  kind: memory
polling:
  interval_ms: 200
agent:
  max_concurrent_agents: 2
  max_turns: 3
  stall_timeout_ms: 5000
  handoff_state: in_review
runner:
  kind: echo
---
You are working on {{ issue.identifier }} (attempt {{ attempt }}).
Title: {{ issue.title }}
Body: {{ issue.description }}
"""


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    tmpdir = Path("./demo_workspaces")
    tmpdir.mkdir(exist_ok=True)
    wf_path = tmpdir / "WORKFLOW.md"
    wf_path.write_text(WORKFLOW_TEXT, encoding="utf-8")
    workflow = load_workflow(wf_path)
    workflow.config.workspace_root = str(tmpdir / "ws")

    issues = [
        Issue(
            id="demo-1",
            identifier="MT-1",
            title="Refactor login form",
            description="Make it accessible.",
            priority=1,
            state="open",
            branch_name=None,
            url="https://example/MT-1",
            labels=["a11y"],
        ),
        Issue(
            id="demo-2",
            identifier="MT-2",
            title="Add dark mode",
            description="System default + user toggle.",
            priority=2,
            state="open",
            branch_name=None,
            url="https://example/MT-2",
        ),
        Issue(
            id="demo-3",
            identifier="MT-3",
            title="Fix flaky test",
            description="test_concurrency_smoke fails 5% of the time.",
            priority=1,
            state="open",
            branch_name=None,
            url="https://example/MT-3",
        ),
    ]
    tracker = InMemoryTracker(issues)
    workspaces = WorkspaceManager(workflow.config.workspace_root)
    runner = EchoRunner(delay_s=0.3)
    orch = Orchestrator(
        workflow=workflow, tracker=tracker, workspaces=workspaces, runner=runner
    )

    # Subscribe to all events for visibility
    def on_event(issue_id: str, event) -> None:  # noqa: ANN001
        snippet = ""
        if event.kind.value == "message_delta":
            snippet = f" — {event.data.get('text', '')[:60]}"
        print(f"  [{issue_id}] {event.kind.value}{snippet}")

    orch.add_event_listener(on_event)

    # External-state injector: simulate a human moving MT-2 to in_review,
    # and the system marking MT-3 as closed externally during the run.
    def external_changes() -> None:
        time.sleep(0.7)
        print(">>> External: MT-2 moved to 'in_review' (handoff)")
        tracker.transition("demo-2", "in_review")
        time.sleep(0.3)
        print(">>> External: MT-3 closed externally")
        tracker.transition("demo-3", "closed")

    threading.Thread(target=external_changes, daemon=True).start()

    print("=" * 60)
    print("Symphony MVP demo — 3 issues, max_concurrent_agents=2")
    print("=" * 60)

    # Run a few ticks
    for i in range(8):
        print(f"\n--- Tick {i + 1} ---")
        orch.tick()
        time.sleep(0.25)

    # Wait for any remaining workers
    time.sleep(0.5)

    print("\n" + "=" * 60)
    print("Final state:")
    print(json.dumps(orch.state_snapshot(), indent=2, default=str))
    print("\nWorkspaces created:")
    for p in workspaces.list_existing():
        marker = p / "agent_ran.txt"
        print(f"  {p.name}/  marker={'yes' if marker.exists() else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
