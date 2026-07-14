"""Tests for the Scout (U3) intake handler."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
from pacto_bot_sdk._generated.models import AgentEventParams

from shipply.protocols.acp import HarnessResult
from shipply.config import ShipplyConfig
from shipply.db import close_db, get_db
import shipply.handlers.scout as scout

pytestmark = pytest.mark.asyncio

CHAT_ID = "scout-test-chat"
AUTHOR = "alice"


def make_event(
    content: str,
    *,
    author: str = AUTHOR,
    chat_id: str = CHAT_ID,
    event_type: str = "dm_received",
) -> AgentEventParams:
    """Build a pacto-bot-api event for Scout tests."""
    event_id = f"evt-{abs(hash((author, chat_id, content)))}"
    return AgentEventParams(
        author=author,
        bot_id="shipply-scout",
        chat_id=chat_id,
        content=content,
        event_id=event_id,
        rumor_id="rum-1",
        timestamp=1234567890,
        type=event_type,
    )


@pytest.fixture
def patch_config(test_config: ShipplyConfig, monkeypatch) -> None:
    """Patch the handler's internal config helper to use the test config."""
    monkeypatch.setattr(scout, "_config", lambda: test_config)


@pytest.fixture
def mock_bot(monkeypatch) -> MagicMock:
    """Return a mocked pacto_bot_sdk.Bot with the methods Scout uses."""
    bot = MagicMock()
    bot.own_pubkey = "shipply-scout-own"
    bot.send_dm = AsyncMock()
    bot.send_group_message = AsyncMock()
    bot.reply = MagicMock(return_value={"type": "reply"})
    monkeypatch.setattr(scout, "bot", bot)
    return bot


@pytest.fixture
def mock_pool(monkeypatch) -> MagicMock:
    """Return a mocked harness pool and patch Scout to use it."""
    pool = MagicMock()
    backend = MagicMock()
    backend.send = AsyncMock(
        return_value=HarnessResult(status="needs_input", payload="Tell me more.")
    )
    pool.get = AsyncMock(return_value=backend)
    monkeypatch.setattr(scout, "_pool_for", lambda _config: pool)
    return backend


async def test_first_dm_creates_proposal_and_history(
    patch_config: None,
    mock_bot: MagicMock,
    mock_pool: MagicMock,
    test_config: ShipplyConfig,
) -> None:
    """The first DM from a user creates an INTAKE proposal and a history row."""
    mock_pool.send.return_value = HarnessResult(
        status="needs_input", payload="Can you elaborate?"
    )
    event = make_event("Build a thing that does stuff")

    await scout.on_dm(event, mock_bot)

    db = await get_db(test_config.database.path)
    try:
        proposal = await scout._find_proposal_by_thread(db, "dm", CHAT_ID)
        assert proposal is not None
        assert proposal["state"] == "INTAKE"
        assert proposal["sponsor"] == AUTHOR

        history = await scout._load_history(db, proposal["id"])
        assert len(history) == 1
        assert history[0]["author"] == AUTHOR
        assert history[0]["content"] == "Build a thing that does stuff"
    finally:
        await close_db(db)


async def test_subsequent_dm_appends_history_and_brainstorms(
    patch_config: None,
    mock_bot: MagicMock,
    mock_pool: MagicMock,
    test_config: ShipplyConfig,
) -> None:
    """Follow-up DMs append to history and are sent to the harness as brainstorm."""
    mock_pool.send.return_value = HarnessResult(
        status="needs_input", payload="Tell me more."
    )
    event1 = make_event("Build a thing")
    event2 = make_event("It should be fast")

    await scout.on_dm(event1, mock_bot)
    await scout.on_dm(event2, mock_bot)

    db = await get_db(test_config.database.path)
    try:
        proposal = await scout._find_proposal_by_thread(db, "dm", CHAT_ID)
        history = await scout._load_history(db, proposal["id"])
        assert len(history) == 2
    finally:
        await close_db(db)

    assert mock_pool.send.call_count == 2
    second_call = mock_pool.send.call_args_list[1]
    assert second_call.kwargs["task"] == "brainstorm"
    context = second_call.kwargs["context"]
    assert len(context["messages"]) == 2
    assert context["messages"][0]["content"] == "Build a thing"
    assert context["messages"][1]["content"] == "It should be fast"
    assert set(context["contributors"]) == {AUTHOR}
    assert context["title"] == "Build a thing"
    assert context["done"] is False


async def test_completed_requirements_doc_writes_revision_and_notifies(
    patch_config: None,
    mock_bot: MagicMock,
    mock_pool: MagicMock,
    test_config: ShipplyConfig,
) -> None:
    """A completed harness response produces rev1, DOC_REVIEW, and a hand-off DM."""
    requirements_doc = "# Requirements\n\nBuild a fast thing."
    mock_pool.send.return_value = HarnessResult(
        status="success",
        payload={"requirements_doc": requirements_doc, "title": "Build a Fast Thing"},
    )
    event = make_event("Build a fast thing")

    await scout.on_dm(event, mock_bot)

    db = await get_db(test_config.database.path)
    try:
        proposal = await scout._find_proposal_by_thread(db, "dm", CHAT_ID)
        assert proposal is not None
        assert proposal["state"] == "DOC_REVIEW"
        assert proposal["title"] == "Build a Fast Thing"

        cursor = await db.execute(
            "SELECT rev_num, requirements_doc FROM revisions WHERE proposal_id = ?",
            (proposal["id"],),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        assert row[0] == 1
        assert row[1] == requirements_doc

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? ORDER BY id",
            (proposal["id"],),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        transitions = [
            json.loads(r[1]) for r in rows if r[0] == "proposal_transition"
        ]
        assert any(t["to_state"] == "DOC_REVIEW" for t in transitions)
    finally:
        await close_db(db)

    mock_bot.send_dm.assert_called_once()
    notify = json.loads(mock_bot.send_dm.call_args.kwargs["content"])
    assert notify == {
        "shipply": "transition",
        "proposal_id": proposal["id"],
        "from": "INTAKE",
        "to": "DOC_REVIEW",
    }
    mock_bot.reply.assert_called_once()
    assert "moving to Doc Review" in mock_bot.reply.call_args[0][1]


async def test_done_early_termination_writes_warning_revision(
    patch_config: None,
    mock_bot: MagicMock,
    mock_pool: MagicMock,
    test_config: ShipplyConfig,
) -> None:
    """"/done" terminates the interview early and writes a warning revision."""
    mock_pool.send.return_value = HarnessResult(
        status="success", payload=None
    )
    event = make_event("/done")

    await scout.on_dm(event, mock_bot)

    db = await get_db(test_config.database.path)
    try:
        proposal = await scout._find_proposal_by_thread(db, "dm", CHAT_ID)
        assert proposal is not None
        assert proposal["state"] == "DOC_REVIEW"

        cursor = await db.execute(
            "SELECT requirements_doc FROM revisions WHERE proposal_id = ?",
            (proposal["id"],),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        assert "Warning" in row[0]
        assert "User terminated the interview early" in row[0]
    finally:
        await close_db(db)

    mock_bot.send_dm.assert_called_once()
    notify = json.loads(mock_bot.send_dm.call_args.kwargs["content"])
    assert notify["to"] == "DOC_REVIEW"

    mock_bot.reply.assert_called_once()
    assert "ended early" in mock_bot.reply.call_args[0][1]


async def test_group_thread_tracks_multiple_contributors(
    patch_config: None,
    mock_bot: MagicMock,
    mock_pool: MagicMock,
    test_config: ShipplyConfig,
) -> None:
    """Multiple users in a group thread are tracked as contributors."""
    group_id = "scout-group-1"
    event1 = make_event(
        "Idea A",
        author="alice",
        chat_id=group_id,
        event_type="mls_group_message_received",
    )
    event2 = make_event(
        "Idea B",
        author="bob",
        chat_id=group_id,
        event_type="mls_group_message_received",
    )
    mock_bot.own_pubkey = "shipply-scout"  # ensure Scout does not skip its own messages

    await scout.on_group_message(event1, mock_bot)
    await scout.on_group_message(event2, mock_bot)

    db = await get_db(test_config.database.path)
    try:
        proposal = await scout._find_proposal_by_thread(db, "group", group_id)
        assert proposal is not None
        contributors = await scout._load_contributors(db, proposal["id"])
        assert set(contributors) == {"alice", "bob"}
    finally:
        await close_db(db)

    assert mock_pool.send.call_count == 2
    context = mock_pool.send.call_args_list[1].kwargs["context"]
    assert set(context["contributors"]) == {"alice", "bob"}
    assert len(context["messages"]) == 2
