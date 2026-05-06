"""Tests for AgentEvent translation in the Claude CLI runner.

Verifies the mapping from Claude CLI NDJSON event types to AgentEventKind,
which is exactly the abstraction Symphony spec calls out as replaceable from
Codex App-Server JSON-RPC.
"""
from __future__ import annotations

from symphony_mvp.agent_runner import AgentEventKind, ClaudeCLIRunner, EchoRunner


def test_translate_system_init() -> None:
    msg = {"type": "system", "subtype": "init", "session_id": "abc123"}
    events = list(ClaudeCLIRunner._translate(msg))  # noqa: SLF001
    assert len(events) == 1
    assert events[0].kind == AgentEventKind.ITEM_STARTED
    assert events[0].data["session_id"] == "abc123"


def test_translate_assistant_text() -> None:
    msg = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "hello"}]},
    }
    events = list(ClaudeCLIRunner._translate(msg))  # noqa: SLF001
    assert events[0].kind == AgentEventKind.MESSAGE_DELTA
    assert events[0].data["text"] == "hello"


def test_translate_tool_use() -> None:
    msg = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "Bash",
                    "input": {"command": "ls"},
                }
            ]
        },
    }
    events = list(ClaudeCLIRunner._translate(msg))  # noqa: SLF001
    assert events[0].kind == AgentEventKind.TOOL_CALL
    assert events[0].data["tool"] == "Bash"


def test_translate_tool_result() -> None:
    msg = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01",
                    "is_error": False,
                }
            ]
        },
    }
    events = list(ClaudeCLIRunner._translate(msg))  # noqa: SLF001
    assert events[0].kind == AgentEventKind.TOOL_RESULT
    assert events[0].data["tool_use_id"] == "toolu_01"


def test_translate_result_success() -> None:
    msg = {
        "type": "result",
        "subtype": "success",
        "session_id": "abc",
        "total_cost_usd": 0.001,
        "num_turns": 3,
    }
    events = list(ClaudeCLIRunner._translate(msg))  # noqa: SLF001
    assert events[0].kind == AgentEventKind.TURN_COMPLETED
    assert events[0].data["session_id"] == "abc"


def test_translate_result_error() -> None:
    msg = {"type": "result", "subtype": "error_max_turns"}
    events = list(ClaudeCLIRunner._translate(msg))  # noqa: SLF001
    assert events[0].kind == AgentEventKind.ERROR


def test_echo_runner_writes_marker(tmp_path) -> None:  # noqa: ANN001
    runner = EchoRunner()
    events = list(
        runner.run(
            prompt="hi",
            workspace_path=str(tmp_path),
            max_turns=1,
        )
    )
    kinds = [e.kind for e in events]
    assert AgentEventKind.TURN_STARTED in kinds
    assert AgentEventKind.DONE in kinds
    assert (tmp_path / "agent_ran.txt").exists()
