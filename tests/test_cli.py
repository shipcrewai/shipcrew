"""Tests for the Shipply CLI."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from shipply.cli import cli
from shipply.config import ShipplyConfig


@pytest.fixture
def runner() -> CliRunner:
    """Provide a Click test runner."""
    return CliRunner()


def test_cli_help(runner: CliRunner) -> None:
    """shipply --help runs without error and shows the CLI description."""
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Shipply orchestration CLI" in result.output


def test_status_shows_summary(
    runner: CliRunner,
    test_config: ShipplyConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shipply status prints proposals by stage, beads by status, and recent events."""
    monkeypatch.setattr("shipply.cli.load_config", lambda: test_config)

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "Proposals in flight" in result.output
    assert "GATE_1:" in result.output or "BLUEPRINT:" in result.output
    assert "Beads by status" in result.output
    assert (
        "running:" in result.output
        or "pending:" in result.output
        or "done:" in result.output
    )
    assert "Recent events" in result.output
    assert "prop-001" in result.output or "prop-003" in result.output


def test_tail_prints_event_stream(
    runner: CliRunner,
    test_config: ShipplyConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shipply tail <id> prints the event stream for a proposal."""
    monkeypatch.setattr("shipply.cli.load_config", lambda: test_config)

    result = runner.invoke(cli, ["tail", "prop-001"])
    assert result.exit_code == 0
    assert "proposal_created" in result.output
    assert "proposal_transition" in result.output
    assert "GATE_1" in result.output
    assert "INTAKE" in result.output


def test_tail_missing_proposal(
    runner: CliRunner,
    test_config: ShipplyConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shipply tail <missing> reports no events and exits cleanly."""
    monkeypatch.setattr("shipply.cli.load_config", lambda: test_config)

    result = runner.invoke(cli, ["tail", "missing-id"])
    assert result.exit_code == 0
    assert "No events for proposal missing-id" in result.output
