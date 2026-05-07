"""Tests for AgentEvent translation in the Claude CLI runner.

Verifies the mapping from Claude CLI NDJSON event types to AgentEventKind,
which is exactly the abstraction Symphony spec calls out as replaceable from
Codex App-Server JSON-RPC.
"""
from __future__ import annotations

from symphony_mvp.agent_runner import (
    AgentEventKind,
    ClaudeCLIRunner,
    EchoRunner,
    OpenCodeRunner,
)


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


# --------------------------------------------------------------------------- #
# OpenCodeRunner translation tests — mirrors the ClaudeCLIRunner pattern.     #
# Real `opencode` binary is intentionally NOT spawned; only the pure          #
# wire-format → AgentEvent mapping and CLI argv shape are tested.             #
# --------------------------------------------------------------------------- #


def test_opencode_translate_session_start() -> None:
    msg = {"kind": "session_start", "session_id": "s1"}
    events = list(OpenCodeRunner._translate(msg))  # noqa: SLF001
    assert len(events) == 1
    assert events[0].kind == AgentEventKind.ITEM_STARTED
    assert events[0].data["session_id"] == "s1"
    assert events[0].data["item_kind"] == "session_start"


def test_opencode_translate_assistant_text() -> None:
    msg = {"kind": "message", "text": "hi"}
    events = list(OpenCodeRunner._translate(msg))  # noqa: SLF001
    assert len(events) == 1
    assert events[0].kind == AgentEventKind.MESSAGE_DELTA
    assert events[0].data["text"] == "hi"


def test_opencode_translate_tool_call() -> None:
    msg = {
        "kind": "tool_call",
        "name": "Bash",
        "id": "c1",
        "input": {"command": "ls"},
    }
    events = list(OpenCodeRunner._translate(msg))  # noqa: SLF001
    assert events[0].kind == AgentEventKind.TOOL_CALL
    assert events[0].data["tool"] == "Bash"
    assert events[0].data["tool_use_id"] == "c1"
    assert events[0].data["input"] == {"command": "ls"}


def test_opencode_translate_tool_result() -> None:
    msg = {"kind": "tool_result", "tool_use_id": "c1", "is_error": False}
    events = list(OpenCodeRunner._translate(msg))  # noqa: SLF001
    assert events[0].kind == AgentEventKind.TOOL_RESULT
    assert events[0].data["tool_use_id"] == "c1"
    assert events[0].data["is_error"] is False


def test_opencode_translate_turn_complete() -> None:
    msg = {
        "kind": "turn_complete",
        "cost_usd": 0.012,
        "session_id": "s1",
        "turn": 2,
    }
    events = list(OpenCodeRunner._translate(msg))  # noqa: SLF001
    assert events[0].kind == AgentEventKind.TURN_COMPLETED
    assert events[0].data["cost_usd"] == 0.012
    assert events[0].data["session_id"] == "s1"
    assert events[0].data["num_turns"] == 2


def test_opencode_translate_error() -> None:
    msg = {"kind": "error", "message": "boom"}
    events = list(OpenCodeRunner._translate(msg))  # noqa: SLF001
    assert events[0].kind == AgentEventKind.ERROR
    assert events[0].data["message"] == "boom"


def test_opencode_build_argv_basic() -> None:
    runner = OpenCodeRunner(provider="anthropic", model="claude-opus-4-7")
    argv = runner._build_argv(  # noqa: SLF001
        prompt="hi", max_turns=20, session_id=None
    )
    assert argv[0] == "opencode"
    assert argv[1] == "run"
    assert argv[2] == "hi"
    assert "--print" in argv
    assert "--provider" in argv and argv[argv.index("--provider") + 1] == "anthropic"
    assert "--max-turns" in argv and argv[argv.index("--max-turns") + 1] == "20"
    assert "--allowed-tools" in argv
    assert "--model" in argv and argv[argv.index("--model") + 1] == "claude-opus-4-7"
    assert "--session" not in argv  # not provided ⇒ flag omitted


def test_opencode_build_argv_with_session() -> None:
    runner = OpenCodeRunner()
    argv = runner._build_argv(  # noqa: SLF001
        prompt="hi", max_turns=5, session_id="abc-123"
    )
    assert "--session" in argv
    assert argv[argv.index("--session") + 1] == "abc-123"
