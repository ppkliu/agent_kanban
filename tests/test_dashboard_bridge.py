"""DashboardBridge unit tests — store correctness in isolation from orchestrator."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from symphony_mvp.agent_runner import AgentEvent, AgentEventKind
from symphony_mvp.dashboard.bridge import DashboardBridge


@pytest.fixture()
def bridge(tmp_path) -> DashboardBridge:
    b = DashboardBridge(tmp_path / "dashboard.db", ring_size=5)
    yield b
    b.close()


def _ev(kind: AgentEventKind = AgentEventKind.MESSAGE_DELTA, **data) -> AgentEvent:
    return AgentEvent(
        kind=kind,
        timestamp=datetime.now(timezone.utc),
        data=data,
    )


def test_hint_round_trip(bridge: DashboardBridge) -> None:
    hid = bridge.add_hint("id-MT-1", "alice", "use useReducer")
    assert isinstance(hid, int) and hid > 0

    pending = bridge.fetch_pending_hints("id-MT-1")
    assert len(pending) == 1
    assert pending[0]["author"] == "alice"
    assert pending[0]["content"] == "use useReducer"

    bridge.mark_hints_consumed([hid], attempt_number=2)
    assert bridge.fetch_pending_hints("id-MT-1") == []

    history = bridge.list_hints("id-MT-1")
    assert len(history) == 1
    assert history[0]["consumed"] is True
    assert history[0]["consumed_attempt"] == 2


def test_hint_per_issue_isolation(bridge: DashboardBridge) -> None:
    bridge.add_hint("id-A", "alice", "for A")
    bridge.add_hint("id-B", "bob", "for B")
    assert len(bridge.fetch_pending_hints("id-A")) == 1
    assert len(bridge.fetch_pending_hints("id-B")) == 1
    assert bridge.fetch_pending_hints("id-A")[0]["content"] == "for A"


def test_event_persisted_and_ringbuffered(bridge: DashboardBridge) -> None:
    bridge.record_attempt_number("id-MT-1", 1)
    for i in range(7):
        bridge.on_event("id-MT-1", _ev(turn=i))
    # ring is size 5
    recent = bridge.get_recent_events("id-MT-1", limit=10)
    assert len(recent) == 5
    # but persistence is unbounded
    rows = bridge.fetch_events("id-MT-1")
    assert len(rows) == 7


def test_event_subscribers_fanout(bridge: DashboardBridge) -> None:
    received = []

    def sub(iid, ev):
        received.append((iid, ev.kind))

    bridge.add_event_subscriber(sub)
    bridge.on_event("id-X", _ev())
    assert received == [("id-X", AgentEventKind.MESSAGE_DELTA)]

    bridge.remove_event_subscriber(sub)
    bridge.on_event("id-X", _ev())
    assert len(received) == 1  # no new event after removal


def test_priority_override_set_get_clear(bridge: DashboardBridge) -> None:
    bridge.set_priority_override("id-A", rank=2, set_by="alice")
    bridge.set_priority_override("id-B", rank=0, set_by="alice")
    bridge.set_priority_override("id-C", rank=1, set_by="alice")
    overrides = bridge.get_priority_overrides()
    assert overrides == {"id-A": 2, "id-B": 0, "id-C": 1}

    bridge.clear_priority_override("id-B")
    overrides = bridge.get_priority_overrides()
    assert "id-B" not in overrides


def test_priority_override_expires(bridge: DashboardBridge) -> None:
    # ttl of effectively-zero seconds via a negative-ish ttl edge; emulate by
    # writing a row directly with expires_at in the past.
    bridge.set_priority_override("id-A", rank=0, set_by="alice", ttl_hours=0.0001)
    time.sleep(0.5)
    overrides = bridge.get_priority_overrides()
    assert "id-A" not in overrides


def test_reorder_pending_assigns_sequential_ranks(bridge: DashboardBridge) -> None:
    bridge.reorder_pending(["id-X", "id-Y", "id-Z"], set_by="alice")
    overrides = bridge.get_priority_overrides()
    assert overrides == {"id-X": 0, "id-Y": 1, "id-Z": 2}


def test_fetch_events_filters_by_attempt(bridge: DashboardBridge) -> None:
    bridge.record_attempt_number("id-A", 1)
    bridge.on_event("id-A", _ev(turn=1))
    bridge.record_attempt_number("id-A", 2)
    bridge.on_event("id-A", _ev(turn=2))

    all_events = bridge.fetch_events("id-A")
    assert len(all_events) == 2
    only_first = bridge.fetch_events("id-A", attempt_number=1)
    assert len(only_first) == 1
    assert only_first[0]["data"]["turn"] == 1
