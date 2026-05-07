"""WORKFLOW.md loader — SPEC.md Section 5 (Policy & Configuration layers).

Parses YAML front matter + Markdown prompt body. Performs strict template
validation (unknown variables/filters fail render rather than silently passing).

Hot-reload semantics: caller is responsible for catching exceptions and keeping
the last-known-good copy alive — see Orchestrator.tick() handling.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined, TemplateSyntaxError, UndefinedError


@dataclass
class WorkflowConfig:
    """Typed runtime values derived from WORKFLOW.md front matter + env resolution.

    Defaults follow SPEC's safer-by-default posture.
    """

    # tracker.* — issue tracker adapter selection
    tracker_kind: str = "github"  # "github" | "linear" | "memory"
    tracker_repo: str | None = None  # "owner/name" for github; project_slug for linear
    tracker_api_key: str | None = None
    active_states: list[str] = field(default_factory=lambda: ["open", "ready"])
    terminal_states: list[str] = field(
        default_factory=lambda: ["closed", "cancelled", "done", "duplicate"]
    )

    # polling.*
    polling_interval_ms: int = 30_000

    # workspace.*
    workspace_root: str = "./workspaces"
    after_create_hook: str | None = None  # Shell script to run after workspace creation
    before_run_hook: str | None = None
    after_run_hook: str | None = None
    before_remove_hook: str | None = None

    # agent.*
    max_concurrent_agents: int = 5
    max_turns: int = 20
    stall_timeout_ms: int = 600_000  # 10 minutes
    retry_initial_backoff_ms: int = 10_000
    retry_max_backoff_ms: int = 300_000
    retry_max_attempts: int = 3

    # agent runner backend selection — Symphony spec leaves this open
    runner_kind: str = "claude_cli"  # "claude_cli" | "anthropic_api" | "echo" | "opencode"
    runner_command: str | None = None  # Override CLI command if needed
    runner_model: str = "claude-opus-4-7"
    runner_max_tokens: int = 4096
    runner_provider: str = "anthropic"  # used by opencode (LiteLLM provider)
    runner_allowed_tools: list[str] | None = None  # None ⇒ runner-specific default

    # Handoff state — when agent moves issue here, treat as success (not error)
    handoff_state: str = "in_review"

    # observability.*
    server_port: int | None = None  # Optional HTTP dashboard port


@dataclass
class Workflow:
    """Loaded workflow: typed config + raw prompt template body."""

    config: WorkflowConfig
    prompt_template: str
    source_path: Path
    raw_front_matter: dict[str, Any]


_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_ENV_VAR_RE = re.compile(r"\$([A-Z_][A-Z0-9_]*)")


def _resolve_env(value: Any) -> Any:
    """Recursively expand $VAR references in string values."""
    if isinstance(value, str):
        def _sub(m: re.Match) -> str:
            var = m.group(1)
            return os.environ.get(var, m.group(0))  # leave as-is if unset
        return _ENV_VAR_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def load_workflow(path: str | Path) -> Workflow:
    """Load + parse WORKFLOW.md. Raises ValueError on malformed input.

    The orchestrator catches these and decides whether to (a) fail to boot
    on the first load, or (b) keep last-known-good on a hot-reload failure.
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"WORKFLOW.md not found at {p}")
    text = p.read_text(encoding="utf-8")

    m = _FRONT_MATTER_RE.match(text)
    if not m:
        raise ValueError(
            f"{p}: missing YAML front matter (must start with '---' on first line)"
        )
    front_matter_text = m.group(1)
    body = text[m.end():].strip()

    try:
        fm: dict[str, Any] = yaml.safe_load(front_matter_text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"{p}: invalid YAML front matter: {e}") from e

    fm = _resolve_env(fm)
    config = _build_config(fm)
    _validate_template(body)

    return Workflow(
        config=config,
        prompt_template=body,
        source_path=p,
        raw_front_matter=fm,
    )


def _build_config(fm: dict[str, Any]) -> WorkflowConfig:
    """Map front matter onto WorkflowConfig with defaults."""
    cfg = WorkflowConfig()

    tracker = fm.get("tracker", {}) or {}
    cfg.tracker_kind = tracker.get("kind", cfg.tracker_kind)
    cfg.tracker_repo = tracker.get("repo") or tracker.get("project_slug")
    cfg.tracker_api_key = tracker.get("api_key")
    cfg.active_states = tracker.get("active_states", cfg.active_states)
    cfg.terminal_states = tracker.get("terminal_states", cfg.terminal_states)

    polling = fm.get("polling", {}) or {}
    cfg.polling_interval_ms = int(polling.get("interval_ms", cfg.polling_interval_ms))

    workspace = fm.get("workspace", {}) or {}
    cfg.workspace_root = workspace.get("root", cfg.workspace_root)
    hooks = workspace.get("hooks", {}) or {}
    cfg.after_create_hook = hooks.get("after_create")
    cfg.before_run_hook = hooks.get("before_run")
    cfg.after_run_hook = hooks.get("after_run")
    cfg.before_remove_hook = hooks.get("before_remove")

    agent = fm.get("agent", {}) or {}
    cfg.max_concurrent_agents = int(
        agent.get("max_concurrent_agents", cfg.max_concurrent_agents)
    )
    cfg.max_turns = int(agent.get("max_turns", cfg.max_turns))
    cfg.stall_timeout_ms = int(agent.get("stall_timeout_ms", cfg.stall_timeout_ms))
    cfg.retry_initial_backoff_ms = int(
        agent.get("retry_initial_backoff_ms", cfg.retry_initial_backoff_ms)
    )
    cfg.retry_max_backoff_ms = int(
        agent.get("retry_max_backoff_ms", cfg.retry_max_backoff_ms)
    )
    cfg.retry_max_attempts = int(
        agent.get("retry_max_attempts", cfg.retry_max_attempts)
    )
    cfg.handoff_state = agent.get("handoff_state", cfg.handoff_state)

    runner = fm.get("runner", {}) or {}
    cfg.runner_kind = runner.get("kind", cfg.runner_kind)
    cfg.runner_command = runner.get("command")
    cfg.runner_model = runner.get("model", cfg.runner_model)
    cfg.runner_max_tokens = int(runner.get("max_tokens", cfg.runner_max_tokens))
    cfg.runner_provider = runner.get("provider", cfg.runner_provider)
    cfg.runner_allowed_tools = runner.get("allowed_tools", cfg.runner_allowed_tools)

    server = fm.get("server", {}) or {}
    cfg.server_port = server.get("port")

    return cfg


def _validate_template(body: str) -> None:
    """Strict syntax check — render-time variable check is enforced via StrictUndefined."""
    env = Environment(undefined=StrictUndefined)
    try:
        env.parse(body)
    except TemplateSyntaxError as e:
        raise ValueError(f"WORKFLOW.md prompt template has syntax error: {e}") from e


def render_prompt(workflow: Workflow, context: dict[str, Any]) -> str:
    """Render the prompt for a specific issue + attempt.

    Raises ValueError if the template references unknown variables — SPEC requires
    this to fail loudly rather than render an empty/incorrect prompt.
    """
    env = Environment(undefined=StrictUndefined)
    template = env.from_string(workflow.prompt_template)
    try:
        return template.render(**context)
    except UndefinedError as e:
        raise ValueError(f"Prompt render failed (undefined variable): {e}") from e
