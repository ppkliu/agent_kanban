# Post-MVP Gaps & Roadmap

> 對應的初期 sprint plan 在 [symphony-dashboard-todolist.md](symphony-dashboard-todolist.md)。
> 這份檔案專門記錄 README 當初 "Gap to production" 段落以及目前其他文件中提到的
> 未完成項目,讓 README 本體保持簡潔。

## 排優先序

| Severity | 數量 |
|----------|------|
| **High** | 3 |
| Medium   | 4 |
| Low      | 3 |

---

## High — 阻塞文件描述的部署路徑或安全紅線

- [ ] **Docker scaffolding** — `Dockerfile` + `docker-compose.yml` 還沒 check in
  - 文件 ([`docs/guide/docker-quickstart.md`](../guide/docker-quickstart.md))
    描述的部署路徑都靠這兩個檔案
  - 同時要在 CI 加一條 `docker compose up -d` 的 smoke test
  - 影響:`README` / `docs/README.zh-TW.md` 的「Docker (recommended)」段落、
    user guide 的所有指令都要等這個落地
- [ ] **Workspace 沙箱**
  - Native 模式:只靠 host OS,任何高權限工具場景都關鍵 (privileged Bash / Edit / Write)
  - Docker 模式:`symphony-opencode` 容器一旦落地就提供基本隔離
  - 進階升級路徑:firecracker / gVisor / rootless Docker
- [ ] **Prompt injection 防護**
  - 在 `render_prompt` 後做 token-level 隔離,或用 system message 強化
  - 需要先決定:在 runner 那層做 (集中) 還是在 workflow render 後做 (early)?

## Medium — MVP 可上線但長期會痛

- [ ] **持久化 retry queue** — 重啟丟失內存 attempt
  - 解法:把 `_attempts` 落到 SQLite WAL (Dashboard SQLite 已 WAL,複用即可)
  - 需要決定:是保留現有的 in-memory 為主、SQLite 為災難復原 backup,
    還是直接讓 SQLite 為單一真相
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
