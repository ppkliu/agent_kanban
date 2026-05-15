---
# Symphony MVP — Zero-LLM demo WORKFLOW.md
#
# Same defaults as examples/WORKFLOW.docker.md, but with the EchoRunner
# instead of opencode. Use this if you just want to see the kanban move —
# no Ollama / vLLM / Anthropic key required. Copy to ./WORKFLOW.md:
#
#     cp examples/WORKFLOW.demo-echo.md WORKFLOW.md
#     docker compose up -d
#
# Then submit a task and watch a card transition Unclaimed → Claimed →
# Running → Released in under five seconds. Once you've seen the motion,
# swap to examples/WORKFLOW.docker.md (or examples/WORKFLOW.md) for real
# coding work against opencode.

tracker:
  kind: memory                  # built-in local kanban — nothing leaves the host

polling:
  interval_ms: 1000             # tight loop so demo motion is visible

workspace:
  root: /app/workspaces

agent:
  max_concurrent_agents: 2
  max_turns: 3                  # echo only needs one turn
  stall_timeout_ms: 30000
  retry_initial_backoff_ms: 1000
  retry_max_backoff_ms: 5000
  retry_max_attempts: 1
  handoff_state: in_review

runner:
  kind: echo                    # deterministic, no LLM, writes a marker file

server:
  port: 7957
---
DEMO TASK — {{ issue.identifier }}: {{ issue.title }}

The echo runner ignores this prompt and writes a marker file to the
workspace before declaring done. Switch `runner.kind` to `opencode` and
restart to drive real coding work against a local LLM.
