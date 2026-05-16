# Symphony API Reference

> English: [API-REFERENCE.md](API-REFERENCE.md)
> 即時 OpenAPI spec:**`GET /openapi.json`** (FastAPI 自動產生)
> 即時 API 探索器:**`GET /docs`** (Swagger UI,FastAPI 自動產生)

這份文件是 Symphony 所有 HTTP endpoint 的 curated index — 按受眾分類,
不是按 URL 字母排序。每個 endpoint 列出 method、路徑、所需認證、
一句話用途、加上一個 curl 範例。完整的 schema (request body、response
shape) 看 `<host>/openapi.json` 即可。

---

## 0. 慣例

- **Base URL**:`http://localhost:17957` (Docker host) 或 `http://localhost:7957` (native dev)。
- **認證**:server 端有設 `DASHBOARD_API_KEY` env 時,所有 REST + WebSocket 都要帶 `Authorization: Bearer <token>`。空 token = 開放模式,只用於本機開發。
- **Content-Type**:所有 POST/PUT/PATCH body 都是 `application/json`。
- **錯誤**:非 2xx 回 `{"detail": "..."}` (FastAPI 預設);workflow 驗證失敗特別回 `{"detail": "...", "kept_previous": true}`。

---

## 1. Coding Service Tool API (對外 — 給上游 LLM agent 用)

**受眾**:外部 LLM agent 透過 function-calling 把 Symphony 當 coding-service
backend。Fire-and-check 語意 — 絕對不要同步等 `submit_coding_task` 跑完。
完整整合說明見 [user-manual.zh-TW.md §5](guide/user-manual.zh-TW.md);
設計理由見 [coding-service-tool-api.md](design/coding-service-tool-api.md)。

| Method | 路徑 | 用途 |
|---|---|---|
| `POST` | `/api/v1/tools/list_repos` | 列出本服務可操作的 repo |
| `POST` | `/api/v1/tools/inspect_repo` | 讀 AGENTS.md + tree + 語言分布 (便宜;不起 agent) |
| `POST` | `/api/v1/tools/submit_coding_task` | 派發 coding task;1 秒內返回 `task_id` |
| `POST` | `/api/v1/tools/check_task_status` | 輪詢 status + 粗粒度 stage |
| `POST` | `/api/v1/tools/get_task_result` | 取已完成 task 的結構化結果 |
| `POST` | `/api/v1/tools/cancel_task` | 中止 running task (idempotent) |
| `POST` | `/api/v1/tools/list_tasks` | 列出 Tool API task;可選 `parent_task_id` + `status` 過濾 |

### 範例 round-trip

```bash
# 1. 列 repo
curl -X POST http://localhost:17957/api/v1/tools/list_repos \
  -H "content-type: application/json" -d '{}'
# → {"repos": [{"id": "memory", "name": "symphony in-memory tracker", ...}]}

# 2. 派發 task (立刻回傳)
curl -X POST http://localhost:17957/api/v1/tools/submit_coding_task \
  -H "content-type: application/json" -d '{
    "task": "加一個 /health endpoint,回傳 {status:\"ok\"}",
    "repo": "memory",
    "mode": "build",
    "files_hint": ["src/routes/"],
    "idempotency_key": "client-abc"
  }'
# → {"task_id": "tsk_e3f1...", "status": "pending", "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"}

# 3. 輪詢狀態
curl -X POST http://localhost:17957/api/v1/tools/check_task_status \
  -H "content-type: application/json" -d '{"task_id": "tsk_e3f1..."}'

# 4. 完成後取結構化結果
curl -X POST http://localhost:17957/api/v1/tools/get_task_result \
  -H "content-type: application/json" -d '{"task_id": "tsk_e3f1..."}'
```

完整可跑的端到端 demo (純標準函式庫):
[`examples/tool_api_client.py`](../examples/tool_api_client.py)。

### 重要欄位

- **`mode`** (`plan` / `build` / `review`):runner 層硬性工具 whitelist。
  `plan` = read+grep+bash;`build` = 全工具 (預設);`review` = read+grep only。
- **`idempotency_key`**:1–128 字元,Symphony 不解讀內容。同 key 1h 內回原
  `task_id`,不會建出第二個 task。
- **Rate limit** (選用,預設關閉):server 上設
  `SYMPHONY_SUBMIT_RATE_LIMIT_PER_MINUTE=N` 限制 submit 數。超過會回
  **`429 Too Many Requests`** 加 `Retry-After: 60`。Idempotency replay
  不算 (short-circuit 先發生)。
- **`trace_id`** (response,一定有):跟這個 task 綁定的 32-hex W3C
  trace_id。若 caller 帶 W3C `traceparent` HTTP header
  (`00-<32hex>-<16hex>-<2hex>`),Symphony 從中抽出 trace_id 部分原樣回傳;
  沒帶就現生一個。Idempotency replay 回原 task 的 trace_id,讓 log
  correlation 在重試之間維持指向同一個 task。
- **`parent_task_id`** + **`depends_on`** (request,選填):Phase D1
  subtask graph。`parent_task_id` 被 encode 成 `parent:<id>` issue label;
  若 parent attempt 以失敗 terminal_reason 結束 (ERROR / ABORTED /
  MAX_TURNS / STALL_TIMEOUT / USER_INPUT_REQUIRED),orchestrator 會把
  該 parent 之下所有 pending child 都 gate 起來,不再 dispatch。
  `depends_on` 是 sibling task_id 的 list,所有列出的 task 必須先進入
  terminal state,這個 task 才會被 dispatch,直接 map 到
  `Issue.blocked_by`,由既有的 dispatch gate 處理。
- **`list_tasks`**:回傳 flat list 的 Tool API task,依 `created_at`
  升冪排序。用 `parent_task_id` 走 child graph、用 `project_id` 限定
  單一專案、用 `status` 篩 (例如只看失敗或執行中)、`limit` 控制最多回幾
  筆 (預設 100,上限 500)。只讀,不會起 agent。
- **`project_id`** (Phase E1):把 task 綁到一個邏輯 project,讓 dashboard
  能 group / filter。會被 encode 成 `project:<id>` issue label。Child
  繼承 parent 的 project。沒帶會自動掛到 `default` project,既有單租
  caller 不受影響。Archived project 會被拒絕 (400)。
- **`subtasks`** (D2a Path A):一次 call 帶一 list 的 `SubTaskSpec`,
  Symphony 同時建立 **1 個 parent + N 個 child** issue (上限 50 個
  subtask)。每個 spec 帶 `task`、選填 `depends_on` (sibling 的 index,
  只能 forward reference;server 會 rewrite 成真正的 task_id)、選填
  `mode`、選填 `files_hint`。整批共用一個 `idempotency_key`,replay
  回原 parent id,不會重複建 child。整個子圖共用一個 `trace_id`,跨
  task 的 log correlation 維持指向同一個 trace。Path B (Symphony 自己
  跑 plan-mode runner 拆解) 仍在 roadmap (D2b),目前還沒上;暫時請
  上游 LLM 自己做拆解。
- **`status`** (in `check_task_status`):`pending` / `running` / `done` /
  `failed` / `cancelled`。
- **`stage`** (in `check_task_status`):`queued` / `exploring_codebase` /
  `modifying_files` / `running_tests` / `summarising` / `done` /
  `failed` / `cancelled` — 從 agent event stream 翻譯出來。

---

## 2. Dashboard / Operator API (對內 — 給 React SPA)

**受眾**:React kanban UI,以及想要 script 化 HITL 動作的 operator。
這層的 endpoint 多半對應 dashboard 上的一個 UI 控件。

### State + read

| Method | 路徑 | 用途 |
|---|---|---|
| `GET` | `/api/v1/state` | 5 欄 Kanban 的完整 state snapshot |
| `GET` | `/api/v1/issues/{issue_id}` | 單一 issue 詳細資料 (hints + rendered prompt preview + workspace path) |
| `GET` | `/api/v1/issues/{issue_id}/events` | 某 attempt 的歷史 agent 事件 (`?attempt_number=N&limit=M`) |
| `GET` | `/api/v1/issues/{issue_id}/attempts` | 某 issue 完整 attempt 歷程 |
| `GET` | `/api/v1/issues/{issue_id}/workspace` | 列 per-issue workspace 內的檔案 |
| `GET` | `/api/v1/issues/{issue_id}/workspace/file` | 預覽 workspace 內檔案前 ~200 行 (`?path=`) |
| `GET` | `/api/v1/workflow` | 當前解析過的 `WORKFLOW.md` 配置 |

### 單 issue 動作

| Method | 路徑 | 用途 |
|---|---|---|
| `POST` | `/api/v1/issues/{issue_id}/hint` | 把 operator 註記附加到下一個 attempt 的 prompt |
| `POST` | `/api/v1/issues/{issue_id}/pause` | 協同式暫停 running attempt |
| `POST` | `/api/v1/issues/{issue_id}/resume` | 恢復被暫停的 attempt |
| `POST` | `/api/v1/issues/{issue_id}/abort` | 硬中止;標 RELEASED + `aborted` reason |
| `POST` | `/api/v1/issues/{issue_id}/retry` | 重派一個曾經 RELEASED 的 issue |

### 全域動作

| Method | 路徑 | 用途 |
|---|---|---|
| `POST` | `/api/v1/emergency_stop` | TopBar 紅色 kill switch — 一鍵 abort 所有 active worker (idempotent) |
| `POST` | `/api/v1/priority` | 拖拉重排 Pending 欄 (priority override,不寫 tracker) |
| `PATCH` | `/api/v1/config` | 白名單 runtime config:`max_concurrent_agents`、`polling_interval_ms`、`runner_kind` / `runner_model` / `runner_provider` / `runner_base_url` / `runner_api_key` |
| `PUT` | `/api/v1/workflow` | 熱載入 `WORKFLOW.md` (parse 失敗回 422 + `kept_previous: true`) |

### Projects (Phase E1)

| Method | 路徑 | 用途 |
|---|---|---|
| `GET` | `/api/v1/projects` | 列 project。`?include_archived=true` 把封存的也一起回 |
| `POST` | `/api/v1/projects` | 建 project。Body:`{name, id?}` — 沒給 id 就 server 自動生 `proj_<hex>`;同 id 重複建是 idempotent |
| `PATCH` | `/api/v1/projects/{project_id}` | 改名 / 封存。Body:`{name?, archived?: bool}` |

Project 是多 project 架構的基礎:每個 Tool API task 都帶 `project:<id>`
label,dashboard kanban 可依 project 過濾,Tool API 的
`list_tasks ?project_id=` 走某 project 的 task graph。`submit_coding_task`
沒帶 `project_id` 會自動掛到 `default` project,讓既有單租 caller 不受影響。

### 即時事件 (WebSocket)

```
GET /api/v1/events?filter=issue:MT-1,MT-3
Upgrade: websocket
```

連線後 server 先推送每個非 terminal attempt 最近 100 筆事件,接著即時
推送 `agent_event` / `fsm_transition` / `config_changed` /
`workflow_reloaded`。

Filter 語法:`?filter=issue:<id>,...` 縮窄到特定 issue;省略則收全部。

---

## 3. 基礎建設 endpoint

| Method | 路徑 | 用途 | 認證 |
|---|---|---|---|
| `GET` | `/healthz` | Liveness 檢查;回 `{"ok": true}` | ❌ 不檢查 (刻意 — 讓 `docker compose` healthcheck 不用 token) |
| `GET` | `/metrics` | Prometheus 格式 — 9 個 metric (attempts / events / hints / idempotency 等),建議 scrape 間隔 15s | ❌ 不檢查 (Prometheus 慣例 — 用 reverse-proxy 擋) |
| `GET` | `/openapi.json` | OpenAPI 3.1 spec,FastAPI 自動產生 | ❌ 不檢查 |
| `GET` | `/docs` | Swagger UI (互動式 API 探索器) | ❌ 不檢查 |
| `GET` | `/` | React SPA 首頁 | 看 auth 設定 |
| `GET` | `/assets/*` | SPA 打包後的 JS/CSS 靜態檔 | ❌ 不檢查 |
| `GET` | `/{full_path}` | SPA fallback (沒匹配到的路徑 → `index.html`) | 看 auth 設定 |

---

## 4. 認證範例

```bash
# 1. 產生 token
TOKEN=$(openssl rand -hex 32)
echo "DASHBOARD_API_KEY=$TOKEN" >> .env
docker compose restart dashboard

# 2. REST 加 header
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:17957/api/v1/state

# 3. WebSocket — 多數 client 接受 header 或 query string;
# Symphony 在 upgrade request 上讀 header。
wscat -H "Authorization: Bearer $TOKEN" \
  -c ws://localhost:17957/api/v1/events
```

---

## 5. 延伸閱讀

| 主題 | 連結 |
|---|---|
| Tool API 整合手把手 | [user-manual.zh-TW.md §5](guide/user-manual.zh-TW.md) |
| Tool API 設計 + Q3 對齊 | [design/coding-service-tool-api.md](design/coding-service-tool-api.md) |
| 完整 SPEC + dashboard 設計理由 | [design/symphony-dashboard-spec.md](design/symphony-dashboard-spec.md) |
| MVP 完成度 audit | [MVP-STATUS.zh-TW.md](MVP-STATUS.zh-TW.md) |
