# Symphony Dashboard 實作 Todolist (v1.0)

> 對應 spec: [docs/design/symphony-dashboard-spec.md](../design/symphony-dashboard-spec.md)
> 對齊 spec §10 工作分解 (W1–W6),前面加一個 P0 前置階段以保護既有 29 tests。

## 排程總覽

| 階段 | 項數 | 對應 spec 估時 | 完成後可驗證的事 |
|------|------|---------------|----------------|
| P0 前置 | 2 | 0.5d | MVP 加 opt-in hooks,29 tests 仍綠 |
| W1 後端骨架 | 8 | 2d | curl 打 `/api/v1/state`、WS 收到事件、hint 注入完整 round-trip |
| W2 前端骨架 | 5 | 3d | 瀏覽器看到 5 欄 Kanban + Drawer 殼 |
| W3 互動動作 | 4 | 1.5d | 拖拉 / hint / pause / abort 從 UI 實際生效 |
| W4 進階觀察 | 5 | 2d | Events 時間線 / Prompt 預覽 / Replay 重播 |
| W5 Workflow 編輯 | 1 | 1d | 線上編輯 + last-known-good 熱載 |
| W6 測試與文件 | 4 | 1.5d | E2E 全綠、DoD 11 項全勾、docs 完成 |

---

## P0 — 前置:opt-in 擴充點 (0.5d)

> 這一階段的目的是讓既有 MVP 程式碼**對 dashboard 預留可注入的鉤子**,但 dashboard 不存在時行為完全不變。spec §11 DoD #11 要求關掉 dashboard 後 29/29 tests 仍綠 — 這條紅線從這裡開始守。

- [ ] **P0.1** 擴充 MVP 資料模型與 orchestrator opt-in 鉤子
  - `models.Issue.to_template_context(attempt, hints=None)` — 加 hints 可選參數,放進 returned dict (預設 `[]`)
  - `models.RunAttempt` — 加 `paused_until: datetime | None = None`
  - `models.TerminalReason` — 加 `ABORTED = "aborted"`
  - `Orchestrator.__init__` — 加可選 `bridge: DashboardBridge | None = None` 參數 (forward declaration / Protocol)
  - `Orchestrator._dispatch` — 排序前若 `bridge`,呼叫 `bridge.get_priority_overrides()` 套用
  - `_AgentWorker.run` — `render_prompt` 之前若 `bridge`,呼叫 `bridge.fetch_pending_hints(issue.id)` 並把 ids 暫存,render 成功後呼叫 `bridge.mark_hints_consumed(ids)`
  - `Orchestrator` — 加 `pause(issue_id, reason)` / `resume(issue_id)` / `abort(issue_id)` / `force_retry(issue_id)` 公開方法
  - `_reconcile` — 加上 `paused_until` 檢查:在還未到時間點之前讓 worker stop 並轉 RETRY_QUEUED
  - 所有改動必須是 **append-only / 預設值**;既有 29 tests 不能改
- [ ] **P0.2** 建立 `symphony_mvp/dashboard/` 子套件 + pyproject 依賴
  - `pyproject.toml` 新增 `[project.optional-dependencies] dashboard = ["fastapi>=0.110", "uvicorn[standard]>=0.27", "websockets>=12"]`
  - 建立 `symphony_mvp/dashboard/__init__.py`、`bridge.py`、`server.py`、`schema.sql`、`__main__.py` (空殼)

---

## W1 — 後端骨架 (2d)

- [ ] **W1.1** `DashboardBridge` 核心 — SQLite schema + 事件訂閱 + ring buffer
  - `event_records` / `hints` / `priority_overrides` 三張表 (見 spec §3)
  - `__init__(orch, db_path, ring_size=500)`:訂閱 `orch.add_event_listener(self._on_event)`
  - `_on_event(issue_id, event)`:落 SQLite + 寫 ring buffer + fan-out 給 subscribers
  - `add_subscriber(fn)` / `remove_subscriber(fn)`:給 WebSocket 連線用
  - `get_recent_events(issue_id, limit=100)`:給新連上的 WS 補歷史
- [ ] **W1.2** Hint 注入完整 round-trip
  - `bridge.add_hint(issue_id, author, content) -> int`
  - `bridge.fetch_pending_hints(issue_id) -> list[dict]`
  - `bridge.mark_hints_consumed(hint_ids: list[int])`
  - `bridge.list_hints(issue_id) -> list[dict]` (consumed + pending)
  - 跟 P0.1 的 worker hook 接起來,寫整合測試:add → dispatch → render 看到 → consumed_at 被填
- [ ] **W1.3** `PriorityOverrideStore`
  - `set_override(issue_id, rank, set_by, reason, ttl_hours=24)`
  - `get_priority_overrides() -> dict[issue_id, rank]`
  - `clear_expired()` 每 tick 呼叫一次
  - 跟 `Orchestrator._dispatch` 排序鉤子接起來
- [ ] **W1.4** Orchestrator pause/resume/abort/retry 動作
  - `pause(issue_id, reason)`:`att.paused_until = datetime.max`,`_reconcile` 看到後 stop worker 並轉 RETRY_QUEUED (保留 session_id)
  - `resume(issue_id)`:`att.paused_until = None`,下個 tick 重排
  - `abort(issue_id)`:立刻 stop worker,`_release(att, ABORTED, ...)`,**不**進 retry queue
  - `force_retry(issue_id)`:即使 RELEASED 也清掉 attempt counter,允許下個 tick 重排
  - 對應單元測試
- [ ] **W1.5** FastAPI server (REST 路由)
  - `GET /api/v1/state` — 5 欄分類後的 snapshot + totals
  - `GET /api/v1/issues/{id}` — 含 hints / rendered_prompt_preview / workspace_path
  - `POST /api/v1/issues/{id}/hint`
  - `POST /api/v1/issues/{id}/pause` `/resume` `/abort` `/retry`
  - `POST /api/v1/priority` — 重排 pending 順序
  - `PATCH /api/v1/config` — 白名單 (max_concurrent_agents、polling_interval_ms)
  - `GET /api/v1/workflow` `PUT /api/v1/workflow` (last-known-good)
  - `Bearer` token middleware:env `DASHBOARD_API_KEY` 為空時不檢
  - 確保 `tracker.api_key` 不會出現在任何 response
- [ ] **W1.6** WebSocket `/api/v1/events`
  - 連線後立刻推送目前所有未終結 attempt 的最近 100 個事件
  - 即時推送新事件 (`agent_event` / `fsm_transition` / `config_changed` / `workflow_reloaded`)
  - 支援 `?filter=issue:MT-1,MT-3` query 縮窄範圍
- [ ] **W1.7** CLI entrypoint `symphony_mvp/dashboard/__main__.py`
  - `python -m symphony_mvp.dashboard ./WORKFLOW.md --port 7957 --db ./dashboard.db --api-key $KEY`
  - Orchestrator 在背景 thread 跑;FastAPI 在主執行緒 (uvicorn)
  - 同程序、同 port,前端 build 後由 FastAPI `StaticFiles` serve
- [ ] **W1.8** 後端單元測試 + legacy 回歸
  - bridge:hint round-trip、ring buffer 滿、event persist
  - priority override:排序順序、過期清理
  - pause/resume/abort/retry FSM 轉移
  - FastAPI:TestClient 打每個 endpoint 驗 schema + 401/403
  - **驗收**:`python -m pytest` 仍 29/29 (legacy) + 新增的 ~15+ 條全綠

---

## W2 — 前端骨架 (3d)

- [ ] **W2.1** `frontend/` 架設 Vite + React 19 + TS + Tailwind 4 + Zustand
  - `pnpm create vite frontend --template react-ts`
  - 整合 Tailwind 4、Zustand、@dnd-kit、xterm、monaco-editor、react-diff-viewer-continued
  - build 後 output 給 FastAPI `StaticFiles` 同 port serve
- [ ] **W2.2** WebSocket client + Zustand store
  - 自動重連 (exponential backoff)、bearer token header、訊息分派
  - REST fetcher helpers (typed)、錯誤 toast
- [ ] **W2.3** Top bar + Filter bar
  - Top: 狀態燈、`max_concurrent` 下拉、tick interval、Settings、Workflow 進入
  - Filter: agent / priority / label 下拉 + 搜尋
- [ ] **W2.4** Kanban 5 欄 + IssueCard
  - 欄位:Pending / Claimed / Running / Retry-Queued / Released
  - Card 顯示:identifier、priority、title、labels、runtime、attempt #/N、turn 進度條、agent、cost
- [ ] **W2.5** Issue Detail Drawer 殼 + Overview tab
  - 右側 70% 寬度抽屜、Tab 路由 (Overview/Events/Prompt/Hints/Workspace/Replay)
  - Overview:tracker URL、description (md render)、labels、blocked_by、run history table、動作按鈕

---

## W3 — 互動動作 (1.5d)

- [ ] **W3.1** @dnd-kit 拖拉重排 (Pending 欄內限定)
  - 跨欄 drop **disable** (FSM 由 orchestrator 決定)
  - drag end → POST `/api/v1/priority` → optimistic update
- [ ] **W3.2** Hint composer (Hints tab)
  - textarea + Add Hint → POST `/hint`
  - Pending / Consumed 兩段顯示;consumed 灰色顯示 "已注入 attempt #N"
- [ ] **W3.3** Pause / Resume / Abort / Retry
  - Overview tab 的動作按鈕 + card 右鍵快速選單
  - Abort 顯示確認對話框
- [ ] **W3.4** 全域 Activity Feed
  - 右下角可摺疊小視窗、最新事件列表
  - 點擊任一行 → 開該 issue 的 Events tab

---

## W4 — 進階觀察 (2d)

- [ ] **W4.1** Events tab 時間線
  - 每事件一行,kind/icon/顏色區分
  - 過濾條:message_delta / thinking / tool_call / errors
  - 即時 append + 自動捲到底
- [ ] **W4.2** xterm.js ANSI 顯示
  - Events tab 上方終端機面板,渲染 tool_result 的 ANSI 輸出
- [ ] **W4.3** Prompt tab
  - Monaco read-only viewer 顯示這個 attempt 實際 render 出來的 prompt
  - "Re-render with current hints" 按鈕呼叫後端 preview endpoint
- [ ] **W4.4** Workspace tab
  - 列 workspace 檔案 (頂層 + 一層遞迴)
  - 點檔案預覽前 200 行
  - **明確不提供** Open in IDE / 編輯按鈕
- [ ] **W4.5** Replay tab
  - 從 SQLite 拉歷史 attempt 事件
  - 播放控制:▶ / ⏸ / ⏪ 1× 2× 4× / 跳到時間點

---

## W5 — Workflow 編輯 (1d)

- [ ] **W5** Monaco YAML+MD editor + last-known-good
  - 左:Monaco 編輯區
  - 右上:解析後的 front matter live preview
  - 右下:對 Pending 最高優先 issue 做 sample render
  - `Validate` 按鈕呼叫 `load_workflow`,錯誤行高亮
  - `Save & Reload` 寫回檔案 → 後端熱載入;parse 失敗回 422 + toast,記憶體保留舊 config

---

## W6 — 測試與文件 (1.5d)

- [ ] **W6.1** Frontend 單元測試
  - Vitest + React Testing Library
  - KanbanColumn / IssueCard / Drawer tabs / hint composer
  - Mock WebSocket + REST
- [ ] **W6.2** Playwright E2E (5 條 DoD path)
  1. 拖拉 Pending 順序 → 下次 dispatch 照新順序
  2. 加 hint → 等下次 attempt → render_prompt 預覽看到 hint → consumed_at 被填
  3. Pause → worker 在 5s 內停 → Resume → 同 session_id 接續
  4. Abort → worker 立即終止 → 卡片移到 Released 標 ABORTED
  5. WORKFLOW.md 故意寫錯 YAML → save 失敗顯示錯誤、orchestrator 仍用舊設定運作
- [ ] **W6.3** Definition of Done 驗收 (spec §11 #1–#11)
  - **#9** Tracker API key 不會出現在任何 API response (用 `--api-key=$LEAKED` 啟動然後抓所有路由的 response 看)
  - **#10** Bearer token 無效時所有 REST + WS 都回 401/403
  - **#11** `python -m pytest` 仍 29/29 pass (證明 dashboard 完全 opt-in)
- [ ] **W6.4** 文件
  - README dashboard 章節
  - 部署手冊 (單機 + Docker + 反向代理)
  - `/api/v1/*` API 參考

---

## 關鍵設計決定 (開工前對齊)

1. **opt-in 注入點 (W1.1–W1.4) 是整個 plan 的脊椎** — 三個 MVP hook 全用「dashboard 不在時為 None」實作,並且只**新增**參數預設值,不**修改**既有簽章語意,這樣 spec §11 DoD #11 (關掉 dashboard 後 29/29 tests 仍綠) 才能成立。
2. **Hint 注入 (W1.2) 必須跟 attempt 同 transaction 標記 consumed** — 否則 worker 中途 crash 會導致 hint 重複注入。規畫在 `_AgentWorker.run` 的 `render_prompt` 成功之後立刻 mark,而不是等到 worker 結束。
3. **拖拉只允許 Pending 內互換** (W3.1) — 跨欄拖拉違反 spec §4.2 「FSM 變化由 orchestrator 決定」。UI 層直接 disable cross-column drop。
4. **Workflow editor 的 last-known-good** (W5) 寫在後端 — 前端只負責顯示 422 錯誤,記憶體裡的舊 config 由 FastAPI 那層守住。
5. **明確排除 (對齊 spec §1.2 Non-goals)**:無 IDE 整合、無 PR/Merge UI、無 chat 介面、無 Approve-every-step、無「直接寫 tracker」按鈕。
