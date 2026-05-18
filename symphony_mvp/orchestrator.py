"""Orchestrator — SPEC.md Section 9 (Coordination layer).

The Orchestrator is the ONLY component that mutates scheduling state. Every
worker outcome flows back here as an explicit state transition. This makes
restart recovery feasible from tracker state + filesystem alone (no DB needed).

Per-tick sequence (SPEC, fixed order):
  1. Reconcile  — detect stalls, refresh tracker state for running issues,
                  terminate workers whose tickets went terminal externally.
  2. Preflight  — re-validate config; on failure, skip dispatch this tick
                  (but reconciliation still runs).
  3. Fetch      — pull candidate issues from tracker.
  4. Sort       — priority asc, then created_at asc, then identifier lex.
  5. Dispatch   — within concurrency limit, start new workers.

Run is single-threaded — workers run on background threads but the tick loop
itself is sequential. Symphony is not a high-concurrency platform; it's a
work-coordinator. Concurrency lives in the agent processes themselves.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol, runtime_checkable

from .agent_runner import AgentEvent, AgentEventKind, AgentRunner
from .models import Issue, RunAttempt, RunState, TerminalReason
from .tracker import Tracker
from .workflow import Workflow, render_prompt
from .workspace import WorkspaceManager

logger = logging.getLogger(__name__)

# Children of a parent task whose attempt ended for one of these reasons are
# held back at the dispatch gate — the parent failed, so the subtask graph
# under it is no longer meaningful. Keep distinct from the SPEC success set
# ({HANDOFF, TRACKER_TERMINAL, AGENT_FINISHED}); anything outside that success
# set counts as failure here.
_PARENT_FAILURE_REASONS: frozenset[TerminalReason] = frozenset(
    {
        TerminalReason.ABORTED,
        TerminalReason.ERROR,
        TerminalReason.MAX_TURNS,
        TerminalReason.STALL_TIMEOUT,
        TerminalReason.USER_INPUT_REQUIRED,
        # Phase D3 — a parent waiting on human resolution must hold its
        # children too; resuming the parent via resolve_human_block
        # un-gates them on the next dispatch tick.
        TerminalReason.NEEDS_HUMAN,
    }
)
_PARENT_LABEL_PREFIX = "parent:"


@runtime_checkable
class DashboardBridgeProtocol(Protocol):
    """Optional dashboard hook surface — lets the dashboard plug into orchestrator
    behaviour without orchestrator depending on the dashboard subpackage.

    A bridge that does not want to participate in a given hook may return an empty
    list / dict / no-op. Methods are called on the orchestrator's tick / worker
    threads — implementations must be thread-safe.
    """

    def fetch_pending_hints(self, issue_id: str) -> list[dict[str, Any]]:
        """Return prompt supplements pending injection for this issue."""

    def mark_hints_consumed(self, hint_ids: list[int]) -> None:
        """Flag hints as injected so they are not re-applied on retry."""

    def get_priority_overrides(self) -> dict[str, int]:
        """Return {issue_id: rank} mapping to apply BEFORE spec sort key.
        Lower rank dispatches first; missing ids fall back to spec ordering."""

    def notify_fsm_transition(
        self, issue_id: str, from_state: str, to_state: str
    ) -> None:
        """Lifecycle hook invoked on RunState transitions for WS broadcast."""


class Orchestrator:
    """Single-process orchestrator. Wraps tracker + workspace + runner + workflow."""

    def __init__(
        self,
        *,
        workflow: Workflow,
        tracker: Tracker,
        workspaces: WorkspaceManager,
        runner: AgentRunner,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        bridge: DashboardBridgeProtocol | None = None,
    ) -> None:
        self.workflow = workflow
        self.tracker = tracker
        self.workspaces = workspaces
        self.runner = runner
        self.clock = clock
        # Optional dashboard hook surface. None == headless / CI mode (default).
        self.bridge = bridge

        # Internal state — owned exclusively by the orchestrator thread/tick.
        self._attempts: dict[str, RunAttempt] = {}  # keyed by issue.id
        self._workers: dict[str, _AgentWorker] = {}
        self._attempt_count: dict[str, int] = defaultdict(int)
        self._stop = threading.Event()
        self._lock = threading.RLock()  # Reentrant — _release can be invoked from
                                        # within an already-held lock context.
        self._on_event: list[Callable[[str, AgentEvent], None]] = []

        # Persistent retry queue — opt-in via bridge.
        # If the bridge exposes load_active_attempts(), rehydrate _attempts
        # from SQLite so a process restart doesn't lose in-flight scheduling
        # state. Released attempts are not stored in the persistent table
        # (their snapshot lives in attempt_history instead), so this loop
        # only re-populates non-terminal attempts.
        if bridge is not None and hasattr(bridge, "load_active_attempts"):
            try:
                self._hydrate_active_attempts()
            except Exception:  # noqa: BLE001
                logger.exception("bridge.load_active_attempts hydration failed")

    # ----- Public API -----
    def add_event_listener(self, fn: Callable[[str, AgentEvent], None]) -> None:
        """Subscribe to per-issue agent events. Called as fn(issue_id, event)."""
        self._on_event.append(fn)

    def state_snapshot(self) -> dict[str, Any]:
        """Operator-visible state — backs the dashboard / `--json` introspection."""
        with self._lock:
            return {
                "tick_at": self.clock().isoformat(),
                "attempts": [
                    {
                        "issue_id": a.issue_id,
                        "attempt_number": a.attempt_number,
                        "state": a.state.value,
                        "session_id": a.session_id,
                        "turns_consumed": a.turns_consumed,
                        "terminal_reason": a.terminal_reason.value
                        if a.terminal_reason
                        else None,
                        "last_event_at": a.last_event_at.isoformat()
                        if a.last_event_at
                        else None,
                        "error_message": a.error_message,
                    }
                    for a in self._attempts.values()
                ],
            }

    def run_forever(self) -> None:
        """Main loop. Blocks until stop() is called."""
        logger.info("Symphony starting (interval=%dms)", self.workflow.config.polling_interval_ms)
        self._startup_cleanup()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("Tick failed; will retry next interval")
            self._stop.wait(self.workflow.config.polling_interval_ms / 1000.0)
        # Final shutdown — wait briefly for worker threads
        for w in list(self._workers.values()):
            w.join(timeout=5.0)
        logger.info("Symphony stopped")

    def stop(self) -> None:
        self._stop.set()

    # ----- Tick: 5-step sequence -----
    def tick(self) -> None:
        now = self.clock()
        self._reconcile(now)
        if not self._preflight():
            return
        candidates = self.tracker.fetch_active(self.workflow.config.active_states)
        # Dashboard opt-in: apply human-set priority overrides BEFORE spec sort.
        # Override rank wins; missing ids fall back to (priority, created_at, id).
        overrides: dict[str, int] = {}
        if self.bridge is not None:
            try:
                overrides = self.bridge.get_priority_overrides() or {}
            except Exception:  # noqa: BLE001
                logger.exception("Bridge get_priority_overrides failed; ignoring")
        candidates.sort(
            key=lambda i: (
                overrides.get(i.id, 1_000_000),
                i.priority,
                i.created_at,
                i.identifier,
            )
        )
        self._dispatch(candidates, now)

    # ----- Step 1: Reconcile -----
    def _reconcile(self, now: datetime) -> None:
        with self._lock:
            running_attempts = [
                a for a in self._attempts.values() if a.is_active()
            ]
        if not running_attempts:
            return
        # Cooperative pause — operator-set paused_until in the future.
        # We park the attempt in RETRY_QUEUED (preserving session_id) so resume()
        # can clear it and the next tick re-dispatches naturally.
        for att in running_attempts:
            if att.paused_until and att.paused_until > now:
                logger.info(
                    "Issue %s paused (until %s) — releasing worker",
                    att.issue_id,
                    att.paused_until,
                )
                self._park_for_pause(att)
        with self._lock:
            running_attempts = [
                a for a in self._attempts.values() if a.is_active()
            ]
        # Stall detection — any agent silent past stall_timeout_ms gets terminated
        stall_window = timedelta(milliseconds=self.workflow.config.stall_timeout_ms)
        for att in running_attempts:
            ref = att.last_event_at or att.started_at
            if ref and (now - ref) > stall_window:
                logger.warning(
                    "Issue %s stalled (no event for %s) — terminating",
                    att.issue_id,
                    now - ref,
                )
                self._release(att, TerminalReason.STALL_TIMEOUT, "stalled")
        # External-terminal detection — refresh tracker state
        ids = [a.issue_id for a in running_attempts]
        try:
            states = self.tracker.reconcile_states(ids)
        except Exception:  # noqa: BLE001
            logger.exception("Tracker reconcile failed; skipping external check this tick")
            return
        cfg = self.workflow.config
        terminal_set = {s.lower() for s in cfg.terminal_states}
        handoff_set = {cfg.handoff_state.lower()}
        for iid, state in states.items():
            att = self._attempts.get(iid)
            if not att or not att.is_active():
                continue
            if state.lower() in terminal_set:
                self._release(att, TerminalReason.TRACKER_TERMINAL, f"tracker -> {state}")
            elif state.lower() in handoff_set:
                self._release(att, TerminalReason.HANDOFF, f"handoff -> {state}")

    # ----- Step 2: Preflight -----
    def _preflight(self) -> bool:
        cfg = self.workflow.config
        if not cfg.tracker_kind:
            logger.error("preflight: tracker.kind missing")
            return False
        if cfg.tracker_kind in ("github", "linear") and not cfg.tracker_api_key:
            logger.error("preflight: tracker.api_key missing")
            return False
        return True

    # ----- Step 5: Dispatch -----
    def _dispatch(self, candidates: list[Issue], now: datetime) -> None:
        cfg = self.workflow.config
        with self._lock:
            running = sum(1 for a in self._attempts.values() if a.is_active())
            slots = max(0, cfg.max_concurrent_agents - running)
        if slots == 0:
            return
        for issue in candidates:
            if slots <= 0:
                break
            with self._lock:
                existing = self._attempts.get(issue.id)
                if existing and existing.is_active():
                    continue
                # Already released with a terminal (non-retry) reason — leave alone
                if existing and existing.state == RunState.RELEASED:
                    continue
                if existing and existing.state == RunState.RETRY_QUEUED:
                    if existing.paused_until and existing.paused_until > now:
                        continue
                    if existing.retry_after and now < existing.retry_after:
                        continue
                # Block if dependencies aren't done
                if self._has_open_blockers(issue):
                    continue
                attempt_num = self._attempt_count[issue.id] + 1
                if attempt_num > cfg.retry_max_attempts and existing:
                    # Out of retry budget — leave released with prior reason.
                    continue
                self._attempt_count[issue.id] = attempt_num
                prior_state = (
                    existing.state.value if existing else RunState.UNCLAIMED.value
                )
                att = RunAttempt(
                    issue_id=issue.id,
                    attempt_number=attempt_num,
                    state=RunState.CLAIMED,
                    started_at=now,
                    last_event_at=now,
                    session_id=existing.session_id if existing else None,
                )
                self._attempts[issue.id] = att
                self._persist_attempt_state(att)
            if self.bridge is not None:
                try:
                    self.bridge.record_attempt_number(issue.id, attempt_num)
                except Exception:  # noqa: BLE001
                    logger.exception("bridge.record_attempt_number raised")
                try:
                    self.bridge.notify_fsm_transition(
                        issue.id, prior_state, RunState.CLAIMED.value
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("bridge.notify_fsm_transition raised")
            self._spawn_worker(issue, att)
            slots -= 1

    def _has_open_blockers(self, issue: Issue) -> bool:
        # Phase D1 parent gate: if the issue carries a parent:<id> label and
        # the parent's attempt finalised in a failure terminal_reason, freeze
        # this child. Parent failure poisons the subtask graph under it.
        parent_id = self._extract_parent_id(issue.labels)
        if parent_id is not None:
            parent_att = self._attempts.get(parent_id)
            if (
                parent_att is not None
                and parent_att.state == RunState.RELEASED
                and parent_att.terminal_reason in _PARENT_FAILURE_REASONS
            ):
                return True
        if not issue.blocked_by:
            return False
        try:
            states = self.tracker.reconcile_states(issue.blocked_by)
        except Exception:  # noqa: BLE001
            return True  # Be conservative: don't dispatch if we can't verify
        terminal_set = {s.lower() for s in self.workflow.config.terminal_states}
        for blocker_id in issue.blocked_by:
            state = states.get(blocker_id, "open")
            if state.lower() not in terminal_set:
                return True
        return False

    @staticmethod
    def _extract_parent_id(labels: list[str]) -> str | None:
        for label in labels:
            if label.startswith(_PARENT_LABEL_PREFIX):
                return label[len(_PARENT_LABEL_PREFIX):]
        return None

    # ----- Worker management -----
    def _spawn_worker(self, issue: Issue, att: RunAttempt) -> None:
        worker = _AgentWorker(self, issue, att)
        with self._lock:
            self._workers[issue.id] = worker
        worker.start()

    def _on_worker_event(self, issue_id: str, event: AgentEvent) -> None:
        """Called from worker threads. Updates attempt + notifies listeners."""
        prev_state: str | None = None
        new_state: str | None = None
        with self._lock:
            att = self._attempts.get(issue_id)
            if not att:
                return
            att.mark_event()
            if event.kind == AgentEventKind.TURN_STARTED:
                if att.state != RunState.RUNNING:
                    prev_state = att.state.value
                    att.state = RunState.RUNNING
                    new_state = att.state.value
                att.turns_consumed += 1
            elif event.kind == AgentEventKind.ITEM_STARTED and event.data.get("session_id"):
                att.session_id = event.data["session_id"]
            elif event.kind == AgentEventKind.TURN_COMPLETED:
                if event.data.get("session_id"):
                    att.session_id = event.data["session_id"]
                # Accumulate cost from each completed turn for the live UI badge.
                turn_cost = event.data.get("cost_usd")
                if isinstance(turn_cost, (int, float)):
                    att.cost_usd = (att.cost_usd or 0.0) + float(turn_cost)
            elif event.kind == AgentEventKind.USER_INPUT_REQUIRED:
                att.error_message = "user_input_required (high-trust mode treats as failure)"
            elif event.kind == AgentEventKind.ERROR:
                att.error_message = str(event.data.get("message", "unknown error"))
        if prev_state and new_state and self.bridge is not None:
            try:
                self.bridge.notify_fsm_transition(issue_id, prev_state, new_state)
            except Exception:  # noqa: BLE001
                logger.exception("bridge.notify_fsm_transition raised")
        for fn in self._on_event:
            try:
                fn(issue_id, event)
            except Exception:  # noqa: BLE001
                logger.exception("Event listener raised")

        if event.kind == AgentEventKind.MESSAGE_DELTA:
            self._emit_checkpoints_from(issue_id, event)

    def _emit_checkpoints_from(self, issue_id: str, event: AgentEvent) -> None:
        """Phase D4 — scan a MESSAGE_DELTA event's text for ``[CHECKPOINT]``
        markers and fan synthetic CHECKPOINT events through the same listener
        chain so the bridge persists them and the WS push surfaces them live."""
        text = event.data.get("text") if isinstance(event.data, dict) else None
        if not isinstance(text, str) or not text:
            return
        from .checkpoint import detect_checkpoints  # noqa: PLC0415
        specs = detect_checkpoints(text)
        if not specs:
            return
        for spec in specs:
            cp_event = AgentEvent.now(AgentEventKind.CHECKPOINT, **spec)
            for fn in self._on_event:
                try:
                    fn(issue_id, cp_event)
                except Exception:  # noqa: BLE001
                    logger.exception("Event listener raised on CHECKPOINT")

    def _on_worker_done(self, issue_id: str, reason: str | None) -> None:
        """Worker thread finished (cleanly or with error). Map to terminal reason.

        Idempotent — workers may invoke this twice (once on DONE event, once in
        the finally block). Second call is a no-op once the attempt is RELEASED.
        """
        with self._lock:
            att = self._attempts.get(issue_id)
            if not att:
                return
            if att.state == RunState.RELEASED:
                # Already finalized; just clear worker bookkeeping and return.
                self._workers.pop(issue_id, None)
                return
            cfg = self.workflow.config
            if att.error_message:
                terminal = TerminalReason.ERROR
                if "user_input_required" in att.error_message:
                    terminal = TerminalReason.USER_INPUT_REQUIRED
            elif reason == "max_turns" or att.turns_consumed >= cfg.max_turns:
                terminal = TerminalReason.MAX_TURNS
            else:
                # Phase D3 — agent may have ended its turn with a
                # `[HUMAN_REQUIRED] <reason>` marker asking to escalate.
                # Scan the recent message_delta events for it (fenced
                # code blocks are stripped first to defeat prompt
                # injection). Markerless DONE → AGENT_FINISHED as before.
                needs_human_reason = self._detect_needs_human(issue_id)
                if needs_human_reason is not None:
                    terminal = TerminalReason.NEEDS_HUMAN
                    reason = f"needs_human: {needs_human_reason}"
                else:
                    terminal = TerminalReason.AGENT_FINISHED
            self._release(att, terminal, reason or terminal.value, _holding_lock=True)
            self._workers.pop(issue_id, None)

    def _detect_needs_human(self, issue_id: str) -> str | None:
        """Read recent message_delta events for this issue's current
        attempt and check for the `[HUMAN_REQUIRED] <reason>` escalation
        marker. Returns the reason text on match, ``None`` otherwise.
        Bridge-less (headless) mode short-circuits to ``None``."""
        if self.bridge is None:
            return None
        try:
            events = self.bridge.get_recent_events(issue_id, limit=200)
        except Exception:  # noqa: BLE001
            return None
        from .escalation import detect_needs_human  # noqa: PLC0415
        parts: list[str] = []
        for ev in events:
            if ev.kind.value != "message_delta":
                continue
            text = ev.data.get("text") if isinstance(ev.data, dict) else None
            if isinstance(text, str):
                parts.append(text)
        return detect_needs_human("".join(parts))

    def _release(
        self,
        att: RunAttempt,
        reason: TerminalReason,
        message: str,
        *,
        _holding_lock: bool = False,
    ) -> None:
        """Move an attempt to terminal state. Schedule retry if eligible."""
        prev_state = att.state.value

        def _do() -> None:
            att.ended_at = self.clock()
            att.terminal_reason = reason
            cfg = self.workflow.config
            retry_eligible = reason in (
                TerminalReason.STALL_TIMEOUT,
                TerminalReason.ERROR,
            ) and att.attempt_number < cfg.retry_max_attempts
            if retry_eligible:
                # Exponential backoff
                backoff_ms = min(
                    cfg.retry_initial_backoff_ms * (2 ** (att.attempt_number - 1)),
                    cfg.retry_max_backoff_ms,
                )
                att.retry_after = self.clock() + timedelta(milliseconds=backoff_ms)
                att.state = RunState.RETRY_QUEUED
                logger.info(
                    "Issue %s released %s (will retry in %dms): %s",
                    att.issue_id,
                    reason.value,
                    backoff_ms,
                    message,
                )
            else:
                att.state = RunState.RELEASED
                logger.info("Issue %s RELEASED %s: %s", att.issue_id, reason.value, message)

        if _holding_lock:
            _do()
        else:
            with self._lock:
                _do()

        new_state = att.state.value

        # Persist + broadcast state change so dashboards see the move.
        # If the attempt is now RELEASED, drop its row from attempts_state —
        # the canonical home for finalised attempts is attempt_history.
        # RETRY_QUEUED stays in attempts_state so a restart picks it up.
        if self.bridge is not None:
            try:
                self.bridge.record_finalised_attempt(att)
            except Exception:  # noqa: BLE001
                logger.exception("bridge.record_finalised_attempt raised")
            if att.state == RunState.RELEASED:
                self._delete_attempt_state(att.issue_id)
            else:
                self._persist_attempt_state(att)
            if prev_state != new_state:
                try:
                    self.bridge.notify_fsm_transition(
                        att.issue_id, prev_state, new_state
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("bridge.notify_fsm_transition raised")

        # Terminate any active worker for this issue
        worker = None
        with self._lock:
            worker = self._workers.get(att.issue_id)
        if worker and worker.is_alive() and worker.thread_id != threading.get_ident():
            worker.stop()

    # ----- Dashboard cooperative actions (opt-in) -----
    def _park_for_pause(self, att: RunAttempt) -> None:
        """Move an active attempt into RETRY_QUEUED while preserving session_id."""
        with self._lock:
            att.state = RunState.RETRY_QUEUED
            att.retry_after = att.paused_until  # block dispatch until resume
        worker = self._workers.get(att.issue_id)
        if worker and worker.is_alive():
            worker.stop()

    def pause(self, issue_id: str, *, until: datetime | None = None) -> bool:
        """Cooperatively pause this issue. Worker stops on next reconcile.

        Returns True if a state change was applied. The attempt parks in
        RETRY_QUEUED with retry_after = paused_until so dispatch holds off
        until resume() is called.
        """
        sentinel = until or datetime(2999, 12, 31, tzinfo=timezone.utc)
        with self._lock:
            att = self._attempts.get(issue_id)
            if not att:
                return False
            att.paused_until = sentinel
            if att.is_active():
                self._park_for_pause(att)
            elif att.state == RunState.RETRY_QUEUED:
                att.retry_after = sentinel
        return True

    def resume(self, issue_id: str) -> bool:
        """Clear pause sentinel; next tick re-dispatches with same session_id."""
        with self._lock:
            att = self._attempts.get(issue_id)
            if not att or att.paused_until is None:
                return False
            att.paused_until = None
            att.retry_after = None
            # If the attempt was already RELEASED, leave it alone — only the
            # parked-in-RETRY_QUEUED case is meaningfully resumable.
        return True

    def abort(self, issue_id: str, *, message: str = "operator aborted") -> bool:
        """Hard cancel: stop worker, mark attempt RELEASED with ABORTED reason.

        Skips retry queue: ABORTED is not in the retry-eligible reason set.
        """
        with self._lock:
            att = self._attempts.get(issue_id)
            if not att:
                return False
            if att.state == RunState.RELEASED:
                return False
        self._release(att, TerminalReason.ABORTED, message)
        return True

    def abort_all(
        self, *, message: str = "operator emergency stop"
    ) -> list[str]:
        """Emergency stop: abort every active attempt (CLAIMED or RUNNING).

        Returns the list of issue_ids that were aborted. Idempotent — calling
        again immediately returns an empty list. RETRY_QUEUED attempts are
        NOT touched (they have no live worker; pause/resume govern them).
        """
        with self._lock:
            targets = [
                att for att in self._attempts.values()
                if att.state in (RunState.CLAIMED, RunState.RUNNING)
            ]
        aborted: list[str] = []
        for att in targets:
            if self.abort(att.issue_id, message=message):
                aborted.append(att.issue_id)
        return aborted

    def force_retry(self, issue_id: str) -> bool:
        """Allow a previously RELEASED issue to be re-dispatched.

        We clear the attempt entry (preserving last session_id for continuity)
        and reset the attempt counter. Next tick treats the issue as fresh —
        but with its session_id carried forward so Claude can resume context.
        """
        with self._lock:
            att = self._attempts.get(issue_id)
            if not att or att.state != RunState.RELEASED:
                return False
            session_id = att.session_id
            self._attempts.pop(issue_id, None)
            self._attempt_count.pop(issue_id, None)
            # Stash the session_id under a sentinel attempt so the next dispatch
            # picks it up via the existing `existing.session_id` path.
            self._attempts[issue_id] = RunAttempt(
                issue_id=issue_id,
                attempt_number=0,
                state=RunState.UNCLAIMED,
                session_id=session_id,
            )
        return True

    # ----- Persistent retry queue helpers (opt-in via bridge) -----
    def _persist_attempt_state(self, att: RunAttempt) -> None:
        """Snapshot one in-flight attempt to the bridge so it survives a
        restart. No-op when bridge has no persist surface."""
        if self.bridge is None or not hasattr(self.bridge, "persist_attempt"):
            return
        try:
            self.bridge.persist_attempt(att)
        except Exception:  # noqa: BLE001
            logger.exception("bridge.persist_attempt raised for %s", att.issue_id)

    def _delete_attempt_state(self, issue_id: str) -> None:
        if self.bridge is None or not hasattr(self.bridge, "delete_attempt_state"):
            return
        try:
            self.bridge.delete_attempt_state(issue_id)
        except Exception:  # noqa: BLE001
            logger.exception("bridge.delete_attempt_state raised for %s", issue_id)

    def _hydrate_active_attempts(self) -> None:
        """Reconstruct ``self._attempts`` from ``bridge.load_active_attempts()``.

        Only loads non-terminal rows (RELEASED attempts live in
        ``attempt_history`` instead). Reconcile + retry / stall logic on the
        next tick will treat hydrated attempts as if they had never paused —
        e.g. a RUNNING row with stale ``last_event_at`` will trigger
        ``STALL_TIMEOUT`` and flow through the existing retry queue.
        """
        from dateutil.parser import isoparse  # type: ignore[import-untyped]

        rows = self.bridge.load_active_attempts()  # type: ignore[union-attr]
        if not rows:
            return
        loaded = 0
        for row in rows:
            try:
                state = RunState(row["state"])
            except (KeyError, ValueError):
                continue
            if state == RunState.RELEASED:
                # Defensive: should never appear here, but skip if it does.
                continue

            def _opt_dt(key: str) -> datetime | None:
                v = row.get(key)
                return isoparse(v) if v else None

            att = RunAttempt(
                issue_id=row["issue_id"],
                attempt_number=int(row["attempt_number"]),
                state=state,
                started_at=_opt_dt("started_at"),
                last_event_at=_opt_dt("last_event_at"),
                session_id=row.get("session_id"),
                turns_consumed=int(row.get("turns_consumed") or 0),
                cost_usd=float(row.get("cost_usd") or 0.0),
                error_message=row.get("error_message"),
                retry_after=_opt_dt("retry_after"),
                paused_until=_opt_dt("paused_until"),
            )
            self._attempts[att.issue_id] = att
            # Restore attempt_count so the next dispatch increments past it.
            self._attempt_count[att.issue_id] = max(
                self._attempt_count[att.issue_id], att.attempt_number
            )
            loaded += 1
        if loaded:
            logger.info("Hydrated %d active attempt(s) from persistent state", loaded)

    # ----- Startup cleanup -----
    def _startup_cleanup(self) -> None:
        """SPEC: at boot, find existing workspaces whose tracker state is now terminal
        and remove them. Conservative: if reconcile fails, leave workspaces alone."""
        existing = self.workspaces.list_existing()
        if not existing:
            return
        try:
            terminal_issues = self.tracker.fetch_terminal(
                self.workflow.config.terminal_states
            )
        except Exception:  # noqa: BLE001
            logger.warning("Startup cleanup skipped (tracker unreachable)")
            return
        # The workspace dir is sanitize_key(issue.identifier) — match by that.
        from .workspace import sanitize_key
        terminal_keys = {sanitize_key(i.identifier) for i in terminal_issues}
        for path in existing:
            if path.name in terminal_keys:
                logger.info("Startup cleanup: removing %s", path)
                # Build a Workspace stub sufficient for remove()
                from .models import Workspace
                ws = Workspace(
                    issue_id="(startup-cleanup)",
                    issue_identifier=path.name,
                    path=str(path),
                )
                self.workspaces.remove(ws, self.workflow.config.before_remove_hook)


class _AgentWorker(threading.Thread):
    """Per-issue worker thread. Drives the agent runner and reports events back."""

    def __init__(self, orch: Orchestrator, issue: Issue, attempt: RunAttempt) -> None:
        super().__init__(daemon=True, name=f"agent-{issue.identifier}")
        self.orch = orch
        self.issue = issue
        self.attempt = attempt
        self._stop = threading.Event()
        self.thread_id: int | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self.thread_id = threading.get_ident()
        cfg = self.orch.workflow.config
        ws = self.orch.workspaces.ensure(
            self.issue.id, self.issue.identifier, cfg.after_create_hook
        )
        # Dashboard opt-in: pull pending operator hints, inject into prompt ctx,
        # and mark them consumed only AFTER render succeeds. Render failure
        # leaves hints pending so the next attempt still sees them.
        hints: list[dict[str, Any]] = []
        hint_ids: list[int] = []
        if self.orch.bridge is not None:
            try:
                hints = self.orch.bridge.fetch_pending_hints(self.issue.id) or []
                hint_ids = [int(h["id"]) for h in hints if "id" in h]
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Bridge fetch_pending_hints failed for %s; proceeding without hints",
                    self.issue.identifier,
                )
        try:
            ctx = self.issue.to_template_context(self.attempt.attempt_number, hints=hints)
            prompt = render_prompt(self.orch.workflow, ctx)
        except Exception as e:  # noqa: BLE001
            logger.exception("Prompt render failed for %s", self.issue.identifier)
            self.orch._on_worker_event(
                self.issue.id,
                AgentEvent.now(AgentEventKind.ERROR, message=f"render: {e}"),
            )
            self.orch._on_worker_done(self.issue.id, reason="render_error")
            return
        if hint_ids and self.orch.bridge is not None:
            try:
                self.orch.bridge.mark_hints_consumed(hint_ids)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Bridge mark_hints_consumed failed for %s (hint_ids=%s)",
                    self.issue.identifier,
                    hint_ids,
                )

        if cfg.before_run_hook:
            try:
                self.orch.workspaces._run_hook(  # noqa: SLF001
                    "before_run", cfg.before_run_hook, ws.path, fatal=True
                )
            except Exception as e:  # noqa: BLE001
                self.orch._on_worker_event(
                    self.issue.id,
                    AgentEvent.now(AgentEventKind.ERROR, message=f"before_run: {e}"),
                )
                self.orch._on_worker_done(self.issue.id, reason="before_run_failed")
                return

        # Per-task mode override: the Tool API stores the chosen mode as a
        # "mode:plan" / "mode:build" / "mode:review" label on the issue. If
        # present, translate it into a runner allowed_tools whitelist for
        # this attempt only; otherwise pass None (= use the runner's
        # configured default — preserves legacy behaviour).
        mode_allowed_tools: list[str] | None = None
        try:
            from .dashboard.mode import (  # noqa: PLC0415
                allowed_tools_for, extract_mode_from_labels,
            )
            mode = extract_mode_from_labels(self.issue.labels)
            if mode is not None:
                mode_allowed_tools = allowed_tools_for(mode)
                logger.info(
                    "Issue %s mode=%s allowed_tools=%s",
                    self.issue.identifier, mode, mode_allowed_tools,
                )
        except Exception:  # noqa: BLE001
            logger.exception("mode label resolution raised; defaulting to runner config")

        try:
            for event in self.orch.runner.run(
                prompt=prompt,
                workspace_path=ws.path,
                max_turns=cfg.max_turns,
                session_id=self.attempt.session_id,
                allowed_tools=mode_allowed_tools,
            ):
                if self._stop.is_set():
                    logger.info("Worker for %s stopping early", self.issue.identifier)
                    break
                self.orch._on_worker_event(self.issue.id, event)
                if event.kind == AgentEventKind.DONE:
                    self.orch._on_worker_done(
                        self.issue.id, reason=str(event.data.get("reason"))
                    )
                    return
        except Exception:  # noqa: BLE001
            logger.exception("Worker crashed for %s", self.issue.identifier)
            self.orch._on_worker_event(
                self.issue.id,
                AgentEvent.now(AgentEventKind.ERROR, message="worker crashed"),
            )
        finally:
            if cfg.after_run_hook:
                try:
                    self.orch.workspaces._run_hook(  # noqa: SLF001
                        "after_run", cfg.after_run_hook, ws.path, fatal=False
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("after_run hook failed (non-fatal)")
            self.orch._on_worker_done(self.issue.id, reason=None)
