"""NEEDS_HUMAN escalation marker (Phase D3).

When an agent decides it can't proceed (ambiguous spec, missing access,
high-risk decision), it ends its turn with a one-line marker:

    [HUMAN_REQUIRED] <one-sentence reason>

The orchestrator detects the marker in the attempt's final assistant
messages and releases the attempt with ``TerminalReason.NEEDS_HUMAN``,
which freezes any children of this parent in the D1 dispatch gate.
Operators clear the block via the Tool API's ``resolve_human_block``
endpoint (records a hint + force-retries).

The marker is intentionally a plain text convention — works with every
runner backend (echo / opencode / anthropic_api / claude_cli) without
adding new tool definitions. Prompt-injection defence: any marker
appearing inside a fenced markdown code block (``` ``` ``` ```) is
ignored, so an agent quoting source code containing the literal string
``[HUMAN_REQUIRED]`` doesn't accidentally escalate.
"""
from __future__ import annotations

import re

# Public string the marker uses. Surface in user manual / runner prompts.
HUMAN_REQUIRED_MARKER = "[HUMAN_REQUIRED]"

# Match the marker followed by an inline reason up to end-of-line. The
# `re.IGNORECASE` lets agents that write "[Human_Required]" still trip
# the gate; the marker is operator-facing not protocol-strict.
_MARKER_RE = re.compile(
    r"\[HUMAN_REQUIRED\]\s*(.+?)(?:\r?\n|$)", re.IGNORECASE
)

# Strip fenced code blocks before scanning. Both ```...``` and ~~~...~~~
# variants are removed. Avoids false positives when an agent quotes
# source / docs that mention the literal marker string.
_FENCE_RE = re.compile(
    r"```[\s\S]*?```|~~~[\s\S]*?~~~", re.MULTILINE
)


def detect_needs_human(text: str) -> str | None:
    """Return the operator-supplied reason if `text` declares
    ``[HUMAN_REQUIRED] <reason>`` outside any markdown fence. ``None``
    when no marker is present.

    The reason is the rest of the marker line, trimmed. Empty / missing
    reason still counts as a NEEDS_HUMAN signal — returns the marker
    constant as a placeholder so callers can downstream-handle the
    empty-reason case without a None check.
    """
    if not isinstance(text, str) or not text:
        return None
    no_fences = _FENCE_RE.sub("", text)
    m = _MARKER_RE.search(no_fences)
    if not m:
        return None
    reason = m.group(1).strip()
    return reason if reason else "(no reason supplied)"
