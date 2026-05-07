# Docker Quickstart — Symphony Dashboard

> 中文版: [docker-quickstart.zh-TW.md](docker-quickstart.zh-TW.md)

This guide gets you from `git clone` to a working dashboard at
**http://localhost:17957** in a few minutes.

The shipped scaffolding (`Dockerfile` + `docker-compose.yml` at the repo root)
is **single-container** — dashboard + bundled `opencode` CLI in one image,
with the workspace mounted as a volume. The originally documented
two-container blast-radius split (separate `symphony-opencode` service) is
tracked as a follow-up in [`post-mvp-gaps.md`](../todolist/post-mvp-gaps.md);
it requires runner-side rework that's out of scope here.

---

## 1. Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Docker Engine | 24+ | Docker Desktop on macOS / Windows works; Linux native preferred |
| `docker compose` | v2 (built-in subcommand) | The `docker-compose` standalone is also accepted |
| A model endpoint | one of: Ollama, vLLM, SGLang, LM Studio, LiteLLM proxy, or a cloud key | Default is Ollama on `host.docker.internal:11434` |
| Free TCP port | 17957 (host) | Change in `docker-compose.yml` if it clashes |

The dashboard container itself does **not** need network access to the
internet. Only the opencode container reaches out, and only to the model
endpoint you point it at.

## 2. Bring it up

```bash
git clone <this-repo>
cd agent_kanban

# Provide a WORKFLOW.md the container can mount (the example uses the
# in-memory tracker + opencode + ollama provider — edit if you need otherwise)
cp examples/WORKFLOW.docker.md WORKFLOW.md

# (optional but recommended) provide env overrides + a bearer token
cp .env.example .env
echo "DASHBOARD_API_KEY=$(openssl rand -hex 32)" >> .env

docker compose up -d
docker compose ps          # dashboard should be "Up (health: starting → healthy)"
docker compose logs -f dashboard
```

Open `http://localhost:17957` — you should see the 5-column kanban.

Quick sanity checks:

```bash
curl -fsS http://localhost:17957/healthz                  # → {"ok": true}
docker compose exec dashboard which opencode              # → /usr/local/bin/opencode
docker compose exec dashboard symphony-mvp /app/WORKFLOW.md --validate
```

## 3. Configuration

All knobs are environment variables on the compose services. The most common:

| Variable | Default | What it does |
|---|---|---|
| `DASHBOARD_API_KEY` | _(empty = open mode)_ | Bearer token required on every REST + WebSocket request when set |
| `OPENCODE_PROVIDER` | `ollama` | LiteLLM provider id — `ollama` / `openai` / `anthropic` / `vllm` / etc. |
| `OPENCODE_BASE_URL` | `http://host.docker.internal:11434` | Where opencode reaches the model |
| `OPENCODE_MODEL` | _(set via WORKFLOW.md)_ | Model name (`qwen2.5-coder:32b`, `gpt-4o`, ...) |
| `OPENCODE_API_KEY` | _(empty)_ | Required for cloud providers (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) |

Put them in `.env` next to `docker-compose.yml`; compose picks them up
automatically.

### Volumes (what is persisted on the host)

| Mount | Purpose |
|---|---|
| `./data:/app/data` | `dashboard.db` — events, hints, priority overrides, attempt history |
| `./WORKFLOW.md:/app/WORKFLOW.md:ro` | Live-reloadable orchestrator policy + prompt template |
| `workspaces` (named volume, shared) | Per-issue agent workspaces; visible to both containers |

To start over from scratch: `docker compose down -v` (drops the volume too).

## 4. Pick a runner via WORKFLOW.md

`WORKFLOW.md` is the single source of truth for orchestrator behaviour.
The `runner:` block selects the backend; the rest configures dispatch /
retry / handoff. Local LLM through opencode (the documented default):

```yaml
---
tracker:
  kind: memory                  # built-in local kanban (no SaaS)
agent:
  max_concurrent_agents: 2
  max_turns: 20
  retry_max_attempts: 3
  handoff_state: in_review
runner:
  kind: opencode
  provider: ollama
  model: qwen2.5-coder:32b
  allowed_tools: [bash, read, edit, write]
---
You are an autonomous engineer working on issue {{ issue.identifier }}.
{{ issue.title }}

{{ issue.description }}
```

Cloud fallback for hard tickets — change one line:

```yaml
runner:
  kind: opencode
  provider: anthropic            # OPENCODE_API_KEY must be set
  model: claude-opus-4-7
```

A complete annotated example lives in `examples/WORKFLOW.md`.

## 5. Production hardening

| Concern | Recommendation |
|---|---|
| TLS | Front the dashboard with Caddy / nginx / Traefik on the host; terminate TLS there |
| Auth | Always set `DASHBOARD_API_KEY` outside of single-user laptops |
| Backups | `docker compose stop dashboard && cp data/dashboard.db backup.db` (SQLite WAL is safe to copy hot) |
| Restarts | `restart: unless-stopped` is set in the compose file; orchestrator state recovers from tracker + filesystem on boot |
| Outbound network | Only the `opencode` container needs egress; firewall the `dashboard` container if your host policy allows |

## 6. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Browser shows nothing on :17957 | `docker compose ps` — is `dashboard` Up? Check `logs dashboard` for a port bind error |
| WebSocket disconnects every few seconds | Reverse proxy stripping `Upgrade` header — pass through `Connection: Upgrade` and `Upgrade: websocket` |
| `Bearer token` 401 errors | Confirm `.env` was loaded (`docker compose config` shows it); set the same key in the browser via the ⚙ button |
| `connection refused` from opencode container | Wrong `OPENCODE_BASE_URL`; on Linux `host.docker.internal` is not automatic — use the host's bridge IP or `--network host` |
| Agents stuck in `claimed` | Check `docker compose logs opencode`; provider auth is the most common cause |
| Want to wipe everything | `docker compose down -v && rm -rf data/ workspaces/` |

## 7. Going further

- Architecture, FSM, runner trade-offs: [`README.md`](../../README.md)
- Spec compliance + design rationale: [`docs/design/symphony-dashboard-spec.md`](../design/symphony-dashboard-spec.md)
- Adding a new agent backend: [`docs/design/agent-invocation-and-adapters.md`](../design/agent-invocation-and-adapters.md)
- Roadmap / known gaps: [`docs/todolist/post-mvp-gaps.md`](../todolist/post-mvp-gaps.md)
