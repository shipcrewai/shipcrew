"""Gate 1 (Product RFC) handler.

The handler manages the Product RFC MLS Squad, collects votes, surfaces a
Dependency Card, and transitions proposals between GATE_1 and BLUEPRINT or
back to INTAKE.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import uuid
from typing import Any

import aiosqlite
from pacto_bot_sdk import Bot, parse_command
from pacto_bot_sdk._generated.models import AgentEventParams

from shipply.config import ShipplyConfig, load_config
from shipply.db import close_db, get_db, init_db
from shipply.models import ProposalState, valid_transition
from shipply.observability import EventEmitter

GATE_TYPE = "gate-1"
REJECTION_REASON_MAX_LEN = 1024

bot = Bot(
    bot_id="shipply-gate-1",
    event_types=["dm_received", "mls_group_message_received"],
)


def _config() -> ShipplyConfig:
    return load_config()


async def _open_db(config: ShipplyConfig | None = None) -> aiosqlite.Connection:
    cfg = config or _config()
    db = await get_db(cfg.database.path)
    await init_db(db)
    return db


async def _group_id(config: ShipplyConfig) -> str | None:
    return config.squads.group_id(GATE_TYPE)


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


async def _update_gate_votes(
    db: aiosqlite.Connection,
    gate_id: str,
    votes: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> None:
    if metadata is None:
        await db.execute(
            "UPDATE gates SET votes = ? WHERE id = ?",
            (json.dumps(votes), gate_id),
        )
    else:
        await db.execute(
            "UPDATE gates SET votes = ?, metadata = ? WHERE id = ?",
            (json.dumps(votes), json.dumps(metadata), gate_id),
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
        # Migration may not have run in a non-standard DB; ignore roster failures.
        pass


async def _active_member_count(db: aiosqlite.Connection, group_id: str) -> int:
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM squad_members WHERE group_id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else 0
    except sqlite3.Error:
        return 0


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
        "SELECT id, rev_num, requirements_doc, blueprint_doc, frozen_at FROM revisions WHERE proposal_id = ? ORDER BY rev_num DESC LIMIT 1",
        (proposal_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return {
        "id": row[0],
        "rev_num": row[1],
        "requirements_doc": row[2],
        "blueprint_doc": row[3],
        "frozen_at": row[4],
    }


async def _freeze_rev2(db: aiosqlite.Connection, proposal_id: str) -> None:
    rev = await _latest_revision(db, proposal_id)
    if rev is None:
        return
    rev2_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO revisions (id, proposal_id, rev_num, requirements_doc, blueprint_doc) VALUES (?, ?, ?, ?, ?)",
        (rev2_id, proposal_id, 2, rev["requirements_doc"], None),
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
        ProposalState.BLUEPRINT.value: "shipply-blueprint",
        ProposalState.INTAKE.value: "shipply-scout",
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


async def _post_to_squad(
    bot: Bot,
    group_id: str,
    proposal: dict[str, Any],
    revision: dict[str, Any] | None,
) -> str | None:
    title = proposal["title"]
    requirements_doc = revision["requirements_doc"] if revision else None
    if not requirements_doc:
        content = (
            f"Product RFC for **{title}** is now open for review.\n\n"
            f"Proposal ID: `{proposal['id']}`"
        )
    else:
        content = (
            f"Product RFC for **{title}** is now open for review.\n\n"
            f"Proposal ID: `{proposal['id']}`\n\n"
            f"{requirements_doc}"
        )
    return await bot.send_group_message(group_id, content)


def _parse_dependencies(text: str | None) -> list[dict[str, Any]]:
    """Extract declared dependencies from a requirements doc or blueprint.

    Supports a fenced ``json`` code block under a ``Dependencies`` heading, or
    an HTML comment ``<!-- shipply-deps: ... -->`` containing JSON. Unknown or
    free-text dependencies are intentionally ignored (R32).
    """
    if not text:
        return []

    deps_match = re.search(
        r"(?i)#+\s*dependencies\s*\n.*?```(?:json)?\s*\n(.*?)\n```",
        text,
        re.DOTALL,
    )
    if deps_match:
        try:
            data = json.loads(deps_match.group(1))
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
            if isinstance(data, dict):
                return [data]
        except json.JSONDecodeError:
            pass

    comment_match = re.search(
        r"<!--\s*shipply-deps:\s*(.*?)\s*-->",
        text,
        re.DOTALL,
    )
    if comment_match:
        try:
            data = json.loads(comment_match.group(1))
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
            if isinstance(data, dict):
                return [data]
        except json.JSONDecodeError:
            pass

    return []


async def _bd_list() -> list[dict[str, Any]]:
    """Run ``bd list --json`` and return a list of bead objects."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bd",
            "list",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return []
        data = json.loads(stdout.decode("utf-8", errors="replace"))
        if isinstance(data, dict):
            if "data" in data and isinstance(data["data"], list):
                return data["data"]
            if "items" in data and isinstance(data["items"], list):
                return data["items"]
            if "beads" in data and isinstance(data["beads"], list):
                return data["beads"]
        if isinstance(data, list):
            return data
        return []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


async def dependency_card(proposal_id: str) -> dict[str, Any]:
    """Build a structured Dependency Card for a proposal.

    Reads the proposal's declared dependencies, queries the local SQLite
    registry for active proposals, and queries the Beads store via ``bd list
    --json`` for open and closed beads. The result intentionally omits
    cross-proposal titles and owner details (R27).
    """
    config = _config()
    db = await _open_db(config)
    try:
        proposal = await _get_proposal(db, proposal_id)
        if proposal is None:
            return {"error": "proposal not found"}

        revision = await _latest_revision(db, proposal_id)
        declared: list[dict[str, Any]] = []
        if revision is not None and revision.get("requirements_doc"):
            declared.extend(_parse_dependencies(revision["requirements_doc"]))
        if revision is not None and revision.get("blueprint_doc"):
            declared.extend(_parse_dependencies(revision["blueprint_doc"]))

        seen = set()
        unique: list[dict[str, Any]] = []
        for dep in declared:
            key = json.dumps(dep, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                unique.append(dep)

        target_areas = set()
        affected_components = set()
        bead_ids = set()
        for dep in unique:
            if "target_area" in dep:
                target_areas.add(dep["target_area"])
            if "affected_component" in dep:
                affected_components.add(dep["affected_component"])
            if "bead_id" in dep:
                bead_ids.add(dep["bead_id"])

        cursor = await db.execute(
            "SELECT id, state, created_at FROM proposals WHERE state != ? AND id != ?",
            (ProposalState.CLOSED.value, proposal_id),
        )
        active_rows = await cursor.fetchall()
        await cursor.close()
        active_proposals = [
            {"id": row[0], "state": row[1], "created_at": row[2]}
            for row in active_rows
        ]

        bd_items = await _bd_list()
        open_beads: list[dict[str, str]] = []
        closed_beads: list[dict[str, str]] = []
        for item in bd_items:
            if not isinstance(item, dict):
                continue
            bead_id = item.get("id") or item.get("bead_id") or ""
            status = (item.get("status") or "").lower()
            if bead_id in bead_ids or any(
                item.get(k) in target_areas or item.get(k) in affected_components
                for k in ("area", "component", "target_area", "affected_component")
            ):
                entry = {
                    "id": bead_id,
                    "status": status,
                    "molecule_id": item.get("molecule_id") or "",
                }
                if status in {"closed", "merged", "done"}:
                    closed_beads.append(entry)
                else:
                    open_beads.append(entry)

        return {
            "proposal_id": proposal_id,
            "declared": unique,
            "active_proposals": active_proposals,
            "open_beads": open_beads,
            "closed_beads": closed_beads,
        }
    finally:
        await close_db(db)


async def _send_dependency_card(
    bot: Bot, group_id: str, proposal_id: str
) -> str | None:
    card = await dependency_card(proposal_id)
    if "error" in card:
        return None
    lines = ["**Dependency Card**", f"Proposal ID: `{proposal_id}`"]
    if card["declared"]:
        lines.append("\nDeclared dependencies:")
        for dep in card["declared"]:
            lines.append(f"- `{dep}`")
    else:
        lines.append("\nNo declared dependencies.")
    if card["active_proposals"]:
        lines.append(f"\nActive proposals: {len(card['active_proposals'])}")
        for p in card["active_proposals"]:
            lines.append(f"- `{p['id']}` ({p['state']})")
    else:
        lines.append("\nNo active proposals in the registry.")
    if card["open_beads"]:
        lines.append(f"\nOpen beads: {len(card['open_beads'])}")
        for b in card["open_beads"]:
            lines.append(f"- `{b['id']}` ({b['status']})")
    else:
        lines.append("\nNo open beads.")
    if card["closed_beads"]:
        lines.append(f"\nClosed beads: {len(card['closed_beads'])}")
        for b in card["closed_beads"]:
            lines.append(f"- `{b['id']}` ({b['status']})")
    else:
        lines.append("\nNo closed beads.")
    content = "\n".join(lines)
    return await bot.send_group_message(group_id, content)


async def _load_votes(db: aiosqlite.Connection, gate_id: str) -> dict[str, Any]:
    cursor = await db.execute("SELECT votes FROM gates WHERE id = ?", (gate_id,))
    row = await cursor.fetchone()
    await cursor.close()
    return json.loads(row[0] if row else "{}")


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


async def _handle_vote(
    bot: Bot,
    event: AgentEventParams,
    group_id: str,
    vote: str,
    reason: str | None,
) -> dict[str, Any] | None:
    """Record a vote, update gate state, and transition if quorum reached."""
    config = _config()
    db = await _open_db(config)
    emitter = EventEmitter(db)
    try:
        await _record_squad_member(db, group_id, event.author)

        is_member = await bot.is_squad_member(group_id, event.author)
        if not is_member:
            await emitter.emit(
                event_type="gate_vote_rejected",
                proposal_id="unknown",
                stage=ProposalState.GATE_1.value,
                payload={
                    "author": event.author,
                    "group_id": group_id,
                    "reason": "not a squad member",
                },
            )
            return bot.reply(event, "Only Squad members may vote.")

        group_info = await _resolve_group_proposal(db, group_id)
        if group_info is None:
            return bot.reply(event, "No proposal is currently under review in this Squad.")

        gate_id = group_info["gate_id"]
        proposal_id = group_info["proposal_id"]
        metadata = group_info["metadata"]
        votes = await _load_votes(db, gate_id)

        previous = votes.get(event.author)
        votes[event.author] = {"vote": vote, "reason": reason}
        await _update_gate_votes(db, gate_id, votes, metadata)

        await emitter.emit(
            event_type="gate_vote",
            proposal_id=proposal_id,
            stage=ProposalState.GATE_1.value,
            payload={
                "author": event.author,
                "vote": vote,
                "reason": reason,
                "previous_vote": previous,
            },
        )

        proposal = await _get_proposal(db, proposal_id)
        if proposal is None:
            return bot.reply(event, "Proposal not found.")

        active_members = await _active_member_count(db, group_id)
        gate_cfg = config.gates.get(GATE_TYPE)
        quorum = gate_cfg.quorum if gate_cfg else 1
        threshold = gate_cfg.threshold if gate_cfg else 0.5
        required_votes = max(quorum, math.ceil(active_members * threshold))

        approve_count = sum(1 for v in votes.values() if v.get("vote") == "approve")
        reject_count = sum(1 for v in votes.values() if v.get("vote") == "reject")
        total_votes = approve_count + reject_count

        result_message: str | None = None
        if total_votes >= required_votes:
            if approve_count > reject_count:
                await _freeze_rev2(db, proposal_id)
                await _transition_proposal(
                    db,
                    emitter,
                    proposal_id,
                    proposal["state"],
                    ProposalState.BLUEPRINT.value,
                )
                await _notify_next_handler(
                    bot, proposal_id, ProposalState.GATE_1.value, ProposalState.BLUEPRINT.value
                )
                result_message = "Quorum reached. Proposal approved and advanced to BLUEPRINT."
            elif reject_count > approve_count:
                rejection_reason = reason or "majority rejected in Gate 1"
                if len(rejection_reason) > REJECTION_REASON_MAX_LEN:
                    rejection_reason = rejection_reason[:REJECTION_REASON_MAX_LEN]
                await _transition_proposal(
                    db,
                    emitter,
                    proposal_id,
                    proposal["state"],
                    ProposalState.INTAKE.value,
                    reason=rejection_reason,
                )
                await _notify_next_handler(
                    bot, proposal_id, ProposalState.GATE_1.value, ProposalState.INTAKE.value
                )
                result_message = (
                    f"Quorum reached. Proposal rejected and returned to INTAKE: {rejection_reason}"
                )
            else:
                result_message = "Quorum reached but votes are tied; no transition."

        if result_message is None:
            result_message = (
                f"Vote recorded ({vote}). "
                f"{approve_count} approve, {reject_count} reject "
                f"({total_votes}/{required_votes} votes toward quorum)."
            )

        return bot.reply(event, result_message)
    finally:
        await close_db(db)


async def _handle_entry(
    bot: Bot, event: AgentEventParams, proposal_id: str
) -> dict[str, Any] | None:
    """Handle a proposal entering GATE_1."""
    config = _config()
    group_id = await _group_id(config)
    if group_id is None:
        bot.log(f"no squad configured for {GATE_TYPE}", level="warn")
        return None

    db = await _open_db(config)
    emitter = EventEmitter(db)
    try:
        proposal = await _get_proposal(db, proposal_id)
        if proposal is None:
            bot.log(f"proposal {proposal_id} not found", level="warn")
            return None
        if proposal["state"] != ProposalState.GATE_1.value:
            bot.log(
                f"proposal {proposal_id} is in {proposal['state']}, not GATE_1; ignoring",
                level="warn",
            )
            return None

        revision = await _latest_revision(db, proposal_id)
        message_id = await _post_to_squad(bot, group_id, proposal, revision)
        await _send_dependency_card(bot, group_id, proposal_id)

        gate_state = await _ensure_gate_state(db, proposal_id)
        metadata = gate_state["metadata"]
        metadata["group_id"] = group_id
        if message_id:
            metadata["pinned_message_id"] = message_id
        await _update_gate_votes(db, gate_state["id"], gate_state["votes"], metadata)

        await emitter.emit(
            event_type="gate_entered",
            proposal_id=proposal_id,
            stage=ProposalState.GATE_1.value,
            payload={
                "group_id": group_id,
                "pinned_message_id": message_id,
            },
        )
        return None
    finally:
        await close_db(db)


def _parse_transition(content: str) -> dict[str, str] | None:
    """Parse a state-transition DM payload.

    Supports JSON of the form
    ``{"shipply": "transition", "proposal_id": "...", "from": "...", "to": "..."}``.
    """
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


@bot.dm
async def on_dm(event: AgentEventParams, bot: Bot) -> dict[str, Any] | None:
    """Handle incoming DMs, including state-transition notifications."""
    transition = _parse_transition(event.content)
    if transition is None:
        return None
    if transition["to"] == ProposalState.GATE_1.value:
        await _handle_entry(bot, event, transition["proposal_id"])
    return None


@bot.event("mls_group_message_received")
async def on_group_message(event: AgentEventParams, bot: Bot) -> dict[str, Any] | None:
    """Handle MLS group messages: votes, commands, and roster updates."""
    config = _config()
    group_id = event.chat_id or ""
    if not group_id:
        return None

    expected_group = await _group_id(config)
    if expected_group is not None and group_id != expected_group:
        return None

    content = event.content.strip()

    db = await _open_db(config)
    try:
        await _record_squad_member(db, group_id, event.author)
    finally:
        await close_db(db)

    if content in ("👍", "👎", ":thumbs_up:", ":thumbs_down:"):
        vote = "approve" if content in ("👍", ":thumbs_up:") else "reject"
        return await _handle_vote(bot, event, group_id, vote, None)

    parsed = parse_command(content)
    if parsed is None:
        return None
    command = parsed["command"]
    args = parsed.get("args", [])

    if command == "vote":
        if not args:
            return bot.reply(event, "Usage: /vote approve|reject [reason]")
        vote = args[0].lower()
        if vote not in ("approve", "reject"):
            return bot.reply(event, "Vote must be `approve` or `reject`.")
        reason = " ".join(args[1:]) if len(args) > 1 else None
        return await _handle_vote(bot, event, group_id, vote, reason)

    if command in ("deps", "dependencies", "card"):
        db = await _open_db(config)
        try:
            group_info = await _resolve_group_proposal(db, group_id)
            if group_info is None:
                return bot.reply(event, "No proposal is currently under review.")
            await _send_dependency_card(bot, group_id, group_info["proposal_id"])
        finally:
            await close_db(db)
        return None

    return None


if __name__ == "__main__":
    bot.run()
