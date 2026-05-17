# Post-MVP Gaps & Roadmap

> 對應的初期 sprint plan 在 [symphony-dashboard-todolist.md](symphony-dashboard-todolist.md)。
> 這份檔案專門記錄 README 當初 "Gap to production" 段落以及目前其他文件中提到的
> 未完成項目,讓 README 本體保持簡潔。

## 排優先序

| Severity | 數量 |
|----------|------|
| **High** | 1 |
| Medium   | 3 |
| Low      | 3 |

---

## High — 阻塞文件描述的部署路徑或安全紅線

- [x] **Docker scaffolding (Phase 1, 單容器)** — `Dockerfile` +
  `docker-compose.yml` + `.dockerignore` + `.env.example` +
  `examples/WORKFLOW.docker.md` 已落地
  - 驗證過:`docker compose build` / `up -d` / `/healthz` / SPA /
    `which opencode` / `down -v` 全部 OK
  - CI smoke test 仍是後續工作 (見下方 Medium 項目)
- [ ] **Workspace 沙箱**
  - Native 模式:只靠 host OS,任何高權限工具場景都關鍵 (privileged Bash / Edit / Write)
  - Docker 模式 Phase 1 (已落地):workspace 跟 SQLite 都在容器內,但 opencode
    跟 dashboard 共用同一個 fs namespace,所以隔離只到容器邊界
  - Docker 模式 Phase 2 (見下方 Medium):雙容器把 opencode 抽出去後,
    爆炸半徑會縮小到 workspace volume
  - 進階升級路徑:firecracker / gVisor / rootless Docker
- [x] **Prompt injection 防護 — Phase 1 完成**
  - **Stage 1 (分析):**
    [`docs/design/prompt-injection-defense.md`](../design/prompt-injection-defense.md)
    比較 5 種防護方案並推薦 (A) system-message preamble + (B) template
    framing 雙層作為 MVP Phase 1
  - **Stage 2 (實作,已落地):** `symphony_mvp/workflow.py` 新增
    `_FRAMING_PREAMBLE` (system message) + `_wrap_untrusted_fields()`
    (在 Jinja2 render 前把 `issue.title` / `issue.description` /
    `hint.content` 包進 `<<<ISSUE_DATA_BEGIN/END>>>` 與
    `<<<OPERATOR_HINT_BEGIN/END>>>` 邊界) + `_escape_boundary_tokens()`
    (defang 攻擊者塞的偽造邊界 token);6 個 unit tests 涵蓋 preamble、
    boundary 包裝、hint 包裝、攻擊 forge boundary 不會逃逸、helper 不
    mutate 入參、既有 substring assertions 兼容
  - Phase 2 (token-level isolation per-runner) 與 Phase C (LLM-judge /
    tool sanitisation) 留作後續

## Medium — MVP 可上線但長期會痛

- [ ] **Coding Service Tool API (Q3 對齊)** — 對外暴露 6 個語意化 tool
  (`list_repos` / `inspect_repo` / `submit_coding_task` / `check_task_status` /
  `get_task_result` / `cancel_task`),讓上游 LLM agent 透過 RESTful function-calling
  把 Symphony 當 coding backend
  - **Stage 1 (分析,已完成):** [`docs/design/coding-service-tool-api.md`](../design/coding-service-tool-api.md)
    把 Q3 (`Q3_OpenCode作為Tool的設計方案.md`) 對齊到 Symphony 既有 orchestrator + bridge,
    確認 ~90% infrastructure 已存在,只缺對外 RESTful 包裝層 + stage/result 兩個 helper module
  - **Phase A (已完成):** 5 個端點上線 (`list_repos` / `submit_coding_task` /
    `check_task_status` / `get_task_result` / `cancel_task`),掛在
    `/api/v1/tools/*`,新增 `symphony_mvp/dashboard/tool_api.py` + 11 個 pytest case
    (TestClient + EchoRunner round-trip);Phase A 跳過 inspect_repo / per-task mode / idempotency
  - **Phase B (已完成):** ✅ inspect_repo (`/api/v1/tools/inspect_repo` +
    `dashboard/repo_inspect.py`),✅ stage 翻譯 (`dashboard/stage.py`),
    ✅ result derivation 補 files_changed / tests_run / blockers / follow_ups
    (`dashboard/task_result.py`),✅ mode 隔離 per-task allowed_tools — 新增
    `dashboard/mode.py` (plan/build/review whitelist) + 擴充 AgentRunner Protocol
    accept-and-honor `allowed_tools=` kwarg + orchestrator 從 issue 的 `mode:*`
    label 讀模式並透傳給 runner
  - **Phase C (進行中):**
    ✅ idempotency (新增 `idempotency_keys` 表 + `bridge.lookup_idempotency_key` /
    `record_idempotency_key` / `clear_expired_idempotency_keys`,
    `submit_coding_task` 支援 `idempotency_key` 欄位,同 key 在 1h 內回原 task_id);
    ✅ quota — submit 全域 rate limit (新增 `submit_log` 表 +
    `bridge.record_submit` / `count_submits_since` / `clear_old_submits`,
    env var `SYMPHONY_SUBMIT_RATE_LIMIT_PER_MINUTE` 控制 N/分鐘上限,
    超過回 429 + `Retry-After: 60`;idempotency replay 不計入額度);
    ⏳ multi-repo / sandbox 三層防線
  - 跟 Docker Phase 2 完全正交,可平行做
- [ ] **Autonomous coding loop (Phase D — 5-phase roadmap)** — 對外暴露一條
  「下放高階目標 → 子任務拆解 → 依相依度排序 → 執行 → 回報 → 出現阻塞時
  escalate → 整體 summary」的完整 loop。詳細 plan 在
  `~/.claude/plans/w6-1-vitest-eager-meadow.md`。每個 D-phase 約 3–7 commit + 6–8 test。
  - **D1 (本批 commits,已落地)** — Subtask graph 基礎建設:`SubmitTaskIn`
    新增 `parent_task_id` + `depends_on` 兩欄;`_make_task_issue` 把
    parent_task_id 寫成 `parent:<id>` issue label、把 depends_on 直接灌進
    `Issue.blocked_by`;orchestrator `_has_open_blockers` 多 gate 一條:若
    parent attempt 以 ERROR / ABORTED / MAX_TURNS / STALL_TIMEOUT /
    USER_INPUT_REQUIRED 結束,所有 pending child 都凍結;新 endpoint
    `/api/v1/tools/list_tasks` 給上游 agent 走 child graph + 按 status 篩。
    自含 7 個新 test (2 orchestrator + 5 tool_api)
  - **D2a (本批 commits,已落地)** — Decomposition Path A:`SubmitTaskIn`
    新增 `subtasks: list[SubTaskSpec]` 欄位,handler 同時建 1 parent + N child
    (cap 50);sibling depends_on 用 forward-only sibling index 表達,server
    在 ingestion 時 rewrite 成真正的 task_id;整批共用 1 個 idempotency_key
    跟 1 個 trace_id。6 個新 test 覆蓋 parent+children linkage / sibling
    dep translation / trace_id inheritance / idempotency replay / invalid
    forward-ref / per-child mode label
  - **D2b (待開)** — Decomposition Path B (`decompose=true`):caller 只給
    一個 high-level goal,Symphony 自己跑一次 plan-mode runner 拆成
    subtasks。需要 orchestrator 加 post-terminal hook (parent 跑完才能
    parse output 建 child),所以從 D2 拆出來單獨做
  - **D3 (待開)** — Human-intervention escalation:新 `TerminalReason.NEEDS_HUMAN`
    + runner 解析 `[HUMAN_REQUIRED]` marker + `resolve_human_block` endpoint
  - **D4 (待開)** — Periodic CHECKPOINT events 給 mid-flight 進度回報
  - **D5 (待開)** — Cross-task rollup summary (`get_workflow_result` aggregator)
  - 「定時 self-trigger」這條已由現有 30s polling loop 覆蓋,只剩文件補上
- [ ] **Multi-project foundation (Phase E — 5-phase roadmap)** — 多 project /
  conversation 管理。詳細 plan 在
  `~/.claude/plans/w6-1-vitest-eager-meadow.md`。每個 E-phase 約 3–6 commit。
  - **E1 (本批 commits,已落地)** — Project schema 基礎建設:新 `projects`
    SQLite table (id / name / created_at / archived_at)、bridge 加
    create / get / list / rename / archive / unarchive + `ensure_default_project`;
    `submit_coding_task` 新增 `project_id` 欄位,encode 成 `project:<id>` issue
    label,沒帶就掛 `default` project,archived project 拒收 (400);subtask
    fan-out 繼承 parent 的 project_id;`list_tasks` 新增 `project_id` 過濾;
    新 REST 端點 `GET / POST /api/v1/projects`、`PATCH /api/v1/projects/{id}`。
    14 個新 test (4 bridge + 10 tool_api/REST)
  - **E2 (本批 commits,已落地)** — Frontend project selector:新
    `projectStore.ts` (Zustand,localStorage-persisted 選擇)、`api.listProjects` /
    `createProject` / `patchProject` helper、TopBar 左側 `ProjectSelector`
    下拉(All / 每個 project / + New project…)、KanbanBoard 依當前 project
    過濾(`project:<id>` label 比對,沒帶 label 的視為 default 以兼容舊資料);
    ChatPanel 送出時帶當前 project_id;App.tsx 在 mount 時 refresh project
    list。12 個新 frontend test(8 projectStore + 4 ProjectSelector)
  - **E3 (本批 commits,已落地)** — Per-project chat 歷史:新
    `chatHistory.ts` module(localStorage keyed by project_id,FIFO 20 條
    上限);ChatPanel 在送出後寫入當前 project 的歷史,idle 狀態顯示
    "Recent in this project" 清單,點 entry 預填 textarea;切 project 時
    ChatPanel 自動 reset prompt / preview / phase 並重讀新 project 的
    歷史。8 個新 frontend test (chatHistory 單元測試)
  - **E4 (本批 commits,已落地)** — Per-project event-trace view:後端
    WS `?filter=` 擴充支援 `project:<id>` 語法,looks up issue.labels →
    `project:` 比對,沒帶 label 的視為 `default`(legacy 相容);新
    `TracePanel.tsx`(左下浮動面板)由 `🔍 Trace` button 觸發,客戶端依
    snapshot 建立 issue→project 對應,過濾現有 store 的 `activity` ring,
    含 "All projects" 暫時繞過。6 個新 test(2 backend WS filter + 4
    frontend TracePanel)
  - **E5 (本批 commits,已落地)** — 跨 project audit + archive UI:
    ProjectSelector 每筆 active project 加 📦 archive 按鈕(`default`
    project 不可 archive,UI 直接擋掉);archived project 收進可摺疊
    "Archived (N)" 區段,展開後每筆有 ↩ unarchive;projectStore 預設
    一次撈全部(active + archived),UI 自己分組;IssueCard 在「All
    projects」模式顯示 📁 chip 標示所屬 project,讓跨 project 看板
    一眼分得出來。7 個新 frontend test (3 ProjectSelector archive +
    4 IssueCard chip)
  - 整個 Phase E roadmap 全綠;後續若還要 multi-tenant RBAC / 跨 project
    task 依賴等進階能力,另起 Phase F roadmap
- [ ] **Docker scaffolding Phase 2:雙容器爆炸半徑切分** — 把 `opencode` CLI
  從 dashboard 容器抽離到獨立的 `symphony-opencode` service
  - 動機:目前 dashboard 容器同時跑 FastAPI 跟 opencode 子行程,任意 tool call
    跟 SQLite / WORKFLOW.md 共享一個 fs namespace。把 opencode 隔出去可以縮小
    爆炸半徑。
  - **Stage 1 (分析,已完成):**
    [`docs/design/opencode-two-container-analysis.md`](../design/opencode-two-container-analysis.md)
    比較 5 種跨容器呼叫方案,推薦使用 opencode 自帶的 `serve` HTTP/SSE 介面
    (§4.3),沒有 docker socket 暴露成本。
  - **Stage 2 (實作,待開):** 等使用者讀完分析、確認方案後另開 plan:
    `OpenCodeRunner` 改 httpx + SSE、compose 加第二個 service、新增整合測試
  - 對應原本 user guide 描述的雙容器拓撲;Phase 1 已先落單容器版本,
    Stage 2 完成後 user guide 會切回雙容器圖
- [x] **CI smoke test** — `.github/workflows/ci.yml` 已落地
  - 三個並行 job:`pytest` (147 backend tests + Python 3.12) /
    `frontend` (typecheck + 45 vitest 在 Node 20) /
    `docker-smoke` (build image → `compose up -d` → 輪詢 `/healthz` →
    跑 `examples/tool_api_client.py` 整套 Tool API round-trip → `compose down -v`)
  - 失敗時自動 dump `docker compose logs dashboard` 給 diagnostics
  - 觸發條件:push to main / PR to main / 手動 workflow_dispatch
- [x] **持久化 retry queue** — 重啟後 in-flight attempts 自動 hydrate
  - 已落地:新增 `attempts_state` SQLite 表 +
    `bridge.persist_attempt` / `load_active_attempts` / `delete_attempt_state`,
    Orchestrator `__init__` 從 bridge hydrate `_attempts`,dispatch / release
    自動 persist / delete
  - 設計選擇:in-memory 為主、SQLite 為災難復原 backup;released 的 row 從
    `attempts_state` 刪掉,只在 `attempt_history` 留 finalised snapshot
  - 測試覆蓋 4 個 case (round-trip / idempotent / delete / empty);完整
    orchestrator restart-recovery 整合測試留作後續 (現有 dashboard tests
    已驗證 persist 不破壞 hot path)
- [ ] **Tracing / Metrics** (部分完成)
  - ✅ Prometheus `/metrics` 端點已落地 (`dashboard/metrics.py`,9 個 gauge/counter:
    `symphony_attempts{state=}` / `symphony_attempts_finalised_total{terminal_reason=}` /
    `symphony_events_total` / `symphony_events_by_kind_total{kind=}` / `symphony_hints_*` /
    `symphony_priority_overrides` / `symphony_attempts_state_rows` / `symphony_idempotency_keys`),
    純標準函式庫,沒新依賴;`/metrics` 故意不檢查 auth (Prometheus scrape convention)
  - ✅ W3C TraceContext trace_id 傳遞 (本批 commits):`submit_coding_task`
    讀 `traceparent` HTTP header,抽出 32-hex trace_id 寫成 `trace:<id>`
    issue label,response 回傳給上游 caller;沒帶 header 就現生 fresh id;
    idempotency replay 回原 task 的 trace_id,跨重試維持 log correlation;
    純標準函式庫 (`re` + `secrets`),沒新依賴
  - ⏳ OpenTelemetry span emission / context propagation 到 runner 子行程
    留作後續 (現在的 trace_id 已足以做 log correlation,但 span tree 還沒)
- [x] **Permission policy 比 Codex 簡化** — 跨 runner allow-list hook 已落地
  - Phase B 的 per-task `mode` (`dashboard/mode.py` + AgentRunner Protocol
    的 `allowed_tools` kwarg) 就是這條 gap 要的東西:Tool API 用
    `mode: plan|build|review` 把硬性 whitelist 傳給 orchestrator,orchestrator
    再透傳給 runner;OpenCodeRunner / ClaudeCLIRunner 重建 argv,
    AnthropicAPIRunner / EchoRunner accept-and-ignore (沒有 CLI whitelist 介面)
  - 進階作法 (per-tool allow vs deny / 細粒度 path-based permissions) 留作
    後續 Phase C governance,不在當前 gap 範圍
- [x] **W6.4 部署手冊延伸** — 反向代理章節 / TLS 章節
  - 已落地:user manual §4.2 雙語版本展開,涵蓋 Caddy (推薦,自動 TLS) /
    nginx (含 limit_req_zone rate-limit + WebSocket map) / Traefik (compose
    label 自動探查) 三種完整 config block,加上 HSTS / X-Content-Type-Options
    安全 header,以及 `curl -i -N` WebSocket Upgrade 自我驗證方法

## Low — 屬於更廣 backlog,當前不阻塞 DoD

- [x] **Linear adapter** — `LinearTracker` 已落地
  - `symphony_mvp/tracker.py` 新增 `LinearTracker` (GraphQL,~180 行) +
    `build_tracker("linear", ...)` 工廠分支;`tracker.repo` 用 Linear team
    key (例如 `ENG` → `ENG-123`);state mapping 涵蓋 type-driven (started /
    unstarted / completed / canceled / backlog) 與 name-driven (`In Review` /
    `Blocked` / `Ready` / `Triage`);分頁透過 `pageInfo.endCursor`
  - 22 個 unit test (mock `requests.post`) 涵蓋 state mapping (10 cases)、
    `_to_issue` 邊界、pagination、reconcile 找不到 issue、GraphQL errors[]
    surface、headers shape、build_tracker 工廠驗證
- [ ] **`linear_graphql` 動態工具** (SPEC §14 提到)
  - 用 GitHub 時不需要;若要接 Linear,包成 MCP server 一層即可
- [ ] **Dashboard 多人 RBAC**
  - 目前只有單 token 全有 / 全無 (spec §7 已留升級空間)
  - 若團隊多人共用,需要 user / role / scoped token
- [x] **W6.4 API reference** — `/api/v1/*` 獨立文件
  - 已落地:[docs/API-REFERENCE.md](../API-REFERENCE.md) + 中文鏡像,
    按受眾分三層 (Coding Service Tool API / Dashboard Operator API / 基礎建設),
    含 curl 範例與認證 recipe;同時直接連到 FastAPI 自動產生的 `/openapi.json`
    與 `/docs` Swagger UI 當權威來源
- [ ] **frontend i18n**
  - 目前 UI 文字硬寫英文。Hint composer / 動作按鈕等對中文 operator 不友善
  - 需要先決定 i18n 方案 (react-intl / lingui / 自家 hook)

---

## 來源對照

這份清單來自:

- 過去 README 的 `## Gap to production` 表格 (英中各一),已在這次重構中
  從 README 搬到本檔
- W6 階段 sprint plan 的 W6.4 文件項裡未完成的兩條
- Docker scaffolding milestone 的後續工作 (從 user guide / README 各處的
  「next milestone」備註彙整)

每完成一項,把對應的 `[ ]` 改 `[x]` 並補上 commit / PR 連結。
