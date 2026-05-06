"""Tests for WORKFLOW.md loading + prompt rendering."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from symphony_mvp.models import Issue
from symphony_mvp.workflow import load_workflow, render_prompt


def write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "WORKFLOW.md"
    p.write_text(content, encoding="utf-8")
    return p


def test_load_minimal_workflow(tmp_path: Path) -> None:
    p = write(
        tmp_path,
        """---
tracker:
  kind: memory
agent:
  max_concurrent_agents: 3
---
Issue: {{ issue.identifier }}
""",
    )
    wf = load_workflow(p)
    assert wf.config.tracker_kind == "memory"
    assert wf.config.max_concurrent_agents == 3
    assert "{{ issue.identifier }}" in wf.prompt_template


def test_env_var_expansion(tmp_path: Path) -> None:
    os.environ["TEST_TRACKER_KEY"] = "s3cret"
    try:
        p = write(
            tmp_path,
            """---
tracker:
  kind: github
  repo: foo/bar
  api_key: $TEST_TRACKER_KEY
---
Hello.
""",
        )
        wf = load_workflow(p)
        assert wf.config.tracker_api_key == "s3cret"
    finally:
        del os.environ["TEST_TRACKER_KEY"]


def test_missing_front_matter_raises(tmp_path: Path) -> None:
    p = write(tmp_path, "no front matter, just body")
    with pytest.raises(ValueError, match="missing YAML front matter"):
        load_workflow(p)


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    p = write(tmp_path, "---\ntracker: [oops\n---\nbody")
    with pytest.raises(ValueError, match="invalid YAML"):
        load_workflow(p)


def test_invalid_template_syntax_raises(tmp_path: Path) -> None:
    p = write(tmp_path, "---\ntracker: {kind: memory}\n---\n{% if  unclosed")
    with pytest.raises(ValueError, match="syntax error"):
        load_workflow(p)


def test_render_strict_undefined(tmp_path: Path) -> None:
    p = write(
        tmp_path,
        """---
tracker:
  kind: memory
---
Hi {{ unknown_var }}
""",
    )
    wf = load_workflow(p)
    issue = Issue(
        id="abc",
        identifier="MT-1",
        title="t",
        description="d",
        priority=1,
        state="open",
        branch_name=None,
        url="",
    )
    with pytest.raises(ValueError, match="undefined"):
        render_prompt(wf, issue.to_template_context(attempt=1))


def test_render_with_attempt(tmp_path: Path) -> None:
    p = write(
        tmp_path,
        """---
tracker:
  kind: memory
---
Issue {{ issue.identifier }} (attempt {{ attempt }}): {{ issue.title }}
Labels: {% for l in issue.labels %}{{ l }}{% if not loop.last %},{% endif %}{% endfor %}
""",
    )
    wf = load_workflow(p)
    issue = Issue(
        id="abc",
        identifier="MT-1",
        title="Fix login bug",
        description="d",
        priority=1,
        state="open",
        branch_name=None,
        url="",
        labels=["bug", "auth"],
    )
    out = render_prompt(wf, issue.to_template_context(attempt=2))
    assert "MT-1" in out
    assert "attempt 2" in out
    assert "Fix login bug" in out
    assert "bug,auth" in out
