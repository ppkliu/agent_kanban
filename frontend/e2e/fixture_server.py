"""Playwright fixture server.

Spins up a real Symphony orchestrator + dashboard FastAPI in the same process,
seeded with deterministic InMemoryTracker issues so E2E specs can target
known identifiers (MT-1, MT-2, MT-3) and known FSM states.

The orchestrator runs on a background thread and uses ``runner.kind: echo``
so no LLM is involved. SQLite path defaults to a temp file that the script
deletes on exit.

Usage (called by playwright.config.ts webServer):
    python frontend/e2e/fixture_server.py --port 7958 --workflow tmp.md
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

# Ensure the repo root is on sys.path so symphony_mvp imports work when this
# script is launched from frontend/ via Playwright's webServer.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from symphony_mvp.agent_runner import EchoRunner  # noqa: E402
from symphony_mvp.dashboard.bridge import DashboardBridge  # noqa: E402
from symphony_mvp.dashboard.server import create_app  # noqa: E402
from symphony_mvp.models import Issue  # noqa: E402
from symphony_mvp.orchestrator import Orchestrator  # noqa: E402
from symphony_mvp.tracker import InMemoryTracker  # noqa: E402
from symphony_mvp.workflow import load_workflow  # noqa: E402
from symphony_mvp.workspace import WorkspaceManager  # noqa: E402


WORKFLOW_TEMPLATE = """---
tracker:
  kind: memory
polling:
  interval_ms: 200
agent:
  max_concurrent_agents: 1
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


def _seed_issues() -> list[Issue]:
    """Three deterministic issues — referenced by id/identifier in E2E specs."""
    base_ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
    return [
        Issue(
            id=f"id-MT-{i}",
            identifier=f"MT-{i}",
            title=f"E2E test issue {i}",
            description=f"Body for MT-{i}",
            priority=i,
            state="open",
            branch_name=None,
            url=f"https://example.com/MT-{i}",
            labels=[f"label-{i}"],
            blocked_by=[],
            created_at=base_ts,
            updated_at=base_ts,
        )
        for i in (1, 2, 3)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7958)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    # Use a temp dir for the workflow + workspace + db so each Playwright run
    # is hermetic.
    tmp = Path(tempfile.mkdtemp(prefix="symphony-e2e-"))
    wf_path = tmp / "WORKFLOW.md"
    wf_path.write_text(WORKFLOW_TEMPLATE, encoding="utf-8")
    workflow = load_workflow(wf_path)
    workflow.config.workspace_root = str(tmp / "workspaces")

    tracker = InMemoryTracker(_seed_issues())
    workspaces = WorkspaceManager(workflow.config.workspace_root)
    runner = EchoRunner(delay_s=0.3)  # small delay so RUNNING is observable

    bridge = DashboardBridge(str(tmp / "dashboard.db"))
    orch = Orchestrator(
        workflow=workflow,
        tracker=tracker,
        workspaces=workspaces,
        runner=runner,
        bridge=bridge,
    )
    orch.add_event_listener(bridge.on_event)

    app = create_app(orch, bridge, workflow_path=wf_path, api_key=None)

    def _shutdown(_signum: object, _frame: object) -> None:
        print("fixture_server: shutting down", file=sys.stderr)
        orch.stop()
        try:
            bridge.close()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    threading.Thread(
        target=orch.run_forever, name="orchestrator", daemon=True
    ).start()

    import uvicorn  # noqa: PLC0415

    print(
        f"fixture_server: orchestrator + dashboard live on "
        f"http://{args.host}:{args.port} (tmp={tmp})",
        file=sys.stderr,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
