# Docker 快速啟動 — Symphony Dashboard

> English: [docker-quickstart.md](docker-quickstart.md)

這份指南帶你從 `git clone` 一路到 **http://localhost:17957** 看到 Dashboard。

目前 repo 落地的 scaffolding (根目錄的 `Dockerfile` + `docker-compose.yml`)
是**單容器**版本 — dashboard + 內建 `opencode` CLI 包在同一個 image,workspace
透過 volume 掛入。原本文件描述的雙容器爆炸半徑切分 (獨立 `symphony-opencode`
service) 留到 [`post-mvp-gaps.md`](../todolist/post-mvp-gaps.md) 追蹤;那需要
動到 runner 端的程式碼,不適合在這個 milestone 一起做。

---

## 1. 前置需求

| 需求 | 版本 | 備註 |
|---|---|---|
| Docker Engine | 24+ | macOS / Windows 用 Docker Desktop;Linux 原生最佳 |
| `docker compose` | v2 (內建子命令) | 舊版 `docker-compose` 也接受 |
| 模型 endpoint | Ollama / vLLM / SGLang / LM Studio / LiteLLM proxy / 雲端 API key 任一 | 預設是 Ollama 跑在 `host.docker.internal:11434` |
| 空閒 TCP port | 17957 (host) | 跟其他服務衝突的話到 `docker-compose.yml` 改 |

Dashboard 容器**不需要對外網路**,只有 opencode 容器會出去,而且只連到你
指給它的模型 endpoint。

## 2. 啟動

```bash
git clone <this-repo>
cd agent_kanban

# 容器需要一個 WORKFLOW.md 可以掛入 (內建範例用 in-memory tracker
# + opencode + ollama,需要時自行調整)
cp examples/WORKFLOW.docker.md WORKFLOW.md

# (建議) 從範例複製 .env 並設一個 bearer token
cp .env.example .env
echo "DASHBOARD_API_KEY=$(openssl rand -hex 32)" >> .env

docker compose up -d
docker compose ps          # dashboard 應該 "Up (health: starting → healthy)"
docker compose logs -f dashboard
```

開瀏覽器到 `http://localhost:17957`,應該會看到 5 欄的 Kanban。

幾條 sanity check:

```bash
curl -fsS http://localhost:17957/healthz                  # → {"ok": true}
docker compose exec dashboard which opencode              # → /usr/local/bin/opencode
docker compose exec dashboard symphony-mvp /app/WORKFLOW.md --validate
```

## 3. 配置

所有可調的參數都是 compose service 的環境變數。最常用的:

| 變數 | 預設值 | 作用 |
|---|---|---|
| `DASHBOARD_API_KEY` | _(空 = 開放模式)_ | 設了之後,所有 REST + WebSocket 請求都要帶 bearer token |
| `OPENCODE_PROVIDER` | `ollama` | LiteLLM provider id — `ollama` / `openai` / `anthropic` / `vllm` 等 |
| `OPENCODE_BASE_URL` | `http://host.docker.internal:11434` | opencode 連到模型的 URL |
| `OPENCODE_MODEL` | _(透過 WORKFLOW.md 設定)_ | 模型名稱 (`qwen2.5-coder:32b`、`gpt-4o`、…) |
| `OPENCODE_API_KEY` | _(空)_ | 雲端 provider 才需要 (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`) |

把這些放在跟 `docker-compose.yml` 同層的 `.env`,compose 會自動讀。

### Volumes (host 上會持久化的東西)

| Mount | 用途 |
|---|---|
| `./data:/app/data` | `dashboard.db` — 事件 / hint / priority override / attempt 歷程 |
| `./WORKFLOW.md:/app/WORKFLOW.md:ro` | 可熱載入的 orchestrator policy + prompt template |
| `workspaces` (named volume, 雙容器共用) | 每個 issue 的 agent workspace;dashboard 跟 opencode 都看得到 |

要從零來過:`docker compose down -v` (連 volume 一起砍)。

## 4. 用 WORKFLOW.md 選 runner

`WORKFLOW.md` 是 orchestrator 行為的單一真相來源。`runner:` 區塊選 backend,
其他段落配置 dispatch / retry / handoff。預設用 opencode 接本地 LLM:

```yaml
---
tracker:
  kind: memory                  # 內建本機 kanban (沒有 SaaS)
agent:
  max_concurrent_agents: 2
  max_turns: 20
  retry_max_attempts: 3
  handoff_state: in_review
runner:
  kind: opencode
  provider: ollama
  model: qwen2.5-coder:32b
  allowed_tools: [bash, read, edit, write]
---
你是一個自主工程師,負責處理 issue {{ issue.identifier }}。
{{ issue.title }}

{{ issue.description }}
```

困難 ticket 切到雲端 — 改一行就好:

```yaml
runner:
  kind: opencode
  provider: anthropic            # 必須設 OPENCODE_API_KEY
  model: claude-opus-4-7
```

完整有註解的範例放在 `examples/WORKFLOW.md`。

## 5. 生產環境加固

| 項目 | 建議 |
|---|---|
| TLS | 在 host 上用 Caddy / nginx / Traefik 擋一層,在那邊終結 TLS |
| 認證 | 不是單機自用都務必設 `DASHBOARD_API_KEY` |
| 備份 | `docker compose stop dashboard && cp data/dashboard.db backup.db` (SQLite WAL 熱備可行) |
| 重啟 | compose 已寫 `restart: unless-stopped`;orchestrator 開機後從 tracker + 檔案系統重建狀態 |
| Outbound 網路 | 只有 `opencode` 容器需要 egress;若 host policy 允許,把 `dashboard` 容器隔離 |

## 6. 疑難排解

| 症狀 | 通常原因 / 解法 |
|---|---|
| 瀏覽器 :17957 看不到東西 | `docker compose ps` — `dashboard` 是 Up 嗎?`logs dashboard` 看 port bind 是否衝突 |
| WebSocket 每幾秒就斷 | reverse proxy 把 `Upgrade` header 吃掉了,把 `Connection: Upgrade` + `Upgrade: websocket` 透傳 |
| `Bearer token` 401 | 確認 `.env` 有被讀到 (`docker compose config` 看得到);瀏覽器右上 ⚙ 也要填同一個 key |
| opencode 容器報 `connection refused` | `OPENCODE_BASE_URL` 不對;Linux 上 `host.docker.internal` 不會自動解析 — 用 host 的 bridge IP 或 `--network host` |
| Agent 卡在 `claimed` | `docker compose logs opencode` 看,9 成是 provider 認證問題 |
| 想全部清乾淨 | `docker compose down -v && rm -rf data/ workspaces/` |

## 7. 延伸閱讀

- 架構、FSM、runner 取捨:[`docs/README.zh-TW.md`](../README.zh-TW.md)
- Spec 符合度與設計理由:[`docs/design/symphony-dashboard-spec.md`](../design/symphony-dashboard-spec.md)
- 接新 agent backend:[`docs/design/agent-invocation-and-adapters.md`](../design/agent-invocation-and-adapters.md)
- Roadmap 與已知 gap:[`docs/todolist/post-mvp-gaps.md`](../todolist/post-mvp-gaps.md)
