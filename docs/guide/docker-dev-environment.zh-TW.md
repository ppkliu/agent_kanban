# Docker 開發環境 — 後端在 container,前端在 host

> English: [docker-dev-environment.md](docker-dev-environment.md)

這份指南幫你拿到一個 **開發專用** 的 Docker 環境:後端跑在 container 裡,
Python toolchain 都預裝好,代碼從 host bind mount 進去 (你的編輯器看到
什麼,container 就看到什麼),前端 dev server 跑在 host 上,Vite proxy
直接打進 container。

跟 [`docker-quickstart.md`](docker-quickstart.zh-TW.md) (production
部署) **完全分開** — production 把 SPA 編進 image 後一包搞定;這條
dev 流程預期你在 host 改 code、在 container 裡執行。

---

## 1. 你會拿到的

| 面向 | 這套設定 |
|---|---|
| Python toolchain | `Dockerfile.dev` 預裝 (`pip install -e ".[dev,dashboard]"`) — host 不用 `uv venv` |
| opencode CLI | dev image 一起裝,所以 OpenCodeRunner 測試也能在 container 內跑 |
| 代碼 | 從 host bind mount — IDE 改完 container 馬上看到 |
| host 上的 `__pycache__` 噪音 | 用 `PYTHONDONTWRITEBYTECODE=1` 壓掉 |
| Hot reload | **故意沒開** — `./scripts/dev.sh restart` 大概 2 秒,clean re-import |
| 檔案同步 | WSL2 / macOS / Linux 都 OK — 細節看 §5 |
| 前端 dev server | 留在 host (`cd frontend && npm run dev`) — Vite 把 API + WebSocket proxy 到 dev container 的 `:7957` |
| Host port | `:7957` (dev port)。Production 用 `:17957`,兩個可以同時跑 |

---

## 2. 一鍵啟動

```bash
git clone <this-repo>
cd agent_kanban

# (必須先做) 放一份 WORKFLOW.md。echo demo 或 opencode 預設都可以。
# 漏這步的話 Docker 會把 bind-mount 來源端自動建成「目錄」,然後
# container 啟動時讀 WORKFLOW.md 會丟 IsADirectoryError 直接退出。
# scripts/dev.sh 現在會 pre-flight 擋掉,但你還是得先把檔案放好。
cp examples/WORKFLOW.demo-echo.md WORKFLOW.md

# 起 dev 後端
./scripts/dev.sh up

# 另開一個 terminal:前端 dev server (host 上)
cd frontend && npm install && npm run dev
# → 開 http://localhost:5173
```

完事。改 `symphony_mvp/**.py` 後跑 `./scripts/dev.sh restart` 就好。
改 `frontend/src/` 下的東西,Vite 在瀏覽器自動 hot-reload。

### 如果漏了 `cp` 步驟、WORKFLOW.md 變成資料夾

症狀:`./scripts/dev.sh ps` 表格是空的 (或 `ps -a` 顯示 "Exited (1)"),
`./scripts/dev.sh logs` 印出 `IsADirectoryError: '/app/WORKFLOW.md'`。
修法:

```bash
./scripts/dev.sh down
rmdir WORKFLOW.md                              # 空目錄 + parent 你有權限
# 或:  sudo rm -rf WORKFLOW.md                 # root-owned 就用 sudo
cp examples/WORKFLOW.demo-echo.md WORKFLOW.md
./scripts/dev.sh up
```

---

## 3. dev image 裡有什麼

dev image 定義在 repo 根目錄的 [`Dockerfile.dev`](../../Dockerfile.dev)。
**只有後端**,沒有 SPA build stage。Build 完大概 ~500 MB:

| Layer | 內容 |
|---|---|
| `python:3.12-slim` base | Python runtime + glibc |
| OS deps | `git`、`curl`、`ca-certificates`、`nodejs`、`npm` (給 opencode-ai 用) |
| opencode CLI | `npm install -g opencode-ai`,讓 OpenCodeRunner 能 spawn |
| Python deps | `pip install -e ".[dev,dashboard]"` — fastapi / uvicorn / pydantic / pytest / requests / watchfiles 等 |
| 代碼 skeleton | `COPY symphony_mvp/` 只是為了 editable install 能解析 metadata;runtime 的 bind mount 會把它整個蓋掉 |

---

## 4. Volume 配置

```
host                                container                              mode
────────────────────────────────    ──────────────────────────────────    ──────
./symphony_mvp                      /app/symphony_mvp                     rw
./tests                             /app/tests                            ro
./pyproject.toml                    /app/pyproject.toml                   ro
./examples                          /app/examples                         ro
./data                              /app/data                             rw  (SQLite)
./WORKFLOW.md                       /app/WORKFLOW.md                      ro
(named volume) workspaces           /app/workspaces                       rw  (per-issue agent workspaces)
```

代碼是 **read-write** 因為 container 要寫一些東西像 `*.egg-info`,但
你絕對不會想 container 寫回去的東西 (tests、pyproject) 都是 **read-only**
當保險絲。Per-issue agent workspace 放在 **named volume**,不是 host bind,
這樣前端 dev server 才不會被 workspace 的檔案變動弄到狂 reload。

---

## 5. 檔案同步 — 實際上發生什麼

當你在 host 改 `symphony_mvp/dashboard/tool_api.py`:

1. Docker bind mount 立刻把新 bytes 送進 container 的 filesystem (沒有
   複製的步驟)。
2. Container 裡跑著的 uvicorn process **已經把舊 code import 進記憶體**。
   新進來的 request 還是會打到舊 code。
3. `./scripts/dev.sh restart` 送 SIGTERM、container re-exec、Python 從
   現在已經更新的 bind mount 重新 import。大約 2 秒。

整個模型就這樣。沒有 auto-reload、沒有 `watchdog`、沒有 inotify 魔法、
沒有 daemon polling — 因為替代方案 (uvicorn `--reload`) 跟長期運行的
orchestrator 執行緒 + SQLite handle 不對盤。2 秒 restart 是對的權衡。

### WSL2 / macOS 偶爾遇到的坑

| 症狀 | 可能原因 | 修法 |
|---|---|---|
| Host 改了 container 看不到 | 你在 Windows 從 `\\wsl$` 編輯,但 Docker Desktop 的 WSL2 integration 設成另一個 distro | `docker compose exec dashboard-dev cat /app/symphony_mvp/__init__.py` 確認;如果是舊的就 `restart` |
| `vite dev` 存檔不會 hot-reload | 編輯器用 atomic save (寫 tmp 再 rename);Vite 在 watch 舊 inode | 編輯器關掉 atomic save,或設 `VITE_CHOKIDAR_USEPOLLING=true` |
| `restart` 慢 (10 秒以上) | pip editable install 在慢的 filesystem 上重做 metadata 檢查 | 確認專案放在 **原生** ext4 (WSL2 home) 而不是 `/mnt/c/...` |
| `__pycache__` 出現在 git status | `PYTHONDONTWRITEBYTECODE=1` 沒生效,因為之前的 run 已經寫過了 | `rm -rf $(find symphony_mvp -name __pycache__)` 一次,之後就乾淨 |

---

## 6. `scripts/dev.sh` 指令對照

| 子指令 | 做什麼 |
|---|---|
| `up` | (需要時) build + 起 detached dev container。印出健康檢查跟 URL 提示 |
| `down` | 停掉 + 移除 container。Named volume (workspaces) 保留 |
| `nuke` | `down -v` — 連 workspaces volume 都丟掉。要從乾淨狀態開始時用 |
| `restart` | 只 restart container — 改完 code 之後最快的迭代方式 |
| `logs` | `docker compose logs -f dashboard-dev`,Ctrl-C 離開 |
| `shell` | 進 container 開 bash |
| `test` | 在 container 內跑 `python -m pytest`。可傳參:`./scripts/dev.sh test -k mode` |
| `lint` | 在 container 內跑 `ruff check` (假設 ruff 在 `[dev]` extras 裡) |
| `ps` | 看 container 狀態 |
| `build` | 強制 no-cache rebuild dev image。改了 `Dockerfile.dev` 或 `pyproject.toml` 時用 |
| `help` | 印完整子指令清單跟設計意圖 |

---

## 7. 跟其他 wrapper 風格的取捨

你問:**為什麼用 shell script,不用 Makefile 或乾脆裸 compose?**
這份對照表是當初的決策依據:

| 做法 | 優點 | 缺點 | 為什麼選 / 不選 |
|---|---|---|---|
| `./scripts/dev.sh` (採用) | 原生 bash,不裝額外工具,容易加條件分支跟有用的訊息,每個 shell 行為一致 | bash-isms 容易溜進來,host 要有 `bash` / `zsh` | **採用**:對這個 WSL2 / Linux-first 受眾最合適;留空間加 health-check 橫幅這種小體貼 |
| `Makefile` | 普及,`make help` 是社群慣例,target-based syntax | tab vs space 的雷,shell + Make syntax 混在一起很容易踩 bug,沒地方放多行邏輯 | **不選**:wrapper 做了夠多條件邏輯 (傳參給 pytest、`nuke` 模式),用 Make 會強迫寫一堆難看的 `$$` escape |
| 裸 `docker compose -f docker-compose.dev.yml ...` | 零新檔案、零魔法 | 指令一長串,沒有友善訊息,多步驟操作 (up + ps + health curl) 包不起來 | **不選**:user 明確要求「一鍵」框架 — wrapper 層才是重點 |

compose 檔本身才是 source of truth,`scripts/dev.sh` 只是方便層。如果
你偏好裸 compose,每個子指令都對得到一個 one-liner:

```bash
# `./scripts/dev.sh up`
docker compose -f docker-compose.dev.yml up -d --build

# `./scripts/dev.sh test -k mode`
docker compose -f docker-compose.dev.yml exec dashboard-dev python -m pytest -k mode

# `./scripts/dev.sh restart`
docker compose -f docker-compose.dev.yml restart dashboard-dev
```

---

## 8. 前端 dev — 為什麼留在 host

我們刻意 **不** 把前端 dev server 包進 container。原因:

1. **Bind mount 上的 file watching 不可靠**。Vite / Chokidar 用 inotify;
   WSL2 跟 macOS Docker Desktop 上,bind mount 的 inotify 事件常常飛掉。
   替代方案是 polling,但 `node_modules` 那麼大的樹會吃爆 CPU。
2. **`node_modules` shadowing 是另一個坑**。把 `./frontend:/app/frontend`
   bind mount 進去會把 image 裡的 `node_modules` 蓋掉 (host 的可能空的
   或 platform 不對)。修法是給 `/app/frontend/node_modules` 一個
   anonymous volume,但那會破壞「host 編輯、host npm install」的肌肉記憶。
3. **Vite 本來就已經 proxy 到後端**。[`frontend/vite.config.ts`](../../frontend/vite.config.ts)
   把 `/api/*` 跟 `/api/v1/events` 的 WebSocket 轉到 `127.0.0.1:7957`,
   剛好就是這個 dev container publish 的位置。零 config 變更。

直接跑:

```bash
cd frontend
npm install      # 一次性
npm run dev      # → http://localhost:5173
```

瀏覽器就會在 hot-reload SPA 的同時打到 container 裡的後端。

---

## 9. Dev 切到 Prod

dev compose 是純粹疊加上去的 — `docker-compose.yml` (production) 完全
沒動。要切過去:

```bash
./scripts/dev.sh down            # 停 dev
docker compose up -d --build     # 起 prod,host :17957
```

Production image 已經把 build 好的 SPA 包進去,所以 host 端的前端
dev server **不需要** 開。完整 production reference 看
[docker-quickstart.zh-TW.md](docker-quickstart.zh-TW.md)。

---

## 10. 想看更多

- Production: [`docs/guide/docker-quickstart.zh-TW.md`](docker-quickstart.zh-TW.md)
- 把 Symphony 接成 coding-service backend:
  [`docs/guide/user-manual.zh-TW.md`](user-manual.zh-TW.md)
- 架構 + FSM + runner 取捨: [`README.md`](../../README.md) (英文)
  / [`docs/README.zh-TW.md`](../README.zh-TW.md) (中文)
- 已知 gap / roadmap: [`docs/todolist/post-mvp-gaps.md`](../todolist/post-mvp-gaps.md)
