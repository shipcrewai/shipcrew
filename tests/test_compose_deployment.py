"""Tests for the Docker Compose deployment and secrets handling."""

from __future__ import annotations

import fnmatch
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _docker_compose_config(*profiles: str) -> dict[str, Any]:
    """Return the parsed ``docker compose config --format json`` output.

    Pass profile names to include optional services such as ``auth-broker``.
    """
    cmd = ["docker", "compose"]
    for profile in profiles:
        cmd.extend(["--profile", profile])
    cmd.extend(["config", "--format", "json"])
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _git_tracked_files() -> list[str]:
    """Return paths currently tracked by git."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


@pytest.fixture(scope="module")
def compose_config() -> dict[str, Any]:
    return _docker_compose_config()


@pytest.fixture(scope="module")
def compose_config_with_broker() -> dict[str, Any]:
    return _docker_compose_config("auth-broker")


@pytest.fixture(scope="module")
def git_tracked() -> list[str]:
    return _git_tracked_files()


class TestComposeConfig:
    """Assertions against the rendered Docker Compose configuration."""

    def test_docker_compose_config_is_valid(self, compose_config: dict[str, Any]) -> None:
        """``docker compose config`` parses and produces a services mapping."""
        assert "services" in compose_config
        assert "secrets" in compose_config

    def test_github_app_token_manager_service_exists(self, compose_config: dict[str, Any]) -> None:
        services = compose_config["services"]
        assert "github-app-token-manager" in services
        manager = services["github-app-token-manager"]
        assert manager["command"] == [
            "uvicorn",
            "shipply.token_manager_service:create_app",
            "--factory",
            "--host",
            "0.0.0.0",
            "--port",
            "8001",
        ]
        assert manager["read_only"] is True
        secret_sources = {s.get("source") for s in manager.get("secrets", [])}
        assert "github-app-private-key" in secret_sources
        env = manager.get("environment", {})
        assert env.get("GITHUB_APP_PRIVATE_KEY_PATH") == "/run/secrets/github-app-private-key"
        # The token manager must not be exposed on a public ingress port.
        assert "ports" not in manager or not manager["ports"]

    def test_no_github_token_secret_remains(self, compose_config: dict[str, Any]) -> None:
        secrets = compose_config.get("secrets", {})
        assert "github-token" not in secrets

    def test_forge_and_gate3_use_app_entrypoint(self, compose_config: dict[str, Any]) -> None:
        services = compose_config["services"]
        for name in ("forge", "gate3"):
            service = services[name]
            assert service.get("entrypoint") == ["/app/scripts/entrypoint-gh-app.sh"]
            secret_sources = {s.get("source") for s in service.get("secrets", [])}
            assert "github-token" not in secret_sources
            env = service.get("environment", {})
            assert env.get("GITHUB_TOKEN_MANAGER_URL") == "http://github-app-token-manager:8001"
            assert "github-app-token-manager" in service.get("depends_on", {})

    def test_handlers_mount_omp_config_read_only(self, compose_config: dict[str, Any]) -> None:
        """Every handler service mounts the omp-config volume read-only."""
        handler_names = {
            "scout",
            "doc-review",
            "blueprint",
            "forge",
            "gate1",
            "gate2",
            "gate3",
            "github-webhook-bridge",
        }
        services = compose_config["services"]
        for name in handler_names:
            assert name in services, f"handler {name} missing from compose"
            volumes = services[name].get("volumes", [])
            assert any(
                v.get("source") == "omp-config"
                and v.get("target") == "/etc/omp"
                and v.get("read_only") is True
                for v in volumes
            ), f"{name} does not mount omp-config read-only"

    def test_handlers_are_read_only_with_tmpfs(self, compose_config: dict[str, Any]) -> None:
        handler_names = {
            "scout",
            "doc-review",
            "blueprint",
            "forge",
            "gate1",
            "gate2",
            "gate3",
            "github-webhook-bridge",
            "github-app-token-manager",
        }
        services = compose_config["services"]
        for name in handler_names:
            service = services[name]
            assert service.get("read_only") is True, f"{name} is not read_only"
            assert service.get("tmpfs"), f"{name} has no tmpfs mounts"


class TestBuildContextSecrets:
    """Assertions that no credential files leak into the build context or git."""

    @pytest.mark.parametrize(
        "pattern",
        [
            "*.pem",
        ],
    )
    def test_git_tracked_files_excluded_patterns(self, git_tracked: list[str], pattern: str) -> None:
        """No tracked files match excluded credential patterns."""
        matches = [
            p
            for p in git_tracked
            if fnmatch.fnmatch(p, pattern) or fnmatch.fnmatch(Path(p).name, pattern)
        ]
        assert not matches, f"tracked files match excluded pattern {pattern!r}: {matches}"

    def test_no_tracked_real_env_files(self, git_tracked: list[str]) -> None:
        """No real .env files (not .env.example templates) are tracked."""
        tracked_env = [
            p for p in git_tracked if Path(p).name.startswith(".env") and not p.endswith(".example")
        ]
        assert not tracked_env, f"tracked real .env files found: {tracked_env}"

    def test_no_tracked_secrets_directory(self, git_tracked: list[str]) -> None:
        """The secrets/ directory itself is not tracked."""
        tracked_dirs = {p.split("/")[0] for p in git_tracked if "/" in p}
        assert "secrets" not in tracked_dirs

    def test_no_tracked_shipply_toml(self, git_tracked: list[str]) -> None:
        assert "shipply.toml" not in git_tracked

    def test_dockerignore_excludes_credentials(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text()
        required = ["*.pem", ".env*", "secrets/", "shipply.toml"]
        for item in required:
            assert item in dockerignore, f".dockerignore missing {item!r}"

    def test_gitignore_excludes_credentials(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text()
        required = ["*.pem", ".env*", "secrets/", "shipply.toml"]
        for item in required:
            assert item in gitignore, f".gitignore missing {item!r}"

    def test_docker_compose_uses_only_expected_secret_files(
        self, compose_config_with_broker: dict[str, Any]
    ) -> None:
        """All declared secrets point to the expected files."""
        secrets = compose_config_with_broker.get("secrets", {})
        expected_files = {
            "github-app-private-key": "secrets/github-app-private-key.pem",
            "github-app-webhook-secret": "secrets/github-app-webhook-secret.txt",
            "reconcile-token": "secrets/reconcile-token.txt",
            "pacto-secret-token": "secrets/pacto-secret-token.txt",
            "omp-auth-broker-token": "secrets/omp-auth-broker-token.txt",
            "omp-provider-config.yml": "secrets/omp-provider-config.yml",
        }
        for name, expected_suffix in expected_files.items():
            secret = secrets.get(name, {})
            file_path = secret.get("file", "")
            assert file_path.endswith(expected_suffix), (
                f"secret {name!r} expected file ending with {expected_suffix!r}, got {file_path!r}"
            )
