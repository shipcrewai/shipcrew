"""Event emitter, Prometheus metrics, and structured logging helpers."""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite
from prometheus_client import CollectorRegistry, Counter, Gauge

logger = logging.getLogger("shipply")

REGISTRY = CollectorRegistry()

proposal_transitions_total = Counter(
    "shipply_proposal_transitions_total",
    "Total number of proposal state transitions",
    ["from_state", "to_state"],
    registry=REGISTRY,
)

events_total = Counter(
    "shipply_events_total",
    "Total number of emitted events",
    ["event_type"],
    registry=REGISTRY,
)

proposals_in_flight = Gauge(
    "shipply_proposals_in_flight",
    "Number of proposals currently in flight by stage",
    ["stage"],
    registry=REGISTRY,
)

beads_in_flight = Gauge(
    "shipply_beads_in_flight",
    "Number of beads currently in flight by status",
    ["status"],
    registry=REGISTRY,
)


class EventEmitter:
    """Persist observable events and update Prometheus metrics."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def emit(
        self,
        event_type: str,
        proposal_id: str,
        payload: dict[str, Any],
        stage: str | None = None,
    ) -> int:
        """Write an event row and update counters.

        Returns the newly created event ``id``.
        """
        payload_json = json.dumps(payload, default=str)
        cursor = await self._db.execute(
            """
            INSERT INTO events (proposal_id, event_type, stage, payload)
            VALUES (?, ?, ?, ?)
            """,
            (proposal_id, event_type, stage, payload_json),
        )
        await self._db.commit()
        row_id = cursor.lastrowid
        if row_id is None:
            raise RuntimeError("event insert did not return a row id")
        events_total.labels(event_type=event_type).inc()
        if event_type == "proposal_transition" and "to_state" in payload:
            to_state = payload["to_state"]
            from_state = payload.get("from_state", "unknown")
            proposal_transitions_total.labels(
                from_state=from_state, to_state=to_state
            ).inc()
        return int(row_id)

    async def refresh_gauges(self) -> None:
        """Refresh in-flight gauges from the materialized views."""
        cursor = await self._db.execute(
            "SELECT stage, count FROM v_proposals_in_flight"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        for stage, count in rows:
            proposals_in_flight.labels(stage=stage).set(count)

        cursor = await self._db.execute(
            "SELECT status, count FROM v_beads_in_flight"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        for status, count in rows:
            beads_in_flight.labels(status=status).set(count)


def configure_logging(level: str = "INFO") -> None:
    """Attach a simple stream handler to the ``shipply`` logger."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger("shipply")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not root.handlers:
        root.addHandler(handler)
    root.propagate = False


def log_event(message: str, **extra: Any) -> None:
    """Emit a structured JSON log line via the ``shipply`` logger."""
    record: dict[str, Any] = {"message": message}
    record.update(extra)
    logger.info(json.dumps(record, default=str))
