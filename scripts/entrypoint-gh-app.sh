#!/usr/bin/env bash
set -euo pipefail

# Entrypoint wrapper for services that need a GitHub App installation token.
# Requests a source-org token from the token manager service, falls back to a
# local GITHUB_TOKEN file or env var, then exports GH_TOKEN and GITHUB_TOKEN
# for the wrapped command (e.g., the ``gh`` CLI).

GITHUB_TOKEN_MANAGER_URL="${GITHUB_TOKEN_MANAGER_URL:-}"
GITHUB_TOKEN_MANAGER_SECRET="${GITHUB_TOKEN_MANAGER_SECRET:-}"
GITHUB_TOKEN_FILE="${GITHUB_TOKEN_FILE:-/run/secrets/github-token}"

TOKEN=""

if [ -n "$GITHUB_TOKEN_MANAGER_URL" ]; then
    # Request a source token from the token manager.
    AUTH_HEADER=""
    if [ -n "$GITHUB_TOKEN_MANAGER_SECRET" ]; then
        AUTH_HEADER="Authorization: Bearer $GITHUB_TOKEN_MANAGER_SECRET"
    fi
    # shellcheck disable=SC2086
    TOKEN=$(curl -fsS --retry 3 --retry-delay 2 \
        -H "Accept: application/json" \
        ${AUTH_HEADER:+-H "$AUTH_HEADER"} \
        "$GITHUB_TOKEN_MANAGER_URL/token/source" 2>/dev/null | jq -r '.token // empty')
fi

if [ -z "$TOKEN" ] && [ -f "$GITHUB_TOKEN_FILE" ]; then
    TOKEN=$(cat "$GITHUB_TOKEN_FILE")
fi

if [ -z "$TOKEN" ]; then
    TOKEN="${GITHUB_TOKEN:-}"
fi

if [ -n "$TOKEN" ]; then
    export GH_TOKEN="$TOKEN"
    export GITHUB_TOKEN="$TOKEN"
fi

exec "$@"
