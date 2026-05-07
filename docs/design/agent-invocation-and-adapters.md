# Agent Invocation & Adapter Design

> 三個問題一份文件:
> 1. Symphony 怎麼呼叫一個 agent?(call chain)
> 2. 為什麼這個框架特別強調 DPO?(訓練資料蒐集點)
> 3. 怎麼接一個新 agent backend(以 [opencode](https://github.com/sst/opencode) 為例)

---

## 1. Agent 呼叫架構 — 從 issue 到 AgentEvent stream

Symphony 的呼叫鏈刻意做得很扁:**orchestrator 不管 agent 怎麼跑、agent 不管 issue 怎麼來**。中間靠 `AgentRunner` Protocol 隔開,protocol 只有一個方法。

### 1.1 整條 call chain

```
Tracker.fetch_active()                               # tick step 3
        │
        ▼ list[Issue]
Orchestrator.tick()                                   # tick step 4: sort + override
        │
        ▼ priority + override-resolved candidates
Orchestrator._dispatch(candidates, now)               # tick step 5
        │
        │ for each candidate within concurrency budget:
        │   - check FSM (UNCLAIMED / RETRY_QUEUED / paused_until / blocked_by)
        │   - mint a new RunAttempt
        │   - record_attempt_number on bridge (so events get tagged correctly)
        │   - notify_fsm_transition(UNCLAIMED → CLAIMED)
        ▼
_spawn_worker(issue, attempt)                         # background thread
        │
        ▼
_AgentWorker.run()                                    # the worker thread body
        │
        │ ┌────────────────────────────────────────┐
        │ │ 1. workspaces.ensure(...)              │  ← SPEC §7 invariants
        │ │    + after_create_hook (fatal)         │
        │ │ 2. bridge.fetch_pending_hints(...)     │  ← opt-in HITL
        │ │ 3. issue.to_template_context(          │
        │ │       attempt_number, hints=...)       │
        │ │ 4. render_prompt(workflow, ctx)        │  ← Jinja2 strict
        │ │ 5. bridge.mark_hints_consumed(...)     │  ← only on render success
        │ │ 6. before_run_hook (fatal)             │
        │ └────────────────────────────────────────┘
        ▼
runner.run(                                           # ← THIS is the agent boundary
    prompt=prompt,
    workspace_path=ws.path,                           # subprocess cwd MUST equal this
    max_turns=cfg.max_turns,
    session_id=attempt.session_id,                    # for resume on retry
) -> Iterator[AgentEvent]
        │
        │ runner streams events back, one per yield:
        │   TURN_STARTED, ITEM_STARTED, MESSAGE_DELTA, TOOL_CALL,
        │   TOOL_RESULT, TURN_COMPLETED, USER_INPUT_REQUIRED, ERROR, DONE
        ▼
Orchestrator._on_worker_event(issue_id, event)        # back on caller thread
        │   - mark_event() for stall detection
        │   - bump turns_consumed on TURN_STARTED
        │   - accumulate cost_usd on TURN_COMPLETED
        │   - capture session_id for resume
        │   - bridge.notify_fsm_transition(CLAIMED → RUNNING)
        │   - fan out to all add_event_listener subscribers (incl. dashboard)
        │
        ▼ when DONE / ERROR / max_turns:
Orchestrator._on_worker_done(issue_id, reason)
        │   - choose TerminalReason (AGENT_FINISHED / MAX_TURNS / ERROR / USER_INPUT_REQUIRED / ...)
        ▼
Orchestrator._release(att, reason, ...)
        │   - retry-eligible? → RETRY_QUEUED with retry_after = now + backoff
        │   - else            → RELEASED (terminal)
        │   - bridge.record_finalised_attempt(att)    ← survives in-memory eviction
        │   - bridge.notify_fsm_transition(... → released / retry_queued)
        │   - after_run_hook (non-fatal)
```

### 1.2 `AgentRunner` Protocol — 唯一的契約

```python
@runtime_checkable
class AgentRunner(Protocol):
    def run(
        self,
        *,
        prompt: str,
        workspace_path: str,    # subprocess cwd
        max_turns: int,
        session_id: str | None = None,
    ) -> Iterator[AgentEvent]:
        ...
```

兩條硬規定:
- **subprocess `cwd` 必須等於 `workspace_path`** (SPEC §7 invariant #1)
- **必須串流 yield `AgentEvent`** — 不能等到結束才一次回傳;orchestrator 靠 `last_event_at` 偵測 stall

### 1.3 `AgentEvent` 種類 — runner 跟 orchestrator 的共同語言

```python
class AgentEventKind(str, Enum):
    TURN_STARTED       = "turn_started"      # 新一輪 LLM call 開始
    ITEM_STARTED       = "item_started"      # 子事件 (system_init / tool 啟動)
    MESSAGE_DELTA      = "message_delta"     # assistant 文字輸出
    ITEM_COMPLETED     = "item_completed"
    TOOL_CALL          = "tool_call"         # 工具呼叫 (含 input)
    TOOL_RESULT        = "tool_result"       # 工具結果 (含 is_error)
    TURN_COMPLETED     = "turn_completed"    # 一輪完成 (含 cost_usd / session_id)
    USER_INPUT_REQUIRED= "user_input_required"  # 高信任模式視為失敗
    ERROR              = "error"
    DONE               = "done"              # runner 自己結束 (reason=...)
```

把 backend 套進這套 enum 是寫一個 runner 的全部工作。其他層 (FSM 排程、stall detection、cost 累計、HITL hint 注入、retry/backoff) **完全跟 backend 無關**。

### 1.4 三種既有 runner 的對位

| 操作 | EchoRunner | ClaudeCLIRunner | AnthropicAPIRunner |
|---|---|---|---|
| 怎麼啟動 | 直接寫 marker 檔 | `subprocess.Popen("claude -p ... --output-format stream-json")` | `requests.post("/v1/messages", ...)` |
| 怎麼讀事件 | yield 三個固定事件 | 逐行讀 NDJSON,逐行 `_translate(msg)` | 逐 turn 拆 `content` blocks |
| Tools | 無 | Claude CLI 內建 (Bash/Read/Edit/Write) | 你自己提供 (`tools=[]` + `ToolDispatcher`) |
| Session resume | 無 | `--resume <session_id>` | 自行重組 messages 陣列 |
| 用途 | 測試 / demo | 生產上連真 LLM | DPO 蒐集 / 本地 LLM / 領域特化 |

---

## 2. 為什麼這個框架要強調 DPO?

### 2.1 DPO 是什麼

DPO (Direct Preference Optimization, [Rafailov et al. 2023](https://arxiv.org/abs/2305.18290)) 是一種比 RLHF 更輕的偏好對齊方法。它需要的訓練資料是**配對的**「好的軌跡 vs 不好的軌跡」(chosen vs rejected),不需要訓練 reward model,直接用 cross-entropy 對 base model 做微調。

對 agent 來說,「軌跡」= 一整個 turn-by-turn 的 message 陣列(含 tool_call 跟 tool_result)。

### 2.2 為什麼 Symphony 的 event stream 天生適合 DPO

DPO 訓練資料最難收集的部分有兩塊:**(a) 完整的 turn 序列要被原樣保留**、**(b) 偏好 label 要可信**。Symphony 的執行模型剛好在這兩件事上都送禮:

| DPO 需要的東西 | Symphony 對應的機制 |
|---|---|
| 完整 turn-by-turn 訊息 | `AgentEvent` stream 已是 segment-friendly:`TURN_STARTED` / `TURN_COMPLETED` 是 segment 邊界,`TOOL_CALL` / `TOOL_RESULT` 是 action / outcome 對 |
| 「好 / 不好」的 label | `RunAttempt.terminal_reason` 一級分類就提供:`AGENT_FINISHED` / `HANDOFF` 偏向 chosen,`ERROR` / `USER_INPUT_REQUIRED` / `ABORTED` 偏向 rejected |
| 同樣輸入下的對比樣本 | 重試機制天然產生 — 同一個 issue,attempt #1 失敗、attempt #2 成功,兩條 trace **prompt 完全相同**(除了 hint 注入)就是天然的 DPO pair |
| 人類介入的訊號 | Hint store 每筆都有 `consumed_attempt`;在 hint **被注入後**成功的 attempt 跟**沒注入時**失敗的 attempt,是「人類 feedback 改變了結果」的純粹 DPO signal |
| 蒐集 instrumentation 不該影響執行 | `Orchestrator.add_event_listener(fn)` 就是純 listener fan-out;dashboard 已經用 listener 把事件灌進 SQLite,你的 DPO collector listener 可以**並行掛上**,互不干擾 |

### 2.3 蒐集器範例

```python
# 你的 DPO listener 跟 dashboard listener 是 peer 的關係,各自獨立。
import json, redis

r = redis.Redis()

def dpo_collector(issue_id: str, event: AgentEvent) -> None:
    r.xadd(
        f"agent_trace:{issue_id}",
        {
            "kind": event.kind.value,
            "ts": event.timestamp.isoformat(),
            "data": json.dumps(event.data, default=str),
        },
    )

orch.add_event_listener(dpo_collector)
```

訓練 pipeline 之後就只是把 stream 切成 `(prompt, [chosen_turns], [rejected_turns])` 三元組:
- `chosen` = `terminal_reason ∈ {AGENT_FINISHED, HANDOFF}` 那次 attempt 的所有 turn
- `rejected` = 同 issue id 之前 `terminal_reason ∈ {ERROR, USER_INPUT_REQUIRED, MAX_TURNS}` 的 attempt
- prompt = `issue.to_template_context(attempt_number) → render_prompt`(或從 dashboard 的 `rendered_prompt_preview` 直接拿)

### 2.4 為什麼**不**要直接從 RLHF 開始

| RLHF | DPO + Symphony |
|---|---|
| 需要 reward model | 不需要,terminal_reason 直接當二元偏好 |
| 需要 PPO 實作 | 一個 cross-entropy loss 就解決 |
| 線上 sampling 跟 training 耦合 | 完全離線,Symphony 跑生產蒐 trace,訓練另開 |
| 需要 separate reward model 維運 | 0 額外服務 |

這就是為什麼 `AnthropicAPIRunner` 的存在的第一個理由是 "**DPO pipeline 友好** — 每個 turn 的 `messages` array 是你自己擁有的"。`ClaudeCLIRunner` 也能蒐 trace,但 prompt cache breakpoint 跟 messages 結構由 Claude CLI 包好了,客製空間較小;`AnthropicAPIRunner` 直接打 `/v1/messages`,你完全擁有訊息序列。

---

## 3. 接一個新 agent backend — 範本與 OpenCode 設計

### 3.1 通用 adapter 範本

任何 backend 要接進 Symphony,需要做的就只有一件事:**寫一個滿足 `AgentRunner` Protocol 的類別,把 backend 的事件流翻譯成 `AgentEvent`**。

```python
from collections.abc import Iterator
from symphony_mvp.agent_runner import AgentEvent, AgentEventKind


class MyBackendRunner:
    def __init__(self, *, command: str, model: str | None = None, ...) -> None:
        self.command = command
        self.model = model

    def run(
        self,
        *,
        prompt: str,
        workspace_path: str,
        max_turns: int,
        session_id: str | None = None,
    ) -> Iterator[AgentEvent]:
        # 1. spawn the backend (subprocess / API call / SDK)
        argv = self._build_argv(prompt, max_turns, session_id)
        proc = subprocess.Popen(
            argv,
            cwd=workspace_path,    # ← invariant #1, must equal workspace_path
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        yield AgentEvent.now(AgentEventKind.TURN_STARTED, turn=1)

        # 2. stream-translate into AgentEvent
        for line in proc.stdout:
            ev = self._translate(line)
            if ev is not None:
                yield ev

        rc = proc.wait()
        if rc != 0:
            yield AgentEvent.now(
                AgentEventKind.ERROR, message=f"backend exited {rc}"
            )
        else:
            yield AgentEvent.now(AgentEventKind.DONE, reason="agent_finished")

    def _build_argv(self, prompt, max_turns, session_id) -> list[str]:
        ...

    def _translate(self, line: str) -> AgentEvent | None:
        # backend-native event → AgentEvent
        ...
```

接著在 `agent_runner.build_runner(...)` factory 加一個 `kind` 分支,在 `WORKFLOW.md` 寫 `runner.kind: my_backend` 就完成。**不必動 orchestrator、不必動 tracker、不必動 dashboard。**

### 3.2 OpenCodeRunner 設計

[opencode](https://github.com/sst/opencode) 是 SST 開的一個開源 AI coding agent,主打 TUI + provider 中立 (走 LiteLLM,可接 Anthropic / OpenAI / Groq / 本地)。它本身的設計哲學跟 Claude Code CLI 接近,但**事件協議跟 binary 都不同**,所以要自己寫 adapter。

#### 3.2.1 opencode 的 invocation 模式

opencode 預設是互動 TUI,但在 `0.x` 之後支援 headless mode:

```bash
# 互動模式 (給人用)
opencode

# Headless / one-shot (給 framework 嵌入)
opencode run "<prompt>" \
    --print \                    # 將輸出送 stdout 而非 TUI
    --provider anthropic \       # 或 openai / groq / etc.
    --model claude-opus-4-7
```

opencode 也吐 NDJSON / JSONL 結構化輸出,以及 session 機制 (`--continue`, `--session <id>`)。具體 flag 隨版本演進,寫 adapter 時抓最新版的 `opencode run --help` 為準。

#### 3.2.2 WORKFLOW.md 上長什麼樣

```yaml
runner:
  kind: opencode
  command: opencode              # 預設值
  provider: anthropic             # opencode 透過 LiteLLM 支援多家
  model: claude-opus-4-7
  allowed_tools: [bash, edit, read, write]
  max_turns: 20                   # 跟 agent.max_turns 對齊
```

#### 3.2.3 Adapter 實作骨架

```python
# symphony_mvp/agent_runner.py 加一個類別

class OpenCodeRunner:
    """Wraps the `opencode` CLI in headless / streaming mode.

    opencode is provider-agnostic via LiteLLM; pick provider + model in
    WORKFLOW.md so the orchestrator-side workflow stays portable.
    """

    def __init__(
        self,
        *,
        command: str = "opencode",
        provider: str = "anthropic",
        model: str | None = None,
        allowed_tools: list[str] | None = None,
    ) -> None:
        self.command = command
        self.provider = provider
        self.model = model
        self.allowed_tools = allowed_tools or ["bash", "read", "edit", "write"]

    def _build_argv(
        self, prompt: str, max_turns: int, session_id: str | None
    ) -> list[str]:
        argv = [
            self.command,
            "run",
            prompt,
            "--print",                       # NDJSON to stdout
            "--provider", self.provider,
            "--max-turns", str(max_turns),
            "--allowed-tools", ",".join(self.allowed_tools),
        ]
        if self.model:
            argv += ["--model", self.model]
        if session_id:
            argv += ["--session", session_id]   # session resume
        return argv

    def run(
        self,
        *,
        prompt: str,
        workspace_path: str,
        max_turns: int,
        session_id: str | None = None,
    ) -> Iterator[AgentEvent]:
        argv = self._build_argv(prompt, max_turns, session_id)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=workspace_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            yield AgentEvent.now(
                AgentEventKind.ERROR, message=f"`{self.command}` not on PATH"
            )
            return

        yield AgentEvent.now(AgentEventKind.TURN_STARTED, turn=1)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield from self._translate(msg)
        finally:
            rc = proc.wait()
            if rc != 0:
                err = (proc.stderr.read() if proc.stderr else "") or ""
                yield AgentEvent.now(
                    AgentEventKind.ERROR,
                    message=f"opencode exited {rc}: {err[:500]}",
                )
            else:
                yield AgentEvent.now(AgentEventKind.DONE, reason="agent_finished")

    @staticmethod
    def _translate(msg: dict) -> Iterator[AgentEvent]:
        # opencode's event shapes differ from Claude CLI; adjust to actual schema.
        # Below is illustrative — check `opencode run --help` and stdout schema.
        kind = msg.get("kind") or msg.get("type")

        if kind in ("session_start", "init"):
            yield AgentEvent.now(
                AgentEventKind.ITEM_STARTED,
                item_kind="session_start",
                session_id=msg.get("session_id"),
            )
        elif kind in ("assistant_text", "message", "content_delta"):
            yield AgentEvent.now(
                AgentEventKind.MESSAGE_DELTA, text=msg.get("text", "")
            )
        elif kind in ("tool_call", "tool_use"):
            yield AgentEvent.now(
                AgentEventKind.TOOL_CALL,
                tool=msg.get("tool") or msg.get("name"),
                tool_use_id=msg.get("id"),
                input=msg.get("input") or msg.get("arguments"),
            )
        elif kind in ("tool_result",):
            yield AgentEvent.now(
                AgentEventKind.TOOL_RESULT,
                tool_use_id=msg.get("tool_use_id") or msg.get("id"),
                is_error=bool(msg.get("is_error")),
            )
        elif kind in ("turn_complete", "turn_done"):
            yield AgentEvent.now(
                AgentEventKind.TURN_COMPLETED,
                cost_usd=msg.get("cost_usd") or msg.get("cost"),
                num_turns=msg.get("turn"),
                session_id=msg.get("session_id"),
            )
        elif kind in ("error",):
            yield AgentEvent.now(
                AgentEventKind.ERROR, message=msg.get("message", "unknown error")
            )


# 同檔案最下面的 build_runner factory:

def build_runner(kind: str, *, command=None, model=None, max_tokens=4096, **extra):
    if kind == "echo":
        return EchoRunner()
    if kind == "claude_cli":
        return ClaudeCLIRunner(command=command or "claude", model=model)
    if kind == "anthropic_api":
        return AnthropicAPIRunner(model=model or "claude-opus-4-7", max_tokens=max_tokens)
    if kind == "opencode":
        return OpenCodeRunner(
            command=command or "opencode",
            provider=extra.get("provider", "anthropic"),
            model=model,
            allowed_tools=extra.get("allowed_tools"),
        )
    raise ValueError(f"Unknown runner kind: {kind!r}")
```

#### 3.2.4 跑起來需要注意的事

1. **provider 鍵的取得方式跟 Claude CLI 不一樣** — opencode 透過 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GROQ_API_KEY` 等環境變數,**或**用 `~/.config/opencode/config.json`。生產建議用 env 即可。
2. **session 持續性** — opencode 的 `--session` 等價於 Claude CLI 的 `--resume`。Symphony 在 `_AgentWorker.run()` 自動把 `attempt.session_id` 傳進去,所以只要你在 `_translate` 收到 `session_id` 時 yield `ITEM_STARTED` / `TURN_COMPLETED` 帶 `session_id`,orchestrator 會自動寫回 `attempt.session_id`,下次 retry 自動 resume。
3. **stall detection** — 如果 opencode 在 tool 執行期間沒有任何 output,Symphony 預設 `stall_timeout_ms=600000` (10 分鐘) 才會 kill。比 LLM call 還久的 tool 要在 WORKFLOW.md 把 `agent.stall_timeout_ms` 調大。
4. **權限** — opencode 沒有 Claude CLI 的 `--permission-mode acceptEdits`,所以 dangerous tool 的 allow-list 要在 WORKFLOW.md 的 `runner.allowed_tools` 收緊,或在 `before_run_hook` 用 sandbox 工具(firejail / bubblewrap / Docker)再包一層。
5. **WebSocket dashboard 行為不變** — 因為 `OpenCodeRunner` 吐的是同一套 `AgentEvent`,Kanban、Hint composer、Replay tab、Activity feed **全部不必改任何一行**就會運作。這是 Protocol 抽象的紅利。

#### 3.2.5 測試策略 — 不要對 opencode CLI 做 e2e

跟 `ClaudeCLIRunner` 一樣的策略:**單元測試只測 `_translate(msg)`,不真的 spawn `opencode`**。對應 `tests/test_agent_runner.py` 的格式:

```python
def test_opencode_translate_assistant_text():
    events = list(OpenCodeRunner._translate({"kind": "message", "text": "hello"}))
    assert len(events) == 1
    assert events[0].kind == AgentEventKind.MESSAGE_DELTA
    assert events[0].data["text"] == "hello"


def test_opencode_translate_tool_call():
    events = list(OpenCodeRunner._translate({
        "kind": "tool_call",
        "name": "Bash",
        "id": "call_42",
        "input": {"command": "ls"},
    }))
    assert events[0].kind == AgentEventKind.TOOL_CALL
    assert events[0].data["tool"] == "Bash"
```

End-to-end 跑真 `opencode` 的測試放在 `examples/`,不放 CI。

---

## 4. Adapting to your domain — 通用換件清單

把 Symphony 接到自己領域時,通常會碰到的取代:

| Symphony 元素 | 你的環境怎麼換 |
|---|---|
| `GitHubIssuesTracker` | 寫一個自己的 `Tracker` 實作:三個 protocol method (`fetch_active`, `fetch_terminal`, `reconcile_states`) |
| `claude_cli` runner | 換 `anthropic_api` 或 `opencode` 或自寫 runner,綁進你領域的 tool 集 |
| `agent.handoff_state="in_review"` | 任何代表「人類接手」的 tracker 狀態字串 |
| `WORKFLOW.md` 的 prompt body | 你的標準作業流程 / playbook,引用 `AGENTS.md` 風格的角色界定 |
| `AnthropicAPIRunner.tools` | 你定義的工具 schema 陣列 |
| `ToolDispatcher.dispatch()` | 你的工具實作 + schema 驗證 + allow-list |

延伸閱讀:
- [docs/design/symphony-dashboard-spec.md](symphony-dashboard-spec.md) — Dashboard 完整設計
- [docs/todolist/symphony-dashboard-todolist.md](../todolist/symphony-dashboard-todolist.md) — 實作工作分解
