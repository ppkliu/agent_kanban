# Symphony MVP — Completion Status

> 中文版: [MVP-STATUS.zh-TW.md](MVP-STATUS.zh-TW.md)
> Audit date: **2026-05-11** · Tests: **156 passing** (rolling — re-stamp at each MVP-impacting commit)

A 30-second audit answering "is Symphony's MVP framework complete?"
For deeper docs see the [user manual](guide/user-manual.md), the
[`/api/v1/*` API reference](API-REFERENCE.md), or the running roadmap in
[post-mvp-gaps.md](todolist/post-mvp-gaps.md).

---

## 1. What this is

Symphony is a **local-first coding service** that lets external LLM agents
(Claude / GPT / your own) drive coding tasks via a small RESTful Tool API
(`POST /api/v1/tools/*`, fire-and-check). The orchestrator + dashboard +
runner stack is self-hostable via Docker and works against a local LLM
endpoint with no required cloud dependency.

The MVP framework's contract: **6 semantic tools to upstream agents,
opt-in dashboard for human observation, four pluggable runners selectable
via `WORKFLOW.md`**.

---

## 2. MVP completion checklist

| Capability | Status | Evidence (commit) | Verify yourself |
|---|---|---|---|
| Single-container Docker scaffolding | ✅ | `d6993d1` `1cd5141` | `docker compose up -d && curl http://localhost:17957/healthz` |
| Coding Service Tool API — Phase A (5 endpoints) | ✅ | `7f9707a..67b388c` | `python examples/tool_api_client.py` |
| Coding Service Tool API — Phase B (stage / result / inspect_repo / mode) | ✅ | `2fc84d7..344fa20`, `997a4aa..515ad66` | `pytest tests/test_tool_api.py tests/test_stage.py tests/test_task_result.py tests/test_repo_inspect.py tests/test_mode.py` |
| Per-task mode (plan / build / review) — hard tool whitelist at runner level | ✅ | `997a4aa..515ad66` | `pytest tests/test_mode.py tests/test_agent_runner.py::test_opencode_build_argv_uses_per_call_allowed_tools_override` |
| Persistent retry queue — process restart re-hydrates in-flight attempts | ✅ | `2fda864..6986240` | `pytest tests/test_dashboard_bridge.py -k attempt` |
| Phase C idempotency — safe retry of `submit_coding_task` via `idempotency_key` | ✅ | this batch | `pytest tests/test_dashboard_bridge.py -k idempotency` |
| Emergency stop-all (operator kill switch) | ✅ | `1c2a45a` `2448f65` | TopBar red button in browser, or `POST /api/v1/emergency_stop` |
| Four pluggable runners (echo / opencode / anthropic_api / claude_cli) | ✅ | `3b81799` `2ff85ba` | edit `WORKFLOW.md`'s `runner.kind` and restart |
| Bilingual user manual + Tool API client demo | ✅ | `2fa23f1` `09dbd8c` `3754e0d` | open [user-manual.md](guide/user-manual.md) |
| 5-column kanban + 6-tab issue drawer | ✅ | `2c9ef2f` and earlier | open `http://localhost:17957` |
| Self-contained test suite (no LLM / no GitHub required) | ✅ | 156 tests | `.venv/bin/python -m pytest` |
| Bearer-token auth on REST + WebSocket | ✅ | `dashboard/server.py:_require_auth` | `DASHBOARD_API_KEY=$(openssl rand -hex 32) docker compose up -d` |

**Net result**: every behavioural promise in the README and user manual
is backed by code, tests, or both — no "coming soon" loose ends.

---

## 3. What is intentionally out-of-MVP

These belong to **production hardening**, not MVP framework completeness.
Tracked in detail in [post-mvp-gaps.md](todolist/post-mvp-gaps.md).

| Gap | Severity | Why it's post-MVP |
|---|---|---|
| Workspace sandbox upgrades (firecracker / gVisor / rootless) | High | Single-container Phase 1 already provides container isolation; harder boundaries are an upgrade path |
| Prompt injection defence | High | Requires a design decision (token-level isolation vs system-message hardening); MVP runs trusted-tenant only |
| Two-container blast-radius split | Medium | OpenCodeRunner subprocess works today; HTTP-based runner is the [analyzed](design/opencode-two-container-analysis.md) follow-up |
| OpenTelemetry tracing / metrics | Medium | dashboard event log + structured uvicorn logs cover MVP audit needs |
| Multi-repo pool | Medium | Single-tracker MVP is the path of least surprise for first-time integrators |
| Multi-user RBAC | Low | Single-token all-or-nothing covers single-team self-host |
| `linear_graphql` dynamic tool | Low | only matters if you bridge to Linear (GitHubIssuesTracker doesn't need it) |
| Frontend i18n | Low | UI text is operator-facing only; the integrator-facing surface (Tool API + manual) is already bilingual |
| CI smoke test | Medium | manual verification script covers the path; GH Actions wiring is pending push to a remote |

---

## 4. How to verify yourself

```bash
# 1. Clone + sanity-check the test suite (no LLM, no Docker required)
git clone <this-repo> && cd agent_kanban
uv venv && uv pip install -e ".[dev,dashboard]"
.venv/bin/python -m pytest                  # expect: 156 passed

# 2. Bring the framework up
cp examples/WORKFLOW.docker.md WORKFLOW.md
docker compose up -d
curl -fsS http://localhost:17957/healthz    # expect: {"ok": true}

# 3. Drive it from external code (the canonical Tool API round-trip)
SYMPHONY_URL=http://localhost:17957 \
  python examples/tool_api_client.py

# 4. Watch the kanban update in real time
open http://localhost:17957
```

If any of those four steps fail on a clean checkout, file a bug — that's
a regression against the MVP completion line.

---

## 5. Version stamp

| | |
|---|---|
| Audit date | **2026-05-11** |
| Commit | rolling — see `git log` for latest checkpoint |
| Tests passing | **156** (`pytest -q`) |
| Frontend tests | **45** (`npm test`) |
| Docker image | `symphony-dashboard:dev` (single-container, Phase 1) |
| Tool API endpoints live | `list_repos / inspect_repo / submit_coding_task / check_task_status / get_task_result / cancel_task` |

> Re-run the audit after any commit that touches `symphony_mvp/`, `tests/`,
> `Dockerfile`, or `docker-compose.yml` so the checklist stays honest.
