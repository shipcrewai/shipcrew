"""Tests for the Doc Review (U3) handler."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
from pacto_bot_sdk._generated.models import AgentEventParams

from shipply.protocols.acp import HarnessResult
from shipply.config import ShipplyConfig
from shipply.db import close_db, get_db
import shipply.handlers.doc_review as doc_review

pytestmark = pytest.mark.asyncio

DM_CHAT_ID = "doc-review-dm"


def make_event(
    content: str,
    *,
    author: str = "shipply-scout",
    chat_id: str = DM_CHAT_ID,
    event_type: str = "dm_received",
) -> AgentEventParams:
    """Build a pacto-bot-api DM event for Doc Review tests."""
    return AgentEventParams(
        author=author,
        bot_id="shipply-doc-review",
        chat_id=chat_id,
        content=content,
        event_id="evt-1",
        rumor_id="rum-1",
        timestamp=1234567890,
        type=event_type,
    )


@pytest.fixture
def patch_config(test_config: ShipplyConfig, monkeypatch) -> None:
    """Patch the handler's internal config helper to use the test config."""
    monkeypatch.setattr(doc_review, "_config", lambda: test_config)


@pytest.fixture
def mock_bot(monkeypatch) -> MagicMock:
    """Return a mocked pacto_bot_sdk.Bot with the methods Doc Review uses."""
    bot = MagicMock()
    bot.own_pubkey = "shipply-doc-review-own"
    bot.send_dm = AsyncMock()
    bot.send_group_message = AsyncMock()
    bot.reply = MagicMock(return_value={"type": "reply"})
    monkeypatch.setattr(doc_review, "bot", bot)
    return bot


@pytest.fixture
def mock_pool(monkeypatch) -> MagicMock:
    """Return a mocked harness backend and patch Doc Review to use it."""
    pool = MagicMock()
    backend = MagicMock()
    backend.send = AsyncMock(return_value=HarnessResult(status="success", payload=[]))
    pool.get = AsyncMock(return_value=backend)
    monkeypatch.setattr(doc_review, "_pool_for", lambda _config: pool)
    return backend


async def insert_doc_review_proposal(
    db: aiosqlite.Connection,
    proposal_id: str,
    requirements_doc: str,
    frozen_at: str = "1970-01-01 00:00:00",
) -> str:
    """Create a proposal in DOC_REVIEW and a rev1 with a known frozen_at."""
    await db.execute(
        """
        INSERT INTO proposals (id, state, title, sponsor)
        VALUES (?, 'DOC_REVIEW', ?, ?)
        """,
        (proposal_id, "Test Proposal", "sponsor-1"),
    )
    rev_id = f"rev-{proposal_id}"
    await db.execute(
        """
        INSERT INTO revisions (id, proposal_id, rev_num, requirements_doc, frozen_at)
        VALUES (?, ?, 1, ?, ?)
        """,
        (rev_id, proposal_id, requirements_doc, frozen_at),
    )
    await db.execute(
        """
        INSERT INTO interview_history
        (message_id, proposal_id, source, chat_id, author, content)
        VALUES (?, ?, 'dm', ?, ?, ?)
        """,
        ("msg-1", proposal_id, "scout-thread-1", "sponsor-1", "initial spark"),
    )
    await db.commit()
    return rev_id


def make_transition_content(proposal_id: str) -> str:
    return json.dumps(
        {
            "shipply": "transition",
            "proposal_id": proposal_id,
            "from": "INTAKE",
            "to": "DOC_REVIEW",
        }
    )


async def test_transition_triggers_doc_review_harness(
    patch_config: None,
    mock_bot: MagicMock,
    mock_pool: MagicMock,
    test_config: ShipplyConfig,
) -> None:
    """A DOC_REVIEW transition DM triggers the doc-review harness task."""
    db = await get_db(test_config.database.path)
    try:
        await insert_doc_review_proposal(db, "prop-dr-1", "# Requirements\n\nOriginal")
    finally:
        await close_db(db)

    event = make_event(make_transition_content("prop-dr-1"))
    await doc_review.on_dm(event, mock_bot)

    assert mock_pool.send.call_count == 1
    call = mock_pool.send.call_args
    assert call.kwargs["task"] == "doc-review"
    assert call.kwargs["context"]["requirements_doc"] == "# Requirements\n\nOriginal"


async def test_safe_auto_fixes_and_advances_to_gate_1(
    patch_config: None,
    mock_bot: MagicMock,
    mock_pool: MagicMock,
    test_config: ShipplyConfig,
) -> None:
    """safe_auto findings are applied and the proposal advances to GATE_1."""
    db = await get_db(test_config.database.path)
    try:
        rev_id = await insert_doc_review_proposal(
            db, "prop-dr-2", "The system must be fast."
        )
    finally:
        await close_db(db)

    mock_pool.send.return_value = HarnessResult(
        status="success",
        payload=[
            {
                "category": "safe_auto",
                "severity": "P2",
                "description": "Add scalability",
                "replacement": {"old": "fast", "new": "fast and scalable"},
            }
        ],
    )
    event = make_event(make_transition_content("prop-dr-2"))
    await doc_review.on_dm(event, mock_bot)

    db = await get_db(test_config.database.path)
    try:
        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", ("prop-dr-2",)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == "GATE_1"

        cursor = await db.execute(
            "SELECT requirements_doc, frozen_at FROM revisions WHERE id = ?",
            (rev_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert "fast and scalable" in row[0]
        assert row[1] != "1970-01-01 00:00:00"

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? ORDER BY id",
            ("prop-dr-2",),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        transitions = [
            json.loads(r[1]) for r in rows if r[0] == "proposal_transition"
        ]
        assert any(t["to_state"] == "GATE_1" for t in transitions)
    finally:
        await close_db(db)

    # Last notification is the hand-off to Gate 1.
    notify = json.loads(mock_bot.send_dm.call_args_list[-1].kwargs["content"])
    assert notify == {
        "shipply": "transition",
        "proposal_id": "prop-dr-2",
        "from": "DOC_REVIEW",
        "to": "GATE_1",
    }


async def test_blocking_findings_bounce_to_intake(
    patch_config: None,
    mock_bot: MagicMock,
    mock_pool: MagicMock,
    test_config: ShipplyConfig,
) -> None:
    """Blocking P0/P1 findings bounce the proposal back to Scout with a gap report."""
    db = await get_db(test_config.database.path)
    try:
        await insert_doc_review_proposal(db, "prop-dr-3", "The system must be fast.")
    finally:
        await close_db(db)

    mock_pool.send.return_value = HarnessResult(
        status="success",
        payload=[
            {
                "category": "gated_auto",
                "severity": "P0",
                "description": "Missing user stories",
            }
        ],
    )
    event = make_event(make_transition_content("prop-dr-3"))
    await doc_review.on_dm(event, mock_bot)

    db = await get_db(test_config.database.path)
    try:
        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", ("prop-dr-3",)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == "INTAKE"

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? ORDER BY id",
            ("prop-dr-3",),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        transitions = [
            json.loads(r[1]) for r in rows if r[0] == "proposal_transition"
        ]
        assert any(t["to_state"] == "INTAKE" for t in transitions)
    finally:
        await close_db(db)

    # The gap report is sent to the originating thread, then Scout is notified.
    thread_call = mock_bot.send_dm.call_args_list[-2]
    assert "Blocking gaps found" in thread_call.kwargs["content"]
    assert "Missing user stories" in thread_call.kwargs["content"]

    notify = json.loads(mock_bot.send_dm.call_args_list[-1].kwargs["content"])
    assert notify == {
        "shipply": "transition",
        "proposal_id": "prop-dr-3",
        "from": "DOC_REVIEW",
        "to": "INTAKE",
    }


async def test_harness_error_bounces_to_intake(
    patch_config: None,
    mock_bot: MagicMock,
    mock_pool: MagicMock,
    test_config: ShipplyConfig,
) -> None:
    """A harness error during doc review bounces the proposal back to Scout."""
    db = await get_db(test_config.database.path)
    try:
        await insert_doc_review_proposal(db, "prop-dr-4", "# Requirements\n\n")
    finally:
        await close_db(db)

    mock_pool.send.return_value = HarnessResult(
        status="error", payload="model invocation failed"
    )
    event = make_event(make_transition_content("prop-dr-4"))
    await doc_review.on_dm(event, mock_bot)

    db = await get_db(test_config.database.path)
    try:
        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", ("prop-dr-4",)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == "INTAKE"

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? ORDER BY id",
            ("prop-dr-4",),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        transitions = [
            json.loads(r[1]) for r in rows if r[0] == "proposal_transition"
        ]
        assert any(t["to_state"] == "INTAKE" for t in transitions)
    finally:
        await close_db(db)

    # The error is reported to the thread, then Scout is notified.
    thread_call = mock_bot.send_dm.call_args_list[-2]
    assert "Doc review harness error" in thread_call.kwargs["content"]

    notify = json.loads(mock_bot.send_dm.call_args_list[-1].kwargs["content"])
    assert notify == {
        "shipply": "transition",
        "proposal_id": "prop-dr-4",
        "from": "DOC_REVIEW",
        "to": "INTAKE",
    }
