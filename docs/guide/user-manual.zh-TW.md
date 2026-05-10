# Symphony MVP — 使用說明書

> English: [user-manual.md](user-manual.md)

這是把 Symphony **當成 Coding Service 來操作**的正門文件。從 git clone
一路帶你到讓上游 LLM agent 透過 RESTful Tool API 真的派發 coding task。

如果你只想把 dashboard 拉起來看 agent 跑,
[Docker quickstart](docker-quickstart.zh-TW.md) 是 5 分鐘版本。
等你要把 Symphony 接成 tool-callable backend、或想搞懂底層元件再回這裡。

---

## 0. 90 秒心智模型

```
┌────────────────────────────────────────────────────────────────────┐
│  你的上游 LLM agent (Claude / GPT / 自家)                           │
│   只看到 6 個 tool:list_repos / inspect_repo / submit_coding_      │
│                    task / check_task_status / get_task_result /    │
│                    cancel_task                                     │
└──────────────────────────────────┬─────────────────────────────────┘
                                   │ HTTP + JSON  (POST /api/v1/tools/*)
                                   ▼
┌────────────────────────────────────────────────────────────────────┐
│  Symphony Dashboard (本專案)                                       │
│   • FastAPI + React kanban (operator 觀察介面)                      │
│   • Orchestrator (FSM / dispatch / retry / abort)                  │
│   • SQLite 存事件 / hint / priority / attempt history              │
└──────────────────────────────────┬─────────────────────────────────┘
                                   │ subprocess.Popen (Phase 1)
                                   ▼
┌────────────────────────────────────────────────────────────────────┐
│  opencode CLI (或 Claude CLI / Anthropic API / Echo)               │
│   • 真正改檔案、跑工具的 runner                                     │
│   • 預設連你本機的 LLM (Ollama / vLLM)                              │
└────────────────────────────────────────────────────────────────────┘
```

**三層、三份契約**:

1. **上游契約** — 6 個 fire-and-check tool (見 §5)。LLM agent 只看到這層。
2. **Operator 契約** — kanban + drawer + 緊急停止。人類觀察用,task
   是 LLM agent 派發的,不是人。
3. **Runner 契約** — `WORKFLOW.md` 選每次 attempt 要起哪個 CLI;
   orchestrator 對 runner 是 backend-agnostic。

Autonomy 承諾:「讓 agent 連續跑數小時不需人工盯。」Operator 端的 hint /
pause / abort 是逃生門,不是工作流的一部分。

---

## 1. 安裝

### 1a. Docker (建議)

```bash
git clone <this-repo>
cd agent_kanban

# 容器需要一個 WORKFLOW.md 可以掛入
cp examples/WORKFLOW.docker.md WORKFLOW.md

# 建議:設一個 bearer token + endpoint config
cp .env.example .env
echo "DASHBOARD_API_KEY=$(openssl rand -hex 32)" >> .env

docker compose up -d
docker compose ps          # 兩個 service 都應該是 "Up"
docker compose logs -f dashboard
```

開瀏覽器到 `http://localhost:17957`,看到 5 欄 Kanban 就成功。Dashboard
容器**沒有 outbound network policy**;只有容器內的 runner 會去連模型
endpoint,而且只有你明確指定它時才會。

### 1b. Native (開發 / CI)

```bash
uv venv
uv pip install -e ".[dev,dashboard]"

# Sanity:應該 147 tests 全綠
.venv/bin/python -m pytest

# 純記憶體 demo (不走 Tool API、不需 LLM)
.venv/bin/python examples/demo_echo.py

# 完整 dashboard + Tool API
.venv/bin/python -m symphony_mvp.dashboard examples/WORKFLOW.docker.md \
    --port 7957
```

Native 的 port 是 **7957**;Docker 對 host 的 port 是 **17957**。
本手冊所有範例都用 `SYMPHONY_URL` 變數切換。

---

## 2. 配置

配置散在 **三個地方**,優先序從上而下:

| 檔案 / 來源 | 控制什麼 | reload 方式 |
|---|---|---|
| `.env` (compose env) | 容器 env vars:`DASHBOARD_API_KEY` / `OPENCODE_PROVIDER` / `OPENCODE_BASE_URL` / `OPENCODE_API_KEY` | 重啟 compose |
| `WORKFLOW.md` (mount ro) | tracker kind / runner kind+model+allowed_tools / polling interval / retry policy / prompt template | 透過 `PUT /api/v1/workflow` 熱載入 (last-known-good) |
| `examples/WORKFLOW.docker.md` | 一份合理的中英雙語預設 — 複製到 `WORKFLOW.md` 後修 | 純參考 |

### 2.1 選 runner

`WORKFLOW.md` 裡的 `runner.kind`:

| `runner.kind` | 適用場景 | 需要哪些設定 |
|---|---|---|
| `echo` | smoke test / demo / CI | 無 |
| **`opencode`** (預設) | 用 [sst/opencode](https://github.com/sst/opencode) 接本地 LLM | Docker image 內建;`OPENCODE_PROVIDER` + `OPENCODE_BASE_URL` env |
| `anthropic_api` | 直接打 `/v1/messages` (vLLM / Anthropic-compatible endpoint) | `ANTHROPIC_API_KEY` (本地 proxy 用 `dummy` 也行) |
| `claude_cli` | host 上的 Claude Code CLI | `npm install -g @anthropic-ai/claude-code && claude login` |

切換 runner 不用動其他配置。完整的 adapter 契約見
[`docs/design/agent-invocation-and-adapters.md`](../design/agent-invocation-and-adapters.md)。

### 2.2 選 tracker

`WORKFLOW.md` `tracker.kind`:

| Kind | 何時用 | 備註 |
|---|---|---|
| **`memory`** (預設) | Tool API 消費者、單機評估、demo | 所有狀態都在 `dashboard.db`,沒有外部 API |
| `github` | 把 Symphony 接到實際 GitHub Issues | `tracker.repo` + `tracker.api_key` 環境變數展開 |

**Phase A 的 Tool API 只支援 `memory`** — 對 `github` tracker 呼叫
`/api/v1/tools/submit_coding_task` 會回 400。

### 2.3 Bearer 認證

設了 `DASHBOARD_API_KEY` (env 或 `--api-key`),所有 REST + WebSocket 都要
帶 `Authorization: Bearer <token>`。單機自用可以留空;**其他場合一律設**。

---

## 3. Dashboard (operator 視角)

`http://localhost:17957` (Docker) 或 `:7957` (native) 看到:

| UI 元件 | 作用 |
|---|---|
| **5 欄 Kanban** | Pending / Claimed / Running / Retry-Queued / Released |
| **Issue Drawer** | 每個 issue 的 Overview / Events / Prompt / Hints / Workspace / Replay |
| **Hint Composer** | 補 operator 註記,會被 render 到下一個 attempt 的 prompt |
| **拖拉重排** | priority override (僅 Pending 欄;不寫 tracker) |
| **TopBar Stop All** | 紅色 kill switch;一次確認後 abort 所有 active worker |
| **Workflow Editor** | 線上編 `WORKFLOW.md`,有 last-known-good 防護 |
| **WebSocket Feed** | 即時事件流 + FSM transition |

Dashboard 對 tracker 是**唯讀的** — 從來不直接改 issue 狀態。狀態變化都
經過 orchestrator FSM。Operator 唯一能改的是:hint store、priority store、
對 worker 的 pause/abort 訊號。

---

## 4. 生產環境操作

### 4.1 持久化狀態

單一 SQLite 檔 (`dashboard.db`) 在 host volume 上,存:

- `event_records` — agent 事件歷程,給 Replay tab 用
- `hints` — operator 補的 prompt 補充
- `priority_overrides` — 拖拉重排的 rank
- `attempt_history` — 已 finalised 的 attempt (每個 RELEASED 一筆)
- `attempts_state` — in-flight attempt (orchestrator 重啟時自動 hydrate)

持久化 retry queue 讓長任務能撐過 process bounce:dashboard 中途 restart,
下一個 tick 會偵測到 stale worker、走 retry 邏輯,session_id 也會保留。

備份:`docker compose stop dashboard && cp data/dashboard.db backup.db`
(SQLite WAL 熱備可行)。

### 4.2 反向代理 / TLS

在 host 上用 Caddy / nginx / Traefik 擋一層,在那邊終結 TLS。
**務必透傳 `Connection: Upgrade` 跟 `Upgrade: websocket`** — 不然事件流會斷。

```caddy
# Caddyfile 片段
example.com {
    reverse_proxy localhost:17957 {
        header_up Host {host}
    }
}
```

### 4.3 Observability

目前:結構化 log (uvicorn) + Dashboard event log (在 `dashboard.db` 裡)。
OpenTelemetry instrumentation 是
[`docs/todolist/post-mvp-gaps.md`](../todolist/post-mvp-gaps.md) 的 Medium gap。

### 4.4 緊急停止

agent 失控時,TopBar 紅色的 **Stop All** 會打 `POST /api/v1/emergency_stop`,
經過一次瀏覽器確認後 abort 所有 active worker。Idempotent。

---

## 5. Coding Service Tool API

這是上游 LLM agent 看到的契約。6 個端點、fire-and-check、刻意小。
所有端點都在 `POST /api/v1/tools/*`,回 JSON;有設 token 就帶 Bearer。

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

讀 repo 的 `AGENTS.md` (或 `CONTRIBUTING.md` / `README.md` fallback)、
tree summary、language breakdown。**便宜 — 不會起 agent。** 任何非小修
都建議先呼叫這個,讓 task description 對齊專案慣例。

```bash
curl -X POST http://localhost:17957/api/v1/tools/inspect_repo \
  -H "content-type: application/json" \
  -d '{"repo": "memory"}'
```

### 5.3 `submit_coding_task`

**1 秒內**回傳 `task_id`。Agent 在 background worker 跑;不要同步等。

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

**`mode` 是硬性的 tool whitelist** (對應 Q3 §7.4.2),不只是 prompt 提示。
這次 attempt 起的 runner 只會看到該 mode 允許的工具:

| `mode` | Runner `allowed_tools` | 何時用 |
|---|---|---|
| `plan` | `bash`, `read`, `grep` | 探索 / 寫計畫 / 不改檔 |
| `build` (預設) | `bash`, `read`, `edit`, `write` | 完整實作 |
| `review` | `read`, `grep` | 純唯讀 audit |

不傳 `mode` 就用 runner 自己的預設 (= `build`)。
Symphony 強制執行的方式是 — 每次 attempt 重建 runner argv,把
`--allowed-tools` 設成該 mode 的 whitelist,agent 連呼叫白名單外的
工具都辦不到。

**寫好 `task` 文字是結果品質最大的槓桿**:

```
### Bad
"修一下登入的 bug"

### Good
"In src/auth/login.ts 約第 45 行,rate limiter 是每次 request 都呼叫 reset(),
 不是每個 IP。預期行為是 per-IP throttling。修這個邏輯並補一個 regression test。"
```

Agent **看不到你之前的對話 context** — 只看到 `task` 字串 + `files_hint`。
所有 dependency 都要 inline 寫進去。

### 5.4 `check_task_status`

便宜可以重複打;response 帶 `next_poll_after_ms`,**請尊重它**。

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

`stage` 給 LLM reason 用:
`queued` → `exploring_codebase` → `modifying_files` → `running_tests` →
`summarising` → `done` / `failed` / `cancelled`。

### 5.5 `get_task_result`

只在 `status ∈ {done, failed, cancelled}` 時可用。回**結構化的摘要**,
不是整段 transcript。設計上控制在 ~500 tokens 內。

```json
{
  "status": "done",
  "summary": "修好 rate limiter,改成 per-IP throttling。在 tests/auth/rate_limit.test.ts 加了 regression test 驗多 IP 場景。",
  "files_changed": [
    {"path": "src/auth/login.ts", "change": "modified"},
    {"path": "tests/auth/rate_limit.test.ts", "change": "added"}
  ],
  "tests_run": {"passed": 47, "failed": 0, "framework": "vitest"},
  "blockers": [],
  "follow_ups": ["建議把 rate-limit window 抽成 config"],
  "detailed_url": "http://localhost:17957/issues/tsk_abc123",
  "cost_usd": 0.012
}
```

需要完整 transcript (給人看的版本) 就開 `detailed_url` — 那是 operator
dashboard。

### 5.6 `cancel_task`

```bash
curl -X POST http://localhost:17957/api/v1/tools/cancel_task \
  -H "content-type: application/json" \
  -d '{"task_id": "tsk_abc123", "reason": "user 改變主意"}'
# → {"ok": true, "was_running": true, "task_id": "tsk_abc123"}
```

Idempotent。`was_running: false` 表示打過來時 task 已經做完了。

### 5.7 反模式 (要避免)

這些是 Symphony 設計上特意排除的 failure mode,但你的上游 agent prompt
寫不好還是有可能踩進去:

- **不要 `submit_coding_task` 後緊湊 polling check_task_status。**
  用 backoff (`next_poll_after_ms` 是給你的)。
- **不要把 operator 的整段對話當 `task` 丟。** 蒸餾成自含的描述。
  Agent 從零開始,不知道你之前在聊什麼。
- **不要每次 task 都 inspect_repo。** inspect_repo 自己便宜,但它的
  output 在 LLM context 裡是 token 成本。在 task 之間 cache。
- **不要忽略 `blockers`。** 如果 agent 說「沒有 config Y 我做不了 X」,
  就把 config 補上重新 submit — 不要無腦重試。

---

## 6. 工作範例:`examples/tool_api_client.py`

純 Python (只用標準函式庫) 的 client,把所有 6 個 Tool API endpoint 都打過:

```bash
# Terminal 1 — 拉起 Symphony
docker compose up -d
# (或 native:python -m symphony_mvp.dashboard examples/WORKFLOW.docker.md --port 7957)

# Terminal 2 — 跑 demo client
SYMPHONY_URL=http://localhost:17957 python examples/tool_api_client.py
```

你會看到即時的 polling 輸出:`status` 跟 `stage` 隨 round-trip 變化、最後
印出 `TaskResult`。腳本約 150 行、沒有第三方依賴 — 把它當「我從 Python
怎麼接這個 API?」的範本來讀。

## 7. 接 LLM (Anthropic / OpenAI tool use)

Tool API 設計上就是給 tool-calling LLM 直接吃的。一個最小 Anthropic
範例 (pseudo-code,你自己的 agent loop 套上):

```python
import anthropic
from examples.tool_api_client import (
    list_repos, inspect_repo, submit_coding_task,
    check_task_status, get_task_result, cancel_task,
)

TOOL_SCHEMAS = [
    {
      "name": "submit_coding_task",
      "description": "把 coding task 派給 Coding Service。立刻返回 task_id;用 check_task_status polling。",
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
    # 其他 5 個 tool 同樣寫
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
# ...你的 agent loop 呼叫 client.messages.create(tools=TOOL_SCHEMAS, ...)
# 並在 response 的 tool_use block 上 dispatch()
```

你給 LLM 看的 tool description 跟 schema 一樣重要。Symphony 的
auto-generated OpenAPI 在 `/openapi.json`,description 會抄 endpoint
docstring — 想保持 tool 註冊跟服務同步,grep 一行就能拿出來。

---

## 8. 疑難排解

| 症狀 | 通常原因 / 解法 |
|---|---|
| 瀏覽器 `:17957` 看不到東西 | `docker compose ps` 看 `dashboard` 是不是 Up;`logs dashboard` 看 port bind |
| WebSocket 每幾秒就斷 | Reverse proxy 把 `Upgrade` header 吃掉了。見 §4.2 |
| `submit_coding_task` 回 400 "unknown repo" | 先呼叫 `list_repos`;in-memory tracker 唯一合法的 `repo` 是 `"memory"` |
| `submit_coding_task` 回 400 "tracker_kind='github'" | Phase A 不會從 Tool API 建真的 GitHub issue。把 WORKFLOW.md 改成 `tracker.kind: memory`,或等 Phase C 多 repo 支援 |
| `get_task_result` 回 409 "no finalised attempt yet" | task 還在 pending/running。繼續 polling `check_task_status` |
| 每次都被 401 擋 | `DASHBOARD_API_KEY` 沒進到容器。`docker compose config` 要看得到 |
| Agent 卡在 `claimed` 好幾分鐘 | `docker compose logs dashboard` — 99% 是 opencode 連不到 model endpoint。檢查 `OPENCODE_BASE_URL` |
| Linux 上 `host.docker.internal` 解不到 | compose file 的 `extra_hosts: host-gateway` 在 Docker Engine 20.10+ 可以;舊的要手動 `OPENCODE_BASE_URL=http://172.17.0.1:11434` |
| 想全部清乾淨 | `docker compose down -v && rm -rf data/ workspaces/` |

---

## 9. 延伸閱讀

| 主題 | 連結 |
|---|---|
| **MVP 完成度 audit (30 秒評估視角)** | [`MVP-STATUS.zh-TW.md`](../MVP-STATUS.zh-TW.md) |
| Repo 概觀 + 跟上游 symphony 比較 | [`docs/README.zh-TW.md`](../README.zh-TW.md) |
| Docker quickstart (5 分鐘版) | [`docker-quickstart.zh-TW.md`](docker-quickstart.zh-TW.md) |
| Tool API 設計 + Q3 對齊 | [`docs/design/coding-service-tool-api.md`](../design/coding-service-tool-api.md) |
| 控制 opencode 的所有方法 | [`docs/design/opencode-two-container-analysis.md`](../design/opencode-two-container-analysis.md) |
| Adapter 契約 (寫新 runner) | [`docs/design/agent-invocation-and-adapters.md`](../design/agent-invocation-and-adapters.md) |
| 完整 SPEC + 設計理由 | [`docs/design/symphony-dashboard-spec.md`](../design/symphony-dashboard-spec.md) |
| 還沒做的事 (post-MVP roadmap) | [`docs/todolist/post-mvp-gaps.md`](../todolist/post-mvp-gaps.md) |

---

## 10. MVP 完成的判準

當下面這些都成立,就算「MVP 完成」(2026-05-09 在 commit `515ad66` 驗過):

- [x] `docker compose up -d` 把 dashboard 帶起來在 `:17957`
- [x] `pytest` 在本機全綠 (**147 tests**,2026-05-09)
- [x] `python examples/tool_api_client.py` 能在新拉起的 stack 上跑完整
      Tool API round-trip
- [x] `examples/WORKFLOW.docker.md` 是可直接跑的預設,LLM 透過
      `submit_coding_task` 派的 task 會被 orchestrator 撿走
- [x] Operator 可以在瀏覽器按 Stop All 一鍵停掉所有 running agent
- [x] 持久化 retry queue:dashboard process 重啟後從 `attempts_state`
      SQLite 自動 hydrate in-flight attempts
- [x] **per-task `mode` (plan / build / review) 在 runner 層強制 tool
      whitelist** — `submit_coding_task` 寫 `mode:*` label,orchestrator
      把 `allowed_tools` 透傳給 `runner.run()`,OpenCodeRunner /
      ClaudeCLIRunner 重建 argv 把工具集縮到 whitelist

要看獨立的**30 秒評估 audit** (每個能力都附 commit hash 證據),見
[`MVP-STATUS.zh-TW.md`](../MVP-STATUS.zh-TW.md)。
MVP 之外 — sandbox 隔離、multi-repo、quota、OpenTelemetry、Phase C
governance — 都記在 [`post-mvp-gaps.md`](../todolist/post-mvp-gaps.md)。
