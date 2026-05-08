"""Tests for symphony_mvp.dashboard.task_result derivation."""
from __future__ import annotations

from symphony_mvp.agent_runner import AgentEventKind
from symphony_mvp.dashboard.task_result import (
    FileChange,
    TestRun,
    derive_result,
)


def _ev(kind: AgentEventKind, **data) -> dict:
    return {"kind": kind.value, "data": data, "attempt_number": 1, "id": 1}


def test_files_changed_from_edit_and_write_tool_calls() -> None:
    events = [
        _ev(AgentEventKind.TOOL_CALL, tool="write", tool_use_id="t1",
            input={"path": "src/new.py", "content": "..."}),
        _ev(AgentEventKind.TOOL_CALL, tool="edit", tool_use_id="t2",
            input={"path": "src/old.py"}),
        _ev(AgentEventKind.TOOL_CALL, tool="delete", tool_use_id="t3",
            input={"path": "src/dead.py"}),
    ]
    res = derive_result({}, events, fallback_summary="-")
    paths = {f.path: f.change for f in res.files_changed}
    assert paths == {
        "src/new.py": "added",
        "src/old.py": "modified",
        "src/dead.py": "deleted",
    }


def test_files_changed_dedupes_and_promotes_added_to_modified() -> None:
    """If the same file is created then edited, it ends up 'modified'."""
    events = [
        _ev(AgentEventKind.TOOL_CALL, tool="create", tool_use_id="a",
            input={"path": "x.py"}),
        _ev(AgentEventKind.TOOL_CALL, tool="edit", tool_use_id="b",
            input={"path": "x.py"}),
    ]
    res = derive_result({}, events, fallback_summary="-")
    assert res.files_changed == [FileChange(path="x.py", change="modified")]


def test_files_changed_ignores_tool_calls_without_path() -> None:
    events = [
        _ev(AgentEventKind.TOOL_CALL, tool="grep", tool_use_id="t1",
            input={"pattern": "TODO"}),
        _ev(AgentEventKind.TOOL_CALL, tool="bash", tool_use_id="t2",
            input={"command": "ls"}),
    ]
    res = derive_result({}, events, fallback_summary="-")
    assert res.files_changed == []


def test_tests_run_parses_pytest_output() -> None:
    events = [
        _ev(AgentEventKind.TOOL_CALL, tool="bash", tool_use_id="b1",
            input={"command": "pytest -q"}),
        _ev(AgentEventKind.TOOL_RESULT, tool_use_id="b1",
            output="............\n12 passed in 0.4s"),
    ]
    res = derive_result({}, events, fallback_summary="-")
    assert res.tests_run == TestRun(passed=12, failed=0, framework="pytest")


def test_tests_run_parses_failures() -> None:
    events = [
        _ev(AgentEventKind.TOOL_CALL, tool="bash", tool_use_id="b1",
            input={"command": "vitest run"}),
        _ev(AgentEventKind.TOOL_RESULT, tool_use_id="b1",
            output="Tests:  3 failed | 9 passed (12)"),
    ]
    res = derive_result({}, events, fallback_summary="-")
    assert res.tests_run is not None
    assert res.tests_run.passed == 9
    assert res.tests_run.failed == 3
    assert res.tests_run.framework == "vitest"


def test_tests_run_none_when_no_test_command() -> None:
    events = [
        _ev(AgentEventKind.TOOL_CALL, tool="bash", tool_use_id="b1",
            input={"command": "ls -la"}),
        _ev(AgentEventKind.TOOL_RESULT, tool_use_id="b1", output="..."),
    ]
    res = derive_result({}, events, fallback_summary="-")
    assert res.tests_run is None


def test_summary_uses_last_message_delta_run() -> None:
    events = [
        _ev(AgentEventKind.MESSAGE_DELTA, text="Looking at the auth code. "),
        _ev(AgentEventKind.TOOL_CALL, tool="grep", input={"pattern": "x"}),
        _ev(AgentEventKind.MESSAGE_DELTA, text="Found the bug. "),
        _ev(AgentEventKind.MESSAGE_DELTA, text="Fixed it and added a test."),
    ]
    res = derive_result({}, events, fallback_summary="ignored")
    assert "Found the bug" in res.summary
    assert "Fixed it" in res.summary
    # Earlier delta block should NOT bleed in
    assert "Looking at" not in res.summary


def test_summary_falls_back_when_no_message_delta() -> None:
    res = derive_result(
        {"terminal_reason": "error"}, events=[], fallback_summary="Task failed."
    )
    assert res.summary == "Task failed."


def test_blockers_and_followups_extracted_from_summary() -> None:
    events = [
        _ev(
            AgentEventKind.MESSAGE_DELTA,
            text=(
                "Implemented OAuth handler. Requires the ANTHROPIC_API_KEY env "
                "var to be set in production. Consider adding integration tests "
                "for the redirect flow. Suggest reviewing src/auth/redirect.ts."
            ),
        ),
    ]
    res = derive_result({}, events, fallback_summary="-")
    assert any("ANTHROPIC_API_KEY" in b for b in res.blockers)
    assert any("integration tests" in f for f in res.follow_ups)
    assert any("reviewing" in f for f in res.follow_ups)


def test_blockers_capped_at_five_and_deduped() -> None:
    # Repeat the same "requires X" sentence 7 times → cap 5 + dedup → 1
    text = ". ".join(["Requires the FOO env"] * 7) + "."
    events = [_ev(AgentEventKind.MESSAGE_DELTA, text=text)]
    res = derive_result({}, events, fallback_summary="-")
    assert len(res.blockers) == 1
    assert "FOO" in res.blockers[0]
