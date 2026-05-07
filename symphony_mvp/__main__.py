"""CLI entry point — `python -m symphony_mvp ./WORKFLOW.md`."""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from pathlib import Path

from .agent_runner import build_runner
from .orchestrator import Orchestrator
from .tracker import build_tracker
from .workflow import load_workflow
from .workspace import WorkspaceManager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="symphony-mvp")
    parser.add_argument(
        "workflow",
        nargs="?",
        default="./WORKFLOW.md",
        help="Path to WORKFLOW.md (default: ./WORKFLOW.md)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single tick instead of looping forever (useful for CI)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate the workflow file and exit",
    )
    parser.add_argument(
        "--state",
        action="store_true",
        help="Print state snapshot as JSON and exit (after one tick)",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="Increase log verbosity"
    )
    args = parser.parse_args(argv)

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        workflow = load_workflow(args.workflow)
    except ValueError as e:
        print(f"WORKFLOW load error: {e}", file=sys.stderr)
        return 2

    if args.validate:
        print(f"OK: {Path(args.workflow).resolve()} is valid.")
        return 0

    cfg = workflow.config
    tracker = build_tracker(cfg.tracker_kind, cfg.tracker_repo, cfg.tracker_api_key)
    workspaces = WorkspaceManager(cfg.workspace_root)
    runner = build_runner(
        cfg.runner_kind,
        command=cfg.runner_command,
        model=cfg.runner_model,
        max_tokens=cfg.runner_max_tokens,
        provider=cfg.runner_provider,
        allowed_tools=cfg.runner_allowed_tools,
    )
    orch = Orchestrator(
        workflow=workflow,
        tracker=tracker,
        workspaces=workspaces,
        runner=runner,
    )

    if args.once or args.state:
        orch.tick()
        if args.state:
            print(json.dumps(orch.state_snapshot(), indent=2, default=str))
        return 0

    def _handle_sigterm(_signum: int, _frame: object) -> None:
        print("Received signal, stopping...", file=sys.stderr)
        orch.stop()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    orch.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
