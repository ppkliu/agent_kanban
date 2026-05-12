# Symphony MVP — 完成度狀態

> English: [MVP-STATUS.md](MVP-STATUS.md)
> 審核日期:**2026-05-11** · 測試:**164 個全綠** (滾動式 — 每次影響 MVP 的 commit 後重貼)

30 秒內回答「Symphony 的 MVP 框架做完了嗎?」想看深入文件就讀
[使用說明書](guide/user-manual.zh-TW.md)、[`/api/v1/*` API reference](API-REFERENCE.zh-TW.md);
要追進行中的 roadmap 看 [post-mvp-gaps.md](todolist/post-mvp-gaps.md)。

---

## 1. 這是什麼

Symphony 是個 **local-first 的 coding service**,讓外部 LLM agent
(Claude / GPT / 自家) 透過小而精的 RESTful Tool API
(`POST /api/v1/tools/*`,fire-and-check) 派發 coding task。
Orchestrator + dashboard + runner 整套用 Docker 自架,接本地 LLM
endpoint,**沒有強制的雲端依賴**。

MVP 框架的契約:**對上游 agent 暴露 6 個語意化 tool、opt-in dashboard
讓人類觀察、四種可替換 runner 用 `WORKFLOW.md` 一行切換**。

---

## 2. MVP 完成度檢查表

| 能力 | 狀態 | 證據 (commit) | 自己驗證 |
|---|---|---|---|
| 單容器 Docker scaffolding | ✅ | `d6993d1` `1cd5141` | `docker compose up -d && curl http://localhost:17957/healthz` |
| Coding Service Tool API — Phase A (5 endpoints) | ✅ | `7f9707a..67b388c` | `python examples/tool_api_client.py` |
| Coding Service Tool API — Phase B (stage / result / inspect_repo / mode) | ✅ | `2fc84d7..344fa20`, `997a4aa..515ad66` | `pytest tests/test_tool_api.py tests/test_stage.py tests/test_task_result.py tests/test_repo_inspect.py tests/test_mode.py` |
| Per-task mode (plan / build / review) — runner 層硬性 tool whitelist | ✅ | `997a4aa..515ad66` | `pytest tests/test_mode.py tests/test_agent_runner.py::test_opencode_build_argv_uses_per_call_allowed_tools_override` |
| 持久化 retry queue — 行程重啟自動 hydrate in-flight attempts | ✅ | `2fda864..6986240` | `pytest tests/test_dashboard_bridge.py -k attempt` |
| Phase C idempotency — `submit_coding_task` 透過 `idempotency_key` 安全重試 | ✅ | 本批 commits | `pytest tests/test_dashboard_bridge.py -k idempotency` |
| Prometheus `/metrics` — 9 個零依賴 counter,Prometheus/Grafana 可直接 scrape | ✅ | 本批 commits | `curl http://localhost:17957/metrics` |
| Emergency Stop All (operator 緊急停止) | ✅ | `1c2a45a` `2448f65` | 瀏覽器 TopBar 紅色按鈕,或 `POST /api/v1/emergency_stop` |
| 四種可替換 runner (echo / opencode / anthropic_api / claude_cli) | ✅ | `3b81799` `2ff85ba` | 改 `WORKFLOW.md` 的 `runner.kind` 重啟 |
| 雙語使用說明書 + Tool API client demo | ✅ | `2fa23f1` `09dbd8c` `3754e0d` | 開 [user-manual.zh-TW.md](guide/user-manual.zh-TW.md) |
| 5 欄 Kanban + 6 tab Issue Drawer | ✅ | `2c9ef2f` 之前 | 開 `http://localhost:17957` |
| 自含測試套件 (不需 LLM / 不需 GitHub) | ✅ | 164 tests | `.venv/bin/python -m pytest` |
| REST + WebSocket Bearer 認證 | ✅ | `dashboard/server.py:_require_auth` | `DASHBOARD_API_KEY=$(openssl rand -hex 32) docker compose up -d` |

**結論**:README 跟使用說明書裡的每個行為承諾,**都有 code 或 test 撐住**
— 沒有「coming soon」的尾巴。

---

## 3. 刻意留在 MVP 之外的項目

這些屬於**生產級加固**,不是 MVP 框架的完成度問題。詳細追蹤在
[post-mvp-gaps.md](todolist/post-mvp-gaps.md)。

| 缺口 | 嚴重度 | 為什麼留在 MVP 之外 |
|---|---|---|
| Workspace 沙箱升級 (firecracker / gVisor / rootless) | 高 | 單容器 Phase 1 已提供容器隔離;再強的邊界是升級路徑 |
| Prompt injection 防護 | 高 | 需要決定 (token-level 隔離 vs system message 強化);MVP 預設 trusted-tenant |
| 雙容器爆炸半徑切分 | 中 | OpenCodeRunner subprocess 目前可用;HTTP-based runner 是已[分析過](design/opencode-two-container-analysis.md)的後續工作 |
| OpenTelemetry tracing / metrics | 中 | Dashboard event log + uvicorn 結構化 log 涵蓋 MVP 審計需求 |
| Multi-repo pool | 中 | 單 tracker 對首次整合者最不意外 |
| 多人 RBAC | 低 | 單 token 全有/全無對單一團隊自架夠用 |
| `linear_graphql` 動態工具 | 低 | 只有要橋接 Linear 才需要 (GitHubIssuesTracker 不需要) |
| Frontend i18n | 低 | UI 文字只給 operator 看;對 integrator 的介面 (Tool API + 說明書) 已是雙語 |
| CI smoke test | 中 | 手動驗證腳本涵蓋路徑;GH Actions 接線等推到 remote 才有意義 |

---

## 4. 自己驗證

```bash
# 1. Clone + 跑測試 (不需 LLM、不需 Docker)
git clone <this-repo> && cd agent_kanban
uv venv && uv pip install -e ".[dev,dashboard]"
.venv/bin/python -m pytest                  # 預期:164 passed

# 2. 拉起整套框架
cp examples/WORKFLOW.docker.md WORKFLOW.md
docker compose up -d
curl -fsS http://localhost:17957/healthz    # 預期:{"ok": true}

# 3. 從外部 code 驅動 (Tool API 標準 round-trip)
SYMPHONY_URL=http://localhost:17957 \
  python examples/tool_api_client.py

# 4. 瀏覽器看 kanban 即時更新
open http://localhost:17957
```

任何一步在乾淨 checkout 上失敗都算 regression — 報 bug。

---

## 5. 版本戳記

| | |
|---|---|
| 審核日期 | **2026-05-11** |
| Commit | 滾動式 — 最新 checkpoint 看 `git log` |
| 測試通過數 | **164** (`pytest -q`) |
| 前端測試 | **45** (`npm test`) |
| Docker image | `symphony-dashboard:dev` (Phase 1 單容器) |
| Tool API 上線端點 | `list_repos / inspect_repo / submit_coding_task / check_task_status / get_task_result / cancel_task` |

> 動到 `symphony_mvp/` / `tests/` / `Dockerfile` / `docker-compose.yml`
> 後請重跑這份 audit,讓 checklist 保持誠實。
