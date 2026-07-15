"""Shipply configuration loading and validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover (python < 3.11)
    import tomli as tomllib


class PersonaConfig(BaseModel):
    """Model routing and harness invocation for a single bot persona."""

    model: str
    binary: str = "omp"
    args: list[str] = Field(default_factory=lambda: ["acp"])


class SquadsConfig(BaseModel):
    """MLS Squad group IDs for governance gates."""

    model_config = ConfigDict(populate_by_name=True)

    gate_1: str | None = Field(default=None, alias="gate-1")
    gate_2: str | None = Field(default=None, alias="gate-2")
    gate_3: str | None = Field(default=None, alias="gate-3")

    def group_id(self, gate_type: str) -> str | None:
        """Return the configured MLS group id for a gate type such as ``gate-1``."""
        normalized = gate_type.lower().replace("-", "_")
        return getattr(self, normalized, None)


class GateConfig(BaseModel):
    """Governance gate thresholds."""

    quorum: int = Field(default=1, ge=1)
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)


class DatabaseConfig(BaseModel):
    """SQLite persistence settings."""

    path: str = "data/proposals.db"


class ObservabilityConfig(BaseModel):
    """Logging and metrics settings."""

    log_level: str = "INFO"
    metrics_port: int = Field(default=9090, ge=1, le=65535)


class OMPConfig(BaseModel):
    """Shared OMP environment provisioning and runtime settings."""

    config_dir: str = "/etc/omp"
    coding_agent_dir: str = "/tmp/omp-state"
    auth_broker_url: str | None = None
    model_roles: dict[str, str] = Field(
        default_factory=lambda: {
            "default": "openrouter-default",
            "smol": "openrouter-smol",
            "slow": "claude-slow",
        }
    )
    provider_config: dict[str, Any] = Field(default_factory=dict)
    plugins: dict[str, str] = Field(default_factory=dict)
    skills: dict[str, str] = Field(default_factory=dict)

    @field_validator("model_roles")
    @classmethod
    def _require_model_roles(cls, value: dict[str, str]) -> dict[str, str]:
        required = {"default", "smol", "slow"}
        missing = required - set(value.keys())
        if missing:
            raise ValueError(f"missing required OMP model roles: {', '.join(sorted(missing))}")
        return value


class GitHubConfig(BaseModel):
    """GitHub App installation and repository scoping settings."""

    app_id: str | None = None
    private_key_path: str | None = None
    source_org: str | None = None
    workspace_org: str | None = None
    source_installation_id: str | None = None
    workspace_installation_id: str | None = None
    repo_scope: list[str] = Field(default_factory=list)
    webhook_secret: str | None = None
    webhook_dedup_ttl_days: int = Field(default=7, ge=1)


class ShipplyConfig(BaseModel):
    """Root configuration object loaded from ``shipply.toml``."""

    personas: dict[str, PersonaConfig]
    gates: dict[str, GateConfig]
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    squads: SquadsConfig = Field(default_factory=SquadsConfig)
    omp: OMPConfig = Field(default_factory=OMPConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)

    @field_validator("personas")
    @classmethod
    def _require_personas(cls, value: dict[str, PersonaConfig]) -> dict[str, PersonaConfig]:
        required = {
            "scout",
            "doc-review",
            "blueprint",
            "forge",
            "gate-1",
            "gate-2",
            "gate-3",
        }
        missing = required - set(value.keys())
        if missing:
            raise ValueError(f"missing required personas: {', '.join(sorted(missing))}")
        return value

    @field_validator("gates")
    @classmethod
    def _require_gates(cls, value: dict[str, GateConfig]) -> dict[str, GateConfig]:
        required = {"gate-1", "gate-2", "gate-3"}
        missing = required - set(value.keys())
        if missing:
            raise ValueError(f"missing required gates: {', '.join(sorted(missing))}")
        return value


def load_config(path: str | Path | None = None) -> ShipplyConfig:
    """Load and validate ``shipply.toml``.

    If ``path`` is not provided, the ``SHIPPLY_CONFIG`` environment variable is
    consulted, defaulting to ``shipply.toml`` in the current working directory.
    """
    if path is None:
        path = os.environ.get("SHIPPLY_CONFIG", "shipply.toml")
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"shipply config not found: {path}")
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return ShipplyConfig(**data)


# ---------------------------------------------------------------------------
# OMP model alias mapping and YAML rendering helpers
# ---------------------------------------------------------------------------


ALIAS_TO_ROLE: dict[str, str] = {
    "fast": "smol",
    "default": "default",
    "slow": "slow",
}


def model_alias_to_role(alias: str) -> str:
    """Map a ``shipply.toml`` model alias to an OMP model role.

    The mapping is hard-coded per the shared-omp-config plan:
    ``fast`` → ``smol``, ``default`` → ``default``, ``slow`` → ``slow``.
    Unknown aliases are returned unchanged so callers can decide how to handle
    them.
    """
    return ALIAS_TO_ROLE.get(alias, alias)


def render_omp_model_roles(config: ShipplyConfig) -> dict[str, str]:
    """Return the ``modelRoles`` mapping for OMP ``config.yml``.

    The keys are OMP role names (``default``, ``smol``, ``slow``) and the
    values are the provider model IDs declared in ``models.yml``.
    """
    return dict(config.omp.model_roles)


def _default_models_yml() -> dict[str, Any]:
    """Build the default ``models.yml`` content with env-var references only.

    Covers the initial provider set: Ollama / Ollama Cloud, OpenRouter,
    Kimi Coding, and Claude. API keys and base URLs are referenced by env-var
    name so the auth broker can resolve them at runtime.
    """
    return {
        "models": {
            "claude-slow": {
                "provider": "claude",
                "model": "claude-opus-4",
                "api_key": "$ANTHROPIC_API_KEY",
            },
            "openrouter-default": {
                "provider": "openrouter",
                "model": "openrouter/auto",
                "api_key": "$OPENROUTER_API_KEY",
            },
            "openrouter-smol": {
                "provider": "openrouter",
                "model": "openrouter/smol",
                "api_key": "$OPENROUTER_API_KEY",
            },
            "kimi-coding": {
                "provider": "kimi",
                "model": "kimi-k2-coding",
                "api_key": "$MOONSHOT_API_KEY",
            },
            "ollama-local": {
                "provider": "ollama",
                "model": "llama3.1",
                "api_key": "$OLLAMA_API_KEY",
                "base_url": "$OLLAMA_BASE_URL",
            },
            "ollama-cloud": {
                "provider": "ollama_cloud",
                "model": "llama3.1",
                "api_key": "$OLLAMA_CLOUD_API_KEY",
                "base_url": "$OLLAMA_CLOUD_BASE_URL",
            },
        }
    }


def render_omp_models_yml(config: ShipplyConfig) -> dict[str, Any]:
    """Return the ``models.yml`` content for the configured OMP environment.

    Provider API keys are always referenced by env-var name; actual values are
    never written to the returned dictionary. ``config.omp.provider_config``
    can add additional provider-specific entries or override defaults.
    """
    data = _default_models_yml()
    if config.omp.provider_config:
        data["models"].update(config.omp.provider_config)
    return data


def render_omp_config_files(config: ShipplyConfig, dest_dir: str | Path) -> None:
    """Render OMP ``config.yml`` and ``models.yml`` into ``dest_dir``.

    ``dest_dir`` is created if it does not exist. Files are written to the
    locations expected by the OMP CLI when ``PI_CONFIG_DIR`` points to
    ``dest_dir``:

    - ``agent/config.yml`` contains the ``modelRoles`` mapping.
    - ``models.yml`` contains provider declarations.

    The rendered YAML files contain only non-sensitive configuration; provider
    credentials are referenced by env-var name.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    agent_dir = dest / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    config_yml = {"modelRoles": render_omp_model_roles(config)}
    models_yml = render_omp_models_yml(config)

    with open(agent_dir / "config.yml", "w", encoding="utf-8") as fh:
        yaml.safe_dump(config_yml, fh, sort_keys=False, default_flow_style=False)
    with open(dest / "models.yml", "w", encoding="utf-8") as fh:
        yaml.safe_dump(models_yml, fh, sort_keys=False, default_flow_style=False)
