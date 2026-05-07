# Symphony Dashboard 開發規格 (v1.0)

> **目標**:為 [Symphony MVP](../symphony-mvp) 建立一個 Web 儀表板,讓人能 (a) 即時觀察 orchestrator 排程與 agent 執行狀態,(b) 在不破壞 Symphony 核心哲學的前提下,以人機協作方式介入任務優先順序、暫停/恢復、補充上下文。

> **核心約束**: Symphony 把所有「業務邏輯」推到 Agent prompt 裡,**Orchestrator 對 tracker 全程唯讀**。Dashboard 必須延續這個哲學 — 任何 ticket 內容變更 (狀態、留言、PR link、handoff label) 都不能由 dashboard 直接寫入 tracker,只能透過「補充 prompt 給 agent」或「給 agent 排個新 attempt」這類間接手段達成。這是 Symphony 跟 vibe-kanban / ai-agent-board 等專案最根本的設計差異。

---

## 0. 與四個參考專案的批判性比較

設計 dashboard 之前必須先釐清:這幾個 kanban-for-AI-agents 專案表面相似,但 **設計哲學跟 Symphony 完全不同**。複製錯地方會破壞 Symphony 的核心抽象。

| 專案 | 設計哲學 | 跟 Symphony 的衝突點 |
|------|---------|---------------------|
| **vibe-kanban** (BloopAI, 23.7k★) | UI 是 source of truth,kanban 跟 agent run 一對一,UI 直接寫 task DB,還包了 IDE 預覽、devtools、PR 開立、合併 | UI 直接寫 task,Symphony 不允許;PR/合併屬個人開發場景,服務型 daemon 部署用不到 |
| **ai-agent-board** (DanWahlin) | 拖拉建立 task,選 agent 後執行,**自動合併 worktree 到 main**,有 PR 建立按鈕 | 同樣 UI 直接寫 task;auto-merge to main 對任何受監管環境都不能用 |
| **Claude-Code-Board** (cablate, 146★) | 是 Claude Code session 管理器,不是真的 kanban — 強調「同一專案併發多 session」 | 沒有 orchestrator 排程概念,只是 session 切換器;**作者已自封不維護** |
| **cline** (saoudrizwan, VS Code 套件) | IDE 內 chat panel,human-in-the-loop 每步 approve | 不是 dashboard,是 chat UI;每步 approval 跟 Symphony 「持續自主執行」哲學相反 |

**會借鑑的設計元素**:
- 從 **vibe-kanban**:kanban 四欄佈局、agent worktree 隔離、即時事件串流
- 從 **ai-agent-board**:`AgentEvent` 正規化、終端機風格事件檢視 (xterm.js)、task group 並行化
- 從 **Claude-Code-Board**:訊息類型分類 / 過濾 (user / assistant / tool_use / thinking)

**會明確排除的設計元素** (因為跟 Symphony 哲學衝突):

1. **❌ UI 直接寫 tracker / 後端資料庫**:Symphony 規格明文 orchestrator 唯讀。這在四個參考專案都做錯了。我們的 dashboard 對 ticket 內容**沒有**直接寫入路徑。
2. **❌ Auto-merge to main / 個人 GitHub PR 流程**:這是個人開發場景的特有需求;Symphony 是 daemon 服務,合併策略由 agent 用 `gh` 或 MCP 工具自行完成。
3. **❌ IDE 整合 (VS Code / Cursor / JetBrains)**:跟伺服器型 daemon 部署架構衝突。
4. **❌ 內建瀏覽器/devtools/inspect mode**:vibe-kanban 那一整套個人開發者體驗的東西全部不要。
5. **❌ Approve-every-step UX**:cline 風格的逐步審批會破壞 Symphony 的吞吐量設計;改用 *post-hoc* 觀察 + 介入。

---

## 1. 設計目標與非目標

### 1.1 必達目標 (MVP scope)

**G1 觀察性 (Observability First)**
- 即時看到所有 issue 的內部 FSM 狀態 (UNCLAIMED / CLAIMED / RUNNING / RETRY_QUEUED / RELEASED)
- 每個 issue 的 agent 事件流 (turn / tool_call / tool_result / message_delta) 即時呈現
- 目前 worker 在哪個 workspace 路徑跑、用哪個 model、累計 turns / tokens / cost

**G2 人機協作 (Human-in-the-Loop, post-hoc)**
- 拉動優先順序 (drag-and-drop 重新排序待 dispatch 的 issues)
- 暫停 / 恢復 / 強制中止特定 issue 的執行
- 補上 prompt 給 agent (透過 `Symphony Hint` 機制 — 見 §4)
- 強制重試一個已 RELEASED 的 issue
- 切換 max_concurrent_agents (排程閾值即時調整)

**G3 後設管理 (Workflow Mgmt)**
- 線上編輯 WORKFLOW.md 並做 last-known-good 熱載入
- 觀察 prompt template render 結果 (給某個具體 issue 預覽展開後是什麼樣)
- 執行歷史回放 (replay) 一個 attempt 的事件流

### 1.2 明確 Non-goals

- **不做 IDE 整合**。沒有 VS Code 套件、沒有 vscode:// URL、沒有 Open in editor 按鈕
- **不做 PR / Merge / Git 流程 UI**。這些屬於 agent prompt 領域,dashboard 不沾
- **不做 chat 介面**。這不是 chatbot,是 daemon 觀察台
- **不做行動裝置完整支援**。手機只看 read-only 摘要,複雜操作要桌面
- **不取代 Linear / GitHub UI**。issue 內容的真實編輯仍在 tracker 原生介面做

---

## 2. 系統架構

```
┌──────────────────────────────────────────────────────────────────┐
│                         Browser (React + TS)                      │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │ Kanban Board   Filter Bar   Issue Detail Drawer          │    │
│  │ Activity Feed  Workflow Editor   Hint Composer           │    │
│  └──────────────────────────────────────────────────────────┘    │
└────────┬─────────────────────────────────────────────────┬───────┘
         │ HTTPS REST                          WebSocket   │
         │ (mutations & queries)               (events)    │
┌────────▼─────────────────────────────────────────────────▼───────┐
│                  Symphony Dashboard Server (FastAPI)              │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │ /api/v1/state          GET  state snapshot               │    │
│  │ /api/v1/issues/{id}    GET  detail + history             │    │
│  │ /api/v1/issues/{id}/hint   POST  attach human hint       │    │
│  │ /api/v1/issues/{id}/pause  POST  cooperative pause       │    │
│  │ /api/v1/issues/{id}/resume POST                          │    │
│  │ /api/v1/issues/{id}/abort  POST  hard cancel + release   │    │
│  │ /api/v1/issues/{id}/retry  POST  force re-attempt        │    │
│  │ /api/v1/priority           POST  reorder pending queue   │    │
│  │ /api/v1/config             PATCH  live config knobs      │    │
│  │ /api/v1/workflow           GET / PUT  WORKFLOW.md edit   │    │
│  │ /api/v1/events             WS   live AgentEvent stream   │    │
│  └──────────────────────────────────────────────────────────┘    │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │ DashboardBridge:                                         │    │
│  │   - subscribes to Orchestrator.add_event_listener        │    │
│  │   - keeps in-memory ring buffer of last N events / issue │    │
│  │   - persists to SQLite for history queries               │    │
│  │   - exposes mutation calls -> Orchestrator + HintStore   │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────┬───────────────────────────────────────┘
                           │ in-process Python imports
┌──────────────────────────▼───────────────────────────────────────┐
│                Symphony MVP Orchestrator (existing)                │
│  Tracker (READ-ONLY)   Workspace   AgentRunner                    │
└──────────────────────────────────────────────────────────────────┘
```

### 2.1 為什麼是 in-process,不是獨立微服務?

Symphony 的 in-memory 排程狀態 (RunAttempt / FSM transitions) 不寫資料庫 — SPEC §3 故意這樣設計,讓重啟可以從 tracker 重建。如果把 dashboard 拆成獨立程序,會被迫 (a) 暴露大量 IPC API 或 (b) 把狀態複製進另一個 store。兩者都跟 SPEC 哲學衝突。

**所以:dashboard server 跟 orchestrator 共用同一個 Python 程序**,FastAPI 起在另一個 thread,直接持有 `Orchestrator` instance。這也對應 SPEC §13 的設計提示 ("server.port enables OPTIONAL HTTP dashboard on the same process")。

### 2.2 技術選型

| 層 | 技術 | 為什麼這個 |
|----|------|----------|
| 後端框架 | FastAPI | 跟既有 Python MVP 同生態;原生 WebSocket;async 支援 |
| 即時通訊 | WebSocket (FastAPI) | 比 SSE 雙向友善;事件 + 控制訊號共用 |
| 持久化 | SQLite (events 歷史) | 零配置;Symphony 主狀態仍不寫 DB,這只放歷史回放 |
| 前端框架 | React 19 + TypeScript + Vite | 跟 vibe-kanban / ai-agent-board 對齊,容易借用社群元件 |
| 樣式 | Tailwind CSS 4 | 快速、一致;社群 kanban 範例多 |
| 拖拉 | @dnd-kit/core | vibe-kanban 跟 ai-agent-board 都用,生態成熟 |
| 終端機事件 | xterm.js | ai-agent-board 已驗證適合 ANSI tool output |
| 狀態管理 | Zustand | 比 Redux 輕,符合單頁中型應用規模 |
| 程式碼 diff 顯示 | react-diff-viewer-continued | 簡單 read-only 場景夠用 |
| Markdown / WORKFLOW 編輯 | Monaco Editor | YAML / Jinja2 語法高亮 |

---

## 3. 資料模型 (Dashboard 額外加的)

Symphony 既有的 `Issue` / `RunAttempt` / `AgentEvent` 不動。Dashboard 加三個新的 entity,**全部存在 dashboard 自己的 SQLite,不影響 orchestrator 的 in-memory 狀態**。

### 3.1 `EventRecord` (事件歷史)

```sql
CREATE TABLE event_records (
    id             INTEGER PRIMARY KEY,
    issue_id       TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    kind           TEXT NOT NULL,       -- AgentEventKind enum value
    timestamp      TEXT NOT NULL,       -- ISO 8601
    data_json      TEXT NOT NULL,       -- AgentEvent.data as JSON
    INDEX idx_issue_time (issue_id, timestamp)
);
```

寫入路徑:`DashboardBridge` 訂閱 `Orchestrator.add_event_listener`,每個 event 同時 (a) 推給所有 connected WebSocket、(b) 落 SQLite。

### 3.2 `Hint` (人類補上的 prompt 片段)

> 這是整個 dashboard 最重要的設計。**不是直接編輯 ticket,而是在 issue 上掛一個「下一次 attempt 時要追加進 prompt 的人類補充」**。完美對齊 Symphony 「業務邏輯 in prompt」 哲學。

```sql
CREATE TABLE hints (
    id           INTEGER PRIMARY KEY,
    issue_id     TEXT NOT NULL,
    author       TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    consumed     INTEGER NOT NULL DEFAULT 0,  -- 0 = pending, 1 = injected
    consumed_at  TEXT
);
```

**注入時機**:Orchestrator 在 `_AgentWorker.run()` 呼叫 `render_prompt()` 之前,先 query `HintStore.fetch_pending(issue.id)`,把每筆 hint 包進 `{{ hints }}` 變數傳進 Jinja2 context。這需要在 MVP 既有 `to_template_context()` 上加一個 hook。

**WORKFLOW.md 模板使用方式**:

```markdown
{% if hints %}
# Operator hints (human-supplied, treat as authoritative supplements)

The following notes were added by a human operator AFTER the issue was created.
Treat them as additional context, not as the original task definition.

{% for h in hints %}- [{{ h.author }} @ {{ h.created_at }}] {{ h.content }}
{% endfor %}
{% endif %}
```

這樣 hint 仍然是 prompt 的一部分,Symphony 沒有跳過它的「全部由 agent 自己處理」原則 — 只是 prompt source 多了一個。

### 3.3 `PriorityOverride` (人為調整的優先順序)

Symphony SPEC §9 的排序規則寫死是 `(priority asc, created_at, identifier)`。Dashboard 提供一個 override 表,用拖拉就能臨時打破排序:

```sql
CREATE TABLE priority_overrides (
    issue_id      TEXT PRIMARY KEY,
    rank          INTEGER NOT NULL,    -- 越小越早 dispatch
    set_by        TEXT NOT NULL,
    expires_at    TEXT,                -- 預設 24 小時自動過期
    reason        TEXT
);
```

**注入時機**:Orchestrator 的 `_dispatch()` 排序前,先把 override 表覆蓋上去。Override 只影響 dispatch 順序,**不會修改 tracker 的 priority 欄位** — 對齊唯讀原則。

> 這三張表都放在 dashboard 自己的 SQLite。Orchestrator 重啟後,已 dispatch 的 issue 不受影響;尚未 dispatch 的 issue 會看到歷史 hint + 仍生效的 priority override。

---

## 4. UI 設計規格

### 4.1 整體佈局

```
┌────────────────────────────────────────────────────────────────────────┐
│  Symphony  [● running]   max_concurrent: [5▼]   tick: 30s   ⚙ Settings │  ← Top bar
├────────────────────────────────────────────────────────────────────────┤
│  Filter: [agent ▼] [priority ▼] [label ▼]   Search: [_______]   [📝 Workflow]│
├──────────┬──────────┬──────────┬──────────────────────┬────────────────┤
│ Pending  │ Claimed  │ Running  │ Retry-Queued         │ Released        │
│ (12)     │ (2)      │ (3)      │ (1)                  │ (47)            │
├──────────┼──────────┼──────────┼──────────────────────┼────────────────┤
│ ┌──────┐ │ ┌──────┐ │ ┌──────┐ │ ┌──────────────────┐ │ ┌──────────┐    │
│ │ MT-1 │ │ │ MT-3 │ │ │ MT-5 │ │ │ MT-9 retry in 4m │ │ │ MT-11 ✓  │    │
│ │ p:1  │ │ │ ...  │ │ │ ●●○  │ │ │ ⚠ stall #2       │ │ │ MT-12 ✓  │    │
│ │ ⚲ a11y│ │ └──────┘ │ │ ▍ 4m │ │ └──────────────────┘ │ │ MT-13 ⚠  │    │
│ └──────┘ │          │ └──────┘ │                      │ └──────────┘    │
│ ┌──────┐ │          │          │                      │                 │
│ │ MT-2 │ │          │          │                      │                 │
│ │ ...  │ │          │          │                      │                 │
│ └──────┘ │          │          │                      │                 │
└──────────┴──────────┴──────────┴──────────────────────┴────────────────┘
```

**五個欄位** 直接對應 Symphony FSM 狀態(不是 tracker 狀態 — 這個區分非常重要):
- `Pending` = 還沒 dispatch (UNCLAIMED 或從未 enter FSM)
- `Claimed` = 已分配 worker 但 turn 1 還沒開始
- `Running` = 正在跑
- `Retry-Queued` = 等待 backoff 結束
- `Released` = 終態

### 4.2 卡片元素

每張 issue 卡片需要呈現:

```
┌──────────────────────────────────┐
│ MT-1                       p:1  │  ← identifier + priority
│ Refactor login form              │  ← title (truncate)
│ ⚲ a11y, frontend                 │  ← labels
│ ⏱ 3m 24s    🔁 attempt 2 of 3   │  ← runtime + attempt
│ ▰▰▰▱▱  6/20 turns                │  ← turn progress bar
│ 🤖 claude-opus-4-7  $0.03        │  ← agent + cost so far
└──────────────────────────────────┘
```

**互動**:
- 拖拉到別欄位:**禁用** (FSM 變化由 orchestrator 自己決定,UI 不能直接強制)。例外:從 `Pending` 內部互換順序 (= priority override)。
- 點擊:打開 detail drawer (§4.3)
- Right-click 或長按:顯示快速動作選單 — Pause / Abort / Retry / Add Hint

### 4.3 Issue Detail Drawer (右側抽屜)

點 issue 卡片後,從右側滑出 70% 寬度的抽屜:

```
┌─────────────────────────────────────────────────────────────┐
│ MT-1  Refactor login form                            [×]    │
│ State: RUNNING (attempt 2/3)    Worker: agent-MT-1          │
│ Workspace: ~/symphony-workspaces/MT-1                       │
├─────────────────────────────────────────────────────────────┤
│  [Overview] [Events] [Prompt] [Hints] [Workspace] [Replay]  │  ← Tabs
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Tab content area                                           │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Tab: Overview**
- Tracker URL (link out)
- Description (markdown render)
- Labels, blocked_by
- Run history table — 每個 attempt 一列,顯示 started/ended、terminal_reason、turns_consumed、cost
- 動作按鈕:Pause / Abort / Retry / Add Hint

**Tab: Events** (最重要的觀察介面)
- 上方為 xterm.js 終端機,顯示工具呼叫的 ANSI output
- 下方為 timeline,每個 `AgentEvent` 一行,事件類型用 icon + 顏色區分:
  - 🟢 turn_started / turn_completed
  - 🔵 message_delta (collapsible)
  - 🟡 tool_call (展開後顯示 input)
  - 🟣 tool_result (展開後顯示 output / is_error 標紅)
  - 🔴 error
- 過濾條:`[ ] 顯示 message_delta  [ ] 顯示 thinking  [✓] 顯示 tool_call/result  [✓] 顯示 errors`
- 即時模式:WebSocket 推進來的事件自動 append + 自動捲到底
- 歷史模式:從 SQLite 拉 attempt N 的所有事件回放,可暫停/快轉

**Tab: Prompt**
- 顯示這個 attempt 實際 render 出來的 prompt (Jinja2 展開後的純文字)
- 上方按鈕 `[Re-render with current hints]` — 預覽:如果現在有新 hint,下次 attempt 會看到什麼
- 程式碼區塊用 Monaco read-only 顯示

**Tab: Hints** ← 這是人機協作的主要管道
- 列出此 issue 已加上的所有 hint (consumed / pending 分開)
- 底部一個 textarea + `Add Hint` 按鈕
- 已 consumed 的 hint 用淡灰色顯示,標明「已注入 attempt #N」

**Tab: Workspace**
- 顯示 workspace 路徑
- 列出 workspace 內目前的檔案 (頂層 + 一層遞迴,用 backend `os.listdir` 抓)
- 點檔案可預覽前 200 行 (純 read-only)
- ⚠️ **明確不提供**:Open in IDE / 編輯檔案 / git diff 生產 UI。這些對 daemon 部署是反 pattern。

**Tab: Replay**
- 選擇歷史的某個 attempt,把當時所有 event 用滾軸時間線重播
- 播放控制:▶ / ⏸ / ⏪ 1× 2× 4× / 跳到時間點

### 4.4 Workflow Editor 模式

從 top bar 點 `📝 Workflow` 進入:

```
┌─────────────────────────────────────────────────────────────────────┐
│ WORKFLOW.md         [Saved 3 min ago]   [Validate]   [Save & Reload] │
├──────────────────────────────────────┬──────────────────────────────┤
│                                       │ Live preview                 │
│  Monaco YAML+Markdown editor          │ ┌──────────────────────────┐│
│                                       │ │ Front matter (parsed):   ││
│   ---                                 │ │   tracker.kind = github  ││
│   tracker:                            │ │   max_concurrent = 5     ││
│     kind: github                      │ │   handoff_state = ...    ││
│   ...                                 │ └──────────────────────────┘│
│   ---                                 │ ┌──────────────────────────┐│
│                                       │ │ Sample render (MT-1):    ││
│   {{ issue.title }}                   │ │ ...展開後的純文字 prompt  ││
│                                       │ └──────────────────────────┘│
└──────────────────────────────────────┴──────────────────────────────┘
```

- `Validate`:呼叫後端 `load_workflow()`,把錯誤行高亮
- `Save & Reload`:寫回檔案,後端做 last-known-good 熱載入 (失敗時顯示 toast 並保留舊版)
- 預設 sample render 抓 `Pending` 欄位最高優先的那個 issue 當變數來源

### 4.5 全域 Activity Feed

固定在頁面右下角的小視窗 (可摺疊),呈現所有 issue 的最新事件。類似 Slack 訊息流:

```
┌─────────────────────────────────────┐
│ Activity                       [▼] │
├─────────────────────────────────────┤
│ 14:23:01  MT-3  → tool_call: Bash  │
│ 14:22:58  MT-1  → message: "I'll   │
│           start by reading…"       │
│ 14:22:45  MT-5  → turn_completed   │
│           cost=$0.012, 3 turns     │
│ 14:22:30  MT-9  ⚠ STALL detected   │
└─────────────────────────────────────┘
```

點擊任一行可直接打開該 issue 的 Events tab。

---

## 5. REST + WebSocket API 詳細規格

### 5.1 GET `/api/v1/state` — 整體狀態快照

```json
{
  "tick_at": "2026-05-05T03:14:00Z",
  "config": {
    "max_concurrent_agents": 5,
    "polling_interval_ms": 30000,
    "handoff_state": "in_review",
    "runner_kind": "claude_cli"
  },
  "columns": {
    "pending":       [{"issue": {...}, "queue_rank": 0}, ...],
    "claimed":       [{"issue": {...}, "attempt": {...}}, ...],
    "running":       [{"issue": {...}, "attempt": {...}, "current_turn": 4}, ...],
    "retry_queued":  [{"issue": {...}, "attempt": {...}, "retry_in_ms": 12000}, ...],
    "released":      [{"issue": {...}, "attempt": {...}}, ...]
  },
  "totals": {
    "total_runs_today": 47,
    "total_cost_usd_today": 1.823,
    "active_workers": 3
  }
}
```

### 5.2 GET `/api/v1/issues/{id}` — 單一 issue 詳情 + 歷史

```json
{
  "issue": { ... full Issue dataclass ... },
  "current_attempt": { ... RunAttempt ... },
  "all_attempts": [ ... 歷史 attempts ... ],
  "hints": [
    {"id": 1, "author": "alice", "content": "...", "consumed": true, "consumed_at": "..."}
  ],
  "rendered_prompt_preview": "...",
  "workspace_path": "/.../MT-1",
  "tracker_url": "https://github.com/.../issues/1"
}
```

### 5.3 POST `/api/v1/issues/{id}/hint`

```json
// Request
{
  "author": "alice",
  "content": "之前在 PR #88 試過 useFormState,效能不好;這次改用 useReducer."
}
// Response
{ "id": 7, "consumed": false }
```

行為:寫入 `hints` 表。**不立即影響當前 attempt**。下一次 dispatch 時 `_AgentWorker.run()` 會撈進 prompt context。

> **設計重點**:這是 Symphony 哲學的純粹體現。人類想介入 → 寫文字 → agent 下次跑時讀到 → agent 用工具更新 ticket / 修改 PR / 加 comment。**Dashboard 沒有任何按鈕直接改 tracker 內容**。

### 5.4 POST `/api/v1/issues/{id}/pause` / `/resume`

```json
// Pause request — cooperative
{ "reason": "等 PR #99 合併後再繼續" }
// 行為:設定 attempt.paused_until = 一個遠未來時間;orchestrator 的 _reconcile 看到後會 stop 該 worker,改放 RETRY_QUEUED。
//      `resume` 把 paused_until 抹掉,下個 tick 重排。

// 注意:這跟 abort 不同 — pause 保留 session_id,resume 後可從 Claude session 接續。
```

### 5.5 POST `/api/v1/issues/{id}/abort` / `/retry`

```
abort:  立刻停 worker、attempt -> RELEASED with terminal_reason=ABORTED (新增的 enum)
retry:  即使先前 RELEASED 也強制 enqueue 新 attempt (清掉 RELEASED 記錄,重設 attempt counter)
```

### 5.6 POST `/api/v1/priority` — 拖拉重排

```json
{
  "ordered_issue_ids": ["id-MT-3", "id-MT-1", "id-MT-7", ...]
}
```
寫入 `priority_overrides` 表;`_dispatch` 排序時優先讀 override,再 fall back 到 SPEC 規定的 `(priority, created_at, identifier)`。

### 5.7 PATCH `/api/v1/config` — 即時調整 runtime knobs

```json
{
  "max_concurrent_agents": 10,
  "polling_interval_ms": 10000
}
```
**只能改特定白名單欄位**(不允許從 UI 改 tracker.api_key、handoff_state 之類牽動語意的設定 — 那種要走 WORKFLOW.md 編輯流程)。

### 5.8 GET / PUT `/api/v1/workflow`

- `GET`:回傳目前 WORKFLOW.md 純文字 + 當前 parsed config + 最近一次 reload 結果 (success / failed_with_error)
- `PUT`:寫入新內容並 trigger reload。如果 parse 失敗,**保留舊的 config 在記憶體裡** (last-known-good per SPEC §5),回傳 422 + 錯誤訊息

### 5.9 WebSocket `/api/v1/events`

連線後馬上推送目前所有未終結 attempt 的最近 100 個事件,然後即時推送新事件。

```json
{ "type": "agent_event", "issue_id": "id-MT-1",
  "event": {"kind": "tool_call", "timestamp": "...", "data": {...}} }
{ "type": "fsm_transition", "issue_id": "id-MT-1",
  "from": "claimed", "to": "running" }
{ "type": "config_changed", "config": {...} }
{ "type": "workflow_reloaded", "ok": true }
```

訂閱可選用 `?filter=issue:MT-1,MT-3` query 參數縮窄範圍。

---

## 6. 後端實作:DashboardBridge

新增模組 `symphony_mvp/dashboard/bridge.py`,作為 orchestrator 跟 FastAPI 的中介層。

```python
from collections import deque
from threading import Lock
import sqlite3
import json
from datetime import datetime, timezone

class DashboardBridge:
    """
    Bridges Orchestrator events to dashboard subscribers + persistence.

    Symphony 的 in-memory 狀態仍是 source of truth。本類別只在側邊維護:
      1. 最近 N 個事件的 ring buffer (給新連上的 WebSocket 補歷史用)
      2. SQLite 永久層 (歷史 replay 用)
      3. 訂閱者列表 (WebSocket 連線)
    """
    def __init__(self, orch: Orchestrator, db_path: str, ring_size: int = 500):
        self.orch = orch
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()
        self._rings: dict[str, deque[AgentEvent]] = {}
        self._lock = Lock()
        self._subscribers: list[Callable[[str, AgentEvent], None]] = []
        orch.add_event_listener(self._on_event)

    def _on_event(self, issue_id: str, event: AgentEvent) -> None:
        # 1. ring buffer
        with self._lock:
            self._rings.setdefault(issue_id, deque(maxlen=500)).append(event)
        # 2. persist
        att = self.orch._attempts.get(issue_id)
        attempt_n = att.attempt_number if att else 0
        self.db.execute(
            "INSERT INTO event_records(issue_id, attempt_number, kind, timestamp, data_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (issue_id, attempt_n, event.kind.value,
             event.timestamp.isoformat(), json.dumps(event.data, default=str)),
        )
        self.db.commit()
        # 3. fan out to subscribers
        for sub in self._subscribers:
            try: sub(issue_id, event)
            except Exception: pass

    # ----- Hint store -----
    def add_hint(self, issue_id: str, author: str, content: str) -> int:
        cur = self.db.execute(
            "INSERT INTO hints(issue_id, author, content, created_at, consumed)"
            " VALUES (?, ?, ?, ?, 0)",
            (issue_id, author, content, datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()
        return cur.lastrowid

    def fetch_pending_hints(self, issue_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, author, content, created_at FROM hints"
            " WHERE issue_id = ? AND consumed = 0 ORDER BY id",
            (issue_id,)
        ).fetchall()
        return [{"id": r[0], "author": r[1], "content": r[2], "created_at": r[3]} for r in rows]

    def mark_hints_consumed(self, hint_ids: list[int]) -> None:
        if not hint_ids: return
        placeholders = ",".join("?" * len(hint_ids))
        self.db.execute(
            f"UPDATE hints SET consumed=1, consumed_at=? WHERE id IN ({placeholders})",
            (datetime.now(timezone.utc).isoformat(), *hint_ids),
        )
        self.db.commit()
```

### 6.1 修改既有 MVP 程式碼

需要在 MVP 三個地方加 hook,**改動非常小**:

1. **`Issue.to_template_context(attempt, hints=None)`** — 加一個可選的 `hints` 參數,放進 returned dict
2. **`_AgentWorker.run()`** — render prompt 之前先呼叫 `bridge.fetch_pending_hints(issue.id)`,把結果傳進去;render 完之後 call `bridge.mark_hints_consumed(...)`
3. **`Orchestrator._dispatch()` 排序** — 排序之前先 query `bridge.get_priority_overrides()`,套用上去

這三個 hook 都用「dashboard 不在時為 None」的方式設計,所以**啟用 dashboard 是 opt-in**,不影響 headless / CI 模式。

---

## 7. 安全與授權

**MVP 只需要單機可用,不做外網部署**。但設計時要先把擴展點留好:

| 層 | MVP 做法 | 之後升級空間 |
|----|---------|-------------|
| 認證 | Bearer token (env `DASHBOARD_API_KEY`,空 = 開放) | OIDC / SAML SSO |
| 授權 | 全有或全無 | RBAC: viewer / operator / admin |
| 多使用者 | 單使用者 | 加 `users` 表,hint.author 改 FK |
| WebSocket | 同 token | session-scoped JWT |
| WORKFLOW 編輯 | 任何認證使用者 | 只有 admin |
| 路徑安全 | Tab Workspace 顯示 file 限制在 workspace_root 之下 | 不變 |

**明確不存的東西**:tracker.api_key 不在 dashboard SQLite 裡 — 它從 `WORKFLOW.md` 的 `$ENV` 解析,只活在 Orchestrator 程序記憶體;dashboard API 也不回傳。

---

## 8. 測試策略

| 層 | 測試類型 | 工具 |
|----|--------|------|
| Bridge 單元測試 | hint round-trip、ring buffer 滿、event persist | pytest |
| FastAPI 路由 | TestClient 打每個 endpoint 驗證 schema | pytest + httpx |
| WebSocket 流 | 連上 → 觸發事件 → 收到 → 過濾正確 | pytest-asyncio |
| 整合測試 | 真的跑 orchestrator + EchoRunner + dashboard,驗證 UI 可看到事件 | playwright |
| 前端元件 | Kanban column / Card / Drawer 渲染 | Vitest + RTL |
| E2E | 拖拉 / 加 hint / pause-resume / workflow edit | playwright |

**整合測試最重要的一條**:**驗證 hint 注入機制完整 flow** — `add_hint` → orchestrator 重新 dispatch → render_prompt 看得到 → mark_consumed 後再 dispatch 看不到。

---

## 9. 部署架構

### 9.1 單機 (預設)

```bash
# 1. 啟動 dashboard + orchestrator (同程序)
python -m symphony_mvp.dashboard ./WORKFLOW.md \
    --port 7957 --db ./dashboard.db --api-key "$DASHBOARD_API_KEY"

# 2. 瀏覽器
open http://localhost:7957
```

### 9.2 容器化

```dockerfile
FROM python:3.12-slim
COPY . /app
WORKDIR /app
RUN pip install --no-cache-dir -e ".[dashboard]"
EXPOSE 7957
CMD ["python", "-m", "symphony_mvp.dashboard", "/etc/symphony/WORKFLOW.md", \
     "--port", "7957", "--db", "/var/lib/symphony/dashboard.db"]
```

前端 build 過後是純靜態檔,可由 FastAPI 的 `StaticFiles` 直接 serve,單一 image 一個 port 解決。

### 9.3 反向代理 / SSL

預期使用方在前面放 nginx/Caddy/Traefik,所以 FastAPI 不自己處理 TLS。WebSocket upgrade 走相同 path。

---

## 10. 實作工作分解 (估時)

| 工作項 | 估時 | 相依 |
|-------|------|-----|
| **W1 後端骨架** | 2 天 | MVP |
| `DashboardBridge` 類別 + SQLite schema | 0.5d | |
| 三個 MVP hook (Issue/Worker/Orchestrator) | 0.5d | |
| FastAPI 路由 (state/issues/hint/pause/abort/retry/priority) | 0.5d | |
| WebSocket 端點 + 訂閱機制 | 0.5d | |
| **W2 前端骨架** | 3 天 | W1 |
| Vite + React + Tailwind + Zustand 設定 | 0.5d | |
| Kanban 五欄佈局 + 卡片元件 | 0.5d | |
| Issue Drawer + tabs | 1d | |
| WebSocket client + 自動重連 | 0.5d | |
| Activity feed + filter bar | 0.5d | |
| **W3 拖拉與動作** | 1.5 天 | W2 |
| @dnd-kit 整合 + priority override | 0.5d | |
| Pause / Resume / Abort / Retry 動作 | 0.5d | |
| Hint composer + 已注入歷史顯示 | 0.5d | |
| **W4 進階觀察** | 2 天 | W2 |
| xterm.js Events tab | 0.5d | |
| Prompt tab + Monaco editor | 0.5d | |
| Replay tab (歷史回放) | 1d | |
| **W5 Workflow 編輯** | 1 天 | W2 |
| Monaco YAML+MD editor | 0.5d | |
| Validate / Save & Reload (last-known-good) | 0.5d | |
| **W6 測試與文件** | 1.5 天 | 全部 |
| Playwright E2E (核心 5 條 path) | 1d | |
| README + 部署手冊 | 0.5d | |

**合計 11 天**,單人;可平行化前後端到 ~7 天。

---

## 11. Definition of Done (V1.0)

dashboard v1.0 視為可發布,當且僅當:

1. ✅ Orchestrator 跑著的時候,所有 5 個欄位 issue 都正確分類顯示
2. ✅ 任何 `AgentEvent` 在 < 1 秒內出現在前端 (WebSocket)
3. ✅ 加 hint → 等下次 attempt → render_prompt 預覽看到 hint 內容 → consumed_at 被填
4. ✅ 拖拉 Pending 順序 → 下次 dispatch 確實照新順序進
5. ✅ Pause → worker 在 5s 內停 → Resume → 同 session_id 接續
6. ✅ Abort → worker 立即終止 → 卡片移到 Released 標 ABORTED
7. ✅ Retry 已 RELEASED → 開新 attempt
8. ✅ 編輯 WORKFLOW.md 故意寫錯 YAML → save 失敗顯示錯誤、orchestrator 仍用舊設定運作
9. ✅ Tracker API key 不會出現在任何 API response (用 `--api-key=$LEAKED` 啟動然後抓所有路由的 response 看)
10. ✅ Bearer token 無效時所有 REST + WS 都回 401/403
11. ✅ 把 dashboard 整個關掉(只剩 MVP),`python -m pytest` 仍 29/29 pass — 證明完全 opt-in,沒污染核心

---

## 12. 跟 Symphony 既有 MVP 的兼容性聲明

| MVP 行為 | Dashboard 是否影響 | 說明 |
|---------|------------------|------|
| `python -m symphony_mvp ./WORKFLOW.md` | **不影響** | dashboard 是另一個 entrypoint `python -m symphony_mvp.dashboard` |
| 既有 29 個單元測試 | **不影響** | 三個 hook 都可選參數 / opt-in subscribe |
| `EchoRunner` / `ClaudeCLIRunner` / `AnthropicAPIRunner` | **不影響** | runner 介面完全沒動 |
| `Tracker` 唯讀原則 | **強化** | dashboard 也唯讀,並且把 hint 機制做成 prompt 注入而非 tracker 寫入 |
| 內部 FSM 跟 tracker 狀態雙軌分離 | **強化** | dashboard 五欄就是 FSM 狀態,跟 tracker state 顯式分離 |
| `WORKFLOW.md` 仍是 source of truth | **強化** | 線上編輯 + last-known-good 重載 |
| Hot reload 語意 | **保留** | dashboard reload 失敗回退舊版,跟 SPEC 一致 |

---

## 13. 為什麼這樣設計符合 Symphony 哲學?

回到開頭 user 強調的那句話:

> Ticket 的寫入 (狀態轉換、留言、PR 連結) 全部由 Coding Agent 自己用工具完成。這是 Symphony 跟其他 workflow engine 最大的不同 — 它把「業務邏輯」全部推到 Agent 的 prompt 裡。

本 dashboard 嚴格執行這個哲學的具體做法:

1. **沒有「Update Ticket Status」按鈕**。狀態變化由 reconcile 從 tracker 拉,以及由 agent 自己用工具寫回 tracker 後反映。
2. **沒有「Add Comment to Ticket」按鈕**。如果想跟 issue 對話,只能 add hint;hint 進 prompt → agent 看到 → agent 用 `gh issue comment` 寫回。
3. **沒有「Move to In-Review」按鈕**。Handoff 是 agent 的職責 — agent 完成工作後自己用工具加上 `in-review` label。
4. **沒有「Edit Issue Title / Description」按鈕**。要改 issue 內容請去 GitHub / Linear UI 改;dashboard 是 daemon 觀察台,不取代 tracker。
5. **拖拉重排的是 dispatch 優先順序,不是 tracker 的 priority 欄位**。Symphony orchestrator 唯讀原則在 dashboard 層繼續守住。

換句話說:**dashboard 加的所有功能,都是「給 orchestrator 看的訊號」或「給下一次 prompt 看的素材」,從來不是「給 tracker 寫入的命令」**。這是它跟 vibe-kanban、ai-agent-board 那種 「UI 直接改 task DB」 的根本差異,也是 Symphony 的核心精神得以延續的關鍵。
