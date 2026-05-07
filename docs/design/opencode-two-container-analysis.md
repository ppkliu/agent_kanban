# Two-Container Phase 2 — opencode 跨容器呼叫方案分析

> 對應 [`docs/todolist/post-mvp-gaps.md`](../todolist/post-mvp-gaps.md) Medium 第 1 項。
> Phase 1 (單容器) 已落地;這份文件是 Phase 2 (雙容器爆炸半徑切分) 動工前的設計分析,
> 給使用者選方案用,不是實作 PR 的設計記錄。

---

## 1. 動機

[`docs/guide/docker-quickstart.md`](../guide/docker-quickstart.md) 與
[`docs/guide/docker-quickstart.zh-TW.md`](../guide/docker-quickstart.zh-TW.md) 起初描述的部署是**雙容器**:

> Why separate containers — **Blast radius** — opencode runs arbitrary tool
> calls (Bash, Edit, Write). Putting it in its own container means a
> compromised attempt can only see the workspace volume, not the dashboard's
> SQLite or WORKFLOW.md.

但 Phase 1 為了快速 ship,把 opencode CLI 跟 FastAPI dashboard 一起 bake 到同一個 image:
[`Dockerfile:33`](../../Dockerfile) 用 `npm install -g opencode-ai` 把 `opencode` 放在容器內 PATH 上,
[`OpenCodeRunner`](../../symphony_mvp/agent_runner.py) 直接 `subprocess.Popen(["opencode", "run", ...])`。
這個 trade-off 拿到了「能跑」,但欠了一筆債:

- 任何 opencode tool call (Bash / Edit / Write) 跑在跟 FastAPI 同一個 fs namespace 的容器裡
- `/app/data/dashboard.db`、`/app/WORKFLOW.md`、整個 `symphony_mvp/` Python 套件,
  都在 opencode 子行程的 reach 內
- 只要一個攻陷 attempt 能 escape 工具白名單,SQLite 跟 WORKFLOW.md 就同時暴露

Phase 2 的目標就是補這條紅線:**讓 opencode 在獨立容器跑**,dashboard 容器只看得到自己的東西。

## 2. 現況 (Phase 1 single-container)

```
┌─────────────────────────────────────────────────────────────┐
│  symphony-dashboard  (Phase 1, 一個容器全包)                  │
│                                                              │
│  /app/                                                       │
│    ├─ symphony_mvp/        (FastAPI + orchestrator)          │
│    ├─ frontend/dist/       (built SPA)                       │
│    ├─ data/dashboard.db    (events / hints / priority)       │
│    ├─ WORKFLOW.md          (mounted ro from host)            │
│    └─ workspaces/          (named volume; 每個 issue 一個)    │
│                                                              │
│  Process tree:                                               │
│    uvicorn (FastAPI)                                         │
│      └─ orchestrator thread                                  │
│             └─ subprocess.Popen("opencode run ...",          │
│                                  cwd=workspaces/<issue>)     │
│                  └─ tool calls: Bash / Edit / Write          │
└─────────────────────────────────────────────────────────────┘
```

**已隔離**:host fs (除了三個 mount)、host network 命名空間、host 的其他行程。
**未隔離**:dashboard ↔ opencode 共用同一個容器內 fs / pid / user namespace。

Phase 2 的目標是把 opencode 那個 subprocess 從這個 box 抽出去,讓它跑在獨立容器裡,
只看得到一個 workspace volume,看不到 SQLite / WORKFLOW.md / Python 套件。

## 3. OpenCodeRunner 的呼叫面

任何方案都得在保持以下契約的前提下動:
[`agent_runner.py:300-370`](../../symphony_mvp/agent_runner.py)

| 元素 | 現況 | Phase 2 必須保留 |
|---|---|---|
| 啟動指令 | `opencode run <prompt> --print --provider X --model Y --max-turns N --allowed-tools ...` | argv shape 不變或可逆 — 8 個既有 unit test 鎖住這個 shape |
| 工作目錄 | `cwd=workspace_path` (SPEC §7 invariant #1) | opencode 進程必須在對應的 workspace 內執行 |
| 串流輸出 | NDJSON over stdout,逐行 parse,丟給 `_translate` | 串流語意不能退化 — 終端使用者需要即時看到 message_delta |
| 結束碼 | `proc.wait()`;非 0 → ERROR,0 → DONE | 失敗訊號要可區分 |
| Session resume | `--session <id>` 第二次以後 attempt 帶上 | 跨容器 session 持續性必須保持 |
| 認證 | provider 的 API key 透過 env 進 process | 認證資訊只給 opencode,不給 dashboard |

8 個既有測試 (`tests/test_agent_runner.py`) 全部測 `_build_argv` + `_translate` 純函式,
不測實際 spawn,所以 invocation 內部換掉並不會 break legacy tests — 但**會 break 新測試的覆蓋面**,
所以新方案必須補對應 invocation 的整合測試。

## 4. 跨容器呼叫方案

### 4.1 Docker exec via socket bind

**做法**:在 `dashboard` 服務 mount `/var/run/docker.sock`,把 `Popen(["opencode", ...])`
換成 `Popen(["docker", "exec", "-i", "-w", workspace_path_in_opencode_container,
"symphony-opencode", "opencode", "run", ...])`。

**code 變更量**:`OpenCodeRunner._build_argv` 加前綴 (~10 行);compose 加 `volumes:
- /var/run/docker.sock:/var/run/docker.sock`;新增 `opencode` service (long-running
sleep/idle 容器,讓 dashboard 隨時 exec 進去)。

**必須驗證**:
- workspace volume 在兩個容器都 mount 在**同一條路徑** (例:`/workspaces`),不然 cwd 對不上
- `docker exec` 的 stdout/stderr 串流跟 Popen 直連 stdout 行為一致

**已知 / 已驗證**:
- Docker socket bind 是 [Docker security 文件](https://docs.docker.com/engine/security/protect-access/)
  明確警告的 anti-pattern:**容器內擁有 socket = 容器逃脫 + host root** (因為可以
  `docker run --privileged -v /:/host`)。Mitigations 有 docker-socket-proxy 之類的中介,但都會
  增加維運面積。

### 4.2 Docker SDK (Python `docker` 套件) via socket bind

**做法**:同 4.1 的 socket 暴露,但 dashboard 端用 [`docker` Python SDK](https://docker-py.readthedocs.io/)
跑 `client.containers.get("symphony-opencode").exec_run(cmd, stream=True)`,拿 typed log stream。

**相對 4.1 的差別**:
- 多一個 Python 依賴 (`docker>=7`)
- 串流 API 比 `docker exec` shell wrapping 乾淨
- 一樣承擔 socket bind 的安全成本

**結論**:跟 4.1 是同一條路的兩個寫法,選哪個都不影響核心 trade-off。

### 4.3 opencode `serve` HTTP/SSE (✅ 推薦)

**做法**:用 opencode upstream 已經提供的 [`opencode serve`](https://opencode.ai/docs/server/) —
一個 headless HTTP 服務,有 OpenAPI 3.1 spec、SSE 串流、basic auth 支援。

```
┌────────────────────────────┐            ┌────────────────────────────┐
│  symphony-dashboard        │            │  symphony-opencode         │
│    FastAPI + SQLite        │  HTTP/SSE  │    opencode serve --port   │
│    OpenCodeRunner          │ ─────────► │      4096 --hostname 0.0.0.0│
│      ↳ aiohttp/httpx       │            │    /session  /event  ...   │
│                            │            │    binds workspace volume  │
└────────────────────────────┘            └────────────────────────────┘
        ▲                                           ▲
        │                                           │
        └─ mounts: data, WORKFLOW.md, frontend/dist │
                                                    └─ mounts: workspaces only
```

**已知** (來自 opencode docs 與 README,2026-05 取得):
- `opencode serve` 啟動 OpenAPI 3.1 服務,預設 listen `127.0.0.1:4096`
- OpenAPI spec 在 `http://<host>:<port>/doc`
- SSE 端點 `/event` 與 `/global/event` 提供事件串流 (對應目前 NDJSON 的串流語意)
- session / message / file / find / provider endpoint 涵蓋多數 invocation 需求
- 可選 basic auth:`OPENCODE_SERVER_PASSWORD` + `OPENCODE_SERVER_USERNAME` env var

**code 變更量**:`OpenCodeRunner.run()` 從 Popen-NDJSON 換成 httpx client + SSE 訂閱
(~80–120 LOC);compose 新增 `opencode` service 用 official 容器 image (或自包 Dockerfile);
`_translate` 邏輯**完全保留** (event kind mapping 一樣適用,只是 transport 換了)。

**必須驗證 (留到實作時)**:
- `/session` POST body 是否接受 cwd / working_directory 參數讓 opencode 在 workspace 內跑
  (對應 SPEC §7 invariant #1);如果只能由 server 預設 cwd,就要把 workspace 改成「每個 attempt
  起一個獨立的 opencode 容器」(更嚴格的爆炸半徑,代價是 cold-start)
- `--max-turns` / `--allowed-tools` / `--session` 怎麼從 CLI 對映到 `/session` 或 `/session/:id/message`
  的 request body
- SSE 的 event 形狀跟現在 `_translate` 接收的 `kind` / `type` 是否一致;若不同需要新增
  `_translate_sse` 對應分支
- basic auth 跟 `OPENCODE_API_KEY` (provider key) 是兩件事 — server password 是 dashboard ↔ opencode
  之間的;provider key 還是 opencode ↔ LLM endpoint 之間的

### 4.4 共用 volume + Unix socket sidecar

**做法**:在 opencode 容器跑一個小 supervisor,把 `opencode run` wrap 成 Unix socket server
(socket file 放在 dashboard ↔ opencode 共用的 named volume)。dashboard 端用 `socket` module
連 socket,送 request、收 NDJSON。

**好處**:沒有 docker socket 暴露,沒有 TCP egress 出容器。

**壞處**:得自己寫 supervisor + 協議 (request schema、long-running stream),維護成本高。
opencode 已經提供 §4.3 的 HTTP server,自己再造一輪 IPC 沒道理。

**結論**:除非 §4.3 因為某個未知原因不可行,否則這條不該選。

### 4.5 維持單容器 + drop-privileges (anti-pattern,僅列出做對照)

**做法**:Phase 1 既有結構不變,但:
- 新增非 root user (例:`opencode:opencode`,uid 1001)
- opencode subprocess `setuid` 到那個 user,只給 `/app/workspaces` 寫權限;
  `/app/data` 跟 `/app/WORKFLOW.md` 改成 root-owned + ro
- 用 Linux capabilities drop / seccomp profile 收進一步

**好處**:零 docker 改動;爆炸半徑能縮小到 process boundary 等級。

**壞處**:
- 不算「容器隔離」,只是 process / user 隔離 — 一旦 opencode 拿到 dashboard root 的能力
  (例:tool call 觸發 setuid binary),就退回 Phase 1 的暴露面
- WORKFLOW.md 的 ro mount 對 hot-reload 仍 OK,但 SQLite 的 root-only 讀寫意味著 dashboard 與
  opencode 不能再共享同一個 sqlite — dashboard 端可以,但 opencode tool 用 sqlite client 直接
  讀就讀不到了 (這對目前的 use case 不是問題,只是要點記錄下來)

**結論**:備選,不是首選。如果 §4.3 走不下去再考慮這條。

## 5. 成本效益矩陣

評分:✅ 好 / ⚠️ 中性或有 caveat / ❌ 不好。LOC 是估的 net 增量。

| 軸 | 4.1 docker exec | 4.2 docker SDK | **4.3 opencode serve (推薦)** | 4.4 socket sidecar | 4.5 single + dropprivs |
|---|---|---|---|---|---|
| 爆炸半徑縮小 | ✅ 完整容器隔離 | ✅ 完整容器隔離 | **✅ 完整容器隔離** | ✅ 完整容器隔離 | ⚠️ 只到 process / user |
| docker socket 暴露 | ❌ 必須暴露 | ❌ 必須暴露 | **✅ 不需要** | ✅ 不需要 | ✅ 不需要 |
| 串流語意保留 | ⚠️ 透過 docker exec stdout,要驗收 | ⚠️ 同左 | **✅ SSE 是一級公民** | ⚠️ 自寫協議 | ✅ 不變 (沒搬) |
| 上游耦合 | ⚠️ 綁 docker CLI / API 介面 | ⚠️ 綁 docker SDK 版本 | **✅ 用 OpenAPI 3.1 — 上游契約** | ❌ 自家土砲協議 | ✅ 沒搬 |
| Net code (LOC) | ~30 (Popen wrap) + compose | ~50 (SDK calls) + compose | **~120 (httpx + SSE) + compose** | ~250+ (含 supervisor) | ~40 (Dockerfile + compose) |
| Image 改動 | 多一個 idle opencode container | 同左 + dashboard 加 docker SDK | **多一個 opencode container (用 official image)** | 兩邊都得加 supervisor | 0 |
| Cold start latency | docker exec / 次,~50–200ms | 同左 | **HTTP 連線 reuse,~5–20ms** | socket reuse,類似 4.3 | 0 |
| Ops 複雜度 | ⚠️ socket-proxy 加固 | ⚠️ 同 4.1 | **✅ 兩個 service,標準 compose 用法** | ⚠️ supervisor 監控 | ✅ 1 個 service |
| 上游 drift 風險 | ⚠️ docker CLI/API 介面變 | ⚠️ docker SDK 版本相容 | **✅ OpenAPI spec 自帶版本** | ❌ 自家寫的協議要自己維護 | ✅ 沒上游 |
| 對既有 8 個 unit test 影響 | 0 (`_translate` / `_build_argv` 不動) | 0 | **0** | 0 | 0 |
| 需要新增的整合測試 | docker-in-docker pytest fixture | 同左 | **httpx + SSE,可用 opencode container 起來真打** | socket fixture | seccomp profile 驗證 |

最後一列特別關鍵:**4.3 是唯一可以用 `docker compose up -d opencode` + 真實 HTTP 測試 的方案**,
其他幾條的整合測試要嘛要 docker-in-docker (4.1 / 4.2),要嘛要自寫 supervisor harness (4.4),
都比直接打 HTTP 麻煩。

## 6. 推薦

**選 §4.3:opencode serve + HTTP/SSE。**

三個理由:
1. **沒有 docker socket 暴露**。其他兩條走容器隔離的路 (4.1 / 4.2) 都要把 host root level 的權限
   塞進 dashboard 容器,這直接 cancel 掉「縮小爆炸半徑」的初衷。
2. **上游已經提供 stable surface**。OpenAPI 3.1 spec + SSE 是公開、版本化、有文件的介面;
   不用自己造輪子 (4.4),也不用綁 docker 介面 (4.1 / 4.2)。
3. **`_translate` 重用率最高**。Event kind 對映邏輯一行不改,只換 transport。對既有 8 個
   unit test 零衝擊,新增的整合測試可以用真實 opencode container 跑 — 相對好寫好維護。

**會推翻這個推薦的條件** (寫進 §8):
- §4.3 假設 `/session` 接受 cwd 參數,讓 opencode 在 workspace 內跑;若上游 API 不允許指定 cwd,
  那 cwd-per-attempt 必須改成 container-per-attempt,代價是 cold-start 從 ~10ms 漲到 ~500ms
- 若 SSE event shape 跟現在 NDJSON 形狀差太多 (例如 SSE 的 `event:` 名跟 NDJSON 的 `kind` 不對
  齊),需要寫一層 adapter,LOC 會多 ~50 行

兩個都不是 dealbreaker,但會在 stage 2 的 plan 裡量化處理。

## 7. 落地步驟 (給 stage 2 用,不是現在動)

1. **驗證**:在本地用 `docker run --rm -p 4096:4096 ghcr.io/sst/opencode:latest serve` 把 opencode
   server 跑起來,`curl /doc` 拿到 OpenAPI,把 §6 列的兩個未驗證項打勾或記成 known-broken
2. **runner**:`OpenCodeRunner.run()` 改寫:
   - 啟動時 POST `/session`,拿 session_id
   - POST `/session/:id/message` (帶 cwd / working_directory / model / provider / allowed_tools
     等參數,對映現在 `_build_argv` 的 flag)
   - 訂閱 SSE `/event?session=...`,把 event 拋給既有的 `_translate`
   - resume case 帶現有 session_id 直接 POST `/session/:id/message`
   - 完成或錯誤把 stream 關掉、yield TURN_COMPLETED / ERROR
3. **compose**:新增 `opencode` service 用 `ghcr.io/sst/opencode:latest`,內部 listen 4096;
   dashboard 加 env `OPENCODE_SERVER_URL=http://opencode:4096` + 相關 password/api_key;
   把 dashboard Dockerfile 裡的 `npm install -g opencode-ai` 拿掉
4. **測試**:
   - 既有 8 個 unit test:不動 (除非 `_build_argv` 因 server 改用而 deprecate,則改成測 message body)
   - 新整合測試:在 CI 起 `opencode` service,跑真實 session round-trip,驗 event mapping
5. **docs**:user guide 雙語版回到原本的雙容器拓撲圖;post-mvp-gaps 把 Phase 2 改成 `[x]`
6. **back-out plan**:Phase 2 commits 全在獨立分支驗收完才合 main;若整合測試發現 SSE shape
   問題,直接 revert compose 改動 + runner 改動,Phase 1 的 image 沒被破壞,user guide
   commit 可以選擇性 revert

## 8. 已知未確定項 (stage 2 開工時要先驗)

- [ ] `POST /session` 的 body 是否能指定 `cwd` / `working_directory`,讓 opencode 在 workspace 內執行
- [ ] SSE `/event` 的 `event:` 名 vs 現有 NDJSON 的 `kind` 是否對齊;對不齊就要寫 adapter
- [ ] `--allowed-tools` / `--max-turns` 在 server mode 下對應的 request 欄位
- [ ] `--session <id>` resume 對應到哪個 endpoint (POST `/session/:id/message`?)
- [ ] basic auth 流量壓力 — 每次 attempt 起新 session 還是 reuse session
- [ ] cold-start vs warm: dashboard 啟動先打 `/global/health` poll 直到 opencode 起來
- [ ] image 如何拿:`ghcr.io/sst/opencode:latest` 是公開可拉的嗎?還是要自包 Dockerfile?

每一個 dealbreaker 都應該在 stage 2 動工的第一個小時內驗,而不是寫到一半才發現。

---

## 附錄 A:對照 stage 0 (今天) 跟 stage 2 (預計) 的拓撲

```
stage 0 (Phase 1, 已落地)               stage 2 (Phase 2 §4.3, 預計)
─────────────────────────────           ─────────────────────────────
[ symphony-dashboard ]                  [ symphony-dashboard ]
  uvicorn + orchestrator                  uvicorn + orchestrator
    └─ Popen(opencode run ...)              └─ httpx → http://opencode:4096
       cwd=/app/workspaces/<id>                 (SSE on /event)
  + opencode CLI bundled in image
                                        [ symphony-opencode ]
                                          ghcr.io/sst/opencode:latest
                                          serve --hostname 0.0.0.0 --port 4096
                                          mounts: workspaces volume only
```
