"""Tests for the Beads (``bd``) TaskBackend and Pydantic schema models."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from shipply.backends.beads import BeadsBackend, BeadsBackendError
from shipply.backends.beads_schema import (
    Bead,
    BeadsListResponse,
    FormulaProto,
    Gate,
    Molecule,
    parse_beads_error,
    safe_bead,
    safe_beads,
    unwrap_beads_json,
)

# ---------------------------------------------------------------------------
# Fake subprocess helpers
# ---------------------------------------------------------------------------


class FakeProcess:
    """A minimal asyncio Process stand-in for ``create_subprocess_exec``."""

    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self._stdout = stdout.encode("utf-8")
        self._stderr = stderr.encode("utf-8")
        self.returncode = returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


class SubprocessMocker:
    """Queue ordered subprocess responses for BeadsBackend tests."""

    def __init__(self, monkeypatch) -> None:
        self._responses: list[tuple[str, str, int]] = []
        self._calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            "shipply.backends.beads.asyncio.create_subprocess_exec",
            self._exec,
        )

    def expect(
        self,
        stdout: str | dict[str, Any] | list[Any],
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        """Queue a subprocess response.

        Dicts and lists are JSON-encoded automatically.  Use ``expect_json=False``
        in the backend for plain-text commands like ``dolt remote list``.
        """
        if isinstance(stdout, (dict, list)):
            stdout = json.dumps(stdout)
        self._responses.append((stdout, stderr, returncode))

    async def _exec(self, *cmd: str, **kwargs: Any) -> FakeProcess:
        self._calls.append(cmd)
        if not self._responses:
            raise AssertionError(f"unexpected subprocess call: {cmd}")
        stdout, stderr, rc = self._responses.pop(0)
        return FakeProcess(stdout, stderr, rc)

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def assert_call_count(self, expected: int) -> None:
        assert self.call_count == expected


@pytest.fixture
def backend(tmp_path: Path) -> BeadsBackend:
    return BeadsBackend(cwd=str(tmp_path))


@pytest.fixture
def mocker(monkeypatch) -> SubprocessMocker:
    return SubprocessMocker(monkeypatch)


# ---------------------------------------------------------------------------
# Schema model tests
# ---------------------------------------------------------------------------


def test_unwrap_beads_json_handles_legacy_and_envelope() -> None:
    legacy = {"id": "bead-1", "title": "Legacy"}
    envelope = {"schema_version": 1, "data": legacy}

    assert unwrap_beads_json(json.dumps(legacy)) == legacy
    assert unwrap_beads_json(json.dumps(envelope)) == legacy
    assert unwrap_beads_json(legacy) == legacy
    assert unwrap_beads_json(envelope) == legacy


def test_bead_model_validates_legacy_bead() -> None:
    bead = Bead.model_validate({"id": "bead-1", "title": "Do the thing"})
    assert bead.id == "bead-1"
    assert bead.title == "Do the thing"


def test_bead_model_validates_envelope_bead() -> None:
    envelope = {"schema_version": 1, "data": {"id": "bead-2", "status": "open"}}
    data = unwrap_beads_json(envelope)
    bead = Bead.model_validate(data)
    assert bead.id == "bead-2"
    assert bead.status == "open"


def test_molecule_model_validates_envelope_molecule() -> None:
    raw = {
        "schema_version": 1,
        "data": {
            "id": "mol-1",
            "root_id": "mol-1",
            "bead_ids": ["bead-a", "bead-b"],
            "title": "Proposal molecule",
            "status": "open",
        },
    }
    data = unwrap_beads_json(raw)
    mol = Molecule.model_validate(data)
    assert mol.id == "mol-1"
    assert mol.bead_ids == ["bead-a", "bead-b"]


def test_gate_model_validates_human_gate() -> None:
    gate = Gate.model_validate(
        {"id": "gate-1", "await_type": "human", "blocks": "bead-x"}
    )
    assert gate.id == "gate-1"
    assert gate.blocks == "bead-x"


def test_formula_proto_validates_cooked_formula() -> None:
    proto = FormulaProto.model_validate(
        {
            "id": "proto-1",
            "formula": "shipply.formula.toml",
            "version": 1,
            "steps": [{"name": "build"}],
        }
    )
    assert proto.id == "proto-1"
    assert proto.formula == "shipply.formula.toml"
    assert proto.steps[0]["name"] == "build"


def test_beads_list_response_parses_envelope_array() -> None:
    raw = {
        "schema_version": 1,
        "data": [{"id": "bead-1"}, {"id": "bead-2"}],
    }
    resp = BeadsListResponse.model_validate(raw)
    assert len(resp.data) == 2
    assert resp.data[0].id == "bead-1"


def test_safe_beads_coerces_list_dict_and_none() -> None:
    assert len(safe_beads([{"id": "b1"}, {"id": "b2"}])) == 2
    assert safe_beads({"id": "b3"})[0].id == "b3"
    assert safe_beads(None) == []
    assert safe_beads("nonsense") == []


def test_safe_bead_falls_back_on_skew() -> None:
    bead = safe_bead({"id": "b4", "unknown_field": "x"})
    assert bead.id == "b4"
    assert safe_bead({"missing": "id"}).id == "unknown"
    assert safe_bead("garbage").id == "unknown"


def test_parse_beads_error_extracts_message() -> None:
    assert parse_beads_error({"error": "boom"}) == "boom"
    assert parse_beads_error({"message": "oops"}) == "oops"
    assert "foo" in parse_beads_error("foo bar")
    assert parse_beads_error(b'{"error": "bytes"}') == "bytes"


# ---------------------------------------------------------------------------
# Backend molecule creation
# ---------------------------------------------------------------------------


async def test_create_molecule_from_formula_returns_root_and_beads(
    backend: BeadsBackend, mocker: SubprocessMocker, tmp_path: Path
) -> None:
    formula = tmp_path / "shipply-prop-1.formula.toml"
    formula.write_text("# test formula")
    blueprint = {"formula_path": str(formula)}

    mocker.expect(
        {
            "schema_version": 1,
            "data": {
                "id": "proto-1",
                "formula": "shipply-prop-1",
                "version": 1,
                "steps": [],
            },
        }
    )
    mocker.expect(
        {
            "schema_version": 1,
            "data": [
                {"id": "mol-1", "title": "Molecule", "issue_type": "epic"},
                {"id": "bead-1", "title": "Step 1"},
                {"id": "bead-2", "title": "Step 2"},
            ],
        }
    )

    molecule = await backend.create_molecule(blueprint, "prop-1")

    assert molecule.id == "mol-1"
    assert molecule.root_id == "mol-1"
    assert molecule.bead_ids == ["bead-1", "bead-2"]
    assert molecule.proposal_id == "prop-1"


async def test_create_molecule_from_graph_returns_root_and_beads(
    backend: BeadsBackend, mocker: SubprocessMocker
) -> None:
    blueprint = {
        "plan_graph": {
            "nodes": ["bead-1", "bead-2"],
            "edges": [["bead-1", "bead-2"]],
        }
    }
    mocker.expect(
        {
            "schema_version": 1,
            "data": [
                {"id": "mol-2", "title": "Graph root", "issue_type": "epic"},
                {"id": "bead-1", "title": "First"},
                {"id": "bead-2", "title": "Second"},
            ],
        }
    )

    molecule = await backend.create_molecule(blueprint, "prop-2")

    assert molecule.id == "mol-2"
    assert molecule.root_id == "mol-2"
    assert molecule.bead_ids == ["bead-1", "bead-2"]


async def test_create_molecule_from_legacy_bead_specs_returns_root_and_beads(
    backend: BeadsBackend, mocker: SubprocessMocker
) -> None:
    blueprint = {
        "title": "Legacy proposal",
        "bead_specs": [
            {"id": "step-1", "title": "First step", "type": "task"},
            {
                "id": "step-2",
                "title": "Second step",
                "type": "task",
                "needs": ["step-1"],
            },
        ],
    }

    mocker.expect(
        {"schema_version": 1, "data": {"id": "root-1", "title": "Legacy proposal", "issue_type": "epic"}}
    )
    mocker.expect(
        {"schema_version": 1, "data": {"id": "step-1", "title": "First step", "issue_type": "task"}}
    )
    mocker.expect(
        {"schema_version": 1, "data": {"id": "step-2", "title": "Second step", "issue_type": "task"}}
    )
    mocker.expect({"schema_version": 1, "data": {"ok": True}})

    molecule = await backend.create_molecule(blueprint, "prop-3")

    assert molecule.id == "root-1"
    assert molecule.root_id == "root-1"
    assert molecule.bead_ids == ["step-1", "step-2"]
    mocker.assert_call_count(4)


async def test_create_molecule_rejects_blueprint_without_execution_path(
    backend: BeadsBackend, mocker: SubprocessMocker
) -> None:
    with pytest.raises(BeadsBackendError, match="no formula_path, plan_graph, or bead_specs"):
        await backend.create_molecule({}, "prop-empty")


# ---------------------------------------------------------------------------
# Bead lifecycle
# ---------------------------------------------------------------------------


async def test_claim_atomically_assigns_bead_and_second_claim_fails(
    backend: BeadsBackend, mocker: SubprocessMocker
) -> None:
    mocker.expect(
        {"schema_version": 1, "data": {"id": "bead-1", "status": "claimed", "assignee": "shipply-forge"}}
    )
    mocker.expect(
        "bead-1 is already claimed",
        returncode=1,
    )

    first = await backend.claim("bead-1")
    assert first.status == "claimed"
    assert first.assignee == "shipply-forge"

    with pytest.raises(BeadsBackendError, match="already claimed"):
        await backend.claim("bead-1")


async def test_close_unblocks_dependent_beads(
    backend: BeadsBackend, mocker: SubprocessMocker
) -> None:
    mocker.expect(
        {"schema_version": 1, "data": [{"id": "parent-bead", "title": "Parent"}]}
    )
    mocker.expect(
        {"schema_version": 1, "data": {"id": "parent-bead", "status": "closed"}}
    )
    mocker.expect(
        {
            "schema_version": 1,
            "data": [{"id": "child-bead", "title": "Child", "status": "open"}],
        }
    )

    before = await backend.get_ready("mol-3")
    assert [b.id for b in before] == ["parent-bead"]

    closed = await backend.close("parent-bead", reason="completed")
    assert closed.status == "closed"

    after = await backend.get_ready("mol-3")
    assert [b.id for b in after] == ["child-bead"]

    mocker.assert_call_count(3)


async def test_close_eligible_roots_closes_complete_roots(
    backend: BeadsBackend, mocker: SubprocessMocker
) -> None:
    mocker.expect(
        {
            "schema_version": 1,
            "data": [
                {"id": "mol-4", "status": "closed"},
                {"id": "mol-5", "status": "closed"},
            ],
        }
    )

    closed = await backend.close_eligible_roots()
    assert "mol-4" in closed
    assert "mol-5" in closed


async def test_close_eligible_roots_filters_to_requested_molecule(
    backend: BeadsBackend, mocker: SubprocessMocker
) -> None:
    mocker.expect(
        {
            "schema_version": 1,
            "data": [
                {"id": "mol-4", "status": "closed"},
                {"id": "mol-5", "status": "closed"},
            ],
        }
    )

    closed = await backend.close_eligible_roots("mol-5")
    assert closed == ["mol-5"]


async def test_get_blocked_returns_blocked_beads(
    backend: BeadsBackend, mocker: SubprocessMocker
) -> None:
    mocker.expect(
        {
            "schema_version": 1,
            "data": [
                {"id": "blocked-1", "title": "Blocked"},
            ],
        }
    )

    blocked = await backend.get_blocked("mol-6")
    assert [b.id for b in blocked] == ["blocked-1"]


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


async def test_sync_skips_push_when_no_remote_is_configured(
    backend: BeadsBackend, mocker: SubprocessMocker
) -> None:
    mocker.expect("No remotes configured")

    await backend.sync()

    mocker.assert_call_count(1)
    assert "list" in mocker._calls[0]


async def test_sync_pushes_when_remote_is_configured(
    backend: BeadsBackend, mocker: SubprocessMocker
) -> None:
    mocker.expect("origin\n")
    mocker.expect("To https://remote\n * [new branch]\n")

    await backend.sync()

    mocker.assert_call_count(2)
    assert "list" in mocker._calls[0]
    assert "push" in mocker._calls[1]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def test_run_raises_backend_error_for_nonzero_returncode(
    backend: BeadsBackend, mocker: SubprocessMocker
) -> None:
    mocker.expect("not found", returncode=1)

    with pytest.raises(BeadsBackendError, match="not found"):
        await backend.get_bead("missing")
