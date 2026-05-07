# Symphony MVP — Python implementation + Dashboard

> [中文版 README →](docs/README.zh-TW.md)

> A Python implementation of the
> [openai/symphony](https://github.com/openai/symphony) `SPEC.md` built around
> three principles: **local-first** (data and orchestrator state never leave
> the host), **autonomy-first** (the dashboard observes long-running agents
> rather than micromanaging them), and **Docker-deployable** (one compose
> file brings up the dashboard plus a sandboxed [opencode](https://github.com/sst/opencode)
> container for code generation). The default tracker is the project's own
> SQLite-backed kanban — there is no required cloud or SaaS dependency. Local
> LLMs are the primary runtime target; Claude Code CLI, Anthropic API, and
> Codex are documented secondary backends. Pure Python 3.10+, **80 unit tests
> green** (29 MVP core + 43 dashboard + 8 opencode runner), spec-aligned React
> observatory, all four pluggable runners selectable from a single line of
> `WORKFLOW.md`.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                  symphony_mvp (Python package)                    │
│                                                                  │
│  WORKFLOW.md ──┐                                                 │
│   (in-repo)    │                                                 │
│                ▼                                                 │
│        ┌─────────────────┐                                       │
│        │ workflow.py     │  YAML front matter + Jinja2 (strict)  │
│        │  - load_workflow│  + $ENV var expansion                 │
│        │  - render_prompt│                                       │
│        └────────┬────────┘                                       │
│                 ▼                                                │
│        ┌─────────────────┐    ┌─────────────────────┐            │
│        │ orchestrator.py │◄──►│ tracker.py           │           │
│        │  - poll-tick    │    │   InMemoryTracker    │           │
│        │  - FSM          │    │     (= local kanban) │           │
│        │  - reconcile    │    │   GitHubIssuesTracker│           │
│        │  - dispatch     │    │     (optional bridge)│           │
│        │  - retry        │    └─────────────────────┘            │
│        │  - bridge hooks │    ┌─────────────────────┐            │
│        │    (opt-in)     │    │ agent_runner.py      │           │
│        │                 │    │   OpenCodeRunner     │           │
│        │                 │    │     (local LLM via   │           │
│        │                 │    │      LiteLLM, primary)│          │
│        │                 │    │   AnthropicAPIRunner │           │
│        │                 │    │     (vLLM/SGLang ok) │           │
│        │                 │    │   ClaudeCLIRunner    │           │
│        │                 │    │   EchoRunner (test)  │           │
│        └────────┬────────┘    └─────────────────────┘            │
│                 ▼                                                │
│        ┌─────────────────┐                                       │
│        │ workspace.py    │  three hard invariants + hooks        │
│        └─────────────────┘                                       │
│                                                                  │
│  ── opt-in dashboard (skip entirely for headless CI) ──          │
│        ┌──────────────────────────────────────────┐              │
│        │ dashboard/    (= the local kanban UI)     │             │
│        │   bridge.py    SQLite: issues / events /  │             │
│        │                hints / priority / history │             │
│        │   server.py    FastAPI REST + WS + SPA    │             │
│        │   schema.sql                              │             │
│        └──────────────────────────────────────────┘              │
│                                                                  │
│  ── external (optional) — controlled, not embedded ──            │
│        ┌──────────────────────────────────────────┐              │
│        │ opencode container  (sst/opencode)        │             │
│        │   provider-agnostic via LiteLLM           │             │
│        │   spawned per-attempt by OpenCodeRunner   │             │
│        └──────────────────────────────────────────┘              │
└──────────────────────────────────────────────────────────────────┘
```

Mapping to the SPEC's six layers:

| SPEC layer | Module |
|---|---|
| Policy + Configuration | `workflow.py` |
| Coordination (Orchestrator) | `orchestrator.py` |
| Tracker | `tracker.py` |
| Workspace | `workspace.py` |
| Agent Runner | `agent_runner.py` |
| Observability | `dashboard/` (FastAPI + WebSocket) + `Orchestrator.state_snapshot()` |

## Quick start

The supported deployment target is **Docker Compose** — one file brings up
the dashboard plus a sandboxed `opencode` container that talks to a local
LLM endpoint. Native Python install is provided for development and CI.

### Docker (recommended)

```bash
git clone <this-repo>
cd agent_kanban

# Brings up:
#   - symphony-dashboard  (FastAPI + React, host :17957 → container :7957)
#   - symphony-opencode   (sst/opencode, isolated workspace volume)
#   - SQLite on a named volume so events/hints/priority survive restarts
docker compose up -d

# Browse the kanban
open http://localhost:17957
```

Local LLM endpoint (Ollama / vLLM / SGLang / LiteLLM proxy) is pointed at
via `OPENCODE_BASE_URL` / `OPENCODE_PROVIDER` env vars on the
`symphony-opencode` service. The dashboard container itself never opens an
outbound connection to a cloud API; only the opencode container does, and
only if you point it at one. See [Docker deployment](#docker-deployment)
for the full compose example.

### Native (development / CI)

```bash
# 1. venv + deps (uv recommended)
uv venv
uv pip install -e ".[dev,dashboard]"

# 2. run tests (80 = 29 MVP core + 43 dashboard + 8 opencode runner)
.venv/bin/python -m pytest

# 3. self-contained end-to-end demo (no external API)
.venv/bin/python examples/demo_echo.py
```

> The Docker scaffolding (`Dockerfile`, `docker-compose.yml`) is the next
> milestone; the design is documented in [Docker deployment](#docker-deployment)
> below and in [`docs/design/symphony-dashboard-spec.md` §10](docs/design/symphony-dashboard-spec.md).

## Symphony Dashboard (autonomy observatory)

The dashboard exists so that **agents can run for hours or days without a
human babysitting them** — and the operator can still glance at the
5-column kanban to check that nothing has gone off the rails. It is an
opt-in **FastAPI + React** observation surface aligned with
[`docs/design/symphony-dashboard-spec.md`](docs/design/symphony-dashboard-spec.md).
The orchestrator and FastAPI run in the same process, sharing in-memory
scheduler state. The built frontend is served from the same port via
`StaticFiles` — one process, one port. All persistent state lives in a
single local SQLite file (`dashboard.db`); nothing is shipped off-host.

### Production

```bash
# 1. backend deps
uv pip install -e ".[dashboard]"

# 2. build frontend once
cd frontend && npm install && npm run build && cd ..

# 3. run dashboard + orchestrator (single process)
.venv/bin/python -m symphony_mvp.dashboard ./WORKFLOW.md \
    --port 7957 --db ./dashboard.db
# open http://localhost:7957
```

### Dev (frontend hot reload)

```bash
# Terminal 1: backend
.venv/bin/python -m symphony_mvp.dashboard ./WORKFLOW.md --port 7957
# Terminal 2: Vite dev server (proxies /api → :7957)
cd frontend && npm run dev
# open http://localhost:5173
```

### Feature map

| Feature | Spec section |
|---|---|
| 5-column kanban (Pending / Claimed / Running / Retry-Queued / Released) | §4.1 |
| Issue drawer with 6 tabs (Overview / Events / Prompt / Hints / Workspace / Replay) | §4.3 |
| Hint composer — operator appends prompt context for the next attempt; tracker stays read-only | §3.2, §5.3 |
| Drag-to-reorder Pending — priority override, never writes the tracker | §3.3, §5.6 |
| Pause / Resume / Abort / Force-retry actions | §5.4, §5.5 |
| **Emergency Stop All** — TopBar kill switch, aborts every active worker in one confirmation | §5.4 (extension) |
| Workflow editor — Monaco + last-known-good hot reload | §4.4, §5.8 |
| Live WebSocket events + FSM transitions + config_changed / workflow_reloaded | §5.9 |
| Bearer-token auth (env `DASHBOARD_API_KEY` or `--api-key`) | §7 |

### Dashboard's hard line

The dashboard is built on the assumption that the agent is doing the actual
work, autonomously, and the operator is **observing rather than driving**.
The HITL hooks below are escape hatches for the rare case where intervention
is necessary — not a workflow primitive:

- attaching operator notes that get **appended to the next prompt** (Hint store)
- expressing operator intent via **dispatch-order overrides** (PriorityOverride store)
- cooperative pause / abort signals to running workers

What separates this from `vibe-kanban`, `ai-agent-board`, and similar
projects: **the dashboard is read-only against the tracker as well.**
Cross-system writes (state transitions, PR links, comments) are still done
by the agent itself through `gh` / MCP tools. There is no "edit ticket"
button anywhere — by design.

## Docker deployment

The recommended deployment topology runs **two containers on a single host**,
talking through a private docker network:

```
                ┌───────────────────────────────────────┐
  :17957  ◄────►│  symphony-dashboard                   │
                │    FastAPI + React + SQLite           │
                │    volumes:                           │
                │      ./data:/app/data    (dashboard.db)│
                │      ./WORKFLOW.md:/app/WORKFLOW.md:ro │
                │      ./workspaces:/app/workspaces      │
                └────────────────┬──────────────────────┘
                                 │ exec / spawn opencode per attempt
                                 ▼
                ┌───────────────────────────────────────┐
                │  symphony-opencode                    │
                │    sst/opencode (LiteLLM-backed)      │
                │    OPENCODE_PROVIDER=ollama (default) │
                │    OPENCODE_BASE_URL=...              │
                │    workspace volume shared w/ above   │
                └───────────────────────────────────────┘
```

Why separate containers:

- **Blast radius** — opencode runs arbitrary tool calls (Bash, Edit, Write).
  Putting it in its own container means a compromised attempt can only see
  the workspace volume, not the dashboard's SQLite or WORKFLOW.md
- **Local-first** — the dashboard container has **no outbound network
  policy**. Only the opencode container talks to a model endpoint, and that
  endpoint defaults to `host.docker.internal:11434` (Ollama on the host)
- **Reproducibility** — the same compose file works on a laptop, an
  on-prem server, or an air-gapped machine with a sideloaded GGUF model

`docker-compose.yml` skeleton (full version coming with the Docker
scaffolding milestone):

```yaml
services:
  dashboard:
    build: .
    ports: ["17957:7957"]
    volumes:
      - ./data:/app/data
      - ./WORKFLOW.md:/app/WORKFLOW.md:ro
      - workspaces:/app/workspaces
    environment:
      DASHBOARD_API_KEY: ${DASHBOARD_API_KEY:-}
    networks: [symphony]
    restart: unless-stopped

  opencode:
    image: ghcr.io/sst/opencode:latest
    volumes:
      - workspaces:/workspaces
    environment:
      OPENCODE_PROVIDER: ${OPENCODE_PROVIDER:-ollama}
      OPENCODE_BASE_URL: ${OPENCODE_BASE_URL:-http://host.docker.internal:11434}
    networks: [symphony]
    restart: unless-stopped

volumes:
  workspaces:

networks:
  symphony:
    driver: bridge
```

`WORKFLOW.md` then selects the opencode runner with a local model:

```yaml
runner:
  kind: opencode
  provider: vllm        # local first; or "anthropic" / "openai" / ...
  model: qwen3.6:27b
  allowed_tools: [bash, read, edit, write]
```

The dashboard's `OpenCodeRunner` invokes the opencode CLI inside the
`symphony-opencode` container (via `docker exec` or compose-managed
ephemeral runs — implementation choice handled by the runner adapter).
Cloud providers (Anthropic, OpenAI) are still selectable by changing
`provider:` and supplying the corresponding API key — but they are **off
the default path**, not on it.

> Status: the Docker scaffolding (`Dockerfile` + `docker-compose.yml`) is
> the next deployment milestone. The runner-side code is already in place
> — see [`OpenCodeRunner`](symphony_mvp/agent_runner.py) and the design
> doc [`docs/design/agent-invocation-and-adapters.md` §3.2](docs/design/agent-invocation-and-adapters.md).

## Four pluggable runners — the core value of this MVP

Switching backends is **a single line in `WORKFLOW.md`'s `runner.kind`**. The
orchestrator never sees the difference: every runner emits the same
`AgentEvent` stream, so stall detection, turn counting, and FSM transitions
all share one code path. Local-first runners come first; cloud-only ones come
last.

### 1. OpenCodeRunner — local LLM via [sst/opencode](https://github.com/sst/opencode) (default)

```yaml
runner:
  kind: opencode
  provider: ollama         # local first; or "anthropic", "openai", ...
  model: qwen2.5-coder:32b
  allowed_tools: [bash, read, edit, write]
```

Wraps the `opencode` CLI in headless / streaming mode. opencode is
provider-agnostic via LiteLLM, so the **same runner switches between
vLLM / local Ollama / SGLang / LM Studio and any cloud provider just by
changing one YAML field**. This is why it's the documented default — it
keeps everything on the host until you explicitly opt out.

The 6 event kinds it produces (`session_start`, `assistant_text`,
`tool_call`, `tool_result`, `turn_complete`, `error`) translate 1:1 to
`AgentEventKind` via `OpenCodeRunner._translate()` — see
`tests/test_agent_runner.py` for the full mapping.

### 2. AnthropicAPIRunner — direct `/v1/messages` for local LLMs and full message control

```yaml
runner:
  kind: anthropic_api
  model: claude_model           # any Anthropic-compatible endpoint
  max_tokens: 262144
```

```bash
ANTHROPIC_API_KEY=dummy \
ANTHROPIC_BASE_URL=http://localhost:58000 \
python -m symphony_mvp ./WORKFLOW.md
```

Use this when you want:

1. **Full message ownership** — every turn's `messages` array is yours to keep, log, replay, or pipe elsewhere
2. **Local LLM compatibility** — any Anthropic-compatible endpoint works (vLLM, SGLang, LiteLLM proxy translating OpenAI → Anthropic)
3. **Full prompt-cache control** — you decide where to set cache breakpoints
4. **Domain tools** — you wire `tools=[{"name":"your_query_tool",...}]` and a `ToolDispatcher`; no Bash/Edit defaults

### 3. ClaudeCLIRunner — wraps `claude -p --output-format stream-json` (cloud)

```yaml
runner:
  kind: claude_cli
  model: claude-opus-4-7
```

Requires Claude Code CLI on the host:

```bash
npm install -g @anthropic-ai/claude-code
claude login
```

Symphony invokes:

```
claude -p "<rendered prompt>" \
  --output-format stream-json --verbose \
  --max-turns 20 \
  --allowedTools "Bash,Read,Edit,Write" \
  --permission-mode acceptEdits \
  [--resume <session_id>]
```

The Codex JSON-RPC ↔ Claude NDJSON event mapping (in `_translate()`, with unit tests):

| Codex App-Server | Claude CLI | Symphony internal |
|---|---|---|
| `turn/started` | (implicit on stream start) | `TURN_STARTED` |
| `item/started` (system_init) | `{"type":"system","subtype":"init"}` | `ITEM_STARTED` |
| `item/agentMessage/delta` | `{"type":"assistant", ...{"type":"text"}}` | `MESSAGE_DELTA` |
| `item/started` (commandExecution) | `{"type":"assistant", ...{"type":"tool_use"}}` | `TOOL_CALL` |
| `item/completed` (toolResult) | `{"type":"user", ...{"type":"tool_result"}}` | `TOOL_RESULT` |
| `turn/completed` | `{"type":"result","subtype":"success"}` | `TURN_COMPLETED` |
| (transport error) | `{"type":"result","subtype":"error_*"}` | `ERROR` |

### 4. EchoRunner — deterministic, no LLM (tests / demo only)

```yaml
runner:
  kind: echo
```

Writes a marker file and returns. Used by unit tests and the in-memory demo.

## SPEC compliance

| SPEC section | Implementation | Status |
|---|---|---|
| §4.1 normalized issue record | `models.Issue` (full SPEC field set) | ✅ |
| §5 policy/config layer | `workflow.py` YAML + Jinja2 strict | ✅ |
| §5 env var expansion (`$VAR`) | `_resolve_env()` | ✅ |
| §5 hot reload (last-known-good) | dashboard `PUT /workflow` keeps prior config | ✅ |
| §6 coordination FSM | `RunState` enum + `_dispatch`/`_release` | ✅ |
| §7 workspace invariants | `workspace.py` (`sanitize_key`/`path_for`/`ensure`) | ✅ |
| §7 hooks (after_create / before_run / after_run / before_remove) | `_run_hook` with fatal/non-fatal split | ✅ |
| §8 agent runner abstraction | `AgentRunner` Protocol + 4 impls | ✅ |
| §9 tick 5-step sequence | `Orchestrator.tick()` | ✅ |
| §9 stall detection | `_reconcile` via `last_event_at` | ✅ |
| §9 reconcile (external terminal/handoff) | `_reconcile` cross-checks tracker | ✅ |
| §9 priority sort + override | spec sort + `bridge.get_priority_overrides()` | ✅ |
| §9 bounded concurrency | `max_concurrent_agents` | ✅ |
| §9 exponential backoff retry | `_release` computes `retry_after` | ✅ |
| §9 blocked-by check | `_has_open_blockers` | ✅ |
| §9 startup cleanup | `_startup_cleanup` | ✅ |
| §10 tracker adapter (3 ops) | `Tracker` Protocol | ✅ |
| §10 Linear adapter | (stub; GitHub adapter implemented) | ❌ MVP scope |
| §11 user_input_required = hard fail | `TerminalReason.USER_INPUT_REQUIRED` | ✅ |
| §13 HTTP server (`/api/v1/state`) | `dashboard.server` (FastAPI + WS + SPA) | ✅ |
| §13 Phoenix LiveView dashboard | (Elixir-only; replaced with React + WS) | ✅ (substituted) |
| §14 `linear_graphql` dynamic tool | (not needed for GitHub; deferred) | ❌ MVP scope |

> **Compliance estimate:** ~97% on core execution semantics. The dashboard
> supersedes the original spec's §13 observability surface and adds HITL
> mechanisms on top.

## Three traps avoided

### Trap 1 — hard-coding the tracker to a SaaS

The `Tracker` Protocol abstracts this away — `InMemoryTracker` (the
default; backs the local kanban) and `GitHubIssuesTracker` (optional
bridge) share one interface. The SPEC says "tracker should be pluggable",
but the official Elixir implementation only ships Linear. Defaulting to
the local kanban is what makes the system genuinely local-first.

### Trap 2 — orchestrator writing back to the tracker

The SPEC is emphatic: **orchestrator is read-only**. All ticket writes
(state, comments, PR links) are done by the agent through tools. There is no
`update_state` or `add_comment` method anywhere in `tracker.py`. Agents use
`gh` CLI or custom MCP tools. **The dashboard holds the same line.**

### Trap 3 — conflating internal FSM with tracker state

The MVP keeps `RunState` (internal scheduler state) and the string `state`
field (tracker state) on two separate axes. **This is the SPEC's key design
point** — reconcile logic only stays clean when the two are kept apart. The
dashboard's 5-column kanban shows `RunState`, **not** tracker state.

## Adapting to your domain

Symphony is intentionally domain-agnostic. The substitutions are surgical:

| Symphony element | Your replacement |
|---|---|
| `InMemoryTracker` (default = local kanban) | Keep as-is for local-first, or implement `Tracker` Protocol's three methods (`fetch_active`, `fetch_terminal`, `reconcile_states`) against whatever issue/incident system you bridge to |
| `GitHubIssuesTracker` | Use as-is for GitHub-backed projects, or replace with a Jira/Linear/your-own adapter |
| `opencode` runner with `provider: ollama` | Swap `provider:` to `anthropic` / `openai` / your-own LiteLLM endpoint when you need a cloud model |
| `agent.handoff_state="in_review"` | Any tracker state name that means "human takes over" |
| WORKFLOW.md prompt body | Your standard operating procedure / playbook, with `AGENTS.md`-style role framing |
| `AnthropicAPIRunner.tools` | Your tool schema array |
| `ToolDispatcher.dispatch()` | Your tool implementation + schema validation + allow-list |

For the call chain, the agent invocation flow, and a worked example of plugging
in a new agent (with **OpenCodeRunner** as the walked example), see
[`docs/design/agent-invocation-and-adapters.md`](docs/design/agent-invocation-and-adapters.md).

### Tapping the event stream

`Orchestrator.add_event_listener(fn)` lets any number of subscribers observe
the live `AgentEvent` stream in parallel. The dashboard already uses one such
listener to write SQLite + push to WebSocket — your own listener (logging,
metrics, archive, etc.) can subscribe alongside without interference.

```python
def my_listener(issue_id: str, event: AgentEvent) -> None:
    redis_client.xadd(
        f"agent_trace:{issue_id}",
        {"kind": event.kind.value, "ts": event.timestamp.isoformat(),
         "data": json.dumps(event.data, default=str)},
    )

orch.add_event_listener(my_listener)
```

## Gap to production

Honest list of what's still missing:

| Gap | Severity | Fix |
|---|---|---|
| Docker scaffolding (`Dockerfile` + `docker-compose.yml`) not yet checked in | **High** (the documented deployment relies on it) | Land the two files; CI smoke test that compose up works |
| Workspace sandboxing (host OS only when running native; container-isolated under Docker) | **High** (critical for any privileged-tool scenario) | Once Docker scaffolding lands, the `symphony-opencode` container provides isolation; firecracker / gVisor are upgrade paths |
| In-memory retry queue lost on restart | Medium | Persist `_attempts` in SQLite WAL (dashboard SQLite already WAL) |
| Linear adapter is a stub | Low | Mirror GitHub adapter as GraphQL (~150 LOC) |
| `linear_graphql` dynamic tool | Low | Wrap as MCP server |
| Prompt injection defense | **High** | Token-level isolation post `render_prompt`, or system message hardening |
| Tracing / metrics (logs + dashboard event log only) | Medium | OpenTelemetry instrumentation |
| Permission policy simpler than Codex | Medium | Claude CLI's `--permission-mode` covers basics; opencode's `allowed_tools` covers the rest; add allow-list hook |
| Dashboard multi-user RBAC | Low–Medium | Currently single-token all-or-nothing (spec §7 leaves room to upgrade) |

## Layout

```
agent_kanban/
├── pyproject.toml
├── requirements.txt
├── README.md                  # English (this file)
├── docs/
│   ├── README.zh-TW.md        # 中文版 README
│   ├── design/
│   │   └── symphony-dashboard-spec.md
│   └── todolist/
│       └── symphony-dashboard-todolist.md
├── symphony_mvp/
│   ├── __init__.py            # public API
│   ├── __main__.py            # CLI: python -m symphony_mvp
│   ├── models.py              # Issue / RunAttempt / RunState / Workspace
│   ├── workflow.py            # WORKFLOW.md parser + Jinja2 strict render
│   ├── tracker.py             # Protocol + InMemory + GitHubIssues
│   ├── workspace.py           # invariants + hooks
│   ├── agent_runner.py        # OpenCode / AnthropicAPI / ClaudeCLI / Echo runners
│   ├── orchestrator.py        # tick + FSM + reconcile + dispatch + bridge hooks
│   └── dashboard/             # opt-in HITL layer
│       ├── __init__.py
│       ├── __main__.py        # python -m symphony_mvp.dashboard
│       ├── bridge.py          # SQLite store: events / hints / overrides / attempt history
│       ├── server.py          # FastAPI REST + WebSocket + SPA serve + bearer auth
│       └── schema.sql
├── frontend/                  # React 19 + TS + Tailwind 4 + Zustand
│   ├── package.json
│   ├── vite.config.ts
│   └── src/
│       ├── App.tsx
│       ├── store.ts
│       ├── api/               # types / REST client / WebSocket client
│       └── components/
│           ├── KanbanBoard.tsx
│           ├── IssueCard.tsx
│           ├── IssueDrawer.tsx
│           ├── drawer/        # Overview / Events / Prompt / Hints / Workspace / Replay
│           ├── ActivityFeed.tsx
│           ├── WorkflowEditor.tsx
│           ├── TopBar.tsx
│           └── FilterBar.tsx
├── tests/                     # 80 tests
│   ├── conftest.py
│   ├── test_workflow.py                # 7
│   ├── test_workspace.py               # 8
│   ├── test_agent_runner.py            # 15  (7 legacy + 8 opencode)
│   ├── test_orchestrator.py            # 7
│   ├── test_dashboard_bridge.py        # 8
│   ├── test_dashboard_orchestrator.py  # 6
│   ├── test_dashboard_server.py        # 12
│   └── test_dashboard_extras.py        # 17  (13 + 4 emergency stop)
└── examples/
    ├── WORKFLOW.md            # full example (local kanban + opencode)
    └── demo_echo.py           # in-memory end-to-end demo
```

## Run log
```bash
$ .venv/bin/python -m pytest
======================== 80 passed in 4.55s =========================

$ .venv/bin/python examples/demo_echo.py
... (3 workspaces created, marker files written)

$ .venv/bin/python -m symphony_mvp examples/WORKFLOW.md --validate
OK: examples/WORKFLOW.md is valid.

$ .venv/bin/python -m symphony_mvp.dashboard examples/WORKFLOW.md --port 7957
INFO:     Uvicorn running on http://127.0.0.1:7957
# open the browser, see the 5-column kanban
```

## License

Apache-2.0, aligned with upstream `openai/symphony`.

## Suggested rollout

1. **Week 1** — get the in-memory demo running, internalize the 5-step `tick()` in `orchestrator.py`, browse the local kanban at :7957
2. **Week 2** — point `OpenCodeRunner` at your local vLLM / Ollama / SGLang and rehearse a real task end-to-end on a single host
3. **Week 3** — land the Docker scaffolding, move dashboard + opencode into containers, verify the dashboard container has no outbound traffic
4. **Week 4** — when local model quality is the bottleneck, swap `provider: vllm` → `provider: anthropic` (or `openai`) for the hard tickets only; everything else keeps running locally
5. **Weeks 5–6** — write your own `Tracker` adapter if you need to bridge into Jira / Linear / your-own; add OpenTelemetry; promote to staging

---

> Detailed design rationale, the kanban-for-AI-agents comparison, and the full
> dashboard spec live in [`docs/design/symphony-dashboard-spec.md`](docs/design/symphony-dashboard-spec.md).
> The 中文版 of this README is at [`docs/README.zh-TW.md`](docs/README.zh-TW.md).
