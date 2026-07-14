#!/usr/bin/env bash
set -euo pipefail

# Initialize Beads (bd) against an external Dolt SQL server.
# This script is idempotent and can be used as a Docker entrypoint.

DOLT_HOST="${DOLT_HOST:-dolt-server}"
DOLT_PORT="${DOLT_PORT:-3306}"
DOLT_USER="${DOLT_USER:-root}"
DOLT_PASSWORD="${DOLT_PASSWORD:-}"
DOLT_DB="${DOLT_DB:-shipply_beads}"
BEADS_DIR="${BEADS_DIR:-/app/.beads}"
INIT_MARKER="${INIT_MARKER:-${BEADS_DIR}/.initialized}"

mkdir -p "$BEADS_DIR"
cd /app

wait_for_dolt() {
    echo "Waiting for Dolt SQL server at ${DOLT_HOST}:${DOLT_PORT}..."
    local i
    for i in $(seq 1 30); do
        if bash -c "exec 3<> /dev/tcp/${DOLT_HOST}/${DOLT_PORT}" 2>/dev/null; then
            echo "Dolt SQL server is reachable."
            return 0
        fi
        sleep 2
    done
    echo "Timed out waiting for Dolt SQL server at ${DOLT_HOST}:${DOLT_PORT}."
    return 1
}

init_beads() {
    if [ -f "$INIT_MARKER" ]; then
        echo "Beads already initialized ($INIT_MARKER exists); skipping."
        return 0
    fi

    echo "Initializing Beads in server mode against ${DOLT_HOST}:${DOLT_PORT}..."
    export BEADS_DOLT_PASSWORD="${DOLT_PASSWORD}"

    bd init --server \
        --server-host "$DOLT_HOST" \
        --server-port "$DOLT_PORT" \
        --server-user "$DOLT_USER" \
        --database "$DOLT_DB" \
        --non-interactive \
        --init-if-missing \
        --skip-hooks \
        --skip-agents

    mkdir -p "$(dirname "$INIT_MARKER")"
    touch "$INIT_MARKER"
    echo "Beads initialized successfully."
}

wait_for_dolt
init_beads

# If additional arguments are provided, exec them (useful as an entrypoint wrapper).
if [ $# -gt 0 ]; then
    exec "$@"
fi
