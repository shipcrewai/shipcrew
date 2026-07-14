"""Tests for the Gate 2 (Technical RFC) bot handler."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from pacto_bot_sdk._generated.models import AgentEventParams
from shipply.config import (
    DatabaseConfig,
    GateConfig,
    ObservabilityConfig,
    PersonaConfig,
    ShipplyConfig,
    SquadsConfig,
)
from shipply.db import close_db, get_db, init_db
from shipply.models import ProposalState
import shipply.handlers.gate2 as gate2


pytestmark = pytest.mark.asyncio

GATE_2_GROUP_ID = "test-gate-2-group"


def make_config(tmp_path: Path) -> ShipplyConfig:
    """Return a minimal ShipplyConfig backed by a temporary database."""
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
        squads=SquadsConfig(gate_2=GATE_2_GROUP_ID),
        observability=ObservabilityConfig(),
    )


@pytest.fixture
async def test_config(tmp_path: Path) -> ShipplyConfig:
    return make_config(tmp_path)


@pytest.fixture
async def db(test_config: ShipplyConfig) -> aiosqlite.Connection:
    """Yield an initialized SQLite connection to the temp database."""
    path = Path(test_config.database.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await get_db(path)
    await init_db(conn)
    yield conn
    await close_db(conn)


@pytest.fixture
def patch_config(test_config: ShipplyConfig, monkeypatch) -> None:
    """Patch the handler's internal config helper to use the test config."""
    monkeypatch.setattr(gate2, "_config", lambda: test_config)


@pytest.fixture
def mock_bot(monkeypatch) -> MagicMock:
    """Return a mocked pacto_bot_sdk.Bot with the methods Gate 2 uses."""
    bot = MagicMock()
    bot.send_group_message = AsyncMock(return_value="msg-123")
    bot.send_dm = AsyncMock()
    bot.is_squad_member = AsyncMock(return_value=True)
    bot.reply = MagicMock(return_value={"type": "reply"})
    bot.log = MagicMock()
    monkeypatch.setattr(gate2, "bot", bot)
    return bot


def make_event(
    content: str,
    author: str = "maintainer-1",
    chat_id: str | None = GATE_2_GROUP_ID,
    event_type: str = "mls_group_message_received",
) -> AgentEventParams:
    """Build a pacto-bot-api event for tests."""
    return AgentEventParams(
        author=author,
        bot_id="shipply-gate-2",
        chat_id=chat_id,
        content=content,
        event_id="evt-1",
        rumor_id="rum-1",
        timestamp=1234567890,
        type=event_type,
    )


async def insert_proposal(
    db: aiosqlite.Connection,
    proposal_id: str,
    state: str = ProposalState.GATE_2.value,
    title: str = "Test Proposal",
    sponsor: str = "sponsor-1",
) -> None:
    await db.execute(
        "INSERT INTO proposals (id, state, title, sponsor) VALUES (?, ?, ?, ?)",
        (proposal_id, state, title, sponsor),
    )
    await db.commit()


async def insert_revision(
    db: aiosqlite.Connection,
    proposal_id: str,
    rev_num: int = 1,
    requirements_doc: str | None = None,
    blueprint_doc: str | None = "# Blueprint\n\nFormula output",
) -> str:
    rev_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO revisions (id, proposal_id, rev_num, requirements_doc, blueprint_doc) VALUES (?, ?, ?, ?, ?)",
        (rev_id, proposal_id, rev_num, requirements_doc, blueprint_doc),
    )
    await db.commit()
    return rev_id


async def insert_gate(
    db: aiosqlite.Connection,
    proposal_id: str,
    group_id: str = GATE_2_GROUP_ID,
    metadata: dict | None = None,
) -> str:
    gate_id = str(uuid.uuid4())
    meta = metadata or {"group_id": group_id}
    await db.execute(
        "INSERT INTO gates (id, proposal_id, gate_type, status, votes, metadata) VALUES (?, ?, ?, ?, ?, ?)",
        (gate_id, proposal_id, "gate-2", "open", "{}", json.dumps(meta)),
    )
    await db.commit()
    return gate_id


async def insert_squad_members(
    db: aiosqlite.Connection,
    group_id: str,
    member_pubkeys: list[str],
) -> None:
    for pubkey in member_pubkeys:
        await db.execute(
            "INSERT OR IGNORE INTO squad_members (group_id, member_pubkey) VALUES (?, ?)",
            (group_id, pubkey),
        )
    await db.commit()


class TestHandleEntry:
    """Tests for entering GATE_2 and posting the Blueprint output."""

    async def test_gate_2_dm_posts_blueprint_output_to_squad(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        """A GATE_2 transition DM should post the Blueprint output to the maintainer Squad."""
        proposal_id = "prop-gate2-01"
        title = "RFC Proposal"
        blueprint_doc = "# Blueprint Formula\n\nSteps: one, two, three."
        await insert_proposal(db, proposal_id, title=title)
        await insert_revision(db, proposal_id, rev_num=1, blueprint_doc=blueprint_doc)

        content = json.dumps(
            {
                "shipply": "transition",
                "proposal_id": proposal_id,
                "from": ProposalState.BLUEPRINT.value,
                "to": ProposalState.GATE_2.value,
            }
        )
        event = make_event(content, chat_id=None, event_type="dm_received")
        await gate2.on_dm(event, mock_bot)

        mock_bot.send_group_message.assert_awaited_once()
        call = mock_bot.send_group_message.call_args
        assert call.args[0] == GATE_2_GROUP_ID
        assert title in call.args[1]
        assert blueprint_doc in call.args[1]
        assert "`/authorize`" in call.args[1]
        assert "`/rebuild" in call.args[1]

        cursor = await db.execute(
            "SELECT metadata FROM gates WHERE proposal_id = ? AND gate_type = ?",
            (proposal_id, "gate-2"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        metadata = json.loads(row[0])
        assert metadata["group_id"] == GATE_2_GROUP_ID
        assert metadata["blueprint_message_id"] == "msg-123"

    async def test_gate_2_entry_ignores_proposal_in_wrong_state(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        """A proposal not in GATE_2 should not be announced."""
        proposal_id = "prop-gate2-wrong-state"
        await insert_proposal(db, proposal_id, state=ProposalState.BLUEPRINT.value)
        await insert_revision(db, proposal_id, rev_num=1)

        content = json.dumps(
            {
                "shipply": "transition",
                "proposal_id": proposal_id,
                "from": ProposalState.BLUEPRINT.value,
                "to": ProposalState.GATE_2.value,
            }
        )
        event = make_event(content, chat_id=None, event_type="dm_received")
        result = await gate2.on_dm(event, mock_bot)

        assert result is None
        mock_bot.send_group_message.assert_not_called()
        mock_bot.log.assert_called_once()
        assert "not GATE_2" in mock_bot.log.call_args[0][0]

    async def test_gate_2_entry_warns_when_no_squad_configured(
        self,
        tmp_path: Path,
        db: aiosqlite.Connection,
        monkeypatch,
        mock_bot: MagicMock,
    ) -> None:
        """If no gate-2 squad is configured, the handler should warn and exit."""
        proposal_id = "prop-gate2-no-squad"
        cfg = make_config(tmp_path)
        cfg.squads.gate_2 = None
        monkeypatch.setattr(gate2, "_config", lambda: cfg)
        await insert_proposal(db, proposal_id)
        await insert_revision(db, proposal_id, rev_num=1)

        content = json.dumps(
            {
                "shipply": "transition",
                "proposal_id": proposal_id,
                "from": ProposalState.BLUEPRINT.value,
                "to": ProposalState.GATE_2.value,
            }
        )
        event = make_event(content, chat_id=None, event_type="dm_received")
        result = await gate2.on_dm(event, mock_bot)

        assert result is None
        mock_bot.send_group_message.assert_not_called()
        mock_bot.log.assert_called_once()
        assert "no squad configured" in mock_bot.log.call_args[0][0]


class TestHandleAuthorize:
    """Tests for the /authorize command advancing a proposal to FORGE."""

    async def test_authorize_transitions_to_forge_freezes_blueprint_and_notifies_forge(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        """A maintainer /authorize should freeze the Blueprint and move to FORGE."""
        proposal_id = "prop-gate2-auth"
        await insert_proposal(db, proposal_id)
        rev_id = await insert_revision(db, proposal_id, rev_num=1, blueprint_doc="# Blueprint")
        await insert_gate(db, proposal_id)
        await insert_squad_members(db, GATE_2_GROUP_ID, ["maintainer-1"])

        event = make_event("/authorize", author="maintainer-1")
        await gate2.on_group_message(event, mock_bot)

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.FORGE.value

        cursor = await db.execute(
            "SELECT frozen_at FROM blueprint_frozen WHERE revision_id = ?",
            (rev_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        assert row[0] is not None

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? AND event_type = ?",
            (proposal_id, "proposal_transition"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        payload = json.loads(row[1])
        assert payload["from_state"] == ProposalState.GATE_2.value
        assert payload["to_state"] == ProposalState.FORGE.value

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? AND event_type = ?",
            (proposal_id, "blueprint_frozen"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None

        mock_bot.send_dm.assert_awaited_once()
        dm_call = mock_bot.send_dm.call_args
        assert dm_call.kwargs["recipient"] == "shipply-forge"
        dm_payload = json.loads(dm_call.kwargs["content"])
        assert dm_payload["shipply"] == "transition"
        assert dm_payload["proposal_id"] == proposal_id
        assert dm_payload["from"] == ProposalState.GATE_2.value
        assert dm_payload["to"] == ProposalState.FORGE.value

    async def test_authorize_rejected_for_non_member(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        """Only Squad members may authorize a Blueprint."""
        proposal_id = "prop-gate2-non-member"
        await insert_proposal(db, proposal_id)
        await insert_revision(db, proposal_id, rev_num=1, blueprint_doc="# Blueprint")
        await insert_gate(db, proposal_id)
        mock_bot.is_squad_member.return_value = False

        event = make_event("/authorize", author="stranger")
        result = await gate2.on_group_message(event, mock_bot)

        assert result is not None
        reply_text = mock_bot.reply.call_args[0][1]
        assert "Only Squad members may authorize" in reply_text

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.GATE_2.value

        mock_bot.send_dm.assert_not_called()


class TestHandleRebuild:
    """Tests for the /rebuild command returning a proposal to BLUEPRINT."""

    async def test_rebuild_transitions_to_blueprint_with_amendments_and_notifies_blueprint(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        """A maintainer /rebuild should return the proposal to BLUEPRINT with amendments."""
        proposal_id = "prop-gate2-rebuild"
        await insert_proposal(db, proposal_id)
        await insert_revision(db, proposal_id, rev_num=1, blueprint_doc="# Blueprint")
        gate_id = await insert_gate(db, proposal_id)
        await insert_squad_members(db, GATE_2_GROUP_ID, ["maintainer-1"])

        event = make_event("/rebuild add tests coverage=full", author="maintainer-1")
        await gate2.on_group_message(event, mock_bot)

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.BLUEPRINT.value

        cursor = await db.execute(
            "SELECT metadata FROM gates WHERE id = ?",
            (gate_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        metadata = json.loads(row[0])
        amendments = metadata["amendments"]
        assert amendments["reason"] == "add tests coverage=full"
        assert amendments["coverage"] == "full"

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? AND event_type = ?",
            (proposal_id, "proposal_transition"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        payload = json.loads(row[1])
        assert payload["from_state"] == ProposalState.GATE_2.value
        assert payload["to_state"] == ProposalState.BLUEPRINT.value
        assert payload["reason"] == "maintainer requested rebuild"

        mock_bot.send_dm.assert_awaited_once()
        dm_call = mock_bot.send_dm.call_args
        assert dm_call.kwargs["recipient"] == "shipply-blueprint"
        dm_payload = json.loads(dm_call.kwargs["content"])
        assert dm_payload["shipply"] == "transition"
        assert dm_payload["proposal_id"] == proposal_id
        assert dm_payload["from"] == ProposalState.GATE_2.value
        assert dm_payload["to"] == ProposalState.BLUEPRINT.value

    async def test_rebuild_rejected_for_non_member(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        """Only Squad members may request a Blueprint rebuild."""
        proposal_id = "prop-gate2-rebuild-non-member"
        await insert_proposal(db, proposal_id)
        await insert_revision(db, proposal_id, rev_num=1, blueprint_doc="# Blueprint")
        await insert_gate(db, proposal_id)
        mock_bot.is_squad_member.return_value = False

        event = make_event("/rebuild needs more tests", author="stranger")
        result = await gate2.on_group_message(event, mock_bot)

        assert result is not None
        reply_text = mock_bot.reply.call_args[0][1]
        assert "Only Squad members may request a Blueprint rebuild" in reply_text

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.GATE_2.value

        mock_bot.send_dm.assert_not_called()

    async def test_rebuild_with_no_reason_uses_default_reason(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        """/rebuild with no extra text should still record a default reason."""
        proposal_id = "prop-gate2-rebuild-default"
        await insert_proposal(db, proposal_id)
        await insert_revision(db, proposal_id, rev_num=1, blueprint_doc="# Blueprint")
        gate_id = await insert_gate(db, proposal_id)
        await insert_squad_members(db, GATE_2_GROUP_ID, ["maintainer-1"])

        event = make_event("/rebuild", author="maintainer-1")
        await gate2.on_group_message(event, mock_bot)

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.BLUEPRINT.value

        cursor = await db.execute(
            "SELECT metadata FROM gates WHERE id = ?",
            (gate_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        metadata = json.loads(row[0])
        assert metadata["amendments"]["reason"] == "requested by maintainer"

        mock_bot.send_dm.assert_awaited_once()
        dm_call = mock_bot.send_dm.call_args
        assert dm_call.kwargs["recipient"] == "shipply-blueprint"
