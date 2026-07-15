"""Tests for shipply configuration loading and validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from shipply.config import (
    GateConfig,
    GitHubConfig,
    OMPConfig,
    PersonaConfig,
    ShipplyConfig,
    load_config,
    model_alias_to_role,
    render_omp_config_files,
    render_omp_model_roles,
    render_omp_models_yml,
)


EXAMPLE_CONFIG = Path(__file__).parent.parent / "shipply.toml.example"


def test_load_config_with_example_file(tmp_path: Path) -> None:
    """load_config() succeeds with shipply.toml.example."""
    config = load_config(EXAMPLE_CONFIG)
    assert isinstance(config, ShipplyConfig)
    assert "scout" in config.personas
    assert config.personas["scout"].model == "fast"
    assert "gate-1" in config.gates
    assert config.gates["gate-1"].quorum == 3
    assert config.gates["gate-1"].threshold == pytest.approx(0.66)


def test_load_config_default_path_uses_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """load_config() honors SHIPPLY_CONFIG when no path is provided."""
    config_file = tmp_path / "shipply.toml"
    config_file.write_text(EXAMPLE_CONFIG.read_text())
    monkeypatch.setenv("SHIPPLY_CONFIG", str(config_file))
    config = load_config()
    assert isinstance(config, ShipplyConfig)


def test_load_config_missing_file(tmp_path: Path) -> None:
    """load_config() raises FileNotFoundError for a missing file."""
    missing = tmp_path / "does-not-exist.toml"
    with pytest.raises(FileNotFoundError):
        load_config(missing)


def test_load_config_missing_personas(tmp_path: Path) -> None:
    """load_config() raises ValueError when required personas are missing."""
    config = tmp_path / "no-personas.toml"
    config.write_text(
        textwrap.dedent(
            """
            [personas]

            [gates.gate-1]
            quorum = 3
            threshold = 0.66

            [gates.gate-2]
            quorum = 2
            threshold = 0.75

            [gates.gate-3]
            quorum = 2
            threshold = 0.75
            """
        )
    )
    with pytest.raises(ValueError, match="missing required personas"):
        load_config(config)


def test_load_config_missing_gates(tmp_path: Path) -> None:
    """load_config() raises ValueError when required gates are missing."""
    config = tmp_path / "no-gates.toml"
    config.write_text(
        textwrap.dedent(
            """
            [personas]
            scout = { model = "fast" }
            doc-review = { model = "default" }
            blueprint = { model = "slow" }
            forge = { model = "default" }
            gate-1 = { model = "default" }
            gate-2 = { model = "default" }
            gate-3 = { model = "default" }

            [gates]
            """
        )
    )
    with pytest.raises(ValueError, match="missing required gates"):
        load_config(config)


def test_load_config_rejects_invalid_gate_threshold(tmp_path: Path) -> None:
    """Gate thresholds must be between 0 and 1."""
    config = tmp_path / "bad-threshold.toml"
    config.write_text(
        textwrap.dedent(
            """
            [personas.scout]
            model = "fast"

            [personas.doc-review]
            model = "default"

            [personas.blueprint]
            model = "slow"

            [personas.forge]
            model = "default"

            [personas.gate-1]
            model = "default"

            [personas.gate-2]
            model = "default"

            [personas.gate-3]
            model = "default"

            [gates.gate-1]
            quorum = 3
            threshold = 1.5

            [gates.gate-2]
            quorum = 2
            threshold = 0.75

            [gates.gate-3]
            quorum = 2
            threshold = 0.75
            """
        )
    )
    with pytest.raises(ValueError):
        load_config(config)


def test_load_config_rejects_invalid_gate_quorum(tmp_path: Path) -> None:
    """Gate quorum must be at least 1."""
    config = tmp_path / "bad-quorum.toml"
    config.write_text(
        textwrap.dedent(
            """
            [personas.scout]
            model = "fast"

            [personas.doc-review]
            model = "default"

            [personas.blueprint]
            model = "slow"

            [personas.forge]
            model = "default"

            [personas.gate-1]
            model = "default"

            [personas.gate-2]
            model = "default"

            [personas.gate-3]
            model = "default"

            [gates.gate-1]
            quorum = 0
            threshold = 0.66

            [gates.gate-2]
            quorum = 2
            threshold = 0.75

            [gates.gate-3]
            quorum = 2
            threshold = 0.75
            """
        )
    )
    with pytest.raises(ValueError):
        load_config(config)


def test_load_config_default_database_and_observability(tmp_path: Path) -> None:
    """Defaults are applied when database/observability sections are absent."""
    config = tmp_path / "minimal.toml"
    config.write_text(
        textwrap.dedent(
            """
            [personas.scout]
            model = "fast"

            [personas.doc-review]
            model = "default"

            [personas.blueprint]
            model = "slow"

            [personas.forge]
            model = "default"

            [personas.gate-1]
            model = "default"

            [personas.gate-2]
            model = "default"

            [personas.gate-3]
            model = "default"

            [gates.gate-1]
            quorum = 3
            threshold = 0.66

            [gates.gate-2]
            quorum = 2
            threshold = 0.75

            [gates.gate-3]
            quorum = 2
            threshold = 0.75
            """
        )
    )
    cfg = load_config(config)
    assert cfg.database.path == "data/proposals.db"
    assert cfg.observability.log_level == "INFO"
    assert cfg.observability.metrics_port == 9090


# ---------------------------------------------------------------------------
# OMP config schema and model role mapping tests
# ---------------------------------------------------------------------------


def test_model_alias_to_role_mapping() -> None:
    """Persona aliases map to the expected OMP model roles."""
    assert model_alias_to_role("fast") == "smol"
    assert model_alias_to_role("default") == "default"
    assert model_alias_to_role("slow") == "slow"
    assert model_alias_to_role("unknown") == "unknown"


def test_default_omp_model_roles() -> None:
    """Default OMP model roles cover the required role names."""
    cfg = ShipplyConfig(
        personas={
            "scout": PersonaConfig(model="fast"),
            "doc-review": PersonaConfig(model="default"),
            "blueprint": PersonaConfig(model="slow"),
            "forge": PersonaConfig(model="default"),
            "gate-1": PersonaConfig(model="default"),
            "gate-2": PersonaConfig(model="default"),
            "gate-3": PersonaConfig(model="default"),
        },
        gates={
            "gate-1": GateConfig(quorum=3, threshold=0.66),
            "gate-2": GateConfig(quorum=2, threshold=0.75),
            "gate-3": GateConfig(quorum=2, threshold=0.75),
        },
    )
    roles = render_omp_model_roles(cfg)
    assert "default" in roles
    assert "smol" in roles
    assert "slow" in roles


def test_load_config_with_omp_and_github_sections(tmp_path: Path) -> None:
    """[omp] and [github] sections are parsed and validated."""
    config = tmp_path / "full.toml"
    config.write_text(
        textwrap.dedent(
            """
            [personas.scout]
            model = "fast"

            [personas.doc-review]
            model = "default"

            [personas.blueprint]
            model = "slow"

            [personas.forge]
            model = "default"

            [personas.gate-1]
            model = "default"

            [personas.gate-2]
            model = "default"

            [personas.gate-3]
            model = "default"

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

            [omp.model_roles]
            default = "openrouter-default"
            smol = "openrouter-smol"
            slow = "claude-slow"

            [omp.plugins]
            shipply-git = "1.0.0"

            [omp.skills]
            shipply-forge = "1.0.0"

            [github]
            app_id = "123456"
            source_org = "my-org"
            workspace_org = "my-org-workspace"
            source_installation_id = "987654321"
            workspace_installation_id = "987654322"
            repo_scope = ["my-org/repo-a"]
            webhook_secret = ""
            """
        )
    )
    cfg = load_config(config)
    assert isinstance(cfg.omp, OMPConfig)
    assert cfg.omp.config_dir == "/etc/omp"
    assert cfg.omp.coding_agent_dir == "/tmp/omp-state"
    assert cfg.omp.auth_broker_url == "http://omp-auth-broker:8080"
    assert cfg.omp.plugins == {"shipply-git": "1.0.0"}
    assert cfg.omp.skills == {"shipply-forge": "1.0.0"}
    assert isinstance(cfg.github, GitHubConfig)
    assert cfg.github.app_id == "123456"
    assert cfg.github.source_org == "my-org"
    assert cfg.github.workspace_org == "my-org-workspace"
    assert cfg.github.source_installation_id == "987654321"
    assert cfg.github.workspace_installation_id == "987654322"
    assert cfg.github.repo_scope == ["my-org/repo-a"]
    assert cfg.github.webhook_secret == ""


def test_render_omp_config_files_writes_expected_yaml(tmp_path: Path) -> None:
    """render_omp_config_files emits config.yml and models.yml."""
    cfg = ShipplyConfig(
        personas={
            "scout": PersonaConfig(model="fast"),
            "doc-review": PersonaConfig(model="default"),
            "blueprint": PersonaConfig(model="slow"),
            "forge": PersonaConfig(model="default"),
            "gate-1": PersonaConfig(model="default"),
            "gate-2": PersonaConfig(model="default"),
            "gate-3": PersonaConfig(model="default"),
        },
        gates={
            "gate-1": GateConfig(quorum=3, threshold=0.66),
            "gate-2": GateConfig(quorum=2, threshold=0.75),
            "gate-3": GateConfig(quorum=2, threshold=0.75),
        },
    )
    dest = tmp_path / "omp"
    render_omp_config_files(cfg, dest)

    assert (dest / "config.yml").exists()
    assert (dest / "models.yml").exists()

    config_data = yaml.safe_load((dest / "config.yml").read_text())
    models_data = yaml.safe_load((dest / "models.yml").read_text())

    assert config_data["modelRoles"]["default"] == "openrouter-default"
    assert config_data["modelRoles"]["smol"] == "openrouter-smol"
    assert config_data["modelRoles"]["slow"] == "claude-slow"
    assert "models" in models_data


def test_rendered_yaml_contains_no_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Rendered OMP YAML references env vars but never embeds real values."""
    # Pretend sensitive env vars are set to fake values.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moonshot-secret")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-secret")
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "ollama-cloud-secret")

    cfg = ShipplyConfig(
        personas={
            "scout": PersonaConfig(model="fast"),
            "doc-review": PersonaConfig(model="default"),
            "blueprint": PersonaConfig(model="slow"),
            "forge": PersonaConfig(model="default"),
            "gate-1": PersonaConfig(model="default"),
            "gate-2": PersonaConfig(model="default"),
            "gate-3": PersonaConfig(model="default"),
        },
        gates={
            "gate-1": GateConfig(quorum=3, threshold=0.66),
            "gate-2": GateConfig(quorum=2, threshold=0.75),
            "gate-3": GateConfig(quorum=2, threshold=0.75),
        },
    )
    dest = tmp_path / "omp"
    render_omp_config_files(cfg, dest)

    rendered = (dest / "config.yml").read_text() + (dest / "models.yml").read_text()
    assert "sk-ant-secret" not in rendered
    assert "sk-or-secret" not in rendered
    assert "sk-moonshot-secret" not in rendered
    assert "ollama-secret" not in rendered
    assert "ollama-cloud-secret" not in rendered
    # Env-var references are present so the auth broker can resolve them.
    assert "$ANTHROPIC_API_KEY" in rendered
    assert "$OPENROUTER_API_KEY" in rendered
    assert "$MOONSHOT_API_KEY" in rendered
    assert "$OLLAMA_API_KEY" in rendered
    assert "$OLLAMA_CLOUD_API_KEY" in rendered


def test_rendered_models_yml_covers_all_provider_categories(tmp_path: Path) -> None:
    """models.yml includes the four required provider categories."""
    cfg = ShipplyConfig(
        personas={
            "scout": PersonaConfig(model="fast"),
            "doc-review": PersonaConfig(model="default"),
            "blueprint": PersonaConfig(model="slow"),
            "forge": PersonaConfig(model="default"),
            "gate-1": PersonaConfig(model="default"),
            "gate-2": PersonaConfig(model="default"),
            "gate-3": PersonaConfig(model="default"),
        },
        gates={
            "gate-1": GateConfig(quorum=3, threshold=0.66),
            "gate-2": GateConfig(quorum=2, threshold=0.75),
            "gate-3": GateConfig(quorum=2, threshold=0.75),
        },
    )
    models = render_omp_models_yml(cfg)
    provider_models = list(models["models"].values())
    providers = {m["provider"] for m in provider_models}

    assert "claude" in providers
    assert "openrouter" in providers
    assert "kimi" in providers
    assert "ollama" in providers
    assert "ollama_cloud" in providers


def test_load_config_example_with_new_sections() -> None:
    """shipply.toml.example can be loaded with the new sections."""
    cfg = load_config(EXAMPLE_CONFIG)
    assert isinstance(cfg.omp, OMPConfig)
    assert isinstance(cfg.github, GitHubConfig)
    assert cfg.omp.config_dir == "/etc/omp"
