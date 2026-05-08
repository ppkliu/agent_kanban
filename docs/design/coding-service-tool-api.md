# Coding Service Tool API — Symphony × Q3 設計分析 (Stage 1)

> **狀態**:設計分析,**還沒落地實作**。
> **檔案性質**:Stage 1 的產出,目的是把 Q3 (`Q3_OpenCode作為Tool的設計方案.md`) 的方案
> 對齊到目前 Symphony 既有架構,讓使用者在動代碼之前先 review 對應關係。
> **下一步**:讀完 §11 落地路線後,Phase A 的實作另開 plan。

---

## 1. 動機

Symphony Dashboard 目前對外暴露兩種介面:

1. **人類觀察介面** — React SPA + `/api/v1/state` / `/api/v1/issues/*` (面向 operator)
2. **內部 orchestrator-as-tracker-consumer** — Symphony 自己 poll tracker、dispatch agent

但 Q3 (`Q3_OpenCode作為Tool的設計方案.md`,2026-04) 點出了第三種關鍵介面缺口:**讓上游 LLM agent
把 Symphony 當成 tool 來呼叫**。具體場景例如:Claude / GPT / 自家 LLM 在自己的 reasoning loop 裡,
透過 OpenAI function calling / Anthropic tool use 呼叫 Symphony,把 coding task 委派出去。

這跟 Symphony 「autonomy-first」的設計哲學 ([README.md](../../README.md)) 完全一致 —
讓 agent 可以自主長跑,operator 只負責觀察。Q3 把這個哲學從「人 vs orchestrator」推進到
「上游 agent vs orchestrator」,讓 Symphony 變成一個可被組合的編程後端。

跟 Phase 2 (cross-container,見 [`opencode-two-container-analysis.md`](opencode-two-container-analysis.md))
的差異:

- Phase 2 是**內部 transport** — orchestrator 跟 opencode 之間怎麼跑
- 本文是**外部契約** — 上游 LLM agent 跟 Symphony 之間怎麼接

兩件事正交。本文不依賴 Phase 2 落地,Phase 2 也不依賴本文。

---

## 2. Q3 設計核心 (一頁版)

Q3 的核心三個論點 (壓縮自 587 行):

1. **6 個語意明確的 tool**(不是把 opencode 79+ 個內部工具直接暴露)
   ```
   list_repos()                         → Repo[]
   inspect_repo(repo)                   → { agents_md, structure, languages }
   submit_coding_task(input)            → { task_id }    (< 1s 返回)
   check_task_status(task_id)           → { status, stage, progress }
   get_task_result(task_id)             → { summary, files_changed, ... }
   cancel_task(task_id)                 → boolean
   ```

2. **Fire-and-check 非同步分裂** — `submit_coding_task` 立刻返回 task_id,實際工作丟到 backend;
   上游 agent 用 `check_task_status` 漸進式問狀態。**避免上游 agent timeout、context 爆炸、
   無法平行任務、無法取消**。

3. **Result shape 最小化** — 不要把 opencode 整段 transcript 回給上游;只回上游 agent
   reason 下一步需要的東西:summary (3-5 句)、files_changed (結構化)、tests_run、blockers、
   follow_ups。**~500 tokens 就解釋整個 task 結果**,可以大量平行而不爆 context。

額外的治理面 (Q3 §8) — sandbox、quota、audit、idempotency — 是 production layer 的事,
不是 MVP。本文也採同樣分期。

---

## 3. Symphony 既有架構盤點

把 Q3 對齊到 Symphony 之前,先確認哪些東西已經有了。下面每一條都附 file:line ref,
讓實作者直接對照。

| Q3 概念 | Symphony 既有對應 |
|---|---|
| **Task FSM** (pending / running / done / failed) | `RunState` enum + Orchestrator 維護 `_attempts: dict[str, RunAttempt]` ([`symphony_mvp/orchestrator.py:86`](../../symphony_mvp/orchestrator.py)) |
| **長執行記憶** (task_id 跨 call 持續) | `Issue.id` 已是穩定 PK;orchestrator 跟 attempt_history 都用它 |
| **Async 啟動 + cancel** | `Orchestrator.abort(issue_id)` ([`symphony_mvp/orchestrator.py:481`](../../symphony_mvp/orchestrator.py)) + `abort_all` (`emergency_stop`) |
| **跨 attempt 紀錄** | `attempt_history` SQLite 表 ([`symphony_mvp/dashboard/schema.sql:54-72`](../../symphony_mvp/dashboard/schema.sql)) |
| **每個 attempt 的 event stream** | `event_records` 表 + `DashboardBridge.list_events` ([`symphony_mvp/dashboard/bridge.py`](../../symphony_mvp/dashboard/bridge.py)) |
| **動態加 task** | `InMemoryTracker.add(issue)` ([`symphony_mvp/tracker.py:66`](../../symphony_mvp/tracker.py)) — orchestrator 下一個 tick 會撿到 |
| **REST + auth scaffolding** | FastAPI + Bearer token middleware ([`symphony_mvp/dashboard/server.py`](../../symphony_mvp/dashboard/server.py)) |
| **新增頂層 POST 端點的 pattern** | `/api/v1/emergency_stop` ([`symphony_mvp/dashboard/server.py:439`](../../symphony_mvp/dashboard/server.py)) — Tool API 走完全相同的 pattern |
| **觀察介面 (給人看的)** | React SPA + WebSocket `/api/v1/events` — Tool API 加上後可以共存 |

**結論**:Q3 的 Task Manager + 6-tool 模型,Symphony 的 Orchestrator 已經實作了大概 90%。
缺的是「**對外 RESTful 包裝層**」(包含 stage 翻譯 + result derivation 兩個小 module),
不是「重寫一個 task manager」。

---

## 4. Q3 → Symphony 對應表

| Q3 概念 | Symphony 對應 | 已存在? | 缺口 |
|---|---|---|---|
| Tool API HTTP | `dashboard/server.py` FastAPI router | ✅ | 新增 `/api/v1/tools/*` namespace router |
| Task Manager | `Orchestrator` + `_attempts` | ✅ | 加 `submit_coding_task → InMemoryTracker.add()` 的 wrapper |
| Task store | `attempt_history` 表 + 內存 `_attempts` | ✅ | (Phase C) 加 `idempotency_keys` 表 |
| OpenCode pool | `OpenCodeRunner` (Popen-based, single-instance) | ✅ | Phase 2 (cross-container) 處理;本 plan 不動 |
| Stage 翻譯 | (none) | ❌ | **新增** `dashboard/stage.py` |
| Result shape | `attempt_history` row 不含 summary / files_changed | partial | **新增** `dashboard/task_result.py` derivation 函式 |
| Mode 隔離 | `runner_allowed_tools` 全域,沒 per-task | partial | (Phase B) 新增 per-task runtime override |
| Cancel | `Orchestrator.abort` | ✅ | 直接 reuse |
| Multi-repo | `tracker.py` Protocol,單 instance | partial | (Phase C) repo pool 設計 |
| Idempotency | (none) | ❌ | (Phase C) `idempotency_key` |
| Audit | dashboard event log = audit-light | partial | (Phase C) tighten audit fields |
| Quota | (none) | ❌ | (Phase C) per-caller / per-repo / global |

只有 ✅ 跟 partial 的是 Phase A 要動的;❌ 都是 Phase B/C。

---

## 5. 6 個 Tool API 端點規格

所有端點走 `POST /api/v1/tools/<name>` (Q3 用法是 RPC-over-POST,符合
function-calling 的請求-回應形狀)。Bearer auth 跟現有 routes 共用。

每個端點下面附:
- **路徑** + 對應的 Symphony 既有 method
- **請求/回應 schema** (Pydantic shape)
- **Tool description** (寫給上游 LLM 看的、Q3 §7.4.1 風格的 good-vs-bad 範例)

### 5.1 `POST /api/v1/tools/list_repos`

**對應**:讀 `Workflow.config.tracker_kind` + `tracker_repo`。Phase A 只支援 single-tracker,
回傳一筆;Phase C 才會擴成 multi-repo pool。

**請求**:無 body (或 `{}`)

**回應**:
```json
{
  "repos": [
    {
      "id": "memory" | "github:owner/name",
      "name": "string",
      "default_mode": "build",
      "allowed_modes": ["plan", "build", "review"]
    }
  ]
}
```

**Tool description (給 LLM 看)**:
> List all repositories that this Coding Service can operate on. Call this
> first if you don't know what `repo` value to pass to `submit_coding_task`.
> Most setups expose 1 repo; multi-repo support is opt-in.

### 5.2 `POST /api/v1/tools/inspect_repo` (Phase B)

**對應**:讀 `WorkspaceManager.root` 下的 `AGENTS.md` (fallback `CONTRIBUTING.md`),產生 tree summary。

**請求**:`{ "repo": "string" }`

**回應**:
```json
{
  "repo": "string",
  "agents_md": "string | null",
  "structure": { "tree": "string", "file_count": 1234 },
  "languages": ["python", "typescript"],
  "default_mode": "build",
  "allowed_modes": ["plan", "build", "review"]
}
```

**Tool description**:
> Returns the repo's `AGENTS.md` (project-level coding contract) and a tree
> summary. **Call this before `submit_coding_task` for any non-trivial work**
> so your task description matches the repo's conventions. Cheap (no agent
> spawned) — should always be free of cost / latency concerns.

### 5.3 `POST /api/v1/tools/submit_coding_task`

**對應**:建一個新的 `Issue`,呼叫 `InMemoryTracker.add()`,讓 orchestrator 下個 tick 撿。

**請求**:
```json
{
  "task": "string (required, self-contained)",
  "repo": "string (required)",
  "mode": "plan | build | review (default: build)",
  "files_hint": ["string", "..."],
  "idempotency_key": "string (optional, Phase C)",
  "parent_task_id": "string (optional, for task chains)"
}
```

**回應** (< 1s):
```json
{ "task_id": "tsk_abc123", "status": "pending" }
```

**Tool description (Q3 §7.4.1 改寫)**:
> Delegate a coding task to the Coding Service. Returns a `task_id` immediately;
> use `check_task_status` to poll progress. Use this for: implementation,
> refactoring, test writing, debugging, code review.
>
> **Writing a good `task` description** (the most important field):
>
> ```
> ### Bad
> "Fix the login bug"
>
> ### Good
> "In src/auth/login.ts, the rate limiter at line ~45 calls reset() on every
>  request instead of every IP. The intended behavior is per-IP throttling.
>  Fix the logic and add a regression test."
> ```
>
> Be specific. Self-contained. State the expected behavior. Cite file paths
> when known. The agent does NOT see your prior context — it sees only `task`.

### 5.4 `POST /api/v1/tools/check_task_status`

**對應**:讀 `Orchestrator._attempts[task_id]` (透過一個新 helper `Orchestrator.get_attempt_summary`),
最近一個 event 從 `bridge.list_events` 拿,丟給 `translate_event_to_stage`。

**請求**:`{ "task_id": "string" }`

**回應**:
```json
{
  "task_id": "tsk_abc123",
  "status": "pending | running | done | failed | cancelled",
  "stage": "queued | exploring_codebase | modifying_files | running_tests | summarising | done",
  "progress": 0.0,
  "current_turn": 3,
  "max_turns": 20,
  "started_at": "2026-05-08T10:23:00Z",
  "duration_ms": 45000,
  "next_poll_after_ms": 10000
}
```

`progress` 是 best-effort 估計值 (turn / max_turn,或 stage 對應的固定點)。
`next_poll_after_ms` 是給上游的 hint,免得它每秒打。

**Tool description**:
> Check the status of a previously-submitted task. Cheap to call repeatedly;
> respects the `next_poll_after_ms` hint to avoid hammering. The `stage` field
> tells you what the agent is *currently doing* — useful for deciding whether
> to wait longer or `cancel_task` and retry with `files_hint`.
>
> Status meanings:
> - `pending`: queued, agent not yet dispatched
> - `running`: agent is actively working
> - `done`: success — call `get_task_result`
> - `failed`: terminal error — call `get_task_result` for the failure summary
> - `cancelled`: you (or another caller) ran `cancel_task`

### 5.5 `POST /api/v1/tools/get_task_result`

**對應**:讀 `attempt_history` row + filter `event_records` for `tool_call(edit/write)` /
`tool_result` for tests / final `assistant_text`,丟給 `derive_result()`。

**請求**:`{ "task_id": "string" }`

**回應** (TaskResult shape — Q3 §7.4.4):
```json
{
  "task_id": "tsk_abc123",
  "status": "done | failed",
  "summary": "Added GET /health in src/routes/health.ts. Modified src/server.ts to register route. 1 file added, 1 modified. All tests pass.",
  "files_changed": [
    { "path": "src/routes/health.ts", "change": "added", "lines_changed": 23 },
    { "path": "src/server.ts", "change": "modified", "lines_changed": 4 }
  ],
  "tests_run": { "passed": 47, "failed": 0, "framework": "vitest" },
  "blockers": [],
  "follow_ups": ["Consider adding a separate readiness probe at /ready"],
  "detailed_url": "http://localhost:17957/issues/tsk_abc123",
  "cost_usd": 0.012,
  "duration_ms": 47800
}
```

**關鍵設計** (Q3 §7.4.4):**不要丟 transcript 整段**。`detailed_url` 給人類觀察,
`summary` 給 LLM 用 ~500 tokens 決策下一步。

**Tool description**:
> Get the final result of a completed task. **Only callable when status ∈
> {done, failed}**. Returns a structured summary, file changes, test results,
> blockers, and follow-up suggestions. The full event transcript is NOT in
> the response — open `detailed_url` if you need it.

### 5.6 `POST /api/v1/tools/cancel_task`

**對應**:`Orchestrator.abort(task_id, message=reason)`。

**請求**:`{ "task_id": "string", "reason": "string (optional)" }`

**回應**:
```json
{ "ok": true, "was_running": true, "task_id": "tsk_abc123" }
```

**Tool description**:
> Cancel a running task. Idempotent — calling on an already-completed task
> returns `was_running: false`. Use when:
> - The task is taking too long and you want to retry with `files_hint`
> - You've changed your mind about the approach
> - The user (above this agent) interrupted

---

## 6. Stage 翻譯 (新增 `dashboard/stage.py`)

把 Symphony 的 `AgentEventKind` (低階) 翻譯成上游 LLM 看得懂的 `Stage` (高階)。
純函式,容易測試。

```python
# symphony_mvp/dashboard/stage.py
from typing import Literal
from ..agent_runner import AgentEvent, AgentEventKind

Stage = Literal[
    "queued",
    "exploring_codebase",
    "modifying_files",
    "running_tests",
    "summarising",
    "done",
    "failed",
]

_TEST_CMD_RE = re.compile(r"\b(pytest|jest|vitest|go test|cargo test|npm test)\b")

def translate_event_to_stage(event: AgentEvent) -> Stage | None:
    """Translate one event into a Stage if it implies a state transition.
    Returns None if this event does not change the stage."""
    if event.kind == AgentEventKind.TOOL_CALL:
        tool = event.data.get("tool", "")
        if tool in ("grep", "find", "read"):
            return "exploring_codebase"
        if tool in ("edit", "write", "create"):
            return "modifying_files"
        if tool == "bash":
            cmd = event.data.get("input", {}).get("command", "")
            if _TEST_CMD_RE.search(cmd):
                return "running_tests"
    if event.kind == AgentEventKind.TURN_COMPLETED:
        return "summarising"
    if event.kind == AgentEventKind.DONE:
        return "done"
    if event.kind == AgentEventKind.ERROR:
        return "failed"
    return None
```

`check_task_status` 的 stage 計算流程:
1. 從 `bridge.list_events(task_id, attempt_number=current)` 取最近 N 筆
2. 從新到舊找第一個非 None 的 `translate_event_to_stage` 結果
3. 找不到 → 看 attempt 狀態:CLAIMED → `queued`,RUNNING → `exploring_codebase` (預設)

---

## 7. TaskResult 形狀 (新增 `dashboard/task_result.py`)

```python
# symphony_mvp/dashboard/task_result.py
@dataclass
class TaskResult:
    task_id: str
    status: Literal["done", "failed", "cancelled"]
    summary: str
    files_changed: list[FileChange]
    tests_run: TestRun | None
    blockers: list[str]
    follow_ups: list[str]
    detailed_url: str
    cost_usd: float
    duration_ms: int
```

**`summary` 的來源** (優先序):
1. 最後一個 `assistant_text` event 的最後 3 句
2. (fallback) `terminal_reason` + 簡單模板:`"Task {reason} after N turns. Modified M files."`

**`files_changed`**:從 events 過濾 `kind == TOOL_CALL && tool ∈ {edit, write, create, delete}`,
合併同一個 `path` 的多次 modification,從 `tool_result.lines_changed` (如果有) 加總。

**`tests_run`**:從 events 過濾 `kind == TOOL_RESULT && precedingToolCall.command matches _TEST_CMD_RE`,
parse 輸出抓 passed/failed 數字 (regex per framework)。

**`blockers` / `follow_ups`**:啟發式 — `summary` 裡的句子 match `/(blocked by|requires|missing)/i`
丟到 blockers,match `/(consider|suggest|todo|follow.?up)/i` 丟到 follow_ups。**不完美但夠用,
未來可以換成 LLM judge**。

**`detailed_url`**:`f"{public_dashboard_url}/issues/{task_id}"`,`public_dashboard_url` 從 env
`PUBLIC_DASHBOARD_URL` 讀,fallback `http://localhost:17957`。

---

## 8. Mode 隔離 (Phase B)

| `mode` | 對應 runner config |
|---|---|
| `plan` | `allowed_tools = ["bash", "read", "grep"]` (不 edit/write) |
| `build` | `allowed_tools = ["bash", "read", "edit", "write"]` (current default) |
| `review` | `allowed_tools = ["read", "grep"]` (純 read-only) |

實作方式:Phase B 在 `OpenCodeRunner` 加 per-call override (constructor 已經接受 `allowed_tools`,
只要在 `submit` 路徑記下 task 的 mode、dispatch 時 build runner with override 即可)。

**這是權限的硬隔離**,不是 prompt 提示 — 直接走 opencode CLI 的 `--allowed-tools` 旗標,
runner 從根上限制能用的工具。Q3 §7.4.2 強調過這點:**mode 不能只是字串**。

---

## 9. Idempotency (Phase C)

新增 `idempotency_keys` 表:
```sql
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT    PRIMARY KEY,
    task_id     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL
);
```

`submit_coding_task` 流程:
1. 若 body 有 `idempotency_key`,先查表
2. 命中 + 未過期 → 回原 `task_id` (HTTP 200,不是 409)
3. 沒命中 → 正常建 issue,寫表 (TTL 1h),回新 task_id

對應的清理 cron:每個 tick 清過期 key (跟 priority_overrides 共用清理路徑)。

---

## 10. 跟 Phase 2 (cross-container) 的關係

**完全正交**。

| 維度 | Phase 2 (cross-container) | Tool API (本文) |
|---|---|---|
| 動機 | 把 opencode 從 dashboard 容器隔出去 (爆炸半徑) | 對外暴露給上游 LLM agent |
| 改動位置 | `OpenCodeRunner` 內部 transport | `dashboard/` 新加 router + 兩個 helper module |
| 對其他人的影響 | 對外 API 不變 | runner 不變 |
| 落地順序 | 不重要 — 兩條路可平行 | 不重要 |

只要記得一條:**Tool API 跟現有 Popen-based `OpenCodeRunner` 完全相容**。
Phase 2 落地後,Tool API 端不用改。

---

## 11. 落地路線 (Phase A / B / C)

對應 Q3 §10,但細節對齊到 Symphony 的具體 module 邊界。

### Phase A — MVP 端點 (~1 day)

**目標**:5 個 endpoint 上線,跟既有 orchestrator/bridge 接好,**先確認上游 LLM 真的能接得起來**。

範圍:
- `list_repos` (5.1) — 單一 repo 即可
- `submit_coding_task` (5.3) — 不含 mode、不含 idempotency
- `check_task_status` (5.4) — stage 用簡化版 (3 階段:queued / running / done)
- `get_task_result` (5.5) — summary + files_changed 即可,blockers/follow_ups 留空陣列
- `cancel_task` (5.6)

跳過:`inspect_repo` (5.2)、stage 翻譯細節 (§6)、result derivation 進階欄位 (§7)、mode (§8)。

新增檔案:
- `symphony_mvp/dashboard/tool_api.py` — FastAPI router (~150 LOC)
- `tests/test_tool_api.py` — pytest TestClient,~10 cases (一個 endpoint 約 2 case)

修改檔案:
- `symphony_mvp/dashboard/server.py` — `app.include_router(tool_router)`
- `README.md` + `docs/README.zh-TW.md` — feature map 加一條 "Tool API for upstream LLM agents"
- `docs/todolist/post-mvp-gaps.md` — Phase A 標記 done

### Phase B — Polish (~1 day)

範圍:
- `inspect_repo` (5.2)
- 完整 stage 翻譯 (§6)
- 完整 result derivation (§7) — files_changed 細節 + tests_run + 啟發式 blockers/follow_ups
- mode (§8) per-task allowed_tools override

新增檔案:
- `symphony_mvp/dashboard/stage.py`
- `symphony_mvp/dashboard/task_result.py`
- `symphony_mvp/dashboard/repo_inspect.py`
- `tests/test_stage.py`、`tests/test_task_result.py`

### Phase C — Governance (per-need)

範圍:
- `idempotency_keys` (§9)
- Multi-repo pool
- Quota (per-caller / per-repo / global)
- Audit log 強化
- Sandbox per Q3 §8.1 (三層防線)

各自有自己的 plan + commit batch。

---

## 12. 對齊 Symphony 既有測試

新增的測試**不影響現有 80 個 test**:

- 既有 `tests/test_dashboard_extras.py` 跑 emergency_stop endpoint test 的 pattern,
  Tool API 完全照 mirror (TestClient + InMemoryTracker fixture + 直接 inject `RunAttempt` 到 `_attempts`)
- Phase A 的 ~10 個 case 加進同一個 conftest fixture
- 既有 `OpenCodeRunner` 8 個 unit test 沒有變動

**驗證 Phase A 落地**:
```bash
.venv/bin/python -m pytest -q
# expect: 90 passed (80 + 10)
```

---

## 13. 已知未確定項

- [ ] **Multi-repo 支援** — Symphony 目前是 single-tracker;Q3 §8.2 的 multi-repo pool 需要 tracker
  registry。Phase A 走 single-tracker 路徑,Phase C 處理。
- [ ] **Quota / audit / sandbox** — Q3 §8.1/§8.3/§8.4。現在沒 quota,審計只到 dashboard event log。
  Phase C。
- [ ] **「issue」vs「task」命名** — Symphony 內部 `Issue`,Q3 / 上游 LLM 看到的是 `task`。
  決定:**內部不改名,API 邊界翻譯**。`tool_api.py` 把 issue.id 映射成 `task_id` 字串 (一樣的值,
  概念上是 alias)。對 dashboard SPA 沒影響。
- [ ] **OpenAPI 自動產生的 tool description 對 LLM 有效性** — FastAPI 的 `/openapi.json` 會
  把 docstring 當 description。需要驗 Anthropic / OpenAI tool calling 直接吃這個格式有沒有問題。
  Phase A 早期可以手動 export 一份 OpenAPI 餵 LLM 試試。
- [ ] **`task_id` 格式** — 用 `Issue.id` 直接當 task_id (不需新增 ID 系統)。短碼如 `tsk_xxx`
  可選,但會破壞跟 dashboard issue ID 的可追蹤性。決定:用 Issue.id,對 LLM 不漂亮但對 ops
  最簡單。

---

## 14. 反模式提醒 (Q3 §9 改寫,對齊 Symphony 細節)

實作 Phase A 之前要先記住這些:

| 反模式 | Symphony 場景下的具體警示 |
|---|---|
| 把 opencode 整套 79+ 內部 tool 暴露 | 我們暴露的是 Symphony 的 6 個 tool,不是 opencode 的;這條已經結構性避開 |
| 同步 `submit` 等到 done 才回 | Phase A 一定要 fire-and-check;HTTP 連線不要被 hold 住等 orchestrator |
| 把 SSE 整段 forward 給上游 LLM | 不要從 `/api/v1/tools/check_task_status` 回完整 event stream;只回 summary + stage |
| 上游 LLM 直接決定 opencode agent (build/plan/...) | mode 限定 enum (`plan`/`build`/`review`),不接受任意字串 |
| 不限制 repo / 隨意傳 path | `list_repos` 是 enum 來源;`submit_coding_task` 的 `repo` 欄位驗 enum 命中 |
| Result 包含 opencode 內部 message struct | `TaskResult` 是純 Symphony 設計的 shape,跟 opencode event kind 解耦 |
| 沒 task_id 直接靠 session_id | task_id = Issue.id (Symphony 自家 ID),不洩漏 opencode session_id |
| 共用一個 opencode session 跨 task | Symphony 已經是「一個 issue 一個 attempt」,結構性避開 |

最後一點 Q3 也提醒過:每個 tool 描述要寫得**像給人看的工作說明書,不是 schema dump**。
§5 的 description 都採這個寫法。

---

## 附錄 A:對照圖

```
                     ┌────────────────────────────┐
   外部 LLM agent ───→│ POST /api/v1/tools/...     │
   (Claude/GPT/...)    │                            │
                       │ tool_api.py                │
                       │  (Phase A: ~150 LOC)       │
                       └─────┬──────────────────────┘
                             │  delegates to existing
                             ▼
   ┌──────────────────────────────────────────────────┐
   │ Orchestrator + DashboardBridge (既有)              │
   │   _attempts dict / abort / pause / resume / retry  │
   │   attempt_history SQLite + event_records           │
   │   InMemoryTracker.add(issue) for new tasks         │
   └─────┬──────────────────────────────┬──────────────┘
         │                              │
         ▼                              ▼
   OpenCodeRunner (Popen,         dashboard SPA + WS
   Phase 1 single-container;       (人類觀察介面,既有)
   Phase 2 跨容器:見 two-container-analysis.md)
```

外部 LLM agent 看到的是 6 個語意動作,不知道下面是 Symphony orchestrator + opencode pool。
這就是 Q3 講的「**好的工具抽象**」。
