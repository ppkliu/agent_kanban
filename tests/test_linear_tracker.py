"""Tests for symphony_mvp.tracker.LinearTracker.

Linear's GraphQL endpoint is replaced with a stub that captures the request
and returns canned payloads. No network. Covers:

- state mapping (Linear workflow state.type + name → Symphony state string)
- _to_issue conversion shape
- fetch_active / fetch_terminal filtering
- pagination through pageInfo.endCursor
- reconcile_states 404 (issue == null) handling
- build_tracker factory validation
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from symphony_mvp.tracker import LinearTracker, build_tracker


# --------------------------------------------------------------------------- #
# build_tracker factory                                                       #
# --------------------------------------------------------------------------- #

def test_build_tracker_returns_linear_for_kind_linear() -> None:
    t = build_tracker("linear", "ENG", "fake-key")
    assert isinstance(t, LinearTracker)
    assert t.team_key == "ENG"


def test_build_tracker_linear_requires_repo() -> None:
    with pytest.raises(ValueError, match="team key"):
        build_tracker("linear", None, "fake-key")


def test_build_tracker_linear_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        build_tracker("linear", "ENG", None)


# --------------------------------------------------------------------------- #
# State mapping (the most likely place to ship a bug)                         #
# --------------------------------------------------------------------------- #

def _make_tracker() -> LinearTracker:
    return LinearTracker(repo="ENG", api_key="lin_api_key_fake")


@pytest.mark.parametrize(
    "linear_state,expected_symphony_state",
    [
        # type-driven mapping (the default path)
        ({"name": "Todo", "type": "unstarted"}, "open"),
        ({"name": "In Progress", "type": "started"}, "open"),
        ({"name": "Backlog", "type": "backlog"}, "open"),
        ({"name": "Done", "type": "completed"}, "closed"),
        ({"name": "Canceled", "type": "canceled"}, "cancelled"),
        # name-driven overrides (Symphony's handoff conventions take precedence)
        ({"name": "In Review", "type": "started"}, "in_review"),
        ({"name": "Blocked", "type": "started"}, "blocked"),
        ({"name": "Ready", "type": "unstarted"}, "ready"),
        ({"name": "Triage", "type": "triage"}, "triage"),
        # truly unrecognized type — fall through to normalised name
        ({"name": "Custom Stage", "type": "weird-unknown"}, "custom_stage"),
        # empty state should default to "open" (defensive)
        ({}, "open"),
    ],
)
def test_state_for_maps_linear_states(linear_state, expected_symphony_state) -> None:
    t = _make_tracker()
    assert t._state_for({"state": linear_state}) == expected_symphony_state  # noqa: SLF001


# --------------------------------------------------------------------------- #
# _to_issue conversion                                                        #
# --------------------------------------------------------------------------- #

def test_to_issue_extracts_canonical_fields() -> None:
    t = _make_tracker()
    raw = {
        "id": "lin-uuid-1",
        "identifier": "ENG-42",
        "title": "Fix login rate limiter",
        "description": "details here",
        "priority": 2,
        "url": "https://linear.app/foo/issue/ENG-42",
        "createdAt": "2026-05-13T10:00:00.000Z",
        "updatedAt": "2026-05-13T11:00:00.000Z",
        "branchName": "eng-42-fix-login",
        "state": {"name": "In Progress", "type": "started"},
        "labels": {"nodes": [{"name": "auth"}, {"name": "bug"}]},
    }
    issue = t._to_issue(raw)  # noqa: SLF001
    assert issue.id == "lin-uuid-1"
    assert issue.identifier == "ENG-42"
    assert issue.title == "Fix login rate limiter"
    assert issue.description == "details here"
    assert issue.priority == 2
    assert issue.state == "open"
    assert issue.branch_name == "eng-42-fix-login"
    assert issue.url == "https://linear.app/foo/issue/ENG-42"
    assert set(issue.labels) == {"auth", "bug"}


def test_to_issue_handles_missing_optional_fields() -> None:
    t = _make_tracker()
    raw = {
        "id": "lin-2",
        "identifier": "ENG-43",
        "createdAt": "2026-05-13T10:00:00.000Z",
        "updatedAt": "2026-05-13T11:00:00.000Z",
        "state": {"name": "Done", "type": "completed"},
        # title / description / priority / labels / branchName omitted
    }
    issue = t._to_issue(raw)  # noqa: SLF001
    assert issue.title == ""
    assert issue.description == ""
    assert issue.priority == 3  # default
    assert issue.branch_name is None
    assert issue.labels == []
    assert issue.state == "closed"


# --------------------------------------------------------------------------- #
# fetch_active / fetch_terminal / reconcile — mocked HTTP                     #
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


def _make_node(
    identifier: str, *, state_name: str, state_type: str, issue_id: str | None = None
) -> dict[str, Any]:
    return {
        "id": issue_id or f"uuid-{identifier}",
        "identifier": identifier,
        "title": f"Title {identifier}",
        "description": "",
        "priority": 2,
        "url": f"https://linear.app/team/issue/{identifier}",
        "createdAt": "2026-05-13T10:00:00.000Z",
        "updatedAt": "2026-05-13T11:00:00.000Z",
        "branchName": None,
        "state": {"name": state_name, "type": state_type},
        "labels": {"nodes": []},
    }


def test_fetch_active_filters_out_terminal_issues(monkeypatch) -> None:
    t = _make_tracker()
    nodes = [
        _make_node("ENG-1", state_name="In Progress", state_type="started"),
        _make_node("ENG-2", state_name="Done",        state_type="completed"),
        _make_node("ENG-3", state_name="Todo",        state_type="unstarted"),
    ]
    fake_post = MagicMock(return_value=_FakeResponse({
        "data": {"team": {"issues": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}
    }))
    monkeypatch.setattr("requests.post", fake_post)

    active = t.fetch_active(["open"])
    ids = {i.identifier for i in active}
    assert ids == {"ENG-1", "ENG-3"}
    # The GraphQL query was POSTed exactly once for this single-page result.
    assert fake_post.call_count == 1
    sent_body = fake_post.call_args.kwargs["json"]
    assert sent_body["variables"] == {"team": "ENG", "after": None}


def test_fetch_terminal_only_returns_completed_or_canceled(monkeypatch) -> None:
    t = _make_tracker()
    nodes = [
        _make_node("ENG-1", state_name="Done",     state_type="completed"),
        _make_node("ENG-2", state_name="Canceled", state_type="canceled"),
        _make_node("ENG-3", state_name="Todo",     state_type="unstarted"),
    ]
    monkeypatch.setattr("requests.post", MagicMock(return_value=_FakeResponse({
        "data": {"team": {"issues": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}
    })))

    terminal = t.fetch_terminal(["closed", "cancelled"])
    ids = {i.identifier for i in terminal}
    assert ids == {"ENG-1", "ENG-2"}


def test_fetch_active_paginates_through_endcursor(monkeypatch) -> None:
    """Two GraphQL responses with hasNextPage=True then False; aggregate them."""
    t = _make_tracker()
    page1_nodes = [_make_node("ENG-1", state_name="Todo", state_type="unstarted")]
    page2_nodes = [_make_node("ENG-2", state_name="Todo", state_type="unstarted")]
    responses = [
        _FakeResponse({"data": {"team": {"issues": {
            "nodes": page1_nodes,
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
        }}}}),
        _FakeResponse({"data": {"team": {"issues": {
            "nodes": page2_nodes,
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}}),
    ]
    fake_post = MagicMock(side_effect=responses)
    monkeypatch.setattr("requests.post", fake_post)

    active = t.fetch_active(["open"])
    assert {i.identifier for i in active} == {"ENG-1", "ENG-2"}
    assert fake_post.call_count == 2
    # Second call should pass after=cursor-1
    second_call_vars = fake_post.call_args_list[1].kwargs["json"]["variables"]
    assert second_call_vars["after"] == "cursor-1"


def test_reconcile_states_handles_unknown_issue_id(monkeypatch) -> None:
    """Linear returns ``issue: null`` for unknown ids; skip rather than crash."""
    t = _make_tracker()
    responses = [
        _FakeResponse({"data": {"issue": _make_node(
            "ENG-1", state_name="Done", state_type="completed", issue_id="uuid-1",
        )}}),
        _FakeResponse({"data": {"issue": None}}),  # gone / wrong id
    ]
    monkeypatch.setattr("requests.post", MagicMock(side_effect=responses))

    out = t.reconcile_states(["uuid-1", "uuid-gone"])
    assert out == {"uuid-1": "closed"}


def test_graphql_errors_are_surfaced(monkeypatch) -> None:
    """Linear's `errors[]` field must abort, not silently return empty."""
    t = _make_tracker()
    monkeypatch.setattr("requests.post", MagicMock(return_value=_FakeResponse({
        "errors": [{"message": "Authentication required"}],
        "data": None,
    })))
    with pytest.raises(RuntimeError, match="Linear GraphQL errors"):
        t.fetch_active(["open"])


def test_headers_include_api_key_and_json_content_type() -> None:
    t = LinearTracker(repo="ENG", api_key="lin_api_key_xyz")
    h = t._headers()  # noqa: SLF001
    assert h["Authorization"] == "lin_api_key_xyz"
    assert h["Content-Type"] == "application/json"
