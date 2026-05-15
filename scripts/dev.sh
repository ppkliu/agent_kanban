#!/usr/bin/env bash
# Symphony dev environment wrapper — thin shell around `docker compose
# -f docker-compose.dev.yml`. Design rationale and the comparison against
# Makefile / raw compose live in docs/guide/docker-dev-environment.md.
#
# Usage: ./scripts/dev.sh <subcommand> [args...]
#
# Subcommands:
#   up        Build (if needed) + start the dev container detached
#   down      Stop + remove the dev container (keeps named volumes)
#   nuke      down + drop the named volume (loses workspace state)
#   restart   Re-import source after host edits — fastest dev iteration
#   logs      Tail the dev container logs (Ctrl-C to detach)
#   shell     Open an interactive bash shell inside the dev container
#   test      Run the backend test suite inside the dev container
#   lint      Run the linter (ruff) inside the dev container
#   ps        Show container status
#   build     Force a rebuild of the dev image
#   help      Show this help text
#
# The frontend dev server stays on the host (cd frontend && npm run dev) —
# vite proxies REST + WebSocket to this backend container on :7957. See
# docs/guide/docker-dev-environment.md for why frontend is NOT in Docker.

set -euo pipefail

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

readonly COMPOSE="docker compose -f docker-compose.dev.yml"
readonly SERVICE="dashboard-dev"

# --------------------------------------------------------------------------- #
# Subcommand handlers                                                         #
# --------------------------------------------------------------------------- #

cmd_up() {
    # Pre-flight: WORKFLOW.md must exist on the host as a file. If it's
    # missing, the `./WORKFLOW.md:/app/WORKFLOW.md:ro` bind-mount makes
    # Docker auto-create a DIRECTORY at the source path, and the container
    # then crashes loading "/app/WORKFLOW.md" as a workflow file
    # (IsADirectoryError). Catch it before that footgun fires.
    if [ ! -e WORKFLOW.md ]; then
        echo "ERROR: ./WORKFLOW.md is missing." >&2
        echo "  → Copy one of the shipped examples first:" >&2
        echo "    cp examples/WORKFLOW.demo-echo.md WORKFLOW.md   # zero-LLM demo" >&2
        echo "    cp examples/WORKFLOW.docker.md     WORKFLOW.md   # opencode + local LLM" >&2
        exit 1
    fi
    if [ -d WORKFLOW.md ]; then
        echo "ERROR: ./WORKFLOW.md is a directory, not a file." >&2
        echo "  This usually means a prior \`up\` ran without WORKFLOW.md and" >&2
        echo "  Docker auto-created the bind-mount source as a directory." >&2
        echo "  → Remove it (often root-owned; try in order):" >&2
        echo "    rmdir WORKFLOW.md                    # if empty + parent is yours" >&2
        echo "    sudo rm -rf WORKFLOW.md              # otherwise" >&2
        echo "  Then re-copy a workflow file and re-run \`./scripts/dev.sh up\`." >&2
        exit 1
    fi

    $COMPOSE up -d --build

    # Post-up health gate: the container can `Started` then exit within ~1s
    # if the workflow load, DB open, or static-file mount fails. Surface the
    # tail of logs immediately so the user doesn't have to hunt.
    sleep 2
    if [ -z "$($COMPOSE ps -q $SERVICE 2>/dev/null)" ]; then
        echo
        echo "ERROR: $SERVICE is not running. Tail of logs:" >&2
        $COMPOSE logs --tail 50 $SERVICE >&2
        exit 1
    fi

    echo
    echo "→ Dev backend ready at http://localhost:7957"
    echo "→ Health:  curl -fsS http://localhost:7957/healthz"
    echo "→ Frontend:  cd frontend && npm run dev  (vite at :5173, proxies here)"
    echo
    $COMPOSE ps
}

cmd_down() {
    $COMPOSE down "$@"
}

cmd_nuke() {
    $COMPOSE down -v
    echo "→ Volumes dropped. Next \`up\` will start with a clean slate."
}

cmd_restart() {
    $COMPOSE restart $SERVICE
    echo "→ Restarted in $(( $(date +%s) - ${SECONDS_AT_START:-$(date +%s)} ))s"
}

cmd_logs() {
    $COMPOSE logs -f "$@" $SERVICE
}

cmd_shell() {
    $COMPOSE exec $SERVICE /bin/bash
}

cmd_test() {
    # Forwards any remaining args to pytest (e.g. `./scripts/dev.sh test -k mode`)
    $COMPOSE exec $SERVICE python -m pytest "$@"
}

cmd_lint() {
    # ruff if it's installed in the dev image; otherwise warn but don't fail
    if $COMPOSE exec $SERVICE python -c "import ruff" 2>/dev/null; then
        $COMPOSE exec $SERVICE python -m ruff check symphony_mvp/ tests/ "$@"
    else
        echo "ruff not installed in dev image — add it to pyproject.toml [dev] extras"
        exit 1
    fi
}

cmd_ps() {
    $COMPOSE ps
}

cmd_build() {
    $COMPOSE build --no-cache "$@"
}

cmd_help() {
    sed -n '1,/^set -e/p' "$0" | sed -e '1d' -e 's/^# \{0,1\}//' -e '$d'
}

# --------------------------------------------------------------------------- #
# Dispatch                                                                    #
# --------------------------------------------------------------------------- #

main() {
    local cmd="${1:-help}"
    shift || true
    case "$cmd" in
        up|down|nuke|restart|logs|shell|test|lint|ps|build|help)
            "cmd_$cmd" "$@"
            ;;
        -h|--help)
            cmd_help
            ;;
        *)
            echo "unknown subcommand: $cmd" >&2
            echo "run \`./scripts/dev.sh help\` for the full list" >&2
            exit 2
            ;;
    esac
}

main "$@"
