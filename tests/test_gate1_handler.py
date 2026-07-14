"""Tests for the Gate 1 (Product RFC) bot handler."""

from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
import shipply.handlers.gate1 as gate1


pytestmark = pytest.mark.asyncio

GROUP_ID = "test-gate-1-group"
GATE_TYPE = gate1.GATE_TYPE


def make_config(tmp_path: Path, *, quorum: int = 3, threshold: float = 0.5) -> ShipplyConfig:
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
            "gate-1": GateConfig(quorum=quorum, threshold=threshold),
            "gate-2": GateConfig(quorum=2, threshold=0.75),
            "gate-3": GateConfig(quorum=2, threshold=0.75),
        },
        database=DatabaseConfig(path=str(tmp_path / "test.db")),
        squads=SquadsConfig(gate_1=GROUP_ID),
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
    monkeypatch.setattr(gate1, "_config", lambda: test_config)


@pytest.fixture
def mock_bot(monkeypatch) -> MagicMock:
    """Return a mocked pacto_bot_sdk.Bot with the methods Gate 1 uses."""
    bot = MagicMock()
    bot.send_group_message = AsyncMock(return_value="msg-123")
    bot.send_dm = AsyncMock()
    bot.is_squad_member = AsyncMock(return_value=True)
    bot.reply = MagicMock(return_value={"type": "reply"})
    bot.log = MagicMock()
    monkeypatch.setattr(gate1, "bot", bot)
    return bot


def make_event(
    content: str,
    author: str = "member-1",
    chat_id: str | None = GROUP_ID,
    event_type: str = "mls_group_message_received",
) -> AgentEventParams:
    """Build a pacto-bot-api event for tests."""
    return AgentEventParams(
        author=author,
        bot_id="shipply-gate-1",
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
    state: str = ProposalState.GATE_1.value,
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
    blueprint_doc: str | None = None,
) -> str:
    rev_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO revisions (id, proposal_id, rev_num, requirements_doc, blueprint_doc) VALUES (?, ?, ?, ?, ?)",
        (rev_id, proposal_id, rev_num, requirements_doc, blueprint_doc),
    )
    await db.commit()
    return rev_id


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
    """Tests for proposal entry into Gate 1."""

    async def test_handle_entry_posts_requirements_doc_and_dependency_card(
        self,
        db: aiosqlite.Connection,
        test_config: ShipplyConfig,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        proposal_id = "prop-entry"
        await insert_proposal(db, proposal_id, ProposalState.GATE_1.value)
        requirements = "# Requirements\n\nGoals: build the thing."
        await insert_revision(db, proposal_id, rev_num=1, requirements_doc=requirements)

        event = make_event("ignored", chat_id=None, event_type="dm_received")
        await gate1._handle_entry(mock_bot, event, proposal_id)

        assert mock_bot.send_group_message.call_count == 2
        first_call = mock_bot.send_group_message.call_args_list[0]
        assert first_call.args[0] == GROUP_ID
        assert "Test Proposal" in first_call.args[1]
        assert requirements in first_call.args[1]

        second_call = mock_bot.send_group_message.call_args_list[1]
        assert second_call.args[0] == GROUP_ID
        assert "Dependency Card" in second_call.args[1]
        assert proposal_id in second_call.args[1]

        cursor = await db.execute(
            "SELECT votes, metadata FROM gates WHERE proposal_id = ? AND gate_type = ?",
            (proposal_id, GATE_TYPE),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        metadata = json.loads(row[1])
        assert metadata["group_id"] == GROUP_ID
        assert metadata["pinned_message_id"] == "msg-123"

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? ORDER BY id",
            (proposal_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        assert len(rows) == 1
        assert rows[0][0] == "gate_entered"
        payload = json.loads(rows[0][1])
        assert payload["group_id"] == GROUP_ID
        assert payload["pinned_message_id"] == "msg-123"

    async def test_handle_entry_ignores_proposal_in_wrong_state(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        proposal_id = "prop-wrong-state"
        await insert_proposal(db, proposal_id, ProposalState.INTAKE.value)
        event = make_event("ignored", chat_id=None, event_type="dm_received")

        result = await gate1._handle_entry(mock_bot, event, proposal_id)

        assert result is None
        mock_bot.send_group_message.assert_not_called()
        mock_bot.log.assert_called_once()
        assert "not GATE_1" in mock_bot.log.call_args[0][0]

    async def test_handle_entry_warns_when_no_group_configured(
        self,
        tmp_path: Path,
        db: aiosqlite.Connection,
        monkeypatch,
        mock_bot: MagicMock,
    ) -> None:
        proposal_id = "prop-no-group"
        cfg = make_config(tmp_path)
        cfg.squads.gate_1 = None
        monkeypatch.setattr(gate1, "_config", lambda: cfg)
        await insert_proposal(db, proposal_id, ProposalState.GATE_1.value)
        event = make_event("ignored", chat_id=None, event_type="dm_received")

        result = await gate1._handle_entry(mock_bot, event, proposal_id)

        assert result is None
        mock_bot.send_group_message.assert_not_called()
        mock_bot.log.assert_called_once()
        assert "no squad configured" in mock_bot.log.call_args[0][0]


class TestHandleVoteApprove:
    """Tests for approving votes and transition to BLUEPRINT."""

    async def test_vote_approve_reaches_quorum_transitions_to_blueprint(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        proposal_id = "prop-approve"
        await insert_proposal(db, proposal_id, ProposalState.GATE_1.value)
        await insert_revision(db, proposal_id, rev_num=1, requirements_doc="# Requirements")
        await insert_squad_members(db, GROUP_ID, ["member-1", "member-2", "member-3"])
        await gate1._ensure_gate_state(db, proposal_id)
        await db.execute(
            "UPDATE gates SET metadata = ? WHERE proposal_id = ? AND gate_type = ?",
            (json.dumps({"group_id": GROUP_ID}), proposal_id, GATE_TYPE),
        )
        await db.commit()

        for i in range(1, 4):
            event = make_event("/vote approve", author=f"member-{i}")
            await gate1._handle_vote(mock_bot, event, GROUP_ID, "approve", None)

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.BLUEPRINT.value

        cursor = await db.execute(
            "SELECT COUNT(*) FROM revisions WHERE proposal_id = ? AND rev_num = 2",
            (proposal_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == 1

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? AND event_type = ?",
            (proposal_id, "proposal_transition"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        payload = json.loads(row[1])
        assert payload["from_state"] == ProposalState.GATE_1.value
        assert payload["to_state"] == ProposalState.BLUEPRINT.value

        mock_bot.send_dm.assert_awaited_once()
        dm_call = mock_bot.send_dm.call_args
        assert dm_call.kwargs["recipient"] == "shipply-blueprint"
        payload = json.loads(dm_call.kwargs["content"])
        assert payload["shipply"] == "transition"
        assert payload["proposal_id"] == proposal_id
        assert payload["from"] == ProposalState.GATE_1.value
        assert payload["to"] == ProposalState.BLUEPRINT.value

    async def test_vote_approve_before_quorum_records_vote_no_transition(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        proposal_id = "prop-partial"
        await insert_proposal(db, proposal_id, ProposalState.GATE_1.value)
        await insert_squad_members(db, GROUP_ID, ["member-1", "member-2", "member-3"])
        await gate1._ensure_gate_state(db, proposal_id)
        await db.execute(
            "UPDATE gates SET metadata = ? WHERE proposal_id = ? AND gate_type = ?",
            (json.dumps({"group_id": GROUP_ID}), proposal_id, GATE_TYPE),
        )
        await db.commit()

        event = make_event("/vote approve", author="member-1")
        await gate1._handle_vote(mock_bot, event, GROUP_ID, "approve", None)

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.GATE_1.value

        cursor = await db.execute(
            "SELECT votes FROM gates WHERE proposal_id = ? AND gate_type = ?",
            (proposal_id, GATE_TYPE),
        )
        row = await cursor.fetchone()
        await cursor.close()
        votes = json.loads(row[0])
        assert votes["member-1"]["vote"] == "approve"

        mock_bot.send_dm.assert_not_called()


class TestHandleVoteReject:
    """Tests for rejection votes and transition back to INTAKE."""

    async def test_vote_reject_majority_reaches_quorum_returns_to_intake(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        proposal_id = "prop-reject"
        await insert_proposal(db, proposal_id, ProposalState.GATE_1.value)
        await insert_squad_members(db, GROUP_ID, ["member-1", "member-2", "member-3"])
        await gate1._ensure_gate_state(db, proposal_id)
        await db.execute(
            "UPDATE gates SET metadata = ? WHERE proposal_id = ? AND gate_type = ?",
            (json.dumps({"group_id": GROUP_ID}), proposal_id, GATE_TYPE),
        )
        await db.commit()

        for i in range(1, 4):
            event = make_event("/vote reject missing requirements", author=f"member-{i}")
            await gate1._handle_vote(mock_bot, event, GROUP_ID, "reject", "missing requirements")

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.INTAKE.value

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? AND event_type = ?",
            (proposal_id, "proposal_transition"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        payload = json.loads(row[1])
        assert payload["from_state"] == ProposalState.GATE_1.value
        assert payload["to_state"] == ProposalState.INTAKE.value
        assert payload["reason"] == "missing requirements"

        mock_bot.send_dm.assert_awaited_once()
        dm_call = mock_bot.send_dm.call_args
        assert dm_call.kwargs["recipient"] == "shipply-scout"
        payload = json.loads(dm_call.kwargs["content"])
        assert payload["to"] == ProposalState.INTAKE.value

        reply_text = mock_bot.reply.call_args[0][1]
        assert "returned to INTAKE" in reply_text
        assert "missing requirements" in reply_text


class TestVoteAuthorization:
    """Tests for squad membership and command validation."""

    async def test_non_member_vote_is_rejected_and_not_counted(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        proposal_id = "prop-non-member"
        await insert_proposal(db, proposal_id, ProposalState.GATE_1.value)
        await gate1._ensure_gate_state(db, proposal_id)
        await db.execute(
            "UPDATE gates SET metadata = ? WHERE proposal_id = ? AND gate_type = ?",
            (json.dumps({"group_id": GROUP_ID}), proposal_id, GATE_TYPE),
        )
        await db.commit()
        mock_bot.is_squad_member.return_value = False

        event = make_event("/vote approve", author="stranger")
        result = await gate1._handle_vote(mock_bot, event, GROUP_ID, "approve", None)

        assert result is not None
        reply_text = mock_bot.reply.call_args[0][1]
        assert "Only Squad members may vote" in reply_text

        cursor = await db.execute(
            "SELECT votes FROM gates WHERE proposal_id = ? AND gate_type = ?",
            (proposal_id, GATE_TYPE),
        )
        row = await cursor.fetchone()
        await cursor.close()
        votes = json.loads(row[0])
        assert "stranger" not in votes

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE event_type = ?",
            ("gate_vote_rejected",),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        assert len(rows) == 1
        reject_payload = json.loads(rows[0][1])
        assert reject_payload["reason"] == "not a squad member"
        assert reject_payload["author"] == "stranger"

    async def test_vote_with_unknown_group_returns_no_proposal_message(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        event = make_event("/vote approve", author="member-1")
        result = await gate1._handle_vote(mock_bot, event, GROUP_ID, "approve", None)

        reply_text = mock_bot.reply.call_args[0][1]
        assert "No proposal is currently under review" in reply_text


class TestEmojiVotes:
    """Tests for emoji voting via on_group_message."""

    async def test_thumbs_up_emoji_records_approve_vote(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        proposal_id = "prop-emoji"
        await insert_proposal(db, proposal_id, ProposalState.GATE_1.value)
        await insert_squad_members(db, GROUP_ID, ["member-1"])
        await gate1._ensure_gate_state(db, proposal_id)
        await db.execute(
            "UPDATE gates SET metadata = ? WHERE proposal_id = ? AND gate_type = ?",
            (json.dumps({"group_id": GROUP_ID}), proposal_id, GATE_TYPE),
        )
        await db.commit()

        event = make_event("👍", author="member-1")
        await gate1.on_group_message(event, mock_bot)

        cursor = await db.execute(
            "SELECT votes FROM gates WHERE proposal_id = ? AND gate_type = ?",
            (proposal_id, GATE_TYPE),
        )
        row = await cursor.fetchone()
        await cursor.close()
        votes = json.loads(row[0])
        assert votes["member-1"]["vote"] == "approve"

    async def test_thumbs_down_emoji_records_reject_vote(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        proposal_id = "prop-emoji-down"
        await insert_proposal(db, proposal_id, ProposalState.GATE_1.value)
        await insert_squad_members(db, GROUP_ID, ["member-1"])
        await gate1._ensure_gate_state(db, proposal_id)
        await db.execute(
            "UPDATE gates SET metadata = ? WHERE proposal_id = ? AND gate_type = ?",
            (json.dumps({"group_id": GROUP_ID}), proposal_id, GATE_TYPE),
        )
        await db.commit()

        event = make_event("👎", author="member-1")
        await gate1.on_group_message(event, mock_bot)

        cursor = await db.execute(
            "SELECT votes FROM gates WHERE proposal_id = ? AND gate_type = ?",
            (proposal_id, GATE_TYPE),
        )
        row = await cursor.fetchone()
        await cursor.close()
        votes = json.loads(row[0])
        assert votes["member-1"]["vote"] == "reject"


class TestDependencyCard:
    """Tests for the Dependency Card builder."""

    async def test_dependency_card_lists_active_proposals_without_leaking_titles(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        monkeypatch,
    ) -> None:
        proposal_id = "prop-deps"
        await insert_proposal(db, proposal_id, ProposalState.GATE_1.value)
        requirements = (
            "# Requirements\n\n"
            "## Dependencies\n"
            "```json\n"
            '[{"target_area": "search", "affected_component": "indexer"}]\n'
            "```"
        )
        await insert_revision(db, proposal_id, requirements_doc=requirements)

        other_id = "prop-other"
        await insert_proposal(db, other_id, ProposalState.INTAKE.value, title="Secret Title", sponsor="Secret Sponsor")

        monkeypatch.setattr(
            gate1,
            "_bd_list",
            AsyncMock(return_value=[]),
        )

        card = await gate1.dependency_card(proposal_id)

        assert "error" not in card
        assert len(card["active_proposals"]) == 1
        active = card["active_proposals"][0]
        assert active["id"] == other_id
        assert active["state"] == ProposalState.INTAKE.value
        assert "created_at" in active
        assert "title" not in active
        assert "sponsor" not in active

    async def test_dependency_card_returns_open_and_closed_beads(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        monkeypatch,
    ) -> None:
        proposal_id = "prop-beads"
        await insert_proposal(db, proposal_id, ProposalState.GATE_1.value)
        requirements = (
            "# Requirements\n\n"
            "<!-- shipply-deps: [{\"bead_id\": \"bead-1\"}, {\"bead_id\": \"bead-2\"}] -->"
        )
        await insert_revision(db, proposal_id, requirements_doc=requirements)

        monkeypatch.setattr(
            gate1,
            "_bd_list",
            AsyncMock(
                return_value=[
                    {
                        "id": "bead-1",
                        "status": "open",
                        "molecule_id": "mol-1",
                        "area": "search",
                    },
                    {
                        "id": "bead-2",
                        "status": "closed",
                        "molecule_id": "mol-1",
                        "component": "indexer",
                    },
                ]
            ),
        )

        card = await gate1.dependency_card(proposal_id)

        assert len(card["open_beads"]) == 1
        assert card["open_beads"][0]["id"] == "bead-1"
        assert card["open_beads"][0]["status"] == "open"
        assert card["open_beads"][0]["molecule_id"] == "mol-1"
        assert "title" not in card["open_beads"][0]
        assert "owner" not in card["open_beads"][0]

        assert len(card["closed_beads"]) == 1
        assert card["closed_beads"][0]["id"] == "bead-2"
        assert card["closed_beads"][0]["status"] == "closed"

    async def test_dependency_card_returns_error_for_missing_proposal(
        self,
        patch_config: None,
    ) -> None:
        card = await gate1.dependency_card("missing-proposal")
        assert card.get("error") == "proposal not found"

    async def test_dependency_card_filters_by_declared_area_and_component(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        monkeypatch,
    ) -> None:
        proposal_id = "prop-filter"
        await insert_proposal(db, proposal_id, ProposalState.GATE_1.value)
        requirements = (
            "# Requirements\n\n"
            "## Dependencies\n"
            "```json\n"
            '[{"target_area": "search"}]\n'
            "```"
        )
        await insert_revision(db, proposal_id, requirements_doc=requirements)

        monkeypatch.setattr(
            gate1,
            "_bd_list",
            AsyncMock(
                return_value=[
                    {"id": "match-1", "status": "open", "area": "search"},
                    {"id": "match-2", "status": "open", "target_area": "search"},
                    {"id": "no-match", "status": "open", "area": "other"},
                ]
            ),
        )

        card = await gate1.dependency_card(proposal_id)

        ids = {b["id"] for b in card["open_beads"]}
        assert ids == {"match-1", "match-2"}


class TestOnDm:
    """Tests for DM transition handling."""

    async def test_on_dm_transition_to_gate_1_triggers_handle_entry(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        proposal_id = "prop-dm-entry"
        await insert_proposal(db, proposal_id, ProposalState.GATE_1.value)
        await insert_revision(db, proposal_id, requirements_doc="# Requirements")

        content = json.dumps(
            {
                "shipply": "transition",
                "proposal_id": proposal_id,
                "from": ProposalState.DOC_REVIEW.value,
                "to": ProposalState.GATE_1.value,
            }
        )
        event = make_event(content, chat_id=None, event_type="dm_received")

        await gate1.on_dm(event, mock_bot)

        mock_bot.send_group_message.assert_called()
