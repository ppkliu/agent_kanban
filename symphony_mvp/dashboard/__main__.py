"""CLI entry — ``python -m symphony_mvp.dashboard ./WORKFLOW.md [opts]``.

Wires up the orchestrator, the bridge, the FastAPI app, and uvicorn into a
single process. The orchestrator runs on a background thread; FastAPI serves
on the main thread.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from ..agent_runner import build_runner
from ..orchestrator import Orchestrator
from ..tracker import build_tracker
from ..workflow import load_workflow
from ..workspace import WorkspaceManager
from .bridge import DashboardBridge
from .server import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="symphony-dashboard")
    parser.add_argument(
        "workflow",
        nargs="?",
        default="./WORKFLOW.md",
        help="Path to WORKFLOW.md (default: ./WORKFLOW.md)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7957)
    parser.add_argument(
        "--db",
        default="./dashboard.db",
        help="Path to SQLite file for event/hint/override storage",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Bearer token; falls back to env DASHBOARD_API_KEY when omitted",
    )
    parser.add_argument(
        "--ring-size",
        type=int,
        default=500,
        help="Per-issue in-memory event ring buffer size",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
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

    bridge = DashboardBridge(args.db, ring_size=args.ring_size)
    orch = Orchestrator(
        workflow=workflow,
        tracker=tracker,
        workspaces=workspaces,
        runner=runner,
        bridge=bridge,
    )
    # Wire the bridge as an event listener so every AgentEvent flows into
    # SQLite + WS subscribers.
    orch.add_event_listener(bridge.on_event)

    app = create_app(
        orch, bridge, workflow_path=Path(args.workflow), api_key=args.api_key
    )

    # Orchestrator on background thread; uvicorn (blocking) on the main thread.
    orch_thread = threading.Thread(
        target=orch.run_forever, name="orchestrator", daemon=True
    )
    orch_thread.start()

    def _shutdown(_signum: int, _frame: object) -> None:
        print("Received signal, stopping...", file=sys.stderr)
        orch.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is required: pip install -e '.[dashboard]'",
            file=sys.stderr,
        )
        return 2
    # Uvicorn's default access-log formatter omits a timestamp, which makes
    # `INFO: ... "GET /healthz HTTP/1.1" 200 OK` lines hard to correlate with
    # other timestamped events. Drop in a custom log_config that prepends
    # ISO-ish timestamps to both uvicorn.error and uvicorn.access loggers.
    log_config: dict[str, object] = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": (
                    "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
                ),
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "access": {
                "format": (
                    "%(asctime)s %(levelname)-7s %(client_addr)s "
                    '"%(request_line)s" %(status_code)s'
                ),
                "class": "uvicorn.logging.AccessFormatter",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {
                "handlers": ["access"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        log_config=log_config,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
