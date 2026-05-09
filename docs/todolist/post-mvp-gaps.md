# Post-MVP Gaps & Roadmap

> 對應的初期 sprint plan 在 [symphony-dashboard-todolist.md](symphony-dashboard-todolist.md)。
> 這份檔案專門記錄 README 當初 "Gap to production" 段落以及目前其他文件中提到的
> 未完成項目,讓 README 本體保持簡潔。

## 排優先序

| Severity | 數量 |
|----------|------|
| **High** | 2 |
| Medium   | 6 |
| Low      | 5 |

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
- [ ] **Prompt injection 防護**
  - 在 `render_prompt` 後做 token-level 隔離,或用 system message 強化
  - 需要先決定:在 runner 那層做 (集中) 還是在 workflow render 後做 (early)?

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
  - **Phase B (polish, 部分完成):** ✅ inspect_repo (新增 `/api/v1/tools/inspect_repo`
    + `dashboard/repo_inspect.py`),✅ 完整 stage 翻譯 (`dashboard/stage.py`,
    `check_task_status` 自動 surface `exploring_codebase` / `modifying_files` /
    `running_tests` / `summarising` 等細粒度 stage),✅ result derivation 補
    files_changed / tests_run / blockers / follow_ups (`dashboard/task_result.py`,
    `get_task_result` 從 event stream 萃取結構化 outcome)。
    **未完成:** mode 隔離 per-task allowed_tools — 需要 orchestrator/runner 端
    的 runtime override plumbing,留待下一個 plan
  - **Phase C (per-need):** idempotency / multi-repo / quota / sandbox 三層防線
  - 跟 Docker Phase 2 完全正交,可平行做
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
- [ ] **CI smoke test** — `docker compose up -d` 在 GH Actions / 自架 runner 跑
  - 確認 `/healthz` 200、SPA 載入、`docker compose down -v` 乾淨
  - 跟現有的 pytest / vitest job 並行
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
- [ ] **Tracing / Metrics**
  - 目前只有 logs + Dashboard event log
  - 加 OpenTelemetry instrumentation,export 到 Tempo / Jaeger / Loki / Prometheus
- [ ] **Permission policy 比 Codex 簡化**
  - Claude CLI 的 `--permission-mode` 涵蓋大部分;opencode 的 `allowed_tools` 補齊另一塊
  - 缺一個跨 runner 的 allow-list hook (寫到 `WorkflowConfig`?)
- [ ] **W6.4 部署手冊延伸** — 反向代理章節 / TLS 章節
  - User guide §5 已有 stub,但需要可複製的 Caddy / nginx config block
  - 包含 WebSocket upgrade header 透傳 + 速率限制建議

## Low — 屬於更廣 backlog,當前不阻塞 DoD

- [ ] **Linear adapter** 仍是 stub
  - 對照 GitHub adapter 寫 GraphQL 版本 (~150 LOC)
- [ ] **`linear_graphql` 動態工具** (SPEC §14 提到)
  - 用 GitHub 時不需要;若要接 Linear,包成 MCP server 一層即可
- [ ] **Dashboard 多人 RBAC**
  - 目前只有單 token 全有 / 全無 (spec §7 已留升級空間)
  - 若團隊多人共用,需要 user / role / scoped token
- [ ] **W6.4 API reference** — `/api/v1/*` 獨立文件
  - 路由形狀已記在 spec §6 散文裡,但沒有抽出獨立檔案 (例如 OpenAPI YAML)
  - FastAPI 已內建 `/openapi.json` 與 `/docs`,可以直接連
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
