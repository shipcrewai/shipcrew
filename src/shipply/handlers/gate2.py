"""Gate 2 (Technical RFC) handler.

Gate 2 manages the maintainer-only MLS Squad.  It posts the Blueprint output
(formula, plan graph, or legacy JSON), handles maintainer comments that
request a rebuild, and processes the ``/authorize`` command that advances the
proposal to ``FORGE`` and freezes the Blueprint revision.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import aiosqlite
from pacto_bot_sdk import Bot, parse_command
from pacto_bot_sdk._generated.models import AgentEventParams

from shipply.config import ShipplyConfig, load_config
from shipply.db import close_db, get_db, init_db
from shipply.models import ProposalState, valid_transition
from shipply.observability import EventEmitter

BOT_ID = "shipply-gate-2"
GATE_TYPE = "gate-2"
FORMULA_DIR = Path(".beads/formulas")

bot = Bot(bot_id=BOT_ID, event_types=["dm_received", "mls_group_message_received"])


def _config() -> ShipplyConfig:
    return load_config()


async def _open_db(config: ShipplyConfig | None = None) -> aiosqlite.Connection:
    cfg = config or _config()
    db = await get_db(cfg.database.path)
    await init_db(db)
    return db


async def _group_id(config: ShipplyConfig) -> str | None:
    return config.squads.group_id(GATE_TYPE)


async def _get_proposal(db: aiosqlite.Connection, proposal_id: str) -> dict[str, Any] | None:
    cursor = await db.execute(
        "SELECT id, state, title, sponsor, created_at, updated_at FROM proposals WHERE id = ?",
        (proposal_id,),
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


async def _latest_revision(db: aiosqlite.Connection, proposal_id: str) -> dict[str, Any] | None:
    cursor = await db.execute(
        "SELECT id, rev_num, requirements_doc, blueprint_doc FROM revisions "
        "WHERE proposal_id = ? ORDER BY rev_num DESC LIMIT 1",
        (proposal_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    frozen = False
    frozen_at: str | None = None
    cur2 = await db.execute(
        "SELECT frozen_at FROM blueprint_frozen WHERE revision_id = ?",
        (row[0],),
    )
    fr = await cur2.fetchone()
    await cur2.close()
    if fr is not None:
        frozen = True
        frozen_at = fr[0]
    return {
        "id": row[0],
        "rev_num": row[1],
        "requirements_doc": row[2],
        "blueprint_doc": row[3],
        "frozen": frozen,
        "frozen_at": frozen_at,
    }


async def _ensure_gate_state(db: aiosqlite.Connection, proposal_id: str) -> dict[str, Any]:
    cursor = await db.execute(
        "SELECT id, status, votes, metadata FROM gates WHERE proposal_id = ? AND gate_type = ?",
        (proposal_id, GATE_TYPE),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is not None:
        return {
            "id": row[0],
            "status": row[1],
            "votes": json.loads(row[2] or "{}"),
            "metadata": json.loads(row[3] or "{}"),
        }

    gate_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO gates (id, proposal_id, gate_type, status, votes, metadata) VALUES (?, ?, ?, ?, ?, ?)",
        (gate_id, proposal_id, GATE_TYPE, "open", "{}", "{}"),
    )
    await db.commit()
    return {"id": gate_id, "status": "open", "votes": {}, "metadata": {}}


async def _update_gate_metadata(
    db: aiosqlite.Connection,
    gate_id: str,
    metadata: dict[str, Any],
) -> None:
    await db.execute(
        "UPDATE gates SET metadata = ? WHERE id = ?",
        (json.dumps(metadata), gate_id),
    )
    await db.commit()


async def _record_squad_member(
    db: aiosqlite.Connection, group_id: str, member_pubkey: str
) -> None:
    try:
        await db.execute(
            "INSERT OR IGNORE INTO squad_members (group_id, member_pubkey) VALUES (?, ?)",
            (group_id, member_pubkey),
        )
        await db.commit()
    except sqlite3.Error:
        pass


async def _resolve_group_proposal(
    db: aiosqlite.Connection, group_id: str
) -> dict[str, Any] | None:
    cursor = await db.execute(
        "SELECT id, proposal_id, metadata FROM gates WHERE gate_type = ? AND metadata LIKE ? ORDER BY rowid DESC LIMIT 1",
        (GATE_TYPE, f'%"group_id": "{group_id}"%'),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return {
        "gate_id": row[0],
        "proposal_id": row[1],
        "metadata": json.loads(row[2] or "{}"),
    }


async def _transition_proposal(
    db: aiosqlite.Connection,
    emitter: EventEmitter,
    proposal_id: str,
    from_state: str,
    to_state: str,
    reason: str | None = None,
) -> None:
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
    bot: Bot, proposal_id: str, from_state: str, to_state: str
) -> None:
    next_handler = {
        ProposalState.FORGE.value: "shipply-forge",
        ProposalState.BLUEPRINT.value: "shipply-blueprint",
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


def _parse_transition(content: str) -> dict[str, str] | None:
    """Parse a state-transition DM payload from another Shipply handler."""
    stripped = content.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("shipply") != "transition":
        return None
    proposal_id = data.get("proposal_id") or data.get("id")
    to_state = data.get("to") or data.get("to_state")
    if not proposal_id or not to_state:
        return None
    return {
        "proposal_id": proposal_id,
        "from": data.get("from") or data.get("from_state"),
        "to": to_state,
    }


def _load_blueprint_content(revision: dict[str, Any] | None, proposal_id: str) -> str:
    """Return the Blueprint output to post to the Squad.

    Prefers the persisted ``blueprint_doc`` field; falls back to the on-disk
    formula file if it exists.
    """
    if revision is not None and revision.get("blueprint_doc"):
        return revision["blueprint_doc"]

    formula_path = FORMULA_DIR / f"shipply-{proposal_id}.formula.toml"
    if formula_path.exists():
        return formula_path.read_text(encoding="utf-8")

    return "Blueprint output is not yet available."


async def _handle_entry(
    bot: Bot, event: AgentEventParams, proposal_id: str
) -> dict[str, Any] | None:
    """Post the Blueprint output to the maintainer Squad when entering GATE_2."""
    config = _config()
    group_id = await _group_id(config)
    if group_id is None:
        bot.log("no squad configured for gate-2", level="warn")
        return None

    db = await _open_db(config)
    emitter = EventEmitter(db)
    try:
        proposal = await _get_proposal(db, proposal_id)
        if proposal is None:
            bot.log(f"proposal {proposal_id} not found", level="warn")
            return None
        if proposal["state"] != ProposalState.GATE_2.value:
            bot.log(
                f"proposal {proposal_id} is in {proposal['state']}, not GATE_2; ignoring",
                level="warn",
            )
            return None

        revision = await _latest_revision(db, proposal_id)
        blueprint_content = _load_blueprint_content(revision, proposal_id)
        frozen = revision.get("frozen") if revision else False

        header = (
            f"Technical RFC for **{proposal['title']}** is now open for review.\n\n"
            f"Proposal ID: `{proposal_id}`\n"
            f"Status: {'frozen' if frozen else 'draft'}\n\n"
            "Reply with `/authorize` to advance to FORGE, or `/rebuild <reason>` to amend the Blueprint.\n\n"
        )
        message_id = await bot.send_group_message(group_id, header + blueprint_content)

        gate_state = await _ensure_gate_state(db, proposal_id)
        metadata = gate_state["metadata"]
        metadata["group_id"] = group_id
        if message_id:
            metadata["blueprint_message_id"] = message_id
        await _update_gate_metadata(db, gate_state["id"], metadata)

        await emitter.emit(
            event_type="gate_entered",
            proposal_id=proposal_id,
            stage=ProposalState.GATE_2.value,
            payload={
                "group_id": group_id,
                "blueprint_message_id": message_id,
            },
        )
        return None
    finally:
        await close_db(db)


async def _handle_authorize(
    bot: Bot, event: AgentEventParams, group_id: str
) -> dict[str, Any] | None:
    """Authorize a proposal to advance from GATE_2 to FORGE."""
    config = _config()
    db = await _open_db(config)
    emitter = EventEmitter(db)
    try:
        await _record_squad_member(db, group_id, event.author)

        is_member = await bot.is_squad_member(group_id, event.author)
        if not is_member:
            await emitter.emit(
                event_type="gate_authorize_rejected",
                proposal_id="unknown",
                stage=ProposalState.GATE_2.value,
                payload={
                    "author": event.author,
                    "group_id": group_id,
                    "reason": "not a squad member",
                },
            )
            return bot.reply(event, "Only Squad members may authorize a Blueprint.")

        group_info = await _resolve_group_proposal(db, group_id)
        if group_info is None:
            return bot.reply(event, "No proposal is currently under review in this Squad.")

        proposal_id = group_info["proposal_id"]
        proposal = await _get_proposal(db, proposal_id)
        if proposal is None:
            return bot.reply(event, "Proposal not found.")

        revision = await _latest_revision(db, proposal_id)
        if revision is not None:
            await db.execute(
                "INSERT OR REPLACE INTO blueprint_frozen (revision_id, frozen_at) VALUES (?, datetime('now'))",
                (revision["id"],),
            )
            await db.commit()

        await emitter.emit(
            event_type="blueprint_frozen",
            proposal_id=proposal_id,
            stage=ProposalState.GATE_2.value,
            payload={"revision_id": revision["id"] if revision else None},
        )

        await _transition_proposal(
            db,
            emitter,
            proposal_id,
            ProposalState.GATE_2.value,
            ProposalState.FORGE.value,
        )
        await _notify_next_handler(
            bot, proposal_id, ProposalState.GATE_2.value, ProposalState.FORGE.value
        )
        return bot.reply(
            event,
            f"Blueprint authorized. Proposal `{proposal_id}` is advancing to FORGE.",
        )
    finally:
        await close_db(db)


async def _handle_rebuild(
    bot: Bot,
    event: AgentEventParams,
    group_id: str,
    args: list[str],
) -> dict[str, Any] | None:
    """Return a proposal to BLUEPRINT with amended parameters."""
    config = _config()
    db = await _open_db(config)
    emitter = EventEmitter(db)
    try:
        await _record_squad_member(db, group_id, event.author)

        is_member = await bot.is_squad_member(group_id, event.author)
        if not is_member:
            await emitter.emit(
                event_type="gate_rebuild_rejected",
                proposal_id="unknown",
                stage=ProposalState.GATE_2.value,
                payload={
                    "author": event.author,
                    "group_id": group_id,
                    "reason": "not a squad member",
                },
            )
            return bot.reply(event, "Only Squad members may request a Blueprint rebuild.")

        group_info = await _resolve_group_proposal(db, group_id)
        if group_info is None:
            return bot.reply(event, "No proposal is currently under review in this Squad.")

        proposal_id = group_info["proposal_id"]
        proposal = await _get_proposal(db, proposal_id)
        if proposal is None:
            return bot.reply(event, "Proposal not found.")

        gate_id = group_info["gate_id"]
        metadata = group_info["metadata"]
        amendments: dict[str, Any] = {"reason": " ".join(args) if args else "requested by maintainer"}
        # Try to parse key=value pairs from the args so the Blueprint handler can
        # apply structured amendments if it supports them.
        for token in args:
            if "=" in token:
                key, value = token.split("=", 1)
                if value:
                    amendments[key] = value
        metadata["amendments"] = amendments
        await _update_gate_metadata(db, gate_id, metadata)

        await emitter.emit(
            event_type="blueprint_rebuild_requested",
            proposal_id=proposal_id,
            stage=ProposalState.GATE_2.value,
            payload={"amendments": amendments},
        )

        await _transition_proposal(
            db,
            emitter,
            proposal_id,
            ProposalState.GATE_2.value,
            ProposalState.BLUEPRINT.value,
            reason="maintainer requested rebuild",
        )
        await _notify_next_handler(
            bot, proposal_id, ProposalState.GATE_2.value, ProposalState.BLUEPRINT.value
        )
        return bot.reply(
            event,
            f"Blueprint rebuild requested. Proposal `{proposal_id}` is returning to BLUEPRINT.",
        )
    finally:
        await close_db(db)


@bot.dm
async def on_dm(event: AgentEventParams, bot: Bot) -> dict[str, Any] | None:
    """Handle incoming DMs, including state-transition notifications."""
    transition = _parse_transition(event.content)
    if transition is None:
        return None
    if transition["to"] == ProposalState.GATE_2.value:
        await _handle_entry(bot, event, transition["proposal_id"])
    return None


@bot.event("mls_group_message_received")
async def on_group_message(event: AgentEventParams, bot: Bot) -> dict[str, Any] | None:
    """Handle MLS group messages in the maintainer Squad."""
    config = _config()
    group_id = event.chat_id or ""
    if not group_id:
        return None

    expected_group = await _group_id(config)
    if expected_group is not None and group_id != expected_group:
        return None

    db = await _open_db(config)
    try:
        await _record_squad_member(db, group_id, event.author)
    finally:
        await close_db(db)

    content = event.content.strip()
    parsed = parse_command(content)
    if parsed is None:
        return None

    command = parsed["command"]
    args = parsed.get("args", [])

    if command == "authorize":
        return await _handle_authorize(bot, event, group_id)

    if command == "rebuild":
        return await _handle_rebuild(bot, event, group_id, args)

    return None


if __name__ == "__main__":
    bot.run()
