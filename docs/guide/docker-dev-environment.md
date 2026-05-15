# Docker dev environment — Symphony backend in a container, frontend on host

> 中文版: [docker-dev-environment.zh-TW.md](docker-dev-environment.zh-TW.md)

This guide gets you a one-command **development** environment: backend
running in Docker with all Python tooling pre-installed, source code
bind-mounted from the host so your editor sees the same files the
container does, and the frontend dev server running on the host with
Vite's proxy pointing into the container.

It is **separate** from the production deployment in
[`docker-quickstart.md`](docker-quickstart.md) — production builds the
SPA into the image and serves a single self-contained binary; this dev
flow expects you to edit code on the host and exercise it inside the
container.

---

## 1. What this gives you

| Concern | This setup |
|---|---|
| Python toolchain | Pre-installed in `Dockerfile.dev` (`pip install -e ".[dev,dashboard]"`) — no `uv venv` on the host |
| opencode CLI | Bundled in the dev image too, so OpenCodeRunner tests can run inline |
| Source code | Bind-mounted from host — edit in your IDE, container sees it instantly |
| `__pycache__` churn on host | Suppressed via `PYTHONDONTWRITEBYTECODE=1` |
| Hot reload | **Intentionally not enabled** — `./scripts/dev.sh restart` is ~2s and gives you a clean re-import |
| File watching | Works on WSL2 / macOS / Linux — see §5 for caveats |
| Frontend dev server | Stays on host (`cd frontend && npm run dev`) — Vite proxies API + WebSocket to the dev container on `:7957` |
| Host port | `:7957` (dev port). Production uses `:17957` so the two can coexist |

---

## 2. One-command start

```bash
git clone <this-repo>
cd agent_kanban

# (one-time) put a WORKFLOW.md in place. Either the echo demo or opencode default works.
cp examples/WORKFLOW.demo-echo.md WORKFLOW.md

# Bring up the dev backend
./scripts/dev.sh up

# In another terminal: frontend dev server (host-side)
cd frontend && npm install && npm run dev
# → open http://localhost:5173
```

That's it. Edit `symphony_mvp/**.py` in your IDE; when the change is
ready to exercise, `./scripts/dev.sh restart`. Edit anything under
`frontend/src/`; Vite hot-reloads in the browser automatically.

---

## 3. What's in the dev image

The dev image is defined in [`Dockerfile.dev`](../../Dockerfile.dev) at
the repo root. It is **backend-only** — no SPA build stage. Roughly
~500 MB built:

| Layer | Contents |
|---|---|
| `python:3.12-slim` base | Python runtime + glibc |
| OS deps | `git`, `curl`, `ca-certificates`, `nodejs`, `npm` (for opencode-ai) |
| opencode CLI | `npm install -g opencode-ai` so OpenCodeRunner can spawn it |
| Python deps | `pip install -e ".[dev,dashboard]"` — fastapi, uvicorn, pydantic, pytest, requests, watchfiles, etc. |
| Source skeleton | The `COPY symphony_mvp/` is just bootstrap so editable install resolves metadata; the bind mount at runtime overlays it with live code |

---

## 4. Volume layout

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

Source is **read-write** so the container can write things like
`*.egg-info` and a few cache files, but the things you'd never want to
write back (tests, pyproject) are mounted **read-only** as a guardrail.
Per-issue agent workspaces live on a **named** volume, not a host bind,
so the frontend dev server doesn't churn on workspace file changes.

---

## 5. File sync — what actually happens

When you edit `symphony_mvp/dashboard/tool_api.py` on the host:

1. Docker bind-mount delivers the new bytes into the container's
   filesystem immediately (no copy step).
2. The container's running uvicorn process has **already imported** the
   old code into memory. New requests will still hit the old code.
3. `./scripts/dev.sh restart` sends SIGTERM, the container re-execs, and
   Python re-imports from the now-updated bind mount. ~2 seconds.

That's the entire model. There is no auto-reload, no `watchdog`, no
inotify magic, no daemon polling — because the alternative (uvicorn
`--reload`) is hostile to a long-running orchestrator thread that owns
SQLite handles. The 2-second restart cost is the right trade-off.

### WSL2 / macOS gotchas (rare but real)

| Symptom | Likely cause | Fix |
|---|---|---|
| Host edit not visible in container | You're editing inside `\\wsl$` from Windows but Docker Desktop is configured for WSL2 integration with a different distro | Verify with `docker compose exec dashboard-dev cat /app/symphony_mvp/__init__.py`; if stale, `restart` |
| `vite dev` doesn't hot-reload on save | Editor doing atomic save (write to tmp + rename); Vite watches the original inode | Disable atomic save in your editor, or set `VITE_CHOKIDAR_USEPOLLING=true` |
| `restart` is slow (10+ seconds) | Pip's editable install is re-doing metadata checks on a slow filesystem | Make sure the project is on a **native** ext4 (WSL2 home) rather than `/mnt/c/...` |
| `__pycache__` appearing in your git status | `PYTHONDONTWRITEBYTECODE=1` not respected because a previous run wrote them | `rm -rf $(find symphony_mvp -name __pycache__)` once; it stays clean |

---

## 6. `scripts/dev.sh` reference

| Subcommand | What it does |
|---|---|
| `up` | Build (if needed) + start the dev container detached. Prints health-check + URL hints. |
| `down` | Stop + remove the container. Named volumes (workspaces) persist. |
| `nuke` | `down -v` — also drops the workspaces volume. Use when you want a clean slate. |
| `restart` | Restart the container only — fastest dev iteration after a source edit. |
| `logs` | `docker compose logs -f dashboard-dev`. Ctrl-C to detach. |
| `shell` | Open an interactive bash shell inside the dev container. |
| `test` | Run `python -m pytest` inside the container. Forwards args: `./scripts/dev.sh test -k mode`. |
| `lint` | Run `ruff check` inside the container (assuming ruff is in `[dev]` extras). |
| `ps` | Show container status. |
| `build` | Force a no-cache rebuild of the dev image. Use when `Dockerfile.dev` or `pyproject.toml` change. |
| `help` | Print the full subcommand list and design intent. |

---

## 7. Trade-offs vs other wrapper styles

You asked: **why a shell script, not a Makefile or raw compose?** Here's
the comparison that drove the choice:

| Approach | Pros | Cons | Why we picked / passed |
|---|---|---|---|
| `./scripts/dev.sh` (chosen) | Native bash, no extra tool, easy to add conditionals + helpful messages, runs the same on every shell | Bash-isms can creep in; requires `bash`/`zsh` host shell | **Picked**: best fit for project's WSL2/Linux-first audience; gives us room for niceties like the health-check banner |
| `Makefile` | Universal, `make help` is a community pattern, target-based syntax | Tab-vs-space gotchas, mixing shell + Make syntax invites bugs, no obvious place for multi-line logic | **Passed**: the wrapper does enough conditional work (forwarding args to pytest, `nuke` mode) that Make would force awkward `$$` escapes |
| Raw `docker compose -f docker-compose.dev.yml ...` | Zero new files, zero magic | Long command strings, no nice messages, no way to package multi-step ops (`up` + `ps` + health curl) | **Passed**: the user explicitly asked for "one-click" framing — a wrapper layer is the whole point |

The compose file itself is the source of truth — `scripts/dev.sh` is
just a convenience layer. If you prefer raw compose, every subcommand
above maps to a one-liner you can run directly:

```bash
# `./scripts/dev.sh up`
docker compose -f docker-compose.dev.yml up -d --build

# `./scripts/dev.sh test -k mode`
docker compose -f docker-compose.dev.yml exec dashboard-dev python -m pytest -k mode

# `./scripts/dev.sh restart`
docker compose -f docker-compose.dev.yml restart dashboard-dev
```

---

## 8. Frontend dev — why it stays on the host

We deliberately do NOT containerize the frontend dev server. Reasons:

1. **File-watching on bind mounts is unreliable.** Vite/Chokidar uses
   inotify; on WSL2 and macOS Docker Desktop, bind-mount inotify events
   are flaky. The workaround is polling, which spikes CPU on a
   `node_modules`-sized tree.
2. **`node_modules` shadowing is its own can of worms.** Bind-mounting
   `./frontend:/app/frontend` overwrites the in-image `node_modules`
   with an empty (or platform-mismatched) host dir. The fix is an
   anonymous volume on `/app/frontend/node_modules`, but that breaks
   "edit on host, npm-install-on-host" muscle memory.
3. **Vite already proxies to the backend.** [`frontend/vite.config.ts`](../../frontend/vite.config.ts)
   forwards `/api/*` and the WebSocket at `/api/v1/events` to
   `127.0.0.1:7957` — exactly where this dev container publishes. Zero
   config changes needed.

Just run:

```bash
cd frontend
npm install      # one-time
npm run dev      # → http://localhost:5173
```

and your browser will hot-reload the SPA while talking to the
containerized backend.

---

## 9. Going from dev to prod

The dev compose is purely additive — `docker-compose.yml` (production)
is untouched. To switch:

```bash
./scripts/dev.sh down            # stop dev
docker compose up -d --build     # bring up prod on :17957
```

The production image bakes the built SPA in, so the host frontend dev
server is **not** needed there. See [docker-quickstart.md](docker-quickstart.md)
for the full production reference.

---

## 10. Going further

- Production: [`docs/guide/docker-quickstart.md`](docker-quickstart.md)
- Integrate Symphony as a coding-service backend:
  [`docs/guide/user-manual.md`](user-manual.md)
- Architecture + FSM + runner trade-offs: [`README.md`](../../README.md)
- Known gaps / roadmap: [`docs/todolist/post-mvp-gaps.md`](../todolist/post-mvp-gaps.md)
