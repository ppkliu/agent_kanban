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
    $COMPOSE up -d --build
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
