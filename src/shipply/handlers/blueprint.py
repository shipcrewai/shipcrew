"""Blueprint handler.

The Blueprint bot receives a state-transition DM when a proposal enters
``BLUEPRINT``, fetches the approved requirements doc (``rev2``), asks the
Oh My Pi ``blueprint`` persona to produce a technical plan, and persists the
plan as a Beads formula.  Valid plans are transitioned to ``GATE_2``.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import aiosqlite
from pacto_bot_sdk import Bot
from pacto_bot_sdk._generated.models import AgentEventParams

from shipply.config import ShipplyConfig, load_config
from shipply.db import close_db, get_db, init_db
from shipply.harness import HarnessError, HarnessPool
from shipply.models import ProposalState, valid_transition
from shipply.observability import EventEmitter

BOT_ID = "shipply-blueprint"
FORMULA_DIR = Path(".beads/formulas")
SCHEMA_PATH = Path(__file__).with_suffix("").parent.parent / "schemas" / "blueprint.json"

bot = Bot(bot_id=BOT_ID, event_types=["dm_received"])

_pool: HarnessPool | None = None


def _load_schema() -> dict[str, Any]:
    if not SCHEMA_PATH.exists():
        return {}
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate_blueprint(data: dict[str, Any]) -> tuple[bool, str | None]:
    """Lightweight validation against the documented Blueprint schema (R26)."""
    required = {"bead_specs", "file_deltas", "dependencies", "step_ordering"}
    missing = required - set(data.keys())
    if missing:
        return False, f"missing required fields: {', '.join(sorted(missing))}"

    for key in required:
        if not isinstance(data[key], list):
            return False, f"{key} must be a list"

    return True, None


def _parse_payload(payload: Any) -> dict[str, Any]:
    """Coerce a harness payload into a Blueprint dict."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            raise ValueError("empty blueprint payload")
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    raise ValueError("blueprint payload is not a JSON object")


def _toml_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return f'"{_toml_escape(value)}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value type: {type(value)}")


def _toml_dumps(data: dict[str, Any]) -> str:
    """Minimal TOML writer for the formula schema.

    Supports scalar values, inline lists, and arrays of tables for lists of
    dicts.  This is intentionally small and dependency-free.
    """
    lines: list[str] = []
    tables: list[tuple[str, list[dict[str, Any]]]] = []

    for key, value in data.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            tables.append((key, value))
        elif isinstance(value, dict):
            lines.append(f"[{key}]")
            for sub_key, sub_value in value.items():
                if sub_value is None:
                    continue
                lines.append(f"{sub_key} = {_toml_value(sub_value)}")
            lines.append("")
        else:
            if value is None:
                continue
            lines.append(f"{key} = {_toml_value(value)}")

    for key, items in tables:
        for item in items:
            lines.append(f"[[{key}]]")
            for sub_key, sub_value in item.items():
                if sub_value is None:
                    continue
                lines.append(f"{sub_key} = {_toml_value(sub_value)}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _blueprint_to_formula(proposal_id: str, title: str, blueprint: dict[str, Any]) -> str:
    """Convert a normalized Blueprint dict into a Beads formula TOML string."""
    formula: dict[str, Any] = {
        "formula": f"shipply-{proposal_id}",
        "description": f"Blueprint for proposal {proposal_id}: {title}",
        "version": 1,
        "type": "workflow",
    }

    # Carry through the documented schema fields.
    for key in ("bead_specs", "file_deltas", "dependencies", "step_ordering"):
        formula[key] = blueprint.get(key, [])

    # Optional workflow variables surfaced from the blueprint payload.
    vars_ = blueprint.get("vars")
    if isinstance(vars_, dict):
        formula["vars"] = vars_

    # Convert the step ordering into Beads steps.  If the harness already
    # returned a ``steps`` array, prefer it.
    steps = blueprint.get("steps")
    if not isinstance(steps, list):
        steps = []
        prev: str | None = None
        for step_id in blueprint.get("step_ordering", []):
            step = {
                "id": step_id,
                "title": f"Execute step {step_id}",
                "type": "task",
            }
            if prev is not None:
                step["needs"] = [prev]
            steps.append(step)
            prev = step_id

    formula["steps"] = steps
    return _toml_dumps(formula)


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


async def _load_gate_amendments(db: aiosqlite.Connection, proposal_id: str) -> dict[str, Any]:
    """Read any amendments stored by Gate 2 during a rebuild request."""
    cursor = await db.execute(
        "SELECT metadata FROM gates WHERE proposal_id = ? AND gate_type = ?",
        (proposal_id, "gate-2"),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return {}
    metadata = json.loads(row[0] or "{}")
    return metadata.get("amendments", {})


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
        ProposalState.GATE_2.value: "shipply-gate-2",
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


async def _handle_blueprint(
    bot: Bot, event: AgentEventParams, proposal_id: str
) -> dict[str, Any] | None:
    """Generate and validate a Blueprint for ``proposal_id``."""
    config = _config()
    db = await _open_db(config)
    emitter = EventEmitter(db)
    try:
        proposal = await _get_proposal(db, proposal_id)
        if proposal is None:
            bot.log(f"proposal {proposal_id} not found", level="warn")
            return None
        if proposal["state"] != ProposalState.BLUEPRINT.value:
            bot.log(
                f"proposal {proposal_id} is in {proposal['state']}, not BLUEPRINT; ignoring",
                level="warn",
            )
            return None

        revision = await _latest_revision(db, proposal_id)
        requirements_doc = revision["requirements_doc"] if revision else None
        if not requirements_doc:
            await emitter.emit(
                event_type="blueprint_error",
                proposal_id=proposal_id,
                stage=ProposalState.BLUEPRINT.value,
                payload={"reason": "missing approved requirements doc (rev2)"},
            )
            return bot.reply(event, f"Proposal {proposal_id} has no approved requirements doc.")

        amendments = await _load_gate_amendments(db, proposal_id)
        context = {"requirements_doc": requirements_doc}
        if amendments:
            context["amendments"] = amendments

        pool = await _get_pool(config)
        try:
            result = await pool.send(
                persona="blueprint",
                task="plan",
                context=context,
            )
        except HarnessError as exc:
            await emitter.emit(
                event_type="blueprint_error",
                proposal_id=proposal_id,
                stage=ProposalState.BLUEPRINT.value,
                payload={"reason": str(exc)},
            )
            return bot.reply(event, f"Blueprint harness failed: {exc}")

        if result.status == "needs_input":
            await emitter.emit(
                event_type="blueprint_needs_input",
                proposal_id=proposal_id,
                stage=ProposalState.BLUEPRINT.value,
                payload={"payload": result.payload},
            )
            return bot.reply(event, f"Blueprint needs input: {result.payload}")

        if result.status == "error":
            payload = result.payload
            reason = payload
            if isinstance(payload, dict):
                reason = payload.get("message", str(payload))
            await emitter.emit(
                event_type="blueprint_error",
                proposal_id=proposal_id,
                stage=ProposalState.BLUEPRINT.value,
                payload={"reason": reason},
            )
            return bot.reply(event, f"Blueprint generation failed: {reason}")

        try:
            raw = _parse_payload(result.payload)
        except (json.JSONDecodeError, ValueError) as exc:
            await emitter.emit(
                event_type="blueprint_invalid",
                proposal_id=proposal_id,
                stage=ProposalState.BLUEPRINT.value,
                payload={"reason": f"payload is not a JSON object: {exc}"},
            )
            return bot.reply(event, f"Blueprint payload is not valid: {exc}")

        valid, reason = _validate_blueprint(raw)
        if not valid:
            await emitter.emit(
                event_type="blueprint_invalid",
                proposal_id=proposal_id,
                stage=ProposalState.BLUEPRINT.value,
                payload={"reason": reason},
            )
            return bot.reply(event, f"Blueprint validation failed: {reason}")

        blueprint = {
            "bead_specs": raw.get("bead_specs", []),
            "file_deltas": raw.get("file_deltas", []),
            "dependencies": raw.get("dependencies", []),
            "step_ordering": raw.get("step_ordering", []),
        }
        for key in ("vars", "steps"):
            if key in raw:
                blueprint[key] = raw[key]

        # Persist the preferred formula TOML file.
        FORMULA_DIR.mkdir(parents=True, exist_ok=True)
        formula_path = FORMULA_DIR / f"shipply-{proposal_id}.formula.toml"
        formula_text = _blueprint_to_formula(proposal_id, proposal["title"], blueprint)
        formula_path.write_text(formula_text, encoding="utf-8")

        # Legacy fallback: store the formula text in the revisions table so Gate 2
        # can read it without scanning the filesystem.
        await db.execute(
            "UPDATE revisions SET blueprint_doc = ? WHERE id = ?",
            (formula_text, revision["id"]),
        )
        await db.commit()

        await emitter.emit(
            event_type="blueprint_generated",
            proposal_id=proposal_id,
            stage=ProposalState.BLUEPRINT.value,
            payload={
                "formula_path": str(formula_path),
                "rev_num": revision["rev_num"],
            },
        )

        await _transition_proposal(
            db,
            emitter,
            proposal_id,
            ProposalState.BLUEPRINT.value,
            ProposalState.GATE_2.value,
        )
        await _notify_next_handler(
            bot, proposal_id, ProposalState.BLUEPRINT.value, ProposalState.GATE_2.value
        )
        return None
    finally:
        await close_db(db)


@bot.dm
async def on_dm(event: AgentEventParams, bot: Bot) -> dict[str, Any] | None:
    """Handle state-transition DMs for the BLUEPRINT stage."""
    transition = _parse_transition(event.content)
    if transition is None:
        return None
    if transition["to"] == ProposalState.BLUEPRINT.value:
        await _handle_blueprint(bot, event, transition["proposal_id"])
    return None


if __name__ == "__main__":
    bot.run()
