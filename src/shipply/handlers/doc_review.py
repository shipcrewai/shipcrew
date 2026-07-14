"""Doc Review handler.

Doc Review audits the requirements doc produced by Scout. It runs the
harness with a "doc-review" task, applies safe automatic fixes, and either
bounces the proposal back to Scout with a gap report or advances it to Gate 1.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite
from pacto_bot_sdk import Bot
from pacto_bot_sdk._generated.models import AgentEventParams

from shipply.config import ShipplyConfig, load_config
from shipply.db import close_db, get_db, init_db
from shipply.harness import HarnessPool
from shipply.models import ProposalState, valid_transition
from shipply.observability import EventEmitter

bot = Bot(
    bot_id="shipply-doc-review",
    event_types=["dm_received"],
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


async def _get_proposal(
    db: aiosqlite.Connection, proposal_id: str
) -> dict[str, Any] | None:
    cursor = await db.execute(
        """
        SELECT id, state, title, sponsor, created_at, updated_at
        FROM proposals
        WHERE id = ?
        """,
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


async def _latest_revision(
    db: aiosqlite.Connection, proposal_id: str
) -> dict[str, Any] | None:
    cursor = await db.execute(
        """
        SELECT id, rev_num, requirements_doc, frozen_at
        FROM revisions
        WHERE proposal_id = ?
        ORDER BY rev_num DESC
        LIMIT 1
        """,
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
        "frozen_at": row[3],
    }


async def _update_revision_doc(
    db: aiosqlite.Connection, rev_id: str, doc: str
) -> None:
    """Update the requirements doc after applying safe_auto fixes."""
    await db.execute(
        "UPDATE revisions SET requirements_doc = ? WHERE id = ?",
        (doc, rev_id),
    )
    await db.commit()


async def _freeze_rev1(db: aiosqlite.Connection, proposal_id: str) -> None:
    """Freeze rev1 by recording the current frozen_at timestamp."""
    rev = await _latest_revision(db, proposal_id)
    if rev is None:
        return
    await db.execute(
        "UPDATE revisions SET frozen_at = datetime('now') WHERE id = ?",
        (rev["id"],),
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
    bot: Bot, proposal_id: str, from_state: str, to_state: str
) -> None:
    """Send a state-transition DM to the next handler bot."""
    next_handler = {
        ProposalState.INTAKE.value: "shipply-scout",
        ProposalState.GATE_1.value: "shipply-gate-1",
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
    """Parse a Shipply state-transition DM payload."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("shipply") != "transition":
        return None
    return {
        "proposal_id": str(data.get("proposal_id", "")),
        "from": str(data.get("from", "")),
        "to": str(data.get("to", "")),
    }


def _findings_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Normalize a harness result payload into a list of finding dicts."""
    if isinstance(payload, list):
        return [f for f in payload if isinstance(f, dict)]
    if isinstance(payload, dict):
        findings = payload.get("findings")
        if isinstance(findings, list):
            return [f for f in findings if isinstance(f, dict)]
    return []


def _apply_safe_auto_fixes(
    doc: str,
    findings: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Apply machine-described safe_auto fixes to the requirements doc.

    Returns the corrected doc and the list of findings that were summarized.
    """
    corrected = doc
    applied: list[dict[str, Any]] = []
    for finding in findings:
        if finding.get("category") != "safe_auto":
            continue
        replacement = finding.get("replacement") or finding.get("fix")
        if isinstance(replacement, dict) and "old" in replacement and "new" in replacement:
            corrected = corrected.replace(
                str(replacement["old"]), str(replacement["new"])
            )
        applied.append(finding)
    return corrected, applied


def _format_findings(findings: list[dict[str, Any]]) -> str:
    """Render findings as a short bulleted list."""
    if not findings:
        return "None"
    lines = []
    for f in findings:
        sev = f.get("severity", "?")
        cat = f.get("category", "?")
        desc = f.get("description", "")
        lines.append(f"- [{sev}][{cat}] {desc}")
    return "\n".join(lines)


async def _send_to_thread(
    bot: Bot,
    db: aiosqlite.Connection,
    proposal_id: str,
    content: str,
) -> None:
    """Send a follow-up message to the originating Scout thread if known."""
    cursor = await db.execute(
        """
        SELECT source, chat_id
        FROM interview_history
        WHERE proposal_id = ?
        LIMIT 1
        """,
        (proposal_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return
    source, chat_id = row[0], row[1]
    try:
        if source == "dm":
            await bot.send_dm(recipient=chat_id, content=content)
        elif source == "group":
            await bot.send_group_message(group_id=chat_id, content=content)
    except Exception:
        # Thread follow-up is best-effort; do not block the state transition.
        pass


async def _handle_transition(
    event: AgentEventParams, bot: Bot
) -> dict[str, Any] | None:
    """Handle incoming state-transition DMs."""
    transition = _parse_transition(event.content)
    if transition is None or transition["to"] != "DOC_REVIEW":
        return None

    config = _config()
    db = await _open_db(config)
    try:
        proposal_id = transition["proposal_id"]
        proposal = await _get_proposal(db, proposal_id)
        if proposal is None or proposal["state"] != "DOC_REVIEW":
            return None

        rev = await _latest_revision(db, proposal_id)
        if rev is None or not rev["requirements_doc"]:
            return None

        pool = _pool_for(config)
        backend = await pool.get("doc-review")
        result = await backend.send(
            task="doc-review",
            context={"requirements_doc": rev["requirements_doc"]},
        )

        emitter = EventEmitter(db)

        if result.status == "error":
            gap_report = f"Doc review harness error: {result.payload}"
            await _send_to_thread(bot, db, proposal_id, gap_report)
            await _transition_proposal(
                db,
                emitter,
                proposal_id,
                "DOC_REVIEW",
                "INTAKE",
                reason="doc review error",
            )
            await _notify_next_handler(bot, proposal_id, "DOC_REVIEW", "INTAKE")
            return bot.reply(event, "Doc review failed; proposal returned to Scout.")

        findings = _findings_from_payload(result.payload)
        safe_auto = [f for f in findings if f.get("category") == "safe_auto"]
        blocking = [
            f
            for f in findings
            if f.get("category") in ("gated_auto", "manual")
            and f.get("severity") in ("P0", "P1")
        ]

        corrected_doc, applied = _apply_safe_auto_fixes(
            rev["requirements_doc"], safe_auto
        )
        await _update_revision_doc(db, rev["id"], corrected_doc)

        await _send_to_thread(
            bot,
            db,
            proposal_id,
            f"Doc review auto-corrections applied:\n{_format_findings(applied)}",
        )

        if blocking:
            gap_report = _format_findings(blocking)
            await _send_to_thread(
                bot,
                db,
                proposal_id,
                f"Blocking gaps found. Returning to Scout for clarification:\n{gap_report}",
            )
            await _transition_proposal(
                db,
                emitter,
                proposal_id,
                "DOC_REVIEW",
                "INTAKE",
                reason="blocking gaps",
            )
            await _notify_next_handler(bot, proposal_id, "DOC_REVIEW", "INTAKE")
            return bot.reply(event, "Blocking gaps found; proposal returned to Scout.")

        await _freeze_rev1(db, proposal_id)
        await _transition_proposal(
            db,
            emitter,
            proposal_id,
            "DOC_REVIEW",
            "GATE_1",
            reason="doc review passed",
        )
        await _notify_next_handler(bot, proposal_id, "DOC_REVIEW", "GATE_1")
        return bot.reply(event, "Doc review passed. Proposal advanced to Gate 1.")
    finally:
        await close_db(db)


@bot.dm
async def on_dm(event: AgentEventParams, bot: Bot) -> dict[str, Any] | None:
    """Handle incoming DMs, including state-transition notifications."""
    return await _handle_transition(event, bot)


if __name__ == "__main__":
    bot.run()
