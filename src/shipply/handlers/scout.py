"""Scout intake handler.

Scout is the first bot in the Shipply pipeline. It accepts sparks from users
via DMs or an MLS Squad channel, conducts an interview through the Oh My Pi
harness, produces a requirements doc, and hands off to Doc Review.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from pacto_bot_sdk import Bot
from pacto_bot_sdk._generated.models import AgentEventParams

from shipply.config import ShipplyConfig, load_config
from shipply.db import close_db, get_db, init_db
from shipply.harness import HarnessPool
from shipply.models import Proposal, ProposalState, valid_transition
from shipply.observability import EventEmitter

bot = Bot(
    bot_id="shipply-scout",
    event_types=["dm_received", "mls_group_message_received"],
)

_pool: HarnessPool | None = None


def _config() -> ShipplyConfig:
    return load_config()


def _pool_for(config: ShipplyConfig) -> HarnessPool:
    """Return a lazily-created harness pool for this handler."""
    global _pool
    if _pool is None:
        _pool = HarnessPool(config=config)
    return _pool


async def _open_db(config: ShipplyConfig | None = None) -> aiosqlite.Connection:
    cfg = config or _config()
    db = await get_db(cfg.database.path)
    await init_db(db)
    return db


def _title_from_content(content: str) -> str:
    """Derive a proposal title from the initial spark message."""
    cleaned = " ".join(content.split())
    if len(cleaned) > 60:
        return cleaned[:57] + "..."
    return cleaned or "Untitled"


async def _create_proposal(
    db: aiosqlite.Connection,
    source: str,
    chat_id: str,
    author: str,
    content: str,
) -> dict[str, Any]:
    """Create a new INTAKE proposal and return its DB representation."""
    proposal_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    proposal = Proposal(
        id=proposal_id,
        state=ProposalState.INTAKE,
        title=_title_from_content(content),
        sponsor=author,
        created_at=now,
        updated_at=now,
    )
    await db.execute(
        """
        INSERT INTO proposals (id, state, title, sponsor, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            proposal.id,
            proposal.state.value,
            proposal.title,
            proposal.sponsor,
            proposal.created_at,
            proposal.updated_at,
        ),
    )
    await db.commit()
    return {
        "id": proposal.id,
        "state": proposal.state.value,
        "title": proposal.title,
        "sponsor": proposal.sponsor,
        "created_at": proposal.created_at,
        "updated_at": proposal.updated_at,
    }


async def _find_proposal_by_thread(
    db: aiosqlite.Connection,
    source: str,
    chat_id: str | None,
) -> dict[str, Any] | None:
    """Return the most recent proposal for a given thread."""
    if chat_id is None:
        return None
    cursor = await db.execute(
        """
        SELECT p.id, p.state, p.title, p.sponsor, p.created_at, p.updated_at
        FROM proposals p
        JOIN interview_history ih ON ih.proposal_id = p.id
        WHERE ih.source = ? AND ih.chat_id = ?
        ORDER BY p.created_at DESC
        LIMIT 1
        """,
        (source, chat_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return {
        "id": row[0],
        "state": row[1],
        "title": row[2],
        "sponsor": row[3],
        "created_at": row[4],
        "updated_at": row[5],
    }


async def _append_history(
    db: aiosqlite.Connection,
    message_id: str,
    proposal_id: str,
    source: str,
    chat_id: str,
    author: str,
    content: str,
) -> None:
    """Store a message as part of the interview history."""
    await db.execute(
        """
        INSERT INTO interview_history
        (message_id, proposal_id, source, chat_id, author, content)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (message_id, proposal_id, source, chat_id, author, content),
    )
    await db.commit()


async def _load_history(
    db: aiosqlite.Connection,
    proposal_id: str,
) -> list[dict[str, Any]]:
    """Return all messages for a proposal in chronological order."""
    cursor = await db.execute(
        """
        SELECT message_id, author, content, created_at
        FROM interview_history
        WHERE proposal_id = ?
        ORDER BY created_at ASC
        """,
        (proposal_id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [
        {"message_id": r[0], "author": r[1], "content": r[2], "created_at": r[3]}
        for r in rows
    ]


async def _load_contributors(
    db: aiosqlite.Connection,
    proposal_id: str,
) -> list[str]:
    """Return the distinct author pubkeys contributing to a proposal."""
    cursor = await db.execute(
        """
        SELECT DISTINCT author
        FROM interview_history
        WHERE proposal_id = ?
        """,
        (proposal_id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [r[0] for r in rows]


async def _update_title(
    db: aiosqlite.Connection,
    proposal_id: str,
    title: str,
) -> None:
    """Update the proposal title when the harness produces a better one."""
    await db.execute(
        "UPDATE proposals SET title = ?, updated_at = datetime('now') WHERE id = ?",
        (title, proposal_id),
    )
    await db.commit()


async def _write_revision(
    db: aiosqlite.Connection,
    proposal_id: str,
    rev_num: int,
    requirements_doc: str,
) -> None:
    """Write a new revision row for the requirements doc."""
    rev_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO revisions (id, proposal_id, rev_num, requirements_doc)
        VALUES (?, ?, ?, ?)
        """,
        (rev_id, proposal_id, rev_num, requirements_doc),
    )
    await db.commit()


async def _transition_proposal(
    db: aiosqlite.Connection,
    emitter: EventEmitter,
    proposal_id: str,
    from_state: str,
    to_state: str,
    reason: str | None = None,
) -> None:
    """Validate and apply a proposal state transition, then emit an event."""
    valid_transition(from_state, to_state)
    await db.execute(
        "UPDATE proposals SET state = ?, updated_at = datetime('now') WHERE id = ?",
        (to_state, proposal_id),
    )
    await db.commit()
    await emitter.emit(
        event_type="proposal_transition",
        proposal_id=proposal_id,
        stage=to_state,
        payload={
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
        },
    )


async def _notify_next_handler(
    bot: Bot,
    proposal_id: str,
    from_state: str,
    to_state: str,
) -> None:
    """Send a state-transition DM to the next handler bot."""
    next_handler = {
        ProposalState.DOC_REVIEW.value: "shipply-doc-review",
    }.get(to_state)
    if next_handler is None:
        return
    message = json.dumps(
        {
            "shipply": "transition",
            "proposal_id": proposal_id,
            "from": from_state,
            "to": to_state,
        }
    )
    await bot.send_dm(recipient=next_handler, content=message)


def _extract_doc(payload: Any) -> str | None:
    """Extract a requirements doc string from a harness payload."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return payload.get("requirements_doc") or payload.get("doc") or payload.get("document")
    return None


def _extract_title(payload: Any) -> str | None:
    """Extract a title override from a harness payload."""
    if isinstance(payload, dict):
        return payload.get("title")
    return None


def _fallback_doc(
    proposal: dict[str, Any],
    history: list[dict[str, Any]],
    done: bool,
) -> str:
    """Build a minimal requirements doc from the interview history."""
    lines = [
        f"# Requirements: {proposal['title']}",
        "",
        f"**Sponsor:** {proposal['sponsor']}",
        f"**Proposal ID:** {proposal['id']}",
        "",
        "## Interview History",
        "",
    ]
    for h in history:
        lines.append(f"- {h['author']}: {h['content']}")
    if done:
        lines.extend(
            [
                "",
                "**Warning:** User terminated the interview early. Requirements may be incomplete.",
            ]
        )
    return "\n".join(lines)


async def _handle_message(
    event: AgentEventParams,
    bot: Bot,
    source: str,
) -> dict[str, Any] | None:
    """Core message handler used for both DMs and Squad messages."""
    # Do not reply to our own messages in the Squad.
    if bot.own_pubkey is not None and event.author == bot.own_pubkey:
        return None

    if event.chat_id is None:
        return None

    config = _config()
    db = await _open_db(config)
    try:
        proposal = await _find_proposal_by_thread(db, source, event.chat_id)
        if proposal is None:
            proposal = await _create_proposal(
                db, source, event.chat_id, event.author, event.content
            )

        await _append_history(
            db,
            event.event_id,
            proposal["id"],
            source,
            event.chat_id,
            event.author,
            event.content,
        )

        done = event.content.strip().startswith("/done")

        history = await _load_history(db, proposal["id"])
        contributors = await _load_contributors(db, proposal["id"])

        pool = _pool_for(config)
        backend = await pool.get("scout")
        context: dict[str, Any] = {
            "proposal_id": proposal["id"],
            "title": proposal["title"],
            "sponsor": proposal["sponsor"],
            "messages": [
                {"author": h["author"], "content": h["content"]}
                for h in history
            ],
            "contributors": contributors,
            "done": done,
        }
        if done:
            context["warning"] = "User terminated the interview early."

        result = await backend.send(task="brainstorm", context=context)

        if result.status == "success" or done:
            requirements_doc = _extract_doc(result.payload) or _fallback_doc(
                proposal, history, done
            )
            title = _extract_title(result.payload)
            if title:
                await _update_title(db, proposal["id"], title)
            await _write_revision(db, proposal["id"], 1, requirements_doc)
            emitter = EventEmitter(db)
            await _transition_proposal(
                db,
                emitter,
                proposal["id"],
                "INTAKE",
                "DOC_REVIEW",
                reason="interview complete",
            )
            await _notify_next_handler(
                bot, proposal["id"], "INTAKE", "DOC_REVIEW"
            )
            if done:
                return bot.reply(
                    event,
                    "Interview ended early. Requirements doc written with a warning; moving to Doc Review.",
                )
            return bot.reply(
                event,
                "Interview complete. Requirements doc written; moving to Doc Review.",
            )

        if result.status == "needs_input":
            return bot.reply(event, str(result.payload))

        if result.status == "error":
            return bot.reply(
                event,
                f"Interview harness error: {result.payload}",
            )

        return bot.reply(
            event,
            f"Unexpected harness status: {result.status}",
        )
    finally:
        await close_db(db)


@bot.dm
async def on_dm(event: AgentEventParams, bot: Bot) -> dict[str, Any] | None:
    """Handle incoming DMs."""
    return await _handle_message(event, bot, "dm")


@bot.event("mls_group_message_received")
async def on_group_message(
    event: AgentEventParams, bot: Bot
) -> dict[str, Any] | None:
    """Handle incoming MLS Squad messages."""
    return await _handle_message(event, bot, "group")


if __name__ == "__main__":
    bot.run()
