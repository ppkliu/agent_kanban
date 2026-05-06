"""Symphony MVP — a Python implementation of the openai/symphony spec
that swaps Codex App-Server for Claude (CLI / API) and supports local LLMs
via OpenAI-compatible endpoints.

Public surface:
  * load_workflow / Workflow / WorkflowConfig
  * Tracker protocol, InMemoryTracker, GitHubIssuesTracker
  * WorkspaceManager
  * AgentRunner protocol, EchoRunner, ClaudeCLIRunner, AnthropicAPIRunner
  * Orchestrator
  * Issue / RunAttempt / RunState models
"""
from .agent_runner import (
    AgentEvent,
    AgentEventKind,
    AgentRunner,
    AnthropicAPIRunner,
    ClaudeCLIRunner,
    EchoRunner,
    ToolDispatcher,
    build_runner,
)
from .models import Issue, RunAttempt, RunState, TerminalReason, Workspace
from .orchestrator import Orchestrator
from .tracker import GitHubIssuesTracker, InMemoryTracker, Tracker, build_tracker
from .workflow import Workflow, WorkflowConfig, load_workflow, render_prompt
from .workspace import WorkspaceManager, sanitize_key

__version__ = "0.1.0"

__all__ = [
    "AgentEvent",
    "AgentEventKind",
    "AgentRunner",
    "AnthropicAPIRunner",
    "ClaudeCLIRunner",
    "EchoRunner",
    "GitHubIssuesTracker",
    "InMemoryTracker",
    "Issue",
    "Orchestrator",
    "RunAttempt",
    "RunState",
    "TerminalReason",
    "ToolDispatcher",
    "Tracker",
    "Workflow",
    "WorkflowConfig",
    "Workspace",
    "WorkspaceManager",
    "build_runner",
    "build_tracker",
    "load_workflow",
    "render_prompt",
    "sanitize_key",
]
