"""Pydantic models for Beads (``bd``) CLI JSON output.

The models support both legacy raw JSON output and the stable v2 envelope
shape produced when ``BD_JSON_ENVELOPE=1`` is set.  See the U6 plan section
for the verified Beads JSON shapes.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Bead(BaseModel):
    """A single Beads issue / bead.

    Covers the object shapes returned by ``bd create``, ``bd show``,
    ``bd update``, ``bd close``, ``bd list``, and ``bd ready``.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: int | None = None
    issue_type: str | None = Field(default=None, alias="issue_type")
    assignee: str | None = None
    owner: str | None = None
    created_at: str | None = None
    created_by: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    closed_at: str | None = None
    close_reason: str | None = None
    labels: list[str] | None = None
    dependent_count: int | None = None
    dependency_count: int | None = None
    comment_count: int | None = None
    molecule_id: str | None = None
    parent: str | None = None
    external_ref: str | None = None
    url: str | None = None
    pr_url: str | None = None
    children: list["Bead"] | None = None


class Molecule(BaseModel):
    """A poured Beads molecule: a root epic and its child beads."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    root_id: str | None = None
    proposal_id: str | None = None
    bead_ids: list[str] = Field(default_factory=list)
    beads: list[Bead] = Field(default_factory=list)
    title: str | None = None
    status: str | None = None
    issue_type: str | None = None


class Gate(BaseModel):
    """A Beads coordination gate (typically a human gate)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    await_type: str | None = None
    description: str | None = None
    status: str | None = None
    issue_type: str | None = None
    priority: int | None = None
    created_at: str | None = None
    created_by: str | None = None
    updated_at: str | None = None
    title: str | None = None
    blocks: str | None = None


class FormulaProto(BaseModel):
    """Resolved Beads formula / proto produced by ``bd cook``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str | None = None
    formula: str | None = None
    description: str | None = None
    version: int | None = None
    type: str | None = None
    steps: list[Any] = Field(default_factory=list)
    vars: dict[str, Any] = Field(default_factory=dict)
    phase: str | None = None


class BeadsListResponse(BaseModel):
    """Envelope for list/ready responses that may carry a top-level array."""

    model_config = ConfigDict(extra="allow")

    data: list[Bead] = Field(default_factory=list)
    schema_version: int | None = None


def unwrap_beads_json(raw: str | bytes | Any) -> Any:
    """Parse Beads JSON and unwrap the v2 envelope if present.

    Legacy mode returns the payload directly.  Envelope mode wraps it as
    ``{"schema_version": 1, "data": <payload>}``.
    """
    if isinstance(raw, (bytes, str)):
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        data = json.loads(text)
    else:
        data = raw

    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def parse_beads_error(raw: str | bytes | Any) -> str:
    """Best-effort extraction of an error message from Beads output."""
    try:
        data = unwrap_beads_json(raw)
    except (json.JSONDecodeError, ValueError):
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        return text.strip()

    if isinstance(data, dict):
        if "error" in data:
            return str(data["error"])
        if "message" in data:
            return str(data["message"])
    return json.dumps(data, default=str)


def safe_bead(data: Any) -> Bead:
    """Validate a Beads object, falling back to a minimal bead on skew."""
    try:
        return Bead.model_validate(data)
    except ValidationError:
        if isinstance(data, dict):
            bead_id = data.get("id")
            if isinstance(bead_id, str):
                return Bead(id=bead_id)
        return Bead(id="unknown")


def safe_beads(data: Any) -> list[Bead]:
    """Validate a Beads array or single object into a list of beads."""
    if data is None:
        return []
    if isinstance(data, list):
        return [safe_bead(item) for item in data]
    if isinstance(data, dict):
        return [safe_bead(data)]
    return []
