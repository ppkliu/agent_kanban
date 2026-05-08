"""Derive a structured ``TaskResult`` from finalised attempt history + events.

This is the Phase B replacement for the placeholder ``files_changed=[]`` /
``blockers=[]`` shape that ``tool_api.py`` ships with in Phase A. See
``docs/design/coding-service-tool-api.md`` §7 for the contract upstream LLM
agents read against.

Only consumes:
- one finalised ``attempt_history`` row (dict, from ``DashboardBridge.list_attempt_history``)
- the per-attempt event list (dict, from ``DashboardBridge.fetch_events``)

…and emits a flat dataclass. No FastAPI imports here; it stays trivial to
test in isolation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..agent_runner import AgentEventKind


# ---- regexes shared with stage.py kept in sync deliberately --------------- #

_TEST_RE = re.compile(
    r"\b(pytest|jest|vitest|go test|cargo test|npm test|yarn test|deno test|"
    r"mvn test|gradle test|rspec|phpunit)\b",
    re.IGNORECASE,
)

_PASSED_RE = re.compile(r"(\d+)\s+passed", re.IGNORECASE)
_FAILED_RE = re.compile(r"(\d+)\s+failed", re.IGNORECASE)

# Heuristic blocker / follow-up extraction. Match a sentence that contains
# any of the keywords; capture the sentence verbatim. Imperfect but cheap;
# Phase C may swap for an LLM judge.
_BLOCKER_RE = re.compile(
    r"(?:^|(?<=[.!?\n]))\s*([^.!?\n]*\b(?:blocked by|requires|missing|cannot|"
    r"unable to|needs?)\b[^.!?\n]*[.!?]?)",
    re.IGNORECASE,
)
_FOLLOWUP_RE = re.compile(
    r"(?:^|(?<=[.!?\n]))\s*([^.!?\n]*\b(?:consider|suggest(?:ed)?|"
    r"recommend(?:ed)?|todo|follow.?up|next step)\b[^.!?\n]*[.!?]?)",
    re.IGNORECASE,
)

# Tool args we read for file paths.
_PATH_KEYS = ("path", "file_path", "filename", "target_file")


# ---- dataclasses (kept dataclass-shaped to match server.py conventions) --- #


@dataclass(frozen=True)
class FileChange:
    path: str
    change: str  # "added" | "modified" | "deleted"


@dataclass(frozen=True)
class TestRun:
    __test__ = False  # tell pytest this is not a test class
    passed: int
    failed: int
    framework: str | None = None


@dataclass
class DerivedResult:
    summary: str
    files_changed: list[FileChange] = field(default_factory=list)
    tests_run: TestRun | None = None
    blockers: list[str] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)


# ---- internal helpers ----------------------------------------------------- #


def _path_from_input(inp: Any) -> str | None:
    if not isinstance(inp, dict):
        return None
    for k in _PATH_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _last_assistant_text(events: list[dict[str, Any]]) -> str | None:
    """Concatenate trailing MESSAGE_DELTA chunks of the last turn into one
    summary block; cap to 5 sentences to keep Q3's token budget."""
    chunks: list[str] = []
    # Collect deltas from the end backwards until we hit a non-delta event
    for ev in reversed(events):
        if ev.get("kind") == AgentEventKind.MESSAGE_DELTA.value:
            text = (ev.get("data") or {}).get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
        elif chunks:
            # Stop scanning once we've gathered the trailing run of deltas
            break
    if not chunks:
        return None
    text = "".join(reversed(chunks)).strip()
    if not text:
        return None
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(sentences[:5]).strip() or None


def _extract_files_changed(events: list[dict[str, Any]]) -> list[FileChange]:
    """Walk events forward; later writes override earlier (added → modified).

    De-duplicated by path. Sorted alphabetically for stable output.
    """
    seen: dict[str, str] = {}
    for ev in events:
        if ev.get("kind") != AgentEventKind.TOOL_CALL.value:
            continue
        data = ev.get("data") or {}
        tool = (data.get("tool") or "").lower()
        path = _path_from_input(data.get("input"))
        if path is None:
            continue
        if tool in ("edit", "str_replace_editor"):
            seen[path] = "modified"
        elif tool in ("write", "create"):
            # If we already have it as modified, keep modified; else added
            seen[path] = seen.get(path, "added")
        elif tool == "delete":
            seen[path] = "deleted"
    return [FileChange(path=p, change=c) for p, c in sorted(seen.items())]


def _extract_tests_run(events: list[dict[str, Any]]) -> TestRun | None:
    """Find the most recent bash test command + its tool_result; parse counts."""
    pending: dict[str, str] = {}  # tool_use_id → framework
    last_result: dict[str, Any] | None = None
    last_framework: str | None = None
    for ev in events:
        kind = ev.get("kind", "")
        data = ev.get("data") or {}
        if kind == AgentEventKind.TOOL_CALL.value:
            tool = (data.get("tool") or "").lower()
            if tool != "bash":
                continue
            inp = data.get("input")
            cmd = inp.get("command", "") if isinstance(inp, dict) else ""
            m = _TEST_RE.search(str(cmd))
            if m:
                tu_id = data.get("tool_use_id") or data.get("id") or ""
                pending[str(tu_id)] = m.group(1).lower()
        elif kind == AgentEventKind.TOOL_RESULT.value:
            tu_id = str(data.get("tool_use_id") or data.get("id") or "")
            if tu_id and tu_id in pending:
                last_result = ev
                last_framework = pending[tu_id]

    if last_result is None:
        return None
    output = (last_result.get("data") or {}).get("output")
    if not isinstance(output, str):
        # Some runners stash output under different keys; fall back to str().
        output = str(output) if output is not None else ""
    p = _PASSED_RE.search(output)
    f = _FAILED_RE.search(output)
    return TestRun(
        passed=int(p.group(1)) if p else 0,
        failed=int(f.group(1)) if f else 0,
        framework=last_framework,
    )


def _extract_heuristics(text: str) -> tuple[list[str], list[str]]:
    """Best-effort regex extraction of blockers / follow-up suggestions from a
    summary string. Capped at 5 of each, dedup-preserving order."""
    if not text:
        return [], []
    blockers = [m.group(1).strip() for m in _BLOCKER_RE.finditer(text)]
    follow_ups = [m.group(1).strip() for m in _FOLLOWUP_RE.finditer(text)]
    return (
        list(dict.fromkeys([b for b in blockers if b]))[:5],
        list(dict.fromkeys([f for f in follow_ups if f]))[:5],
    )


# ---- public entry point --------------------------------------------------- #


def derive_result(
    history_row: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    fallback_summary: str,
) -> DerivedResult:
    """Combine the finalised attempt row + per-attempt event list into a
    ``DerivedResult`` ready for the Tool API to serialize.

    ``fallback_summary`` is used when no MESSAGE_DELTA was emitted (e.g. early
    failure); typically the Phase A template summary built by ``tool_api.py``.
    """
    summary = _last_assistant_text(events) or fallback_summary
    files_changed = _extract_files_changed(events)
    tests_run = _extract_tests_run(events)
    blockers, follow_ups = _extract_heuristics(summary)
    return DerivedResult(
        summary=summary,
        files_changed=files_changed,
        tests_run=tests_run,
        blockers=blockers,
        follow_ups=follow_ups,
    )
