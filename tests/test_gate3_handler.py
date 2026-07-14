"""Tests for the Gate 3 (PR Review) bot handler."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
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
import shipply.handlers.gate3 as gate3


pytestmark = pytest.mark.asyncio

GATE_3_GROUP_ID = "test-gate-3-group"
PR_URL = "https://github.com/owner/repo/pull/1"


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
            "gate-1": GateConfig(quorum=3, threshold=0.66),
            "gate-2": GateConfig(quorum=2, threshold=0.75),
            "gate-3": GateConfig(quorum=2, threshold=0.75),
        },
        database=DatabaseConfig(path=str(tmp_path / "test.db")),
        squads=SquadsConfig(gate_3=GATE_3_GROUP_ID),
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
    monkeypatch.setattr(gate3, "_config", lambda: test_config)


@pytest.fixture
def mock_bot(monkeypatch) -> MagicMock:
    """Return a mocked pacto_bot_sdk.Bot with the methods Gate 3 uses."""
    bot = MagicMock()
    bot.send_group_message = AsyncMock(return_value="msg-123")
    bot.send_dm = AsyncMock()
    bot.is_squad_member = AsyncMock(return_value=True)
    bot.reply = MagicMock(return_value={"type": "reply"})
    bot.log = MagicMock()
    monkeypatch.setattr(gate3, "bot", bot)
    return bot


@pytest.fixture
def mock_subprocess(monkeypatch):
    """Return a helper that patches asyncio.create_subprocess_exec for gh pr view."""

    def _patch(pr_info: dict[str, Any] | None = None, returncode: int = 0, stderr: bytes = b""):
        calls: list[list[str]] = []

        async def fake_create_subprocess_exec(*cmd, stdout=None, stderr=None):
            calls.append(list(cmd))
            proc = AsyncMock()
            proc.returncode = returncode
            stdout_bytes = json.dumps(pr_info).encode() if pr_info is not None else b""
            proc.communicate = AsyncMock(return_value=(stdout_bytes, stderr))
            return proc

        monkeypatch.setattr(gate3.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
        return calls

    return _patch


def make_event(
    content: str,
    author: str = "maintainer-1",
    chat_id: str | None = GATE_3_GROUP_ID,
    event_type: str = "mls_group_message_received",
) -> AgentEventParams:
    """Build a pacto-bot-api event for tests."""
    return AgentEventParams(
        author=author,
        bot_id="shipply-gate-3",
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
    state: str = ProposalState.GATE_3.value,
    title: str = "Test Proposal",
    sponsor: str = "sponsor-1",
) -> None:
    await db.execute(
        "INSERT INTO proposals (id, state, title, sponsor) VALUES (?, ?, ?, ?)",
        (proposal_id, state, title, sponsor),
    )
    await db.commit()


async def insert_molecule(
    db: aiosqlite.Connection,
    proposal_id: str,
    pr_url: str | None = PR_URL,
    molecule_id: str = "mol-001",
) -> None:
    await db.execute(
        "INSERT INTO molecules (proposal_id, molecule_id, pr_url) VALUES (?, ?, ?)",
        (proposal_id, molecule_id, pr_url),
    )
    await db.commit()


async def insert_gate(
    db: aiosqlite.Connection,
    proposal_id: str,
    group_id: str = GATE_3_GROUP_ID,
    metadata: dict[str, Any] | None = None,
) -> str:
    gate_id = str(uuid.uuid4())
    meta = metadata or {"group_id": group_id}
    await db.execute(
        "INSERT INTO gates (id, proposal_id, gate_type, status, votes, metadata) VALUES (?, ?, ?, ?, ?, ?)",
        (gate_id, proposal_id, "gate-3", "open", "{}", json.dumps(meta)),
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
    """Tests for entering GATE_3 and posting the PR URL."""

    async def test_gate_3_dm_posts_pr_url_to_squad_with_check_instructions(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        """A GATE_3 transition DM should post the PR URL and /gate3 check instructions."""
        proposal_id = "prop-gate3-01"
        title = "PR Review Proposal"
        await insert_proposal(db, proposal_id, title=title)
        await insert_molecule(db, proposal_id, pr_url=PR_URL)

        content = json.dumps(
            {
                "shipply": "transition",
                "proposal_id": proposal_id,
                "from": ProposalState.FORGE.value,
                "to": ProposalState.GATE_3.value,
            }
        )
        event = make_event(content, chat_id=None, event_type="dm_received")
        await gate3.on_dm(event, mock_bot)

        mock_bot.send_group_message.assert_awaited_once()
        call = mock_bot.send_group_message.call_args
        assert call.args[0] == GATE_3_GROUP_ID
        body = call.args[1]
        assert title in body
        assert PR_URL in body
        assert "`/gate3 check`" in body

        cursor = await db.execute(
            "SELECT metadata FROM gates WHERE proposal_id = ? AND gate_type = ?",
            (proposal_id, "gate-3"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        metadata = json.loads(row[0])
        assert metadata["group_id"] == GATE_3_GROUP_ID
        assert metadata["entry_message_id"] == "msg-123"

    async def test_gate_3_entry_ignores_proposal_in_wrong_state(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
    ) -> None:
        """A proposal not in GATE_3 should not be announced."""
        proposal_id = "prop-gate3-wrong-state"
        await insert_proposal(db, proposal_id, state=ProposalState.FORGE.value)
        await insert_molecule(db, proposal_id, pr_url=PR_URL)

        content = json.dumps(
            {
                "shipply": "transition",
                "proposal_id": proposal_id,
                "from": ProposalState.FORGE.value,
                "to": ProposalState.GATE_3.value,
            }
        )
        event = make_event(content, chat_id=None, event_type="dm_received")
        result = await gate3.on_dm(event, mock_bot)

        assert result is None
        mock_bot.send_group_message.assert_not_called()
        mock_bot.log.assert_called_once()
        assert "not GATE_3" in mock_bot.log.call_args[0][0]

    async def test_gate_3_entry_warns_when_no_squad_configured(
        self,
        tmp_path: Path,
        db: aiosqlite.Connection,
        monkeypatch,
        mock_bot: MagicMock,
    ) -> None:
        """If no gate-3 squad is configured, the handler should warn and exit."""
        proposal_id = "prop-gate3-no-squad"
        cfg = make_config(tmp_path)
        cfg.squads.gate_3 = None
        monkeypatch.setattr(gate3, "_config", lambda: cfg)
        await insert_proposal(db, proposal_id)
        await insert_molecule(db, proposal_id, pr_url=PR_URL)

        content = json.dumps(
            {
                "shipply": "transition",
                "proposal_id": proposal_id,
                "from": ProposalState.FORGE.value,
                "to": ProposalState.GATE_3.value,
            }
        )
        event = make_event(content, chat_id=None, event_type="dm_received")
        result = await gate3.on_dm(event, mock_bot)

        assert result is None
        mock_bot.send_group_message.assert_not_called()
        mock_bot.log.assert_called_once()
        assert "no squad configured" in mock_bot.log.call_args[0][0]


class TestHandleCheck:
    """Tests for the /gate3 check command."""

    async def test_check_merged_transitions_to_closed_and_posts_status(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
        mock_subprocess,
    ) -> None:
        """/gate3 check on a merged PR should close the proposal and post the final status."""
        proposal_id = "prop-gate3-merged"
        title = "Proposal to Merge"
        pr_title = "The PR Title"
        await insert_proposal(db, proposal_id, title=title)
        await insert_molecule(db, proposal_id, pr_url=PR_URL)
        await insert_gate(db, proposal_id)
        await insert_squad_members(db, GATE_3_GROUP_ID, ["maintainer-1"])

        pr_info = {
            "state": "MERGED",
            "url": PR_URL,
            "title": pr_title,
            "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN",
        }
        subprocess_calls = mock_subprocess(pr_info=pr_info)

        event = make_event("/gate3 check", author="maintainer-1")
        await gate3.on_group_message(event, mock_bot)

        assert len(subprocess_calls) == 1
        assert "gh" in subprocess_calls[0]
        assert "pr" in subprocess_calls[0]
        assert PR_URL in subprocess_calls[0]

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.CLOSED.value

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? AND event_type = ?",
            (proposal_id, "proposal_transition"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        payload = json.loads(row[1])
        assert payload["from_state"] == ProposalState.GATE_3.value
        assert payload["to_state"] == ProposalState.CLOSED.value
        assert payload["reason"] == "PR merged"

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? AND event_type = ?",
            (proposal_id, "pr_merged"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None

        mock_bot.send_group_message.assert_awaited_once()
        call = mock_bot.send_group_message.call_args
        assert call.args[0] == GATE_3_GROUP_ID
        body = call.args[1]
        assert "CLOSED" in body
        assert title in body
        assert proposal_id in body
        assert pr_title in body
        assert PR_URL in body

    async def test_check_changes_requested_transitions_to_forge_and_notifies_forge(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
        mock_subprocess,
    ) -> None:
        """/gate3 check with CHANGES_REQUESTED should return the proposal to FORGE."""
        proposal_id = "prop-gate3-changes"
        title = "Proposal Needs Rework"
        pr_title = "The PR Title"
        await insert_proposal(db, proposal_id, title=title)
        await insert_molecule(db, proposal_id, pr_url=PR_URL)
        await insert_gate(db, proposal_id)
        await insert_squad_members(db, GATE_3_GROUP_ID, ["maintainer-1"])

        pr_info = {
            "state": "OPEN",
            "url": PR_URL,
            "title": pr_title,
            "reviewDecision": "CHANGES_REQUESTED",
            "mergeStateStatus": "BLOCKED",
        }
        subprocess_calls = mock_subprocess(pr_info=pr_info)

        event = make_event("/gate3 check", author="maintainer-1")
        await gate3.on_group_message(event, mock_bot)

        assert len(subprocess_calls) == 1

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.FORGE.value

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? AND event_type = ?",
            (proposal_id, "proposal_transition"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        payload = json.loads(row[1])
        assert payload["from_state"] == ProposalState.GATE_3.value
        assert payload["to_state"] == ProposalState.FORGE.value
        assert payload["reason"] == "PR changes requested"

        mock_bot.send_group_message.assert_awaited_once()
        call = mock_bot.send_group_message.call_args
        assert call.args[0] == GATE_3_GROUP_ID
        body = call.args[1]
        assert title in body
        assert proposal_id in body
        assert "CHANGES_REQUESTED" in body
        assert "FORGE" in body or "Returning proposal to FORGE" in body

        mock_bot.send_dm.assert_awaited_once()
        dm_call = mock_bot.send_dm.call_args
        assert dm_call.kwargs["recipient"] == "shipply-forge"
        dm_payload = json.loads(dm_call.kwargs["content"])
        assert dm_payload["shipply"] == "transition"
        assert dm_payload["proposal_id"] == proposal_id
        assert dm_payload["from"] == ProposalState.GATE_3.value
        assert dm_payload["to"] == ProposalState.FORGE.value

    async def test_check_open_posts_status_without_transitioning(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
        mock_subprocess,
    ) -> None:
        """/gate3 check on an open PR should post status without changing state."""
        proposal_id = "prop-gate3-open"
        title = "Proposal Under Review"
        pr_title = "The PR Title"
        await insert_proposal(db, proposal_id, title=title)
        await insert_molecule(db, proposal_id, pr_url=PR_URL)
        await insert_gate(db, proposal_id)
        await insert_squad_members(db, GATE_3_GROUP_ID, ["maintainer-1"])

        pr_info = {
            "state": "OPEN",
            "url": PR_URL,
            "title": pr_title,
            "reviewDecision": "APPROVED",
            "mergeStateStatus": "BLOCKED",
        }
        subprocess_calls = mock_subprocess(pr_info=pr_info)

        event = make_event("/gate3 check", author="maintainer-1")
        await gate3.on_group_message(event, mock_bot)

        assert len(subprocess_calls) == 1

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.GATE_3.value

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? AND event_type = ?",
            (proposal_id, "pr_status_check"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None

        cursor = await db.execute(
            "SELECT event_type FROM events WHERE proposal_id = ? AND event_type = ?",
            (proposal_id, "proposal_transition"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is None

        mock_bot.send_group_message.assert_awaited_once()
        call = mock_bot.send_group_message.call_args
        assert call.args[0] == GATE_3_GROUP_ID
        body = call.args[1]
        assert title in body
        assert proposal_id in body
        assert "No transition needed" in body

        mock_bot.send_dm.assert_not_called()

    async def test_check_invalid_pr_url_is_rejected_and_not_executed(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
        mock_subprocess,
    ) -> None:
        """An invalid PR URL should fail the check without invoking gh."""
        proposal_id = "prop-gate3-invalid-url"
        invalid_url = "https://evil.com/owner/repo/pull/1"
        await insert_proposal(db, proposal_id)
        await insert_molecule(db, proposal_id, pr_url=invalid_url)
        await insert_gate(db, proposal_id)
        await insert_squad_members(db, GATE_3_GROUP_ID, ["maintainer-1"])

        subprocess_calls = mock_subprocess(pr_info=None)

        event = make_event("/gate3 check", author="maintainer-1")
        result = await gate3.on_group_message(event, mock_bot)

        assert result is not None
        assert subprocess_calls == []
        mock_bot.send_group_message.assert_not_called()
        reply_text = mock_bot.reply.call_args[0][1]
        assert "Failed to query PR status" in reply_text
        assert invalid_url in reply_text

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.GATE_3.value

    async def test_check_rejected_for_non_member(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
        mock_subprocess,
    ) -> None:
        """Only Squad members may run /gate3 check."""
        proposal_id = "prop-gate3-non-member"
        await insert_proposal(db, proposal_id)
        await insert_molecule(db, proposal_id, pr_url=PR_URL)
        await insert_gate(db, proposal_id)
        mock_bot.is_squad_member.return_value = False

        subprocess_calls = mock_subprocess(pr_info=None)

        event = make_event("/gate3 check", author="stranger")
        result = await gate3.on_group_message(event, mock_bot)

        assert result is not None
        reply_text = mock_bot.reply.call_args[0][1]
        assert "Only Squad members may run `/gate3 check`" in reply_text
        assert subprocess_calls == []
        mock_bot.send_group_message.assert_not_called()

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.GATE_3.value

    async def test_check_no_proposal_under_review(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        mock_bot: MagicMock,
        mock_subprocess,
    ) -> None:
        """/gate3 check with no active proposal in the Squad should reply with an error."""
        proposal_id = "prop-gate3-no-gate"
        await insert_proposal(db, proposal_id)
        await insert_molecule(db, proposal_id, pr_url=PR_URL)
        # No gate row with this group_id in metadata

        subprocess_calls = mock_subprocess(pr_info=None)

        event = make_event("/gate3 check", author="maintainer-1")
        result = await gate3.on_group_message(event, mock_bot)

        assert result is not None
        reply_text = mock_bot.reply.call_args[0][1]
        assert "No proposal is currently under PR review" in reply_text
        assert subprocess_calls == []
        mock_bot.send_group_message.assert_not_called()
