"""CHECKPOINT mid-flight progress marker (Phase D4).

When a long-running agent wants to report progress without ending its
turn, it emits a one-line marker inside any assistant message:

    [CHECKPOINT] <free-form progress message>
    [CHECKPOINT step=2/5] <message with structured progress>

The orchestrator scans every ``MESSAGE_DELTA`` event's text for the
marker and, on match, emits a synthetic ``AgentEventKind.CHECKPOINT``
event into the bridge. Dashboards consume these to show what the agent
is actually doing right now (a kanban-card ticker, an events-tab badge)
instead of waiting for the terminal reason at the end of the run.

Like the D3 ``[HUMAN_REQUIRED]`` marker, this is a plain-text convention
that works with every runner backend. Fenced markdown code blocks
(``` ``` ```...``` ``` ``` and ``~~~...~~~``) are stripped before scanning so an
agent quoting source code that contains the literal marker string does
not accidentally emit a phantom checkpoint.
"""
from __future__ import annotations

import re
from typing import TypedDict

CHECKPOINT_MARKER = "[CHECKPOINT]"


class CheckpointSpec(TypedDict, total=False):
    message: str
    step: int
    total: int


# Match the marker, with an optional ``step=<n>/<m>`` attribute inside
# the brackets, then horizontal-only whitespace and the message text up
# to end-of-line. Newlines end the message, so a bare marker on its own
# line yields an empty message rather than swallowing the next paragraph.
_MARKER_RE = re.compile(
    r"""
    \[CHECKPOINT
    (?:[ \t]+step=(?P<step>\d+)/(?P<total>\d+))?
    \]
    [ \t]*
    (?P<message>[^\n\r]*)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_FENCE_RE = re.compile(
    r"```[\s\S]*?```|~~~[\s\S]*?~~~", re.MULTILINE
)


def detect_checkpoints(text: str) -> list[CheckpointSpec]:
    """Return every checkpoint marker found in ``text``, in document order.

    Fenced code blocks are removed before scanning. An empty message is
    still a valid checkpoint (returned with ``message=""``) — callers can
    distinguish empty vs missing if they care.
    """
    if not isinstance(text, str) or not text:
        return []
    no_fences = _FENCE_RE.sub("", text)
    out: list[CheckpointSpec] = []
    for m in _MARKER_RE.finditer(no_fences):
        spec: CheckpointSpec = {"message": m.group("message").strip()}
        step = m.group("step")
        total = m.group("total")
        if step is not None and total is not None:
            try:
                spec["step"] = int(step)
                spec["total"] = int(total)
            except ValueError:
                pass
        out.append(spec)
    return out
