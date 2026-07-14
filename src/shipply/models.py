"""Pydantic models for the Shipply proposal lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProposalState(str, Enum):
    """Ordered lifecycle stages for a proposal."""

    INTAKE = "INTAKE"
    DOC_REVIEW = "DOC_REVIEW"
    GATE_1 = "GATE_1"
    BLUEPRINT = "BLUEPRINT"
    GATE_2 = "GATE_2"
    FORGE = "FORGE"
    GATE_3 = "GATE_3"
    CLOSED = "CLOSED"


_VALID_TRANSITIONS: dict[ProposalState, set[ProposalState]] = {
    ProposalState.INTAKE: {ProposalState.DOC_REVIEW},
    ProposalState.DOC_REVIEW: {ProposalState.INTAKE, ProposalState.GATE_1},
    ProposalState.GATE_1: {ProposalState.INTAKE, ProposalState.BLUEPRINT},
    ProposalState.BLUEPRINT: {ProposalState.GATE_2},
    ProposalState.GATE_2: {ProposalState.BLUEPRINT, ProposalState.FORGE},
    ProposalState.FORGE: {ProposalState.GATE_3},
    ProposalState.GATE_3: {ProposalState.FORGE, ProposalState.CLOSED},
    ProposalState.CLOSED: set(),
}


def valid_transition(from_state: ProposalState | str, to_state: ProposalState | str) -> None:
    """Raise ``ValueError`` if ``from_state`` may not transition to ``to_state``.

    Self-transitions are not considered valid because the lifecycle stages are
    states, not activities; discussion/review activity within a stage is emitted
    as events without changing proposal state.
    """
    source = from_state if isinstance(from_state, ProposalState) else ProposalState(from_state)
    target = to_state if isinstance(to_state, ProposalState) else ProposalState(to_state)
    if target not in _VALID_TRANSITIONS[source]:
        raise ValueError(f"invalid proposal transition: {source.value} -> {target.value}")


class Proposal(BaseModel):
    """Top-level product proposal."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    state: ProposalState = ProposalState.INTAKE
    title: str
    sponsor: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Revision(BaseModel):
    """Frozen artifact bundle at a gate boundary."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    proposal_id: str
    rev_num: int = Field(default=1, ge=1)
    requirements_doc: str | None = None
    blueprint_doc: str | None = None
    frozen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BeadRef(BaseModel):
    """Reference to a bead executing within a Dolt molecule."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    proposal_id: str
    bead_id: str
    status: str = "pending"
    molecule_id: str | None = None


class GateState(BaseModel):
    """Current voting/authorization state for a governance gate."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    proposal_id: str
    gate_type: str
    status: str = "open"
    votes: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
