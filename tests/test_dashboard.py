"""Tests for the Shipply dashboard HTTP API."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from shipply.config import ShipplyConfig
from shipply.dashboard import build_app


@pytest.fixture
async def dashboard_client(
    test_config: ShipplyConfig, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """Build an aiohttp TestClient backed by the seeded database."""
    monkeypatch.setattr("shipply.dashboard.load_config", lambda: test_config)

    app = build_app()
    async with TestClient(TestServer(app)) as client:
        yield client


pytestmark = pytest.mark.asyncio


async def test_health_returns_ok(dashboard_client: TestClient) -> None:
    """GET /health returns 200 and a simple ok payload."""
    resp = await dashboard_client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"status": "ok"}


async def test_metrics_includes_shipply_metrics(dashboard_client: TestClient) -> None:
    """GET /metrics returns Prometheus exposition containing shipply metrics."""
    resp = await dashboard_client.get("/metrics")
    assert resp.status == 200
    text = await resp.text()
    assert resp.content_type == "text/plain"
    assert "shipply_events_total" in text
    assert "shipply_proposal_transitions_total" in text
    assert "shipply_proposals_in_flight" in text
    assert "shipply_beads_in_flight" in text


async def test_list_proposals_returns_active_proposals(
    dashboard_client: TestClient,
) -> None:
    """GET /proposals returns active proposals with stage, age, and last event."""
    resp = await dashboard_client.get("/proposals")
    assert resp.status == 200
    data = await resp.json()
    assert "proposals" in data
    proposals = data["proposals"]
    assert len(proposals) == 2

    ids = {p["id"] for p in proposals}
    assert "prop-001" in ids
    assert "prop-003" in ids
    assert "prop-002" not in ids  # CLOSED proposals are excluded

    active = next(p for p in proposals if p["id"] == "prop-001")
    assert active["stage"] == "GATE_1"
    assert active["title"] == "Active Gate 1 Proposal"
    assert active["sponsor"] == "alice"
    assert "age_seconds" in active
    assert active["age_seconds"] >= 0
    assert active["created_at"] is not None
    assert active["last_event_at"] is not None

    blueprint = next(p for p in proposals if p["id"] == "prop-003")
    assert blueprint["stage"] == "BLUEPRINT"
    assert blueprint["title"] == "Blueprint Proposal"


async def test_get_proposal_returns_full_timeline(
    dashboard_client: TestClient,
) -> None:
    """GET /proposals/<id> returns proposal, revisions, events, gates, and beads."""
    resp = await dashboard_client.get("/proposals/prop-001")
    assert resp.status == 200
    data = await resp.json()

    assert data["proposal"]["id"] == "prop-001"
    assert data["proposal"]["state"] == "GATE_1"
    assert data["proposal"]["title"] == "Active Gate 1 Proposal"
    assert data["proposal"]["sponsor"] == "alice"

    assert len(data["revisions"]) == 1
    revision = data["revisions"][0]
    assert revision["rev_num"] == 1
    assert revision["requirements_doc"] == "requirements.md"
    assert revision["blueprint_doc"] == "blueprint.md"

    assert len(data["events"]) == 3
    event_types = {e["event_type"] for e in data["events"]}
    assert "proposal_created" in event_types
    assert "proposal_transition" in event_types

    assert len(data["gates"]) == 1
    gate = data["gates"][0]
    assert gate["gate_type"] == "gate-1"
    assert gate["status"] == "open"
    assert gate["votes"] == {"alice": "approve"}
    assert gate["metadata"] == {"threshold": 0.66}

    assert len(data["beads"]) == 2
    bead_ids = {b["bead_id"] for b in data["beads"]}
    assert bead_ids == {"scout", "gate-1"}


async def test_get_missing_proposal_returns_404(dashboard_client: TestClient) -> None:
    """GET /proposals/<missing> returns a 404 JSON error."""
    resp = await dashboard_client.get("/proposals/does-not-exist")
    assert resp.status == 404
    data = await resp.json()
    assert "error" in data
    assert "proposal not found" in data["error"]
