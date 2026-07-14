#!/usr/bin/env bash
set -euo pipefail

# Entrypoint wrapper for services that need a GitHub token.
# Reads the token from a Docker secret or mounted file and exports it for
# the `gh` CLI, then execs the original command.

GITHUB_TOKEN_FILE="${GITHUB_TOKEN_FILE:-/run/secrets/github-token}"

if [ -f "$GITHUB_TOKEN_FILE" ]; then
    GH_TOKEN="$(cat "$GITHUB_TOKEN_FILE")"
    export GH_TOKEN
    export GITHUB_TOKEN="$GH_TOKEN"
fi

exec "$@"
