#!/usr/bin/env bash
# Idempotent one-shot provisioning of the shared OMP environment.
#
# Reads shipply.toml (or the file pointed to by SHIPPLY_CONFIG), renders
# config.yml and models.yml into a staging directory, then atomically swaps the
# staging directory into PI_CONFIG_DIR.
#
# Environment variables:
#   SHIPPLY_CONFIG      Path to shipply.toml (default: shipply.toml)
#   PI_CONFIG_DIR       Active OMP config directory (default: /etc/omp)
#   PI_CONFIG_STAGING_DIR  Optional staging directory override.
#
# Provider API keys are never written by this script; only env-var names are
# rendered into models.yml. Actual credentials are resolved at runtime via the
# OMP auth broker.
set -euo pipefail

CONFIG_FILE="${SHIPPLY_CONFIG:-shipply.toml}"
TARGET_DIR="${PI_CONFIG_DIR:-/etc/omp}"

if [ -n "${PI_CONFIG_STAGING_DIR:-}" ]; then
    STAGING_DIR="$PI_CONFIG_STAGING_DIR"
else
    STAGING_DIR="$(mktemp -d "${TARGET_DIR}.staging.XXXXXX")"
fi

# Ensure the parent directory exists so the atomic rename is on the same fs.
mkdir -p "$(dirname "$TARGET_DIR")"

# Render OMP YAML files into the staging directory. This is done in Python so
# the exact schema and env-var references live in shipply.config alongside the
# Pydantic models. ``PYTHON`` can be overridden for tests or containers.
PYTHON="${PYTHON:-python3}"
"$PYTHON" - "$CONFIG_FILE" "$STAGING_DIR" <<'PY'
import sys
from pathlib import Path
from shipply.config import load_config, render_omp_config_files

config_path = Path(sys.argv[1])
staging_dir = Path(sys.argv[2])
config = load_config(config_path)
render_omp_config_files(config, staging_dir)
PY

# Atomic swap: rename the active directory out of the way, then move staging in.
if [ -d "$TARGET_DIR" ] || [ -L "$TARGET_DIR" ]; then
    BACKUP_DIR="${TARGET_DIR}.old"
    rm -rf "$BACKUP_DIR"
    mv "$TARGET_DIR" "$BACKUP_DIR"
fi

mv "$STAGING_DIR" "$TARGET_DIR"

# Clean up the backup directory if we created one.
if [ -n "${BACKUP_DIR:-}" ]; then
    rm -rf "$BACKUP_DIR"
fi

# Print the final location for observability.
echo "OMP config provisioned at $TARGET_DIR"
ls -la "$TARGET_DIR"
