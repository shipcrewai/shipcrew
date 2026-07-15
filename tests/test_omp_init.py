"""Tests for the init-omp provisioning script and compose wiring."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
INIT_SCRIPT = REPO_ROOT / "scripts" / "init-omp.sh"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"


FAKE_OMP = '''#!/usr/bin/env python3
"""Fake OMP CLI for testing init-omp without network access."""
import json
import os
import sys
from pathlib import Path

config_dir = os.environ.get("PI_CONFIG_DIR", "")
log_path = Path(os.environ.get("FAKE_OMP_LOG", "/tmp/fake_omp.log"))

with open(log_path, "a") as fh:
    fh.write(" ".join(sys.argv) + "\\n")

if len(sys.argv) < 2:
    print("Usage: fake-omp <command>", file=sys.stderr)
    sys.exit(1)

cmd = sys.argv[1]

if cmd == "plugin":
    subcmd = sys.argv[2]
    if subcmd == "install":
        source = sys.argv[3]
        name, _, version = source.partition("@")
        if os.environ.get("FAKE_OMP_FAIL_PLUGIN_INSTALL"):
            print(f"Failed to install plugin {name}", file=sys.stderr)
            sys.exit(1)
        manifest = Path(config_dir) / "installed_plugins.json"
        data = json.loads(manifest.read_text()) if manifest.exists() else {}
        data[name] = version
        manifest.write_text(json.dumps(data, indent=2))
    elif subcmd == "list":
        manifest = Path(config_dir) / "installed_plugins.json"
        data = json.loads(manifest.read_text()) if manifest.exists() else {}
        for name, version in data.items():
            print(f"{name} {version}")
    else:
        print(f"Unknown plugin subcommand: {subcmd}", file=sys.stderr)
        sys.exit(1)
elif cmd == "skill":
    subcmd = sys.argv[2]
    if subcmd == "install":
        source = sys.argv[3]
        name, _, version = source.partition("@")
        manifest = Path(config_dir) / "installed_skills.json"
        data = json.loads(manifest.read_text()) if manifest.exists() else {}
        data[name] = version
        manifest.write_text(json.dumps(data, indent=2))
    else:
        print(f"Unknown skill subcommand: {subcmd}", file=sys.stderr)
        sys.exit(1)
elif cmd == "config":
    subcmd = sys.argv[2]
    if subcmd == "list":
        if os.environ.get("FAKE_OMP_FAIL_CONFIG_LIST"):
            print("config list verification failed", file=sys.stderr)
            sys.exit(1)
        if not (Path(config_dir) / "agent" / "config.yml").exists():
            print("Missing agent/config.yml", file=sys.stderr)
            sys.exit(1)
        if not (Path(config_dir) / "models.yml").exists():
            print("Missing models.yml", file=sys.stderr)
            sys.exit(1)
        print("config list OK")
    else:
        print(f"Unknown config subcommand: {subcmd}", file=sys.stderr)
        sys.exit(1)
elif cmd == "--version":
    print("fake-omp 0.0.1")
else:
    print(f"Unknown command: {cmd}", file=sys.stderr)
    sys.exit(1)
'''


def _write_shipply_toml(path: Path, **overrides: Any) -> None:
    """Write a minimal shipply.toml suitable for the init-omp tests."""
    plugins = overrides.get("plugins", {})
    skills = overrides.get("skills", {})
    plugins_toml = "\n".join(f'{name} = "{version}"' for name, version in plugins.items())
    skills_toml = "\n".join(f'{name} = "{version}"' for name, version in skills.items())
    text = textwrap.dedent(
        f"""
        [personas]
        scout = {{ model = "fast" }}
        doc-review = {{ model = "default" }}
        blueprint = {{ model = "slow" }}
        forge = {{ model = "default" }}
        gate-1 = {{ model = "default" }}
        gate-2 = {{ model = "default" }}
        gate-3 = {{ model = "default" }}

        [gates.gate-1]
        quorum = 3
        threshold = 0.66

        [gates.gate-2]
        quorum = 2
        threshold = 0.75

        [gates.gate-3]
        quorum = 2
        threshold = 0.75

        [omp]
        config_dir = "/etc/omp"
        coding_agent_dir = "/tmp/omp-state"
        auth_broker_url = "http://omp-auth-broker:8080"

        [omp.plugins]
        {plugins_toml}

        [omp.skills]
        {skills_toml}
        """
    )
    path.write_text(text, encoding="utf-8")


def _make_fake_omp(tmp_path: Path) -> Path:
    """Create an executable fake ``omp`` binary in a temp bin directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    omp_bin = bin_dir / "omp"
    omp_bin.write_text(FAKE_OMP, encoding="utf-8")
    omp_bin.chmod(omp_bin.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _run_init_omp(
    tmp_path: Path,
    target_dir: Path,
    shipply_toml: Path,
    fake_omp_bin: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the init-omp script in a controlled environment."""
    test_env = {
        **os.environ,
        "SHIPPLY_CONFIG": str(shipply_toml),
        "PI_CONFIG_DIR": str(target_dir),
        "PYTHON": sys.executable,
        "PATH": f"{fake_omp_bin}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    if env:
        test_env.update(env)
    return subprocess.run(
        [str(INIT_SCRIPT)],
        capture_output=True,
        text=True,
        env=test_env,
        cwd=str(tmp_path),
    )


def test_happy_path_renders_and_installs(tmp_path: Path) -> None:
    """init-omp renders YAML, installs plugins/skills, and exits 0."""
    target_dir = tmp_path / "omp"
    shipply_toml = tmp_path / "shipply.toml"
    _write_shipply_toml(
        shipply_toml,
        plugins={"test-plugin": "1.2.3"},
        skills={"test-skill": "0.1.0"},
    )
    fake_omp_bin = _make_fake_omp(tmp_path)
    fake_omp_log = tmp_path / "fake_omp.log"

    result = _run_init_omp(
        tmp_path,
        target_dir,
        shipply_toml,
        fake_omp_bin,
        env={"FAKE_OMP_LOG": str(fake_omp_log)},
    )

    assert result.returncode == 0, result.stderr

    # Rendered config and models are in the expected OMP locations.
    assert (target_dir / "agent" / "config.yml").exists()
    assert (target_dir / "models.yml").exists()

    config_data = yaml.safe_load((target_dir / "agent" / "config.yml").read_text())
    assert config_data["modelRoles"]["default"] == "openrouter-default"
    assert config_data["modelRoles"]["smol"] == "openrouter-smol"
    assert config_data["modelRoles"]["slow"] == "claude-slow"

    models_data = yaml.safe_load((target_dir / "models.yml").read_text())
    assert "models" in models_data

    # Installed plugins and skills are recorded.
    plugins_manifest = json.loads((target_dir / "installed_plugins.json").read_text())
    assert plugins_manifest == {"test-plugin": "1.2.3"}
    skills_manifest = json.loads((target_dir / "installed_skills.json").read_text())
    assert skills_manifest == {"test-skill": "0.1.0"}

    # Verification commands were invoked.
    log = fake_omp_log.read_text()
    assert "plugin install test-plugin@1.2.3" in log
    assert "skill install test-skill@0.1.0" in log
    assert "config list" in log
    assert "plugin list" in log
    assert "--version" in log


def test_verification_failure_leaves_active_dir_intact(tmp_path: Path) -> None:
    """If OMP verification fails, the active directory is not replaced."""
    target_dir = tmp_path / "omp"
    target_dir.mkdir()
    (target_dir / "existing-marker.txt").write_text("keep-me")
    shipply_toml = tmp_path / "shipply.toml"
    _write_shipply_toml(shipply_toml)
    fake_omp_bin = _make_fake_omp(tmp_path)
    fake_omp_log = tmp_path / "fake_omp.log"

    result = _run_init_omp(
        tmp_path,
        target_dir,
        shipply_toml,
        fake_omp_bin,
        env={"FAKE_OMP_LOG": str(fake_omp_log), "FAKE_OMP_FAIL_CONFIG_LIST": "1"},
    )

    assert result.returncode != 0, result.stdout
    assert "leaving" in result.stderr.lower() or "failed" in result.stderr.lower()

    # Active directory should retain its original content.
    assert (target_dir / "existing-marker.txt").read_text() == "keep-me"
    # No rendered config should have been swapped in.
    assert not (target_dir / "agent" / "config.yml").exists()


def test_atomic_swap_replaces_active_directory(tmp_path: Path) -> None:
    """A second run with changed config atomically replaces the active dir."""
    target_dir = tmp_path / "omp"
    shipply_toml = tmp_path / "shipply.toml"
    _write_shipply_toml(shipply_toml)
    fake_omp_bin = _make_fake_omp(tmp_path)
    fake_omp_log = tmp_path / "fake_omp.log"

    # First run.
    result1 = _run_init_omp(
        tmp_path,
        target_dir,
        shipply_toml,
        fake_omp_bin,
        env={"FAKE_OMP_LOG": str(fake_omp_log)},
    )
    assert result1.returncode == 0
    first_plugins = json.loads(
        (target_dir / "installed_plugins.json").read_text()
        if (target_dir / "installed_plugins.json").exists()
        else "{}"
    )

    # Modify the plugin manifest and run again.
    _write_shipply_toml(
        shipply_toml,
        plugins={"another-plugin": "2.0.0"},
    )
    fake_omp_log.unlink()

    result2 = _run_init_omp(
        tmp_path,
        target_dir,
        shipply_toml,
        fake_omp_bin,
        env={"FAKE_OMP_LOG": str(fake_omp_log)},
    )
    assert result2.returncode == 0

    # Active directory should contain the new plugin and no backup.
    second_plugins = json.loads((target_dir / "installed_plugins.json").read_text())
    assert second_plugins != first_plugins
    assert second_plugins == {"another-plugin": "2.0.0"}
    assert not (target_dir.parent / (target_dir.name + ".old")).exists()


def test_install_is_idempotent_for_pinned_versions(tmp_path: Path) -> None:
    """Re-running init-omp with unchanged plugins/skills skips re-installation."""
    target_dir = tmp_path / "omp"
    shipply_toml = tmp_path / "shipply.toml"
    _write_shipply_toml(
        shipply_toml,
        plugins={"pinned-plugin": "1.0.0"},
        skills={"pinned-skill": "1.0.0"},
    )
    fake_omp_bin = _make_fake_omp(tmp_path)
    fake_omp_log = tmp_path / "fake_omp.log"

    result1 = _run_init_omp(
        tmp_path,
        target_dir,
        shipply_toml,
        fake_omp_bin,
        env={"FAKE_OMP_LOG": str(fake_omp_log)},
    )
    assert result1.returncode == 0
    first_log = fake_omp_log.read_text()
    assert first_log.count("plugin install pinned-plugin@1.0.0") == 1
    assert first_log.count("skill install pinned-skill@1.0.0") == 1

    # Second run with the same pinned versions should skip installs.
    fake_omp_log.unlink()
    result2 = _run_init_omp(
        tmp_path,
        target_dir,
        shipply_toml,
        fake_omp_bin,
        env={"FAKE_OMP_LOG": str(fake_omp_log)},
    )
    assert result2.returncode == 0
    second_log = fake_omp_log.read_text()
    assert "plugin install" not in second_log
    assert "skill install" not in second_log
    assert "config list" in second_log
    assert "plugin list" in second_log


def test_no_secrets_in_output_or_rendered_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sensitive env-var values never appear in output or rendered YAML."""
    # Fake sensitive env vars that might be present in the host environment.
    sensitive_values = {
        "ANTHROPIC_API_KEY": "sk-ant-secret-12345",
        "OPENROUTER_API_KEY": "sk-or-secret-67890",
        "MOONSHOT_API_KEY": "sk-moonshot-secret-abcde",
    }
    for name, value in sensitive_values.items():
        monkeypatch.setenv(name, value)

    target_dir = tmp_path / "omp"
    shipply_toml = tmp_path / "shipply.toml"
    _write_shipply_toml(shipply_toml)
    fake_omp_bin = _make_fake_omp(tmp_path)
    fake_omp_log = tmp_path / "fake_omp.log"

    result = _run_init_omp(
        tmp_path,
        target_dir,
        shipply_toml,
        fake_omp_bin,
        env={"FAKE_OMP_LOG": str(fake_omp_log)},
    )
    assert result.returncode == 0

    combined_output = result.stdout + result.stderr + fake_omp_log.read_text()
    for value in sensitive_values.values():
        assert value not in combined_output

    rendered = (
        (target_dir / "agent" / "config.yml").read_text()
        + (target_dir / "models.yml").read_text()
    )
    for value in sensitive_values.values():
        assert value not in rendered
    for name in sensitive_values:
        assert f"${name}" in rendered


def test_compose_declares_init_omp_service_and_volume() -> None:
    """docker-compose.yml contains the init-omp service and omp-config volume."""
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    services = compose.get("services", {})
    volumes = compose.get("volumes", {})

    assert "init-omp" in services
    init_omp = services["init-omp"]
    assert init_omp["entrypoint"] == ["/app/scripts/init-omp.sh"]
    assert any("/etc/omp" in str(v) for v in init_omp.get("volumes", []))
    assert any("/etc/shipply/shipply.toml" in str(v) for v in init_omp.get("volumes", []))

    assert "omp-config" in volumes


HANDLERS = [
    "scout",
    "doc-review",
    "blueprint",
    "forge",
    "gate1",
    "gate2",
    "gate3",
]


@pytest.mark.parametrize("service", HANDLERS)
def test_compose_handlers_depend_on_init_omp(service: str) -> None:
    """Each handler waits for init-omp to complete successfully."""
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    svc = compose["services"][service]
    depends_on = svc.get("depends_on", {})
    assert "init-omp" in depends_on
    assert depends_on["init-omp"]["condition"] == "service_completed_successfully"


@pytest.mark.parametrize("service", HANDLERS)
def test_compose_handlers_mount_read_only_omp_config(service: str) -> None:
    """Each handler mounts omp-config read-only at /etc/omp."""
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    svc = compose["services"][service]
    assert any(v == "omp-config:/etc/omp:ro" for v in svc.get("volumes", []))


@pytest.mark.parametrize("service", HANDLERS)
def test_compose_handlers_set_omp_env_vars(service: str) -> None:
    """Each handler sets PI_CONFIG_DIR, PI_CODING_AGENT_DIR, and broker env vars."""
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    svc = compose["services"][service]
    env = svc.get("environment", {})
    assert env.get("PI_CONFIG_DIR") == "/etc/omp"
    assert env.get("OMP_AUTH_BROKER_URL") is not None
    assert env.get("OMP_AUTH_BROKER_TOKEN") is not None
    expected_coding_dir = f"/tmp/omp-state/{service}"
    assert env.get("PI_CODING_AGENT_DIR") == expected_coding_dir


def test_compose_config_validates() -> None:
    """``docker compose config`` exits 0 with the current file."""
    result = subprocess.run(
        ["docker", "compose", "config"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
