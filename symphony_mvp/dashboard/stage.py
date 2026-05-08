"""Translate raw AgentEvents into a coarse Stage label that upstream LLM
agents can reason about (Q3 §7.4.3).

Pure functions over the dict shape returned by ``DashboardBridge.fetch_events``
— no SQLite, no orchestrator state. That keeps this module trivial to unit
test and trivial to swap out later (e.g. when an opencode-native event
taxonomy lands and we want a richer mapping).
"""
from __future__ import annotations

import re
from typing import Any, Literal

from ..agent_runner import AgentEventKind

Stage = Literal[
    "queued",
    "exploring_codebase",
    "modifying_files",
    "running_tests",
    "summarising",
    "done",
    "failed",
    "cancelled",
]

# Tools that imply the agent is reading the codebase to plan its work.
_EXPLORE_TOOLS = {"grep", "find", "read", "ls", "list_files", "view"}

# Tools that imply the agent is making changes.
_MODIFY_TOOLS = {"edit", "write", "create", "str_replace_editor", "delete"}

# Bash command patterns that imply the agent is running tests.
_TEST_RE = re.compile(
    r"\b(pytest|jest|vitest|go test|cargo test|npm test|yarn test|deno test|"
    r"mvn test|gradle test|rspec|phpunit)\b",
    re.IGNORECASE,
)


def translate_event_to_stage(event: dict[str, Any]) -> Stage | None:
    """Map one event dict (from ``bridge.fetch_events``) to a stage transition.

    Returns None when the event does not change the stage — e.g. a
    `message_delta` mid-conversation has no stage implication on its own.
    """
    kind = event.get("kind", "")
    data = event.get("data") or {}

    if kind == AgentEventKind.TOOL_CALL.value:
        tool = (data.get("tool") or "").lower()
        if tool in _EXPLORE_TOOLS:
            return "exploring_codebase"
        if tool in _MODIFY_TOOLS:
            return "modifying_files"
        if tool == "bash":
            inp = data.get("input")
            cmd = inp.get("command", "") if isinstance(inp, dict) else ""
            if _TEST_RE.search(str(cmd)):
                return "running_tests"
        return None
    if kind == AgentEventKind.TURN_COMPLETED.value:
        return "summarising"
    if kind == AgentEventKind.DONE.value:
        return "done"
    if kind == AgentEventKind.ERROR.value:
        return "failed"
    if kind == AgentEventKind.USER_INPUT_REQUIRED.value:
        return "failed"
    return None


def derive_stage(events: list[dict[str, Any]], fallback: Stage) -> Stage:
    """Walk events newest-to-oldest; return the most recent observed stage.

    ``fallback`` is used when no event implies a stage (e.g. attempt has no
    events recorded yet). Callers usually pass a status-derived stage like
    ``"queued"`` or ``"running"``.
    """
    for event in reversed(events):
        s = translate_event_to_stage(event)
        if s is not None:
            return s
    return fallback
