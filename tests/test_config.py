"""Tests for shipply configuration loading and validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from shipply.config import ShipplyConfig, load_config


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
