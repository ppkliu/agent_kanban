"""Tests for symphony_mvp.dashboard.stage — pure-function event → Stage map."""
from __future__ import annotations

from symphony_mvp.agent_runner import AgentEventKind
from symphony_mvp.dashboard.stage import derive_stage, translate_event_to_stage


def _ev(kind: AgentEventKind, **data) -> dict:
    return {"kind": kind.value, "data": data}


def test_grep_tool_call_maps_to_exploring_codebase() -> None:
    e = _ev(AgentEventKind.TOOL_CALL, tool="grep", input={"pattern": "TODO"})
    assert translate_event_to_stage(e) == "exploring_codebase"


def test_edit_tool_call_maps_to_modifying_files() -> None:
    e = _ev(AgentEventKind.TOOL_CALL, tool="edit", input={"path": "src/x.py"})
    assert translate_event_to_stage(e) == "modifying_files"


def test_write_tool_call_maps_to_modifying_files() -> None:
    e = _ev(
        AgentEventKind.TOOL_CALL, tool="write", input={"path": "src/y.py", "content": "..."}
    )
    assert translate_event_to_stage(e) == "modifying_files"


def test_bash_with_pytest_maps_to_running_tests() -> None:
    e = _ev(
        AgentEventKind.TOOL_CALL, tool="bash", input={"command": "pytest tests/ -q"}
    )
    assert translate_event_to_stage(e) == "running_tests"


def test_bash_with_unrelated_command_returns_none() -> None:
    e = _ev(AgentEventKind.TOOL_CALL, tool="bash", input={"command": "ls -la"})
    assert translate_event_to_stage(e) is None


def test_turn_completed_maps_to_summarising() -> None:
    assert translate_event_to_stage(_ev(AgentEventKind.TURN_COMPLETED)) == "summarising"


def test_done_event_maps_to_done() -> None:
    assert translate_event_to_stage(_ev(AgentEventKind.DONE, reason="agent_finished")) == "done"


def test_error_event_maps_to_failed() -> None:
    assert translate_event_to_stage(_ev(AgentEventKind.ERROR, message="boom")) == "failed"


def test_message_delta_returns_none() -> None:
    assert translate_event_to_stage(_ev(AgentEventKind.MESSAGE_DELTA, text="hi")) is None


def test_derive_stage_returns_most_recent_observed() -> None:
    events = [
        _ev(AgentEventKind.TOOL_CALL, tool="grep", input={"pattern": "x"}),
        _ev(AgentEventKind.MESSAGE_DELTA, text="found"),
        _ev(AgentEventKind.TOOL_CALL, tool="edit", input={"path": "a.py"}),
        _ev(AgentEventKind.TOOL_CALL, tool="bash", input={"command": "vitest run"}),
    ]
    assert derive_stage(events, "queued") == "running_tests"


def test_derive_stage_uses_fallback_when_no_event_implies_stage() -> None:
    events = [_ev(AgentEventKind.MESSAGE_DELTA, text="hi")]
    assert derive_stage(events, "running") == "running"
