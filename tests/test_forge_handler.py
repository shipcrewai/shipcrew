"""Tests for the Forge handler."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
from pacto_bot_sdk._generated.models import AgentEventParams

from shipply.backends.beads_schema import Bead, Gate, Molecule
from shipply.config import (
    DatabaseConfig,
    GateConfig,
    ObservabilityConfig,
    PersonaConfig,
    ShipplyConfig,
    SquadsConfig,
)
from shipply.db import close_db, get_db, init_db
from shipply.harness import HarnessResult
from shipply.models import ProposalState
import shipply.handlers.forge as forge


FORGE_GROUP_ID = "test-gate-2-group"


def make_config(tmp_path: Path) -> ShipplyConfig:
    """Return a minimal ShipplyConfig with a Gate 2 squad."""
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
            "gate-1": GateConfig(quorum=3, threshold=0.5),
            "gate-2": GateConfig(quorum=2, threshold=0.75),
            "gate-3": GateConfig(quorum=2, threshold=0.75),
        },
        database=DatabaseConfig(path=str(tmp_path / "test.db")),
        squads=SquadsConfig(gate_2=FORGE_GROUP_ID),
        observability=ObservabilityConfig(),
    )


@pytest.fixture
def forge_test_config(tmp_path: Path) -> ShipplyConfig:
    return make_config(tmp_path)


@pytest.fixture
async def db(forge_test_config: ShipplyConfig) -> aiosqlite.Connection:
    """Yield an initialized SQLite connection to the temporary database."""
    path = Path(forge_test_config.database.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await get_db(path)
    await init_db(conn)
    yield conn
    await close_db(conn)


@pytest.fixture
def patch_config(forge_test_config: ShipplyConfig, monkeypatch) -> None:
    """Patch the handler's internal config helper to use the test config."""
    monkeypatch.setattr(forge, "_config", lambda: forge_test_config)


@pytest.fixture
def mock_bot(monkeypatch) -> MagicMock:
    """Return a mocked pacto_bot_sdk.Bot with the methods Forge uses."""
    bot = MagicMock()
    bot.send_dm = AsyncMock()
    bot.send_group_message = AsyncMock()
    bot.reply = MagicMock(return_value={"type": "reply"})
    bot.log = MagicMock()
    monkeypatch.setattr(forge, "bot", bot)
    return bot


@pytest.fixture
def mock_pool(monkeypatch) -> MagicMock:
    """Return a mock HarnessPool whose send returns a successful result."""
    pool = MagicMock()
    pool.send = AsyncMock(return_value=HarnessResult(status="success", payload={}))
    monkeypatch.setattr(forge, "_get_pool", AsyncMock(return_value=pool))
    return pool


@pytest.fixture
def mock_backend(monkeypatch) -> MagicMock:
    """Return a mock BeadsBackend instance and patch the handler's import."""
    backend = MagicMock()
    backend.create_molecule = AsyncMock()
    backend.get_ready = AsyncMock(return_value=[])
    backend.claim = AsyncMock()
    backend.close = AsyncMock()
    backend.sync = AsyncMock()
    backend.get_blocked = AsyncMock(return_value=[])
    backend.close_eligible_roots = AsyncMock(return_value=[])
    backend.get_bead = AsyncMock()
    backend.create_human_gate = AsyncMock()
    monkeypatch.setattr(forge, "BeadsBackend", MagicMock(return_value=backend))
    return backend


@pytest.fixture
def no_sleep(monkeypatch) -> None:
    """Make retry delays instantaneous."""
    monkeypatch.setattr(forge.asyncio, "sleep", AsyncMock())


@pytest.fixture
def no_diff(monkeypatch) -> None:
    """Suppress the working-directory diff helper."""
    monkeypatch.setattr(forge, "_working_directory_diff", AsyncMock(return_value=""))


def make_transition_event(proposal_id: str, to_state: str = ProposalState.FORGE.value) -> AgentEventParams:
    """Build a FORGE state-transition DM event."""
    return AgentEventParams(
        author="shipply-gate-2",
        bot_id="shipply-forge",
        chat_id=None,
        content=json.dumps(
            {
                "shipply": "transition",
                "proposal_id": proposal_id,
                "from": ProposalState.GATE_2.value,
                "to": to_state,
            }
        ),
        event_id="evt-1",
        rumor_id="rum-1",
        timestamp=1234567890,
        type="dm_received",
    )


async def insert_forge_proposal(
    db: aiosqlite.Connection,
    proposal_id: str,
    blueprint_doc: str = '{"plan_graph": {"nodes": [], "edges": []}}',
    state: str = ProposalState.FORGE.value,
) -> None:
    """Insert a proposal, frozen revision, and blueprint_frozen record."""
    await db.execute(
        "INSERT INTO proposals (id, state, title, sponsor) VALUES (?, ?, ?, ?)",
        (proposal_id, state, f"Proposal {proposal_id}", "sponsor-1"),
    )
    rev_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO revisions (id, proposal_id, rev_num, requirements_doc, blueprint_doc) VALUES (?, ?, ?, ?, ?)",
        (rev_id, proposal_id, 1, "# Requirements", blueprint_doc),
    )
    await db.execute(
        "INSERT INTO blueprint_frozen (revision_id, frozen_at) VALUES (?, datetime('now'))",
        (rev_id,),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


async def test_forge_dm_executes_ready_beads_and_advances_to_gate_3(
    db: aiosqlite.Connection,
    patch_config: None,
    mock_bot: MagicMock,
    mock_backend: MagicMock,
    mock_pool: MagicMock,
    no_sleep: None,
    no_diff: None,
) -> None:
    """A FORGE transition should run all ready beads and move to GATE_3."""
    proposal_id = "prop-forge-01"
    await insert_forge_proposal(db, proposal_id)

    root = Bead(id="mol-1")
    bead_a = Bead(id="bead-a", title="Bead A")
    bead_b = Bead(id="bead-b", title="Bead B")
    molecule = Molecule(
        id="mol-1",
        root_id="mol-1",
        proposal_id=proposal_id,
        bead_ids=["bead-a", "bead-b"],
        beads=[root, bead_a, bead_b],
    )
    mock_backend.create_molecule.return_value = molecule
    mock_backend.get_ready.side_effect = [[bead_a, bead_b], []]
    mock_backend.claim.side_effect = lambda bead_id: Bead(id=bead_id)
    mock_backend.close.return_value = Bead(id="dummy", status="closed")
    mock_backend.close_eligible_roots.return_value = ["mol-1"]
    mock_backend.get_bead.return_value = Bead(
        id="mol-1", pr_url="https://github.com/org/repo/pull/1"
    )

    event = make_transition_event(proposal_id)
    await forge.on_dm(event, mock_bot)

    mock_backend.create_molecule.assert_awaited_once()
    assert mock_backend.claim.await_count == 2
    for call in mock_backend.claim.call_args_list:
        assert call.args[0] in {"bead-a", "bead-b"}

    assert mock_pool.send.await_count == 2
    for call in mock_pool.send.call_args_list:
        assert call.kwargs["task"] == "execute-bead"
        assert call.kwargs["persona"] == "forge"
        assert call.kwargs["context"]["bead"]["id"] in {"bead-a", "bead-b"}
        assert call.kwargs["context"]["proposal_id"] == proposal_id

    mock_backend.close.assert_awaited()
    for call in mock_backend.close.call_args_list:
        assert call.kwargs["reason"] == "completed"
    assert mock_backend.sync.await_count == 2
    mock_backend.close_eligible_roots.assert_awaited_once_with("mol-1")
    mock_backend.get_bead.assert_awaited_once_with("mol-1")

    # Database should reflect completion and the PR URL.
    cur = await db.execute("SELECT state FROM proposals WHERE id = ?", (proposal_id,))
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    assert row[0] == ProposalState.GATE_3.value

    cur = await db.execute("SELECT pr_url FROM molecules WHERE proposal_id = ?", (proposal_id,))
    row = await cur.fetchone()
    await cur.close()
    assert row[0] == "https://github.com/org/repo/pull/1"

    mock_bot.send_dm.assert_awaited_once()
    dm_call = mock_bot.send_dm.call_args
    assert dm_call.kwargs["recipient"] == "shipply-gate-3"
    assert ProposalState.GATE_3.value in dm_call.kwargs["content"]


# ---------------------------------------------------------------------------
# Failure / escalation path
# ---------------------------------------------------------------------------


async def test_forge_harness_failure_retries_and_creates_human_gate(
    db: aiosqlite.Connection,
    patch_config: None,
    mock_bot: MagicMock,
    mock_backend: MagicMock,
    mock_pool: MagicMock,
    no_sleep: None,
    no_diff: None,
) -> None:
    """A failing bead should be retried 3 times and then escalated to a human gate."""
    proposal_id = "prop-forge-02"
    await insert_forge_proposal(db, proposal_id)

    bead = Bead(id="bead-fail", title="Failing bead")
    molecule = Molecule(
        id="mol-2",
        root_id="mol-2",
        proposal_id=proposal_id,
        bead_ids=["bead-fail"],
        beads=[bead],
    )
    mock_backend.create_molecule.return_value = molecule
    mock_backend.get_ready.side_effect = [[bead], []]
    mock_backend.claim.return_value = bead
    mock_backend.create_human_gate.return_value = Gate(id="gate-1", blocks="bead-fail")
    mock_pool.send.return_value = HarnessResult(status="error", payload="boom")

    event = make_transition_event(proposal_id)
    await forge.on_dm(event, mock_bot)

    assert mock_pool.send.await_count == 3
    for call in mock_pool.send.call_args_list:
        assert call.kwargs["task"] == "execute-bead"
        assert call.kwargs["persona"] == "forge"

    # Retry context amendments on attempts 2 and 3.
    third_context = mock_pool.send.call_args_list[2].kwargs["context"]
    assert third_context["previous_attempt"] == 2
    assert "previous_error" in third_context
    assert "extended_instructions" in third_context

    mock_backend.create_human_gate.assert_awaited_once_with(
        "bead-fail", reason="Harness failed after retries"
    )
    mock_bot.send_group_message.assert_awaited_once()
    alert_call = mock_bot.send_group_message.call_args
    assert alert_call.args[0] == FORGE_GROUP_ID
    assert "Forge bead failure" in alert_call.args[1]
    assert "bead-fail" in alert_call.args[1]

    cur = await db.execute(
        "SELECT status FROM beads WHERE proposal_id = ? AND bead_id = ?",
        (proposal_id, "bead-fail"),
    )
    row = await cur.fetchone()
    await cur.close()
    assert row[0] == "blocked"

    cur = await db.execute("SELECT state FROM proposals WHERE id = ?", (proposal_id,))
    row = await cur.fetchone()
    await cur.close()
    assert row[0] == ProposalState.FORGE.value


# ---------------------------------------------------------------------------
# All beads complete
# ---------------------------------------------------------------------------


async def test_forge_all_beads_complete_closes_root_and_records_pr_url(
    db: aiosqlite.Connection,
    patch_config: None,
    mock_bot: MagicMock,
    mock_backend: MagicMock,
    mock_pool: MagicMock,
    no_sleep: None,
    no_diff: None,
) -> None:
    """When no beads are ready or blocked, the root should close and the PR URL recorded."""
    proposal_id = "prop-forge-03"
    await insert_forge_proposal(db, proposal_id)

    root = Bead(id="mol-3")
    molecule = Molecule(
        id="mol-3",
        root_id="mol-3",
        proposal_id=proposal_id,
        bead_ids=["bead-done"],
        beads=[root, Bead(id="bead-done", title="Done")],
    )
    mock_backend.create_molecule.return_value = molecule
    mock_backend.get_ready.return_value = []
    mock_backend.get_blocked.return_value = []
    mock_backend.close_eligible_roots.return_value = ["mol-3"]
    mock_backend.get_bead.return_value = Bead(id="mol-3", pr_url="https://pr/3")

    event = make_transition_event(proposal_id)
    await forge.on_dm(event, mock_bot)

    mock_backend.create_molecule.assert_awaited_once()
    mock_backend.get_ready.assert_awaited_once_with("mol-3")
    mock_backend.get_blocked.assert_awaited_once_with("mol-3")
    mock_backend.close_eligible_roots.assert_awaited_once_with("mol-3")
    mock_backend.get_bead.assert_awaited_once_with("mol-3")

    cur = await db.execute("SELECT pr_url FROM molecules WHERE proposal_id = ?", (proposal_id,))
    row = await cur.fetchone()
    await cur.close()
    assert row[0] == "https://pr/3"

    cur = await db.execute("SELECT state FROM proposals WHERE id = ?", (proposal_id,))
    row = await cur.fetchone()
    await cur.close()
    assert row[0] == ProposalState.GATE_3.value

    mock_bot.send_dm.assert_awaited_once()
    dm_call = mock_bot.send_dm.call_args
    assert dm_call.kwargs["recipient"] == "shipply-gate-3"
    assert ProposalState.GATE_3.value in dm_call.kwargs["content"]

    # No harness calls and no diagnostic alerts when there is nothing to do.
    mock_pool.send.assert_not_awaited()
    mock_bot.send_group_message.assert_not_awaited()
