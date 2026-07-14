"""Tests for the Blueprint bot handler."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import tomllib

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
from shipply.harness import HarnessResult
from shipply.models import ProposalState
import shipply.handlers.blueprint as blueprint


pytestmark = pytest.mark.asyncio

BLUEPRINT_GROUP_ID = "test-blueprint-group"
GATE_2_GROUP_ID = "test-gate-2-group"


def _valid_blueprint_payload() -> dict:
    return {
        "bead_specs": [{"id": "bead-1", "type": "task"}],
        "file_deltas": [{"path": "src/foo.py", "change": "add"}],
        "dependencies": [{"target_area": "core"}],
        "step_ordering": ["step-1", "step-2"],
    }


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
            "gate-1": GateConfig(quorum=3, threshold=0.5),
            "gate-2": GateConfig(quorum=2, threshold=0.75),
            "gate-3": GateConfig(quorum=2, threshold=0.75),
        },
        database=DatabaseConfig(path=str(tmp_path / "test.db")),
        squads=SquadsConfig(
            gate_1=BLUEPRINT_GROUP_ID,
            gate_2=GATE_2_GROUP_ID,
        ),
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
    monkeypatch.setattr(blueprint, "_config", lambda: test_config)


@pytest.fixture
def patch_formula_dir(tmp_path: Path, monkeypatch) -> Path:
    """Redirect formula output to a temp directory."""
    formula_dir = tmp_path / ".beads" / "formulas"
    monkeypatch.setattr(blueprint, "FORMULA_DIR", formula_dir)
    return formula_dir


@pytest.fixture
def mock_bot(monkeypatch) -> MagicMock:
    """Return a mocked pacto_bot_sdk.Bot with the methods Blueprint uses."""
    bot = MagicMock()
    bot.send_group_message = AsyncMock(return_value="msg-123")
    bot.send_dm = AsyncMock()
    bot.is_squad_member = AsyncMock(return_value=True)
    bot.reply = MagicMock(return_value={"type": "reply"})
    bot.log = MagicMock()
    monkeypatch.setattr(blueprint, "bot", bot)
    return bot


@pytest.fixture
def mock_pool(monkeypatch) -> MagicMock:
    """Return a mock HarnessPool whose send returns a valid Blueprint result."""
    pool = MagicMock()
    pool.send = AsyncMock(
        return_value=HarnessResult(
            status="success",
            payload=_valid_blueprint_payload(),
        )
    )
    monkeypatch.setattr(blueprint, "_get_pool", AsyncMock(return_value=pool))
    return pool


def make_event(
    content: str,
    author: str = "shipply-doc-review",
    chat_id: str | None = None,
    event_type: str = "dm_received",
) -> AgentEventParams:
    """Build a pacto-bot-api event for tests."""
    return AgentEventParams(
        author=author,
        bot_id="shipply-blueprint",
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
    state: str = ProposalState.BLUEPRINT.value,
    title: str = "Test Proposal",
    sponsor: str = "sponsor-1",
) -> None:
    await db.execute(
        "INSERT INTO proposals (id, state, title, sponsor) VALUES (?, ?, ?, ?)",
        (proposal_id, state, title, sponsor),
    )
    await db.commit()


async def insert_revision(
    db: aiosqlite.Connection,
    proposal_id: str,
    rev_num: int = 2,
    requirements_doc: str | None = "# Approved Requirements\n\nGoals: build the thing.",
    blueprint_doc: str | None = None,
) -> str:
    rev_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO revisions (id, proposal_id, rev_num, requirements_doc, blueprint_doc) VALUES (?, ?, ?, ?, ?)",
        (rev_id, proposal_id, rev_num, requirements_doc, blueprint_doc),
    )
    await db.commit()
    return rev_id


class TestBlueprintTransition:
    """Tests for the BLUEPRINT transition DM handler."""

    async def test_blueprint_dm_fetches_rev2_requirements_and_calls_harness(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        patch_formula_dir: Path,
        mock_bot: MagicMock,
        mock_pool: MagicMock,
    ) -> None:
        """A transition DM should pass the approved requirements doc to the harness with task=plan."""
        proposal_id = "prop-blueprint-01"
        requirements = "# Approved Requirements\n\nGoals: build the thing."
        await insert_proposal(db, proposal_id)
        await insert_revision(db, proposal_id, rev_num=2, requirements_doc=requirements)

        content = json.dumps(
            {
                "shipply": "transition",
                "proposal_id": proposal_id,
                "from": ProposalState.GATE_1.value,
                "to": ProposalState.BLUEPRINT.value,
            }
        )
        event = make_event(content)
        await blueprint.on_dm(event, mock_bot)

        mock_pool.send.assert_awaited_once()
        call_kwargs = mock_pool.send.call_args.kwargs
        assert call_kwargs["persona"] == "blueprint"
        assert call_kwargs["task"] == "plan"
        assert call_kwargs["context"]["requirements_doc"] == requirements

        assert not mock_bot.reply.called
        assert not mock_bot.log.called

    async def test_valid_blueprint_saves_formula_and_transitions_to_gate_2(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        patch_formula_dir: Path,
        mock_bot: MagicMock,
        mock_pool: MagicMock,
    ) -> None:
        """A valid Blueprint payload should persist the formula and move to GATE_2."""
        proposal_id = "prop-blueprint-02"
        title = "Formula Test Proposal"
        await insert_proposal(db, proposal_id, title=title)
        await insert_revision(db, proposal_id, rev_num=2)

        content = json.dumps(
            {
                "shipply": "transition",
                "proposal_id": proposal_id,
                "from": ProposalState.GATE_1.value,
                "to": ProposalState.BLUEPRINT.value,
            }
        )
        event = make_event(content)
        await blueprint.on_dm(event, mock_bot)

        formula_path = patch_formula_dir / f"shipply-{proposal_id}.formula.toml"
        assert formula_path.exists()
        formula_text = formula_path.read_text(encoding="utf-8")
        assert f'formula = "shipply-{proposal_id}"' in formula_text
        assert f"description = \"Blueprint for proposal {proposal_id}: {title}\"" in formula_text
        assert "[[bead_specs]]" in formula_text

        # Verify the formula is also persisted in the revisions table for Gate 2.
        cursor = await db.execute(
            "SELECT blueprint_doc FROM revisions WHERE proposal_id = ? ORDER BY rev_num DESC LIMIT 1",
            (proposal_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        assert row[0] == formula_text

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.GATE_2.value

        cursor = await db.execute(
            "SELECT event_type, payload FROM events WHERE proposal_id = ? AND event_type = ?",
            (proposal_id, "proposal_transition"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        payload = json.loads(row[1])
        assert payload["from_state"] == ProposalState.BLUEPRINT.value
        assert payload["to_state"] == ProposalState.GATE_2.value

        mock_bot.send_dm.assert_awaited_once()
        dm_call = mock_bot.send_dm.call_args
        assert dm_call.kwargs["recipient"] == "shipply-gate-2"
        dm_payload = json.loads(dm_call.kwargs["content"])
        assert dm_payload["shipply"] == "transition"
        assert dm_payload["proposal_id"] == proposal_id
        assert dm_payload["from"] == ProposalState.BLUEPRINT.value
        assert dm_payload["to"] == ProposalState.GATE_2.value

    async def test_invalid_blueprint_rejected_and_no_transition(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        patch_formula_dir: Path,
        mock_bot: MagicMock,
        monkeypatch,
    ) -> None:
        """A Blueprint payload missing required fields should be rejected and the state unchanged."""
        proposal_id = "prop-blueprint-03"
        await insert_proposal(db, proposal_id)
        await insert_revision(db, proposal_id, rev_num=2)

        invalid_payload = {"bead_specs": []}  # missing file_deltas, dependencies, step_ordering
        pool = MagicMock()
        pool.send = AsyncMock(
            return_value=HarnessResult(status="success", payload=invalid_payload)
        )
        monkeypatch.setattr(blueprint, "_get_pool", AsyncMock(return_value=pool))

        content = json.dumps(
            {
                "shipply": "transition",
                "proposal_id": proposal_id,
                "from": ProposalState.GATE_1.value,
                "to": ProposalState.BLUEPRINT.value,
            }
        )
        event = make_event(content)
        await blueprint.on_dm(event, mock_bot)

        mock_bot.reply.assert_called_once()
        reply_text = mock_bot.reply.call_args[0][1]
        assert "Blueprint validation failed" in reply_text
        assert "missing required fields" in reply_text

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.BLUEPRINT.value

        formula_path = patch_formula_dir / f"shipply-{proposal_id}.formula.toml"
        assert not formula_path.exists()

        mock_bot.send_dm.assert_not_called()

    async def test_blueprint_validated_against_schema_required_fields(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        patch_formula_dir: Path,
        mock_bot: MagicMock,
        monkeypatch,
    ) -> None:
        """The schema required fields should be enforced by the handler's validation."""
        proposal_id = "prop-blueprint-04"
        await insert_proposal(db, proposal_id)
        await insert_revision(db, proposal_id, rev_num=2)

        schema_path = blueprint.SCHEMA_PATH
        assert schema_path.exists()
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        required_fields = set(schema["required"])

        # Send a payload that omits one of the schema-required fields.
        for field in required_fields:
            payload = _valid_blueprint_payload()
            del payload[field]
            pool = MagicMock()
            pool.send = AsyncMock(return_value=HarnessResult(status="success", payload=payload))
            monkeypatch.setattr(blueprint, "_get_pool", AsyncMock(return_value=pool))

            content = json.dumps(
                {
                    "shipply": "transition",
                    "proposal_id": proposal_id,
                    "from": ProposalState.GATE_1.value,
                    "to": ProposalState.BLUEPRINT.value,
                }
            )
            event = make_event(content)
            await blueprint.on_dm(event, mock_bot)

            reply_text = mock_bot.reply.call_args[0][1]
            assert "Blueprint validation failed" in reply_text
            assert field in reply_text

            cursor = await db.execute(
                "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
            assert row[0] == ProposalState.BLUEPRINT.value

            mock_bot.reply.reset_mock()

    async def test_payload_with_wrong_types_rejected(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        patch_formula_dir: Path,
        mock_bot: MagicMock,
        monkeypatch,
    ) -> None:
        """Blueprint fields must be lists as required by the schema."""
        proposal_id = "prop-blueprint-05"
        await insert_proposal(db, proposal_id)
        await insert_revision(db, proposal_id, rev_num=2)

        payload = _valid_blueprint_payload()
        payload["step_ordering"] = "step-1"  # string, not list
        pool = MagicMock()
        pool.send = AsyncMock(return_value=HarnessResult(status="success", payload=payload))
        monkeypatch.setattr(blueprint, "_get_pool", AsyncMock(return_value=pool))

        content = json.dumps(
            {
                "shipply": "transition",
                "proposal_id": proposal_id,
                "from": ProposalState.GATE_1.value,
                "to": ProposalState.BLUEPRINT.value,
            }
        )
        event = make_event(content)
        await blueprint.on_dm(event, mock_bot)

        mock_bot.reply.assert_called_once()
        reply_text = mock_bot.reply.call_args[0][1]
        assert "step_ordering must be a list" in reply_text

        cursor = await db.execute(
            "SELECT state FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == ProposalState.BLUEPRINT.value


class TestBlueprintAmendments:
    """Tests for Gate 2 rebuild amendments."""

    async def test_amendments_loaded_and_passed_to_harness(
        self,
        db: aiosqlite.Connection,
        patch_config: None,
        patch_formula_dir: Path,
        mock_bot: MagicMock,
        mock_pool: MagicMock,
    ) -> None:
        """If a Gate 2 rebuild stored amendments, the harness context should include them."""
        proposal_id = "prop-blueprint-06"
        await insert_proposal(db, proposal_id)
        await insert_revision(db, proposal_id, rev_num=2)

        gate_id = str(uuid.uuid4())
        amendments = {"reason": "add tests", "tests": "yes"}
        await db.execute(
            "INSERT INTO gates (id, proposal_id, gate_type, status, votes, metadata) VALUES (?, ?, ?, ?, ?, ?)",
            (gate_id, proposal_id, "gate-2", "open", "{}", json.dumps({"amendments": amendments})),
        )
        await db.commit()

        content = json.dumps(
            {
                "shipply": "transition",
                "proposal_id": proposal_id,
                "from": ProposalState.BLUEPRINT.value,
                "to": ProposalState.BLUEPRINT.value,
            }
        )
        event = make_event(content)
        await blueprint.on_dm(event, mock_bot)

        mock_pool.send.assert_awaited_once()
        context = mock_pool.send.call_args.kwargs["context"]
        assert context["amendments"] == amendments
