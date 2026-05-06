"""Agent Runner — SPEC.md Section 8 (Agent Runner layer).

This is the layer Symphony abstracts away from Codex. We provide three runners:

  EchoRunner          — deterministic, no network. Used by tests + demos.
  ClaudeCLIRunner     — wraps `claude -p --output-format stream-json` subprocess.
                        Closest substitute for `codex app-server`.
  AnthropicAPIRunner  — direct /v1/messages calls + custom tool loop.
                        Highest control, best for DPO data collection.

All runners yield the same `AgentEvent` stream so the orchestrator's stall-detection,
turn-counting, and outcome-reporting logic is identical regardless of backend.

Mapping to Codex App-Server JSON-RPC events (from SPEC):
  turn/started            -> AgentEventKind.TURN_STARTED
  item/started            -> AgentEventKind.ITEM_STARTED
  item/agentMessage/delta -> AgentEventKind.MESSAGE_DELTA
  item/completed          -> AgentEventKind.ITEM_COMPLETED
  turn/completed          -> AgentEventKind.TURN_COMPLETED
  user_input_required     -> AgentEventKind.USER_INPUT_REQUIRED  (hard-fail per SPEC)
  (transport error)       -> AgentEventKind.ERROR
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class AgentEventKind(str, Enum):
    TURN_STARTED = "turn_started"
    ITEM_STARTED = "item_started"
    MESSAGE_DELTA = "message_delta"
    ITEM_COMPLETED = "item_completed"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TURN_COMPLETED = "turn_completed"
    USER_INPUT_REQUIRED = "user_input_required"
    ERROR = "error"
    DONE = "done"  # Final event — runner has finished cleanly


@dataclass
class AgentEvent:
    kind: AgentEventKind
    timestamp: datetime
    data: dict[str, Any]

    @classmethod
    def now(cls, kind: AgentEventKind, **data: Any) -> "AgentEvent":
        return cls(kind=kind, timestamp=datetime.now(timezone.utc), data=data)


@runtime_checkable
class AgentRunner(Protocol):
    """All runners stream AgentEvent. The orchestrator does NOT care about the backend."""

    def run(
        self,
        *,
        prompt: str,
        workspace_path: str,
        max_turns: int,
        session_id: str | None = None,
    ) -> Iterator[AgentEvent]:
        """Run one agent session. Yield events until done or terminal failure."""
        ...


# --------------------------------------------------------------------------- #
# Echo runner — deterministic, no external dependency. Used by tests/demos.   #
# --------------------------------------------------------------------------- #
class EchoRunner:
    """A trivial runner that pretends to "do work" then declares done.

    Useful for unit tests and end-to-end orchestrator demos without an LLM.
    Optionally writes a marker file in the workspace so tests can verify
    the agent ran with the correct cwd.
    """

    def __init__(self, *, marker_filename: str = "agent_ran.txt", delay_s: float = 0.0) -> None:
        self.marker_filename = marker_filename
        self.delay_s = delay_s

    def run(
        self,
        *,
        prompt: str,
        workspace_path: str,
        max_turns: int,
        session_id: str | None = None,
    ) -> Iterator[AgentEvent]:
        yield AgentEvent.now(AgentEventKind.TURN_STARTED, turn=1, session_id="echo-session")
        if self.delay_s:
            time.sleep(self.delay_s)
        marker_path = os.path.join(workspace_path, self.marker_filename)
        with open(marker_path, "w", encoding="utf-8") as f:
            f.write(f"prompt_chars={len(prompt)}\n")
        yield AgentEvent.now(
            AgentEventKind.MESSAGE_DELTA, text=f"Wrote {marker_path}"
        )
        yield AgentEvent.now(
            AgentEventKind.TURN_COMPLETED, turn=1, tokens_used=0
        )
        yield AgentEvent.now(AgentEventKind.DONE, reason="agent_finished")


# --------------------------------------------------------------------------- #
# Claude CLI runner — wraps `claude -p --output-format stream-json`.          #
# --------------------------------------------------------------------------- #
class ClaudeCLIRunner:
    """Wraps the Claude Code CLI in headless / streaming mode.

    Equivalent role to `codex app-server` for Symphony — but uses NDJSON
    instead of JSON-RPC 2.0. Event semantics mapped onto AgentEventKind.

    Requirements on the host:
      * `claude` CLI installed and authenticated (`claude login` once)
      * Optional: ANTHROPIC_API_KEY in the environment

    Claude CLI events (relevant subset):
      {"type": "system", "subtype": "init", "session_id": "..."}
      {"type": "assistant", "message": {"content": [...]}}
      {"type": "user", "message": ...}            # tool results come back as user
      {"type": "result", "subtype": "success", "session_id": "...", ...}
    """

    def __init__(
        self,
        *,
        command: str = "claude",
        model: str | None = None,
        allowed_tools: list[str] | None = None,
        permission_mode: str = "acceptEdits",
    ) -> None:
        self.command = command
        self.model = model
        self.allowed_tools = allowed_tools or ["Bash", "Read", "Edit", "Write"]
        self.permission_mode = permission_mode

    def _build_argv(
        self, prompt: str, max_turns: int, session_id: str | None
    ) -> list[str]:
        argv = [
            self.command,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(max_turns),
            "--allowedTools",
            ",".join(self.allowed_tools),
            "--permission-mode",
            self.permission_mode,
        ]
        if self.model:
            argv += ["--model", self.model]
        if session_id:
            argv += ["--resume", session_id]
        return argv

    def run(
        self,
        *,
        prompt: str,
        workspace_path: str,
        max_turns: int,
        session_id: str | None = None,
    ) -> Iterator[AgentEvent]:
        argv = self._build_argv(prompt, max_turns, session_id)
        logger.info("Spawning Claude CLI in %s: %s", workspace_path, shlex.join(argv))

        try:
            proc = subprocess.Popen(
                argv,
                cwd=workspace_path,  # SPEC invariant #1
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            yield AgentEvent.now(
                AgentEventKind.ERROR, message=f"`{self.command}` not found on PATH"
            )
            return

        yield AgentEvent.now(AgentEventKind.TURN_STARTED, turn=1)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON line from claude CLI: %s", line[:120])
                    continue
                yield from self._translate(msg)
        finally:
            rc = proc.wait()
            if rc != 0 and rc is not None:
                err = (proc.stderr.read() if proc.stderr else "") or ""
                yield AgentEvent.now(
                    AgentEventKind.ERROR, message=f"claude exited {rc}: {err[:500]}"
                )
            else:
                yield AgentEvent.now(AgentEventKind.DONE, reason="agent_finished")

    @staticmethod
    def _translate(msg: dict[str, Any]) -> Iterator[AgentEvent]:
        t = msg.get("type")
        if t == "system" and msg.get("subtype") == "init":
            yield AgentEvent.now(
                AgentEventKind.ITEM_STARTED,
                item_kind="system_init",
                session_id=msg.get("session_id"),
            )
        elif t == "assistant":
            content = (msg.get("message") or {}).get("content") or []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    yield AgentEvent.now(AgentEventKind.MESSAGE_DELTA, text=block.get("text", ""))
                elif btype == "tool_use":
                    yield AgentEvent.now(
                        AgentEventKind.TOOL_CALL,
                        tool=block.get("name"),
                        tool_use_id=block.get("id"),
                        input=block.get("input"),
                    )
        elif t == "user":
            content = (msg.get("message") or {}).get("content") or []
            for block in content:
                if block.get("type") == "tool_result":
                    yield AgentEvent.now(
                        AgentEventKind.TOOL_RESULT,
                        tool_use_id=block.get("tool_use_id"),
                        is_error=block.get("is_error", False),
                    )
        elif t == "result":
            sub = msg.get("subtype", "success")
            if sub == "success":
                yield AgentEvent.now(
                    AgentEventKind.TURN_COMPLETED,
                    cost_usd=msg.get("total_cost_usd"),
                    duration_ms=msg.get("duration_ms"),
                    num_turns=msg.get("num_turns"),
                    session_id=msg.get("session_id"),
                )
            else:
                yield AgentEvent.now(
                    AgentEventKind.ERROR, message=f"claude result subtype={sub}"
                )


# --------------------------------------------------------------------------- #
# Anthropic API runner — direct /v1/messages with a custom tool loop.         #
# Use this when you want full control over prompt cache, tool schema, etc.    #
# --------------------------------------------------------------------------- #
class AnthropicAPIRunner:
    """Direct API runner with a minimal tool loop.

    Compared to ClaudeCLIRunner, this:
      * Has no Bash/Edit/Write tools by default (you wire in domain tools)
      * Preserves full message history -> useful for DPO data collection
      * Lets you control prompt-cache breakpoints precisely
      * Talks to ANY Anthropic-compatible endpoint (including local proxies)

    Tool definitions are passed in via `tools` parameter — kept domain-agnostic
    here. For your AIOps fork you'd plug `redfish_query`, `bmc_action`, etc.
    """

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        tool_dispatcher: "ToolDispatcher | None" = None,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.tools = tools or []
        self.tool_dispatcher = tool_dispatcher
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.base_url = base_url.rstrip("/")
        if not self.api_key:
            raise ValueError(
                "AnthropicAPIRunner requires ANTHROPIC_API_KEY env var or api_key arg"
            )

    def run(
        self,
        *,
        prompt: str,
        workspace_path: str,
        max_turns: int,
        session_id: str | None = None,
    ) -> Iterator[AgentEvent]:
        import requests

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        for turn in range(1, max_turns + 1):
            yield AgentEvent.now(AgentEventKind.TURN_STARTED, turn=turn)
            try:
                resp = requests.post(
                    f"{self.base_url}/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": self.max_tokens,
                        "messages": messages,
                        "tools": self.tools,
                    },
                    timeout=120,
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as e:  # noqa: BLE001
                yield AgentEvent.now(AgentEventKind.ERROR, message=f"API call failed: {e}")
                return

            content = payload.get("content", [])
            tool_uses: list[dict[str, Any]] = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    yield AgentEvent.now(AgentEventKind.MESSAGE_DELTA, text=block.get("text", ""))
                elif btype == "tool_use":
                    tool_uses.append(block)
                    yield AgentEvent.now(
                        AgentEventKind.TOOL_CALL,
                        tool=block.get("name"),
                        tool_use_id=block.get("id"),
                        input=block.get("input"),
                    )

            messages.append({"role": "assistant", "content": content})
            stop_reason = payload.get("stop_reason")
            yield AgentEvent.now(
                AgentEventKind.TURN_COMPLETED,
                turn=turn,
                stop_reason=stop_reason,
                usage=payload.get("usage"),
            )

            if stop_reason == "end_turn" and not tool_uses:
                yield AgentEvent.now(AgentEventKind.DONE, reason="agent_finished")
                return

            if tool_uses:
                if self.tool_dispatcher is None:
                    yield AgentEvent.now(
                        AgentEventKind.ERROR,
                        message="Agent called tools but no tool_dispatcher configured",
                    )
                    return
                tool_results: list[dict[str, Any]] = []
                for tu in tool_uses:
                    try:
                        result = self.tool_dispatcher.dispatch(
                            name=tu["name"],
                            inputs=tu.get("input", {}),
                            workspace_path=workspace_path,
                        )
                        is_err = False
                    except Exception as e:  # noqa: BLE001
                        result = f"Tool error: {e}"
                        is_err = True
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": str(result),
                            "is_error": is_err,
                        }
                    )
                    yield AgentEvent.now(
                        AgentEventKind.TOOL_RESULT,
                        tool_use_id=tu["id"],
                        is_error=is_err,
                    )
                messages.append({"role": "user", "content": tool_results})

        # Hit max_turns without natural end_turn.
        yield AgentEvent.now(AgentEventKind.DONE, reason="max_turns")


@runtime_checkable
class ToolDispatcher(Protocol):
    """Pluggable tool implementation. The MVP ships no concrete tools — you wire
    in domain-specific ones (e.g. `redfish_query`, `gh_issue_update`)."""

    def dispatch(self, *, name: str, inputs: dict[str, Any], workspace_path: str) -> Any:
        ...


def build_runner(
    kind: str,
    *,
    command: str | None = None,
    model: str | None = None,
    max_tokens: int = 4096,
) -> AgentRunner:
    """Factory used by orchestrator startup to build the configured runner."""
    if kind == "echo":
        return EchoRunner()
    if kind == "claude_cli":
        return ClaudeCLIRunner(command=command or "claude", model=model)
    if kind == "anthropic_api":
        return AnthropicAPIRunner(model=model or "claude-opus-4-7", max_tokens=max_tokens)
    raise ValueError(f"Unknown runner kind: {kind!r}")
