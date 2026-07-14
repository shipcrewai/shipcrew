"""Forge handler.

The Forge bot receives a state-transition DM when a proposal enters ``FORGE``,
creates a Beads molecule from the frozen Blueprint, executes ready beads
through the Oh My Pi harness with self-healing retries, and advances the
proposal to ``GATE_3`` when all beads complete.
"""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from pathlib import Path
from typing import Any

import aiosqlite
from pacto_bot_sdk import Bot
from pacto_bot_sdk._generated.models import AgentEventParams

from shipply.backends import BeadsBackend, BeadsBackendError
from shipply.backends.beads_schema import Bead
from shipply.config import ShipplyConfig, load_config
from shipply.db import close_db, get_db, init_db
from shipply.harness import HarnessError, HarnessPool, HarnessResult
from shipply.models import ProposalState, valid_transition
from shipply.observability import EventEmitter

BOT_ID = "shipply-forge"
FORMULA_DIR = Path(".beads/formulas")

bot = Bot(bot_id=BOT_ID, event_types=["dm_received"])

_pool: HarnessPool | None = None


def _config() -> ShipplyConfig:
    return load_config()


async def _open_db(config: ShipplyConfig | None = None) -> aiosqlite.Connection:
    cfg = config or _config()
    db = await get_db(cfg.database.path)
    await init_db(db)
    return db


async def _get_pool(config: ShipplyConfig | None = None) -> HarnessPool:
    global _pool
    if _pool is None:
        _pool = HarnessPool(config=config or _config())
    return _pool


async def _group_id(config: ShipplyConfig) -> str | None:
    return config.squads.group_id("gate-2")


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


async def _latest_revision(
    db: aiosqlite.Connection, proposal_id: str
) -> dict[str, Any] | None:
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


def _extract_var_values(raw_vars: Any) -> dict[str, Any]:
    """Flatten Beads formula variable definitions into key=value pairs.

    Formula variables may be defined as scalars or as metadata objects with a
    ``default`` / ``value`` field.  Only resolved values are returned.
    """
    if not isinstance(raw_vars, dict):
        return {}
    values: dict[str, Any] = {}
    for key, value in raw_vars.items():
        if isinstance(value, dict):
            resolved = value.get("default") or value.get("value")
            if resolved is not None:
                values[key] = resolved
        else:
            values[key] = value
    return values


def _load_blueprint(revision: dict[str, Any] | None, proposal_id: str) -> dict[str, Any]:
    """Load the machine-consumable Blueprint output for Forge.

    Prefers the persisted ``blueprint_doc`` (JSON plan graph or TOML formula),
    falling back to the on-disk formula file.
    """
    blueprint: dict[str, Any] = {}

    if revision is not None:
        text = (revision.get("blueprint_doc") or "").strip()
        if text:
            if text.startswith("{"):
                try:
                    blueprint = json.loads(text)
                except json.JSONDecodeError:
                    blueprint = {}
            else:
                try:
                    import tomllib

                    blueprint = tomllib.loads(text)
                except Exception:
                    blueprint = {"formula_text": text}

            if "vars" in blueprint:
                blueprint["vars"] = _extract_var_values(blueprint["vars"])

    # Always prefer the on-disk formula file when it exists, so bd cook can read
    # the authoritative template.
    formula_path = FORMULA_DIR / f"shipply-{proposal_id}.formula.toml"
    if formula_path.exists():
        blueprint["formula_path"] = str(formula_path)

    return blueprint


async def _record_molecule(
    db: aiosqlite.Connection, proposal_id: str, molecule_id: str
) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO molecules (proposal_id, molecule_id) VALUES (?, ?)",
        (proposal_id, molecule_id),
    )
    await db.commit()


async def _record_beads(
    db: aiosqlite.Connection,
    proposal_id: str,
    molecule_id: str,
    bead_ids: list[str],
) -> None:
    for bead_id in bead_ids:
        row_id = str(uuid.uuid4())
        await db.execute(
            "INSERT OR REPLACE INTO beads (id, proposal_id, bead_id, status, molecule_id) VALUES (?, ?, ?, ?, ?)",
            (row_id, proposal_id, bead_id, "pending", molecule_id),
        )
    await db.commit()


async def _update_bead_status(
    db: aiosqlite.Connection, proposal_id: str, bead_id: str, status: str
) -> None:
    await db.execute(
        "UPDATE beads SET status = ? WHERE proposal_id = ? AND bead_id = ?",
        (status, proposal_id, bead_id),
    )
    await db.commit()


async def _record_pr_url(
    db: aiosqlite.Connection, proposal_id: str, pr_url: str | None
) -> None:
    await db.execute(
        "UPDATE molecules SET pr_url = ? WHERE proposal_id = ?",
        (pr_url, proposal_id),
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
        ProposalState.GATE_3.value: "shipply-gate-3",
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


async def _working_directory_diff() -> str:
    """Return a concise git diff of the current working directory."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--no-color",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        diff = stdout.decode("utf-8", errors="replace")
    except Exception as exc:
        return f"Unable to compute diff: {exc}"
    return diff[:4000]


def _format_excerpt(detail: Any) -> str:
    """Extract a short, human-readable log excerpt from harness output."""
    if isinstance(detail, HarnessResult):
        text = json.dumps(detail.payload, default=str)
    elif isinstance(detail, Exception):
        text = str(detail)
    else:
        text = str(detail)
    if len(text) > 2000:
        text = text[:2000] + "..."
    return text


async def _post_diagnostic_alert(
    bot: Bot,
    config: ShipplyConfig,
    proposal_id: str,
    bead_id: str,
    excerpt: str,
    diff: str,
) -> None:
    group_id = await _group_id(config)
    if group_id is None:
        bot.log("no squad configured for gate-2; skipping diagnostic alert", level="warn")
        return

    content = (
        f"Forge bead failure requires human attention.\n\n"
        f"Proposal: `{proposal_id}`\n"
        f"Bead: `{bead_id}`\n\n"
        f"**Harness excerpt:**\n```\n{excerpt}\n```\n\n"
        f"**Working directory diff:**\n```\n{diff}\n```\n\n"
        "Resolve the underlying issue and run `bd gate resolve` to unblock the bead."
    )
    try:
        await bot.send_group_message(group_id, content)
    except Exception:
        bot.log("failed to post diagnostic alert to gate-2 squad", level="warn")


async def _execute_bead_with_retries(
    bead: Bead, pool: HarnessPool, proposal: dict[str, Any]
) -> tuple[bool, Any]:
    """Execute a bead through the harness with exponential-backoff retries.

    Returns ``(True, result)`` on success and ``(False, detail)`` after the
    maximum number of attempts (initial + 2 retries).  Progressive context
    amendments (previous error, extended instructions) are added on each retry.
    """
    max_attempts = 3
    base_delay = 1.0

    context: dict[str, Any] = {
        "bead": bead.model_dump(),
        "proposal_id": proposal["id"],
        "proposal_title": proposal["title"],
    }
    last_detail: Any = None

    for attempt in range(max_attempts):
        try:
            result = await pool.send(
                persona="forge", task="execute-bead", context=context
            )
            last_detail = result
            if result.status == "success":
                return True, result
            if result.status == "needs_input":
                # Needs input cannot be resolved automatically; escalate.
                return False, result
            # Treat status=error as a retryable failure.
            raise HarnessError(str(result.payload))
        except Exception as exc:
            last_detail = exc
            if attempt == max_attempts - 1:
                return False, exc

            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            await asyncio.sleep(delay)
            context["previous_attempt"] = attempt + 1
            context["previous_error"] = str(exc)
            context["extended_instructions"] = (
                "The previous attempt failed. Retry with additional logging, "
                "validation, and a more careful approach."
            )

    return False, last_detail


async def _handle_forge(
    bot: Bot, event: AgentEventParams, proposal_id: str
) -> dict[str, Any] | None:
    """Execute the frozen Blueprint for ``proposal_id``."""
    config = _config()
    db = await _open_db(config)
    emitter = EventEmitter(db)
    try:
        proposal = await _get_proposal(db, proposal_id)
        if proposal is None:
            bot.log(f"proposal {proposal_id} not found", level="warn")
            return None
        if proposal["state"] != ProposalState.FORGE.value:
            bot.log(
                f"proposal {proposal_id} is in {proposal['state']}, not FORGE; ignoring",
                level="warn",
            )
            return None

        revision = await _latest_revision(db, proposal_id)
        if revision is None or not revision.get("frozen"):
            await emitter.emit(
                event_type="forge_error",
                proposal_id=proposal_id,
                stage=ProposalState.FORGE.value,
                payload={
                    "reason": "missing frozen Blueprint revision",
                    "revision_id": revision["id"] if revision else None,
                },
            )
            return bot.reply(event, f"Proposal {proposal_id} has no frozen Blueprint.")

        blueprint = _load_blueprint(revision, proposal_id)
        if not blueprint:
            await emitter.emit(
                event_type="forge_error",
                proposal_id=proposal_id,
                stage=ProposalState.FORGE.value,
                payload={"reason": "empty blueprint output"},
            )
            return bot.reply(event, f"Proposal {proposal_id} has no Blueprint output.")

        backend = BeadsBackend()
        try:
            molecule = await backend.create_molecule(blueprint, proposal_id)
        except BeadsBackendError as exc:
            await emitter.emit(
                event_type="forge_error",
                proposal_id=proposal_id,
                stage=ProposalState.FORGE.value,
                payload={"reason": str(exc), "stderr": exc.stderr},
            )
            return bot.reply(event, f"Failed to create Beads molecule: {exc}")

        await _record_molecule(db, proposal_id, molecule.id)
        await _record_beads(db, proposal_id, molecule.id, molecule.bead_ids)
        await emitter.emit(
            event_type="molecule_created",
            proposal_id=proposal_id,
            stage=ProposalState.FORGE.value,
            payload={
                "molecule_id": molecule.id,
                "root_id": molecule.root_id,
                "bead_count": len(molecule.bead_ids),
            },
        )

        pool = await _get_pool(config)
        completed: set[str] = set()
        failed: set[str] = set()
        in_progress: set[str] = set()

        while True:
            ready = await backend.get_ready(molecule.id)
            ready_to_process = [
                b
                for b in ready
                if b.id not in completed and b.id not in in_progress and b.id not in failed
            ]

            if ready_to_process:
                for bead in ready_to_process:
                    in_progress.add(bead.id)
                    await _update_bead_status(db, proposal_id, bead.id, "in_progress")
                    await emitter.emit(
                        event_type="bead_claimed",
                        proposal_id=proposal_id,
                        stage=ProposalState.FORGE.value,
                        payload={"bead_id": bead.id},
                    )

                    claimed = await backend.claim(bead.id)
                    success, detail = await _execute_bead_with_retries(
                        claimed, pool, proposal
                    )

                    if success:
                        await backend.close(bead.id, reason="completed")
                        completed.add(bead.id)
                        in_progress.discard(bead.id)
                        await _update_bead_status(
                            db, proposal_id, bead.id, "closed"
                        )
                        await backend.sync()
                        await emitter.emit(
                            event_type="bead_closed",
                            proposal_id=proposal_id,
                            stage=ProposalState.FORGE.value,
                            payload={
                                "bead_id": bead.id,
                                "status": "closed",
                                "detail": _format_excerpt(detail),
                            },
                        )
                    else:
                        gate = await backend.create_human_gate(
                            bead.id, reason="Harness failed after retries"
                        )
                        failed.add(bead.id)
                        in_progress.discard(bead.id)
                        await _update_bead_status(
                            db, proposal_id, bead.id, "blocked"
                        )
                        excerpt = _format_excerpt(detail)
                        diff = await _working_directory_diff()
                        await _post_diagnostic_alert(
                            bot, config, proposal_id, bead.id, excerpt, diff
                        )
                        await emitter.emit(
                            event_type="bead_human_gate",
                            proposal_id=proposal_id,
                            stage=ProposalState.FORGE.value,
                            payload={
                                "bead_id": bead.id,
                                "gate_id": gate.id,
                                "excerpt": excerpt,
                            },
                        )

                # Re-query ready: dependents may now be unblocked.
                continue

            # No ready beads that we can process.
            if in_progress:
                # Synchronous execution means this should not happen, but guard
                # against an unexpected state and exit the loop.
                break

            blocked = await backend.get_blocked(molecule.id)
            if blocked:
                # Remaining blocked beads require external action (e.g., human
                # gates).  Stay in FORGE and wait for resolution.
                await emitter.emit(
                    event_type="forge_blocked",
                    proposal_id=proposal_id,
                    stage=ProposalState.FORGE.value,
                    payload={
                        "blocked_bead_ids": [b.id for b in blocked],
                    },
                )
                break

            # All beads are complete.
            break

        if failed:
            # One or more beads require human intervention.  Remain in FORGE.
            return bot.reply(
                event,
                f"Forge paused for proposal `{proposal_id}`; {len(failed)} bead(s) "
                "need human attention in the Gate 2 Squad.",
            )

        # Close the molecule root now that all children are complete.
        try:
            closed_roots = await backend.close_eligible_roots(molecule.id)
        except BeadsBackendError as exc:
            await emitter.emit(
                event_type="forge_error",
                proposal_id=proposal_id,
                stage=ProposalState.FORGE.value,
                payload={
                    "reason": "failed to close eligible roots",
                    "stderr": exc.stderr,
                },
            )
            return bot.reply(event, f"Failed to close molecule root: {exc}")

        pr_url: str | None = None
        if molecule.root_id:
            try:
                root_bead = await backend.get_bead(molecule.root_id)
                pr_url = root_bead.pr_url or root_bead.url or root_bead.external_ref
            except BeadsBackendError:
                pr_url = None

        await _record_pr_url(db, proposal_id, pr_url)
        await emitter.emit(
            event_type="molecule_closed",
            proposal_id=proposal_id,
            stage=ProposalState.FORGE.value,
            payload={
                "molecule_id": molecule.id,
                "closed_roots": closed_roots,
                "pr_url": pr_url,
            },
        )

        await _transition_proposal(
            db,
            emitter,
            proposal_id,
            ProposalState.FORGE.value,
            ProposalState.GATE_3.value,
            reason="all beads complete",
        )
        await _notify_next_handler(
            bot, proposal_id, ProposalState.FORGE.value, ProposalState.GATE_3.value
        )
        return bot.reply(
            event,
            f"Proposal `{proposal_id}` finished FORGE. Advancing to GATE_3.",
        )
    finally:
        await close_db(db)


@bot.dm
async def on_dm(event: AgentEventParams, bot: Bot) -> dict[str, Any] | None:
    """Handle state-transition DMs for the FORGE stage."""
    transition = _parse_transition(event.content)
    if transition is None:
        return None
    if transition["to"] == ProposalState.FORGE.value:
        await _handle_forge(bot, event, transition["proposal_id"])
    return None


if __name__ == "__main__":
    bot.run()
