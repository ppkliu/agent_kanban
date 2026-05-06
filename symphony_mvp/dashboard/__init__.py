"""Symphony Dashboard — optional FastAPI/WebSocket observability + HITL layer.

Importing this subpackage requires the [dashboard] extra:
    pip install -e ".[dashboard]"

The MVP core (orchestrator, tracker, runner, workflow) does NOT depend on
anything in here. Removing this directory entirely keeps `python -m pytest`
at 29/29 — see Definition of Done #11 in docs/design/symphony-dashboard-spec.md.
"""
from .bridge import DashboardBridge

__all__ = ["DashboardBridge"]
