"""Backend decomposition prompt + JSON-array parser (Phase D2b).

Mirrors ``frontend/src/chatToTasks.ts`` so server-side decomposition (the
``decompose=true`` flag on ``submit_coding_task``) parses output from a
plan-mode agent using the same robust rules as the browser-side chat
panel does today. Fail-closed: any shape mismatch returns an empty list
plus a human-readable error string the caller surfaces.
"""
from __future__ import annotations

import json
import re
from typing import TypedDict


class SubtaskSpec(TypedDict, total=False):
    task: str
    depends_on: list[int]


# Single source of truth for the system message. The Tool API prepends
# this to the user's goal so the plan-mode runner is asked for JSON only.
DECOMPOSITION_SYSTEM_PROMPT = (
    "You are a planning assistant for Symphony, a coding-task queue.\n\n"
    "Decompose the user's high-level goal into 2 to 8 concrete subtasks "
    "that an autonomous coding agent can execute one at a time. Each "
    "subtask should be self-contained and describe what to do, not how.\n\n"
    "Return ONLY a JSON array, no prose, no markdown fence. Each entry must "
    "have:\n"
    "  - \"task\": string — the subtask description\n"
    "  - \"depends_on\": int[] — sibling indices (0-based) that must finish "
    "first. Forward references only (idx < this entry's index).\n\n"
    'Example output for "Build a TODO API":\n'
    "[\n"
    '  {"task": "Define data model with title and done fields", "depends_on": []},\n'
    '  {"task": "Implement CRUD endpoints using the model", "depends_on": [0]},\n'
    '  {"task": "Add integration tests for each endpoint", "depends_on": [1]}\n'
    "]"
)


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?([\s\S]*?)\n?```\s*$", re.IGNORECASE)


def parse_decomposition(raw: str) -> tuple[list[SubtaskSpec], str | None]:
    """Extract a SubtaskSpec list from an LLM-emitted JSON array.

    Returns ``(specs, None)`` on success and ``(empty, error_msg)`` on any
    failure. Tolerates wrapping ``` ```json ``` fences and leading /
    trailing prose. Validates each entry has a non-empty ``task`` string
    and an integer-array ``depends_on`` (defaulting to []).
    """
    if not isinstance(raw, str) or raw.strip() == "":
        return [], "empty response from LLM"

    text = raw.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()

    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return [], "no JSON array found in LLM response"
    slice_ = text[start : end + 1]

    try:
        parsed = json.loads(slice_)
    except json.JSONDecodeError as e:
        return [], f"JSON parse failed: {e.msg}"

    if not isinstance(parsed, list):
        return [], "LLM response is not a JSON array"
    if not parsed:
        return [], "LLM returned an empty subtask list"

    specs: list[SubtaskSpec] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            return [], f"entry {i} is not an object"
        task = item.get("task")
        if not isinstance(task, str) or task.strip() == "":
            return [], f'entry {i} is missing a non-empty "task" string'
        dep_raw = item.get("depends_on")
        depends_on: list[int] = []
        if dep_raw is not None:
            if not isinstance(dep_raw, list):
                return [], f"entry {i}.depends_on is not an array"
            for v in dep_raw:
                # Note: in Python, `bool` is a subclass of `int`. Reject
                # booleans explicitly so {"depends_on": [true]} doesn't
                # silently pass as [1].
                if isinstance(v, bool) or not isinstance(v, int):
                    return (
                        [],
                        f"entry {i}.depends_on contains a non-integer value",
                    )
            depends_on = list(dep_raw)
        specs.append({"task": task.strip(), "depends_on": depends_on})

    return specs, None
