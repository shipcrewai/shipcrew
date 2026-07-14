

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from shipply.config import (
    DatabaseConfig,
    GateConfig,
    ObservabilityConfig,
    PersonaConfig,
    ShipplyConfig,
)
from shipply.db import get_db, init_db
from shipply.observability import EventEmitter


async def seed_database(db: aiosqlite.Connection) -> dict[str, str]:
    """Populate a fresh database with representative proposals and children."""
    proposal_id = "prop-001"
    closed_id = "prop-002"
    blueprint_id = "prop-003"

    # Proposals: one active in GATE_1, one in BLUEPRINT, one CLOSED.
    await db.execute(
        """
        INSERT INTO proposals (id, state, title, sponsor, created_at, updated_at)
        VALUES (?, 'GATE_1', 'Active Gate 1 Proposal', 'alice', ?, ?)
        """,
        (proposal_id, "2026-07-01T10:00:00", "2026-07-01T10:00:00"),
    )
    await db.execute(
        """
        INSERT INTO proposals (id, state, title, sponsor, created_at, updated_at)
        VALUES (?, 'CLOSED', 'Closed Proposal', 'bob', ?, ?)
        """,
        (closed_id, "2026-07-02T10:00:00", "2026-07-02T10:00:00"),
    )
    await db.execute(
        """
        INSERT INTO proposals (id, state, title, sponsor, created_at, updated_at)
        VALUES (?, 'BLUEPRINT', 'Blueprint Proposal', 'carol', ?, ?)
        """,
        (blueprint_id, "2026-07-03T10:00:00", "2026-07-03T10:00:00"),
    )

    # Revisions for the active proposal.
    await db.execute(
        """
        INSERT INTO revisions (id, proposal_id, rev_num, requirements_doc, blueprint_doc, frozen_at)
        VALUES (?, ?, 1, ?, ?, ?)
        """,
        ("rev-001", proposal_id, "requirements.md", "blueprint.md", "2026-07-01T11:00:00"),
    )

    # Beads for the active and blueprint proposals.
    await db.execute(
        """
        INSERT INTO beads (id, proposal_id, bead_id, status, molecule_id)
        VALUES (?, ?, 'scout', 'running', ?)
        """,
        ("bead-001", proposal_id, "mol-001"),
    )
    await db.execute(
        """
        INSERT INTO beads (id, proposal_id, bead_id, status, molecule_id)
        VALUES (?, ?, 'gate-1', 'pending', ?)
        """,
        ("bead-002", proposal_id, None),
    )
    await db.execute(
        """
        INSERT INTO beads (id, proposal_id, bead_id, status, molecule_id)
        VALUES (?, ?, 'blueprint', 'done', ?)
        """,
        ("bead-003", blueprint_id, "mol-002"),
    )

    # Gate for the active proposal.
    await db.execute(
        """
        INSERT INTO gates (id, proposal_id, gate_type, status, votes, metadata)
        VALUES (?, ?, 'gate-1', 'open', ?, ?)
        """,
        ("gate-001", proposal_id, '{"alice": "approve"}', '{"threshold": 0.66}'),
    )

    # Events for the active and blueprint proposals; one for the closed proposal.
    await db.execute(
        """
        INSERT INTO events (proposal_id, event_type, stage, payload, created_at)
        VALUES (?, 'proposal_created', 'INTAKE', '{}', ?)
        """,
        (proposal_id, "2026-07-01T10:00:00"),
    )
    await db.execute(
        """
        INSERT INTO events (proposal_id, event_type, stage, payload, created_at)
        VALUES (?, 'proposal_transition', 'GATE_1', ?, ?)
        """,
        (proposal_id, '{"from_state": "DOC_REVIEW", "to_state": "GATE_1"}', "2026-07-01T12:00:00"),
    )
    await db.execute(
        """
        INSERT INTO events (proposal_id, event_type, stage, payload, created_at)
        VALUES (?, 'proposal_created', 'BLUEPRINT', '{}', ?)
        """,
        (blueprint_id, "2026-07-03T10:00:00"),
    )
    await db.execute(
        """
        INSERT INTO events (proposal_id, event_type, stage, payload, created_at)
        VALUES (?, 'proposal_closed', 'GATE_3', '{}', ?)
        """,
        (closed_id, "2026-07-02T11:00:00"),
    )

    # Emit one event through the emitter so Prometheus counters increment
    # and refresh gauges so gauges are populated for the metrics endpoint.
    emitter = EventEmitter(db)
    await emitter.emit(
        event_type="proposal_transition",
        proposal_id=proposal_id,
        payload={"from_state": "INTAKE", "to_state": "GATE_1"},
        stage="GATE_1",
    )
    await emitter.refresh_gauges()

    return {
        "active": proposal_id,
        "closed": closed_id,
        "blueprint": blueprint_id,
    }


@pytest.fixture
async def seeded_db(tmp_path: Path) -> Path:
    """Create an initialized SQLite database seeded with test data."""
    db_path = tmp_path / "seeded.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await get_db(db_path)
    await init_db(db)
    try:
        await seed_database(db)
        await db.commit()
    finally:
        await db.close()
    return db_path


@pytest.fixture
def test_config(seeded_db: Path) -> ShipplyConfig:
    """Return a ShipplyConfig pointing at the seeded database."""
    return ShipplyConfig(
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
        database=DatabaseConfig(path=str(seeded_db)),
        observability=ObservabilityConfig(),
    )

