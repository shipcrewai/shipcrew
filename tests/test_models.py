"""Tests for shipply Pydantic models and lifecycle validation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from shipply.models import (
    BeadRef,
    GateState,
    Proposal,
    ProposalState,
    Revision,
    valid_transition,
)


EXPECTED_STATES = [
    ProposalState.INTAKE,
    ProposalState.DOC_REVIEW,
    ProposalState.GATE_1,
    ProposalState.BLUEPRINT,
    ProposalState.GATE_2,
    ProposalState.FORGE,
    ProposalState.GATE_3,
    ProposalState.CLOSED,
]


def test_proposal_state_values_and_order() -> None:
    """ProposalState exposes the expected ordered values."""
    assert list(ProposalState) == EXPECTED_STATES
    assert ProposalState.INTAKE.value == "INTAKE"
    assert ProposalState.DOC_REVIEW.value == "DOC_REVIEW"
    assert ProposalState.GATE_1.value == "GATE_1"
    assert ProposalState.BLUEPRINT.value == "BLUEPRINT"
    assert ProposalState.GATE_2.value == "GATE_2"
    assert ProposalState.FORGE.value == "FORGE"
    assert ProposalState.GATE_3.value == "GATE_3"
    assert ProposalState.CLOSED.value == "CLOSED"


VALID_TRANSITIONS: list[tuple[str, str]] = [
    ("INTAKE", "DOC_REVIEW"),
    ("DOC_REVIEW", "INTAKE"),
    ("DOC_REVIEW", "GATE_1"),
    ("GATE_1", "INTAKE"),
    ("GATE_1", "BLUEPRINT"),
    ("BLUEPRINT", "GATE_2"),
    ("GATE_2", "BLUEPRINT"),
    ("GATE_2", "FORGE"),
    ("FORGE", "GATE_3"),
    ("GATE_3", "FORGE"),
    ("GATE_3", "CLOSED"),
]


@pytest.mark.parametrize("from_state,to_state", VALID_TRANSITIONS)
def test_valid_transition_accepts_lifecycle_steps(from_state: str, to_state: str) -> None:
    """valid_transition() accepts every allowed lifecycle step."""
    valid_transition(from_state, to_state)


@pytest.mark.parametrize("from_state,to_state", VALID_TRANSITIONS)
def test_valid_transition_accepts_enum_values(from_state: str, to_state: str) -> None:
    """valid_transition() accepts ProposalState enum values."""
    valid_transition(ProposalState(from_state), ProposalState(to_state))


INVALID_TRANSITIONS: list[tuple[str, str]] = [
    # self-transitions are not allowed
    ("INTAKE", "INTAKE"),
    ("DOC_REVIEW", "DOC_REVIEW"),
    ("GATE_1", "GATE_1"),
    ("BLUEPRINT", "BLUEPRINT"),
    ("FORGE", "FORGE"),
    ("GATE_3", "GATE_3"),
    ("CLOSED", "CLOSED"),
    # skipping stages is not allowed
    ("INTAKE", "GATE_1"),
    ("INTAKE", "BLUEPRINT"),
    ("DOC_REVIEW", "BLUEPRINT"),
    ("GATE_1", "GATE_2"),
    ("GATE_1", "FORGE"),
    ("BLUEPRINT", "FORGE"),
    ("BLUEPRINT", "GATE_3"),
    ("FORGE", "CLOSED"),
    # reverse progression is not allowed (unless explicitly defined)
    ("CLOSED", "GATE_3"),
    ("CLOSED", "FORGE"),
    ("GATE_3", "INTAKE"),
    ("FORGE", "GATE_2"),
    ("FORGE", "BLUEPRINT"),
    ("GATE_2", "GATE_1"),
    ("GATE_2", "INTAKE"),
    ("BLUEPRINT", "GATE_1"),
    ("BLUEPRINT", "INTAKE"),
]


@pytest.mark.parametrize("from_state,to_state", INVALID_TRANSITIONS)
def test_valid_transition_rejects_invalid_steps(from_state: str, to_state: str) -> None:
    """valid_transition() rejects invalid transitions."""
    with pytest.raises(ValueError, match="invalid proposal transition"):
        valid_transition(from_state, to_state)


def test_valid_transition_from_closed_is_terminal() -> None:
    """Once CLOSED, no further transitions are possible."""
    for target in ProposalState:
        if target is ProposalState.CLOSED:
            continue
        with pytest.raises(ValueError):
            valid_transition(ProposalState.CLOSED, target)


def test_proposal_round_trip_json() -> None:
    """Proposal round-trips through JSON."""
    now = datetime.now(timezone.utc)
    proposal = Proposal(
        id="p-1",
        state=ProposalState.INTAKE,
        title="Test Proposal",
        sponsor="alice",
        created_at=now,
        updated_at=now,
    )
    serialized = proposal.model_dump_json()
    deserialized = Proposal.model_validate_json(serialized)
    assert deserialized.id == proposal.id
    assert deserialized.state == proposal.state
    assert deserialized.title == proposal.title
    assert deserialized.sponsor == proposal.sponsor
    assert deserialized.created_at == proposal.created_at
    assert deserialized.updated_at == proposal.updated_at


def test_proposal_rejects_missing_required_fields() -> None:
    """Proposal requires id, title, and sponsor."""
    with pytest.raises(ValidationError):
        Proposal(state=ProposalState.INTAKE)  # type: ignore[call-arg]


def test_revision_round_trip_json() -> None:
    """Revision round-trips through JSON."""
    revision = Revision(
        id="r-1",
        proposal_id="p-1",
        rev_num=2,
        requirements_doc="req.md",
        blueprint_doc="bp.md",
    )
    serialized = revision.model_dump_json()
    deserialized = Revision.model_validate_json(serialized)
    assert deserialized.id == revision.id
    assert deserialized.proposal_id == revision.proposal_id
    assert deserialized.rev_num == revision.rev_num
    assert deserialized.requirements_doc == revision.requirements_doc
    assert deserialized.blueprint_doc == revision.blueprint_doc


def test_revision_rejects_negative_rev_num() -> None:
    """Revision rev_num must be >= 1."""
    with pytest.raises(ValidationError):
        Revision(id="r-1", proposal_id="p-1", rev_num=0)


def test_bead_ref_round_trip_json() -> None:
    """BeadRef round-trips through JSON."""
    bead = BeadRef(id="b-1", proposal_id="p-1", bead_id="bd-1", status="running", molecule_id="m-1")
    serialized = bead.model_dump_json()
    deserialized = BeadRef.model_validate_json(serialized)
    assert deserialized.id == bead.id
    assert deserialized.proposal_id == bead.proposal_id
    assert deserialized.bead_id == bead.bead_id
    assert deserialized.status == bead.status
    assert deserialized.molecule_id == bead.molecule_id


def test_gate_state_round_trip_json() -> None:
    """GateState round-trips through JSON."""
    gate = GateState(
        id="g-1",
        proposal_id="p-1",
        gate_type="gate-1",
        status="open",
        votes={"alice": "approve"},
        metadata={"threshold": 0.66},
    )
    serialized = gate.model_dump_json()
    deserialized = GateState.model_validate_json(serialized)
    assert deserialized.id == gate.id
    assert deserialized.proposal_id == gate.proposal_id
    assert deserialized.gate_type == gate.gate_type
    assert deserialized.status == gate.status
    assert deserialized.votes == gate.votes
    assert deserialized.metadata == gate.metadata
