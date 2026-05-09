"""Per-task `mode` → runner ``allowed_tools`` mapping (Tool API Phase B,
last sub-piece).

The Tool API accepts ``mode: "plan" | "build" | "review"`` on
``submit_coding_task``. This module is the single source of truth for what
each mode actually means in terms of which runner tools are unlocked.

Q3 §7.4.2 calls out that mode MUST be a hard tool whitelist, not just a
prompt suggestion. This module is what makes that real: the orchestrator
reads the mode off the issue's labels and threads ``allowed_tools=...``
through to ``runner.run()``, which in turn rebuilds the per-attempt argv
or per-attempt CLI flags.

Modes
-----
- ``plan``    — read-only exploration. No edit/write/bash mutations.
- ``build``   — full toolset (the historical default).
- ``review``  — pure inspection. Even bash is denied.
"""
from __future__ import annotations

from typing import Final, Literal

Mode = Literal["plan", "build", "review"]

#: The hard whitelist per mode. Returned values match opencode's
#: ``--allowed-tools`` flag and the AgentRunner Protocol's optional
#: ``allowed_tools`` kwarg. Lower-case names match opencode CLI; the
#: ClaudeCLIRunner uses Title-case names but maps the same conceptual
#: tools, so the orchestrator-level override is portable across runners.
MODE_ALLOWED_TOOLS: Final[dict[Mode, list[str]]] = {
    "plan":   ["bash", "read", "grep"],
    "build":  ["bash", "read", "edit", "write"],
    "review": ["read", "grep"],
}

#: Issue label prefix Symphony stores the chosen mode under so it travels
#: through the existing label-based metadata channel without needing a new
#: column on Issue. ``submit_coding_task`` writes
#: ``f"{MODE_LABEL_PREFIX}{mode}"`` into ``issue.labels``; the orchestrator
#: reads it back and feeds it to the runner.
MODE_LABEL_PREFIX: Final[str] = "mode:"

#: The default when no ``mode:*`` label is present. Matches the historical
#: behaviour for issues that came in via the kanban or other paths.
DEFAULT_MODE: Final[Mode] = "build"


def is_valid_mode(value: str) -> bool:
    """Cheap predicate for Pydantic / FastAPI validators."""
    return value in MODE_ALLOWED_TOOLS


def allowed_tools_for(mode: str) -> list[str]:
    """Return the hard whitelist for ``mode``. Unknown values fall back to
    ``DEFAULT_MODE`` rather than raising — defensive against label-mangling
    in storage layers."""
    if mode in MODE_ALLOWED_TOOLS:
        return list(MODE_ALLOWED_TOOLS[mode])  # type: ignore[index]
    return list(MODE_ALLOWED_TOOLS[DEFAULT_MODE])


def extract_mode_from_labels(labels: list[str] | None) -> Mode | None:
    """Pull the first ``mode:*`` label off an issue, validate, and return it.

    Returns ``None`` (not the default) when no label is present so callers
    can distinguish "no mode set" from "explicitly build". The orchestrator
    uses ``None`` to mean "use the runner's configured default" — which
    avoids accidentally narrowing tools for legacy issues.
    """
    if not labels:
        return None
    for label in labels:
        if label.startswith(MODE_LABEL_PREFIX):
            value = label[len(MODE_LABEL_PREFIX):].strip().lower()
            if is_valid_mode(value):
                return value  # type: ignore[return-value]
    return None
