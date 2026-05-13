"""Tracker adapters — SPEC.md Section 4.

A conforming tracker MUST implement three operations:
  1. fetch_active()       — candidate issues in active states
  2. fetch_terminal()     — issues in terminal states (for startup cleanup)
  3. reconcile_states()   — current states for a list of issue IDs

Pagination defaults: 50/page, 30s timeout. Implementations normalize their
native payloads into the Issue dataclass shape regardless of underlying tracker.

This module ships an InMemoryTracker for tests/demos and a stub GitHubIssuesTracker.
A real LinearTracker would follow the same protocol — left as an exercise to keep
the MVP runnable without external API dependencies.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from .models import Issue

logger = logging.getLogger(__name__)


@runtime_checkable
class Tracker(Protocol):
    """Protocol every tracker adapter must satisfy."""

    def fetch_active(self, active_states: list[str]) -> list[Issue]:
        """Return all candidate issues currently in any of the active_states."""
        ...

    def fetch_terminal(self, terminal_states: list[str]) -> list[Issue]:
        """Return issues currently in any terminal state (used at startup cleanup)."""
        ...

    def reconcile_states(self, issue_ids: list[str]) -> dict[str, str]:
        """Return current state for each given issue id. Missing ids are omitted."""
        ...


class InMemoryTracker:
    """Test/demo tracker. Lets tests drive issue lifecycle deterministically.

    Not thread-safe by design — Symphony's orchestrator is single-threaded per
    process, and tests run sequentially.
    """

    def __init__(self, issues: list[Issue] | None = None) -> None:
        self._issues: dict[str, Issue] = {i.id: i for i in (issues or [])}

    # ----- Tracker protocol -----
    def fetch_active(self, active_states: list[str]) -> list[Issue]:
        active_set = {s.lower() for s in active_states}
        return [i for i in self._issues.values() if i.state.lower() in active_set]

    def fetch_terminal(self, terminal_states: list[str]) -> list[Issue]:
        terminal_set = {s.lower() for s in terminal_states}
        return [i for i in self._issues.values() if i.state.lower() in terminal_set]

    def reconcile_states(self, issue_ids: list[str]) -> dict[str, str]:
        return {iid: self._issues[iid].state for iid in issue_ids if iid in self._issues}

    # ----- Test helpers -----
    def add(self, issue: Issue) -> None:
        self._issues[issue.id] = issue

    def transition(self, issue_id: str, new_state: str) -> None:
        """Simulate an external state change (e.g. agent moved ticket to handoff)."""
        if issue_id in self._issues:
            self._issues[issue_id] = self._issues[issue_id].with_state(new_state)
            logger.info("InMemoryTracker: %s -> %s", issue_id, new_state)

    def all(self) -> list[Issue]:
        return list(self._issues.values())


class GitHubIssuesTracker:
    """Read-only GitHub Issues tracker.

    Symphony spec is strict: tracker is READ-ONLY from the orchestrator side.
    Issue mutations (state transitions, comments, PR links) are done by the
    coding agent itself via tools (e.g. `gh` CLI in the workspace).

    Authentication: pass a personal access token (classic or fine-grained).
    Required scopes: `repo` (read).

    Note: We use the `state` field (open/closed) plus labels for finer states.
      Convention used here:
        - active_states: ["open"] or ["ready", "open"]    (uses labels for "ready")
        - terminal_states: ["closed"]
        - handoff_state: "in_review" (label-based)
    """

    def __init__(self, repo: str, api_key: str, base_url: str = "https://api.github.com") -> None:
        if "/" not in repo:
            raise ValueError(f"GitHub repo must be 'owner/name', got {repo!r}")
        self.repo = repo
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        # Lazy-import requests so the package can be imported without it
        import requests  # noqa: F401  (just verify it's installed)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _state_for(self, gh_issue: dict) -> str:
        """Derive Symphony's state from GitHub's open/closed + labels."""
        if gh_issue.get("state") == "closed":
            # state_reason: "completed" | "not_planned" | None (older API)
            reason = gh_issue.get("state_reason") or "completed"
            return "closed" if reason == "completed" else "cancelled"
        labels = [l["name"].lower() for l in gh_issue.get("labels", [])]
        # Convention: a label like "in-review" or "ready" overrides "open"
        for label in ("in-review", "in_review", "blocked", "ready"):
            if label in labels:
                return label.replace("-", "_")
        return "open"

    def _to_issue(self, gh: dict) -> Issue:
        from dateutil.parser import isoparse  # type: ignore[import-untyped]

        return Issue(
            id=f"{self.repo}#{gh['number']}",
            identifier=f"#{gh['number']}",
            title=gh.get("title", ""),
            description=gh.get("body") or "",
            priority=int(gh.get("priority", 3)) if "priority" in gh else 3,
            state=self._state_for(gh),
            branch_name=None,  # Caller can derive from identifier
            url=gh.get("html_url", ""),
            labels=[l["name"] for l in gh.get("labels", [])],
            blocked_by=[],
            created_at=isoparse(gh["created_at"]),
            updated_at=isoparse(gh["updated_at"]),
        )

    def _list_issues(self, state: str = "all", per_page: int = 50) -> list[dict]:
        import requests

        owner, name = self.repo.split("/", 1)
        url = f"{self.base_url}/repos/{owner}/{name}/issues"
        params = {"state": state, "per_page": per_page, "filter": "all"}
        results: list[dict] = []
        while url:
            resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
            resp.raise_for_status()
            page = resp.json()
            # Filter out PRs (GitHub returns them as issues)
            results.extend(i for i in page if "pull_request" not in i)
            url = resp.links.get("next", {}).get("url")
            params = {}  # subsequent pages embed query in `next` url
        return results

    def fetch_active(self, active_states: list[str]) -> list[Issue]:
        gh_issues = self._list_issues(state="open")
        all_issues = [self._to_issue(g) for g in gh_issues]
        active_set = {s.lower() for s in active_states}
        return [i for i in all_issues if i.state.lower() in active_set]

    def fetch_terminal(self, terminal_states: list[str]) -> list[Issue]:
        gh_issues = self._list_issues(state="closed")
        all_issues = [self._to_issue(g) for g in gh_issues]
        terminal_set = {s.lower() for s in terminal_states}
        return [i for i in all_issues if i.state.lower() in terminal_set]

    def reconcile_states(self, issue_ids: list[str]) -> dict[str, str]:
        import requests

        owner, name = self.repo.split("/", 1)
        out: dict[str, str] = {}
        for iid in issue_ids:
            # iid format: "owner/name#NUM"
            try:
                num = iid.split("#", 1)[1]
            except IndexError:
                continue
            url = f"{self.base_url}/repos/{owner}/{name}/issues/{num}"
            resp = requests.get(url, headers=self._headers(), timeout=30)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            out[iid] = self._state_for(resp.json())
        return out


class LinearTracker:
    """Read-only Linear tracker via the GraphQL API.

    Mirrors the same READ-ONLY contract as ``GitHubIssuesTracker``:
    Symphony only fetches and reconciles; the coding agent itself writes
    state transitions through whatever tools the user wires in.

    Authentication: pass a Linear personal API key (https://linear.app/settings/api).
    The `repo` parameter is the Linear *team key* (e.g. ``"ENG"`` for
    ``ENG-123``-style identifiers). Linear has no per-repo concept so the
    team key plays that role.

    State mapping (Linear → Symphony):
      - workflow state ``type == "started"`` or ``"unstarted"`` → ``open``
      - ``type == "completed"`` → ``closed``
      - ``type == "canceled"`` → ``cancelled``
      - ``type == "triage"`` → ``triage``
      - explicit state name ``"in review"`` / ``"in_review"`` → ``in_review``

    The orchestrator-side convention in WORKFLOW.md still applies:
      - active_states: ``["open"]`` or your custom names
      - terminal_states: ``["closed", "cancelled"]``
      - handoff_state: ``"in_review"``
    """

    DEFAULT_BASE_URL = "https://api.linear.app/graphql"

    # Fields we ask Linear for on every issue fetch. Kept minimal to honour
    # the spec's "tracker is a thin read-side" contract.
    _ISSUE_FRAGMENT = """
        id
        identifier
        title
        description
        priority
        url
        createdAt
        updatedAt
        branchName
        state { name type }
        labels { nodes { name } }
    """

    def __init__(
        self, repo: str, api_key: str, base_url: str = DEFAULT_BASE_URL
    ) -> None:
        if not repo:
            raise ValueError("LinearTracker requires a team key (e.g. 'ENG')")
        self.team_key = repo
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        # Lazy-import requests so the package can be imported without it
        import requests  # noqa: F401

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }

    def _post_graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        import requests

        resp = requests.post(
            self.base_url,
            headers=self._headers(),
            json={"query": query, "variables": variables or {}},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            # Linear returns errors[] for partial failures; surface them
            raise RuntimeError(f"Linear GraphQL errors: {body['errors']}")
        return body.get("data", {})

    def _state_for(self, linear_issue: dict[str, Any]) -> str:
        """Derive Symphony's lower-case state string from a Linear issue."""
        state = linear_issue.get("state") or {}
        name = (state.get("name") or "").lower().strip()
        type_ = (state.get("type") or "").lower().strip()
        # Explicit name match for handoff conventions
        normalised = name.replace(" ", "_").replace("-", "_")
        if normalised in {"in_review", "review", "blocked", "ready", "triage"}:
            return normalised
        if type_ == "completed":
            return "closed"
        if type_ == "canceled":
            return "cancelled"
        if type_ in ("started", "unstarted", "backlog"):
            return "open"
        # Fall back to the raw name so operators can write WORKFLOW.md states
        # like "in_progress" if their Linear workspace uses that.
        return normalised or "open"

    def _to_issue(self, ln: dict[str, Any]) -> Issue:
        from dateutil.parser import isoparse  # type: ignore[import-untyped]

        labels_node = (ln.get("labels") or {}).get("nodes") or []
        return Issue(
            id=ln["id"],  # Linear's UUID — globally unique
            identifier=ln.get("identifier") or ln["id"],
            title=ln.get("title") or "",
            description=ln.get("description") or "",
            priority=int(ln.get("priority") or 3),
            state=self._state_for(ln),
            branch_name=ln.get("branchName") or None,
            url=ln.get("url") or "",
            labels=[l.get("name", "") for l in labels_node if l.get("name")],
            blocked_by=[],  # Linear has issue relations but we keep MVP narrow
            created_at=isoparse(ln["createdAt"]),
            updated_at=isoparse(ln["updatedAt"]),
        )

    def _fetch_team_issues(self, *, completed_filter: bool | None) -> list[dict]:
        """Paginate through ``team(key:).issues(first: 50)`` until done.

        ``completed_filter`` is a tri-state:
          - ``None`` — no filter (used by reconcile)
          - ``True`` — only completed/canceled (terminal)
          - ``False`` — exclude completed/canceled (active)
        """
        query = """
        query TeamIssues($team: String!, $after: String) {
          team(key: $team) {
            issues(first: 50, after: $after) {
              nodes {""" + self._ISSUE_FRAGMENT + """}
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        results: list[dict] = []
        cursor: str | None = None
        while True:
            data = self._post_graphql(
                query, {"team": self.team_key, "after": cursor}
            )
            team = data.get("team") or {}
            issues = team.get("issues") or {}
            for node in issues.get("nodes") or []:
                if completed_filter is None:
                    results.append(node)
                else:
                    type_ = ((node.get("state") or {}).get("type") or "").lower()
                    is_terminal = type_ in ("completed", "canceled")
                    if completed_filter == is_terminal:
                        results.append(node)
            page = issues.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
            if not cursor:  # defensive — broken pagination response
                break
        return results

    def fetch_active(self, active_states: list[str]) -> list[Issue]:
        nodes = self._fetch_team_issues(completed_filter=False)
        all_issues = [self._to_issue(n) for n in nodes]
        active_set = {s.lower() for s in active_states}
        return [i for i in all_issues if i.state.lower() in active_set]

    def fetch_terminal(self, terminal_states: list[str]) -> list[Issue]:
        nodes = self._fetch_team_issues(completed_filter=True)
        all_issues = [self._to_issue(n) for n in nodes]
        terminal_set = {s.lower() for s in terminal_states}
        return [i for i in all_issues if i.state.lower() in terminal_set]

    def reconcile_states(self, issue_ids: list[str]) -> dict[str, str]:
        """Bulk lookup specific issues by id — used for stall detection /
        external-terminal reconciliation. Linear supports ``issues(filter:)``
        but a small per-id loop is plenty for MVP scale.
        """
        query = """
        query IssueState($id: String!) {
          issue(id: $id) {""" + self._ISSUE_FRAGMENT + """}
        }
        """
        out: dict[str, str] = {}
        for iid in issue_ids:
            data = self._post_graphql(query, {"id": iid})
            issue = data.get("issue")
            if issue is None:
                continue
            out[iid] = self._state_for(issue)
        return out


def build_tracker(kind: str, repo: str | None, api_key: str | None) -> Tracker:
    """Factory used by the orchestrator startup."""
    if kind == "memory":
        return InMemoryTracker()
    if kind == "github":
        if not repo:
            raise ValueError("tracker.repo is required for github tracker")
        if not api_key:
            raise ValueError("tracker.api_key is required for github tracker")
        return GitHubIssuesTracker(repo=repo, api_key=api_key)
    if kind == "linear":
        if not repo:
            raise ValueError(
                "tracker.repo (team key, e.g. 'ENG') is required for linear tracker"
            )
        if not api_key:
            raise ValueError("tracker.api_key is required for linear tracker")
        return LinearTracker(repo=repo, api_key=api_key)
    raise ValueError(f"Unsupported tracker kind: {kind!r}")
