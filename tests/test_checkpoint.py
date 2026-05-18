"""Unit tests for the CHECKPOINT marker detector (Phase D4).

Mirrors test_escalation.py for the D3 NEEDS_HUMAN marker — both
modules extract a structured signal from arbitrary LLM output and have
the same fenced-code-block / case-insensitive contract. The difference
is that checkpoints can fire many times per attempt (mid-flight) while
NEEDS_HUMAN is a terminal one-shot.
"""
from __future__ import annotations

from symphony_mvp.checkpoint import (
    CHECKPOINT_MARKER,
    detect_checkpoints,
)


def test_returns_empty_list_on_no_marker() -> None:
    assert detect_checkpoints("hello world, no marker here") == []


def test_parses_single_plain_marker() -> None:
    out = detect_checkpoints("Working on it.\n[CHECKPOINT] step 1 of 3 done")
    assert out == [{"message": "step 1 of 3 done"}]


def test_parses_marker_with_structured_step_attribute() -> None:
    out = detect_checkpoints("[CHECKPOINT step=2/5] tests passing")
    assert out == [{"message": "tests passing", "step": 2, "total": 5}]


def test_parses_multiple_markers_in_document_order() -> None:
    text = (
        "Started.\n"
        "[CHECKPOINT] half way\n"
        "More work...\n"
        "[CHECKPOINT step=3/5] running tests\n"
        "[CHECKPOINT] cleanup\n"
    )
    assert detect_checkpoints(text) == [
        {"message": "half way"},
        {"message": "running tests", "step": 3, "total": 5},
        {"message": "cleanup"},
    ]


def test_empty_message_is_still_a_checkpoint() -> None:
    """Marker with no inline message is still a valid checkpoint —
    operators may want a heartbeat without committing to a message."""
    out = detect_checkpoints("[CHECKPOINT]\nlater stuff")
    assert out == [{"message": ""}]


def test_marker_is_case_insensitive() -> None:
    assert detect_checkpoints("[checkpoint] lowercase") == [
        {"message": "lowercase"}
    ]
    assert detect_checkpoints("[Checkpoint] mixed case") == [
        {"message": "mixed case"}
    ]


def test_marker_inside_fenced_code_block_is_ignored() -> None:
    """Prompt-injection defense: an agent quoting source/docs that
    happen to mention the literal marker string must NOT spuriously
    emit a checkpoint."""
    text = (
        "Here is a code snippet I read:\n\n"
        "```python\n"
        "# emit [CHECKPOINT] foo here in production\n"
        "```\n\n"
        "I'm going to keep working."
    )
    assert detect_checkpoints(text) == []


def test_marker_inside_tilde_fence_is_ignored() -> None:
    text = (
        "~~~\n"
        "[CHECKPOINT] fake-out from tilde-fenced quote\n"
        "~~~"
    )
    assert detect_checkpoints(text) == []


def test_marker_outside_fence_still_detected_when_fenced_marker_also_present() -> None:
    text = (
        "Earlier I saw this:\n\n"
        "```\n[CHECKPOINT] quoted, ignore me\n```\n\n"
        "[CHECKPOINT] but here is the real one\n"
    )
    assert detect_checkpoints(text) == [
        {"message": "but here is the real one"}
    ]


def test_returns_empty_on_empty_or_non_string_input() -> None:
    assert detect_checkpoints("") == []
    assert detect_checkpoints(None) == []  # type: ignore[arg-type]


def test_marker_constant_exported_for_runner_prompts() -> None:
    assert CHECKPOINT_MARKER == "[CHECKPOINT]"


def test_marker_alone_on_line_does_not_swallow_next_paragraph() -> None:
    """Horizontal-only whitespace before the message means a bare
    marker on its own line yields ``message=""``, not the next line's
    text."""
    text = "[CHECKPOINT]\nnext paragraph that is not the message"
    assert detect_checkpoints(text) == [{"message": ""}]


def test_malformed_step_falls_back_to_plain_marker_without_progress() -> None:
    """Garbage in the step= clause means the marker isn't matched at
    all (the closing bracket is part of the attribute syntax), which
    fails closed — better than guessing a partial parse."""
    out = detect_checkpoints("[CHECKPOINT step=abc/xyz] noisy")
    assert out == []
