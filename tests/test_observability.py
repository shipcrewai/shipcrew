"""Tests for shipply observability emitter and Prometheus metrics."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest
from prometheus_client import CollectorRegistry, Counter, Gauge

import shipply.observability as observability
from shipply.db import close_db, get_db, init_db
from shipply.observability import EventEmitter

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db(tmp_path: Path) -> aiosqlite.Connection:
    """Yield an initialized SQLite connection backed by a temporary file."""
    path = tmp_path / "test.db"
    conn = await get_db(path)
    await init_db(conn)
    try:
        yield conn
    finally:
        await close_db(conn)


@pytest.fixture
def fresh_metrics(monkeypatch: pytest.MonkeyPatch) -> CollectorRegistry:
    """Install fresh collectors in the observability module for each test."""
    registry = CollectorRegistry()
    fresh_events_total = Counter(
        "shipply_events_total",
        "Total number of emitted events",
        ["event_type"],
        registry=registry,
    )
    fresh_proposal_transitions_total = Counter(
        "shipply_proposal_transitions_total",
        "Total number of proposal state transitions",
        ["from_state", "to_state"],
        registry=registry,
    )
    fresh_proposals_in_flight = Gauge(
        "shipply_proposals_in_flight",
        "Number of proposals currently in flight by stage",
        ["stage"],
        registry=registry,
    )
    fresh_beads_in_flight = Gauge(
        "shipply_beads_in_flight",
        "Number of beads currently in flight by status",
        ["status"],
        registry=registry,
    )
    monkeypatch.setattr("shipply.observability.events_total", fresh_events_total)
    monkeypatch.setattr(
        "shipply.observability.proposal_transitions_total", fresh_proposal_transitions_total
    )
    monkeypatch.setattr(
        "shipply.observability.proposals_in_flight", fresh_proposals_in_flight
    )
    monkeypatch.setattr("shipply.observability.beads_in_flight", fresh_beads_in_flight)
    return registry


async def test_emit_writes_event_and_returns_row_id(
    db: aiosqlite.Connection, fresh_metrics: CollectorRegistry
) -> None:
    """emit() writes an event row, returns the row id, and increments events_total."""
    emitter = EventEmitter(db)
    before = observability.events_total.labels(event_type="test_event")._value.get()
    row_id = await emitter.emit(
        event_type="test_event",
        proposal_id="p-1",
        payload={"hello": "world"},
    )
    assert row_id > 0

    cursor = await db.execute(
        "SELECT event_type, proposal_id, payload FROM events WHERE id = ?",
        (row_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    assert row[0] == "test_event"
    assert row[1] == "p-1"
    assert '"hello": "world"' in row[2]

    after = observability.events_total.labels(event_type="test_event")._value.get()
    assert after == before + 1


async def test_emit_proposal_transition_increments_transition_counter(
    db: aiosqlite.Connection, fresh_metrics: CollectorRegistry
) -> None:
    """proposal_transition events increment both events_total and proposal_transitions_total."""
    emitter = EventEmitter(db)
    before_events = observability.events_total.labels(event_type="proposal_transition")._value.get()
    before_transitions = observability.proposal_transitions_total.labels(
        from_state="INTAKE", to_state="DOC_REVIEW"
    )._value.get()

    row_id = await emitter.emit(
        event_type="proposal_transition",
        proposal_id="p-1",
        payload={"from_state": "INTAKE", "to_state": "DOC_REVIEW"},
    )
    assert row_id > 0

    after_events = observability.events_total.labels(event_type="proposal_transition")._value.get()
    after_transitions = observability.proposal_transitions_total.labels(
        from_state="INTAKE", to_state="DOC_REVIEW"
    )._value.get()

    assert after_events == before_events + 1
    assert after_transitions == before_transitions + 1


async def test_emit_non_transition_event_does_not_increment_transition_counter(
    db: aiosqlite.Connection, fresh_metrics: CollectorRegistry
) -> None:
    """Non-transition events should only increment events_total."""
    emitter = EventEmitter(db)
    before_transitions = observability.proposal_transitions_total.labels(
        from_state="INTAKE", to_state="DOC_REVIEW"
    )._value.get()

    await emitter.emit(
        event_type="comment",
        proposal_id="p-1",
        payload={"message": "hello"},
    )

    after_transitions = observability.proposal_transitions_total.labels(
        from_state="INTAKE", to_state="DOC_REVIEW"
    )._value.get()
    assert after_transitions == before_transitions


async def test_refresh_gauges_reflects_inflight_counts(
    db: aiosqlite.Connection, fresh_metrics: CollectorRegistry
) -> None:
    """refresh_gauges() updates Prometheus gauges from the in-flight views."""
    # Seed proposals and beads.
    await db.execute(
        "INSERT INTO proposals (id, state, title, sponsor) VALUES (?, 'INTAKE', 'T1', 'alice')",
        ("p-1",),
    )
    await db.execute(
        "INSERT INTO proposals (id, state, title, sponsor) VALUES (?, 'BLUEPRINT', 'T2', 'bob')",
        ("p-2",),
    )
    await db.execute(
        "INSERT INTO proposals (id, state, title, sponsor) VALUES (?, 'CLOSED', 'T3', 'carol')",
        ("p-3",),
    )
    await db.execute(
        "INSERT INTO beads (id, proposal_id, bead_id, status) VALUES (?, 'p-1', 'bd-1', 'running')",
        ("b-1",),
    )
    await db.execute(
        "INSERT INTO beads (id, proposal_id, bead_id, status) VALUES (?, 'p-1', 'bd-2', 'running')",
        ("b-2",),
    )
    await db.execute(
        "INSERT INTO beads (id, proposal_id, bead_id, status) VALUES (?, 'p-2', 'bd-3', 'pending')",
        ("b-3",),
    )
    await db.commit()

    emitter = EventEmitter(db)
    await emitter.refresh_gauges()

    assert observability.proposals_in_flight.labels(stage="INTAKE")._value.get() == 1
    assert observability.proposals_in_flight.labels(stage="BLUEPRINT")._value.get() == 1
    assert observability.proposals_in_flight.labels(stage="CLOSED")._value.get() == 0
    assert observability.beads_in_flight.labels(status="running")._value.get() == 2
    assert observability.beads_in_flight.labels(status="pending")._value.get() == 1
    assert observability.beads_in_flight.labels(status="done")._value.get() == 0


async def test_refresh_gauges_is_zero_when_no_inflight_records(
    db: aiosqlite.Connection, fresh_metrics: CollectorRegistry
) -> None:
    """With no in-flight records, gauges are set to zero."""
    emitter = EventEmitter(db)
    await emitter.refresh_gauges()

    assert observability.proposals_in_flight.labels(stage="INTAKE")._value.get() == 0
    assert observability.beads_in_flight.labels(status="running")._value.get() == 0
