"""Tests for symphony_mvp.dashboard.repo_inspect — read-only fs walk."""
from __future__ import annotations

from pathlib import Path

from symphony_mvp.dashboard.repo_inspect import (
    AgentsDoc,
    find_agents_doc,
    inspect_repo,
    language_breakdown,
    tree_summary,
)


def _make_repo(root: Path) -> None:
    (root / "AGENTS.md").write_text("# Coding conventions\nUse tabs.", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('hi')", encoding="utf-8")
    (root / "src" / "util.py").write_text("def x(): pass", encoding="utf-8")
    (root / "frontend").mkdir()
    (root / "frontend" / "index.tsx").write_text("export {}", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("module.exports = {}", encoding="utf-8")


def test_find_agents_doc_prefers_agents_md(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    (tmp_path / "CONTRIBUTING.md").write_text("(should not be picked)", encoding="utf-8")
    doc = find_agents_doc(tmp_path)
    assert doc is not None
    assert doc.filename == "AGENTS.md"
    assert "tabs" in doc.content


def test_find_agents_doc_falls_back_to_contributing(tmp_path: Path) -> None:
    (tmp_path / "CONTRIBUTING.md").write_text("contrib content", encoding="utf-8")
    doc = find_agents_doc(tmp_path)
    assert doc is not None
    assert doc.filename == "CONTRIBUTING.md"


def test_find_agents_doc_returns_none_when_missing(tmp_path: Path) -> None:
    assert find_agents_doc(tmp_path) is None


def test_find_agents_doc_truncates_long_content(tmp_path: Path) -> None:
    big = "x" * 20_000
    (tmp_path / "AGENTS.md").write_text(big, encoding="utf-8")
    doc = find_agents_doc(tmp_path)
    assert isinstance(doc, AgentsDoc)
    assert len(doc.content) == 8000


def test_tree_summary_skips_noise_dirs(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    out = tree_summary(tmp_path)
    assert "src/" in out
    assert "frontend/" in out
    assert ".git" not in out
    assert "node_modules" not in out


def test_tree_summary_respects_depth_cap(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "buried.py").write_text("...", encoding="utf-8")
    out = tree_summary(tmp_path, max_depth=2)
    assert "a/" in out
    assert "b/" in out
    assert "c/" not in out  # depth 3 — skipped


def test_language_breakdown_orders_by_frequency(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    langs, count = language_breakdown(tmp_path)
    # Two .py files, one .tsx, one .md (AGENTS.md). node_modules + .git skipped.
    assert "python" in langs
    assert langs[0] == "python"  # most frequent first
    assert "typescript" in langs
    assert "markdown" in langs
    # node_modules junk.js must NOT count
    assert "javascript" not in langs
    assert count >= 4


def test_inspect_repo_bundles_everything(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    result = inspect_repo(tmp_path, repo_id="memory")
    assert result.repo == "memory"
    assert result.agents_md is not None
    assert result.agents_md.filename == "AGENTS.md"
    assert "src/" in result.structure
    assert "python" in result.languages
    assert result.file_count >= 4


def test_inspect_repo_handles_missing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    result = inspect_repo(missing, repo_id="memory")
    assert result.repo == "memory"
    assert result.agents_md is None
    assert result.structure == ""
    assert result.languages == []
    assert result.file_count == 0
