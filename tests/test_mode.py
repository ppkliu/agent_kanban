"""Tests for symphony_mvp.dashboard.mode — plan/build/review whitelist."""
from __future__ import annotations

from symphony_mvp.dashboard.mode import (
    DEFAULT_MODE,
    MODE_ALLOWED_TOOLS,
    MODE_LABEL_PREFIX,
    allowed_tools_for,
    extract_mode_from_labels,
    is_valid_mode,
)


def test_valid_modes_are_plan_build_review() -> None:
    assert set(MODE_ALLOWED_TOOLS.keys()) == {"plan", "build", "review"}


def test_is_valid_mode_accepts_known_values() -> None:
    assert is_valid_mode("plan") is True
    assert is_valid_mode("build") is True
    assert is_valid_mode("review") is True


def test_is_valid_mode_rejects_unknown_values() -> None:
    assert is_valid_mode("debug") is False
    assert is_valid_mode("") is False
    assert is_valid_mode("BUILD") is False  # case-sensitive


def test_plan_mode_has_no_edit_or_write() -> None:
    tools = allowed_tools_for("plan")
    assert "edit" not in tools
    assert "write" not in tools
    assert "read" in tools


def test_build_mode_has_full_toolset() -> None:
    tools = allowed_tools_for("build")
    assert "bash" in tools
    assert "read" in tools
    assert "edit" in tools
    assert "write" in tools


def test_review_mode_is_read_only() -> None:
    tools = allowed_tools_for("review")
    assert "bash" not in tools
    assert "edit" not in tools
    assert "write" not in tools
    assert "read" in tools


def test_unknown_mode_falls_back_to_default() -> None:
    """Defensive: storage layers might mangle the label; we should not crash."""
    tools = allowed_tools_for("nonsense")
    assert tools == MODE_ALLOWED_TOOLS[DEFAULT_MODE]


def test_extract_mode_from_labels_finds_first_mode_label() -> None:
    labels = ["tool-api", "mode:plan", "priority:high"]
    assert extract_mode_from_labels(labels) == "plan"


def test_extract_mode_from_labels_returns_none_when_absent() -> None:
    assert extract_mode_from_labels(["tool-api"]) is None
    assert extract_mode_from_labels([]) is None
    assert extract_mode_from_labels(None) is None


def test_extract_mode_ignores_invalid_mode_labels() -> None:
    """A `mode:debug` label is a malformed payload; treat as missing."""
    assert extract_mode_from_labels(["mode:debug"]) is None


def test_mode_label_prefix_is_documented_constant() -> None:
    assert MODE_LABEL_PREFIX == "mode:"
