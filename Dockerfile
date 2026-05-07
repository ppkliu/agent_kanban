# syntax=docker/dockerfile:1.7

# ------------------------------------------------------------
# Stage 1 — build the React SPA
# ------------------------------------------------------------
FROM node:20-alpine AS fe
WORKDIR /fe

COPY frontend/package.json frontend/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY frontend/ ./
RUN npm run build
# produces /fe/dist (static SPA)

# ------------------------------------------------------------
# Stage 2 — runtime: Python 3.12 + opencode CLI + symphony-mvp
# ------------------------------------------------------------
FROM python:3.12-slim AS rt

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps:
#   - nodejs/npm: needed both for `opencode` (npm package) and any `npx` use
#   - git: agents that wrap `gh` / git workflows expect it on PATH
#   - curl + ca-certificates: tarball fallback if the npm install path moves
RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs \
        npm \
        git \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# OpenCode CLI on PATH so OpenCodeRunner.subprocess.Popen finds it.
# The published package is `opencode-ai` (sst/opencode publishes under that
# name on npm). If the upstream package id ever moves, swap the line below.
RUN npm install -g opencode-ai

WORKDIR /app

# Install symphony-mvp (editable so the source layer is small).
COPY pyproject.toml requirements.txt ./
COPY symphony_mvp/ ./symphony_mvp/
RUN pip install -e ".[dashboard]"

# Frontend dist served by FastAPI's StaticFiles mount at /assets + SPA fallback.
# server.py resolves dist_dir as <pkg_root>/frontend/dist, where pkg_root is
# Path(server.py).resolve().parents[2]. With the package at /app/symphony_mvp/
# that resolves to /app, so /app/frontend/dist is the expected location.
COPY --from=fe /fe/dist /app/frontend/dist

# Runtime data lives on volumes so it survives container restarts.
RUN mkdir -p /app/data /app/workspaces

EXPOSE 7957
VOLUME ["/app/data", "/app/workspaces"]

# Default entrypoint: FastAPI + orchestrator on port 7957 inside the container.
# docker-compose.yml publishes it on host port 17957.
CMD ["symphony-dashboard", "/app/WORKFLOW.md", \
     "--host", "0.0.0.0", \
     "--port", "7957", \
     "--db", "/app/data/dashboard.db"]
