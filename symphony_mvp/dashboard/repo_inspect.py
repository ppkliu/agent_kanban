"""Read-only filesystem inspection for the ``inspect_repo`` Tool API endpoint.

Returns the repo's `AGENTS.md` (or fallback `CONTRIBUTING.md` / `README.md`),
a short tree summary, and a coarse language breakdown — enough for an
upstream LLM agent to write a well-grounded ``submit_coding_task``
description.

No agent is spawned. No subprocess. No network. Just `os.walk` and small
string parsing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# File-extension → language map. Coarse on purpose; the goal is to give the
# upstream LLM a one-line "this is a python+ts project" hint, not a precise
# linguist breakdown.
_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".sql": "sql",
    ".sh": "bash",
    ".md": "markdown",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".vue": "vue",
}

# Directories to skip when walking.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "target", ".next",
    ".idea", ".vscode", "coverage", ".cache",
})

# Filenames checked in order; first hit wins.
_AGENTS_CANDIDATES: tuple[str, ...] = ("AGENTS.md", "CONTRIBUTING.md", "README.md")


@dataclass(frozen=True)
class AgentsDoc:
    filename: str
    content: str  # truncated to ~8000 chars to keep token budget sane


@dataclass
class RepoInspection:
    repo: str
    agents_md: AgentsDoc | None = None
    structure: str = ""  # tree summary
    languages: list[str] = field(default_factory=list)
    file_count: int = 0


def _is_skipped(rel_parts: tuple[str, ...]) -> bool:
    return any(part in _SKIP_DIRS for part in rel_parts)


def find_agents_doc(workspace_root: Path) -> AgentsDoc | None:
    """Return the first AGENTS-style doc found at the workspace root.

    Truncates content to 8000 chars; that's about 2k tokens — enough for the
    upstream LLM to read but not enough to blow context.
    """
    for cand in _AGENTS_CANDIDATES:
        p = workspace_root / cand
        if not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        return AgentsDoc(filename=cand, content=content[:8000])
    return None


def tree_summary(
    workspace_root: Path, *, max_depth: int = 2, max_entries: int = 200
) -> str:
    """Short tree-style listing of the workspace, skipping noisy dirs.

    Capped depth + entry count to keep the response token-light.
    """
    root = workspace_root.resolve()
    if not root.is_dir():
        return ""
    lines: list[str] = [f"{root.name}/"]
    count = 0
    for p in sorted(root.rglob("*")):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        depth = len(rel.parts)
        if depth > max_depth:
            continue
        if _is_skipped(rel.parts):
            continue
        indent = "  " * (depth - 1)
        suffix = "/" if p.is_dir() else ""
        lines.append(f"{indent}{rel.parts[-1]}{suffix}")
        count += 1
        if count >= max_entries:
            lines.append("  ...(truncated)")
            break
    return "\n".join(lines)


def language_breakdown(
    workspace_root: Path, *, max_files: int = 5000
) -> tuple[list[str], int]:
    """Return (languages-ordered-by-frequency, total-file-count)."""
    counts: dict[str, int] = {}
    total = 0
    for p in workspace_root.rglob("*"):
        try:
            rel = p.relative_to(workspace_root)
        except ValueError:
            continue
        if _is_skipped(rel.parts):
            continue
        if not p.is_file():
            continue
        total += 1
        ext = p.suffix.lower()
        lang = _EXT_LANG.get(ext)
        if lang is not None:
            counts[lang] = counts.get(lang, 0) + 1
        if total > max_files:
            break
    ordered = [lang for lang, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return ordered, total


def inspect_repo(workspace_root: Path, *, repo_id: str) -> RepoInspection:
    """Bundle all the read-only inspections into one struct."""
    if not workspace_root.is_dir():
        return RepoInspection(repo=repo_id, structure="", file_count=0)
    languages, file_count = language_breakdown(workspace_root)
    return RepoInspection(
        repo=repo_id,
        agents_md=find_agents_doc(workspace_root),
        structure=tree_summary(workspace_root),
        languages=languages,
        file_count=file_count,
    )
