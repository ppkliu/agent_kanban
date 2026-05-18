"""Unit tests for the NEEDS_HUMAN marker detector (Phase D3).

Lives next to the parser tests for D2b decomposition because both modules
serve the same purpose: extract a structured signal from arbitrary LLM
output and refuse on ambiguous / fenced cases.
"""
from __future__ import annotations

from symphony_mvp.escalation import (
    HUMAN_REQUIRED_MARKER,
    detect_needs_human,
)


def test_returns_none_on_no_marker() -> None:
    assert detect_needs_human("hello world, no marker here") is None


def test_returns_reason_on_inline_marker() -> None:
    reason = detect_needs_human(
        "I made some progress but [HUMAN_REQUIRED] need clarification on auth\n"
    )
    assert reason == "need clarification on auth"


def test_returns_marker_constant_on_empty_reason() -> None:
    """Marker with no inline reason still escalates; returns a placeholder
    so the caller can downstream-treat empty-reason as a signal."""
    reason = detect_needs_human("Stuck. [HUMAN_REQUIRED]\nDoing nothing else.")
    assert reason == "(no reason supplied)"


def test_marker_is_case_insensitive() -> None:
    # Real LLM output is sometimes casing-inconsistent.
    assert (
        detect_needs_human("[Human_Required] please confirm")
        == "please confirm"
    )
    assert (
        detect_needs_human("[human_required] please confirm")
        == "please confirm"
    )


def test_marker_inside_fenced_code_block_is_ignored() -> None:
    """Prompt-injection defense: agents quoting source / docs that
    happen to mention the literal marker string should NOT escalate."""
    text = (
        "Here is a docs snippet I read:\n\n"
        "```\n"
        "[HUMAN_REQUIRED] fake-out from quoted text\n"
        "```\n\n"
        "I'm going to keep working."
    )
    assert detect_needs_human(text) is None


def test_marker_inside_tilde_fence_is_ignored() -> None:
    text = (
        "~~~\n"
        "[HUMAN_REQUIRED] fake-out from tilde-fenced quote\n"
        "~~~"
    )
    assert detect_needs_human(text) is None


def test_marker_outside_fence_still_detected_when_fenced_marker_also_present() -> None:
    """Mixed: agent quotes the marker in a fence (to-be-ignored) AND
    raises a real one outside. The real one wins."""
    text = (
        "Earlier I saw this in the docs:\n\n"
        "```\n[HUMAN_REQUIRED] quoted, ignore me\n```\n\n"
        "[HUMAN_REQUIRED] but I genuinely need an answer about scope\n"
    )
    assert (
        detect_needs_human(text)
        == "but I genuinely need an answer about scope"
    )


def test_returns_none_on_empty_or_non_string_input() -> None:
    assert detect_needs_human("") is None
    assert detect_needs_human(None) is None  # type: ignore[arg-type]


def test_marker_constant_exported_for_runner_prompts() -> None:
    """Runner prompt templates can reference the marker string from a
    single source of truth so docs / instructions stay in sync."""
    assert HUMAN_REQUIRED_MARKER == "[HUMAN_REQUIRED]"
