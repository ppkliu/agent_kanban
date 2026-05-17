"""FastAPI route + WebSocket tests via Starlette TestClient.

Covers spec §11 DoD #9 (api_key never leaks) and #10 (auth enforcement).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from symphony_mvp import (
    EchoRunner,
    InMemoryTracker,
    Issue,
    Orchestrator,
    WorkspaceManager,
    load_workflow,
)
from symphony_mvp.dashboard.bridge import DashboardBridge
from symphony_mvp.dashboard.server import create_app


WORKFLOW_TEMPLATE = """---
tracker:
  kind: memory
polling:
  interval_ms: 50
agent:
  max_concurrent_agents: 2
  max_turns: 3
  retry_max_attempts: 2
  handoff_state: in_review
runner:
  kind: echo
---
Issue {{ issue.identifier }}: {{ issue.title }}{% if hints %}
HINTS:
{% for h in hints %}- {{ h.content }}
{% endfor %}{% endif %}
"""


def _make_issue(identifier="MT-1") -> Issue:
    return Issue(
        id=f"id-{identifier}",
        identifier=identifier,
        title=f"Title {identifier}",
        description="body",
        priority=1,
        state="open",
        branch_name=None,
        url=f"https://example.com/{identifier}",
    )


@pytest.fixture()
def app_ctx(tmp_path: Path):
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(WORKFLOW_TEMPLATE, encoding="utf-8")
    workflow = load_workflow(wf)
    workflow.config.workspace_root = str(tmp_path / "ws")
    tracker = InMemoryTracker([_make_issue("MT-1"), _make_issue("MT-2")])
    workspaces = WorkspaceManager(workflow.config.workspace_root)
    bridge = DashboardBridge(tmp_path / "dash.db")
    orch = Orchestrator(
        workflow=workflow,
        tracker=tracker,
        workspaces=workspaces,
        runner=EchoRunner(),
        bridge=bridge,
    )
    orch.add_event_listener(bridge.on_event)
    app = create_app(orch, bridge, workflow_path=wf, api_key=None)  # open mode
    client = TestClient(app)
    yield {"orch": orch, "bridge": bridge, "tracker": tracker, "client": client, "wf_path": wf, "workflow": workflow}
    bridge.close()


def test_state_returns_5_columns(app_ctx) -> None:
    r = app_ctx["client"].get("/api/v1/state")
    assert r.status_code == 200
    body = r.json()
    assert set(body["columns"]) == {"pending", "claimed", "running", "retry_queued", "released"}
    # Both issues are pending pre-tick
    pending = body["columns"]["pending"]
    assert {e["issue"]["identifier"] for e in pending} == {"MT-1", "MT-2"}


def test_state_never_leaks_tracker_api_key(app_ctx) -> None:
    # Inject a sentinel api_key into the workflow config and confirm it is not
    # echoed back by /state or /workflow.
    secret = "SECRET_DO_NOT_LEAK_42"
    app_ctx["workflow"].config.tracker_api_key = secret
    r = app_ctx["client"].get("/api/v1/state")
    assert secret not in r.text
    r2 = app_ctx["client"].get("/api/v1/workflow")
    # /workflow returns the literal file content (which has $GITHUB_TOKEN, not
    # the resolved secret). The parsed config block in the response must not
    # contain the resolved secret.
    body = r2.json()
    assert secret not in json.dumps(body["config"])


def test_hint_endpoint_creates_pending(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/issues/id-MT-1/hint",
        json={"author": "alice", "content": "use useReducer"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["consumed"] is False
    assert body["id"] >= 1
    pending = app_ctx["bridge"].fetch_pending_hints("id-MT-1")
    assert len(pending) == 1


def test_priority_reorder_writes_overrides(app_ctx) -> None:
    r = app_ctx["client"].post(
        "/api/v1/priority",
        json={"ordered_issue_ids": ["id-MT-2", "id-MT-1"], "set_by": "alice"},
    )
    assert r.status_code == 200
    overrides = app_ctx["bridge"].get_priority_overrides()
    assert overrides == {"id-MT-2": 0, "id-MT-1": 1}


def test_pause_resume_abort_retry_404_when_no_attempt(app_ctx) -> None:
    # No attempts exist yet — pause/resume/abort should 404.
    assert app_ctx["client"].post("/api/v1/issues/id-NEVER/pause", json={}).status_code == 404
    assert app_ctx["client"].post("/api/v1/issues/id-NEVER/resume", json={}).status_code == 404
    assert app_ctx["client"].post("/api/v1/issues/id-NEVER/abort", json={}).status_code == 404


def test_config_patch_applies_whitelist(app_ctx) -> None:
    r = app_ctx["client"].patch(
        "/api/v1/config", json={"max_concurrent_agents": 7}
    )
    assert r.status_code == 200
    assert app_ctx["orch"].workflow.config.max_concurrent_agents == 7
    # Non-whitelisted fields are ignored (not present in schema).
    r2 = app_ctx["client"].patch(
        "/api/v1/config", json={"tracker_api_key": "leak_me"}
    )
    # Pydantic ignores unknown fields by default in model_validate; status 200 still.
    assert r2.status_code == 200
    # tracker_api_key NOT echoed:
    assert "tracker_api_key" not in r2.text or "leak_me" not in r2.text


def test_config_patch_swaps_backend_runner(app_ctx, monkeypatch) -> None:
    """Runtime runner swap: PATCH /api/v1/config with runner_kind rebuilds
    orch.runner via build_runner() and mutates env vars for base_url /
    api_key. The on-disk WORKFLOW.md is unchanged."""
    from symphony_mvp.agent_runner import EchoRunner

    # Sanity: fixture starts in echo (matches WORKFLOW.demo-echo style).
    assert isinstance(app_ctx["orch"].runner, EchoRunner)

    r = app_ctx["client"].patch(
        "/api/v1/config",
        json={
            "runner_kind": "echo",         # idempotent self-swap; safe
            "runner_model": "qwen3.6-27b-fp8",
            "runner_provider": "vllm",
            "runner_base_url": "http://localhost:8000",
            "runner_api_key": "test-key-1",
        },
    )
    assert r.status_code == 200, r.text
    changed = r.json()["changed"]
    assert changed["runner_model"] == "qwen3.6-27b-fp8"
    assert changed["runner_provider"] == "vllm"
    assert changed["runner_base_url"] == "http://localhost:8000"
    # api_key is intentionally masked in the response.
    assert changed["runner_api_key"] == "<set>"
    assert "test-key-1" not in r.text

    # Workflow config now reflects the swap.
    cfg = app_ctx["orch"].workflow.config
    assert cfg.runner_model == "qwen3.6-27b-fp8"
    assert cfg.runner_provider == "vllm"

    # Env vars set for the next dispatch / subprocess spawn.
    import os
    assert os.environ.get("OPENCODE_BASE_URL") == "http://localhost:8000"
    assert os.environ.get("OPENCODE_API_KEY") == "test-key-1"

    # Cleanup so subsequent tests don't inherit our env mutations.
    os.environ.pop("OPENCODE_BASE_URL", None)
    os.environ.pop("OPENCODE_API_KEY", None)
    os.environ.pop("ANTHROPIC_API_KEY", None)


def test_config_patch_unknown_runner_kind_returns_400(app_ctx) -> None:
    r = app_ctx["client"].patch(
        "/api/v1/config", json={"runner_kind": "totally-fake-runner"}
    )
    assert r.status_code == 400
    assert "Unknown runner kind" in r.text


def test_workflow_get_returns_content(app_ctx) -> None:
    r = app_ctx["client"].get("/api/v1/workflow")
    assert r.status_code == 200
    body = r.json()
    assert "tracker:" in body["content"]
    assert body["config"]["tracker_kind"] == "memory"


def test_workflow_put_with_invalid_yaml_returns_422_and_keeps_old_config(app_ctx) -> None:
    old_max = app_ctx["orch"].workflow.config.max_concurrent_agents
    r = app_ctx["client"].put(
        "/api/v1/workflow",
        json={"content": "this is not yaml front matter"},
    )
    assert r.status_code == 422
    body = r.json()
    assert body.get("kept_previous") is True
    # In-memory config preserved
    assert app_ctx["orch"].workflow.config.max_concurrent_agents == old_max
    # File on disk rolled back to last-known-good
    assert "tracker:" in app_ctx["wf_path"].read_text(encoding="utf-8")


def test_workflow_put_with_valid_content_reloads(app_ctx) -> None:
    new_content = WORKFLOW_TEMPLATE.replace(
        "max_concurrent_agents: 2", "max_concurrent_agents: 9"
    )
    r = app_ctx["client"].put("/api/v1/workflow", json={"content": new_content})
    assert r.status_code == 200
    assert app_ctx["orch"].workflow.config.max_concurrent_agents == 9


# ----- Auth (DoD #10) -----


def test_auth_required_when_api_key_set(tmp_path: Path) -> None:
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(WORKFLOW_TEMPLATE, encoding="utf-8")
    workflow = load_workflow(wf)
    workflow.config.workspace_root = str(tmp_path / "ws")
    bridge = DashboardBridge(tmp_path / "dash.db")
    orch = Orchestrator(
        workflow=workflow,
        tracker=InMemoryTracker([]),
        workspaces=WorkspaceManager(workflow.config.workspace_root),
        runner=EchoRunner(),
        bridge=bridge,
    )
    app = create_app(orch, bridge, workflow_path=wf, api_key="s3cret")
    client = TestClient(app)

    # No header → 401
    assert client.get("/api/v1/state").status_code == 401
    # Wrong token → 403
    r = client.get(
        "/api/v1/state", headers={"Authorization": "Bearer wrong"}
    )
    assert r.status_code == 403
    # Correct token → 200
    r2 = client.get(
        "/api/v1/state", headers={"Authorization": "Bearer s3cret"}
    )
    assert r2.status_code == 200
    bridge.close()


# ----- WebSocket -----


def _recv_until_type(ws, want_type: str) -> dict:
    """Read WS messages, skipping the initial / trailing state_snapshot
    pushes the server emits, until one of the requested `want_type` arrives.
    Bounded so a buggy server doesn't hang the test."""
    for _ in range(20):
        payload = json.loads(ws.receive_text())
        if payload.get("type") == want_type:
            return payload
    raise AssertionError(f"never saw a {want_type} message on the websocket")


def test_websocket_streams_agent_events(app_ctx) -> None:
    bridge = app_ctx["bridge"]
    client = app_ctx["client"]

    with client.websocket_connect("/api/v1/events") as ws:
        # Push an event from the bridge — subscriber path should deliver it.
        from symphony_mvp.agent_runner import AgentEvent, AgentEventKind
        bridge.on_event(
            "id-MT-1",
            AgentEvent(
                kind=AgentEventKind.MESSAGE_DELTA,
                timestamp=datetime.now(timezone.utc),
                data={"text": "hello"},
            ),
        )
        payload = _recv_until_type(ws, "agent_event")
        assert payload["issue_id"] == "id-MT-1"
        assert payload["event"]["kind"] == "message_delta"
        assert payload["event"]["data"]["text"] == "hello"


def test_websocket_filter_narrows_by_issue_id(app_ctx) -> None:
    bridge = app_ctx["bridge"]
    client = app_ctx["client"]
    from symphony_mvp.agent_runner import AgentEvent, AgentEventKind

    with client.websocket_connect("/api/v1/events?filter=issue:id-MT-1") as ws:
        # Event for excluded issue — must NOT come through.
        bridge.on_event(
            "id-MT-2",
            AgentEvent(
                kind=AgentEventKind.MESSAGE_DELTA,
                timestamp=datetime.now(timezone.utc),
                data={"text": "excluded"},
            ),
        )
        # Event for included issue — must come through.
        bridge.on_event(
            "id-MT-1",
            AgentEvent(
                kind=AgentEventKind.MESSAGE_DELTA,
                timestamp=datetime.now(timezone.utc),
                data={"text": "included"},
            ),
        )
        payload = _recv_until_type(ws, "agent_event")
        assert payload["issue_id"] == "id-MT-1"
        assert payload["event"]["data"]["text"] == "included"


def test_websocket_pushes_state_snapshot_on_connect(app_ctx) -> None:
    """The first push after WS connect (and after any replayed events) is
    a state_snapshot containing the same body the REST /state route emits.
    Frontend relies on this to avoid REST polling for kanban state."""
    client = app_ctx["client"]
    with client.websocket_connect("/api/v1/events") as ws:
        payload = _recv_until_type(ws, "state_snapshot")
        snap = payload["snapshot"]
        assert "columns" in snap
        assert "config" in snap
        assert "totals" in snap
        assert set(snap["columns"].keys()) == {
            "pending",
            "claimed",
            "running",
            "retry_queued",
            "released",
        }


def test_websocket_filter_narrows_by_project_id(app_ctx) -> None:
    """Phase E4 — `?filter=project:<id>` only delivers agent_events for issues
    whose `project:<id>` label matches. Tags issues at fixture time via
    Issue.labels (mirrors how the Tool API auto-attaches the label)."""
    from symphony_mvp.agent_runner import AgentEvent, AgentEventKind

    tracker = app_ctx["tracker"]
    bridge = app_ctx["bridge"]
    client = app_ctx["client"]

    # Tag the two pre-seeded issues into different projects.
    tracker.add(
        Issue(
            id="id-PROJA",
            identifier="PROJA",
            title="alpha task",
            description="",
            priority=1,
            state="open",
            branch_name=None,
            url="",
            labels=["tool-api", "project:proj_a"],
        )
    )
    tracker.add(
        Issue(
            id="id-PROJB",
            identifier="PROJB",
            title="beta task",
            description="",
            priority=1,
            state="open",
            branch_name=None,
            url="",
            labels=["tool-api", "project:proj_b"],
        )
    )

    with client.websocket_connect(
        "/api/v1/events?filter=project:proj_a"
    ) as ws:
        bridge.on_event(
            "id-PROJB",
            AgentEvent(
                kind=AgentEventKind.MESSAGE_DELTA,
                timestamp=datetime.now(timezone.utc),
                data={"text": "from-b"},
            ),
        )
        bridge.on_event(
            "id-PROJA",
            AgentEvent(
                kind=AgentEventKind.MESSAGE_DELTA,
                timestamp=datetime.now(timezone.utc),
                data={"text": "from-a"},
            ),
        )
        payload = _recv_until_type(ws, "agent_event")
        # The B event must be filtered out — _recv_until_type would return
        # the first agent_event it sees, which must be the A one.
        assert payload["issue_id"] == "id-PROJA"
        assert payload["event"]["data"]["text"] == "from-a"


def test_websocket_filter_project_default_includes_untagged_legacy(
    app_ctx,
) -> None:
    """Legacy issues with no `project:` label show up under the default
    project's trace — so multi-project rollout doesn't hide pre-E1 tasks."""
    from symphony_mvp.agent_runner import AgentEvent, AgentEventKind

    bridge = app_ctx["bridge"]
    client = app_ctx["client"]
    # The fixture pre-seeds MT-1 / MT-2 without any project label.

    with client.websocket_connect(
        "/api/v1/events?filter=project:default"
    ) as ws:
        bridge.on_event(
            "id-MT-1",
            AgentEvent(
                kind=AgentEventKind.MESSAGE_DELTA,
                timestamp=datetime.now(timezone.utc),
                data={"text": "legacy"},
            ),
        )
        payload = _recv_until_type(ws, "agent_event")
        assert payload["issue_id"] == "id-MT-1"
        assert payload["event"]["data"]["text"] == "legacy"
