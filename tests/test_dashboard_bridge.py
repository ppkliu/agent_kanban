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


# --------------------------------------------------------------------------- #
# Persistent retry queue (attempts_state)                                     #
# --------------------------------------------------------------------------- #

def test_persist_and_load_active_attempt_round_trip(bridge: DashboardBridge) -> None:
    from datetime import timedelta
    from symphony_mvp.models import RunAttempt, RunState

    now = datetime(2026, 5, 8, 10, 0, 0, tzinfo=timezone.utc)
    att = RunAttempt(
        issue_id="id-MT-7",
        attempt_number=2,
        state=RunState.RUNNING,
        started_at=now - timedelta(seconds=30),
        last_event_at=now,
        session_id="sess-abc",
        turns_consumed=4,
        cost_usd=0.01234,
    )
    bridge.persist_attempt(att)

    rows = bridge.load_active_attempts()
    assert len(rows) == 1
    row = rows[0]
    assert row["issue_id"] == "id-MT-7"
    assert row["attempt_number"] == 2
    assert row["state"] == "running"
    assert row["session_id"] == "sess-abc"
    assert row["turns_consumed"] == 4
    assert abs(row["cost_usd"] - 0.01234) < 1e-6


def test_persist_attempt_is_idempotent_on_issue_id(bridge: DashboardBridge) -> None:
    """A second persist for the same issue should overwrite, not duplicate."""
    from symphony_mvp.models import RunAttempt, RunState

    now = datetime.now(timezone.utc)
    att = RunAttempt(
        issue_id="id-X",
        attempt_number=1,
        state=RunState.CLAIMED,
        started_at=now,
        last_event_at=now,
    )
    bridge.persist_attempt(att)

    att.state = RunState.RUNNING
    att.turns_consumed = 7
    bridge.persist_attempt(att)

    rows = bridge.load_active_attempts()
    assert len(rows) == 1
    assert rows[0]["state"] == "running"
    assert rows[0]["turns_consumed"] == 7


def test_delete_attempt_state_removes_row(bridge: DashboardBridge) -> None:
    from symphony_mvp.models import RunAttempt, RunState

    now = datetime.now(timezone.utc)
    bridge.persist_attempt(
        RunAttempt(
            issue_id="id-K",
            attempt_number=1,
            state=RunState.RETRY_QUEUED,
            started_at=now,
            last_event_at=now,
        )
    )
    assert len(bridge.load_active_attempts()) == 1

    bridge.delete_attempt_state("id-K")
    assert bridge.load_active_attempts() == []


def test_load_active_attempts_returns_empty_for_fresh_db(bridge: DashboardBridge) -> None:
    assert bridge.load_active_attempts() == []


# --------------------------------------------------------------------------- #
# Idempotency keys (Tool API Phase C — safe submit_coding_task retry)         #
# --------------------------------------------------------------------------- #

def test_lookup_idempotency_key_returns_none_when_missing(bridge: DashboardBridge) -> None:
    assert bridge.lookup_idempotency_key("never-seen") is None


def test_record_then_lookup_idempotency_round_trip(bridge: DashboardBridge) -> None:
    bridge.record_idempotency_key("client-xyz", "tsk_abc123")
    assert bridge.lookup_idempotency_key("client-xyz") == "tsk_abc123"


def test_record_idempotency_key_overwrites_on_same_key(bridge: DashboardBridge) -> None:
    bridge.record_idempotency_key("client-xyz", "tsk_old")
    bridge.record_idempotency_key("client-xyz", "tsk_new")
    assert bridge.lookup_idempotency_key("client-xyz") == "tsk_new"


def test_lookup_returns_none_for_expired_key(bridge: DashboardBridge) -> None:
    """An expired row should not be returned even if cleanup hasn't run."""
    # Use a negative TTL to make expires_at land in the past.
    bridge.record_idempotency_key("expired-key", "tsk_expired", ttl_hours=-1.0)
    assert bridge.lookup_idempotency_key("expired-key") is None


def test_clear_expired_idempotency_keys_reaps_only_expired(
    bridge: DashboardBridge,
) -> None:
    bridge.record_idempotency_key("fresh", "tsk_fresh", ttl_hours=1.0)
    bridge.record_idempotency_key("stale", "tsk_stale", ttl_hours=-1.0)
    bridge.record_idempotency_key("also-stale", "tsk_also", ttl_hours=-2.0)

    deleted = bridge.clear_expired_idempotency_keys()
    assert deleted == 2
    assert bridge.lookup_idempotency_key("fresh") == "tsk_fresh"
    assert bridge.lookup_idempotency_key("stale") is None
