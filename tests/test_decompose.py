"""Unit tests for the backend decomposition parser (Phase D2b).

Mirrors the assertions in frontend/src/chatToTasks.test.ts so client and
server stay in sync about which LLM outputs they're willing to accept.
"""
from __future__ import annotations

from symphony_mvp.dashboard.decompose import (
    DECOMPOSITION_SYSTEM_PROMPT,
    parse_decomposition,
)


def test_parses_a_clean_json_array() -> None:
    raw = '[{"task":"a","depends_on":[]},{"task":"b","depends_on":[0]}]'
    specs, error = parse_decomposition(raw)
    assert error is None
    assert specs == [
        {"task": "a", "depends_on": []},
        {"task": "b", "depends_on": [0]},
    ]


def test_strips_json_fence() -> None:
    raw = '```json\n[{"task":"x"}]\n```'
    specs, error = parse_decomposition(raw)
    assert error is None
    assert specs == [{"task": "x", "depends_on": []}]


def test_strips_plain_fence() -> None:
    raw = '```\n[{"task":"x"}]\n```'
    specs, error = parse_decomposition(raw)
    assert error is None
    assert specs[0]["task"] == "x"


def test_tolerates_llm_preamble_and_trailing_prose() -> None:
    raw = (
        "Here is the plan:\n\n"
        '[{"task":"first","depends_on":[]}]\n\n'
        "Let me know if you need changes!"
    )
    specs, error = parse_decomposition(raw)
    assert error is None
    assert specs == [{"task": "first", "depends_on": []}]


def test_fails_closed_on_malformed_json() -> None:
    specs, error = parse_decomposition("[not json")
    assert specs == []
    assert error is not None
    assert "JSON" in error or "no JSON array" in error


def test_fails_closed_on_empty_input() -> None:
    specs, error = parse_decomposition("")
    assert specs == []
    assert error is not None and "empty" in error


def test_fails_closed_on_non_array_object() -> None:
    specs, error = parse_decomposition('{"task":"x"}')
    assert specs == []
    assert error is not None and "no JSON array" in error


def test_rejects_entries_missing_task_field() -> None:
    specs, error = parse_decomposition('[{"depends_on":[]}]')
    assert specs == []
    assert error is not None and "task" in error


def test_rejects_non_integer_depends_on_values() -> None:
    raw = '[{"task":"a","depends_on":[]},{"task":"b","depends_on":["0"]}]'
    specs, error = parse_decomposition(raw)
    assert specs == []
    assert error is not None and "depends_on" in error


def test_rejects_boolean_depends_on_values() -> None:
    """In Python, ``bool`` is a subclass of ``int`` — guard against
    ``[true, false]`` silently parsing as ``[1, 0]``."""
    raw = '[{"task":"a","depends_on":[]},{"task":"b","depends_on":[true]}]'
    specs, error = parse_decomposition(raw)
    assert specs == []
    assert error is not None and "depends_on" in error


def test_defaults_missing_depends_on_to_empty_array() -> None:
    specs, error = parse_decomposition('[{"task":"a"}]')
    assert error is None
    assert specs == [{"task": "a", "depends_on": []}]


def test_strips_whitespace_around_task_text() -> None:
    specs, error = parse_decomposition('[{"task":"  hello  "}]')
    assert error is None
    assert specs[0]["task"] == "hello"


def test_system_prompt_mentions_json_array() -> None:
    """Surface check that the prompt text steers the LLM toward the
    structure the parser actually accepts."""
    assert "JSON array" in DECOMPOSITION_SYSTEM_PROMPT
    assert "depends_on" in DECOMPOSITION_SYSTEM_PROMPT
