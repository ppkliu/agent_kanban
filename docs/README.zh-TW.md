# Symphony MVP — Python 實作 + Dashboard

> [English README →](../README.md)

> 這是 [openai/symphony](https://github.com/openai/symphony) SPEC.md 的 Python 實作,圍繞三個原則設計:**Local-first**(資料與排程狀態都不離開本機)、**Autonomy-first**(Dashboard 是用來「觀察」長時間自主運行的 agent,不是用來逐步介入指揮的)、以及 **Docker 化部署**(一個 compose 檔同時帶起 dashboard 與沙箱化的 [opencode](https://github.com/sst/opencode) 容器來做代碼生成)。預設 tracker 是本專案自帶的 SQLite 看板,**沒有任何雲端或 SaaS 強制依賴**。本地 LLM 是主要運行目標;Claude Code CLI / Anthropic API / Codex 都是**次選**的 cloud backend。純 Python 3.10+,**205 個單元測試全綠**(MVP 核心 29 + 提示注入防護 6 + Dashboard 56 + Runner 11 + Tool API 32 + Phase B helpers 30 + mode 11 + metrics 8 + Linear 22),對齊 spec 的 React 觀察台,四種可替換 runner 都用 `WORKFLOW.md` 一行切換。

## 架構總覽

```
┌──────────────────────────────────────────────────────────────────┐
│                   symphony_mvp (Python package)                   │
│                                                                  │
│  WORKFLOW.md ──┐                                                 │
│   (in-repo)    │                                                 │
│                ▼                                                 │
│         ┌─────────────────┐                                      │
│         │ workflow.py     │  YAML front matter + Jinja2 (strict) │
│         │  - load_workflow│  + $ENV var expansion                │
│         │  - render_prompt│                                      │
│         └────────┬────────┘                                      │
│                  ▼                                               │
│         ┌─────────────────┐    ┌─────────────────────┐           │
│         │ orchestrator.py │◄──►│ tracker.py          │           │
│         │  - poll-tick    │    │   InMemoryTracker    │          │
│         │  - FSM          │    │     (= 本機 kanban)   │          │
│         │  - reconcile    │    │   GitHubIssuesTracker│          │
│         │  - dispatch     │    │     (選用 bridge)     │          │
│         │  - retry/backoff│    └─────────────────────┘           │
│         │  - bridge hooks │    ┌─────────────────────┐           │
│         │    (opt-in)     │    │ agent_runner.py      │          │
│         │                 │    │   OpenCodeRunner     │          │
│         │                 │    │     (本地 LLM via    │          │
│         │                 │    │      LiteLLM,主力)   │          │
│         │                 │    │   AnthropicAPIRunner │          │
│         │                 │    │     (vLLM/SGLang ok) │          │
│         │                 │    │   ClaudeCLIRunner    │          │
│         │                 │    │   EchoRunner (test)  │          │
│         └────────┬────────┘    └─────────────────────┘           │
│                  ▼                                               │
│         ┌─────────────────┐                                      │
│         │ workspace.py    │  三大不變式 + hooks                  │
│         └─────────────────┘                                      │
│                                                                  │
│  ─── opt-in dashboard,headless 模式可完全省略 ───                   │
│         ┌─────────────────────────────────────────┐              │
│         │ dashboard/   (= 本機 kanban UI)           │             │
│         │  bridge.py      SQLite: issues / events / │             │
│         │                 hints / priority / 歷程    │             │
│         │  server.py      FastAPI REST + WS + SPA   │             │
│         │  schema.sql                               │             │
│         └─────────────────────────────────────────┘              │
│                                                                  │
│  ─── 外部容器 (選用) — 受控,而非嵌入 ───                            │
│         ┌─────────────────────────────────────────┐              │
│         │ opencode container (sst/opencode)        │             │
│         │   provider-agnostic via LiteLLM           │             │
│         │   每個 attempt 由 OpenCodeRunner 拉起      │             │
│         └─────────────────────────────────────────┘              │
└──────────────────────────────────────────────────────────────────┘
```

**對應 SPEC.md 六層**:

| SPEC 層 | 模組 |
|---------|------|
| Policy + Configuration | `workflow.py` |
| Coordination (Orchestrator) | `orchestrator.py` |
| Tracker | `tracker.py` |
| Workspace | `workspace.py` |
| Agent Runner | `agent_runner.py` |
| Observability | `dashboard/` (FastAPI + WS) + `Orchestrator.state_snapshot()` |

## 安裝與快速開始

支援的部署目標是 **Docker Compose** — 一個檔案同時帶起 Dashboard 與沙箱化的
`opencode` 容器。完整步驟、配置參考、疑難排解都在:

- **[MVP 完成度 (中文,30 秒 audit)](MVP-STATUS.zh-TW.md)** · **[MVP completion status (English)](MVP-STATUS.md)**
- **[使用說明書 (中文) — 安裝 / 配置 / Tool API](guide/user-manual.zh-TW.md)** ← 想把 Symphony 接成 coding-service backend 看這份
- **[User manual (English)](guide/user-manual.md)**
- [API reference (中文)](API-REFERENCE.zh-TW.md) · [API reference (English)](API-REFERENCE.md) — `/api/v1/*` 分層 index 含 curl 範例
- [Docker 快速啟動指南 (中文,5 分鐘版)](guide/docker-quickstart.zh-TW.md)
- [Docker quickstart guide (English)](guide/docker-quickstart.md)

不需要 Docker、想直接開發 / CI:

```bash
uv venv
uv pip install -e ".[dev,dashboard]"

# 205 = MVP 29 + 提示注入 6 + Dashboard 56 + Runner 11 + Tool API 32 + Phase B helpers 30 + mode 11 + metrics 8 + Linear 22
.venv/bin/python -m pytest

# 純記憶體 demo,無外部依賴
.venv/bin/python examples/demo_echo.py
```

## Symphony Dashboard (autonomy 觀察台)

Dashboard 存在的理由:**讓 agent 可以連續跑數小時甚至數天而沒有人類在旁邊盯著**,而 operator 還是隨時可以瞄一眼 5 欄 Kanban 確認沒有偏離軌道。它是 opt-in 的 **FastAPI + React** 觀察介面 (對齊 [docs/design/symphony-dashboard-spec.md](design/symphony-dashboard-spec.md))。orchestrator 跟 FastAPI 跑在同程序、共用 in-memory 排程狀態,前端 build 後由 FastAPI `StaticFiles` 服務,**單一 port 解決**。所有持久化狀態都在一個本機 SQLite 檔 (`dashboard.db`),不會被送出本機。

### 啟動 (production)

```bash
# 1. 後端依賴 (含 fastapi + uvicorn + websockets)
uv pip install -e ".[dashboard]"

# 2. 前端 build 一次
cd frontend && npm install && npm run build && cd ..

# 3. 啟動 dashboard + orchestrator (同程序)
.venv/bin/python -m symphony_mvp.dashboard ./WORKFLOW.md \
    --port 7957 --db ./dashboard.db
# 開啟瀏覽器 → http://localhost:7957
```

### Dev mode (前端熱重載)

```bash
# Terminal 1: 後端
.venv/bin/python -m symphony_mvp.dashboard ./WORKFLOW.md --port 7957
# Terminal 2: 前端 dev server (Vite proxies /api → :7957)
cd frontend && npm run dev
# 開啟瀏覽器 → http://localhost:5173
```

### 功能對照

| 功能 | 對應 spec |
|------|----------|
| Kanban 5 欄 (Pending / Claimed / Running / Retry-Queued / Released) | §4.1 |
| Issue Drawer 6 tabs (Overview / Events / Prompt / Hints / Workspace / Replay) | §4.3 |
| Hint composer (operator 補上 prompt;下次 attempt 注入,tracker 仍唯讀) | §3.2, §5.3 |
| 拖拉重排 Pending (priority override,不寫 tracker) | §3.3, §5.6 |
| Pause / Resume / Abort / Force-retry 動作 | §5.4, §5.5 |
| **緊急全部停止 (Emergency Stop All)** — TopBar 紅色按鈕,經一次確認後一鍵 abort 所有運行中的 agent | §5.4 (extension) |
| **Coding Service Tool API** — `POST /api/v1/tools/*` (list_repos / inspect_repo / submit_coding_task / check_task_status / get_task_result / cancel_task),給上游 LLM agent 透過 function calling 呼叫;Phase B 完成 — 細粒度 stage 翻譯、結構化 TaskResult、per-task `mode` (plan / build / review) 硬性 whitelist | [設計文件](design/coding-service-tool-api.md) |
| Workflow editor (Monaco + last-known-good 熱載入) | §4.4, §5.8 |
| WebSocket 即時事件 + FSM transition + config_changed / workflow_reloaded | §5.9 |
| Bearer token 鑑權 (env `DASHBOARD_API_KEY` 或 `--api-key`) | §7 |

### Dashboard 哲學的紅線

Dashboard 的設計前提是「agent 自己在做事,operator 是觀察者而不是駕駛」。下面這些 HITL 鉤子是**例外狀況的逃生門**,不是日常工作流的一部分:

- 把 operator 想說的話**做為 prompt 附加** (Hint store)
- 把 operator 想要的順序**做為 dispatch 排序覆寫** (PriorityOverride store)
- 對 worker 的 cooperative pause/abort 訊號

跟其他 vibe-kanban / ai-agent-board 等專案最大的差別:**Dashboard 對 tracker 也唯讀**。所有跨頁簽到 (狀態轉換、PR link、留言) 都由 agent 透過 `gh` / MCP 工具自己寫回。從來沒有「修改 ticket 的按鈕」 — 這是設計而不是疏漏。

## 四種 Runner 切換 (這個 MVP 的核心價值)

切換是 **改 WORKFLOW.md 的 `runner.kind` 一行**,Orchestrator 完全不知道後端在做什麼。四種 Runner 都吐出相同的 `AgentEvent` stream,Orchestrator 用同一套 stall-detection / turn-counting / FSM 邏輯處理。**Local-first runner 排前面**,純雲端的排後面。

### 1. OpenCodeRunner — 透過 [sst/opencode](https://github.com/sst/opencode) 接本地 LLM (預設)

```yaml
runner:
  kind: opencode
  provider: vllm        # local first;也可以 "anthropic" / "openai" / ...
  model: qwen3.6:27b
  allowed_tools: [bash, read, edit, write]
```

包 `opencode` CLI 的 headless / streaming 模式。opencode 透過 LiteLLM
做到 provider-agnostic,所以**同一個 runner 改一個 YAML 欄位就能在
本地  vLLM  / Ollama/ SGLang / LM Studio 跟任何雲端 provider 之間切換**。
這是它被列為預設的理由 — 在你明確 opt-out 之前,所有東西都留在本機。

它產生的 6 種事件 (`session_start` / `assistant_text` / `tool_call` /
`tool_result` / `turn_complete` / `error`) 會被 `OpenCodeRunner._translate()`
1:1 對映到 `AgentEventKind`,完整對照在 `tests/test_agent_runner.py`。

### 2. AnthropicAPIRunner (本地 LLM / 完全擁有訊息序列)

```yaml
runner:
  kind: anthropic_api
  model: claude_model          # 任何 Anthropic-compatible endpoint
  max_tokens: 262144
```

```bash
ANTHROPIC_API_KEY=dummy \
ANTHROPIC_BASE_URL=http://localhost:58000 \
python -m symphony_mvp ./WORKFLOW.md
```

這個 Runner 直接呼叫 `/v1/messages`,跑自製的 tool loop。用它的時機:

1. **完整 messages 擁有權**:每個 turn 的 `messages` array 是你自己擁有的 — 可以記錄 / 重播 / 灌進其他管線
2. **支援本地 LLM**:任何 Anthropic-compatible endpoint 都能用 (vLLM / SGLang / LiteLLM proxy 把 OpenAI-format 轉 Anthropic-format)
3. **完整 prompt cache 控制**:你決定哪些 block 設 cache breakpoint
4. **領域特定工具**:你寫 `tools=[{"name":"your_query_tool",...}]` + `ToolDispatcher`,沒有 Bash/Edit 等預設工具

### 3. ClaudeCLIRunner — 包 `claude -p --output-format stream-json` (純雲端)

```yaml
runner:
  kind: claude_cli
  command: claude         # 預設值
  model: claude-opus-4-7
```

需要主機已安裝並登入 Claude Code CLI:
```bash
npm install -g @anthropic-ai/claude-code
claude login
```

啟動時 Symphony 會用以下參數呼叫:
```
claude -p "<rendered prompt>" \
  --output-format stream-json --verbose \
  --max-turns 20 \
  --allowedTools "Bash,Read,Edit,Write" \
  --permission-mode acceptEdits \
  [--resume <session_id>]    # 第二次以後的 attempt
```

**Codex App-Server JSON-RPC ↔ Claude CLI NDJSON 事件對照** (在 `_translate()` 方法,有單元測試):

| Codex App-Server | Claude CLI | Symphony 內部 |
|------------------|-----------|--------------|
| `turn/started` | (隱含於 stream 開始) | `TURN_STARTED` |
| `item/started` (system_init) | `{"type":"system","subtype":"init"}` | `ITEM_STARTED` |
| `item/agentMessage/delta` | `{"type":"assistant","message":{"content":[{"type":"text"}]}}` | `MESSAGE_DELTA` |
| `item/started` (commandExecution) | `{"type":"assistant", ...{"type":"tool_use"}}` | `TOOL_CALL` |
| `item/completed` (toolResult) | `{"type":"user", ...{"type":"tool_result"}}` | `TOOL_RESULT` |
| `turn/completed` | `{"type":"result","subtype":"success"}` | `TURN_COMPLETED` |
| (transport error) | `{"type":"result","subtype":"error_*"}` | `ERROR` |

### 4. EchoRunner — 確定性,不呼叫 LLM (純測試 / demo)

```yaml
runner:
  kind: echo
```

不呼叫任何 LLM,只在 workspace 寫一個 marker 檔。給單元測試和記憶體 demo 用。

## 實作的 SPEC 元素清單

| SPEC.md 段落 | 對應實作 | 狀態 |
|-------------|----------|------|
| §4.1 Issue normalized record | `models.Issue` (含全部 SPEC 欄位) | ✅ |
| §5 Policy/Config layer | `workflow.py` YAML + Jinja2 strict | ✅ |
| §5 環境變數轉介 (`$VAR`) | `_resolve_env()` | ✅ |
| §5 Hot reload (last-known-good) | Dashboard `PUT /workflow` 守住舊 config | ✅ |
| §6 Coordination FSM | `RunState` enum + `_dispatch`/`_release` | ✅ |
| §7 Workspace 三大不變式 | `workspace.py` (`sanitize_key`/`path_for`/`ensure`) | ✅ |
| §7 Hooks (after_create / before_run / after_run / before_remove) | `_run_hook` + fatal/non-fatal 區分 | ✅ |
| §8 Agent Runner 抽象 | `AgentRunner` Protocol + 4 impls | ✅ |
| §9 Tick 五步驟順序 | `Orchestrator.tick()` | ✅ |
| §9 Stall detection | `_reconcile` 用 `last_event_at` | ✅ |
| §9 Reconcile (external terminal/handoff) | `_reconcile` 拉 tracker 比對 | ✅ |
| §9 Priority sort + override | `candidates.sort(key=...)` + `bridge.get_priority_overrides()` | ✅ |
| §9 Bounded concurrency | `max_concurrent_agents` 檢查 | ✅ |
| §9 Exponential backoff retry | `_release` 算 `retry_after` | ✅ |
| §9 Blocked-by 檢查 | `_has_open_blockers` | ✅ |
| §9 Startup cleanup | `_startup_cleanup` | ✅ |
| §10 Tracker adapter (3 ops) | `Tracker` Protocol | ✅ |
| §10 Linear adapter | (留 stub,GitHub adapter 已實作) | ❌ MVP scope |
| §11 user_input_required = hard fail | `TerminalReason.USER_INPUT_REQUIRED` | ✅ |
| §13 HTTP server (`/api/v1/state`) | `dashboard.server` (FastAPI + WS + SPA) | ✅ |
| §13 Phoenix LiveView dashboard | (Elixir-only,Python MVP 改為 React + WS) | ✅ (替代品) |
| §14 `linear_graphql` 動態工具 | (用 GitHub 時不需要;留給日後) | ❌ MVP scope |

> **符合度評估**:核心執行語意 ~97%。預設 tracker 是本機 SQLite kanban (`InMemoryTracker`),GitHub Issues 是選用 bridge,API 形狀完全相同。Dashboard 直接覆蓋了原 spec 的 §13 觀察層。

## 三個沒有踩到的常見實作陷阱

對照從 SPEC 讀來的設計意圖,我刻意避免了以下三個容易踩錯的地方:

### 陷阱 1:把 tracker 寫死成某個 SaaS

我用 `Tracker` Protocol 抽象掉,讓 `InMemoryTracker` (預設,撐起本機 kanban) 跟 `GitHubIssuesTracker` (選用 bridge) 用同一介面。**SPEC 明說 tracker 該可插拔**,但官方 Elixir 實作目前只有 Linear 一個。預設用本機 kanban,系統才真的稱得上 local-first。

### 陷阱 2:Orchestrator 直接寫 tracker

SPEC 反覆強調:**Orchestrator 是唯讀**。所有 ticket 寫入 (狀態、留言、PR link) 都應該由 agent 自己用工具完成。我嚴格守住這一點 — `tracker.py` 沒有任何 `update_state` 或 `add_comment` 方法。Agent 用 `gh` CLI 或自訂 MCP 工具寫回。**Dashboard 也守同一條紅線。**

### 陷阱 3:把內部 FSM 跟 tracker 狀態混為一談

很多人會把這當同一件事。我用 `RunState`(內部排程狀態)跟字串 `state` 欄位(tracker 狀態)分開兩條軌道。**這是 SPEC 一個關鍵設計**,reconcile 邏輯靠這個分離才寫得乾淨。Dashboard 的 5 欄 Kanban 顯示的是 `RunState`,**不是** tracker 狀態。

## 接到自己領域

Symphony 設計就是領域中立 — 換的東西都很表層:

| Symphony 元素 | 你的替換 |
|---|---|
| `InMemoryTracker` (預設 = 本機 kanban) | 維持現狀就是 local-first;若需要橋接外部系統,實作 `Tracker` Protocol 三個方法 (`fetch_active` / `fetch_terminal` / `reconcile_states`) |
| `GitHubIssuesTracker` | GitHub-backed 專案直接用,或換成 Jira / Linear / 你自己的 adapter |
| `opencode` runner with `provider: ollama` | 需要雲端模型時,把 `provider:` 改成 `anthropic` / `openai` / 自家 LiteLLM endpoint |
| `agent.handoff_state="in_review"` | 任何代表「人類接手」的 tracker 狀態字串 |
| `WORKFLOW.md` 的 prompt body | 你的標準作業流程 / playbook,搭配 `AGENTS.md` 風格的角色界定 |
| `AnthropicAPIRunner.tools` | 你定義的工具 schema 陣列 |
| `ToolDispatcher.dispatch()` | 你的工具實作 + schema 驗證 + allow-list |

完整的 call chain、agent 呼叫流程、以及把 [opencode](https://github.com/sst/opencode) 接進來的範例,都在 [`docs/design/agent-invocation-and-adapters.md`](design/agent-invocation-and-adapters.md)。

### 接事件流

`Orchestrator.add_event_listener(fn)` 讓任意數量的訂閱者並行觀察 `AgentEvent` 串流。Dashboard 已經用了一個 listener 灌進 SQLite + 推 WebSocket — 你自己的 listener (記錄、metrics、archive 等) 可以並行掛上,互不干擾。

範例:

```python
def my_listener(issue_id: str, event: AgentEvent) -> None:
    redis_client.xadd(
        f"agent_trace:{issue_id}",
        {"kind": event.kind.value, "ts": event.timestamp.isoformat(),
         "data": json.dumps(event.data, default=str)},
    )

orch.add_event_listener(my_listener)
```

## 從 MVP 到生產的差距

從 README 抽出來的「未完成項目」(workspace 沙箱、prompt injection 防護、
雙容器爆炸半徑切分、tracing、多人 RBAC 等) 都搬到一份獨立
文件:**[docs/todolist/post-mvp-gaps.md](todolist/post-mvp-gaps.md)** (目前 1
高 / 3 中 / 3 低)。已落地:Docker scaffolding (Phase 1 單容器)、Coding
Service Tool API (Phase A + Phase B 含 per-task mode 硬性 whitelist +
Phase C idempotency)、持久化 retry queue、雙語反向代理 / TLS 部署章節、
GH Actions 三 job CI smoke test、中英雙語 `/api/v1/*` API reference、
Prometheus `/metrics` 暴露 (9 個零依賴 counter/gauge)、Phase C quota
(每分鐘 submit rate limit,超過回 429 + `Retry-After`)、Linear GraphQL
tracker adapter、Phase 1 提示注入防護 (自動 system-message preamble +
`<<<…BEGIN/END>>>` 包裝 untrusted issue / hint 欄位)。

## 檔案結構

```
agent_kanban/
├── .github/
│   └── workflows/
│       └── ci.yml             # pytest + frontend + docker-smoke 三個 job
├── pyproject.toml
├── requirements.txt
├── README.md                  # English (this file's parent)
├── docs/
│   ├── README.zh-TW.md        # 你正在看
│   ├── MVP-STATUS.md          # 30-second evaluator audit
│   ├── MVP-STATUS.zh-TW.md    # 中文 MVP 完成度
│   ├── API-REFERENCE.md       # /api/v1/* curated index (English)
│   ├── API-REFERENCE.zh-TW.md # 中文 API reference
│   ├── guide/
│   │   ├── user-manual.md             # English — install / configure / Tool API
│   │   ├── user-manual.zh-TW.md       # 中文使用說明書
│   │   ├── docker-quickstart.md       # English Docker quick path
│   │   └── docker-quickstart.zh-TW.md # 中文 Docker 快速啟動指南
│   ├── design/
│   │   ├── symphony-dashboard-spec.md
│   │   ├── agent-invocation-and-adapters.md
│   │   ├── opencode-two-container-analysis.md   # Phase 2 跨容器方案分析
│   │   ├── coding-service-tool-api.md           # Q3 對齊:對外 LLM agent 的 tool API 設計
│   │   └── prompt-injection-defense.md          # Prompt injection 5 種防護方案分析
│   └── todolist/
│       ├── symphony-dashboard-todolist.md   # P0–W6 sprint plan
│       └── post-mvp-gaps.md                 # production gaps & roadmap
├── symphony_mvp/
│   ├── __init__.py            # 公開 API
│   ├── __main__.py            # CLI entry: python -m symphony_mvp
│   ├── models.py              # Issue / RunAttempt / RunState / Workspace
│   ├── workflow.py            # WORKFLOW.md 解析 + Jinja2 strict render
│   ├── tracker.py             # Protocol + InMemory + GitHubIssues + Linear
│   ├── workspace.py           # 三大不變式 + hooks
│   ├── agent_runner.py        # OpenCode / AnthropicAPI / ClaudeCLI / Echo runners
│   ├── orchestrator.py        # Tick loop + FSM + reconcile + dispatch + bridge hooks
│   └── dashboard/             # opt-in HITL 觀察層
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
├── tests/                     # 205 tests
│   ├── conftest.py
│   ├── test_workflow.py       # 13 tests (7 + 6 prompt-injection framing)
│   ├── test_workspace.py      # 8 tests
│   ├── test_agent_runner.py   # 18 tests (15 + 3 allowed_tools override)
│   ├── test_orchestrator.py   # 7 tests
│   ├── test_dashboard_bridge.py        # 21 tests (12 + 5 idempotency + 4 submit_log)
│   ├── test_dashboard_orchestrator.py  # 6 tests
│   ├── test_dashboard_server.py        # 12 tests
│   ├── test_dashboard_extras.py        # 17 tests (13 + 4 emergency stop)
│   ├── test_tool_api.py                # 32 tests (Phase A/B + idempotency + rate limit + W3C trace_id)
│   ├── test_stage.py                   # 11 tests (Phase B stage translator)
│   ├── test_task_result.py             # 10 tests (Phase B result derivation)
│   ├── test_repo_inspect.py            # 9 tests  (Phase B inspect_repo helpers)
│   ├── test_mode.py                    # 11 tests (Phase B mode whitelist)
│   ├── test_metrics.py                 # 8 tests  (Prometheus /metrics endpoint)
│   └── test_linear_tracker.py          # 22 tests (Linear GraphQL adapter)
└── examples/
    ├── WORKFLOW.md            # 完整範例 (本機 kanban + opencode)
    ├── WORKFLOW.docker.md     # Docker 友善版預設 (memory + opencode)
    ├── demo_echo.py           # 端到端純記憶體 demo
    └── tool_api_client.py     # Tool API round-trip 範例 (純標準庫)
```

## 執行記錄

```bash
$ .venv/bin/python -m pytest
======================== 205 passed in 10.51s ========================

$ .venv/bin/python examples/demo_echo.py
... (正常完成,3 個 workspace 都建立並寫入 marker 檔)

$ .venv/bin/python -m symphony_mvp examples/WORKFLOW.md --validate
OK: examples/WORKFLOW.md is valid.

$ .venv/bin/python -m symphony_mvp.dashboard examples/WORKFLOW.md --port 7957
INFO:     Uvicorn running on http://127.0.0.1:7957
# 開啟瀏覽器看 5 欄 Kanban
```

## License

Apache-2.0,跟上游 openai/symphony 對齊。

## 下一步建議

如果你要把這個 MVP 推進你的領域,建議這個順序:

1. **第 1 週**:把記憶體 demo 跑起來,讀懂 `orchestrator.py` 的 `tick()` 五步驟,瀏覽 :7957 的本機 kanban
2. **第 2 週**:把 `OpenCodeRunner` 接到你本機跑的 vLLM / Ollama / SGLang,在單一主機上跑通一個真實任務
3. **第 3 週**:把 Docker scaffolding 落地,搬 dashboard + opencode 進容器,確認 dashboard 容器確實沒有 outbound 流量
4. **第 4 週**:當本地模型品質成為瓶頸,把 `provider: vllm` 換成 `provider: anthropic` (或 `openai`) **只用於困難 ticket**,其他繼續本機跑
5. **第 5-6 週**:若需要橋接 Jira / Linear / 自家系統,寫 `Tracker` adapter;加 OpenTelemetry;進入 staging
