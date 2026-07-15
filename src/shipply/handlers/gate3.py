"""Gate 3 (PR Review) handler.

Gate 3 is the final governance gate.  When a proposal enters ``GATE_3``, the
handler posts the PR URL recorded during Forge to the configured MLS Squad and
asks maintainers to run ``/gate3 check``.  On each ``/gate3 check`` command the
handler queries the PR status with ``gh pr view`` and:

* merged  -> transition the proposal to ``CLOSED`` and post the final status;
* requested changes -> post a summary, return the proposal to ``FORGE``, and
  notify ``shipply-forge`` via state-transition DM;
* otherwise -> post the current status without transitioning.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import uuid
from typing import Any

import aiosqlite
from pacto_bot_sdk import Bot
from pacto_bot_sdk._generated.models import AgentEventParams

from shipply.config import ShipplyConfig, load_config
from shipply.db import close_db, get_db, init_db
from shipply.models import ProposalState, valid_transition
from shipply.observability import EventEmitter

BOT_ID = "shipply-gate-3"
GATE_TYPE = "gate-3"
logger = logging.getLogger(__name__)

bot = Bot(bot_id=BOT_ID, event_types=["dm_received", "mls_group_message_received"])

# Allow-list for PR identifiers: only https://github.com/<owner>/<repo>/pull/<num>
_GITHUB_PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9][0-9]*)/?$"
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


async def _get_proposal(
    db: aiosqlite.Connection, proposal_id: str
) -> dict[str, Any] | None:
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


async def _get_pr_url(
    db: aiosqlite.Connection, proposal_id: str
) -> str | None:
    cursor = await db.execute(
        "SELECT pr_url FROM molecules WHERE proposal_id = ?",
        (proposal_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return row[0]


async def _ensure_gate_state(
    db: aiosqlite.Connection, proposal_id: str
) -> dict[str, Any]:
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


def _validate_pr_url(pr_url: str) -> bool:
    """Return True if ``pr_url`` matches the allowed GitHub PR URL format."""
    return _GITHUB_PR_URL_RE.match(pr_url.strip()) is not None


def parse_bridge_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a Nostr bridge payload sent by the webhook bridge.

    Returns a normalized dict with the PR URL and relevant event metadata,
    or ``None`` if the payload is not a recognized bridge event.
    """
    if not isinstance(payload, dict) or payload.get("shipply") != "bridge_event":
        return None
    pr_url = payload.get("pr_url")
    if not pr_url or not _validate_pr_url(pr_url):
        return None
    return {
        "pr_url": pr_url,
        "event_type": payload.get("event_type"),
        "action": payload.get("action"),
        "delivery_id": payload.get("delivery_id"),
        "state": payload.get("state"),
        "review_decision": payload.get("review_decision"),
        "updated_at": payload.get("updated_at"),
    }


async def resolve_proposal_by_pr_url(
    db: aiosqlite.Connection, pr_url: str
) -> dict[str, Any] | None:
    """Resolve a proposal record from a GitHub PR URL.

    Looks up the PR URL in the ``molecules`` table and returns the proposal
    record plus its ID.  Returns ``None`` when the PR URL is not known.
    """
    cursor = await db.execute(
        "SELECT proposal_id FROM molecules WHERE pr_url = ?",
        (pr_url,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    proposal_id = row[0]
    proposal = await _get_proposal(db, proposal_id)
    if proposal is None:
        return None
    return {"proposal_id": proposal_id, "proposal": proposal}


async def handle_bridge_event(
    db: aiosqlite.Connection,
    emitter: EventEmitter,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Handle a bridge event for U6 by parsing and logging it.

    U7 will implement the actual proposal transitions.  For now the handler
    validates the payload, resolves the proposal by PR URL, and emits a log
    event so operators can verify the bridge is delivering events.
    """
    parsed = parse_bridge_payload(payload)
    if parsed is None:
        logger.warning("dropping malformed bridge payload: keys=%s", sorted(payload.keys()) if isinstance(payload, dict) else type(payload))
        return None

    resolved = await resolve_proposal_by_pr_url(db, parsed["pr_url"])
    if resolved is None:
        logger.info(
            "bridge event for unknown PR dropped: %s", parsed["pr_url"]
        )
        return None

    await emitter.emit(
        event_type="bridge_event_received",
        proposal_id=resolved["proposal_id"],
        stage=ProposalState.GATE_3.value,
        payload=parsed,
    )
    return resolved


async def _query_pr_status(pr_url: str) -> dict[str, Any] | None:
    """Run ``gh pr view --json ...`` for the validated PR URL.

    The PR URL is validated against an allow-list before execution, and the
    command is built as a list of arguments rather than a shell string.
    """
    if not _validate_pr_url(pr_url):
        return None

    cmd = [
        "gh",
        "pr",
        "view",
        pr_url,
        "--json",
        "state,url,title,reviewDecision,mergeStateStatus",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception:
        return None

    if proc.returncode != 0:
        return None

    try:
        return json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


async def _handle_entry(
    bot: Bot, event: AgentEventParams, proposal_id: str
) -> dict[str, Any] | None:
    """Post the PR URL to the Gate 3 Squad when entering GATE_3."""
    config = _config()
    group_id = await _group_id(config)
    if group_id is None:
        bot.log("no squad configured for gate-3", level="warn")
        return None

    db = await _open_db(config)
    emitter = EventEmitter(db)
    try:
        proposal = await _get_proposal(db, proposal_id)
        if proposal is None:
            bot.log(f"proposal {proposal_id} not found", level="warn")
            return None
        if proposal["state"] != ProposalState.GATE_3.value:
            bot.log(
                f"proposal {proposal_id} is in {proposal['state']}, not GATE_3; ignoring",
                level="warn",
            )
            return None

        pr_url = await _get_pr_url(db, proposal_id)
        gate_state = await _ensure_gate_state(db, proposal_id)
        metadata = gate_state["metadata"]
        metadata["group_id"] = group_id

        header = (
            f"PR Review for **{proposal['title']}** is now open.\n\n"
            f"Proposal ID: `{proposal_id}`\n"
        )
        if pr_url:
            header += f"PR URL: {pr_url}\n"
            body = header + "\nIssue `/gate3 check` to verify PR status."
        else:
            body = header + "No PR URL was recorded during Forge. Please investigate."
        message_id = await bot.send_group_message(group_id, body)

        if message_id:
            metadata["entry_message_id"] = message_id
        await _update_gate_metadata(db, gate_state["id"], metadata)

        await emitter.emit(
            event_type="gate_entered",
            proposal_id=proposal_id,
            stage=ProposalState.GATE_3.value,
            payload={
                "group_id": group_id,
                "pr_url": pr_url,
                "entry_message_id": message_id,
            },
        )
        return None
    finally:
        await close_db(db)


async def _handle_check(
    bot: Bot, event: AgentEventParams, group_id: str
) -> dict[str, Any] | None:
    """Handle ``/gate3 check`` commands in the Gate 3 Squad."""
    config = _config()
    db = await _open_db(config)
    emitter = EventEmitter(db)
    try:
        await _record_squad_member(db, group_id, event.author)

        is_member = await bot.is_squad_member(group_id, event.author)
        if not is_member:
            await emitter.emit(
                event_type="gate_check_rejected",
                proposal_id="unknown",
                stage=ProposalState.GATE_3.value,
                payload={
                    "author": event.author,
                    "group_id": group_id,
                    "reason": "not a squad member",
                },
            )
            return bot.reply(event, "Only Squad members may run `/gate3 check`.")

        group_info = await _resolve_group_proposal(db, group_id)
        if group_info is None:
            return bot.reply(
                event, "No proposal is currently under PR review in this Squad."
            )

        proposal_id = group_info["proposal_id"]
        proposal = await _get_proposal(db, proposal_id)
        if proposal is None:
            return bot.reply(event, "Proposal not found.")

        if proposal["state"] != ProposalState.GATE_3.value:
            return bot.reply(
                event,
                f"Proposal `{proposal_id}` is in {proposal['state']}, not GATE_3.",
            )

        pr_url = await _get_pr_url(db, proposal_id)
        if not pr_url:
            return bot.reply(
                event,
                f"No PR URL recorded for proposal `{proposal_id}`. Cannot run PR check.",
            )

        pr_info = await _query_pr_status(pr_url)
        if pr_info is None:
            return bot.reply(
                event,
                f"Failed to query PR status for {pr_url}. "
                "Please verify the PR exists and `gh` is authenticated.",
            )

        state = (pr_info.get("state") or "UNKNOWN").upper()
        title = pr_info.get("title") or "Untitled"
        url = pr_info.get("url") or pr_url
        review_decision = (pr_info.get("reviewDecision") or "NO_REVIEW").upper()
        merge_state = pr_info.get("mergeStateStatus") or "UNKNOWN"

        if state == "MERGED":
            await _transition_proposal(
                db,
                emitter,
                proposal_id,
                proposal["state"],
                ProposalState.CLOSED.value,
                reason="PR merged",
            )
            final_message = (
                f"Proposal **{proposal['title']}** (`{proposal_id}`) is now CLOSED.\n\n"
                f"PR merged: [{title}]({url})\n"
                f"Review decision: {review_decision}\n"
                f"Merge state status: {merge_state}"
            )
            await bot.send_group_message(group_id, final_message)
            await emitter.emit(
                event_type="pr_merged",
                proposal_id=proposal_id,
                stage=ProposalState.CLOSED.value,
                payload={
                    "pr_url": url,
                    "title": title,
                    "review_decision": review_decision,
                    "merge_state_status": merge_state,
                },
            )
            return None

        if review_decision == "CHANGES_REQUESTED":
            summary = (
                f"PR Review for **{proposal['title']}** (`{proposal_id}`) has requested changes.\n\n"
                f"PR: [{title}]({url})\n"
                f"State: {state}\n"
                f"Review decision: {review_decision}\n"
                f"Merge state status: {merge_state}\n\n"
                f"Returning proposal to FORGE for rework."
            )
            await bot.send_group_message(group_id, summary)
            await _transition_proposal(
                db,
                emitter,
                proposal_id,
                proposal["state"],
                ProposalState.FORGE.value,
                reason="PR changes requested",
            )
            await _notify_next_handler(
                bot, proposal_id, ProposalState.GATE_3.value, ProposalState.FORGE.value
            )
            return None

        status_message = (
            f"PR Review status for **{proposal['title']}** (`{proposal_id}`):\n\n"
            f"PR: [{title}]({url})\n"
            f"State: {state}\n"
            f"Review decision: {review_decision}\n"
            f"Merge state status: {merge_state}\n\n"
            f"No transition needed. Issue `/gate3 check` again later."
        )
        await bot.send_group_message(group_id, status_message)
        await emitter.emit(
            event_type="pr_status_check",
            proposal_id=proposal_id,
            stage=ProposalState.GATE_3.value,
            payload={
                "pr_url": url,
                "title": title,
                "state": state,
                "review_decision": review_decision,
                "merge_state_status": merge_state,
            },
        )
        return None
    finally:
        await close_db(db)


@bot.dm
async def on_dm(event: AgentEventParams, bot: Bot) -> dict[str, Any] | None:
    """Handle incoming DMs, including state-transition notifications."""
    content = (event.content or "").strip()
    transition = _parse_transition(content)
    if transition is None:
        return None
    if transition["to"] == ProposalState.GATE_3.value:
        await _handle_entry(bot, event, transition["proposal_id"])
    return None


@bot.event("mls_group_message_received")
async def on_group_message(event: AgentEventParams, bot: Bot) -> dict[str, Any] | None:
    """Handle MLS group messages in the Gate 3 Squad."""
    config = _config()
    group_id = event.chat_id or ""
    if not group_id:
        return None

    expected_group = await _group_id(config)
    if expected_group is not None and group_id != expected_group:
        return None

    content = (event.content or "").strip()
    parts = content.split()
    if len(parts) >= 2 and parts[0].lower() == "/gate3":
        subcommand = parts[1].lower()
        if subcommand == "check":
            return await _handle_check(bot, event, group_id)
    return None


if __name__ == "__main__":
    bot.run()
