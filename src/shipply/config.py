"""Shipply configuration loading and validation."""

from __future__ import annotations

import os
from pathlib import Path

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


class ShipplyConfig(BaseModel):
    """Root configuration object loaded from ``shipply.toml``."""

    personas: dict[str, PersonaConfig]
    gates: dict[str, GateConfig]
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    squads: SquadsConfig = Field(default_factory=SquadsConfig)

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
