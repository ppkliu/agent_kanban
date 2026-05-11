# Symphony MVP — User Manual

> 中文版: [user-manual.zh-TW.md](user-manual.zh-TW.md)

This is the front-door manual for **operating Symphony as a Coding Service**.
It walks you from a fresh checkout to driving real coding tasks from an
external LLM agent via the RESTful Tool API.

If you only want to bring up the dashboard and watch agents run, the
[Docker quickstart](docker-quickstart.md) is a 5-minute path. Come back here
when you're ready to integrate Symphony as a tool-callable backend or want
to understand all the moving parts.

---

## 0. The 90-second mental model

```
┌────────────────────────────────────────────────────────────────────┐
│  Your upstream LLM agent (Claude / GPT / your own)                 │
│   sees only 6 tools:  list_repos / inspect_repo / submit_coding_   │
│                       task / check_task_status / get_task_result / │
│                       cancel_task                                  │
└──────────────────────────────────┬─────────────────────────────────┘
                                   │ HTTP + JSON  (POST /api/v1/tools/*)
                                   ▼
┌────────────────────────────────────────────────────────────────────┐
│  Symphony Dashboard (this project)                                 │
│   • FastAPI + React kanban (your operator-facing surface)          │
│   • Orchestrator (FSM, dispatch, retry, abort)                     │
│   • SQLite for events / hints / priority / attempt history         │
└──────────────────────────────────┬─────────────────────────────────┘
                                   │ subprocess.Popen (Phase 1)
                                   ▼
┌────────────────────────────────────────────────────────────────────┐
│  opencode CLI (or Claude CLI / Anthropic API / Echo)               │
│   • the runner that actually edits files & runs tools              │
│   • talks to your local LLM (Ollama / vLLM) by default             │
└────────────────────────────────────────────────────────────────────┘
```

**Three layers, three contracts**:

1. **Upstream contract** — 6 fire-and-check tools (see §5). Your LLM agent
   only ever sees this.
2. **Operator contract** — kanban + drawer + emergency stop. Humans observe
   here; they don't drive task creation (that's the upstream's job).
3. **Runner contract** — `WORKFLOW.md` selects which CLI to spawn per
   attempt; the orchestrator stays runner-agnostic.

The autonomy promise: "agents can run for hours without a human babysitting
them." Operator hooks (hint composer, pause, abort) are escape hatches, not
the workflow.

---

## 1. Installation

### 1a. Docker (recommended)

```bash
git clone <this-repo>
cd agent_kanban

# Provide a WORKFLOW.md the container can mount
cp examples/WORKFLOW.docker.md WORKFLOW.md

# Optional but recommended: bearer token + endpoint config
cp .env.example .env
echo "DASHBOARD_API_KEY=$(openssl rand -hex 32)" >> .env

docker compose up -d
docker compose ps          # both services should be "Up"
docker compose logs -f dashboard
```

Open `http://localhost:17957` — you should see the 5-column kanban. The
dashboard container has **no outbound network policy**; only the runner
inside the container reaches a model endpoint, and only if you point it at
one (default: Ollama on the host).

### 1b. Native (development / CI)

```bash
uv venv
uv pip install -e ".[dev,dashboard]"

# Sanity: 147 tests should pass
.venv/bin/python -m pytest

# Self-contained demo (no Tool API, no LLM)
.venv/bin/python examples/demo_echo.py

# Full dashboard + Tool API
.venv/bin/python -m symphony_mvp.dashboard examples/WORKFLOW.docker.md \
    --port 7957
```

The native port is **7957**; the Docker host port is **17957**. All examples
in this manual use `SYMPHONY_URL` to flip between them.

---

## 2. Configuration

Configuration lives in **three places**, in this order of precedence:

| File / source | What it controls | Reload |
|---|---|---|
| `.env` (compose env) | container env vars: `DASHBOARD_API_KEY`, `OPENCODE_PROVIDER`, `OPENCODE_BASE_URL`, `OPENCODE_API_KEY` | restart compose |
| `WORKFLOW.md` (mounted ro) | tracker kind, runner kind/model/allowed_tools, polling interval, retry policy, prompt template | hot-reload via `PUT /api/v1/workflow` (last-known-good) |
| `examples/WORKFLOW.docker.md` | a sane bilingual default — copy to `WORKFLOW.md` then edit | reference only |

### 2.1 Choosing a runner

`WORKFLOW.md` `runner.kind` selects which CLI Symphony spawns per attempt:

| `runner.kind` | When to use | Setup needed |
|---|---|---|
| `echo` | smoke tests, demos, CI | none |
| **`opencode`** (default) | local LLM via [sst/opencode](https://github.com/sst/opencode) | bundled in the Docker image; `OPENCODE_PROVIDER` + `OPENCODE_BASE_URL` env |
| `anthropic_api` | direct `/v1/messages` (vLLM / Anthropic-compatible endpoints) | `ANTHROPIC_API_KEY` (or `dummy` against a local proxy) |
| `claude_cli` | Claude Code CLI on the host | `npm install -g @anthropic-ai/claude-code && claude login` |

You can swap runners without touching any other config. See
[`docs/design/agent-invocation-and-adapters.md`](../design/agent-invocation-and-adapters.md)
for the full adapter contract.

### 2.2 Choosing a tracker

`WORKFLOW.md` `tracker.kind`:

| Kind | Use when | Notes |
|---|---|---|
| **`memory`** (default) | Tool API consumers, single-host evaluation, demos | All state in `dashboard.db`; no external API |
| `github` | Bridging Symphony into a real repo's GitHub Issues | `tracker.repo` + `tracker.api_key` env-expanded |

**The Tool API in Phase A only supports `memory`** — submitting tasks via
`/api/v1/tools/submit_coding_task` against a `github` tracker returns 400.

### 2.3 Bearer auth

Set `DASHBOARD_API_KEY` (env or `--api-key`) and every REST + WebSocket call
needs `Authorization: Bearer <token>`. Leave it empty for single-user
laptops; **always set it** for anything else.

---

## 3. The dashboard (operator's view)

`http://localhost:17957` (Docker) or `:7957` (native) gives you:

| UI element | What it does |
|---|---|
| **5-column kanban** | Pending / Claimed / Running / Retry-Queued / Released |
| **Issue drawer** | per-issue Overview / Events / Prompt / Hints / Workspace / Replay tabs |
| **Hint composer** | append operator notes that get rendered into the next attempt's prompt |
| **Drag-to-reorder** | priority override (Pending column only — never writes the tracker) |
| **TopBar Stop All** | red kill-switch; aborts every active worker after a confirm |
| **Workflow editor** | live edit `WORKFLOW.md` with last-known-good fallback |
| **WebSocket feed** | live event stream + FSM transitions |

The dashboard is **read-only against the tracker** — it never edits an issue
state directly. State changes happen through the orchestrator FSM. The only
operator-driven mutations are: hint store, priority store, pause/abort
signals to running workers.

---

## 4. Operating in production

### 4.1 Persistent state

A single SQLite file (`dashboard.db`) on a host volume holds:

- `event_records` — agent event history for the Replay tab
- `hints` — operator-supplied prompt supplements
- `priority_overrides` — drag-to-reorder ranks
- `attempt_history` — finalised attempts (one row per RELEASED)
- `attempts_state` — in-flight attempts (auto-hydrated on orchestrator restart)

The persistent retry queue is what lets a long-running task survive a
process bounce: if the dashboard restarts mid-attempt, the next tick
detects stalled workers and re-enters retry logic with the same
`session_id` preserved.

Backup: `docker compose stop dashboard && cp data/dashboard.db backup.db`
(SQLite WAL is safe to copy hot).

### 4.2 Reverse-proxy / TLS

Front the dashboard with Caddy / nginx / Traefik on the host and terminate
TLS there. Symphony's container listens on plain HTTP at `:17957` so the
proxy stays in charge of certs, HSTS, and rate-limiting. The two
must-haves:

1. **Pass through `Connection: Upgrade` and `Upgrade: websocket`** — the
   live event feed at `/api/v1/events` is a real WebSocket and will
   disconnect every few seconds if these headers are stripped.
2. **Forward the `Authorization: Bearer …` header verbatim** — Symphony's
   bearer-token check looks at it directly. Most proxies do this by
   default but a stripped or rewritten config is the #1 cause of
   spurious 401s.

#### Caddy (recommended — TLS is automatic)

```caddy
# /etc/caddy/Caddyfile
symphony.example.com {
    encode gzip
    # Long-running WebSocket needs a generous read timeout
    reverse_proxy localhost:17957 {
        header_up Host {host}
        header_up X-Real-IP {remote_host}
        transport http {
            read_timeout 1h
        }
    }

    # Optional rate-limit (requires github.com/mholt/caddy-ratelimit plugin):
    # rate_limit {
    #     zone tool_api {
    #         key {remote_host}
    #         events 60
    #         window 1m
    #     }
    # }

    # Security headers
    header {
        Strict-Transport-Security "max-age=63072000; includeSubDomains; preload"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "no-referrer"
    }
}
```

Caddy fetches and renews TLS certs automatically via Let's Encrypt — no
extra config needed beyond pointing DNS at the host.

#### nginx

```nginx
# /etc/nginx/sites-available/symphony
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl http2;
    server_name symphony.example.com;

    ssl_certificate     /etc/letsencrypt/live/symphony.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/symphony.example.com/privkey.pem;
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;

    # Generous body / read timeouts for long-running agent tasks.
    client_max_body_size 16M;
    proxy_read_timeout   1h;
    proxy_send_timeout   1h;

    # Optional rate-limit on the Tool API specifically.
    # Add to http{}:  limit_req_zone $binary_remote_addr zone=tools:10m rate=60r/m;
    location /api/v1/tools/ {
        # limit_req zone=tools burst=20 nodelay;
        proxy_pass http://127.0.0.1:17957;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Authorization     $http_authorization;
    }

    # WebSocket — events feed at /api/v1/events MUST preserve Upgrade.
    location /api/v1/events {
        proxy_pass http://127.0.0.1:17957;
        proxy_http_version 1.1;
        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        $connection_upgrade;
        proxy_set_header Host              $host;
        proxy_read_timeout                 1d;
    }

    # Everything else (REST + SPA static assets).
    location / {
        proxy_pass http://127.0.0.1:17957;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Authorization     $http_authorization;
    }
}

server {
    listen 80;
    server_name symphony.example.com;
    return 301 https://$host$request_uri;
}
```

#### Traefik (labels-on-the-compose-service style)

If you want Traefik to discover the dashboard automatically, add labels to
the `dashboard` service in `docker-compose.yml`:

```yaml
services:
  dashboard:
    # ...existing config...
    labels:
      traefik.enable: "true"
      traefik.http.routers.symphony.rule: "Host(`symphony.example.com`)"
      traefik.http.routers.symphony.entrypoints: "websecure"
      traefik.http.routers.symphony.tls.certresolver: "letsencrypt"
      # Symphony listens on 7957 INSIDE the container; Traefik talks to
      # the container directly via the symphony network, so the host port
      # mapping (17957) is irrelevant here.
      traefik.http.services.symphony.loadbalancer.server.port: "7957"
      # WebSocket headers are automatic in Traefik 2+; no extra config
      # required for /api/v1/events.
```

#### Verifying the proxy

```bash
# 1. Plain healthz through the proxy
curl -fsS https://symphony.example.com/healthz
# expect: {"ok": true}

# 2. Tool API round-trip (uses bearer + Tool API path)
SYMPHONY_URL=https://symphony.example.com \
DASHBOARD_API_KEY=… \
  python examples/tool_api_client.py

# 3. WebSocket (events feed must NOT 426 / disconnect immediately)
curl -i -N \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: $(openssl rand -base64 16)" \
  -H "Authorization: Bearer $DASHBOARD_API_KEY" \
  https://symphony.example.com/api/v1/events
# expect first response line: HTTP/1.1 101 Switching Protocols
```

If step 3 returns anything other than `101 Switching Protocols` the
WebSocket upgrade is being stripped — re-check the `Connection` and
`Upgrade` rewriting in your proxy.

### 4.3 Observability

Today: structured logs (uvicorn) + Dashboard event log (in `dashboard.db`).
OpenTelemetry instrumentation is tracked as a Medium gap in
[`docs/todolist/post-mvp-gaps.md`](../todolist/post-mvp-gaps.md).

### 4.4 Emergency stop

If a runaway agent is doing damage, the TopBar's red **Stop All** button
calls `POST /api/v1/emergency_stop` which aborts every active worker after
a single browser confirm. Idempotent.

---

## 5. The Coding Service Tool API

This is the contract upstream LLM agents read against. Six endpoints,
fire-and-check semantics, deliberately small. All endpoints live under
`POST /api/v1/tools/*` and return JSON; auth is Bearer when configured.

### 5.1 `list_repos`

```bash
curl -X POST http://localhost:17957/api/v1/tools/list_repos \
  -H "content-type: application/json" -d '{}'
```

```json
{
  "repos": [
    {"id": "memory", "name": "symphony in-memory tracker",
     "default_mode": "build", "allowed_modes": ["plan","build","review"]}
  ]
}
```

### 5.2 `inspect_repo`

Reads the repo's `AGENTS.md` (or `CONTRIBUTING.md` / `README.md`
fallback), a tree summary, and the language breakdown. **Cheap — no
agent is spawned.** Call this before `submit_coding_task` for any
non-trivial work so your task description matches the project's
conventions.

```bash
curl -X POST http://localhost:17957/api/v1/tools/inspect_repo \
  -H "content-type: application/json" \
  -d '{"repo": "memory"}'
```

### 5.3 `submit_coding_task`

Returns a `task_id` in **under 1 second**. The agent runs on a background
worker; never wait synchronously.

```bash
curl -X POST http://localhost:17957/api/v1/tools/submit_coding_task \
  -H "content-type: application/json" -d '{
    "task": "In src/auth/login.ts, the rate limiter resets per request instead of per IP. Fix the logic and add a regression test.",
    "repo": "memory",
    "files_hint": ["src/auth/login.ts"],
    "mode": "build"
  }'
# → {"task_id": "tsk_abc123", "status": "pending"}
```

**`mode` is a hard tool whitelist** (per Q3 §7.4.2) — not a prompt
suggestion. The runner spawned for this attempt sees only the tools
allowed for the chosen mode:

| `mode` | Runner `allowed_tools` | When to use |
|---|---|---|
| `plan` | `bash`, `read`, `grep` | exploration / write a plan / no edits |
| `build` (default) | `bash`, `read`, `edit`, `write` | full implementation |
| `review` | `read`, `grep` | pure read-only audit |

Omit `mode` to use the runner's configured default (= `build`).
Symphony enforces this by rebuilding the runner's argv per attempt with
`--allowed-tools` set to the mode's whitelist; the agent literally
cannot call a tool outside the list.

**Writing good `task` text is the single biggest lever** for result quality:

```
### Bad
"fix the login bug"

### Good
"In src/auth/login.ts, the rate limiter at line ~45 calls reset() on every
 request instead of every IP. The intended behavior is per-IP throttling.
 Fix the logic and add a regression test."
```

The agent does NOT see your prior conversation context — it only sees the
single `task` string + `files_hint`. State dependencies inline.

### 5.4 `check_task_status`

Cheap to call; the response includes `next_poll_after_ms` — respect it.

```bash
curl -X POST http://localhost:17957/api/v1/tools/check_task_status \
  -H "content-type: application/json" \
  -d '{"task_id": "tsk_abc123"}'
```

```json
{
  "task_id": "tsk_abc123",
  "status": "running",
  "stage": "running_tests",
  "progress": 0.6,
  "current_turn": 12, "max_turns": 20,
  "started_at": "2026-05-09T10:23:00Z",
  "duration_ms": 47800,
  "next_poll_after_ms": 2000
}
```

`stage` values your LLM can reason about:
`queued` → `exploring_codebase` → `modifying_files` → `running_tests` →
`summarising` → `done` / `failed` / `cancelled`.

### 5.5 `get_task_result`

Only callable when `status ∈ {done, failed, cancelled}`. Returns a
**structured summary** — not the full transcript. ~500 tokens at most, by
design.

```json
{
  "status": "done",
  "summary": "Fixed the rate limiter to throttle per IP. Added regression test in tests/auth/rate_limit.test.ts that exercises the multi-IP scenario.",
  "files_changed": [
    {"path": "src/auth/login.ts", "change": "modified"},
    {"path": "tests/auth/rate_limit.test.ts", "change": "added"}
  ],
  "tests_run": {"passed": 47, "failed": 0, "framework": "vitest"},
  "blockers": [],
  "follow_ups": ["Consider extracting the rate-limit window into config"],
  "detailed_url": "http://localhost:17957/issues/tsk_abc123",
  "cost_usd": 0.012
}
```

If you need the full transcript (the human kind, not the LLM kind), open
`detailed_url` in a browser — that's the operator dashboard.

### 5.6 `cancel_task`

```bash
curl -X POST http://localhost:17957/api/v1/tools/cancel_task \
  -H "content-type: application/json" \
  -d '{"task_id": "tsk_abc123", "reason": "user changed their mind"}'
# → {"ok": true, "was_running": true, "task_id": "tsk_abc123"}
```

Idempotent. `was_running: false` means it had already completed when the
cancel arrived.

### 5.7 Anti-patterns to avoid

These are the failure modes Symphony's design specifically rules out — but
they can creep back in via your upstream agent's prompt-engineering:

- **Don't `submit_coding_task` then poll synchronously in a tight loop.**
  Poll with backoff (`next_poll_after_ms` is your friend).
- **Don't pass the operator's full conversation as `task`.** Distill it.
  The agent is starting from zero.
- **Don't inspect every repo before every task.** `inspect_repo` is cheap
  but its output is ~tokens-in-context-cost. Cache between tasks.
- **Don't ignore `blockers`.** If the agent reports it cannot do X without
  config Y, fix the config and re-submit — don't just retry blindly.

---

## 6. Working example: `examples/tool_api_client.py`

A pure-Python (stdlib only) client that exercises every Tool API endpoint:

```bash
# Terminal 1 — bring Symphony up
docker compose up -d
# (or for native: python -m symphony_mvp.dashboard examples/WORKFLOW.docker.md --port 7957)

# Terminal 2 — run the demo client
SYMPHONY_URL=http://localhost:17957 python examples/tool_api_client.py
```

You'll see live polling output: `status` and `stage` change over the
course of one round-trip, and the final `TaskResult` is printed. The
script is ~150 LOC, no third-party dependencies — read it as the canonical
"how do I call this from Python?" reference.

## 7. Integrating with an upstream LLM (Anthropic / OpenAI tool use)

The Tool API is shaped for direct consumption by tool-calling LLMs. A
minimal Anthropic example (pseudo-code; you'd run it in your own agent
loop):

```python
import anthropic
from examples.tool_api_client import (
    list_repos, inspect_repo, submit_coding_task,
    check_task_status, get_task_result, cancel_task,
)

TOOL_SCHEMAS = [
    {
      "name": "submit_coding_task",
      "description": "Delegate a coding task to the Coding Service. Returns a task_id immediately. Use check_task_status to poll.",
      "input_schema": {
        "type": "object",
        "required": ["task", "repo"],
        "properties": {
          "task": {"type": "string"},
          "repo": {"type": "string"},
          "files_hint": {"type": "array", "items": {"type": "string"}},
        }
      }
    },
    # ...same for the other 5 tools
]

def dispatch(name, args):
    return {
      "list_repos": lambda: {"repos": list_repos()},
      "inspect_repo": lambda: inspect_repo(args["repo"]),
      "submit_coding_task": lambda: {
          "task_id": submit_coding_task(
              args["task"], args["repo"], args.get("files_hint")
          )
      },
      "check_task_status": lambda: check_task_status(args["task_id"]),
      "get_task_result": lambda: get_task_result(args["task_id"]),
      "cancel_task": lambda: cancel_task(args["task_id"], args.get("reason")),
    }[name]()

client = anthropic.Anthropic()
# ...your agent loop calling client.messages.create(tools=TOOL_SCHEMAS, ...)
# and dispatch()ing on response.tool_use blocks
```

The tool descriptions you ship to your LLM matter as much as the schemas.
Symphony's auto-generated OpenAPI at `/openapi.json` includes the
description text from each endpoint's docstring — you can scrape them with
a one-liner if you want to keep your tool registrations in sync.

---

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Browser at `:17957` shows nothing | `docker compose ps` — is `dashboard` Up? `logs dashboard` for port-bind errors. |
| WebSocket disconnects every few seconds | Reverse proxy stripping `Upgrade` header. See §4.2. |
| `submit_coding_task` returns 400 "unknown repo" | Call `list_repos` first; the only valid `repo` for in-memory tracker is `"memory"`. |
| `submit_coding_task` returns 400 "tracker_kind='github'" | Phase A doesn't create real GitHub issues from the Tool API. Use `tracker.kind: memory` in WORKFLOW.md, or wait for Phase C multi-repo. |
| `get_task_result` returns 409 "no finalised attempt yet" | Status is still pending/running. Keep polling `check_task_status`. |
| Bearer token 401 on every call | `DASHBOARD_API_KEY` env not loaded into the container. `docker compose config` should show it. |
| Agent stuck in `claimed` for minutes | `docker compose logs dashboard` — opencode is most likely failing to reach the model endpoint. Check `OPENCODE_BASE_URL`. |
| Linux: `host.docker.internal` not resolving | The compose file's `extra_hosts: host-gateway` covers Docker Engine 20.10+. Older engines need a manual `OPENCODE_BASE_URL=http://172.17.0.1:11434`. |
| Want to wipe all state | `docker compose down -v && rm -rf data/ workspaces/`. |

---

## 9. Where to read more

| Topic | Link |
|---|---|
| **MVP completion audit (30-second evaluator view)** | [`MVP-STATUS.md`](../MVP-STATUS.md) |
| Repo overview + comparison to upstream | [`README.md`](../../README.md) |
| Docker quickstart (5-minute path) | [`docker-quickstart.md`](docker-quickstart.md) |
| Tool API design + Q3 alignment | [`docs/design/coding-service-tool-api.md`](../design/coding-service-tool-api.md) |
| All ways to control opencode | [`docs/design/opencode-two-container-analysis.md`](../design/opencode-two-container-analysis.md) |
| Adapter contract (writing a new runner) | [`docs/design/agent-invocation-and-adapters.md`](../design/agent-invocation-and-adapters.md) |
| Full SPEC + design rationale | [`docs/design/symphony-dashboard-spec.md`](../design/symphony-dashboard-spec.md) |
| What's still missing (post-MVP roadmap) | [`docs/todolist/post-mvp-gaps.md`](../todolist/post-mvp-gaps.md) |

---

## 10. MVP definition (what's "complete enough")

Treat the framework as **MVP-complete** if all of these are true (verified
2026-05-09 at commit `515ad66`):

- [x] `docker compose up -d` brings the dashboard up at `:17957`
- [x] `pytest` is green locally (**147 tests** as of 2026-05-09)
- [x] `python examples/tool_api_client.py` runs the full Tool API
      round-trip against a fresh stack
- [x] `examples/WORKFLOW.docker.md` is a runnable default that an
      LLM-driven `submit_coding_task` will pick up
- [x] Operator can hit Stop All in the browser to halt every running agent
- [x] Persistent retry queue: a process restart re-hydrates in-flight
      attempts from `attempts_state` SQLite
- [x] **Per-task `mode` (plan / build / review) enforces hard tool
      whitelists at the runner level** — `submit_coding_task` writes a
      `mode:*` label, orchestrator threads `allowed_tools` to
      `runner.run()`, OpenCodeRunner / ClaudeCLIRunner rebuild argv
      narrowed to the whitelist

For a separate **30-second evaluator audit** (commit-hash-backed evidence
per capability), see [`MVP-STATUS.md`](../MVP-STATUS.md).
Beyond MVP — sandboxing, multi-repo, quota, OpenTelemetry, Phase C
governance — are tracked in [`post-mvp-gaps.md`](../todolist/post-mvp-gaps.md).
