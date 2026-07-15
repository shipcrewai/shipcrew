#!/usr/bin/env bash
# Idempotent one-shot provisioning of the shared OMP environment.
#
# Reads shipply.toml (or the file pointed to by SHIPPLY_CONFIG), renders
# config.yml and models.yml into a staging directory, installs/pins the
# plugins and skills declared in the [omp] section, verifies the result with
# the OMP CLI, and atomically swaps the staging directory into PI_CONFIG_DIR.
#
# Environment variables:
#   SHIPPLY_CONFIG      Path to shipply.toml (default: shipply.toml)
#   PI_CONFIG_DIR       Active OMP config directory (default: /etc/omp)
#   PI_CONFIG_STAGING_DIR  Optional staging directory override.
#   PYTHON              Python interpreter to use (default: python3)
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
    # Place the staging directory inside the active directory so that the
    # atomic swap and the OMP install/verify commands all operate on the same
    # shared volume. The staging directory is moved out before the active
    # directory is replaced.
    mkdir -p "$TARGET_DIR"
    STAGING_DIR="$(mktemp -d "${TARGET_DIR}/.staging.XXXXXX")"
fi

export CONFIG_FILE STAGING_DIR TARGET_DIR

PYTHON="${PYTHON:-python3}"

# Render config files, install/pin plugins and skills, and verify with the OMP CLI.
# This step intentionally runs before the atomic swap so a verification failure
# never corrupts the active directory.
if ! "$PYTHON" - "$STAGING_DIR" "$TARGET_DIR" <<'PY';
import json
import os
import subprocess
import sys
from pathlib import Path

from shipply.config import load_config, render_omp_config_files

config_file = os.environ.get("SHIPPLY_CONFIG", "shipply.toml")
staging_dir = Path(sys.argv[1])
active_dir = Path(sys.argv[2])

config = load_config(config_file)
render_omp_config_files(config, staging_dir)

env = {**os.environ, "PI_CONFIG_DIR": str(staging_dir)}


def _installed_versions(manifest_path: Path) -> dict[str, str]:
    """Return a map of artifact name -> version from a JSON manifest.

    Tolerates both ``{"name": "version"}`` and ``{"name": {"version": "..."}}``.
    """
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    versions: dict[str, str] = {}
    for name, value in data.items():
        if isinstance(value, str):
            versions[name] = value
        elif isinstance(value, dict):
            versions[name] = value.get("version", "") or ""
        else:
            versions[name] = ""
    return versions


# Look at the active directory to decide whether a plugin/skill is already
# installed at the pinned version. The staging directory is created fresh each
# run, so its manifests are empty until install commands write into it.
active_plugins = _installed_versions(active_dir / "installed_plugins.json")
active_skills = _installed_versions(active_dir / "installed_skills.json")


def _install_if_needed(kind: str, name: str, version: str, installed: dict[str, str], env: dict) -> None:
    """Install an OMP artifact if the pinned version is not already present."""
    if installed.get(name) == version:
        print(f"{kind.capitalize()} {name}@{version} already installed; skipping.")
        return
    source = f"{name}@{version}" if version else name
    print(f"Installing {kind} {source}...")
    subprocess.run(["omp", kind, "install", source], check=True, env=env)


for name, version in config.omp.plugins.items():
    _install_if_needed("plugin", name, version, active_plugins, env)

for name, version in config.omp.skills.items():
    _install_if_needed("skill", name, version, active_skills, env)

print("Verifying OMP configuration...")
subprocess.run(["omp", "config", "list"], check=True, env=env)
subprocess.run(["omp", "plugin", "list"], check=True, env=env)
subprocess.run(["omp", "--version"], check=True, env=env)
print("OMP configuration verified successfully.")
PY
then
    echo "OMP provisioning failed; leaving ${TARGET_DIR} intact." >&2
    exit 1
fi

STAGING_BASENAME="$(basename "$STAGING_DIR")"

# Atomic swap: rename the active directory out of the way, then move the staging
# directory (which lives inside the active directory) into place. Because both the
# active directory and the staging directory are on the same shared volume, both
# moves are atomic renames.
BACKUP_DIR=""
if [ -d "$TARGET_DIR" ] || [ -L "$TARGET_DIR" ]; then
    BACKUP_DIR="${TARGET_DIR}.old"
    rm -rf "$BACKUP_DIR"
    mv "$TARGET_DIR" "$BACKUP_DIR"
    mv "${BACKUP_DIR}/${STAGING_BASENAME}" "$TARGET_DIR"
else
    mv "$STAGING_DIR" "$TARGET_DIR"
fi

# Clean up the backup directory if we created one.
if [ -n "$BACKUP_DIR" ]; then
    rm -rf "$BACKUP_DIR"
fi

# Print the final location for observability.
echo "OMP config provisioned at $TARGET_DIR"
ls -la "$TARGET_DIR"

# If additional arguments were provided, exec them (useful as an entrypoint wrapper).
if [ $# -gt 0 ]; then
    exec "$@"
fi
