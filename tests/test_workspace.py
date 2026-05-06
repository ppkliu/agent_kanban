"""Tests for WorkspaceManager — verify SPEC's three hard invariants."""
from __future__ import annotations

from pathlib import Path

import pytest

from symphony_mvp.workspace import WorkspaceManager, sanitize_key


def test_sanitize_replaces_unsafe_chars() -> None:
    assert sanitize_key("MT-625") == "MT-625"
    assert sanitize_key("ABC#42") == "ABC_42"
    assert sanitize_key("path/with/slash") == "path_with_slash"
    assert sanitize_key("a..b") == "a..b"
    assert sanitize_key("hello world") == "hello_world"


def test_sanitize_empty_raises() -> None:
    with pytest.raises(ValueError):
        sanitize_key("")


def test_path_for_inside_root(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path)
    p = wm.path_for("MT-1")
    assert p.parent == tmp_path.resolve()


def test_path_for_traversal_attempt(tmp_path: Path) -> None:
    """Ensure trying to escape with sanitized name still works correctly.

    Slashes get replaced with underscore, so even traversal-shaped inputs
    end up inside root.
    """
    wm = WorkspaceManager(tmp_path)
    p = wm.path_for("../etc/passwd")  # becomes ".._etc_passwd"
    assert tmp_path.resolve() in p.parents


def test_ensure_idempotent(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path)
    ws1 = wm.ensure("id1", "MT-1")
    ws2 = wm.ensure("id1", "MT-1")
    assert ws1.path == ws2.path
    assert Path(ws1.path).exists()


def test_after_create_hook_runs(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path)
    ws = wm.ensure("id1", "MT-1", after_create_hook="echo hello > marker.txt")
    assert (Path(ws.path) / "marker.txt").exists()


def test_after_create_hook_failure_is_fatal(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path)
    with pytest.raises(RuntimeError):
        wm.ensure("id1", "MT-fail", after_create_hook="exit 1")


def test_remove_works(tmp_path: Path) -> None:
    wm = WorkspaceManager(tmp_path)
    ws = wm.ensure("id1", "MT-1")
    assert Path(ws.path).exists()
    wm.remove(ws)
    assert not Path(ws.path).exists()
